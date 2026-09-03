"""Remote command worker for the delivery PWA.

The app-api command table owns command state and leases.  This module only
keeps the local facts that cannot leave the machine: which delivery programs
have a workspace here, and one stable worker id.  A command never supplies a
workspace path; it is resolved from that local mapping after the server has
atomically granted the lease.
"""

from __future__ import annotations

import json
import os
import platform
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import server as planner

from delivery_bridge import runtime
from delivery_bridge.errors import BridgeFailure
from delivery_bridge.git_ops import (
    git_branch_catalog,
    git_change_detail,
    git_change_files,
    git_create_branch_targets,
    git_initialize_submodules,
    git_initialize_workspace,
    git_merge_preview,
    git_workspace_check,
    git_workspace_projects,
    git_workspace_status,
)
from delivery_bridge.payloads import business_item_key_of, program_id_of
from delivery_bridge.providers import ai_provider_of


WORKSPACE_MAPPINGS_PATH = runtime.RUNTIME_DIR / "remote-worker-workspaces.json"
WORKER_STATE_PATH = runtime.RUNTIME_DIR / "remote-worker.json"
COMMAND_API_URL_ENV = "DELIVERY_COMMAND_API_URL"
WORKER_ACTIVITY_SECONDS = 20
WORKER_BACKOFF_SECONDS = 15
MAX_RESULT_BYTES = 480 * 1024
MAX_COMMAND_ATTACHMENT_COUNT = 5
MAX_COMMAND_ATTACHMENT_BYTES = 20 * 1024 * 1024
BUSINESS_POLL_SECONDS = 2
# 业务访谈回传的是给业务方看的整段回复，确认文档那一轮就是一篇完整业务文档。
# 任务类命令的 4096 字截断会把它切掉，所以按命令类型放宽单条文本上限。
COMMAND_TEXT_LIMITS = {"business.conversation": 60000}
DEFAULT_COMMAND_TEXT_LIMIT = 4096


# These are the only command names a remote client can cause the worker to
# execute.  In particular, no command maps to an arbitrary local HTTP route,
# Python attribute, or shell process.
COMMAND_CAPABILITIES = frozenset({
    "business.conversation",
    "task.execute",
    "task.execute-batch",
    "task.execute-sequence",
    "task.session",
    "task.conversation",
    "task.planning",
    "task.planning-session",
    "task.planning-stop",
    "requirement.usage",
    "task.stop",
    "task.stop-all",
    "git.status",
    "git.branches",
    "git.changes",
    "git.change",
    "git.projects",
    "git.merge-preview",
    "git.workspace-check",
    "git.init",
    "git.submodules",
    "git.branch",
    "git.prepare",
    "git.push",
    "git.merge",
    "documents.cloud-sync",
})


# 只读快照类命令走独立的领取通道：一条长任务占着执行通道时，工作台仍然能刷新
# 会话、Git 状态和改动列表。这些命令都不写工作目录，也不启动新的执行回合。
READ_ONLY_COMMAND_CAPABILITIES = frozenset({
    "task.session",
    "task.planning-session",
    # 只查用量：读会话表和本机的会话缓存，不动工作目录，也不起回合。
    "requirement.usage",
    "git.status",
    "git.branches",
    "git.changes",
    "git.change",
    "git.projects",
    "git.merge-preview",
    "git.workspace-check",
})


def command_api_url(value: str = "") -> str:
    """Return a configured app-api base URL, normalized to include ``/api``.

    The address is deployment configuration, never a browser or command input.
    Leaving it empty deliberately disables the background worker while keeping
    the existing loopback bridge fully usable.
    """
    raw = str(value or os.environ.get(COMMAND_API_URL_ENV, "")).strip().rstrip("/")
    if not raw:
        return ""
    parsed = urllib.parse.urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.query or parsed.fragment:
        raise BridgeFailure(f"{COMMAND_API_URL_ENV} 必须是有效的 http 或 https URL")
    return raw if raw.endswith("/api") else f"{raw}/api"


