"""需求拆解会话。
"""

from __future__ import annotations

import json
import sys
import threading
from typing import Any

import server as planner

from delivery_bridge.artifacts import ConversationAttachmentStore
from delivery_bridge.attachments_text import message_with_attachments
from delivery_bridge.clients import factory
from delivery_bridge.clients.codex import AppServerClient
from delivery_bridge.documents import requirement_outline_path_of
from delivery_bridge.errors import BridgeFailure
from delivery_bridge.executor_env import codex_environment
from delivery_bridge.item_keys import PLANNING_ITEM_KEY, REQUIREMENT_REVIEW_SESSION_KIND
from delivery_bridge.payloads import (
    assert_runtime_project,
    biz_line_of,
    config_biz_line,
    request_scoped_config,
    session_kind_of,
    task_identity,
    validate_planning_payload,
)
from delivery_bridge.prompt_context import with_mention_context
from delivery_bridge.prompts.common import requirement_document_rule_lines
from delivery_bridge.prompts.conversation import CONVERSATION_TITLE_TIMEOUT_SECONDS
from delivery_bridge.prompts.planning import (
    build_planning_follow_up_prompt,
    build_planning_prompt,
    delete_planning_temp_summary,
    planning_detail_digest,
    planning_temp_document_path,
    requirement_outline_rule_lines,
    write_planning_temp_summary,
)
from delivery_bridge.providers import (
    DEFAULT_BIZ_LINE,
    ai_provider_of,
    executor_provider_of,
    program_id_of,
    provider_label,
    same_executor_purpose,
)
from delivery_bridge.sessions import MAX_PLANNING_CONVERSATIONS
from delivery_bridge.timeutil import utc_now
from delivery_bridge.turn_output import execution_output, final_agent_text_from_output
from delivery_bridge.turn_view import serialize_turns


