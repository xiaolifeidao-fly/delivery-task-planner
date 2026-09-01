"""微调会话，需求级和任务级两套。
"""

from __future__ import annotations

import sys
import threading
from typing import Any

import server as planner

from delivery_bridge.artifacts import ConversationAttachmentStore
from delivery_bridge.clients import factory
from delivery_bridge.clients.codex import AppServerClient
from delivery_bridge.errors import BridgeFailure
from delivery_bridge.executor_env import codex_environment
from delivery_bridge.item_keys import (
    REQUIREMENT_FINE_TUNING_ITEM_KEY,
    REQUIREMENT_FINE_TUNING_SESSION_KIND,
    task_fine_tuning_executor_type,
)
from delivery_bridge.payloads import (
    assert_runtime_project,
    biz_line_of,
    config_biz_line,
    request_scoped_config,
    session_kind_of,
    task_identity,
    validate_fine_tuning_payload,
)
from delivery_bridge.prompts.task import build_requirement_fine_tuning_prompt, build_task_fine_tuning_prompt
from delivery_bridge.providers import (
    DEFAULT_BIZ_LINE,
    ai_provider_of,
    executor_provider_of,
    program_id_of,
    provider_label,
    same_executor_purpose,
)
from delivery_bridge.sessions import (
    MAX_PLANNING_CONVERSATIONS,
    conversation_catalog,
    conversation_metadata,
    merged_conversation_catalog,
    next_conversation_version,
)
from delivery_bridge.timeutil import utc_now
from delivery_bridge.turn_output import SESSION_STATUS
from delivery_bridge.turn_view import serialize_turns