class WorkspaceMappingStore:
    """Persist local program-to-workspace mappings with owner-only permissions."""

    def __init__(self, path: Path = WORKSPACE_MAPPINGS_PATH) -> None:
        self.path = path
        self.lock = threading.Lock()

    def _read(self) -> dict[str, dict[str, Any]]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        except (OSError, json.JSONDecodeError):
            return {}
        mappings = raw.get("mappings") if isinstance(raw, dict) else None
        return mappings if isinstance(mappings, dict) else {}

    def _write(self, mappings: dict[str, dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps({"version": 1, "mappings": mappings}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.chmod(temp, 0o600)
        os.replace(temp, self.path)
        os.chmod(self.path, 0o600)

    def record(self, program_id: int, workspace: Path, biz_line: str) -> None:
        program_id = program_id_of(program_id)
        line = str(biz_line or "").strip()
        if not line or len(line) > 64:
            raise BridgeFailure("项目没有可用于远程 Worker 的业务线")
        try:
            resolved = workspace.resolve(strict=True)
        except OSError as exc:
            raise BridgeFailure(f"本机工作目录不存在：{workspace}") from exc
        if not resolved.is_dir():
            raise BridgeFailure("本机工作目录不是目录")
        with self.lock:
            mappings = self._read()
            mappings[str(program_id)] = {
                "programId": program_id,
                "workspace": str(resolved),
                "bizLine": line,
                "updatedAt": int(time.time()),
            }
            self._write(mappings)

    def get(self, program_id: int) -> dict[str, Any] | None:
        return next((entry for entry in self.snapshot() if entry["programId"] == program_id), None)

    def snapshot(self) -> list[dict[str, Any]]:
        valid: list[dict[str, Any]] = []
        changed = False
        with self.lock:
            mappings = self._read()
            for key, entry in mappings.items():
                if not isinstance(entry, dict):
                    changed = True
                    continue
                try:
                    program_id = program_id_of(entry.get("programId") or key)
                    workspace = Path(str(entry.get("workspace") or "")).expanduser().resolve(strict=True)
                except (BridgeFailure, OSError):
                    changed = True
                    continue
                biz_line = str(entry.get("bizLine") or "").strip()
                if not workspace.is_dir() or not biz_line:
                    changed = True
                    continue
                valid.append({
                    "programId": program_id,
                    "workspace": str(workspace),
                    "bizLine": biz_line,
                    "updatedAt": int(entry.get("updatedAt") or 0),
                })
            if changed:
                self._write({str(item["programId"]): item for item in valid})
        return sorted(valid, key=lambda item: (item["bizLine"], item["programId"]))


class CommandAPI:
    """Small app-api client; it intentionally shares only the user credential."""

    def __init__(self, base_url: str) -> None:
        self.base_url = command_api_url(base_url)

    def request(
        self,
        config: dict[str, Any],
        method: str,
        path: str,
        *,
        biz_line: str = "",
        body: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
        timeout: int = 30,
    ) -> Any:
        if not self.base_url:
            raise BridgeFailure(f"未配置 {COMMAND_API_URL_ENV}，远程命令 Worker 未启用")
        url = f"{self.base_url}{path}"
        values = {key: value for key, value in (query or {}).items() if value not in (None, "")}
        if values:
            url += "?" + urllib.parse.urlencode(values)
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            str(config.get("key_header") or "token"): str(config.get("key") or ""),
        }
        if biz_line:
            headers["X-Biz-Line"] = biz_line
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=payload, headers=headers, method=method), timeout=timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise BridgeFailure(f"远程命令接口返回 HTTP {exc.code}：{detail}") from exc
        except urllib.error.URLError as exc:
            raise BridgeFailure(f"无法连接远程命令接口：{exc.reason}") from exc
        except OSError as exc:
            raise BridgeFailure(f"远程命令接口请求失败：{exc}") from exc
        try:
            envelope = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise BridgeFailure("远程命令接口返回了非 JSON 内容") from exc
        if not isinstance(envelope, dict) or not envelope.get("success"):
            message = (envelope.get("error") or envelope.get("message")) if isinstance(envelope, dict) else "响应格式错误"
            raise BridgeFailure(f"远程命令接口请求失败：{message or '未知错误'}")
        return envelope.get("data")

    def download_attachment(
        self,
        config: dict[str, Any],
        biz_line: str,
        program_id: int,
        attachment_id: str,
    ) -> dict[str, Any]:
        """Download one app-api-owned upload after a command has been leased.

        The app API authenticates this request as the same user that submitted
        the command. The resulting bytes are written only through the already
        mapped local bridge attachment store.
        """
        if not self.base_url:
            raise BridgeFailure(f"未配置 {COMMAND_API_URL_ENV}，远程命令 Worker 未启用")
        value = str(attachment_id or "").strip()
        if not re.fullmatch(r"attachment-[a-f0-9]{32}", value):
            raise BridgeFailure("远程命令附件标识无效")
        url = f"{self.base_url}/workers/attachments/{urllib.parse.quote(value)}?" + urllib.parse.urlencode({"programId": program_id})
        headers = {
            "Accept": "application/octet-stream",
            str(config.get("key_header") or "token"): str(config.get("key") or ""),
            "X-Biz-Line": biz_line,
        }
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers, method="GET"), timeout=60) as response:
                data = response.read(MAX_COMMAND_ATTACHMENT_BYTES + 1)
                name = str(response.headers.get("X-Delivery-Attachment-Name") or "attachment")
                content_type = str(response.headers.get("Content-Type") or "application/octet-stream")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise BridgeFailure(f"下载远程命令附件失败（HTTP {exc.code}）：{detail}") from exc
        except urllib.error.URLError as exc:
            raise BridgeFailure(f"下载远程命令附件失败：{exc.reason}") from exc
        except OSError as exc:
            raise BridgeFailure(f"下载远程命令附件失败：{exc}") from exc
        if not data or len(data) > MAX_COMMAND_ATTACHMENT_BYTES:
            raise BridgeFailure("远程命令附件为空或超过 20 MB")
        return {"name": name, "contentType": content_type[:128], "data": data}