class PlanningMixin:
    @staticmethod
    def _planning_item_key(requirement_key: str = "") -> str:
        """拆解会话在附件仓库里的伪任务键，一条需求一个命名空间。"""
        return f"{PLANNING_ITEM_KEY}:{requirement_key}" if requirement_key else PLANNING_ITEM_KEY

    @staticmethod
    def _planning_identity(program_id: int, requirement_key: str = "") -> tuple[str, int, str]:
        # 每条需求一条独立的拆解线：两个需求同时拆解不该互相判定为「已有运行中的会话」。
        return task_identity("", program_id, PlanningMixin._planning_item_key(requirement_key))

    @staticmethod
    def _planning_catalog(session: dict[str, Any] | None) -> list[dict[str, Any]]:
        if not session:
            return []
        catalog = session.get("catalog") or []
        return [dict(entry) for entry in catalog if isinstance(entry, dict) and entry.get("threadId")]

    def _planning_result(self, config: dict[str, Any], program_id: int, baseline: dict[str, set[str]]) -> dict[str, Any]:
        assert_runtime_project(config, program_id)
        context = planner.project_context(config, program_id)
        items = [item for item in context.get("items") or [] if str(item.get("itemKey") or "") not in baseline["items"]]
        stages = [item for item in context.get("stages") or [] if str(item.get("stageKey") or "") not in baseline["stages"]]
        modules = [item for item in context.get("modules") or [] if str(item.get("moduleKey") or "") not in baseline["modules"]]
        return {
            "items": items,
            "stages": stages,
            "modules": modules,
            "itemKeys": [str(item.get("itemKey") or "") for item in items if item.get("itemKey")],
            "stageKeys": [str(item.get("stageKey") or "") for item in stages if item.get("stageKey")],
            "moduleKeys": [str(item.get("moduleKey") or "") for item in modules if item.get("moduleKey")],
            "updatedAt": utc_now(),
        }

    def _load_planning_session(
        self,
        config: dict[str, Any],
        program_id: int,
        requirement_key: str,
        provider: str,
        thread_id: str = "",
    ) -> dict[str, Any] | None:
        """从任务面板读回这条需求的拆解会话目录。

        桥接是随时会重启的本地进程，聊天列表只能由服务端持有；对话正文仍在执行器
        自己的会话缓存里，这里拿到 threadId 之后再按 thread 读回。
        """
        if not requirement_key:
            return None
        # 目录不按执行器过滤：换了工具也要能看见此前用另一个工具留下的聊天，正文再按线程自己的执行器读。
        rows = planner.request_api(
            config,
            "GET",
            "/delivery/requirement/planning-sessions",
            query={"programId": program_id, "requirementKey": requirement_key},
        )
        # 原型会话与拆解会话共用这张表，靠用途后缀区分；这里只要拆解本身。
        rows = [
            row for row in (rows or [])
            if isinstance(row, dict) and str(row.get("threadId") or "") and same_executor_purpose(row, "")
            and session_kind_of(row) != REQUIREMENT_REVIEW_SESSION_KIND
        ]
        if not rows:
            return None
        catalog = [
            {
                "threadId": str(row.get("threadId") or ""),
                "title": str(row.get("title") or ""),
                "createdAt": str(row.get("createdAt") or ""),
                "updatedAt": str(row.get("updatedAt") or ""),
                "status": str(row.get("status") or "completed"),
                "executorType": executor_provider_of(row, provider),
                "active": False,
            }
            for row in rows
        ]
        current = next((row for row in rows if str(row.get("threadId")) == thread_id), rows[-1])
        metadata = current.get("metadata") if isinstance(current.get("metadata"), dict) else {}
        baseline = metadata.get("baseline") if isinstance(metadata.get("baseline"), dict) else {}
        return {
            "threadId": str(current.get("threadId") or ""),
            "executorType": executor_provider_of(current, provider),
            "turnId": str(metadata.get("turnId") or ""),
            "stageKey": str(metadata.get("stageKey") or ""),
            "moduleKey": str(metadata.get("moduleKey") or ""),
            "kind": str(metadata.get("kind") or ""),
            "detailDigest": str(metadata.get("detailDigest") or ""),
            "requirementKey": requirement_key,
            "baseline": {name: set(baseline.get(name) or []) for name in ("items", "stages", "modules")},
            "result": metadata.get("result") if isinstance(metadata.get("result"), dict) else {},
            "catalog": catalog,
        }

    def _save_planning_session(
        self,
        config: dict[str, Any],
        program_id: int,
        requirement_key: str,
        provider: str,
        session: dict[str, Any],
    ) -> None:
        """把当前这条 thread 的目录项写回任务面板。失败不影响本轮拆解，只是列表少一条。"""
        thread_id = str(session.get("threadId") or "")
        if not requirement_key or not thread_id:
            return
        entry = next(
            (item for item in session.get("catalog") or [] if str(item.get("threadId")) == thread_id),
            {},
        )
        # 线程归属跟着它自己的执行器走，别被当前选中的工具改写。
        provider = executor_provider_of(entry, session.get("executorType") or provider)
        result = session.get("result") or {}
        metadata: dict[str, Any] = {
            "turnId": str(session.get("turnId") or ""),
            "stageKey": str(session.get("stageKey") or ""),
            "moduleKey": str(session.get("moduleKey") or ""),
            "kind": str(session.get("kind") or ""),
            "detailDigest": str(session.get("detailDigest") or ""),
            "baseline": {name: sorted(values) for name, values in (session.get("baseline") or {}).items()},
            "result": result,
        }
        # 服务端给 metadata 留了 256KB；产出对象太多时只留键，前端会回落到看板上的任务明细。
        if len(json.dumps(metadata, ensure_ascii=False).encode("utf-8")) > 200 * 1024:
            metadata["result"] = {
                "items": [],
                "stages": [],
                "modules": [],
                "itemKeys": result.get("itemKeys") or [],
                "stageKeys": result.get("stageKeys") or [],
                "moduleKeys": result.get("moduleKeys") or [],
                "updatedAt": result.get("updatedAt") or "",
            }
        try:
            planner.request_api(
                config,
                "POST",
                "/delivery/requirement/planning-session/bind",
                body={
                    "programId": program_id,
                    "requirementKey": requirement_key,
                    "executorType": provider,
                    "threadId": thread_id,
                    "title": str(entry.get("title") or ""),
                    "status": str(entry.get("status") or "running"),
                    "metadata": metadata,
                },
            )
        except Exception as exc:
            print(f"保存拆解会话目录失败：{program_id}/{requirement_key}: {exc}", file=sys.stderr, flush=True)

    @staticmethod
    def _planning_baseline(context: dict[str, Any]) -> dict[str, set[str]]:
        return {
            "items": {str(item.get("itemKey") or "") for item in context.get("items") or []},
            "stages": {str(item.get("stageKey") or "") for item in context.get("stages") or []},
            "modules": {str(item.get("moduleKey") or "") for item in context.get("modules") or []},
        }

    def planning(
        self,
        program_id: int,
        selected_thread_id: str = "",
        biz_line: str = DEFAULT_BIZ_LINE,
        config: dict[str, Any] | None = None,
        requirement_key: str = "",
        provider: str = "codex",
    ) -> dict[str, Any]:
        provider = ai_provider_of(provider)
        config = request_scoped_config(config, biz_line, program_id)
        biz_line = config_biz_line(config)
        # 目录读服务端，正文读执行器缓存：桥接自己不留状态，重启后照样能把聊天列表列全。
        session = self._load_planning_session(config, program_id, requirement_key, provider, selected_thread_id)
        with self.lock:
            active = self.active_runs.get(self._planning_identity(program_id, requirement_key))
        catalog = self._planning_catalog(session)
        known_thread_ids = {str(entry["threadId"]) for entry in catalog}
        if selected_thread_id and selected_thread_id not in known_thread_ids:
            raise BridgeFailure("所选拆解会话不存在")
        thread_id = selected_thread_id or str((session or {}).get("threadId") or "")
        if not thread_id:
            return {
                "bizLine": biz_line,
                "programId": program_id,
                "requirementKey": requirement_key,
                "threadId": "",
                "executorType": provider,
                "turns": [],
                "conversations": [],
                "active": False,
                "activeTurnId": "",
                "selectedStageKey": "",
                "selectedModuleKey": "",
                "selectedKind": "",
                "result": {"items": [], "stages": [], "modules": [], "itemKeys": [], "stageKeys": [], "moduleKeys": [], "updatedAt": ""},
            }
        thread_entry = next((entry for entry in catalog if str(entry.get("threadId")) == thread_id), {})
        provider = executor_provider_of(thread_entry, session.get("executorType") or provider)
        live_client = active["client"] if active is not None and active.get("threadId") == thread_id else None
        thread = self._read_thread_with_workspace_archive(
            live_client, thread_id, "requirement", requirement_key, config, program_id,
            provider=provider,
            environment=codex_environment(config, program_id, write_allowed=False, provider=provider),
        )
        planning_item_key = self._planning_item_key(requirement_key)
        for entry in catalog:
            entry["active"] = bool(active is not None and entry.get("threadId") == active.get("threadId"))
            # 目录里留着 running，但本进程没有对应的回合：多半是上一次桥接跑一半被重启了。
            if not entry["active"] and entry.get("status") == "running":
                entry["status"] = "interrupted"
        return {
            "bizLine": biz_line,
            "programId": program_id,
            "requirementKey": requirement_key,
            "threadId": thread_id,
            # 选中的这条线程属于哪个工具：面板据此对齐模型下拉和续聊参数。
            "executorType": provider,
            # 附件和产物按需求的伪任务键归档，拆解会话也要能把图片和文件回显出来。
            "turns": serialize_turns(
                thread.get("turns") or [],
                lambda attachment_ids: [
                    ConversationAttachmentStore._public(attachment)
                    for attachment in self.attachments.resolve(program_id, planning_item_key, attachment_ids)
                ],
                lambda paths: self.artifacts.register(config_biz_line(config), program_id, planning_item_key, paths),
            ),
            "conversations": catalog,
            "active": bool(active is not None and active.get("threadId") == thread_id),
            "activeTurnId": str((active or {}).get("turnId") or ""),
            "selectedStageKey": str((session or {}).get("stageKey") or ""),
            "selectedModuleKey": str((session or {}).get("moduleKey") or ""),
            "selectedKind": str((session or {}).get("kind") or ""),
            "result": dict((session or {}).get("result") or {}),
        }

    def send_planning(self, raw: Any, config: dict[str, Any]) -> dict[str, Any]:
        provider = ai_provider_of(raw)
        (
            program_id,
            message,
            requested_thread_id,
            new_conversation,
            selected_stage,
            selected_module,
            selected_kind,
            model,
            reasoning_effort,
            fast_mode,
            requirement,
            attachment_ids,
            chat_references,
            confirm_write,
        ) = validate_planning_payload(raw)
        assert_runtime_project(config, program_id)
        biz_line = config_biz_line(config)
        context = planner.project_context(config, program_id)
        requirement_key = str(requirement.get("requirementKey") or "")
        mention_context = self._conversation_mention_context(
            config, program_id, chat_references, context, requirement_key,
        )
        planner.require_option(selected_stage, context.get("stages") or [], "stageKey", "里程碑")
        planner.require_option(selected_module, context.get("modules") or [], "moduleKey", "模块")
        attachments = self.attachments.resolve(program_id, self._planning_item_key(requirement_key), attachment_ids)
        identity = self._planning_identity(program_id, requirement_key)
        session = self._load_planning_session(config, program_id, requirement_key, provider, requested_thread_id)
        with self.lock:
            active = self.active_runs.get(identity)
        if active is not None:
            if new_conversation or (requested_thread_id and requested_thread_id != active.get("threadId")):
                raise BridgeFailure("当前需求已有正在运行的拆解会话，请先停止或等待完成")
            # 运行中的回合是以预览身份启动的，写入权限改不了，只能等这轮预览结束再确认。
            if confirm_write:
                raise BridgeFailure("当前拆解回合还在运行，请等待本轮梳理结束后再确认写入")
            active["client"].steer_turn(
                str(active["threadId"]),
                str(active["turnId"]),
                message_with_attachments(
                    with_mention_context(
                        message,
                        [
                            *requirement_outline_rule_lines(
                                requirement_outline_path_of(requirement_key).as_posix() if requirement_key else "",
                                False,
                                planning_temp_document_path(
                                    str(requirement.get("name") or ""), requirement_key, str(active["threadId"])
                                ).as_posix(),
                            ),
                            *requirement_document_rule_lines(requirement_key),
                            *mention_context,
                        ],
                    ),
                    attachments,
                ),
                attachments,
                request_id=active["client"].next_request_id(),
            )
            active.setdefault("userMessages", []).append(message)
            self.progress.publish(identity, "message", "已追加拆解要求", message, "running")
            return {"accepted": True, "bizLine": biz_line, "programId": program_id, "requirementKey": requirement_key, "threadId": active["threadId"], "turnId": active["turnId"], "active": True}
        catalog = self._planning_catalog(session)
        known_thread_ids = {str(entry["threadId"]) for entry in catalog}
        if requested_thread_id and requested_thread_id not in known_thread_ids:
            raise BridgeFailure("所选拆解会话不存在")
        started_new_conversation = not session or new_conversation or not session.get("threadId")
        # 这条需求此前一次拆解会话都没有：不管是新增还是编辑进来的，首轮都按用户的问题重定标题。
        first_planning_conversation = started_new_conversation and not catalog
        if started_new_conversation:
            # 一条新会话还没出过预览，没有可确认的方案。
            if confirm_write:
                raise BridgeFailure("请先梳理需求并生成拆解预览，再确认写入")
            if len(catalog) >= MAX_PLANNING_CONVERSATIONS:
                raise BridgeFailure("该需求保留的拆解会话已达上限")
            # 名称留空的新需求先用需求编号占位；标题由开聊时并行跑的那轮自动命名尽快补上。
            title = f"需求拆解 · {requirement.get('name') or requirement_key or context.get('program', {}).get('name') or program_id}"
            if catalog:
                title = f"{title} V0.0.{len(catalog)}"
            client = factory.create_ai_client(
                provider,
                self.workspace,
                lambda event: self._publish_app_server_event(identity, event),
                codex_environment(config, program_id, write_allowed=False, provider=provider),
            )
            try:
                thread_id, turn_id = client.start_task(
                    title,
                    message_with_attachments(
                        build_planning_prompt(program_id, context, message, selected_stage, selected_module, selected_kind, requirement, False, self.workspace, mention_context),
                        attachments,
                    ),
                    attachments,
                    model=model,
                    reasoning_effort=reasoning_effort,
                    fast_mode=fast_mode,
                )
            except Exception:
                client.close()
                raise
            baseline = self._planning_baseline(context)
            session = {
                "threadId": thread_id,
                "turnId": turn_id,
                "stageKey": selected_stage,
                "moduleKey": selected_module,
                "kind": selected_kind,
                "requirementKey": requirement_key,
                "detailDigest": planning_detail_digest(requirement),
                "baseline": baseline,
                "result": {"items": [], "stages": [], "modules": [], "itemKeys": [], "stageKeys": [], "moduleKeys": [], "updatedAt": ""},
                "catalog": [*catalog, {"threadId": thread_id, "title": title, "createdAt": utc_now(), "updatedAt": utc_now(), "status": "running", "active": True}],
            }
        else:
            thread_id = requested_thread_id or str(session.get("threadId") or "")
            # 已有会话只能用它自己的执行器续：线程正文在那个执行器的缓存里，换工具读不到。
            provider = executor_provider_of(
                next((entry for entry in catalog if str(entry.get("threadId")) == thread_id), {}),
                session.get("executorType") or provider,
            )
            detail_digest = planning_detail_digest(requirement)
            client = factory.create_ai_client(
                provider,
                self.workspace,
                lambda event: self._publish_app_server_event(identity, event),
                codex_environment(config, program_id, write_allowed=confirm_write, provider=provider),
            )
            try:
                client.resume_thread(thread_id)
                # 追加回合不重发首轮那整段拆解纪律，只带会变的和丢不起的：本需求已建任务清单
                # （「不要重复建任务」这条约束正是靠它成立，会话被压缩后必须还在）、大纲读写纪律、
                # 本轮的选择与 @ 引用。确认写入是另一套指令（写入契约、命令行动作），仍然走全量。
                follow_up_prompt = (
                    build_planning_prompt(
                        program_id, context, message, selected_stage, selected_module, selected_kind,
                        requirement, True, self.workspace, mention_context, thread_id,
                    )
                    if confirm_write
                    else build_planning_follow_up_prompt(
                        program_id, context, message, selected_stage, selected_module, selected_kind, requirement,
                        self.workspace, mention_context,
                        include_detail=detail_digest != str(session.get("detailDigest") or ""),
                        thread_id=thread_id,
                    )
                )
                turn_id = client.start_turn(
                    thread_id,
                    message_with_attachments(follow_up_prompt, attachments),
                    attachments,
                    request_id=client.next_request_id(),
                    model=model,
                    reasoning_effort=reasoning_effort,
                    fast_mode=fast_mode,
                )
            except Exception:
                client.close()
                raise
            session.update({"threadId": thread_id, "turnId": turn_id, "detailDigest": detail_digest, "stageKey": selected_stage or session.get("stageKey") or "", "moduleKey": selected_module or session.get("moduleKey") or "", "kind": selected_kind or session.get("kind") or ""})
            for entry in session.get("catalog") or []:
                if entry.get("threadId") == thread_id:
                    entry["status"] = "running"
                    entry["active"] = True
                    entry["updatedAt"] = utc_now()
        with self.lock:
            self.active.add(identity)
            self.active_runs[identity] = {
                "client": client,
                "threadId": thread_id,
                "turnId": turn_id,
                "planning": True,
                "provider": provider,
                "config": config,
                "programId": program_id,
                "userMessages": [message],
            }
        # 目录当场写回服务端：这一轮还没跑完桥接就重启，聊天列表里也得留着这条会话。
        self._save_planning_session(config, program_id, requirement_key, provider, session)
        self.progress.publish(
            identity,
            "status",
            "正在写入任务" if confirm_write else "正在梳理需求",
            f"{provider_label(provider)} 正在{'调用任务规划插件写入任务' if confirm_write else '整理拆解预览，确认前不会写入任务'}。",
            "running",
        )
        namer: threading.Thread | None = None
        naming_outcome: dict[str, str] | None = None
        if started_new_conversation:
            namer, naming_outcome = self._start_conversation_naming(
                identity, config, program_id, requirement_key, provider, model, fast_mode,
                message, session, thread_id, first_planning_conversation,
            )
        threading.Thread(
            target=self._follow_planning,
            args=(identity, client, config, program_id, requirement_key, provider, session, thread_id, turn_id,
                  model, reasoning_effort, fast_mode, message, started_new_conversation, confirm_write,
                  namer, naming_outcome, first_planning_conversation),
            daemon=True,
        ).start()
        return {"accepted": True, "bizLine": biz_line, "programId": program_id, "requirementKey": requirement_key, "threadId": thread_id, "turnId": turn_id, "active": True}

    def stop_planning(self, raw: Any, config: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise BridgeFailure("请求体必须是 JSON 对象")
        biz_line = biz_line_of(raw)
        program_id = program_id_of(raw.get("programId"))
        if not program_id:
            raise BridgeFailure("缺少项目标识")
        assert_runtime_project(config, program_id)
        biz_line = config_biz_line(config)
        requirement_key = str(raw.get("requirementKey") or "").strip()
        identity = self._planning_identity(program_id, requirement_key)
        with self.lock:
            active = self.active_runs.get(identity)
        if active is None or not active.get("planning"):
            raise BridgeFailure("该需求当前没有正在运行的拆解会话")
        requested_thread_id = str(raw.get("threadId") or "").strip()
        if requested_thread_id and requested_thread_id != active.get("threadId"):
            raise BridgeFailure("所选拆解会话当前没有正在运行的回合")
        active["client"].interrupt_turn(str(active["threadId"]), str(active["turnId"]), request_id=active["client"].next_request_id())
        self.progress.publish(identity, "status", "已请求停止拆解", "正在等待 Codex 中断当前回合。", "running")
        return {"accepted": True, "bizLine": biz_line, "programId": program_id, "requirementKey": requirement_key, "threadId": active["threadId"], "turnId": active["turnId"], "active": True}

    def _follow_planning(
        self,
        identity: tuple[str, int, str],
        client: AppServerClient,
        config: dict[str, Any],
        program_id: int,
        requirement_key: str,
        provider: str,
        session: dict[str, Any],
        thread_id: str,
        turn_id: str,
        model: str = "",
        reasoning_effort: str = "",
        fast_mode: bool = False,
        user_message: str = "",
        started_new_conversation: bool = False,
        confirm_write: bool = False,
        namer: threading.Thread | None = None,
        naming_outcome: dict[str, str] | None = None,
        first_conversation: bool = False,
    ) -> None:
        status = "failed"
        try:
            status = client.wait_turn(turn_id)
            entry = next(
                (item for item in session.get("catalog") or [] if str(item.get("threadId") or "") == thread_id),
                {},
            )
            title = str(entry.get("title") or "需求拆解")
            generated_title = ""
            reply = ""
            if status == "completed":
                turn = client.read_turn(thread_id, turn_id, client.next_request_id())
                reply = final_agent_text_from_output(execution_output(status, turn))
            # 只有新开聊天的首回合才自动命名；后续追问不能覆盖用户已经识别出的会话标题。
            if started_new_conversation:
                # 开聊时那轮命名一般早就回来了；万一还在跑，等它一下再决定要不要重命名。
                if namer is not None and namer.is_alive():
                    namer.join(CONVERSATION_TITLE_TIMEOUT_SECONDS)
                generated_title = str((naming_outcome or {}).get("title") or "")
                if not generated_title and status == "completed":
                    generated_title = self._name_conversation(
                        config, program_id, provider, model, reasoning_effort, fast_mode, user_message, reply,
                    )
                if generated_title:
                    title = generated_title
                    entry["title"] = title
                    self._rename_conversation(client, thread_id, title)
            # 命名放在释放本轮之前：面板是靠「回合结束」去取标题的，先放行会取到旧标题。
            if status == "completed":
                self._name_requirement_if_empty(
                    identity, config, program_id, requirement_key, provider, model, reasoning_effort, fast_mode,
                    user_message, client, thread_id, turn_id, generated_title, first_conversation,
                )
                try:
                    requirement = planner.requirement_record(config, program_id, requirement_key)
                    requirement_name = str(requirement.get("name") or "").strip() or generated_title or title
                except Exception:
                    requirement_name = generated_title or title or requirement_key
                temp_path = planning_temp_document_path(requirement_name, requirement_key, thread_id)
                if confirm_write:
                    delete_planning_temp_summary(temp_path)
                    session.pop("tempPath", None)
                else:
                    with self.lock:
                        current_run = self.active_runs.get(identity) or {}
                        round_messages = [
                            str(item).strip() for item in current_run.get("userMessages") or [user_message]
                            if str(item).strip()
                        ]
                    write_planning_temp_summary(
                        temp_path,
                        requirement_name,
                        requirement_key,
                        thread_id,
                        "\n\n".join(round_messages) or user_message,
                        reply,
                    )
                    session["tempPath"] = temp_path.as_posix()
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
                phase="planning",
                terminal_status=status,
            )
            session["result"] = self._planning_result(config, program_id, session["baseline"])
            session["turnId"] = turn_id
            for entry in session.get("catalog") or []:
                if entry.get("threadId") == thread_id:
                    entry["status"] = status
                    entry["active"] = False
                    entry["updatedAt"] = utc_now()
            self._save_planning_session(config, program_id, requirement_key, provider, session)
            self.progress.publish(
                identity,
                "status",
                "拆解已完成" if status == "completed" else "拆解未完成",
                "已同步本次创建的项目结构和任务列表。",
                status,
            )
        except Exception as exc:
            self.progress.publish(identity, "error", "同步拆解结果失败", str(exc), "failed")
            print(f"同步项目拆解结果失败：{program_id}: {exc}", file=sys.stderr, flush=True)
        finally:
            client.close()
            with self.lock:
                current = self.active_runs.get(identity)
                if current is not None and current.get("client") is client:
                    self.active.discard(identity)
                    self._release_active_run(identity)
