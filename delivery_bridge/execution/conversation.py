"""任务聊天与业务访谈聊天，以及停止入口。
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from typing import Any

from delivery_bridge.artifacts import ConversationAttachmentStore
from delivery_bridge.attachments_text import message_with_attachments
from delivery_bridge.clients import factory
from delivery_bridge.clients.codex import AppServerClient
from delivery_bridge.clients.pool import (
    ACTIVE_THREAD_READ_TIMEOUT_SECONDS,
    THREAD_READERS,
    read_thread_or_empty,
)
from delivery_bridge.errors import BridgeFailure
from delivery_bridge.executor_env import codex_environment
from delivery_bridge.payloads import (
    biz_line_of,
    business_item_key_of,
    config_biz_line,
    request_scoped_config,
    task_identity,
    validate_business_conversation_payload,
    validate_conversation_payload,
    validate_task_identity,
)
from delivery_bridge.prompt_context import with_mention_context
from delivery_bridge.prompts.common import follow_up_context_lines
from delivery_bridge.providers import DEFAULT_BIZ_LINE, ai_provider_of, executor_provider_of, program_id_of
from delivery_bridge.sessions import conversation_catalog, merged_conversation_catalog, turn_already_finished
from delivery_bridge.turn_view import ensure_terminal_result, serialize_turns


class ConversationMixin:
    def conversation(
        self,
        program_id: int,
        item_key: str,
        selected_thread_id: str = "",
        biz_line: str = DEFAULT_BIZ_LINE,
        config: dict[str, Any] | None = None,
        provider: str = "codex",
    ) -> dict[str, Any]:
        provider = ai_provider_of(provider)
        config = request_scoped_config(config, biz_line, program_id)
        biz_line = config_biz_line(config)
        identity = task_identity(biz_line, program_id, item_key)
        task = self._task_detail(config, program_id, item_key)
        current_binding = self._session_binding(config, program_id, item_key, str(task.get("phase") or "requirement"), provider)
        bindings = self._task_session_bindings(config, program_id, item_key, provider)
        catalog, binding_by_thread = merged_conversation_catalog(bindings)
        current_thread_id = str((current_binding or {}).get("externalSessionId") or "")
        known_thread_ids = {str(entry["threadId"]) for entry in catalog}
        if selected_thread_id and selected_thread_id not in known_thread_ids:
            raise BridgeFailure("所选 Codex 会话不存在")
        thread_id = selected_thread_id or current_thread_id or (catalog[0]["threadId"] if catalog else "")
        binding = binding_by_thread.get(thread_id, current_binding)
        # 线程正文在它自己那个执行器的会话缓存里：读和续都跟着线程走，不跟当前选中的工具走。
        provider = executor_provider_of(binding, provider)
        current_thread_id = str((binding or {}).get("externalSessionId") or "")
        if not thread_id:
            return {
                "bizLine": biz_line,
                "programId": program_id,
                "itemKey": item_key,
                "threadId": "",
                "executorType": provider,
                "turns": [],
                "conversations": catalog,
                "active": False,
                "taskHasActiveConversation": any(session.get("status") == "running" for session in bindings),
                "taskStatus": str(task.get("status") or "todo"),
                "taskPhase": str(task.get("phase") or "requirement"),
                "taskProgress": int(task.get("progress") or 0),
                "sessionPhase": str((current_binding or {}).get("phase") or task.get("phase") or "requirement"),
                "sessionProgress": int((current_binding or {}).get("progress") or 0),
            }
        with self.lock:
            active = self.active_runs.get(identity)
        task_has_active_conversation = active is not None or any(session.get("status") == "running" for session in bindings)
        active_for_thread = active if active is not None and str(active.get("threadId") or "") == thread_id else None
        if active_for_thread is None:
            metadata = (binding or {}).get("metadata") or {}
            turn_id = str(metadata.get("turnId") or "") if isinstance(metadata, dict) else ""
            if binding and binding.get("status") == "running" and current_thread_id == thread_id and turn_id:
                try:
                    active_for_thread = self._resume_active_turn(config, identity, task, binding, thread_id, turn_id, provider)
                except Exception as exc:
                    print(f"恢复 Codex 执行会话失败：{program_id}/{item_key}: {exc}", file=sys.stderr, flush=True)
        live_client = active_for_thread["client"] if active_for_thread is not None else None
        thread = self._read_thread_with_workspace_archive(
            live_client, thread_id, "task", item_key, config, program_id,
            provider=provider,
            environment=codex_environment(config, program_id),
        )
        self.attachments.recover_generated_images(config_biz_line(config), program_id, item_key, thread_id)
        turns = ensure_terminal_result(
            serialize_turns(
                thread.get("turns") or [],
                lambda attachment_ids: [
                    ConversationAttachmentStore._public(attachment)
                    for attachment in self.attachments.resolve(program_id, item_key, attachment_ids)
                ],
                lambda paths: self.artifacts.register(config_biz_line(config), program_id, item_key, paths),
                lambda turn_id: self.attachments.generated_for_turn(
                    program_id, item_key, thread_id, turn_id
                ),
            ),
            task,
            binding,
        )
        for entry in catalog:
            entry["active"] = bool(
                entry["threadId"] == str((active or {}).get("threadId") or "")
                or bool(
                    (binding_by_thread.get(str(entry.get("threadId") or "")) or {}).get("status") == "running"
                    and str((binding_by_thread.get(str(entry.get("threadId") or "")) or {}).get("externalSessionId") or "") == entry["threadId"]
                )
            )
        return {
            "bizLine": biz_line,
            "programId": program_id,
            "itemKey": item_key,
            "threadId": thread_id,
            "executorType": provider,
            "turns": turns,
            "conversations": catalog,
            "active": active_for_thread is not None,
            "taskHasActiveConversation": task_has_active_conversation,
            "activeTurnId": str((active_for_thread or {}).get("turnId") or ""),
            "taskStatus": str(task.get("status") or "todo"),
            "taskPhase": str(task.get("phase") or "requirement"),
            "taskProgress": int(task.get("progress") or 0),
            "sessionPhase": str((binding or {}).get("phase") or task.get("phase") or "requirement"),
            "sessionProgress": int((binding or {}).get("progress") or 0),
        }

    @staticmethod
    def _business_conversation_identity(program_id: int, item_key: str) -> tuple[str, int, str]:
        return task_identity("", program_id, f"__business_intake__:{item_key}")

    def business_conversation(
        self,
        program_id: int,
        item_key: str,
        thread_id: str = "",
        provider: str = "codex",
    ) -> dict[str, Any]:
        """Return a business-side conversation without touching delivery APIs.

        Business intake is deliberately independent from a delivery item. Its
        server has already supplied the project context and prompt, while this
        bridge only owns the persisted Codex thread in the business workspace.
        """
        provider = ai_provider_of(provider)
        if provider != "codex":
            raise BridgeFailure("业务访谈仅支持 Codex")
        program_id = program_id_of(program_id)
        item_key = business_item_key_of(item_key)
        thread_id = str(thread_id or "").strip()
        identity = self._business_conversation_identity(program_id, item_key)
        with self.lock:
            active = self.active_runs.get(identity)
        active_for_thread = active if active is not None and str(active.get("threadId") or "") == thread_id else None
        turns: list[dict[str, Any]] = []
        if thread_id:
            if active_for_thread is not None:
                thread = read_thread_or_empty(active_for_thread["client"], thread_id, ACTIVE_THREAD_READ_TIMEOUT_SECONDS)
            else:
                try:
                    thread = THREAD_READERS.read(provider, self.workspace, None, thread_id)
                except (BridgeFailure, OSError, ValueError) as exc:
                    print(f"读取业务访谈会话失败，按空会话处理：{thread_id}: {exc}", file=sys.stderr, flush=True)
                    thread = {}
            turns = serialize_turns(thread.get("turns") if isinstance(thread, dict) else [])
        return {
            "programId": program_id,
            "itemKey": item_key,
            "threadId": thread_id,
            "executorType": provider,
            "turns": turns,
            "conversations": [],
            "active": active_for_thread is not None,
            "activeTurnId": str((active_for_thread or {}).get("turnId") or ""),
        }

    def save_business_attachments(self, program_id: int, item_key: str, uploads: list[dict[str, Any]]) -> dict[str, Any]:
        """Store business-side uploads inside the business workspace.

        业务访谈不挂在交付任务上，没有面板凭证可验；能约束的是工作目录本身：
        目录由 for_business_workspace 在受控根目录下解析，附件只会落在里面。
        """
        program_id = program_id_of(program_id)
        item_key = business_item_key_of(item_key)
        return {"attachments": self.attachments.save(DEFAULT_BIZ_LINE, program_id, item_key, uploads)}

    def business_attachment(self, program_id: int, item_key: str, attachment_id: str) -> tuple[dict[str, Any], Path]:
        """Read one stored business attachment back for the console preview."""
        program_id = program_id_of(program_id)
        item_key = business_item_key_of(item_key)
        manifest, path = self.attachments.download(attachment_id)
        if manifest.get("programId") != program_id or manifest.get("itemKey") != item_key:
            raise BridgeFailure("附件不属于当前业务诉求")
        return manifest, path

    def send_business_conversation(self, raw: Any) -> dict[str, Any]:
        """Start or continue an AI interview in a server-created workspace."""
        program_id, item_key, message, thread_id, model, reasoning_effort, attachment_ids = validate_business_conversation_payload(raw)
        attachments = self.attachments.resolve(program_id, item_key, attachment_ids) if attachment_ids else []
        identity = self._business_conversation_identity(program_id, item_key)
        with self.lock:
            active = self.active_runs.get(identity)
        if active is not None:
            if thread_id and thread_id != str(active.get("threadId") or ""):
                raise BridgeFailure("该业务诉求已有正在运行的访谈会话")
            active["client"].steer_turn(
                str(active["threadId"]), str(active["turnId"]), message, attachments,
                request_id=active["client"].next_request_id(),
            )
            return {
                "accepted": True, "programId": program_id, "itemKey": item_key,
                "threadId": active["threadId"], "turnId": active["turnId"], "active": True,
            }

        with self.lock:
            if identity in self.active:
                raise BridgeFailure("该业务诉求正在创建访谈会话，请稍后重试")
            self.active.add(identity)
        client = factory.create_ai_client("codex", self.workspace)
        try:
            if thread_id:
                client.resume_thread(thread_id)
                turn_id = client.start_turn(
                    thread_id, message, attachments, request_id=client.next_request_id(),
                    model=model, reasoning_effort=reasoning_effort,
                )
            else:
                thread_id, turn_id = client.start_task(
                    f"业务诉求 · {item_key}", message, attachments,
                    model=model, reasoning_effort=reasoning_effort,
                )
            with self.lock:
                self.active_runs[identity] = {
                    "client": client,
                    "threadId": thread_id,
                    "turnId": turn_id,
                    "businessIntake": True,
                    "provider": "codex",
                }
        except Exception:
            client.close()
            with self.lock:
                self.active.discard(identity)
                self._release_active_run(identity)
            raise
        threading.Thread(
            target=self._follow_business_conversation,
            args=(identity, client, turn_id),
            daemon=True,
        ).start()
        return {
            "accepted": True, "programId": program_id, "itemKey": item_key,
            "threadId": thread_id, "turnId": turn_id, "active": True,
        }

    def _follow_business_conversation(
        self,
        identity: tuple[str, int, str],
        client: AppServerClient,
        turn_id: str,
    ) -> None:
        try:
            client.wait_turn(turn_id)
        except Exception as exc:
            print(f"业务访谈执行失败：{identity[1]}/{identity[2]}: {exc}", file=sys.stderr, flush=True)
        finally:
            client.close()
            with self.lock:
                current = self.active_runs.get(identity)
                if current is None or current.get("client") is client:
                    self.active.discard(identity)
                    self._release_active_run(identity)

    def upload_conversation_attachments(
        self,
        biz_line: str,
        program_id: int,
        item_key: str,
        uploads: list[dict[str, Any]],
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not program_id or not item_key:
            raise BridgeFailure("缺少项目或任务标识")
        config = request_scoped_config(config, biz_line, program_id)
        biz_line = config_biz_line(config)
        return {"bizLine": biz_line, "attachments": self.attachments.save(biz_line, program_id, item_key, uploads)}

    def send_conversation(self, raw: Any, config: dict[str, Any] | None = None) -> dict[str, Any]:
        provider = ai_provider_of(raw)
        biz_line = biz_line_of(raw)
        program_id, item_key, text, requested_thread_id, new_conversation, attachment_ids, model, reasoning_effort, fast_mode, references = validate_conversation_payload(raw)
        config = request_scoped_config(config, biz_line, program_id)
        biz_line = config_biz_line(config)
        attachments = self.attachments.resolve(program_id, item_key, attachment_ids)
        message = message_with_attachments(text, attachments)
        mention_context = self._conversation_mention_context(config, program_id, references)
        identity = task_identity(biz_line, program_id, item_key)
        with self.lock:
            active = self.active_runs.get(identity)
        if active is not None:
            if new_conversation or (requested_thread_id and requested_thread_id != active["threadId"]):
                raise BridgeFailure("该任务已有正在运行的 Codex 会话，请先停止或等待当前回合结束")
            client = active["client"]
            client.steer_turn(
                active["threadId"],
                active["turnId"],
                with_mention_context(
                    message, [*follow_up_context_lines(active.get("task") or {"itemKey": item_key}), *mention_context]
                ),
                attachments,
                request_id=client.next_request_id(),
            )
            self.progress.publish(identity, "message", "已追加要求", text or "已添加附件", "running")
            return {
                "accepted": True,
                "bizLine": biz_line,
                "programId": program_id,
                "itemKey": item_key,
                "threadId": active["threadId"],
                "turnId": active["turnId"],
                "active": True,
            }

        task = self._task_detail(config, program_id, item_key)
        mentioned_message = with_mention_context(message, [*follow_up_context_lines(task), *mention_context])
        binding = self._session_binding(config, program_id, item_key, str(task.get("phase") or "requirement"), provider)
        current_thread_id = str((binding or {}).get("externalSessionId") or "")
        catalog = conversation_catalog(binding)
        known_thread_ids = {str(entry["threadId"]) for entry in catalog}
        if requested_thread_id and requested_thread_id not in known_thread_ids:
            raise BridgeFailure("所选 Codex 会话不存在")
        if new_conversation:
            if binding and binding.get("status") == "running":
                raise BridgeFailure("该任务已有正在运行的 Codex 会话，请先停止或等待当前回合结束")
            return self._start_new_conversation(
                config, program_id, item_key, task, binding, message, attachments, model, provider, reasoning_effort, fast_mode, mention_context
            )
        thread_id = requested_thread_id or current_thread_id
        # 续已有会话只能用这条线程自己的执行器，换工具读不到它的正文。
        provider = executor_provider_of(binding, provider)
        metadata = (binding or {}).get("metadata") or {}
        running_turn_id = str(metadata.get("turnId") or "") if isinstance(metadata, dict) else ""
        if binding and binding.get("status") == "running" and thread_id == current_thread_id and running_turn_id:
            active = self._resume_active_turn(config, identity, task, binding, thread_id, running_turn_id, provider)
            client = active["client"]
            client.steer_turn(thread_id, running_turn_id, mentioned_message, attachments, request_id=client.next_request_id())
            self.progress.publish(identity, "message", "已追加要求", text or "已添加附件", "running")
            return {
                "accepted": True,
                "bizLine": biz_line,
                "programId": program_id,
                "itemKey": item_key,
                "threadId": thread_id,
                "turnId": running_turn_id,
                "active": True,
            }
        if not thread_id:
            return self.execute(
                {
                    "bizLine": biz_line,
                    "programId": program_id,
                    "task": task,
                    "followUp": message,
                    "conversationReferences": references,
                    "followUpAttachments": attachments,
                    "model": model,
                    "provider": provider,
                    **({"reasoningEffort": reasoning_effort} if reasoning_effort else {}),
                    **({"fastMode": True} if fast_mode else {}),
                },
                config=config,
            )
        return self._start_follow_up_turn(
            config, program_id, item_key, task, binding, thread_id, mentioned_message, attachments, model, provider, reasoning_effort, fast_mode
        )

    def stop_conversation(self, raw: Any, config: dict[str, Any] | None = None) -> dict[str, Any]:
        provider = ai_provider_of(raw)
        biz_line, program_id, item_key = validate_task_identity(raw)
        config = request_scoped_config(config, biz_line, program_id)
        biz_line = config_biz_line(config)
        requested_thread_id = str(raw.get("threadId") or "").strip() if isinstance(raw, dict) else ""
        identity = task_identity(biz_line, program_id, item_key)
        with self.lock:
            active = self.active_runs.get(identity)
        if active is not None and requested_thread_id and requested_thread_id != active["threadId"]:
            raise BridgeFailure("所选 Codex 会话当前没有正在运行的回合")
        if active is None:
            task = self._task_detail(config, program_id, item_key)
            binding = self._session_binding(config, program_id, item_key, str(task.get("phase") or "requirement"), provider)
            metadata = (binding or {}).get("metadata") or {}
            thread_id = str((binding or {}).get("externalSessionId") or "")
            turn_id = str(metadata.get("turnId") or "") if isinstance(metadata, dict) else ""
            if requested_thread_id and requested_thread_id != thread_id:
                raise BridgeFailure("所选 Codex 会话当前没有正在运行的回合")
            if not binding or binding.get("status") != "running" or not thread_id or not turn_id:
                raise BridgeFailure("该任务当前没有正在运行的 Codex 回合")
            active = self._resume_active_turn(
                config, identity, task, binding, thread_id, turn_id, executor_provider_of(binding, provider),
            )
        client = active["client"]
        try:
            client.interrupt_turn(active["threadId"], active["turnId"], request_id=client.next_request_id())
        except Exception as error:
            if not turn_already_finished(error):
                raise
            # 回合早就跑完了，本地记录是残留；跟随线程会把会话状态收尾，这里只把事实告诉调用方。
            self.progress.publish(
                identity, "status", "任务已经结束", "该任务当前没有正在运行的回合，状态稍后自动同步。", "success",
            )
            return {
                "accepted": True,
                "alreadyFinished": True,
                "bizLine": biz_line,
                "programId": program_id,
                "itemKey": item_key,
                "threadId": active["threadId"],
                "turnId": active["turnId"],
            }
        self.progress.publish(identity, "status", "已请求停止任务", "正在等待 Codex 中断当前回合。", "running")
        return {
            "accepted": True,
            "alreadyFinished": False,
            "bizLine": biz_line,
            "programId": program_id,
            "itemKey": item_key,
            "threadId": active["threadId"],
            "turnId": active["turnId"],
        }

    def stop_all_executions(self, raw: Any, config: dict[str, Any] | None = None) -> dict[str, Any]:
        """停掉一个项目下所有任务执行：中断在跑的回合，取消还在排队的批量/串行队列，
        并让任务面板把这个项目下还挂着的执行批次全部收尾。

        批次收尾必须走服务端而不是只清内存：断网、桥接重启或跑批线程已经退出时，
        本地根本不知道还有批次没关，而服务端那行 running 会把任务永久锁住，
        导致「再做一次」一直报「任务正在其他执行批次中」。

        只针对任务执行本身，需求拆解、测试、环境预设这些会话各有各的停止入口，不在这里连坐。
        """
        if not isinstance(raw, dict):
            raise BridgeFailure("请求体必须是 JSON 对象")
        biz_line = biz_line_of(raw)
        program_id = program_id_of(raw.get("programId"))
        config = request_scoped_config(config, biz_line, program_id)
        self._remember_config(program_id, config)
        biz_line = config_biz_line(config)
        with self.lock:
            queue_ids = sorted(qid for qid, pid in self.queue_programs.items() if pid == program_id)
            self.cancelled_queues.update(queue_ids)
            runs = [
                (identity, run) for identity, run in self.active_runs.items()
                if identity[0] == biz_line and identity[1] == program_id and run.get("task")
                and not run.get("taskTestingCases")
            ]
        stopped: list[str] = []
        finished: list[str] = []
        for identity, run in runs:
            client = run.get("client")
            thread_id = str(run.get("threadId") or "")
            turn_id = str(run.get("turnId") or "")
            if client is None or not thread_id or not turn_id:
                continue
            try:
                client.interrupt_turn(thread_id, turn_id, request_id=client.next_request_id())
            except Exception as error:
                if turn_already_finished(error):
                    finished.append(identity[2])
                    continue
                print(f"停止任务失败 {program_id}/{identity[2]}: {error}", file=sys.stderr, flush=True)
                continue
            stopped.append(identity[2])
            self.progress.publish(identity, "status", "已请求停止任务", "正在等待中断当前回合。", "running")
        # 本地有没有找到队列都要问服务端要一次收尾：僵尸批次恰恰是本地什么都不知道的那种。
        cancelled_batches = self._cancel_execution_batches(
            config,
            program_id,
            "用户在任务面板点了全部停止，批次被强制关闭。",
        )
        return {
            "accepted": True,
            "bizLine": biz_line,
            "programId": program_id,
            "itemKeys": sorted(stopped),
            "finishedItemKeys": sorted(finished),
            "queueIds": queue_ids,
            "cancelledBatchIds": cancelled_batches,
        }