class FineTuningMixin:
    @staticmethod
    def _requirement_fine_tuning_item_key(requirement_key: str) -> str:
        return f"{REQUIREMENT_FINE_TUNING_ITEM_KEY}:{requirement_key}"

    @staticmethod
    def _requirement_fine_tuning_identity(program_id: int, requirement_key: str) -> tuple[str, int, str]:
        return task_identity("", program_id, FineTuningMixin._requirement_fine_tuning_item_key(requirement_key))

    def _load_requirement_fine_tuning_session(
        self, config: dict[str, Any], program_id: int, requirement_key: str, provider: str, thread_id: str = "",
    ) -> dict[str, Any] | None:
        rows = planner.request_api(
            config, "GET", "/delivery/requirement/testing-sessions",
            query={"programId": program_id, "requirementKey": requirement_key},
        )
        rows = [
            row for row in (rows or [])
            if isinstance(row, dict) and str(row.get("threadId") or "")
            and session_kind_of(row) == REQUIREMENT_FINE_TUNING_SESSION_KIND
        ]
        if not rows:
            return None
        catalog = [
            {
                "threadId": str(row.get("threadId") or ""), "title": str(row.get("title") or ""),
                "createdAt": str(row.get("createdAt") or ""), "updatedAt": str(row.get("updatedAt") or ""),
                "status": str(row.get("status") or "completed"),
                "executorType": executor_provider_of(row, provider), "active": False,
            }
            for row in rows
        ]
        current = next((row for row in rows if str(row.get("threadId") or "") == thread_id), rows[-1])
        metadata = current.get("metadata") if isinstance(current.get("metadata"), dict) else {}
        return {
            "threadId": str(current.get("threadId") or ""), "turnId": str(metadata.get("turnId") or ""),
            "executorType": executor_provider_of(current, provider), "requirementKey": requirement_key, "catalog": catalog,
        }

    def _save_requirement_fine_tuning_session(
        self, config: dict[str, Any], program_id: int, requirement_key: str, provider: str, session: dict[str, Any],
    ) -> None:
        thread_id = str(session.get("threadId") or "")
        if not requirement_key or not thread_id:
            return
        entry = next((item for item in session.get("catalog") or [] if str(item.get("threadId") or "") == thread_id), {})
        provider = executor_provider_of(entry, session.get("executorType") or provider)
        try:
            planner.request_api(
                config, "POST", "/delivery/requirement/testing-session/bind",
                body={
                    "programId": program_id, "requirementKey": requirement_key, "executorType": provider,
                    "threadId": thread_id, "title": str(entry.get("title") or "")[:120],
                    "status": str(entry.get("status") or "running"),
                    "metadata": {
                        "turnId": str(session.get("turnId") or ""), "kind": REQUIREMENT_FINE_TUNING_SESSION_KIND,
                        "workspace": self.workspace.name,
                    },
                    "actorName": f"{provider}-http-bridge",
                },
            )
        except Exception as exc:
            print(f"保存需求微调会话目录失败：{program_id}/{requirement_key}: {exc}", file=sys.stderr, flush=True)

    def requirement_fine_tuning(
        self, program_id: int, requirement_key: str, thread_id: str = "", provider: str = "codex", config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        config = request_scoped_config(config, DEFAULT_BIZ_LINE, program_id)
        provider = ai_provider_of(provider)
        requirement = self._requirement_for_prototype(config, program_id, requirement_key)
        session = self._load_requirement_fine_tuning_session(config, program_id, requirement_key, provider, thread_id)
        catalog = list((session or {}).get("catalog") or [])
        selected_thread_id = thread_id or str((session or {}).get("threadId") or "")
        identity = self._requirement_fine_tuning_identity(program_id, requirement_key)
        with self.lock:
            active = self.active_runs.get(identity)
        if not selected_thread_id:
            return {
                "programId": program_id, "requirementKey": requirement_key, "threadId": "", "executorType": provider,
                "turns": [], "conversations": catalog, "active": False, "activeTurnId": "",
            }
        provider = executor_provider_of(
            next((entry for entry in catalog if str(entry.get("threadId") or "") == selected_thread_id), {}),
            (session or {}).get("executorType") or provider,
        )
        live_client = active["client"] if active is not None and active.get("threadId") == selected_thread_id else None
        thread = self._read_thread_with_workspace_archive(
            live_client, selected_thread_id, "requirement", requirement_key, config, program_id,
            provider=provider, environment=codex_environment(config, program_id, write_allowed=True),
        )
        item_key = self._requirement_fine_tuning_item_key(requirement_key)
        for entry in catalog:
            entry["active"] = bool(active is not None and entry.get("threadId") == active.get("threadId"))
            if not entry["active"] and entry.get("status") == "running":
                entry["status"] = "interrupted"
        return {
            "programId": program_id, "requirementKey": requirement_key, "threadId": selected_thread_id,
            "executorType": provider,
            "turns": serialize_turns(
                thread.get("turns") or [],
                lambda attachment_ids: [ConversationAttachmentStore._public(attachment) for attachment in self.attachments.resolve(program_id, item_key, attachment_ids)],
                lambda paths: self.artifacts.register(config_biz_line(config), program_id, item_key, paths),
            ),
            "conversations": catalog,
            "active": bool(active is not None and active.get("threadId") == selected_thread_id),
            "activeTurnId": str((active or {}).get("turnId") or ""),
        }

    def send_requirement_fine_tuning(self, raw: Any, config: dict[str, Any]) -> dict[str, Any]:
        (
            program_id, requirement_key, message, requested_thread_id, new_conversation, model,
            provider, reasoning_effort, fast_mode,
        ) = validate_fine_tuning_payload(raw, "requirement")
        assert_runtime_project(config, program_id)
        requirement = self._requirement_for_prototype(config, program_id, requirement_key)
        context = planner.project_context(config, program_id)
        identity = self._requirement_fine_tuning_identity(program_id, requirement_key)
        session = self._load_requirement_fine_tuning_session(config, program_id, requirement_key, provider, requested_thread_id)
        with self.lock:
            active = self.active_runs.get(identity)
        if active is not None:
            if new_conversation or (requested_thread_id and requested_thread_id != active.get("threadId")):
                raise BridgeFailure("当前需求已有正在运行的微调会话，请先停止或等待完成")
            active["client"].steer_turn(
                str(active["threadId"]), str(active["turnId"]), message, [], request_id=active["client"].next_request_id(),
            )
            self.progress.publish(identity, "message", "已追加微调要求", message, "running")
            return {"accepted": True, "programId": program_id, "requirementKey": requirement_key, "threadId": active["threadId"], "turnId": active["turnId"], "active": True}
        catalog = list((session or {}).get("catalog") or [])
        known_thread_ids = {str(entry.get("threadId") or "") for entry in catalog}
        if requested_thread_id and requested_thread_id not in known_thread_ids:
            raise BridgeFailure("所选需求微调会话不存在")
        if not session or new_conversation or not session.get("threadId"):
            if len(catalog) >= MAX_PLANNING_CONVERSATIONS:
                raise BridgeFailure("该需求保留的微调会话已达上限")
            title = f"需求微调 · {requirement.get('name') or requirement_key}"
            if catalog:
                title = f"{title} V{len(catalog) + 1}"
            client = factory.create_ai_client(
                provider, self.workspace, lambda event: self._publish_app_server_event(identity, event),
                codex_environment(config, program_id, write_allowed=True),
            )
            try:
                thread_id, turn_id = client.start_task(
                    title, build_requirement_fine_tuning_prompt(program_id, requirement, context, message, self.workspace), [],
                    model=model, reasoning_effort=reasoning_effort, fast_mode=fast_mode,
                )
            except Exception:
                client.close()
                raise
            session = {
                "threadId": thread_id, "turnId": turn_id, "requirementKey": requirement_key, "executorType": provider,
                "catalog": [*catalog, {"threadId": thread_id, "title": title, "createdAt": utc_now(), "updatedAt": utc_now(), "status": "running", "active": True, "executorType": provider}],
            }
        else:
            thread_id = requested_thread_id or str(session.get("threadId") or "")
            provider = executor_provider_of(
                next((entry for entry in catalog if str(entry.get("threadId") or "") == thread_id), {}),
                session.get("executorType") or provider,
            )
            client = factory.create_ai_client(
                provider, self.workspace, lambda event: self._publish_app_server_event(identity, event),
                codex_environment(config, program_id, write_allowed=True),
            )
            try:
                client.resume_thread(thread_id)
                turn_id = client.start_turn(
                    thread_id, build_requirement_fine_tuning_prompt(program_id, requirement, context, message, self.workspace, follow_up=True), [],
                    request_id=client.next_request_id(), model=model, reasoning_effort=reasoning_effort, fast_mode=fast_mode,
                )
            except Exception:
                client.close()
                raise
            session.update({"threadId": thread_id, "turnId": turn_id, "executorType": provider})
            for entry in session.get("catalog") or []:
                if entry.get("threadId") == thread_id:
                    entry.update({"status": "running", "active": True, "updatedAt": utc_now(), "executorType": provider})
        with self.lock:
            self.active.add(identity)
            self.active_runs[identity] = {
                "client": client, "threadId": thread_id, "turnId": turn_id, "requirementFineTuning": True,
                "provider": provider, "config": config, "programId": program_id, "requirementKey": requirement_key,
            }
        self._save_requirement_fine_tuning_session(config, program_id, requirement_key, provider, session)
        self.progress.publish(identity, "status", "正在微调需求", f"{provider_label(provider)} 正在按本轮要求调整需求产物。", "running")
        threading.Thread(
            target=self._follow_requirement_fine_tuning,
            args=(identity, client, config, program_id, requirement_key, provider, session, thread_id, turn_id), daemon=True,
        ).start()
        return {"accepted": True, "programId": program_id, "requirementKey": requirement_key, "threadId": thread_id, "turnId": turn_id, "active": True}

    def stop_requirement_fine_tuning(self, raw: Any, config: dict[str, Any]) -> dict[str, Any]:
        program_id, requirement_key, _message, requested_thread_id, _new, _model, _provider, _effort, _fast = validate_fine_tuning_payload(raw, "requirement", message_required=False)
        assert_runtime_project(config, program_id)
        identity = self._requirement_fine_tuning_identity(program_id, requirement_key)
        with self.lock:
            active = self.active_runs.get(identity)
        if active is None or not active.get("requirementFineTuning"):
            raise BridgeFailure("该需求当前没有正在运行的微调会话")
        if requested_thread_id and requested_thread_id != active.get("threadId"):
            raise BridgeFailure("所选需求微调会话当前没有正在运行的回合")
        active["client"].interrupt_turn(str(active["threadId"]), str(active["turnId"]), request_id=active["client"].next_request_id())
        self.progress.publish(identity, "status", "已请求停止微调", "正在等待当前微调回合中断。", "running")
        return {"accepted": True, "programId": program_id, "requirementKey": requirement_key, "threadId": active["threadId"], "turnId": active["turnId"], "active": True}

    def _follow_requirement_fine_tuning(
        self, identity: tuple[str, int, str], client: AppServerClient, config: dict[str, Any], program_id: int,
        requirement_key: str, provider: str, session: dict[str, Any], thread_id: str, turn_id: str,
    ) -> None:
        try:
            turn_status = client.wait_turn(turn_id)
            entry = next((item for item in session.get("catalog") or [] if str(item.get("threadId") or "") == thread_id), {})
            title = str(entry.get("title") or "需求微调")
            self._archive_terminal_chat(
                client, config=config, program_id=program_id, resource_kind="requirement", resource_key=requirement_key,
                resource_name=title, conversation_title=title, thread_id=thread_id, provider=provider,
                phase="fine-tuning", terminal_status=turn_status,
            )
            for item in session.get("catalog") or []:
                if item.get("threadId") == thread_id:
                    item.update({"status": turn_status, "active": False, "updatedAt": utc_now()})
            session["turnId"] = turn_id
            self._save_requirement_fine_tuning_session(config, program_id, requirement_key, provider, session)
            self.progress.publish(
                identity, "status", "需求微调已完成" if turn_status == "completed" else "需求微调未完成",
                "请查看聊天中的改动和验证结果。", turn_status,
            )
        except Exception as exc:
            self.progress.publish(identity, "error", "同步需求微调结果失败", str(exc), "failed")
            print(f"同步需求微调结果失败：{program_id}/{requirement_key}: {exc}", file=sys.stderr, flush=True)
        finally:
            client.close()
            with self.lock:
                current = self.active_runs.get(identity)
                if current is None or current.get("client") is client:
                    self.active.discard(identity)
                    self._release_active_run(identity)

    @staticmethod
    def _task_fine_tuning_identity(program_id: int, item_key: str, provider: str = "codex") -> tuple[str, int, str]:
        return task_identity("", program_id, f"__fine_tuning__:{ai_provider_of(provider)}:{item_key}")

    def _task_fine_tuning_bindings(
        self, config: dict[str, Any], program_id: int, item_key: str, provider: str,
    ) -> list[dict[str, Any]]:
        sessions = planner.request_api(
            config, "GET", "/delivery/item/execution-session",
            query={"programId": program_id, "itemKey": item_key},
        ) or []
        executor_type = task_fine_tuning_executor_type(provider)
        return [
            session for session in sessions
            if isinstance(session, dict) and same_executor_purpose(session, executor_type)
        ]

    def _bind_task_fine_tuning_session(
        self,
        config: dict[str, Any],
        program_id: int,
        item_key: str,
        task: dict[str, Any],
        provider: str,
        binding: dict[str, Any] | None,
        thread_id: str,
        turn_id: str,
        title: str = "",
        status: str = "running",
    ) -> dict[str, Any]:
        task_phase = str(task.get("phase") or "requirement")
        binding_phase = str((binding or {}).get("phase") or task_phase)
        existing_thread_id = str((binding or {}).get("externalSessionId") or "")
        phase = binding_phase if existing_thread_id == thread_id else task_phase
        metadata = conversation_metadata(binding, thread_id, turn_id, status, title, phase)
        metadata.update({"workspace": self.workspace.name, "source": "task-fine-tuning"})
        body = {
            "programId": program_id, "itemKey": item_key,
            "executorType": task_fine_tuning_executor_type(provider), "phase": phase,
            "status": SESSION_STATUS.get(status, "running"), "progress": 0,
            "metadata": metadata, "actorName": f"{provider}-http-bridge",
        }
        if binding and existing_thread_id == thread_id and binding_phase != task_phase:
            version = int(binding.get("version") or 0)
            if version <= 0:
                raise BridgeFailure("任务微调会话版本无效，请刷新后重试")
            return self._request_with_retry(
                config, "/delivery/item/execution-session/status", {**body, "version": version},
            )
        return planner.request_api(
            config, "POST", "/delivery/item/execution-session/bind",
            body={**body, "externalSessionId": thread_id},
        )

    @staticmethod
    def _task_fine_tuning_title(task: dict[str, Any], binding: dict[str, Any] | None = None) -> str:
        base = f"{' '.join(str(task.get('title') or task.get('itemKey') or '任务').split())} · 微调"
        version = next_conversation_version(binding)
        if version:
            suffix = f" V{version + 1}"
            return f"{base[:80 - len(suffix)].rstrip()}{suffix}"
        return base[:80]

    def _active_task_fine_tuning(self, program_id: int, item_key: str) -> tuple[tuple[str, int, str], dict[str, Any]] | None:
        with self.lock:
            for identity, active in self.active_runs.items():
                if (
                    identity[1] == program_id and active.get("taskFineTuning")
                    and str(active.get("itemKey") or "") == item_key
                ):
                    return identity, active
        return None

    def task_fine_tuning_conversation(
        self,
        program_id: int,
        item_key: str,
        selected_thread_id: str = "",
        provider: str = "codex",
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        provider = ai_provider_of(provider)
        config = request_scoped_config(config, DEFAULT_BIZ_LINE, program_id)
        task = self._task_detail(config, program_id, item_key)
        bindings = self._task_fine_tuning_bindings(config, program_id, item_key, provider)
        binding = bindings[-1] if bindings else None
        catalog, binding_by_thread = merged_conversation_catalog(bindings)
        current_thread_id = str((binding or {}).get("externalSessionId") or "")
        known_thread_ids = {str(entry.get("threadId") or "") for entry in catalog}
        if selected_thread_id and selected_thread_id not in known_thread_ids:
            raise BridgeFailure("所选任务微调会话不存在")
        thread_id = selected_thread_id or current_thread_id or (str(catalog[0].get("threadId") or "") if catalog else "")
        binding = binding_by_thread.get(thread_id, binding)
        provider = executor_provider_of(binding, provider)
        identity = self._task_fine_tuning_identity(program_id, item_key, provider)
        with self.lock:
            active = self.active_runs.get(identity)
        if not thread_id:
            return {
                "programId": program_id, "itemKey": item_key, "threadId": "", "executorType": provider,
                "turns": [], "conversations": catalog, "active": False, "activeTurnId": "",
            }
        active_for_thread = active if active is not None and active.get("threadId") == thread_id else None
        live_client = active_for_thread["client"] if active_for_thread is not None else None
        thread = self._read_thread_with_workspace_archive(
            live_client, thread_id, "task", item_key, config, program_id,
            provider=provider, environment=codex_environment(config, program_id, write_allowed=True),
        )
        for entry in catalog:
            entry["active"] = bool(active_for_thread is not None and entry.get("threadId") == thread_id)
            if not entry["active"] and entry.get("status") == "running":
                entry["status"] = "interrupted"
        return {
            "programId": program_id, "itemKey": item_key, "threadId": thread_id, "executorType": provider,
            "turns": serialize_turns(
                thread.get("turns") or [],
                None,
                lambda paths: self.artifacts.register(config_biz_line(config), program_id, item_key, paths),
            ),
            "conversations": catalog, "active": active_for_thread is not None,
            "activeTurnId": str((active_for_thread or {}).get("turnId") or ""),
        }

    def _resume_task_fine_tuning_turn(
        self,
        config: dict[str, Any],
        identity: tuple[str, int, str],
        task: dict[str, Any],
        binding: dict[str, Any],
        provider: str,
        thread_id: str,
        turn_id: str,
    ) -> dict[str, Any]:
        with self.lock:
            existing = self.active_runs.get(identity)
            if existing is not None:
                return existing
            if identity in self.active:
                raise BridgeFailure("该任务已有正在运行的微调会话")
            self.active.add(identity)
        client = factory.create_ai_client(
            provider, self.workspace, lambda event: self._publish_app_server_event(identity, event),
            codex_environment(config, program_id_of(config.get("_project_id")), write_allowed=True),
        )
        try:
            client.resume_thread(thread_id)
            with self.lock:
                self.active_runs[identity] = {
                    "client": client, "threadId": thread_id, "turnId": turn_id, "taskFineTuning": True,
                    "task": task, "binding": binding, "config": config, "provider": provider,
                    "programId": program_id_of(config.get("_project_id")), "itemKey": str(task.get("itemKey") or ""),
                }
                return self.active_runs[identity]
        except Exception:
            client.close()
            with self.lock:
                self.active.discard(identity)
                self._release_active_run(identity)
            raise

    def send_task_fine_tuning(self, raw: Any, config: dict[str, Any]) -> dict[str, Any]:
        (
            program_id, item_key, message, requested_thread_id, new_conversation, model,
            provider, reasoning_effort, fast_mode,
        ) = validate_fine_tuning_payload(raw, "task")
        config = request_scoped_config(config, biz_line_of(raw), program_id)
        task = self._task_detail(config, program_id, item_key)
        context = planner.project_context(config, program_id)
        requirement_key = str(task.get("requirementKey") or "").strip()
        requirement = planner.requirement_record(config, program_id, requirement_key) if requirement_key else None
        active_entry = self._active_task_fine_tuning(program_id, item_key)
        if active_entry is not None:
            identity, active = active_entry
            if new_conversation or (requested_thread_id and requested_thread_id != active.get("threadId")):
                raise BridgeFailure("该任务已有正在运行的微调会话，请先停止或等待完成")
            active["client"].steer_turn(
                str(active["threadId"]), str(active["turnId"]), message, request_id=active["client"].next_request_id(),
            )
            self.progress.publish(identity, "message", "已追加微调要求", message, "running")
            return {"accepted": True, "programId": program_id, "itemKey": item_key, "threadId": active["threadId"], "turnId": active["turnId"], "active": True}
        bindings = self._task_fine_tuning_bindings(config, program_id, item_key, provider)
        binding = bindings[-1] if bindings else None
        catalog, binding_by_thread = merged_conversation_catalog(bindings)
        known_thread_ids = {str(entry.get("threadId") or "") for entry in catalog}
        if requested_thread_id and requested_thread_id not in known_thread_ids:
            raise BridgeFailure("所选任务微调会话不存在")
        selected_thread_id = requested_thread_id or str((binding or {}).get("externalSessionId") or "")
        if selected_thread_id:
            binding = binding_by_thread.get(selected_thread_id, binding)
            provider = executor_provider_of(binding, provider)
        identity = self._task_fine_tuning_identity(program_id, item_key, provider)
        if binding and binding.get("status") == "running" and selected_thread_id:
            metadata = binding.get("metadata") if isinstance(binding.get("metadata"), dict) else {}
            running_turn_id = str(metadata.get("turnId") or "")
            if running_turn_id:
                active = self._resume_task_fine_tuning_turn(
                    config, identity, task, binding, provider, selected_thread_id, running_turn_id,
                )
                active["client"].steer_turn(
                    selected_thread_id, running_turn_id, message, request_id=active["client"].next_request_id(),
                )
                self.progress.publish(identity, "message", "已追加微调要求", message, "running")
                return {"accepted": True, "programId": program_id, "itemKey": item_key, "threadId": selected_thread_id, "turnId": running_turn_id, "active": True}
        client = factory.create_ai_client(
            provider, self.workspace, lambda event: self._publish_app_server_event(identity, event),
            codex_environment(config, program_id, write_allowed=True),
        )
        title = ""
        try:
            if not selected_thread_id or new_conversation:
                title = self._task_fine_tuning_title(task, binding)
                thread_id, turn_id = client.start_task(
                    title, build_task_fine_tuning_prompt(program_id, task, context, requirement, message, self.workspace),
                    model=model, reasoning_effort=reasoning_effort, fast_mode=fast_mode,
                )
            else:
                thread_id = selected_thread_id
                client.resume_thread(thread_id)
                turn_id = client.start_turn(
                    thread_id, build_task_fine_tuning_prompt(program_id, task, context, requirement, message, self.workspace, follow_up=True),
                    request_id=client.next_request_id(), model=model, reasoning_effort=reasoning_effort, fast_mode=fast_mode,
                )
            refreshed_binding = self._bind_task_fine_tuning_session(
                config, program_id, item_key, task, provider, binding, thread_id, turn_id, title,
            )
            with self.lock:
                if identity in self.active:
                    raise BridgeFailure("该任务已有正在运行的微调会话")
                self.active.add(identity)
                self.active_runs[identity] = {
                    "client": client, "threadId": thread_id, "turnId": turn_id, "taskFineTuning": True,
                    "task": task, "binding": refreshed_binding, "config": config, "provider": provider,
                    "programId": program_id, "itemKey": item_key,
                }
        except Exception:
            client.close()
            with self.lock:
                self.active.discard(identity)
                self._release_active_run(identity)
            raise
        self.progress.publish(identity, "status", "正在微调任务", f"{provider_label(provider)} 正在按本轮要求调整任务产物。", "running")
        threading.Thread(
            target=self._follow_task_fine_tuning,
            args=(identity, client, config, program_id, item_key, provider, thread_id, turn_id, task, refreshed_binding), daemon=True,
        ).start()
        return {"accepted": True, "programId": program_id, "itemKey": item_key, "threadId": thread_id, "turnId": turn_id, "active": True}

    def stop_task_fine_tuning(self, raw: Any, config: dict[str, Any]) -> dict[str, Any]:
        program_id, item_key, _message, requested_thread_id, _new, _model, provider, _effort, _fast = validate_fine_tuning_payload(raw, "task", message_required=False)
        config = request_scoped_config(config, biz_line_of(raw), program_id)
        active_entry = self._active_task_fine_tuning(program_id, item_key)
        if active_entry is None:
            bindings = self._task_fine_tuning_bindings(config, program_id, item_key, provider)
            binding = bindings[-1] if bindings else None
            catalog, binding_by_thread = merged_conversation_catalog(bindings)
            if requested_thread_id:
                binding = binding_by_thread.get(requested_thread_id, binding)
            thread_id = str((binding or {}).get("externalSessionId") or "")
            metadata = (binding or {}).get("metadata") if isinstance((binding or {}).get("metadata"), dict) else {}
            turn_id = str(metadata.get("turnId") or "")
            if not binding or binding.get("status") != "running" or not thread_id or not turn_id:
                raise BridgeFailure("该任务当前没有正在运行的微调会话")
            task = self._task_detail(config, program_id, item_key)
            provider = executor_provider_of(binding, provider)
            identity = self._task_fine_tuning_identity(program_id, item_key, provider)
            active = self._resume_task_fine_tuning_turn(config, identity, task, binding, provider, thread_id, turn_id)
        else:
            identity, active = active_entry
        if requested_thread_id and requested_thread_id != active.get("threadId"):
            raise BridgeFailure("所选任务微调会话当前没有正在运行的回合")
        active["client"].interrupt_turn(str(active["threadId"]), str(active["turnId"]), request_id=active["client"].next_request_id())
        self.progress.publish(identity, "status", "已请求停止微调", "正在等待当前微调回合中断。", "running")
        return {"accepted": True, "programId": program_id, "itemKey": item_key, "threadId": active["threadId"], "turnId": active["turnId"], "active": True}

    def _follow_task_fine_tuning(
        self, identity: tuple[str, int, str], client: AppServerClient, config: dict[str, Any], program_id: int,
        item_key: str, provider: str, thread_id: str, turn_id: str,
        task: dict[str, Any], binding: dict[str, Any],
    ) -> None:
        try:
            turn_status = client.wait_turn(turn_id)
            title = str(next((entry.get("title") for entry in conversation_catalog(binding) if entry.get("threadId") == thread_id), "") or "任务微调")
            self._archive_terminal_chat(
                client, config=config, program_id=program_id, resource_kind="task", resource_key=item_key,
                resource_name=str(task.get("title") or item_key), requirement_key=str(task.get("requirementKey") or ""),
                conversation_title=title, thread_id=thread_id, provider=provider, phase="fine-tuning", terminal_status=turn_status,
            )
            phase = str(binding.get("phase") or task.get("phase") or "requirement")
            metadata = conversation_metadata(binding, thread_id, turn_id, turn_status, phase=phase)
            metadata.update({"workspace": self.workspace.name, "source": "task-fine-tuning"})
            version = int(binding.get("version") or 0)
            if version > 0:
                self._request_with_retry(
                    config, "/delivery/item/execution-session/status",
                    {
                        "programId": program_id, "itemKey": item_key,
                        "executorType": task_fine_tuning_executor_type(provider), "phase": phase,
                        "version": version, "status": SESSION_STATUS.get(turn_status, "blocked"),
                        "progress": 100 if turn_status == "completed" else 0, "metadata": metadata,
                        "actorName": f"{provider}-http-bridge",
                    },
                )
            self.progress.publish(
                identity, "status", "任务微调已完成" if turn_status == "completed" else "任务微调未完成",
                "请查看聊天中的改动和验证结果。", turn_status,
            )
        except Exception as exc:
            self.progress.publish(identity, "error", "同步任务微调结果失败", str(exc), "failed")
            print(f"同步任务微调结果失败：{program_id}/{item_key}: {exc}", file=sys.stderr, flush=True)
        finally:
            client.close()
            with self.lock:
                current = self.active_runs.get(identity)
                if current is None or current.get("client") is client:
                    self.active.discard(identity)
                    self._release_active_run(identity)