class CommandReporter:
    def __init__(self, api: CommandAPI, config: dict[str, Any], command: dict[str, Any], worker_id: str, lease_token: str) -> None:
        self.api = api
        self.config = config
        self.command = command
        self.worker_id = worker_id
        self.lease_token = lease_token
        self.cancelled = threading.Event()
        self.text_limit = COMMAND_TEXT_LIMITS.get(
            str(command.get("commandType") or "").strip().lower(), DEFAULT_COMMAND_TEXT_LIMIT,
        )
        self._last_report_at = 0.0

    @property
    def command_id(self) -> str:
        return str(self.command.get("commandId") or "")

    @property
    def biz_line(self) -> str:
        return str(self.command.get("bizLine") or "")

    def report(self, message: str, progress: int | None = None, data: dict[str, Any] | None = None, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_report_at < WORKER_ACTIVITY_SECONDS:
            return
        body: dict[str, Any] = {
            "workerId": self.worker_id,
            "leaseToken": self.lease_token,
            "message": _safe_text(message, None, 1024),
            "data": _safe_value(data or {}, None, self.text_limit),
        }
        if progress is not None:
            body["progress"] = max(0, min(100, int(progress)))
        response = self.api.request(
            self.config, "POST", f"/workers/commands/{self.command_id}/activity",
            biz_line=self.biz_line, body=body,
        )
        self._last_report_at = now
        if isinstance(response, dict) and response.get("cancelRequested"):
            self.cancelled.set()


class RemoteCommandWorker:
    """Poll app-api, resolve a local mapping, and adapt commands to ExecutionBridge."""

    def __init__(
        self,
        bridge: Any,
        *,
        mappings: WorkspaceMappingStore | None = None,
        api_url: str = "",
        worker_id: str = "",
    ) -> None:
        self.bridge = bridge
        self.mappings = mappings or WorkspaceMappingStore()
        self.api_url = command_api_url(api_url)
        self.worker_id = worker_id or self._worker_id()
        self.stop_event = threading.Event()
        self._running_lock = threading.Lock()
        self._running_command = ""
        self._reading_lock = threading.Lock()
        self._reading_command = ""
        self._registered_identity: tuple[str, tuple[tuple[str, tuple[int, ...]], ...]] | None = None
        self.last_error = ""
        self.last_registered_at = 0
        self._last_state = ""

    @staticmethod
    def _worker_id() -> str:
        try:
            state = json.loads(WORKER_STATE_PATH.read_text(encoding="utf-8")) if WORKER_STATE_PATH.exists() else {}
        except (OSError, json.JSONDecodeError):
            state = {}
        existing = str(state.get("workerId") or "") if isinstance(state, dict) else ""
        if re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", existing):
            return existing
        worker_id = f"worker-{secrets.token_hex(10)}"
        WORKER_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        temp = WORKER_STATE_PATH.with_suffix(".tmp")
        temp.write_text(json.dumps({"workerId": worker_id}, indent=2) + "\n", encoding="utf-8")
        os.chmod(temp, 0o600)
        os.replace(temp, WORKER_STATE_PATH)
        os.chmod(WORKER_STATE_PATH, 0o600)
        return worker_id

    def status(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.api_url),
            "workerId": self.worker_id,
            "workspaceMappings": [
                {"programId": item["programId"], "bizLine": item["bizLine"], "workspace": Path(item["workspace"]).name}
                for item in self.mappings.snapshot()
            ],
            "runningCommand": self._running_command,
            "readingCommand": self._reading_command,
            "lastError": self.last_error,
            "lastRegisteredAt": self.last_registered_at,
        }

    def _log_state(self, state: str, message: str) -> None:
        """只在状态发生变化时打印，别把 15 秒一轮的空转刷成日志噪音。"""
        if self._last_state == state:
            return
        self._last_state = state
        print(message, flush=True)

    def stop(self) -> None:
        self.stop_event.set()

    def run_forever(self) -> None:
        # 禁用和坏掉在日志里必须长得不一样。没有这一行时，一个漏配
        # DELIVERY_COMMAND_API_URL 的 bridge 会安静地空转，面板上只剩一句
        # 「未登记执行电脑」，日志里一条线索都没有。
        if self.api_url:
            print(f"远程命令 Worker 已启用：{self.api_url}（worker {self.worker_id}）", flush=True)
        else:
            print(
                "远程命令 Worker 未启用：没有配置 DELIVERY_COMMAND_API_URL，也没有传 --command-api-url。"
                "任务面板会一直显示「未登记执行电脑」，本机回环接口不受影响。",
                flush=True,
            )
        threading.Thread(target=self._read_forever, daemon=True, name="delivery-remote-worker-read").start()
        while not self.stop_event.is_set():
            try:
                self.run_once(wait_seconds=20)
                self.last_error = ""
            except Exception as exc:
                self.last_error = _safe_text(str(exc), None, 1024)
                print(f"远程命令 Worker 暂时不可用：{self.last_error}", flush=True)
                self.stop_event.wait(WORKER_BACKOFF_SECONDS)

    def _read_forever(self) -> None:
        """只读通道：与执行通道并行领取快照命令，不参与本机写操作。"""
        while not self.stop_event.is_set():
            try:
                self.run_once(wait_seconds=20, command_types=sorted(READ_ONLY_COMMAND_CAPABILITIES))
            except Exception as exc:
                print(f"远程命令只读通道暂时不可用：{_safe_text(str(exc), None, 1024)}", flush=True)
                self.stop_event.wait(WORKER_BACKOFF_SECONDS)

    def run_once(self, wait_seconds: int = 20, command_types: list[str] | None = None) -> bool:
        if not self.api_url:
            self.stop_event.wait(WORKER_BACKOFF_SECONDS)
            return False
        config = planner.load_config()
        mappings = self.mappings.snapshot()
        if not mappings:
            # 同样别静默：没有映射时 Worker 一次心跳都不会发，面板上的表现和
            # 插件没开完全一样，只有这行日志能把两者分开。
            self._log_state("no-mappings", "远程命令 Worker 空转：本机还没有登记任何项目工作目录映射，不会注册也不会领命令。")
            self.stop_event.wait(WORKER_BACKOFF_SECONDS)
            return False
        self._log_state("mapped", f"远程命令 Worker 已登记 {len(mappings)} 个项目工作目录映射。")
        api = CommandAPI(self.api_url)
        self._register(api, config, mappings)
        claim_body: dict[str, Any] = {"workerId": self.worker_id}
        if command_types:
            claim_body["commandTypes"] = list(command_types)
        claimed = api.request(
            config, "POST", "/workers/commands/claim",
            body=claim_body, query={"waitSeconds": max(1, min(25, int(wait_seconds)))}, timeout=max(30, wait_seconds + 10),
        )
        if not isinstance(claimed, dict) or not isinstance(claimed.get("command"), dict):
            return False
        command = claimed["command"]
        lease_token = str(claimed.get("leaseToken") or "")
        if not lease_token:
            raise BridgeFailure("远程命令领取响应缺少租约")
        mapping = self.mappings.get(program_id_of(command.get("programId")))
        if mapping is None or mapping.get("bizLine") != command.get("bizLine"):
            # The server should never return this after registration.  Still complete
            # the lease explicitly so a stale mapping cannot run a wrong project.
            reporter = CommandReporter(api, config, command, self.worker_id, lease_token)
            self._complete(reporter, "failed", {}, "本机项目工作目录映射已失效，未执行命令")
            return True
        self._run_claimed(api, config, command, lease_token, mapping, read_only=bool(command_types))
        return True

    def _register(self, api: CommandAPI, config: dict[str, Any], mappings: list[dict[str, Any]]) -> None:
        by_biz_line: dict[str, list[int]] = {}
        for mapping in mappings:
            by_biz_line.setdefault(str(mapping["bizLine"]), []).append(int(mapping["programId"]))
        fingerprint = tuple(sorted((line, tuple(sorted(set(ids)))) for line, ids in by_biz_line.items()))
        identity = (str(config.get("key") or ""), fingerprint)
        now = int(time.time())
        if identity != self._registered_identity or now - self.last_registered_at >= 60:
            display = f"{platform.node() or 'delivery'} remote worker"
            for biz_line, program_ids in by_biz_line.items():
                api.request(
                    config, "POST", "/workers/register", biz_line=biz_line,
                    body={
                        "workerId": self.worker_id,
                        "displayName": display[:128],
                        "capabilities": sorted(COMMAND_CAPABILITIES),
                        "programIds": sorted(set(program_ids)),
                    },
                )
            self._registered_identity = identity
            self.last_registered_at = now
        for biz_line in by_biz_line:
            api.request(config, "POST", "/workers/heartbeat", biz_line=biz_line, body={"workerId": self.worker_id})

    def _run_claimed(
        self,
        api: CommandAPI,
        config: dict[str, Any],
        command: dict[str, Any],
        lease_token: str,
        mapping: dict[str, Any],
        *,
        read_only: bool = False,
    ) -> None:
        reporter = CommandReporter(api, config, command, self.worker_id, lease_token)
        command_id = reporter.command_id
        lock = self._reading_lock if read_only else self._running_lock
        with lock:
            if read_only:
                if self._reading_command:
                    raise BridgeFailure("Worker 只读通道已有正在执行的命令")
                self._reading_command = command_id
            else:
                if self._running_command:
                    raise BridgeFailure("Worker 已有正在运行的远程命令")
                self._running_command = command_id
        try:
            reporter.report("Worker 已在本机工作目录开始执行命令", 1, {"commandType": command.get("commandType")}, force=True)
            workspace_bridge = self.bridge.for_workspace(mapping["workspace"])
            cancellation_sent = False
            with ThreadPoolExecutor(max_workers=1, thread_name_prefix="delivery-remote-command") as pool:
                future = pool.submit(self._dispatch, command, mapping, config, reporter)
                while not future.done():
                    self._relay_progress(reporter, mapping)
                    if reporter.cancelled.is_set() and not cancellation_sent:
                        self._best_effort_cancel(command, workspace_bridge, config)
                        cancellation_sent = True
                    self.stop_event.wait(2)
                result = future.result()
            if reporter.cancelled.is_set():
                self._complete(reporter, "cancelled", result, "用户请求取消，Worker 已尽力停止本机操作")
            else:
                self._complete(reporter, "succeeded", result, "")
        except Exception as exc:
            self._complete(reporter, "cancelled" if reporter.cancelled.is_set() else "failed", {}, str(exc))
        finally:
            with lock:
                if read_only:
                    self._reading_command = ""
                else:
                    self._running_command = ""

    def _relay_progress(self, reporter: CommandReporter, mapping: dict[str, Any]) -> None:
        command_type = str(reporter.command.get("commandType") or "")
        if command_type.startswith("business."):
            # 业务访谈自己按轮次回传会话快照，服务端读的是最近一条活动。
            # 这里再插一条通用进度会把那份快照顶掉，让访谈界面回退成空白。
            return
        input_value = reporter.command.get("input") if isinstance(reporter.command.get("input"), dict) else {}
        item_key = str(input_value.get("itemKey") or "").strip()
        if command_type.startswith("task.") and item_key:
            identity = ("", int(reporter.command["programId"]), item_key)
            events = self.bridge.for_workspace(mapping["workspace"]).progress.snapshot(identity)
            if events:
                event = events[-1]
                progress = 10 if str(event.get("status") or "") == "running" else 90
                reporter.report(str(event.get("title") or "任务正在执行"), progress, {
                    "kind": str(event.get("kind") or "status"),
                    "status": str(event.get("status") or "running"),
                })
                return
        reporter.report("Worker 正在执行本机操作", 10, {"commandType": command_type})

    def _dispatch(
        self,
        command: dict[str, Any],
        mapping: dict[str, Any],
        config: dict[str, Any],
        reporter: CommandReporter,
    ) -> dict[str, Any]:
        command_type = str(command.get("commandType") or "").strip().lower()
        if command_type not in COMMAND_CAPABILITIES:
            raise BridgeFailure(f"Worker 不支持远程命令类型：{command_type}")
        input_value = command.get("input")
        if not isinstance(input_value, dict):
            raise BridgeFailure("远程命令输入必须是 JSON 对象")
        program_id = program_id_of(command.get("programId"))
        workspace_bridge = self.bridge.for_workspace(mapping["workspace"])
        task_config = {
            **config,
            "_project_id": program_id,
            "_biz_line": str(mapping["bizLine"]),
        }
        if command_type == "business.conversation":
            return self._dispatch_business(input_value, program_id, reporter)
        if command_type.startswith("task."):
            return self._dispatch_task(command_type, input_value, workspace_bridge, task_config, program_id, reporter)
        if command_type == "documents.cloud-sync":
            return workspace_bridge.sync_cloud_workspace(program_id, task_config)
        return self._dispatch_git(command_type, input_value, workspace_bridge, task_config, program_id)

    def _dispatch_business(
        self,
        value: dict[str, Any],
        program_id: int,
        reporter: CommandReporter,
    ) -> dict[str, Any]:
        """Run one server-raised business interview turn.

        An interview has no delivery task and no project checkout: its workspace
        is the logical business path the Go service chose, which
        for_business_workspace resolves under this machine's controlled business
        root. The mapped project directory is deliberately not used here, and the
        command still carries no absolute path of its own.
        """
        workspace = str(value.get("workspace") or "").strip()
        if not workspace:
            raise BridgeFailure("业务访谈命令缺少工作目录")
        bridge = self.bridge.for_business_workspace(workspace)
        item_key = business_item_key_of(value.get("itemKey"))
        provider = ai_provider_of(value.get("provider") or "codex")
        payload = {key: item for key, item in value.items() if key != "workspace"}
        payload["programId"] = program_id
        payload["itemKey"] = item_key
        payload["attachmentIds"] = self._business_attachments(bridge, value, program_id, item_key, reporter)
        action = bridge.send_business_conversation(payload)
        if not action.get("accepted"):
            raise BridgeFailure("本机插件未接受业务访谈请求")
        thread_id = str(action.get("threadId") or "").strip()
        turn_id = str(action.get("turnId") or "").strip()
        # 先把线程标识回传：服务端的 Start 正阻塞等它，而且业务方的下一轮
        # 还要靠这个标识续上同一个 Codex 会话，晚回传就会分叉成新会话。
        reporter.report("业务访谈已在本机开始", 5, {"threadId": thread_id, "turnId": turn_id}, force=True)
        published = ""
        while True:
            conversation = bridge.business_conversation(program_id, item_key, thread_id, provider)
            thread_id = str(conversation.get("threadId") or thread_id).strip()
            snapshot = {
                "threadId": thread_id,
                "turnId": turn_id,
                "conversation": _business_turn_only(conversation, turn_id),
            }
            active = bool(conversation.get("active"))
            if not active or reporter.cancelled.is_set():
                return snapshot
            # 每条活动都会在服务端留一行事件，而模型思考期间快照可以连着几十秒
            # 不变。只在真正有新内容时回传，长访谈才不会堆出成百条一样的快照。
            current = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
            if current != published:
                reporter.report("业务访谈正在进行", 50, snapshot, force=True)
                published = current
            self.stop_event.wait(BUSINESS_POLL_SECONDS)
            if self.stop_event.is_set():
                raise BridgeFailure("Worker 正在退出，业务访谈未能完成")

    def _business_attachments(
        self,
        bridge: Any,
        value: dict[str, Any],
        program_id: int,
        item_key: str,
        reporter: CommandReporter,
    ) -> list[str]:
        """Materialise app-api-held uploads into the business workspace.

        The console uploads to the server, not to this machine, so the ids in the
        command are app-api ids. They only become usable once the bytes are in the
        business workspace under the bridge's own attachment ids.
        """
        attachment_ids = value.get("attachmentIds") or []
        if not isinstance(attachment_ids, list):
            raise BridgeFailure("业务访谈附件标识必须是数组")
        if not attachment_ids:
            return []
        if len(attachment_ids) > MAX_COMMAND_ATTACHMENT_COUNT:
            raise BridgeFailure(f"一条消息最多携带 {MAX_COMMAND_ATTACHMENT_COUNT} 个附件")
        uploads = [
            reporter.api.download_attachment(reporter.config, reporter.biz_line, program_id, str(attachment_id))
            for attachment_id in attachment_ids
        ]
        stored = bridge.save_business_attachments(program_id, item_key, uploads)
        attachments = stored.get("attachments") if isinstance(stored, dict) else None
        if not isinstance(attachments, list) or len(attachments) != len(uploads):
            raise BridgeFailure("业务访谈附件写入本机工作目录失败")
        return [str(item.get("id") or "") for item in attachments]

    def _dispatch_task(
        self,
        command_type: str,
        value: dict[str, Any],
        bridge: Any,
        config: dict[str, Any],
        program_id: int,
        reporter: CommandReporter,
    ) -> dict[str, Any]:
        payload = {key: item for key, item in value.items() if key != "workspace"}
        payload["programId"] = program_id
        payload["bizLine"] = config["_biz_line"]
        if command_type == "task.execute":
            item_key = _item_key(payload)
            payload["task"] = bridge._task_detail(config, program_id, item_key)
            result = bridge.execute(payload, config=config)
            self._wait_for_task(bridge, config, program_id, item_key, reporter)
            result["task"] = bridge._task_detail(config, program_id, item_key)
            result["cloudSync"] = _best_effort_sync(bridge, program_id, config)
            return result
        if command_type == "requirement.usage":
            return bridge.requirement_usage(
                program_id, str(payload.get("requirementKey") or ""), config=config,
            )
        if command_type == "task.session":
            item_key = _item_key(payload)
            return bridge.conversation(
                program_id,
                item_key,
                str(payload.get("threadId") or ""),
                config=config,
                provider=str(payload.get("provider") or "codex"),
            )
        if command_type == "task.conversation":
            attachment_ids = payload.get("attachmentIds") or []
            if not isinstance(attachment_ids, list) or len(attachment_ids) > MAX_COMMAND_ATTACHMENT_COUNT:
                raise BridgeFailure(f"一条消息最多携带 {MAX_COMMAND_ATTACHMENT_COUNT} 个附件")
            if attachment_ids:
                uploads = [
                    reporter.api.download_attachment(reporter.config, reporter.biz_line, program_id, str(attachment_id))
                    for attachment_id in attachment_ids
                ]
                stored = bridge.upload_conversation_attachments(
                    str(config["_biz_line"]), program_id, _item_key(payload), uploads, config,
                )
                attachments = stored.get("attachments") if isinstance(stored, dict) else None
                if not isinstance(attachments, list) or len(attachments) != len(uploads):
                    raise BridgeFailure("远程命令附件写入本机工作目录失败")
                payload["attachmentIds"] = [str(item.get("id") or "") for item in attachments]
            item_key = _item_key(payload)
            result = bridge.send_conversation(payload, config=config)
            self._wait_for_task(bridge, config, program_id, item_key, reporter)
            result["task"] = bridge._task_detail(config, program_id, item_key)
            result["cloudSync"] = _best_effort_sync(bridge, program_id, config)
            return result
        if command_type == "task.planning-session":
            requirement_key = _requirement_key(payload)
            return bridge.planning(
                program_id,
                selected_thread_id=str(payload.get("threadId") or ""),
                config=config,
                requirement_key=requirement_key,
                provider=str(payload.get("provider") or "codex"),
            )
        if command_type == "task.planning-stop":
            return bridge.stop_planning(payload, config=config)
        if command_type == "task.planning":
            requirement_key = _requirement_key(payload)
            # 移动端只知道需求键：拆解会话要的需求正文和阶段开关由 Worker 从任务面板补齐。
            payload = self._planning_payload(bridge, config, program_id, requirement_key, payload, reporter)
            result = bridge.send_planning(payload, config=config)
            self._wait_for_planning(bridge, program_id, requirement_key, reporter)
            thread_id = str(result.get("threadId") or "")
            result["planning"] = bridge.planning(
                program_id,
                selected_thread_id=thread_id,
                config=config,
                requirement_key=requirement_key,
            )
            return result
        if command_type == "task.execute-batch":
            result = bridge.execute_batch(payload, config=config)
            self._wait_for_batch(bridge, str(result.get("batchId") or ""), reporter)
            result["cloudSync"] = _best_effort_sync(bridge, program_id, config)
            return result
        if command_type == "task.execute-sequence":
            result = bridge.execute_sequence(payload, config=config)
            self._wait_for_batch(bridge, str(result.get("sequenceId") or ""), reporter, sequence=True)
            result["cloudSync"] = _best_effort_sync(bridge, program_id, config)
            return result
        if command_type == "task.stop":
            return bridge.stop_conversation(payload, config=config)
        if command_type == "task.stop-all":
            return bridge.stop_all_executions(payload, config=config)
        raise BridgeFailure(f"Worker 不支持远程命令类型：{command_type}")

    def _dispatch_git(self, command_type: str, value: dict[str, Any], bridge: Any, config: dict[str, Any], program_id: int) -> dict[str, Any]:
        workspace = bridge.workspace
        if command_type == "git.status":
            return git_workspace_status(workspace, str(value.get("expectedRemoteUrl") or ""), str(value.get("remoteName") or "origin"))
        if command_type == "git.branches":
            return git_branch_catalog(workspace)
        if command_type == "git.changes":
            return git_change_files(workspace)
        if command_type == "git.change":
            return git_change_detail(workspace, str(value.get("path") or ""))
        if command_type == "git.projects":
            return git_workspace_projects(workspace, str(value.get("branch") or ""), str(value.get("remoteName") or "origin"))
        if command_type == "git.merge-preview":
            sources = [str(item).strip() for item in value.get("sources") or [] if str(item).strip()]
            return git_merge_preview(workspace, str(value.get("target") or ""), sources, str(value.get("remoteName") or "origin"))
        if command_type == "git.workspace-check":
            return git_workspace_check(workspace)
        if command_type == "git.init":
            return git_initialize_workspace(workspace, str(value.get("repositoryUrl") or ""), str(value.get("remoteName") or "origin"), str(value.get("baseBranch") or ""))
        if command_type == "git.submodules":
            return git_initialize_submodules(workspace)
        if command_type == "git.branch":
            return git_create_branch_targets(
                workspace, str(value.get("baseBranch") or ""), str(value.get("branch") or ""),
                [str(item).strip() for item in value.get("targets") or [] if str(item).strip()], bool(value.get("skipRoot")),
            )
        if command_type == "git.prepare":
            return bridge.prepare_requirement_git_branch({"programId": program_id, **value})
        if command_type == "git.push":
            return bridge.push_requirement_branch({"programId": program_id, **value}, config=config)
        if command_type == "git.merge":
            return bridge.merge_time_plan_branches({"programId": program_id, **value}, config=config)
        raise BridgeFailure(f"Worker 不支持远程命令类型：{command_type}")

    def _wait_for_task(self, bridge: Any, config: dict[str, Any], program_id: int, item_key: str, reporter: CommandReporter) -> None:
        identity = ("", program_id, item_key)
        while True:
            with bridge.lock:
                active = identity in bridge.active
            if not active:
                return
            self._relay_progress(reporter, {"workspace": str(bridge.workspace)})
            self.stop_event.wait(1)

    def _planning_payload(
        self,
        bridge: Any,
        config: dict[str, Any],
        program_id: int,
        requirement_key: str,
        payload: dict[str, Any],
        reporter: CommandReporter,
    ) -> dict[str, Any]:
        """Fill a planning turn's requirement context and land its attachments locally."""
        requirement = planner.request_api(
            config, "GET", "/delivery/requirement",
            query={"programId": program_id, "requirementKey": requirement_key},
        )
        if not isinstance(requirement, dict) or str(requirement.get("requirementKey") or "") != requirement_key:
            raise BridgeFailure("需求不存在或无法读取")
        enriched = dict(payload)
        enriched.setdefault("requirementName", str(requirement.get("name") or ""))
        enriched.setdefault("requirementDetail", str(requirement.get("detail") or ""))
        enriched.setdefault("requirementStartPhase", str(requirement.get("startPhase") or "requirement"))
        enriched.setdefault("requirementSplitTasks", bool(requirement.get("splitTasks", True)))
        enriched.setdefault("requirementPreGenerateTaskDocuments", bool(requirement.get("preGenerateTaskDocuments")))
        enriched.setdefault("requirementGeneratePrototype", bool(requirement.get("generatePrototype")))
        attachment_ids = enriched.get("attachmentIds") or []
        if not isinstance(attachment_ids, list) or len(attachment_ids) > MAX_COMMAND_ATTACHMENT_COUNT:
            raise BridgeFailure(f"一条消息最多携带 {MAX_COMMAND_ATTACHMENT_COUNT} 个附件")
        if attachment_ids:
            uploads = [
                reporter.api.download_attachment(reporter.config, reporter.biz_line, program_id, str(attachment_id))
                for attachment_id in attachment_ids
            ]
            stored = bridge.upload_conversation_attachments(
                str(config["_biz_line"]), program_id, bridge._planning_item_key(requirement_key), uploads, config,
            )
            attachments = stored.get("attachments") if isinstance(stored, dict) else None
            if not isinstance(attachments, list) or len(attachments) != len(uploads):
                raise BridgeFailure("远程命令附件写入本机工作目录失败")
            enriched["attachmentIds"] = [str(item.get("id") or "") for item in attachments]
        return enriched

    def _wait_for_planning(self, bridge: Any, program_id: int, requirement_key: str, reporter: CommandReporter) -> None:
        identity = bridge._planning_identity(program_id, requirement_key)
        while True:
            with bridge.lock:
                active = identity in bridge.active
            if not active:
                return
            reporter.report("Worker 正在生成需求拆解", 50, {"requirementKey": requirement_key})
            self.stop_event.wait(1)

    def _wait_for_batch(self, bridge: Any, batch_id: str, reporter: CommandReporter, *, sequence: bool = False) -> None:
        if not batch_id:
            return
        while True:
            with bridge.lock:
                active = batch_id in bridge.active_sequences if sequence else batch_id in bridge.batch_satisfied
            if not active:
                return
            reporter.report("Worker 正在等待本机执行批次完成", 50, {"batchId": batch_id})
            self.stop_event.wait(1)

    @staticmethod
    def _best_effort_cancel(command: dict[str, Any], bridge: Any, config: dict[str, Any]) -> None:
        value = command.get("input") if isinstance(command.get("input"), dict) else {}
        command_type = str(command.get("commandType") or "")
        try:
            if command_type in {"task.execute", "task.conversation"}:
                item_key = str(value.get("itemKey") or "").strip()
                if item_key:
                    bridge.stop_conversation({"programId": command.get("programId"), "itemKey": item_key}, config=config)
            elif command_type in {"task.planning", "task.planning-stop"}:
                requirement_key = str(value.get("requirementKey") or "").strip()
                if requirement_key:
                    bridge.stop_planning({"programId": command.get("programId"), "requirementKey": requirement_key}, config=config)
            elif command_type in {"task.execute-batch", "task.execute-sequence"}:
                bridge.stop_all_executions({"programId": command.get("programId")}, config=config)
        except Exception:
            # A completed or already interrupted task is a valid best-effort
            # cancellation outcome. The terminal command state tells the user it
            # was requested even when there was no active local turn left.
            return

    def _complete(self, reporter: CommandReporter, state: str, result: dict[str, Any], error: str) -> None:
        safe_result = _bounded_result(_safe_value(result, None, reporter.text_limit))
        try:
            reporter.api.request(
                reporter.config, "POST", f"/workers/commands/{reporter.command_id}/complete",
                biz_line=reporter.biz_line,
                body={
                    "workerId": reporter.worker_id,
                    "leaseToken": reporter.lease_token,
                    "state": state,
                    "result": safe_result,
                    "errorMessage": _safe_text(error, None, 1024),
                },
            )
        except Exception as exc:
            self.last_error = _safe_text(f"回传命令结果失败：{exc}", None, 1024)
            print(self.last_error, flush=True)


