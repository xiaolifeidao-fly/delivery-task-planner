"""回合本身：开新会话、续轮、接管进行中的回合、跟进结果并回写面板。

所有阶段最终都汇到这里，差别只在提示词和产物落在哪。
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from typing import Any

import server as planner

from delivery_bridge.attachments_text import text_without_attachment_context
from delivery_bridge.clients import factory
from delivery_bridge.clients.claude import ClaudeCLIClient
from delivery_bridge.clients.codex import AppServerClient
from delivery_bridge.documents import document_set_entries
from delivery_bridge.errors import BridgeFailure
from delivery_bridge.executor_env import codex_environment
from delivery_bridge.item_keys import requirement_prototype_files
from delivery_bridge.payloads import config_biz_line, task_identity
from delivery_bridge.progress_events import generated_image_from_event, progress_event_of
from delivery_bridge.prompts.common import document_path_of, requirement_document_catalog
from delivery_bridge.prompts.conversation import build_conversation_prompt
from delivery_bridge.providers import provider_label, same_executor_purpose
from delivery_bridge.sessions import conversation_catalog, conversation_metadata, conversation_title
from delivery_bridge.turn_output import (
    SESSION_STATUS,
    corrupted_turn_reason,
    execution_output,
    final_agent_text_from_output,
    merged_execution_output,
    testing_verdict_from_output,
)


class TurnsMixin:
    def _conversation_mention_context(
        self,
        config: dict[str, Any],
        program_id: int,
        references: list[dict[str, str]],
        project_context: dict[str, Any] | None = None,
        current_requirement_key: str = "",
    ) -> list[str]:
        """Load authoritative @ references and the requirement/task that connects them."""
        if not references:
            return []
        context = project_context or planner.project_context(config, program_id)
        items = [item for item in context.get("items") or [] if isinstance(item, dict)]
        items_by_key = {str(item.get("itemKey") or ""): item for item in items}
        requirement_cache: dict[str, dict[str, Any]] = {}
        task_cache: dict[str, dict[str, Any]] = {}

        def requirement_of(key: str) -> dict[str, Any]:
            if key not in requirement_cache:
                requirement_cache[key] = planner.requirement_record(config, program_id, key)
            return requirement_cache[key]

        def task_of(key: str) -> dict[str, Any]:
            if key not in task_cache:
                task_cache[key] = self._task_detail(config, program_id, key)
            return task_cache[key]

        def readable_detail(value: Any, limit: int = 6000) -> str:
            text = str(value or "").strip()
            return text if len(text) <= limit else f"{text[:limit]}…（已截断）"

        lines = ["用户在本轮消息中 @ 了以下关联对象。它们是本轮的补充上下文，按需参考，不能改写当前任务或需求的边界："]
        for reference in references:
            kind = reference["kind"]
            key = reference["key"]
            if kind == "file":
                if not current_requirement_key:
                    raise BridgeFailure("文件引用只能用于需求编辑聊天")
                scope = str(reference.get("scope") or "")
                if scope == "requirement-prototype":
                    _, prototype_files = requirement_prototype_files(self.workspace, current_requirement_key)
                    allowed_files = {str(file.get("path") or ""): str(file.get("name") or "") for file in prototype_files}
                else:
                    directory, _, recursive = self._document_set_layout(
                        config, program_id, scope, current_requirement_key,
                    )
                    allowed_files = {
                        str(file.get("path") or ""): str(file.get("name") or "")
                        for file in document_set_entries(self.workspace, directory, recursive)
                    }
                if key not in allowed_files:
                    raise BridgeFailure("引用文件不存在或不属于当前需求")
                lines.extend([
                    f"@文件 {allowed_files[key] or Path(key).name}",
                    f"文件路径: {key}",
                    "这是当前需求的相关文档；需要正文时从工作区按上述路径读取。",
                ])
                continue
            if kind == "requirement":
                requirement = requirement_of(key)
                related_items = [item for item in items if str(item.get("requirementKey") or "") == key]
                related_lines = [
                    f"- {item.get('itemKey')}: {item.get('title') or item.get('itemKey')}"
                    f"（{item.get('phase') or '-'}/{item.get('status') or '-'}；需求文档：{document_path_of(item)}）"
                    for item in related_items[:30]
                ]
                lines.extend([
                    f"@需求 {key}: {requirement.get('name') or key}",
                    "需求详情:",
                    readable_detail(requirement.get("detail")) or "（未填写）",
                    "该需求关联的任务:",
                    *(related_lines or ["- 暂无任务"]),
                ])
                continue
            task = task_of(key)
            requirement_key = str(task.get("requirementKey") or "").strip()
            lines.extend([
                f"@任务 {key}: {task.get('title') or key}",
                f"任务说明: {readable_detail(task.get('description'), 4000) or '（未填写）'}",
                f"当前阶段: {task.get('phase') or 'requirement'}/{task.get('status') or 'todo'}",
                f"需求文档: {document_path_of(task)}",
            ])
            if requirement_key:
                requirement = requirement_of(requirement_key)
                lines.extend([
                    f"所属需求 {requirement_key}: {requirement.get('name') or requirement_key}",
                    "所属需求详情:",
                    readable_detail(requirement.get("detail")) or "（未填写）",
                ])
            elif key in items_by_key:
                lines.append("所属需求: 未关联")
        return lines

    def _session_binding(
        self,
        config: dict[str, Any],
        program_id: int,
        item_key: str,
        phase: str | None = None,
        provider: str = "codex",
    ) -> dict[str, Any] | None:
        if phase is None:
            task = self._task_detail(config, program_id, item_key)
            phase = str(task.get("phase") or "requirement")
        sessions = planner.request_api(
            config,
            "GET",
            "/delivery/item/execution-session",
            query={"programId": program_id, "itemKey": item_key, "phase": phase},
        ) or []
        if not isinstance(sessions, list):
            return None
        candidates = [
            session
            for session in sessions
            if isinstance(session, dict)
            and same_executor_purpose(session, "")
            and str(session.get("phase") or "requirement") == phase
        ]
        # 优先当前选中的工具；没有就回落到同阶段另一个工具留下的会话，别让列表凭空空掉。
        return next(
            (session for session in candidates if session.get("executorType") == provider),
            next(iter(candidates), None),
        )

    def _task_session_bindings(
        self,
        config: dict[str, Any],
        program_id: int,
        item_key: str,
        provider: str,
    ) -> list[dict[str, Any]]:
        """Return this task's execution sessions from every delivery phase.

        执行器不参与过滤：换成另一个工具之后，之前那批聊天也要留在列表里，正文再按线程
        自己的执行器去读。测试用例会话用的是带后缀的执行器类型，仍然要排除掉。
        """
        sessions = planner.request_api(
            config,
            "GET",
            "/delivery/item/execution-session",
            query={"programId": program_id, "itemKey": item_key},
        ) or []
        return [
            session
            for session in sessions
            if isinstance(session, dict) and same_executor_purpose(session, "")
        ]

    def _start_new_conversation(
        self,
        config: dict[str, Any],
        program_id: int,
        item_key: str,
        task: dict[str, Any],
        binding: dict[str, Any] | None,
        text: str,
        attachments: list[dict[str, Any]],
        model: str = "",
        provider: str = "codex",
        reasoning_effort: str = "",
        fast_mode: bool = False,
        mention_context: list[str] | None = None,
    ) -> dict[str, Any]:
        identity = task_identity(config_biz_line(config), program_id, item_key)
        with self.lock:
            if identity in self.active:
                raise BridgeFailure("该任务已经在本地执行中")
            self.active.add(identity)
        title = conversation_title(task, binding)
        client = factory.create_ai_client(
            provider,
            self.workspace,
            lambda message: self._publish_app_server_event(identity, message),
            codex_environment(config, program_id),
        )
        try:
            updated_task = self._claim_task(config, program_id, task, f"{provider_label(provider)} 已领取任务，正在创建新的执行会话。", provider)
        except Exception:
            client.close()
            self._release_failed_claim(config, program_id, updated_task, provider)
            with self.lock:
                self.active.discard(identity)
            raise
        try:
            self._migrate_legacy_task_outline(updated_task)
            catalog = requirement_document_catalog(
                (planner.project_context(config, program_id).get("items") or []),
                updated_task,
                self.workspace,
            )
            thread_id, turn_id = client.start_task(
                title,
                build_conversation_prompt(program_id, updated_task, text, self.workspace, catalog, mention_context),
                attachments,
                model,
                reasoning_effort=reasoning_effort,
                fast_mode=fast_mode,
            )
            metadata = conversation_metadata(
                binding,
                thread_id,
                turn_id,
                "running",
                title,
                str(task.get("phase") or "requirement"),
            )
            metadata.update({"workspace": self.workspace.name, "source": "task-board-conversation"})
            refreshed_binding = planner.request_api(
                config,
                "POST",
                "/delivery/item/execution-session/bind",
                body={
                    "programId": program_id,
                    "itemKey": item_key,
                    "executorType": provider,
                    "phase": str(task.get("phase") or "requirement"),
                    "progress": 0,
                    "externalSessionId": thread_id,
                    "status": "running",
                    "metadata": metadata,
                    "actorName": f"{provider}-http-bridge",
                },
            )
            with self.lock:
                self.active_runs[identity] = {
                    "client": client,
                    "threadId": thread_id,
                    "turnId": turn_id,
                    "task": updated_task,
                    "binding": refreshed_binding,
                    "config": config,
                    "provider": provider,
                }
        except Exception:
            client.close()
            self._release_failed_claim(config, program_id, updated_task, provider)
            with self.lock:
                self.active.discard(identity)
                self._release_active_run(identity)
            raise
        self.progress.publish(identity, "status", "已创建新的 Codex 会话", title, "running")
        threading.Thread(
            target=self._follow,
            args=(
                identity, client, config, program_id, item_key, updated_task, refreshed_binding, turn_id,
                text_without_attachment_context(text), model, reasoning_effort, fast_mode,
            ),
            daemon=True,
        ).start()
        return {
            "accepted": True,
            "bizLine": config_biz_line(config),
            "programId": program_id,
            "itemKey": item_key,
            "threadId": thread_id,
            "turnId": turn_id,
            "active": True,
        }

    def _start_follow_up_turn(
        self,
        config: dict[str, Any],
        program_id: int,
        item_key: str,
        task: dict[str, Any],
        binding: dict[str, Any],
        thread_id: str,
        text: str,
        attachments: list[dict[str, Any]],
        model: str = "",
        provider: str = "codex",
        reasoning_effort: str = "",
        fast_mode: bool = False,
    ) -> dict[str, Any]:
        identity = task_identity(config_biz_line(config), program_id, item_key)
        with self.lock:
            if identity in self.active:
                raise BridgeFailure("该任务已经在本地执行中")
            self.active.add(identity)
        client = factory.create_ai_client(
            provider,
            self.workspace,
            lambda message: self._publish_app_server_event(identity, message),
            codex_environment(config, program_id),
        )
        try:
            updated_task = self._claim_task(config, program_id, task, f"{provider_label(provider)} 已领取任务，正在现有会话中继续执行。", provider)
        except Exception:
            client.close()
            with self.lock:
                self.active.discard(identity)
            raise
        try:
            client.resume_thread(thread_id)
            turn_id = client.start_turn(
                thread_id,
                text,
                attachments,
                model=model,
                reasoning_effort=reasoning_effort,
                fast_mode=fast_mode,
            )
            metadata = conversation_metadata(
                binding,
                thread_id,
                turn_id,
                "running",
                phase=str(task.get("phase") or "requirement"),
            )
            metadata.update({"workspace": self.workspace.name, "source": "task-board-conversation"})
            refreshed_binding = planner.request_api(
                config,
                "POST",
                "/delivery/item/execution-session/bind",
                body={
                    "programId": program_id,
                    "itemKey": item_key,
                    "executorType": provider,
                    "phase": str(task.get("phase") or "requirement"),
                    "progress": 0,
                    "externalSessionId": thread_id,
                    "status": "running",
                    "metadata": metadata,
                    "actorName": f"{provider}-http-bridge",
                },
            )
            with self.lock:
                self.active_runs[identity] = {
                    "client": client,
                    "threadId": thread_id,
                    "turnId": turn_id,
                    "task": updated_task,
                    "binding": refreshed_binding,
                    "config": config,
                    "provider": provider,
                }
        except Exception:
            client.close()
            with self.lock:
                self.active.discard(identity)
                self._release_active_run(identity)
            raise
        self.progress.publish(identity, "status", "Codex 正在处理追加要求", text, "running")
        threading.Thread(
            target=self._follow,
            args=(identity, client, config, program_id, item_key, updated_task, refreshed_binding, turn_id),
            daemon=True,
        ).start()
        return {
            "accepted": True,
            "bizLine": config_biz_line(config),
            "programId": program_id,
            "itemKey": item_key,
            "threadId": thread_id,
            "turnId": turn_id,
            "active": True,
        }

    def _resume_active_turn(
        self,
        config: dict[str, Any],
        identity: tuple[str, int, str],
        task: dict[str, Any],
        binding: dict[str, Any],
        thread_id: str,
        turn_id: str,
        provider: str = "codex",
    ) -> dict[str, Any]:
        with self.lock:
            current = self.active_runs.get(identity)
            if current is not None:
                return current
            if identity in self.active:
                raise BridgeFailure("该任务正在恢复执行状态，请稍后重试")
            self.active.add(identity)
        client = factory.create_ai_client(
            provider,
            self.workspace,
            lambda message: self._publish_app_server_event(identity, message),
            codex_environment(config, identity[1]),
        )
        try:
            client.resume_thread(thread_id)
            active = {
                "client": client,
                "threadId": thread_id,
                "turnId": turn_id,
                "task": task,
                "binding": binding,
                "config": config,
                "provider": provider,
            }
            with self.lock:
                self.active_runs[identity] = active
        except Exception:
            client.close()
            with self.lock:
                self.active.discard(identity)
                self._release_active_run(identity)
            raise
        threading.Thread(
            target=self._follow,
            args=(identity, client, config, identity[1], identity[2], task, binding, turn_id),
            daemon=True,
        ).start()
        return active

    def _publish_app_server_event(self, identity: tuple[str, int, str], message: dict[str, Any]) -> None:
        generated = generated_image_from_event(message)
        if generated is not None:
            with self.lock:
                active = self.active_runs.get(identity)
            if active is not None:
                try:
                    self.attachments.save_generated_image(
                        config_biz_line(active.get("config") or {}),
                        identity[1],
                        identity[2],
                        str(active.get("threadId") or ""),
                        str(active.get("turnId") or ""),
                        generated[0],
                        generated[1],
                    )
                    self.progress.publish(identity, "file", "图片已生成", "可在聊天记录中预览", "success")
                except BridgeFailure as exc:
                    print(f"保存 Codex 生成图片失败：{identity[1]}/{identity[2]}: {exc}", file=sys.stderr, flush=True)
        event = progress_event_of(message)
        if event is not None:
            self.progress.publish(identity, *event)

    def _retry_corrupted_turn(
        self,
        identity: tuple[str, int, str],
        client: AppServerClient | ClaudeCLIClient,
        turn_id: str,
        turn_status: str,
        turn: dict[str, Any],
        phase: str,
        turn_prompt: str,
        model: str = "",
        reasoning_effort: str = "",
        fast_mode: bool = False,
    ) -> tuple[str, str, dict[str, Any], str]:
        """一轮没能发出任何工具调用就用同样的输入重跑一次，仍然不行就判失败。

        返回 `(turn_id, turn_status, turn, corrupted_reason)`。`corrupted_reason`
        非空表示重试后依然无效，此时状态已经被改成 `failed`，调用方不要把这一轮
        的文字当成产物存下去。
        """
        reason = corrupted_turn_reason(turn_status, turn, phase)
        if not reason:
            return turn_id, turn_status, turn, ""
        diagnostics = getattr(client, "stderr_tail", lambda limit=10: "")()
        print(
            f"检测到无效执行回合：{identity} {reason}" + (f"\napp-server stderr:\n{diagnostics}" if diagnostics else ""),
            file=sys.stderr,
            flush=True,
        )
        with self.lock:
            current = self.active_runs.get(identity)
            # 用户已经追加了新回合，那一轮有自己的 _follow，这里不该再插一轮进去。
            has_newer_turn = current is not None and str(current.get("turnId") or "") != turn_id
        if has_newer_turn or not turn_prompt.strip():
            return turn_id, "failed", turn, reason
        self.progress.publish(identity, "status", "本轮执行无效，正在自动重试", reason, "running")
        retried_id = client.start_turn(
            str(client.thread_id or ""),
            turn_prompt,
            request_id=client.next_request_id(),
            model=model,
            reasoning_effort=reasoning_effort,
            fast_mode=fast_mode,
        )
        with self.lock:
            current = self.active_runs.get(identity)
            if current is not None:
                current["turnId"] = retried_id
        retried_status = client.wait_turn(retried_id)
        retried_turn = client.read_turn(client.thread_id, retried_id, request_id=client.next_request_id())
        retried_reason = corrupted_turn_reason(retried_status, retried_turn, phase)
        if retried_reason:
            return retried_id, "failed", retried_turn, f"重试后依然无效：{retried_reason}"
        return retried_id, retried_status, retried_turn, ""

    def _follow(
        self,
        identity: tuple[str, int, str],
        client: AppServerClient,
        config: dict[str, Any],
        program_id: int,
        item_key: str,
        task: dict[str, Any],
        binding: dict[str, Any],
        turn_id: str,
        initial_message: str = "",
        model: str = "",
        reasoning_effort: str = "",
        fast_mode: bool = False,
        turn_prompt: str = "",
    ) -> None:
        provider = str((self.active_runs.get(identity) or {}).get("provider") or "codex")
        try:
            turn_status = client.wait_turn(turn_id)
            turn = client.read_turn(client.thread_id, turn_id, request_id=client.next_request_id())
            task_name = str(task.get("title") or item_key)
            phase = str(task.get("phase") or "requirement")
            turn_id, turn_status, turn, corrupted_reason = self._retry_corrupted_turn(
                identity, client, turn_id, turn_status, turn, phase, turn_prompt,
                model, reasoning_effort, fast_mode,
            )
            thread_id = str(client.thread_id or binding.get("externalSessionId") or "")
            entry = next((item for item in conversation_catalog(binding) if item.get("threadId") == thread_id), {})
            title = str(entry.get("title") or task_name)
            # 任务聊天也只在新开窗口的首回合命名，避免后续追问把既有标题改掉。
            if turn_status == "completed" and initial_message.strip():
                reply = final_agent_text_from_output(execution_output(turn_status, turn))
                generated_title = self._name_conversation(
                    config, program_id, provider, model, reasoning_effort, fast_mode, initial_message, reply,
                )
                if generated_title:
                    title = generated_title
                    self._rename_conversation(client, thread_id, title)
            self._archive_terminal_chat(
                client,
                config=config,
                program_id=program_id,
                resource_kind="task",
                resource_key=item_key,
                resource_name=task_name,
                requirement_key=str(task.get("requirementKey") or ""),
                conversation_title=title,
                thread_id=thread_id,
                provider=provider,
                phase=phase,
                terminal_status=turn_status,
            )
            with self.lock:
                current = self.active_runs.get(identity)
                has_newer_turn = current is not None and str(current.get("turnId") or "") != turn_id
            if not has_newer_turn:
                self._sync_result(
                    config,
                    program_id,
                    item_key,
                    task,
                    binding,
                    turn_id,
                    turn_status,
                    # 无效回合不入库：那段自称完成的文字既不是产物，也会污染累积的产物文档。
                    "" if corrupted_reason else execution_output(turn_status, turn),
                    provider,
                    title,
                    corrupted_reason,
                )
            # Closing app-server flushes the final turn to the shared Codex session
            # store. Consumers notified before this point can observe 100% progress
            # while still reading the previous conversation snapshot.
            client.close()
            self.progress.publish(
                identity,
                "error" if corrupted_reason else "status",
                "任务已完成" if turn_status == "completed" else "任务执行未完成",
                corrupted_reason or f"结果已同步到任务面板，状态：{turn_status}",
                turn_status,
            )
        except Exception as exc:
            self.progress.publish(identity, "error", "同步执行结果失败", str(exc), "failed")
            print(f"同步 Codex 执行结果失败：{program_id}/{item_key}: {exc}", file=sys.stderr, flush=True)
        finally:
            client.close()
            with self.lock:
                current = self.active_runs.get(identity)
                if current is None or current.get("client") is client:
                    self.active.discard(identity)
                    self._release_active_run(identity)

    def _sync_result(
        self,
        config: dict[str, Any],
        program_id: int,
        item_key: str,
        task: dict[str, Any],
        binding: dict[str, Any],
        turn_id: str,
        turn_status: str,
        execution_output_text: str = "",
        provider: str = "codex",
        conversation_title: str = "",
        failure_reason: str = "",
    ) -> None:
        session_status = SESSION_STATUS.get(turn_status, "blocked")
        phase = str(task.get("phase") or "requirement")
        task_status = "done" if turn_status == "completed" else "blocked"
        testing_verdict = testing_verdict_from_output(execution_output_text) if phase == "testing" else ""
        if phase == "testing" and testing_verdict != "通过":
            # A completed Codex turn means the report was produced. The task is
            # done only when that report explicitly accepts the deliverable.
            task_status = "blocked"
        # Keep the task authoritative. If session closing fails, reconciliation can
        # retry it without leaving the task stuck in its current phase.
        current_task = self._task_detail(config, program_id, item_key)
        if current_task.get("status") not in {"dropped", "done"}:
            output_field = {"development": "actionOutput", "testing": "testingReport"}.get(phase)
            patch_body = {
                "programId": program_id,
                "itemKey": item_key,
                "version": int(current_task["version"]),
                "status": task_status,
                "progress": 100 if task_status == "done" else int(current_task.get("progress") or 0),
                "comment": (
                    f"{provider_label(provider)} {phase} 阶段结束，状态：{turn_status}。"
                    + (f"验收判定：{testing_verdict or '缺失'}。" if phase == "testing" else "")
                    + (f"本轮判定为无效执行：{failure_reason}。" if failure_reason else "")
                ),
                "actorName": f"{provider}-http-bridge",
            }
            if output_field:
                # 追加回合只产出增量：覆盖会把同一阶段前几轮的产物文档整段丢掉。
                output = merged_execution_output(
                    str(current_task.get(output_field) or ""), execution_output_text
                )
                patch_body[output_field] = output
                if phase == "testing" and output.strip():
                    # 测试报告和测试用例都以项目内相对路径作为权威预览源；
                    # 不把工作区绝对路径传给面板，避免浏览器按 URL 打开时报 404。
                    self._persist_task_testing_report(item_key, output)
            if phase == "requirement" and turn_status == "completed":
                requirement_text = final_agent_text_from_output(execution_output_text)
                self._persist_requirement_document(current_task, requirement_text)
            self._request_with_retry(
                config,
                "/delivery/item/patch",
                patch_body,
            )
        session_sync = {
            "bizLine": config_biz_line(config),
            "programId": program_id,
            "itemKey": item_key,
            "executorType": provider,
            "phase": phase,
            "version": int(binding["version"]),
            "status": session_status,
            "progress": 100 if turn_status == "completed" else 0,
            "metadata": {
                **conversation_metadata(
                    binding,
                    str(binding.get("externalSessionId") or ""),
                    turn_id,
                    turn_status,
                    conversation_title,
                    phase=phase,
                ),
                "workspace": self.workspace.name,
            },
            "actorName": f"{provider}-http-bridge",
        }
        self.pending_session_syncs.add(session_sync)
        try:
            self._request_with_retry(config, "/delivery/item/execution-session/status", session_sync)
        except Exception as exc:
            print(
                f"关闭执行会话失败，已加入后台重试：{program_id}/{item_key}: {exc}",
                file=sys.stderr,
                flush=True,
            )
        else:
            self.pending_session_syncs.remove(session_sync)
