"""预设环境安装会话。装的是本机全局环境，不挂在任何业务仓库上。
"""

from __future__ import annotations

import sys
import threading
from typing import Any

from delivery_bridge.clients import factory
from delivery_bridge.clients.codex import AppServerClient
from delivery_bridge.clients.pool import (
    ACTIVE_THREAD_READ_TIMEOUT_SECONDS,
    THREAD_READERS,
    read_thread_or_empty,
)
from delivery_bridge.environments import (
    GLOBAL_ENVIRONMENT_SETUP_PROGRAM_ID,
    environment_probe_statuses,
    validate_environment_setup_payload,
)
from delivery_bridge.errors import BridgeFailure
from delivery_bridge.executor_env import codex_environment
from delivery_bridge.github_ssh import ensure_github_ssh_key
from delivery_bridge.item_keys import ENVIRONMENT_SETUP_ITEM_KEY
from delivery_bridge.payloads import assert_runtime_project, request_scoped_config, task_identity
from delivery_bridge.prompts.environment import build_environment_setup_prompt
from delivery_bridge.providers import ai_provider_of, provider_label
from delivery_bridge.sessions import MAX_ENVIRONMENT_SETUP_CONVERSATIONS
from delivery_bridge.stores import ENVIRONMENT_SETUP_SESSIONS
from delivery_bridge.timeutil import utc_now
from delivery_bridge.token_usage import with_usage
from delivery_bridge.turn_view import serialize_turns
from delivery_bridge.workspaces import environment_setup_workspace