def _business_turn_only(conversation: dict[str, Any], turn_id: str) -> dict[str, Any]:
    """Keep the running turn and drop the thread's history.

    A command activity is capped at 64 KB server-side, and a business thread
    accumulates every earlier turn. The server only projects the turn it asked
    about, so sending the rest would buy nothing and eventually overflow.
    """
    turns = conversation.get("turns")
    if not isinstance(turns, list):
        turns = []
    selected = [turn for turn in turns if isinstance(turn, dict) and str(turn.get("id") or "") == turn_id]
    if not selected and turns:
        selected = [turns[-1]]
    return {
        "threadId": str(conversation.get("threadId") or ""),
        "active": bool(conversation.get("active")),
        "turns": selected,
    }


def _item_key(value: dict[str, Any]) -> str:
    item_key = str(value.get("itemKey") or "").strip()
    if not item_key or len(item_key) > 64:
        raise BridgeFailure("远程任务命令缺少有效 itemKey")
    return item_key


def _requirement_key(value: dict[str, Any]) -> str:
    requirement_key = str(value.get("requirementKey") or "").strip()
    if not requirement_key or len(requirement_key) > 64:
        raise BridgeFailure("远程拆解命令缺少有效 requirementKey")
    return requirement_key


def _best_effort_sync(bridge: Any, program_id: int, config: dict[str, Any]) -> dict[str, Any]:
    try:
        return bridge.sync_cloud_workspace(program_id, config)
    except Exception as exc:
        return {"enabled": False, "error": _safe_text(str(exc), bridge.workspace)}


