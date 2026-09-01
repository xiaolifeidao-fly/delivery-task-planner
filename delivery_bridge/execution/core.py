"""执行桥的骨架：构造、按工作目录取实例，以及所有领域共用的那几件事。

任务认领、批次记账、队列取消标记、与任务面板的重试请求都在这里；
具体某个阶段怎么跑，在各自的 Mixin 里。
"""

from __future__ import annotations

import shutil
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import server as planner

from delivery_bridge.artifacts import ConversationAttachmentStore, WorkspaceArtifactStore
from delivery_bridge.clients import factory
from delivery_bridge.clients.pool import THREAD_READERS
from delivery_bridge.codex_cli import available_codex_cli
from delivery_bridge.errors import BridgeFailure
from delivery_bridge.executor_env import codex_environment
from delivery_bridge.payloads import assert_runtime_project, scoped_config
from delivery_bridge.providers import (
    CODEX_MODEL_CATALOG,
    DEFAULT_BIZ_LINE,
    ai_provider_of,
    program_id_of,
    provider_label,
)
from delivery_bridge.stores import PendingSessionSyncStore, ProgressStore
from delivery_bridge.workspaces import (
    DEFAULT_BUSINESS_WORKSPACE_ROOT,
    business_workspace_path_of,
    placeholder_workspace,
    workspace_path_of,
)


if TYPE_CHECKING:
    from . import ExecutionBridge


