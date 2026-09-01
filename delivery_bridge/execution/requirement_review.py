"""需求级评审会话。与总体测试共用会话表，靠 metadata.kind 分流。
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from typing import Any

import server as planner

from delivery_bridge.artifacts import ConversationAttachmentStore
from delivery_bridge.clients import factory
from delivery_bridge.clients.codex import AppServerClient
from delivery_bridge.errors import BridgeFailure
from delivery_bridge.executor_env import codex_environment
from delivery_bridge.item_keys import REQUIREMENT_REVIEW_ITEM_KEY, REQUIREMENT_REVIEW_SESSION_KIND
from delivery_bridge.payloads import (
    assert_runtime_project,
    config_biz_line,
    request_scoped_config,
    session_kind_of,
    task_identity,
    validate_requirement_review_payload,
)
from delivery_bridge.prompt_context import with_mention_context
from delivery_bridge.prompts.requirement import (
    build_requirement_review_prompt,
    requirement_review_report_relative_path,
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
from delivery_bridge.turn_output import execution_output, final_agent_text_from_output
from delivery_bridge.turn_view import serialize_turns


class RequirementReviewMixin:
    @staticmethod
    def _requirement_review_item_key(requirement_key: str) -> str:
        return f"{REQUIREMENT_REVIEW_ITEM_KEY}:{requirement_key}"

    @staticmethod
    def _requirement_review_identity(program_id: int, requirement_key: str) -> tuple[str, int, str]:
        return task_identity("", program_id, RequirementReviewMixin._requirement_review_item_key(requirement_key))

    def _load_requirement_review_session(
        self, config: dict[str, Any], program_id: int, requirement_key: str, provider: str, thread_id: str = "",
    ) -> dict[str, Any] | None:
        # 和测试会话共用一张表，这里只认 metadata.kind 是 review 的那些行。
        rows = planner.request_api(
            config, "GET", "/delivery/requirement/testing-sessions",
            query={"programId": program_id, "requirementKey": requirement_key},
        )
        rows = [
            row for row in (rows or [])
            if isinstance(row, dict) and str(row.get("threadId") or "")
            and session_kind_of(row) == REQUIREMENT_REVIEW_SESSION_KIND
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

    def _save_requirement_review_session(
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
                        "turnId": str(session.get("turnId") or ""), "kind": REQUIREMENT_REVIEW_SESSION_KIND,
                        "workspace": self.workspace.name,
                    },
                    "actorName": f"{provider}-http-bridge",
                },
            )
        except Exception as exc:
            print(f"保存需求 review 会话目录失败：{program_id}/{requirement_key}: {exc}", file=sys.stderr, flush=True)

    def _persist_requirement_review_report(self, requirement_key: str, report: str) -> Path:
        relative = requirement_review_report_relative_path(requirement_key)
        destination = (self.workspace / relative).resolve()
        try:
            destination.relative_to(self.workspace)
        except ValueError as exc:
            raise BridgeFailure("review 报告路径超出当前项目") from exc
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(report.rstrip() + "\n", encoding="utf-8")
        return destination

    def _requirement_review_report(self, requirement_key: str) -> tuple[str, str]:
        """报告只落在工作区文件里，没有独立的库表；读不到就当还没生成。"""
        relative = requirement_review_report_relative_path(requirement_key)
        destination = self.workspace / relative
        try:
            if destination.is_file():
                return destination.read_text(encoding="utf-8"), relative.as_posix()
        except Exception as exc:
            print(f"读取 review 报告失败：{requirement_key}: {exc}", file=sys.stderr, flush=True)
        return "", relative.as_posix()

    def requirement_review(
        self, program_id: int, requirement_key: str, thread_id: str = "", provider: str = "codex", config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        config = request_scoped_config(config, DEFAULT_BIZ_LINE, program_id)
        provider = ai_provider_of(provider)
        requirement_key = str(requirement_key or "").strip()
        if not requirement_key:
            raise BridgeFailure("缺少需求标识")
        session = self._load_requirement_review_session(config, program_id, requirement_key, provider, thread_id)
        catalog = list((session or {}).get("catalog") or [])
        selected_thread_id = thread_id or str((session or {}).get("threadId") or "")
        identity = self._requirement_review_identity(program_id, requirement_key)
        with self.lock:
            active = self.active_runs.get(identity)
        report, report_path = self._requirement_review_report(requirement_key)
        if not selected_thread_id:
            return {
                "programId": program_id, "requirementKey": requirement_key, "threadId": "", "executorType": provider,
                "turns": [], "conversations": catalog, "active": False, "activeTurnId": "",
                "reviewReport": report, "reviewReportPath": report_path,
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
        item_key = self._requirement_review_item_key(requirement_key)
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
            "reviewReport": report, "reviewReportPath": report_path,
        }

    def send_requirement_review(self, raw: Any, config: dict[str, Any]) -> dict[str, Any]:
        provider = ai_provider_of(raw)
        (
            program_id, requirement_key, message, requested_thread_id, new_conversation, model,
            reasoning_effort, fast_mode, scope, chat_references, generate_report,
        ) = validate_requirement_review_payload(raw)
        assert_runtime_project(config, program_id)
        requirement = self._requirement_for_prototype(config, program_id, requirement_key)
        mention_context = self._conversation_mention_context(
            config, program_id, chat_references, None, requirement_key,
        )
        identity = self._requirement_review_identity(program_id, requirement_key)
        session = self._load_requirement_review_session(config, program_id, requirement_key, provider, requested_thread_id)
        with self.lock:
            active = self.active_runs.get(identity)
        if active is not None:
            if new_conversation or (requested_thread_id and requested_thread_id != active.get("threadId")):
                raise BridgeFailure("当前需求已有正在运行的 review 会话，请先停止或等待完成")
            active["client"].steer_turn(
                str(active["threadId"]), str(active["turnId"]), with_mention_context(message, mention_context), [],
                request_id=active["client"].next_request_id(),
            )
            self.progress.publish(identity, "message", "已追加 review 要求", message, "running")
            return {"accepted": True, "programId": program_id, "requirementKey": requirement_key, "threadId": active["threadId"], "turnId": active["turnId"], "active": True}
        catalog = list((session or {}).get("catalog") or [])
        known_thread_ids = {str(entry.get("threadId") or "") for entry in catalog}
        if requested_thread_id and requested_thread_id not in known_thread_ids:
            raise BridgeFailure("所选 review 会话不存在")
        if not session or new_conversation or not session.get("threadId"):
            if len(catalog) >= MAX_PLANNING_CONVERSATIONS:
                raise BridgeFailure("该需求保留的 review 会话已达上限")
            title = f"代码 review · {requirement.get('name') or requirement_key}"
            if catalog:
                title = f"{title} V{len(catalog) + 1}"
            client = factory.create_ai_client(
                provider, self.workspace, lambda event: self._publish_app_server_event(identity, event),
                codex_environment(config, program_id, write_allowed=True),
            )
            try:
                thread_id, turn_id = client.start_task(
                    title, build_requirement_review_prompt(
                        program_id, requirement, message, self.workspace, scope,
                        generate_report=generate_report, mention_context=mention_context,
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
                    build_requirement_review_prompt(
                        program_id, requirement, message, self.workspace, scope,
                        follow_up=True, generate_report=generate_report, mention_context=mention_context,
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
                "client": client, "threadId": thread_id, "turnId": turn_id, "requirementReview": True,
                "provider": provider, "config": config, "programId": program_id, "requirementKey": requirement_key,
            }
        self._save_requirement_review_session(config, program_id, requirement_key, provider, session)
        self.progress.publish(
            identity, "status", "正在生成 review 报告" if generate_report else "正在进行代码 review",
            f"{provider_label(provider)} 正在按勾选范围审查改动。", "running",
        )
        threading.Thread(
            target=self._follow_requirement_review,
            args=(identity, client, config, program_id, requirement_key, provider, session, thread_id, turn_id, generate_report), daemon=True,
        ).start()
        return {"accepted": True, "programId": program_id, "requirementKey": requirement_key, "threadId": thread_id, "turnId": turn_id, "active": True}

    def stop_requirement_review(self, raw: Any, config: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise BridgeFailure("请求体必须是 JSON 对象")
        program_id = program_id_of(raw.get("programId"))
        requirement_key = str(raw.get("requirementKey") or "").strip()
        assert_runtime_project(config, program_id)
        identity = self._requirement_review_identity(program_id, requirement_key)
        with self.lock:
            active = self.active_runs.get(identity)
        if active is None or not active.get("requirementReview"):
            raise BridgeFailure("该需求当前没有正在运行的 review 会话")
        requested_thread_id = str(raw.get("threadId") or "").strip()
        if requested_thread_id and requested_thread_id != active.get("threadId"):
            raise BridgeFailure("所选 review 会话当前没有正在运行的回合")
        active["client"].interrupt_turn(str(active["threadId"]), str(active["turnId"]), request_id=active["client"].next_request_id())
        self.progress.publish(identity, "status", "已请求停止 review", "正在等待 review 回合中断。", "running")
        return {"accepted": True, "programId": program_id, "requirementKey": requirement_key, "threadId": active["threadId"], "turnId": active["turnId"], "active": True}

    def _follow_requirement_review(
        self, identity: tuple[str, int, str], client: AppServerClient, config: dict[str, Any], program_id: int,
        requirement_key: str, provider: str, session: dict[str, Any], thread_id: str, turn_id: str,
        generate_report: bool = False,
    ) -> None:
        try:
            turn_status = client.wait_turn(turn_id)
            entry = next(
                (item for item in session.get("catalog") or [] if str(item.get("threadId") or "") == thread_id),
                {},
            )
            title = str(entry.get("title") or "代码 review")
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
                phase="review",
                terminal_status=turn_status,
            )
            if generate_report and turn_status == "completed":
                # 执行器一般已经自己写过报告；这里按最终回复再落一次，避免它只在聊天里说完就收工。
                turn = client.read_turn(thread_id, turn_id, request_id=client.next_request_id())
                report = final_agent_text_from_output(execution_output(turn_status, turn))
                if report.strip():
                    self._persist_requirement_review_report(requirement_key, report)
            for item in session.get("catalog") or []:
                if item.get("threadId") == thread_id:
                    item.update({"status": turn_status, "active": False, "updatedAt": utc_now()})
            session["turnId"] = turn_id
            self._save_requirement_review_session(config, program_id, requirement_key, provider, session)
            self.progress.publish(
                identity, "status",
                ("review 报告已生成" if generate_report else "代码 review 已完成") if turn_status == "completed"
                else ("review 报告未生成" if generate_report else "代码 review 未完成"),
                f"报告已写入 {requirement_review_report_relative_path(requirement_key).as_posix()}。" if generate_report else "评审意见已回到聊天里。",
                turn_status,
            )
        except Exception as exc:
            self.progress.publish(identity, "error", "同步 review 结果失败", str(exc), "failed")
            print(f"同步需求 review 结果失败：{program_id}/{requirement_key}: {exc}", file=sys.stderr, flush=True)
        finally:
            client.close()
            with self.lock:
                current = self.active_runs.get(identity)
                if current is None or current.get("client") is client:
                    self.active.discard(identity)
                    self._release_active_run(identity)
