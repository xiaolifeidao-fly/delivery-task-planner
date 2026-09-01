"""会话标题与需求名称的自动生成。

首回合结束后另起一轮短会话补标题；需求名称留空时复用同一套生成与清洗规则。
"""

from __future__ import annotations

import sys
import threading
from typing import Any

import server as planner

from delivery_bridge.clients import factory
from delivery_bridge.clients.claude import ClaudeCLIClient
from delivery_bridge.clients.codex import AppServerClient
from delivery_bridge.executor_env import codex_environment
from delivery_bridge.prompts.conversation import (
    CONVERSATION_TITLE_TIMEOUT_SECONDS,
    build_conversation_title_prompt,
    conversation_title_of,
    placeholder_requirement_name,
)
from delivery_bridge.timeutil import utc_now
from delivery_bridge.turn_output import execution_output, final_agent_text_from_output


class NamingMixin:
    def _name_conversation(
        self,
        config: dict[str, Any],
        program_id: int,
        provider: str,
        model: str,
        reasoning_effort: str,
        fast_mode: bool,
        user_message: str,
        reply: str,
    ) -> str:
        """起一轮只读短会话，为新聊天生成标题；超时则保留原始占位标题。"""
        client = factory.create_ai_client(
            provider,
            self.workspace,
            None,
            codex_environment(config, program_id, write_allowed=False, provider=provider),
        )
        try:
            thread_id, turn_id = client.start_task(
                "聊天自动命名",
                build_conversation_title_prompt(user_message, reply),
                None,
                model,
                reasoning_effort=reasoning_effort,
                fast_mode=fast_mode,
            )
            outcome: dict[str, str] = {}

            def wait() -> None:
                try:
                    outcome["status"] = client.wait_turn(turn_id)
                except Exception as exc:
                    # 起名失败只该丢掉这个标题，不该在日志里留一串没人接的线程异常。
                    print(f"聊天自动命名等待失败：{exc}", file=sys.stderr, flush=True)

            waiter = threading.Thread(target=wait, daemon=True)
            waiter.start()
            waiter.join(CONVERSATION_TITLE_TIMEOUT_SECONDS)
            if waiter.is_alive():
                return ""
            status = outcome.get("status") or "failed"
            if status != "completed":
                return ""
            turn = client.read_turn(thread_id, turn_id, client.next_request_id())
            return conversation_title_of(final_agent_text_from_output(execution_output(status, turn)))
        finally:
            client.close()

    @staticmethod
    def _rename_conversation(client: AppServerClient | ClaudeCLIClient, thread_id: str, title: str) -> None:
        """Keep the provider-native title aligned when that provider supports renaming."""
        if not title:
            return
        try:
            client.set_thread_name(thread_id, title, request_id=client.next_request_id())
        except Exception as exc:
            # 面板的会话目录仍会保存新标题；原生线程重命名失败不能影响交付结果。
            print(f"同步原生会话标题失败：{thread_id}: {exc}", file=sys.stderr, flush=True)

    @staticmethod
    def _write_requirement_name(
        config: dict[str, Any],
        program_id: int,
        requirement_key: str,
        name: str,
        replace: str = "",
    ) -> None:
        """把生成的标题落到需求名称上。

        `replace` 是这次允许覆盖的旧名称：留空表示只写空名称，传占位名表示只换掉那个占位名。
        名称是用户随时能自己改的字段，服务端按同一条件再判一次，两边都不会盖掉用户填的名字。
        """
        name = str(name or "").strip()
        replace = str(replace or "").strip()
        if not requirement_key or not name or name == replace:
            return
        try:
            requirement = planner.requirement_record(config, program_id, requirement_key)
            if str(requirement.get("name") or "").strip() != replace:
                return
            planner.request_api(
                config,
                "POST",
                "/delivery/requirement/name/update",
                body={
                    "programId": program_id,
                    "requirementKey": requirement_key,
                    "name": name,
                    "replaceName": replace,
                },
            )
        except Exception as exc:
            print(f"回写需求名称失败：{program_id}/{requirement_key}: {exc}", file=sys.stderr, flush=True)

    def _start_conversation_naming(
        self,
        identity: tuple[str, int, str],
        config: dict[str, Any],
        program_id: int,
        requirement_key: str,
        provider: str,
        model: str,
        fast_mode: bool,
        user_message: str,
        session: dict[str, Any],
        thread_id: str,
        first_conversation: bool = False,
    ) -> tuple[threading.Thread | None, dict[str, str]]:
        """新开聊天时就先定名字：标题只看用户的首条说明，不等首轮回复。

        面板上一条没名字的需求只能按需求编号显示，等整轮拆解跑完才补名字太晚；
        这一轮命名和拆解并行跑，会话标题和需求名称都在开聊的几十秒内确定下来。
        起名本身也要跑一轮模型，那几秒里名称还是空的，所以先用首条消息的前几个字占位，
        AI 的标题回来再把占位名换掉。整段失败都不影响拆解结果，回合结束时还会兜底补一次。

        `first_conversation` 表示这条需求此前一次拆解会话都没有。这种需求即使已经带着
        名字（从编辑入口进来的手填名），首轮也要按用户的问题重定一次标题：把当前这个名字
        当成允许覆盖的旧值，而不是写占位名去盖掉它。
        """
        outcome: dict[str, str] = {}
        # Git 新需求已经用需求编号作临时名：不要再按用户首句并行起名，
        # 必须等首轮执行器返回反馈后，再根据完整问答生成正式标题。
        try:
            current_name = str((planner.requirement_record(config, program_id, requirement_key) or {}).get("name") or "").strip()
        except Exception:
            current_name = ""
        if current_name == requirement_key:
            outcome["placeholder"] = requirement_key
            return None, outcome
        placeholder = placeholder_requirement_name(user_message)
        if current_name and first_conversation:
            # 已经有名字、但一次都没聊过：不写占位名（面板上先留着用户看得懂的原名），
            # 直接把这个名字作为允许被首轮标题覆盖的旧值。
            placeholder = current_name
            outcome["placeholder"] = current_name
        elif not current_name and placeholder:
            # 占位名同步写：这一步只是一个接口调用，要赶在本次请求返回前落库，用户才会立刻看到。
            self._write_requirement_name(config, program_id, requirement_key, placeholder)
            outcome["placeholder"] = placeholder

        def run() -> None:
            try:
                # 起名只看一句话，用最低推理强度跑：名字要在用户还盯着屏幕的时候就出来。
                title = self._name_conversation(
                    config, program_id, provider, model, "low", fast_mode, user_message, "",
                )
                if not title:
                    return
                outcome["title"] = title
                for entry in session.get("catalog") or []:
                    if str(entry.get("threadId") or "") == thread_id:
                        entry["title"] = title
                        entry["updatedAt"] = utc_now()
                self._save_planning_session(config, program_id, requirement_key, provider, session)
                self._write_requirement_name(config, program_id, requirement_key, title, placeholder)
                self.progress.publish(identity, "status", "已确定需求标题", title, "running")
            except Exception as exc:
                print(f"开聊命名失败：{program_id}/{requirement_key}: {exc}", file=sys.stderr, flush=True)

        namer = threading.Thread(target=run, daemon=True)
        namer.start()
        return namer, outcome

    def _name_requirement_if_empty(
        self,
        identity: tuple[str, int, str],
        config: dict[str, Any],
        program_id: int,
        requirement_key: str,
        provider: str,
        model: str,
        reasoning_effort: str,
        fast_mode: bool,
        user_message: str,
        client: AppServerClient,
        thread_id: str,
        turn_id: str,
        suggested_name: str = "",
        first_conversation: bool = False,
    ) -> None:
        """新建需求允许不填名称：这一轮聊完就按聊天内容补上标题。

        开聊时占位名可能已经写进去了，所以「还没起过名」有两种样子：名称为空，
        或者名称就是本轮首条消息的那个占位名。除此之外一律不动 —— 名称是用户随时能自己
        改的字段，服务端按同一条件再判一次。整段失败都不影响拆解结果。

        `first_conversation` 是这条需求的第一次拆解会话：手填过名字的需求也要按首轮问答
        重定标题，所以此时当前名称本身就是允许被覆盖的旧值（开聊那轮并行命名没能出标题时
        才轮得到这里兜底）。
        """
        if not requirement_key:
            return
        try:
            requirement = planner.requirement_record(config, program_id, requirement_key)
        except Exception as exc:
            print(f"读取需求名称失败：{program_id}/{requirement_key}: {exc}", file=sys.stderr, flush=True)
            return
        current = str(requirement.get("name") or "").strip()
        # 开聊那轮已经把标题写进去了：这里不能再起一个名字把它换掉。
        if current and current == suggested_name.strip():
            return
        placeholder = placeholder_requirement_name(user_message)
        # Git 新需求的临时名称是需求编号；首轮 AI 回复完成后允许把它替换成正式标题。
        allowed_placeholders = {placeholder, requirement_key}
        if current and current not in allowed_placeholders and not first_conversation:
            return
        self.progress.publish(identity, "status", "正在生成需求标题", "需求名称还没定，正在按本轮聊天内容生成标题。", "running")
        try:
            name = suggested_name.strip()
            if not name:
                turn = client.read_turn(thread_id, turn_id, client.next_request_id())
                reply = final_agent_text_from_output(execution_output("completed", turn))
                name = self._name_conversation(config, program_id, provider, model, reasoning_effort, fast_mode, user_message, reply)
            if not name:
                return
            self._write_requirement_name(config, program_id, requirement_key, name, current)
        except Exception as exc:
            print(f"生成需求标题失败：{program_id}/{requirement_key}: {exc}", file=sys.stderr, flush=True)