class EnvironmentMixin:
    @staticmethod
    def _environment_setup_identity(program_id: int = GLOBAL_ENVIRONMENT_SETUP_PROGRAM_ID) -> tuple[str, int, str]:
        """预设环境只有一条本机全局会话，不随项目切换。"""
        return task_identity("", program_id, ENVIRONMENT_SETUP_ITEM_KEY)

    @staticmethod
    def _environment_setup_store_key(provider: str, program_id: int = GLOBAL_ENVIRONMENT_SETUP_PROGRAM_ID) -> str:
        return f"{provider}:{program_id}"

    def environment_setup(
        self,
        program_id: int,
        selected_thread_id: str = "",
        config: dict[str, Any] | None = None,
        provider: str = "codex",
        use_git: bool = False,
        environments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        provider = ai_provider_of(provider)
        config = request_scoped_config(config, "", program_id)
        store_key = self._environment_setup_store_key(provider, program_id)
        session = ENVIRONMENT_SETUP_SESSIONS.load(store_key, selected_thread_id)
        identity = self._environment_setup_identity(program_id)
        with self.lock:
            active = self.active_runs.get(identity)
        environment_statuses = environment_probe_statuses(use_git, environments or [])
        catalog = [dict(entry) for entry in (session or {}).get("catalog") or []]
        known_thread_ids = {str(entry.get("threadId") or "") for entry in catalog}
        if selected_thread_id and selected_thread_id not in known_thread_ids:
            raise BridgeFailure("所选预设环境会话不存在")
        thread_id = selected_thread_id or str((session or {}).get("threadId") or "")
        if not thread_id:
            return {
                "programId": program_id,
                "threadId": "",
                "turns": [],
                "conversations": [],
                "active": False,
                "activeTurnId": "",
                "environmentStatuses": environment_statuses,
            }
        live_client = (
            active["client"]
            if active is not None and active.get("environmentSetup") and active.get("threadId") == thread_id
            else None
        )
        thread = (
            read_thread_or_empty(live_client, thread_id, timeout=ACTIVE_THREAD_READ_TIMEOUT_SECONDS)
            if live_client is not None
            else THREAD_READERS.read(
                provider,
                environment_setup_workspace(),
                codex_environment(config, program_id, write_allowed=False, provider=provider),
                thread_id,
            )
        )
        for entry in catalog:
            entry["active"] = bool(active is not None and entry.get("threadId") == active.get("threadId"))
            # 目录里留着 running 但本进程没有对应回合：多半是上一次桥接跑一半被重启了。
            if not entry["active"] and entry.get("status") == "running":
                entry["status"] = "interrupted"
        return with_usage({
            "programId": program_id,
            "threadId": thread_id,
            "turns": serialize_turns(thread.get("turns") or []),
            "conversations": catalog,
            "active": bool(active is not None and active.get("threadId") == thread_id),
            "activeTurnId": str((active or {}).get("turnId") or ""),
            "environmentStatuses": environment_statuses,
        })

    def send_environment_setup(self, raw: Any, config: dict[str, Any]) -> dict[str, Any]:
        provider = ai_provider_of(raw)
        (
            program_id,
            message,
            requested_thread_id,
            new_conversation,
            use_git,
            environments,
            model,
            reasoning_effort,
            fast_mode,
        ) = validate_environment_setup_payload(raw)
        assert_runtime_project(config, program_id)
        github_ssh_status = ensure_github_ssh_key() if use_git else {}
        identity = self._environment_setup_identity(program_id)
        store_key = self._environment_setup_store_key(provider, program_id)
        session = ENVIRONMENT_SETUP_SESSIONS.load(store_key, requested_thread_id)
        with self.lock:
            active = self.active_runs.get(identity)
        if active is not None:
            if new_conversation or (requested_thread_id and requested_thread_id != active.get("threadId")):
                raise BridgeFailure("本机已有正在运行的预设环境会话，请先停止或等待完成")
            active["client"].steer_turn(
                str(active["threadId"]),
                str(active["turnId"]),
                build_environment_setup_prompt(use_git, environments, message, False),
                [],
                request_id=active["client"].next_request_id(),
            )
            self.progress.publish(identity, "message", "已追加预设要求", message, "running")
            return {
                "accepted": True,
                "programId": program_id,
                "threadId": active["threadId"],
                "turnId": active["turnId"],
                "active": True,
                **github_ssh_status,
            }
        catalog = [dict(entry) for entry in (session or {}).get("catalog") or []]
        known_thread_ids = {str(entry.get("threadId") or "") for entry in catalog}
        if requested_thread_id and requested_thread_id not in known_thread_ids:
            raise BridgeFailure("所选预设环境会话不存在")
        workspace = environment_setup_workspace()
        if not session or new_conversation or not session.get("threadId"):
            if len(catalog) >= MAX_ENVIRONMENT_SETUP_CONVERSATIONS:
                raise BridgeFailure("本机保留的预设环境会话已达上限")
            title = "预设环境"
            if catalog:
                title = f"{title} V0.0.{len(catalog)}"
            client = factory.create_ai_client(
                provider,
                workspace,
                lambda event: self._publish_app_server_event(identity, event),
                codex_environment(config, program_id, write_allowed=False, provider=provider),
            )
            try:
                thread_id, turn_id = client.start_task(
                    title,
                    build_environment_setup_prompt(use_git, environments, message, True),
                    [],
                    model=model,
                    reasoning_effort=reasoning_effort,
                    fast_mode=fast_mode,
                )
            except Exception:
                client.close()
                raise
            session = {
                "threadId": thread_id,
                "turnId": turn_id,
                "catalog": [*catalog, {"threadId": thread_id, "title": title, "createdAt": utc_now(), "updatedAt": utc_now(), "status": "running", "active": True}],
            }
        else:
            thread_id = requested_thread_id or str(session.get("threadId") or "")
            client = factory.create_ai_client(
                provider,
                workspace,
                lambda event: self._publish_app_server_event(identity, event),
                codex_environment(config, program_id, write_allowed=False, provider=provider),
            )
            try:
                client.resume_thread(thread_id)
                turn_id = client.start_turn(
                    thread_id,
                    build_environment_setup_prompt(use_git, environments, message, False),
                    [],
                    request_id=client.next_request_id(),
                    model=model,
                    reasoning_effort=reasoning_effort,
                    fast_mode=fast_mode,
                )
            except Exception:
                client.close()
                raise
            session.update({"threadId": thread_id, "turnId": turn_id})
            for entry in session.get("catalog") or []:
                if entry.get("threadId") == thread_id:
                    entry["status"] = "running"
                    entry["active"] = True
                    entry["updatedAt"] = utc_now()
        with self.lock:
            self.active.add(identity)
            self.active_runs[identity] = {
                "client": client, "threadId": thread_id, "turnId": turn_id,
                "environmentSetup": True, "provider": provider, "config": config, "programId": program_id, "useGit": use_git,
            }
        # 目录当场落盘：这一轮还没跑完桥接就重启，会话列表里也得留着这条聊天。
        ENVIRONMENT_SETUP_SESSIONS.save(store_key, session)
        self.progress.publish(
            identity,
            "status",
            "正在预设环境",
            f"{provider_label(provider)} 正在检测本机环境，只补装缺少的部分。",
            "running",
        )
        threading.Thread(
            target=self._follow_environment_setup,
            args=(identity, client, store_key, session, thread_id, turn_id, use_git),
            daemon=True,
        ).start()
        return {
            "accepted": True,
            "programId": program_id,
            "threadId": thread_id,
            "turnId": turn_id,
            "active": True,
            **github_ssh_status,
        }

    def stop_environment_setup(self, raw: Any, config: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise BridgeFailure("请求体必须是 JSON 对象")
        program_id = GLOBAL_ENVIRONMENT_SETUP_PROGRAM_ID
        assert_runtime_project(config, program_id)
        identity = self._environment_setup_identity(program_id)
        with self.lock:
            active = self.active_runs.get(identity)
        if active is None or not active.get("environmentSetup"):
            raise BridgeFailure("本机当前没有正在运行的预设环境会话")
        requested_thread_id = str(raw.get("threadId") or "").strip()
        if requested_thread_id and requested_thread_id != active.get("threadId"):
            raise BridgeFailure("所选预设环境会话当前没有正在运行的回合")
        active["client"].interrupt_turn(str(active["threadId"]), str(active["turnId"]), request_id=active["client"].next_request_id())
        self.progress.publish(identity, "status", "已请求停止预设", "正在等待执行器中断当前回合。", "running")
        return {"accepted": True, "programId": program_id, "threadId": active["threadId"], "turnId": active["turnId"], "active": True}

    def _follow_environment_setup(
        self,
        identity: tuple[str, int, str],
        client: AppServerClient,
        store_key: str,
        session: dict[str, Any],
        thread_id: str,
        turn_id: str,
        use_git: bool,
    ) -> None:
        status = "failed"
        try:
            status = client.wait_turn(turn_id)
            if use_git:
                # Git may only have become available during the preset run.
                ensure_github_ssh_key()
            session["turnId"] = turn_id
            for entry in session.get("catalog") or []:
                if entry.get("threadId") == thread_id:
                    entry["status"] = status
                    entry["active"] = False
                    entry["updatedAt"] = utc_now()
            ENVIRONMENT_SETUP_SESSIONS.save(store_key, session)
            self.progress.publish(
                identity,
                "status",
                "预设环境已完成" if status == "completed" else "预设环境未完成",
                "请查看会话里的环境检测与安装结果。",
                status,
            )
        except Exception as exc:
            self.progress.publish(identity, "error", "预设环境失败", str(exc), "failed")
            print(f"预设环境失败：{identity[1]}: {exc}", file=sys.stderr, flush=True)
        finally:
            client.close()
            with self.lock:
                current = self.active_runs.get(identity)
                if current is not None and current.get("client") is client:
                    self.active.discard(identity)
                    self._release_active_run(identity)