class CoreMixin:
    def __init__(
        self,
        workspace: Path,
        progress: ProgressStore | None = None,
        pending_session_syncs: PendingSessionSyncStore | None = None,
        business_workspace_root: Path | None = None,
    ):
        self.workspace = workspace.resolve()
        self.active: set[tuple[str, int, str]] = set()
        self.active_runs: dict[tuple[str, int, str], dict[str, Any]] = {}
        self.active_sequences: set[str] = set()
        self.sequence_tasks: set[tuple[int, str]] = set()
        self.batch_tasks: set[tuple[int, str]] = set()
        # Queue-local dependency overrides are only used after a completed
        # review marks an interrupted task as ignorable. They never affect a
        # direct task execution request.
        self.sequence_satisfied: dict[str, set[str]] = {}
        self.batch_satisfied: dict[str, set[str]] = {}
        # 用户在任务进度里点「全部停止」后，队列线程要能在下一个检查点自己收摊：
        # 中断当前回合只结束正在跑的那一条，后面排队的任务得靠这两张表拦住。
        self.queue_programs: dict[str, int] = {}
        self.cancelled_queues: set[str] = set()
        self.lock = threading.Lock()
        self.progress = progress or ProgressStore()
        self.pending_session_syncs = pending_session_syncs or PendingSessionSyncStore()
        self.business_workspace_root = (business_workspace_root or DEFAULT_BUSINESS_WORKSPACE_ROOT).expanduser().resolve()
        self.attachments = ConversationAttachmentStore(self.workspace)
        self.artifacts = WorkspaceArtifactStore(self.workspace)
        self.workspace_bridges: dict[str, "ExecutionBridge"] = {str(self.workspace): self}
        self.workspace_bridges_lock = threading.Lock()

    def for_workspace(self, value: Any) -> ExecutionBridge:
        workspace = workspace_path_of(value)
        key = str(workspace)
        with self.workspace_bridges_lock:
            existing = self.workspace_bridges.get(key)
            if existing is not None:
                return existing
            bridge = type(self)(workspace, self.progress, self.pending_session_syncs, self.business_workspace_root)
            self.workspace_bridges[key] = bridge
            return bridge

    def for_business_workspace(self, value: Any) -> ExecutionBridge:
        workspace = business_workspace_path_of(value, self.business_workspace_root)
        key = str(workspace)
        with self.workspace_bridges_lock:
            existing = self.workspace_bridges.get(key)
            if existing is not None:
                return existing
            bridge = type(self)(workspace, self.progress, self.pending_session_syncs, self.business_workspace_root)
            self.workspace_bridges[key] = bridge
            return bridge

    def _release_active_run(self, identity: str) -> dict[str, Any] | None:
        """回合结束就顺手丢掉这条线程的只读快照。

        活跃期正文来自这一路自己的 client，不经过只读池；一旦收尾，面板下一轮
        就会改走池子，这里主动失效可以避免它多等一个 TTL 才看到收尾内容。
        """
        entry = self.active_runs.pop(identity, None)
        THREAD_READERS.invalidate(str((entry or {}).get("threadId") or ""))
        return entry

    def models(self, config: dict[str, Any], provider: str = "codex") -> dict[str, Any]:
        program_id = program_id_of(config.get("_project_id"))
        assert_runtime_project(config, program_id)
        if provider == "codex":
            return {"defaultModel": "gpt-5.6-terra", "models": list(CODEX_MODEL_CATALOG)}
        client = factory.create_ai_client(provider, self.workspace, environment=codex_environment(config, program_id))
        try:
            models = []
            for item in client.list_models():
                model = str(item.get("model") or "").strip()
                if not model or item.get("hidden"):
                    continue
                models.append({
                    "model": model,
                    "displayName": str(item.get("displayName") or model),
                    "description": str(item.get("description") or ""),
                })
            return {"defaultModel": "", "models": models}
        finally:
            client.close()

    def health(self, provider: str = "codex") -> dict[str, Any]:
        provider = ai_provider_of(provider)
        codex_command = available_codex_cli()
        claude_cli = shutil.which("claude")
        executable_available = bool(codex_command) if provider == "codex" else claude_cli is not None
        configured = True
        api_reachable = True
        message = "ready"
        if not executable_available:
            message = f"未找到 {provider_label(provider)} CLI"
        ready = executable_available and configured and api_reachable
        return {
            "ready": ready,
            "bridge": True,
            "codex": bool(codex_command),
            "claude": claude_cli is not None,
            "configured": configured,
            "apiReachable": api_reachable,
            "executorType": provider,
            # 占位目录不是任何项目的仓库，别把它当成"当前工作区"报给面板。
            "workspace": "" if self.workspace == placeholder_workspace() else self.workspace.name,
            "message": message,
            "checkedAt": int(time.time()),
        }

    def active_run_count(self) -> int:
        """Count in-flight runs across every workspace owned by this bridge."""
        with self.workspace_bridges_lock:
            bridges = list(self.workspace_bridges.values())
        count = 0
        for bridge in bridges:
            with bridge.lock:
                count += len(bridge.active_runs)
        return count

    def request_config(self, raw: Any, origin: str, token: str) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise BridgeFailure("请求体必须是 JSON 对象")
        program_id = program_id_of(raw.get("programId"))
        if not program_id:
            raise BridgeFailure("缺少项目标识")
        if not token:
            raise BridgeFailure("当前用户凭证为空")
        api_url = self._resolve_task_board_api(str(raw.get("apiUrl") or "").strip(), origin, token, program_id)
        # 走到这里这个凭证已经被面板验过了（_resolve_task_board_api 打过一次真实接口），
        # 此时才落盘：普通命令行会话没有运行期环境变量，只能读那份文件，切账号后不刷新
        # 就会继续拿旧账号写入，而面板那边报出来只是一句权限不足，排查方向会被带偏。
        planner.remember_browser_identity(token, str(raw.get("userId") or "").strip())
        config = {
            "api_url": api_url,
            "key": token,
            "key_header": "token",
            "user_id": str(raw.get("userId") or "task-executor").strip() or "task-executor",
            "_project_id": program_id,
        }
        context = planner.project_context(config, program_id)
        program = context.get("program") or {}
        if program_id_of(program.get("programId")) != program_id:
            raise BridgeFailure("任务面板项目上下文校验失败")
        return config

    @staticmethod
    def global_environment_config(raw: Any, token: str) -> dict[str, Any]:
        """环境检测只需当前用户凭证，不读取或校验任何任务面板项目。"""
        if not isinstance(raw, dict):
            raise BridgeFailure("请求体必须是 JSON 对象")
        if not token:
            raise BridgeFailure("当前用户凭证为空")
        # 环境检测不打任何面板接口，凭证没被验证过；只认得出用户的面板 JWT 才落盘。
        if planner.token_subject(token):
            planner.remember_browser_identity(token, str(raw.get("userId") or "").strip())
        return {
            "key": token,
            "key_header": "token",
            "user_id": str(raw.get("userId") or "task-executor").strip() or "task-executor",
        }

    @staticmethod
    def _resolve_task_board_api(explicit_url: str, origin: str, token: str, program_id: int) -> str:
        """Use the configured bridge target, never a browser-provided address."""
        del explicit_url, origin
        candidates = [planner.bridge_api_url()]
        last_error: Exception | None = None
        for candidate in candidates:
            try:
                normalized = planner.normalize_api_url(candidate)
            except planner.ToolFailure as exc:
                last_error = exc
                continue
            config = {
                "api_url": normalized,
                "key": token,
                "key_header": "token",
                "user_id": "task-executor",
            }
            try:
                planner.request_api(
                    config,
                    "GET",
                    "/delivery/program",
                    query={"programId": program_id},
                )
                return normalized
            except planner.ToolFailure as exc:
                last_error = exc
        raise BridgeFailure(f"无法连接任务面板接口：{last_error or '没有可用地址'}")

    def _claim_task(self, config: dict[str, Any], program_id: int, task: dict[str, Any], comment: str, provider: str = "codex") -> dict[str, Any]:
        updated = self._request_with_retry(
            config,
            "/delivery/item/patch",
            {
                "programId": program_id,
                "itemKey": str(task["itemKey"]),
                "version": int(task["version"]),
                "status": "doing",
                "progress": max(1, int(task.get("progress") or 0)),
                "ownerName": provider_label(provider),
                "comment": comment,
                "actorName": f"{provider}-http-bridge",
            },
        )
        if not isinstance(updated, dict) or updated.get("status") != "doing":
            raise BridgeFailure(f"任务面板未确认任务已进入进行中，已取消启动 {provider_label(provider)} 会话")
        return {**task, **updated}

    def _register_queue(self, queue_id: str, program_id: int) -> None:
        with self.lock:
            self.queue_programs[queue_id] = program_id

    def _release_queue(self, queue_id: str) -> None:
        with self.lock:
            self.queue_programs.pop(queue_id, None)
            self.cancelled_queues.discard(queue_id)

    def _abort_if_cancelled(self, queue_id: str) -> None:
        """队列每启动一批任务前问一次：用户已经点过停止就别再往下拉了。"""
        with self.lock:
            cancelled = queue_id in self.cancelled_queues
        if cancelled:
            raise BridgeFailure("执行队列已被用户停止")

    @staticmethod
    def _create_execution_batch(
        config: dict[str, Any],
        program_id: int,
        item_keys: list[str],
        mode: str,
        provider: str,
        redo: bool = False,
    ) -> dict[str, Any]:
        """Create the authoritative server-side record before the local queue starts."""
        batch = planner.request_api(
            config,
            "POST",
            "/delivery/execution-batch/create",
            body={
                "programId": program_id,
                "itemKeys": item_keys,
                "mode": mode,
                "executorType": provider,
                # 再做一次：服务端据此放行已完成任务，任务状态不回滚。
                "redo": bool(redo),
                "actorName": f"{provider}-http-bridge",
            },
        )
        if not isinstance(batch, dict) or not str(batch.get("batchId") or "").strip():
            raise BridgeFailure("任务面板没有返回有效的执行批次标识")
        return batch

    @staticmethod
    def _update_execution_batch_item(
        config: dict[str, Any],
        program_id: int,
        batch_id: str,
        item_key: str,
        status: str,
        message: str = "",
        provider: str = "codex",
    ) -> None:
        if not batch_id:
            return
        CoreMixin._request_with_retry(
            config,
            "/delivery/execution-batch/item/status",
            {
                "programId": program_id,
                "batchId": batch_id,
                "itemKey": item_key,
                "status": status,
                "message": message,
                "actorName": f"{provider}-http-bridge",
            },
        )

    @staticmethod
    def _finalize_execution_batch(
        config: dict[str, Any],
        program_id: int,
        batch_id: str,
        status: str,
        summary: str,
        provider: str = "codex",
    ) -> None:
        if not batch_id:
            return
        CoreMixin._request_with_retry(
            config,
            "/delivery/execution-batch/finalize",
            {
                "programId": program_id,
                "batchId": batch_id,
                "status": status,
                "summary": summary,
                "actorName": f"{provider}-http-bridge",
            },
        )

    def _release_failed_claim(self, config: dict[str, Any], program_id: int, task: dict[str, Any], provider: str = "codex") -> None:
        try:
            self._request_with_retry(
                config,
                "/delivery/item/patch",
                {
                    "programId": program_id,
                    "itemKey": str(task["itemKey"]),
                    "version": int(task["version"]),
                    "status": "todo",
                    "progress": 0,
                    "comment": f"{provider_label(provider)} 会话启动失败，任务已自动恢复为未开始。",
                    "actorName": f"{provider}-http-bridge",
                },
            )
        except Exception as exc:
            print(f"恢复启动失败任务状态失败：{program_id}/{task.get('itemKey')}: {exc}", file=sys.stderr, flush=True)

    def reconcile(self) -> None:
        # Board operations always receive a current user token and one project ID
        # from the browser. A process-wide recovery scan would require persisting a
        # credential and would violate that scope, so recovery is intentionally UI-led.
        return

    def _reconcile_pending_session_syncs(self, config: dict[str, Any]) -> None:
        for entry in self.pending_session_syncs.snapshot():
            try:
                self._request_with_retry(
                    scoped_config(config, str(entry.get("bizLine") or DEFAULT_BIZ_LINE)),
                    "/delivery/item/execution-session/status",
                    entry,
                )
                self.pending_session_syncs.remove(entry)
            except Exception as exc:
                print(
                    f"重试关闭执行会话失败：{entry.get('programId')}/{entry.get('itemKey')}: {exc}",
                    file=sys.stderr,
                    flush=True,
                )

    def reconcile_forever(self, interval: float = 5) -> None:
        while True:
            self.reconcile()
            time.sleep(interval)

    def _task_detail(self, config: dict[str, Any], program_id: int, item_key: str) -> dict[str, Any]:
        task = planner.request_api(
            config, "GET", "/delivery/item", query={"programId": program_id, "itemKey": item_key}
        )
        if not isinstance(task, dict) or not task.get("itemKey"):
            raise BridgeFailure("任务不存在")
        return task

    @staticmethod
    def _request_with_retry(config: dict[str, Any], path: str, body: dict[str, Any]) -> Any:
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                return planner.request_api(config, "POST", path, body=body)
            except Exception as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(1 << attempt)
        assert last_error is not None
        raise last_error
