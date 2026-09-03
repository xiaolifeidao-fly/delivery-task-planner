"""需求分析会话。和需求总体测试、review、微调共用会话表，靠 metadata.kind 分流。

这个会话站在需求拆解之前：先在项目工作目录里查清现状、把缺口一条条问回来，
用户点「确认生成」之后才把已确认的口径落成 doc/analysis/<需求键>/ 下的分析文档，
必要时顺带出一套 HTML 原型。拆解会话可以 @ 这份文档，接着往下拆任务。
"""

from __future__ import annotations

import sys
import threading
from typing import Any

import server as planner

from delivery_bridge.artifacts import ConversationAttachmentStore
from delivery_bridge.clients import factory
from delivery_bridge.clients.codex import AppServerClient
from delivery_bridge.documents import (
    document_set_entries,
    requirement_analysis_directory_of,
)
from delivery_bridge.errors import BridgeFailure
from delivery_bridge.executor_env import codex_environment
from delivery_bridge.item_keys import REQUIREMENT_ANALYSIS_ITEM_KEY, REQUIREMENT_ANALYSIS_SESSION_KIND
from delivery_bridge.payloads import (
    assert_runtime_project,
    config_biz_line,
    request_scoped_config,
    session_kind_of,
    task_identity,
    validate_requirement_analysis_payload,
)
from delivery_bridge.prompt_context import with_mention_context
from delivery_bridge.prompts.requirement import (
    build_requirement_analysis_prompt,
    requirement_analysis_document_relative_path,
)
from delivery_bridge.providers import (
    DEFAULT_BIZ_LINE,
    ai_provider_of,
    executor_provider_of,
    program_id_of,
    provider_label,
)
from delivery_bridge.sessions import MAX_PLANNING_CONVERSATIONS
from delivery_bridge.timeutil import utc_now
from delivery_bridge.token_usage import with_usage
from delivery_bridge.turn_view import serialize_turns