def _safe_text(value: Any, workspace: Path | None, limit: int = 4096) -> str:
    text = str(value or "")
    if workspace is not None:
        text = text.replace(str(workspace), ".")
    # Do not send an absolute local path to app-api, including one embedded in a
    # command error. Project-relative paths remain useful to the PWA.
    text = re.sub(r"(?<![A-Za-z0-9])(?:[A-Za-z]:[\\/]|/|\\\\)[^\s\"']+", "<local-path>", text)
    return text.strip()[:limit]


def _safe_value(value: Any, workspace: Path | None, limit: int = DEFAULT_COMMAND_TEXT_LIMIT) -> Any:
    if isinstance(value, dict):
        safe: dict[str, Any] = {}
        for key, item in value.items():
            # Existing Git helpers include this for loopback callers; app-api must
            # only learn that a mapped workspace was used, not its local path.
            if str(key).lower() in {"workspace", "repositoryroot"}:
                safe[f"{key}Name"] = workspace.name if workspace is not None else "local-workspace"
            else:
                safe[str(key)] = _safe_value(item, workspace, limit)
        return safe
    if isinstance(value, list):
        return [_safe_value(item, workspace, limit) for item in value]
    if isinstance(value, str):
        return _safe_text(value, workspace, limit)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _safe_text(value, workspace, limit)


def _bounded_result(value: Any) -> dict[str, Any]:
    result = value if isinstance(value, dict) else {"value": value}
    encoded = json.dumps(result, ensure_ascii=False).encode("utf-8")
    if len(encoded) <= MAX_RESULT_BYTES:
        return result
    return {
        "truncated": True,
        "message": "本机结果超过远程命令存储上限，已截断；请在执行电脑查看完整结果。",
    }