class RequirementAnalysisMixin:
    @staticmethod
    def _requirement_analysis_item_key(requirement_key: str) -> str:
        return f"{REQUIREMENT_ANALYSIS_ITEM_KEY}:{requirement_key}"

    @staticmethod
    def _requirement_analysis_identity(program_id: int, requirement_key: str) -> tuple[str, int, str]:
        return task_identity("", program_id, RequirementAnalysisMixin._requirement_analysis_item_key(requirement_key))

    def _load_requirement_analysis_session(
        self, config: dict[str, Any], program_id: int, requirement_key: str, provider: str, thread_id: str = "",
    ) -> dict[str, Any] | None:
        # 和测试、review、微调共用一张表，这里只认 metadata.kind 是需求分析的那些行。
        rows = planner.request_api(
            config, "GET", "/delivery/requirement/testing-sessions",
            query={"programId": program_id, "requirementKey": requirement_key},
        )
        rows = [
            row for row in (rows or [])
            if isinstance(row, dict) and str(row.get("threadId") or "")
            and session_kind_of(row) == REQUIREMENT_ANALYSIS_SESSION_KIND
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
            "executorType": executor_provider_of(current, provider),
            "requirementKey": requirement_key, "catalog": catalog,
        }

    def _save_requirement_analysis_session(
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
                        "turnId": str(session.get("turnId") or ""), "kind": REQUIREMENT_ANALYSIS_SESSION_KIND,
                        "workspace": self.workspace.name,
                    },
                    "actorName": f"{provider}-http-bridge",
                },
            )
        except Exception as exc:
            print(f"保存需求分析会话目录失败：{program_id}/{requirement_key}: {exc}", file=sys.stderr, flush=True)

    def _requirement_analysis_documents(self, requirement_key: str) -> tuple[str, list[dict[str, Any]]]:
        """分析产出只落在工作区目录里，没有独立库表；目录还没建就当这条需求还没分析过。"""
        directory = requirement_analysis_directory_of(requirement_key)
        try:
            return directory.as_posix(), document_set_entries(self.workspace, directory, True)
        except Exception as exc:
            print(f"读取需求分析文档失败：{requirement_key}: {exc}", file=sys.stderr, flush=True)
            return directory.as_posix(), []

    def requirement_analysis(
        self, program_id: int, requirement_key: str, thread_id: str = "", provider: str = "codex", config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        config = request_scoped_config(config, DEFAULT_BIZ_LINE, program_id)
        provider = ai_provider_of(provider)
        requirement_key = str(requirement_key or "").strip()
        if not requirement_key:
            raise BridgeFailure("缺少需求标识")
        session = self._load_requirement_analysis_session(config, program_id, requirement_key, provider, thread_id)
        catalog = list((session or {}).get("catalog") or [])
        selected_thread_id = thread_id or str((session or {}).get("threadId") or "")
        identity = self._requirement_analysis_identity(program_id, requirement_key)
        with self.lock:
            active = self.active_runs.get(identity)
        directory, documents = self._requirement_analysis_documents(requirement_key)
        document_path = requirement_analysis_document_relative_path(requirement_key).as_posix()
        if not selected_thread_id:
            return {
                "programId": program_id, "requirementKey": requirement_key, "threadId": "", "executorType": provider,
                "turns": [], "conversations": catalog, "active": False, "activeTurnId": "",
                "documentDirectory": directory, "documentPath": document_path, "documents": documents,
            }
        provider = executor_provider_of(
            next((entry for entry in catalog if str(entry.get("threadId") or "") == selected_thread_id), {}),
            (session or {}).get("executorType") or provider,
        )
        live_client = active["client"] if active is not None and active.get("threadId") == selected_thread_id else None
        thread = self._read_thread_with_workspace_archive(
            live_client, selected_thread_id, "requirement", requirement_key, config, program_id,
            provider=provider,
            environment=codex_environment(config, program_id, write_allowed=True),
        )
        item_key = self._requirement_analysis_item_key(requirement_key)
        for entry in catalog:
            entry["active"] = bool(active is not None and entry.get("threadId") == active.get("threadId"))
            if not entry["active"] and entry.get("status") == "running":
                entry["status"] = "interrupted"
        return with_usage({
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
            "documentDirectory": directory, "documentPath": document_path, "documents": documents,
        })

    def send_requirement_analysis(self, raw: Any, config: dict[str, Any]) -> dict[str, Any]:
        provider = ai_provider_of(raw)
        (
            program_id, requirement_key, message, requested_thread_id, new_conversation, model,
            reasoning_effort, fast_mode, chat_references, generate_document, generate_prototype,
        ) = validate_requirement_analysis_payload(raw)
        assert_runtime_project(config, program_id)
        requirement = self._requirement_for_prototype(config, program_id, requirement_key)
        mention_context = self._conversation_mention_context(
            config, program_id, chat_references, None, requirement_key,
        )
        identity = self._requirement_analysis_identity(program_id, requirement_key)
        session = self._load_requirement_analysis_session(config, program_id, requirement_key, provider, requested_thread_id)
        with self.lock:
            active = self.active_runs.get(identity)
        if active is not None:
            if new_conversation or (requested_thread_id and requested_thread_id != active.get("threadId")):
                raise BridgeFailure("当前需求已有正在运行的需求分析会话，请先停止或等待完成")
            active["client"].steer_turn(
                str(active["threadId"]), str(active["turnId"]), with_mention_context(message, mention_context), [],
                request_id=active["client"].next_request_id(),
            )
            self.progress.publish(identity, "message", "已追加需求说明", message, "running")
            return {"accepted": True, "programId": program_id, "requirementKey": requirement_key, "threadId": active["threadId"], "turnId": active["turnId"], "active": True}
        catalog = list((session or {}).get("catalog") or [])
        known_thread_ids = {str(entry.get("threadId") or "") for entry in catalog}
        if requested_thread_id and requested_thread_id not in known_thread_ids:
            raise BridgeFailure("所选需求分析会话不存在")
        if not session or new_conversation or not session.get("threadId"):
            if len(catalog) >= MAX_PLANNING_CONVERSATIONS:
                raise BridgeFailure("该需求保留的需求分析会话已达上限")
            title = f"需求分析 · {requirement.get('name') or requirement_key}"
            if catalog:
                title = f"{title} V{len(catalog) + 1}"
            client = factory.create_ai_client(
                provider, self.workspace, lambda event: self._publish_app_server_event(identity, event),
                codex_environment(config, program_id, write_allowed=True),
            )
            try:
                thread_id, turn_id = client.start_task(
                    title, build_requirement_analysis_prompt(
                        program_id, requirement, message, self.workspace,
                        generate_document=generate_document, generate_prototype=generate_prototype,
                        mention_context=mention_context,
                    ), [],
                    model=model, reasoning_effort=reasoning_effort, fast_mode=fast_mode,
                )
            except Exception:
                client.close()
                raise
            session = {
                "threadId": thread_id, "turnId": turn_id, "requirementKey": requirement_key,
                "catalog": [*catalog, {"threadId": thread_id, "title": title, "createdAt": utc_now(), "updatedAt": utc_now(), "status": "running", "active": True}],
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
                    thread_id,
                    build_requirement_analysis_prompt(
                        program_id, requirement, message, self.workspace, follow_up=True,
                        generate_document=generate_document, generate_prototype=generate_prototype,
                        mention_context=mention_context,
                    ), [],
                    request_id=client.next_request_id(), model=model, reasoning_effort=reasoning_effort, fast_mode=fast_mode,
                )
            except Exception:
                client.close()
                raise
            session.update({"threadId": thread_id, "turnId": turn_id})
            for entry in session.get("catalog") or []:
                if entry.get("threadId") == thread_id:
                    entry.update({"status": "running", "active": True, "updatedAt": utc_now()})
        with self.lock:
            self.active.add(identity)
            self.active_runs[identity] = {
                "client": client, "threadId": thread_id, "turnId": turn_id, "requirementAnalysis": True,
                "provider": provider, "config": config, "programId": program_id, "requirementKey": requirement_key,
            }
        self._save_requirement_analysis_session(config, program_id, requirement_key, provider, session)
        self.progress.publish(
            identity, "status", "正在生成需求分析文档" if generate_document else "正在分析需求",
            f"{provider_label(provider)} 正在结合工作目录里的现状梳理这条需求。", "running",
        )
        threading.Thread(
            target=self._follow_requirement_analysis,
            args=(identity, client, config, program_id, requirement_key, provider, session, thread_id, turn_id, generate_document), daemon=True,
        ).start()
        return {"accepted": True, "programId": program_id, "requirementKey": requirement_key, "threadId": thread_id, "turnId": turn_id, "active": True}

    def stop_requirement_analysis(self, raw: Any, config: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise BridgeFailure("请求体必须是 JSON 对象")
        program_id = program_id_of(raw.get("programId"))
        requirement_key = str(raw.get("requirementKey") or "").strip()
        assert_runtime_project(config, program_id)
        identity = self._requirement_analysis_identity(program_id, requirement_key)
        with self.lock:
            active = self.active_runs.get(identity)
        if active is None or not active.get("requirementAnalysis"):
            raise BridgeFailure("该需求当前没有正在运行的需求分析会话")
        requested_thread_id = str(raw.get("threadId") or "").strip()
        if requested_thread_id and requested_thread_id != active.get("threadId"):
            raise BridgeFailure("所选需求分析会话当前没有正在运行的回合")
        active["client"].interrupt_turn(str(active["threadId"]), str(active["turnId"]), request_id=active["client"].next_request_id())
        self.progress.publish(identity, "status", "已请求停止需求分析", "正在等待需求分析回合中断。", "running")
        return {"accepted": True, "programId": program_id, "requirementKey": requirement_key, "threadId": active["threadId"], "turnId": active["turnId"], "active": True}

    def _follow_requirement_analysis(
        self, identity: tuple[str, int, str], client: AppServerClient, config: dict[str, Any], program_id: int,
        requirement_key: str, provider: str, session: dict[str, Any], thread_id: str, turn_id: str,
        generate_document: bool = False,
    ) -> None:
        try:
            turn_status = client.wait_turn(turn_id)
            entry = next(
                (item for item in session.get("catalog") or [] if str(item.get("threadId") or "") == thread_id),
                {},
            )
            title = str(entry.get("title") or "需求分析")
            self._archive_terminal_chat(
                client,
                config=config,
                program_id=program_id,
                resource_kind="requirement",
                resource_key=requirement_key,
                resource_name=title,
                conversation_title=title,
                thread_id=thread_id,
                provider=provider,
                phase="analysis",
                terminal_status=turn_status,
            )
            for item in session.get("catalog") or []:
                if item.get("threadId") == thread_id:
                    item.update({"status": turn_status, "active": False, "updatedAt": utc_now()})
            session["turnId"] = turn_id
            self._save_requirement_analysis_session(config, program_id, requirement_key, provider, session)
            _, documents = self._requirement_analysis_documents(requirement_key)
            self.progress.publish(
                identity, "status",
                ("需求分析文档已生成" if generate_document else "需求分析回合已完成") if turn_status == "completed"
                else ("需求分析文档未生成" if generate_document else "需求分析未完成"),
                (
                    f"分析目录下现有 {len(documents)} 份文档。"
                    if generate_document
                    else "分析结论和待确认问题已回到聊天里。"
                ),
                turn_status,
            )
        except Exception as exc:
            self.progress.publish(identity, "error", "同步需求分析结果失败", str(exc), "failed")
            print(f"同步需求分析结果失败：{program_id}/{requirement_key}: {exc}", file=sys.stderr, flush=True)
        finally:
            client.close()
            with self.lock:
                current = self.active_runs.get(identity)
                if current is None or current.get("client") is client:
                    self.active.discard(identity)
                    self._release_active_run(identity)
