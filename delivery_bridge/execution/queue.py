"""任务执行的三种入口：单条、按依赖顺序、批量。

队列线程要能在下一个检查点自己收摊：中断当前回合只结束正在跑的那一条，
后面排队的任务靠取消标记拦住。
"""

from __future__ import annotations

import sys
import threading
import time
from typing import Any

import server as planner

from delivery_bridge.attachments_text import text_without_attachment_context
from delivery_bridge.clients import factory
from delivery_bridge.errors import BridgeFailure
from delivery_bridge.executor_env import codex_environment
from delivery_bridge.payloads import (
    biz_line_of,
    config_biz_line,
    request_scoped_config,
    task_identity,
    validate_execute_payload,
)
from delivery_bridge.prompts.common import requirement_document_catalog
from delivery_bridge.prompts.task import build_task_prompt
from delivery_bridge.providers import (
    ai_provider_of,
    fast_mode_of,
    program_id_of,
    provider_label,
    reasoning_effort_of,
)
from delivery_bridge.sessions import conversation_metadata, conversation_title
from delivery_bridge.turn_output import batch_task_outcome


class QueueMixin:
    def execute(self, raw: Any, batch_claim: bool = False, config: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = validate_execute_payload(raw)
        provider = payload["provider"]
        label = provider_label(provider)
        biz_line = ""
        program_id = payload["programId"]
        requested_task = payload["task"]
        config = request_scoped_config(config, biz_line, program_id)
        biz_line = config_biz_line(config)
        context = planner.project_context(config, program_id)
        payload["conversationMentionContext"] = self._conversation_mention_context(
            config,
            program_id,
            payload.get("conversationReferences") or [],
            context,
        )
        task = next((item for item in context["items"] if item.get("itemKey") == requested_task["itemKey"]), None)
        if task is None:
            raise BridgeFailure("任务不存在")
        if int(task.get("version") or 0) != int(requested_task["version"]):
            raise BridgeFailure("任务版本已变化，请刷新任务面板")
        phase = str(task.get("phase") or "requirement")
        execution_batch_id = str(payload.get("executionBatchId") or "").strip()
        if task.get("status") == "done" and not bool(payload.get("redo")):
            raise BridgeFailure("已完成任务不能再次执行")
        by_key = {str(item.get("itemKey")): item for item in context["items"]}
        queue_id = str(payload.get("batchId") or payload.get("sequenceId") or "")
        queue_satisfied: set[str] = set()
        if queue_id:
            with self.lock:
                queue_items = self.batch_tasks if payload.get("batchId") else self.sequence_tasks
                queue_identity = task_identity(biz_line, program_id, str(task.get("itemKey") or ""))
                if queue_identity in queue_items:
                    queue_satisfied = set(
                        (self.batch_satisfied if payload.get("batchId") else self.sequence_satisfied).get(queue_id, set())
                    )
        incomplete = [
            key for key in task.get("dependsOnItemKeys") or []
            if by_key.get(str(key), {}).get("status") != "done" and str(key) not in queue_satisfied
        ]
        if incomplete:
            raise BridgeFailure("前置任务尚未全部完成：" + ", ".join(incomplete))
        # 列表刻意不带大文本；实际启动前单独取详情，将完整需求给 Codex。
        detail = planner.request_api(
            config,
            "GET",
            "/delivery/item",
            query={"programId": program_id, "itemKey": str(task["itemKey"])},
        )
        if isinstance(detail, dict) and detail.get("itemKey"):
            task = detail
        payload["task"] = task
        payload["gitBranch"] = self._ensure_requirement_git_branch(config, program_id, task)
        item_key = str(task["itemKey"])
        identity = task_identity(biz_line, program_id, item_key)
        with self.lock:
            if identity in self.active:
                raise BridgeFailure("该任务已经在本地执行中")
            if identity in self.batch_tasks and not batch_claim:
                raise BridgeFailure("该任务正在等待批量启动")
            if batch_claim:
                self.batch_tasks.discard(identity)
            self.active.add(identity)

        self.progress.publish(identity, "status", "正在领取任务", task["title"])
        client = factory.create_ai_client(
            provider,
            self.workspace,
            lambda message: self._publish_app_server_event(identity, message),
            codex_environment(config, program_id),
        )
        try:
            updated_task = self._claim_task(config, program_id, task, f"{label} 已领取任务，正在创建本地执行会话。", provider)
            self._update_execution_batch_item(
                config,
                program_id,
                execution_batch_id,
                item_key,
                "running",
                provider=provider,
            )
        except Exception:
            client.close()
            with self.lock:
                self.active.discard(identity)
            raise
        payload["task"] = updated_task
        self._migrate_legacy_task_outline(updated_task)
        # 同需求的兄弟任务已经写好的文档：只挂清单，让执行器按相关性自己去读。
        payload["requirementDocuments"] = requirement_document_catalog(
            context.get("items") or [],
            updated_task,
            self.workspace,
        )
        binding: dict[str, Any] | None = None
        try:
            previous_binding = self._session_binding(config, program_id, item_key, phase, provider)
            title = conversation_title(task, previous_binding)
            # 留一份提示词原文：这一轮如果没能发出工具调用，要用同样的输入重试一次。
            task_prompt = build_task_prompt(payload, self.workspace)
            thread_id, turn_id = client.start_task(
                title,
                task_prompt,
                payload.get("followUpAttachments") if isinstance(payload.get("followUpAttachments"), list) else None,
                str(payload.get("model") or ""),
                reasoning_effort=str(payload.get("reasoningEffort") or ""),
                fast_mode=bool(payload.get("fastMode")),
            )
            metadata = conversation_metadata(
                previous_binding,
                thread_id,
                turn_id,
                "running",
                title,
                phase,
            )
            metadata.update({"workspace": self.workspace.name, "source": "task-board-http"})
            binding = planner.request_api(
                config,
                "POST",
                "/delivery/item/execution-session/bind",
                body={
                    "programId": program_id,
                    "itemKey": item_key,
                    "executorType": provider,
                    "phase": phase,
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
                    "binding": binding,
                    "config": config,
                    "provider": provider,
                }
        except Exception:
            client.close()
            self._release_failed_claim(config, program_id, updated_task, provider)
            if binding is not None:
                try:
                    planner.request_api(
                        config,
                        "POST",
                        "/delivery/item/execution-session/status",
                        body={
                            "programId": program_id,
                            "itemKey": item_key,
                            "executorType": provider,
                            "phase": phase,
                            "progress": 0,
                            "version": int(binding["version"]),
                            "status": "blocked",
                            "metadata": {
                                **conversation_metadata(binding, thread_id, turn_id, "blocked"),
                                "startupFailed": True,
                                "workspace": self.workspace.name,
                            },
                            "actorName": f"{provider}-http-bridge",
                        },
                    )
                except Exception as cleanup_error:
                    print(f"清理启动失败的执行会话失败：{program_id}/{item_key}: {cleanup_error}", file=sys.stderr, flush=True)
            with self.lock:
                self.active.discard(identity)
                self._release_active_run(identity)
            raise

        threading.Thread(
            target=self._follow,
            args=(
                identity, client, config, program_id, item_key, updated_task, binding, turn_id,
                text_without_attachment_context(str(payload.get("followUp") or "")),
                str(payload.get("model") or ""), str(payload.get("reasoningEffort") or ""), bool(payload.get("fastMode")),
                task_prompt,
            ),
            daemon=True,
        ).start()
        return {
            "accepted": True,
            "bizLine": biz_line,
            "programId": program_id,
            "itemKey": item_key,
            "threadId": thread_id,
        }

    def execute_sequence(self, raw: Any, config: dict[str, Any] | None = None) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise BridgeFailure("请求体必须是 JSON 对象")
        biz_line = biz_line_of(raw)
        program_id = program_id_of(raw.get("programId"))
        requested_keys = [str(key).strip() for key in raw.get("itemKeys") or [] if str(key).strip()]
        start_item_key = str(raw.get("startItemKey") or "").strip()
        model = str(raw.get("model") or "").strip()
        execution_constraints = str(raw.get("executionConstraints") or "").strip()
        if len(execution_constraints) > 32 * 1024:
            raise BridgeFailure("任务约束条件说明不能超过 32KB")
        provider = ai_provider_of(raw)
        reasoning_effort = reasoning_effort_of(raw, provider)
        fast_mode = fast_mode_of(raw, provider)
        if not program_id:
            raise BridgeFailure("缺少项目标识")
        config = request_scoped_config(config, biz_line, program_id)
        biz_line = config_biz_line(config)
        context = planner.project_context(config, program_id)
        items = [item for item in context.get("items") or [] if isinstance(item, dict)]
        by_key = {str(item.get("itemKey") or ""): item for item in items}
        if start_item_key:
            if start_item_key not in by_key:
                raise BridgeFailure("起始任务不存在")
            selected = {start_item_key}
            changed = True
            while changed:
                changed = False
                for item in items:
                    key = str(item.get("itemKey") or "")
                    dependencies = {str(value) for value in item.get("dependsOnItemKeys") or []}
                    if key not in selected and dependencies & selected:
                        selected.add(key)
                        changed = True
        else:
            selected = set(requested_keys)
        if not selected:
            raise BridgeFailure("请至少选择一个任务")
        missing = sorted(selected - set(by_key))
        if missing:
            raise BridgeFailure("任务不存在：" + ", ".join(missing))
        pending = {
            key for key in selected
            if str(by_key[key].get("status") or "") != "done"
        }
        if not pending:
            raise BridgeFailure("所选任务中没有可串行执行的未完成任务")
        if not start_item_key:
            completed = sorted(selected - pending)
            if completed:
                raise BridgeFailure("串行执行不能选择已完成任务：" + ", ".join(completed))
        ordered: list[str] = []
        remaining = set(pending)
        while remaining:
            ready = sorted(
                key for key in remaining
                if all(
                    str(dep) not in remaining
                    for dep in by_key[key].get("dependsOnItemKeys") or []
                )
            )
            if not ready:
                raise BridgeFailure("任务依赖关系存在环，无法串行执行")
            ordered.extend(ready)
            remaining.difference_update(ready)
        for key in ordered:
            incomplete_external = [
                str(dep) for dep in by_key[key].get("dependsOnItemKeys") or []
                if str(dep) not in pending and by_key.get(str(dep), {}).get("status") != "done"
            ]
            if incomplete_external:
                raise BridgeFailure(f"任务 {key} 的前置任务尚未完成：" + ", ".join(incomplete_external))
        with self.lock:
            reserved = {task_identity(biz_line, program_id, key) for key in ordered}
            sequence_conflicts = sorted(key for _, _, key in reserved if task_identity(biz_line, program_id, key) in self.sequence_tasks)
            batch_conflicts = sorted(key for _, _, key in reserved if task_identity(biz_line, program_id, key) in self.batch_tasks)
            active_conflicts = sorted(key for _, _, key in reserved if task_identity(biz_line, program_id, key) in self.active)
            if sequence_conflicts:
                raise BridgeFailure("任务已经在其他串行队列中：" + ", ".join(sequence_conflicts))
            if batch_conflicts:
                raise BridgeFailure("任务正在等待批量启动：" + ", ".join(batch_conflicts))
            if active_conflicts:
                raise BridgeFailure("任务已经在本地执行中：" + ", ".join(active_conflicts))
            self.sequence_tasks.update(reserved)
        try:
            persisted_batch = self._create_execution_batch(config, program_id, ordered, "sequence", provider)
            sequence_id = str(persisted_batch["batchId"])
        except Exception:
            with self.lock:
                self.sequence_tasks.difference_update(reserved)
            raise
        with self.lock:
            self.active_sequences.add(sequence_id)
            self.sequence_satisfied[sequence_id] = set()
        threading.Thread(
            target=self._run_sequence,
            args=(sequence_id, config, program_id, ordered, model, provider, execution_constraints, reasoning_effort, fast_mode),
            daemon=True,
        ).start()
        return {
            "accepted": True,
            "sequenceId": sequence_id,
            "batchId": sequence_id,
            "bizLine": biz_line,
            "programId": program_id,
            "itemKeys": ordered,
            "model": model,
            "provider": provider,
        }

    def _run_sequence(
        self,
        sequence_id: str,
        config: dict[str, Any],
        program_id: int,
        item_keys: list[str],
        model: str,
        provider: str,
        execution_constraints: str = "",
        reasoning_effort: str = "",
        fast_mode: bool = False,
    ) -> None:
        biz_line = config_biz_line(config)
        terminal_status = "completed"
        terminal_summary = "批次内全部任务已完成。"
        attempted_item = ""
        with self.lock:
            self.sequence_satisfied.setdefault(sequence_id, set())
        self._register_queue(sequence_id, program_id)
        try:
            for item_key in item_keys:
                self._abort_if_cancelled(sequence_id)
                attempted_item = item_key
                task = self._task_detail(config, program_id, item_key)
                status = str(task.get("status") or "")
                if status == "done":
                    self._update_execution_batch_item(
                        config, program_id, sequence_id, item_key, "completed", "执行开始前任务已完成。", provider,
                    )
                    attempted_item = ""
                    continue
                self.execute(
                    {
                        "bizLine": biz_line,
                        "programId": program_id,
                        "task": task,
                        "model": model,
                        "provider": provider,
                        "sequenceId": sequence_id,
                        "executionBatchId": sequence_id,
                        "batchMode": True,
                        **({"executionConstraints": execution_constraints} if execution_constraints else {}),
                        **({"reasoningEffort": reasoning_effort} if reasoning_effort else {}),
                        **({"fastMode": True} if fast_mode else {}),
                    },
                    config=config,
                )
                identity = task_identity(biz_line, program_id, item_key)
                while True:
                    with self.lock:
                        still_active = identity in self.active
                    if not still_active:
                        break
                    time.sleep(0.2)
                completed_task = self._task_detail(config, program_id, item_key)
                outcome, reason = batch_task_outcome(completed_task)
                if outcome == "ignorable":
                    self._update_execution_batch_item(config, program_id, sequence_id, item_key, "blocked", reason, provider)
                    terminal_status = "blocked"
                    terminal_summary = f"任务 {item_key} 未完全完成：{reason}"
                    with self.lock:
                        self.sequence_satisfied.setdefault(sequence_id, set()).add(item_key)
                    self.progress.publish(
                        identity,
                        "status",
                        "任务中断已忽略，继续串行队列",
                        reason,
                        "success",
                    )
                    attempted_item = ""
                    continue
                if outcome != "completed":
                    self._update_execution_batch_item(config, program_id, sequence_id, item_key, "blocked", reason, provider)
                    attempted_item = ""
                    self.progress.publish(identity, "error", "串行队列已暂停", reason, "failed")
                    raise BridgeFailure(
                        f"任务 {item_key} 未成功完成，队列已停止：{reason}"
                    )
                with self.lock:
                    self.sequence_satisfied.setdefault(sequence_id, set()).add(item_key)
                self._update_execution_batch_item(config, program_id, sequence_id, item_key, "completed", reason, provider)
                attempted_item = ""
        except Exception as exc:
            terminal_status = "blocked"
            terminal_summary = str(exc)
            if attempted_item:
                try:
                    self._update_execution_batch_item(
                        config, program_id, sequence_id, attempted_item, "blocked", terminal_summary, provider,
                    )
                except Exception as sync_error:
                    print(f"同步串行批次任务失败 {program_id}/{sequence_id}/{attempted_item}: {sync_error}", file=sys.stderr, flush=True)
            print(f"串行执行失败 {program_id}/{sequence_id}: {exc}", file=sys.stderr, flush=True)
        finally:
            try:
                self._finalize_execution_batch(
                    config, program_id, sequence_id, terminal_status, terminal_summary, provider,
                )
            except Exception as sync_error:
                print(f"同步串行执行批次结果失败 {program_id}/{sequence_id}: {sync_error}", file=sys.stderr, flush=True)
            with self.lock:
                self.active_sequences.discard(sequence_id)
                self.sequence_tasks.difference_update(task_identity(biz_line, program_id, key) for key in item_keys)
                self.sequence_satisfied.pop(sequence_id, None)
            self._release_queue(sequence_id)

    def execute_batch(self, raw: Any, config: dict[str, Any] | None = None) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise BridgeFailure("请求体必须是 JSON 对象")
        biz_line = biz_line_of(raw)
        program_id = program_id_of(raw.get("programId"))
        requested_keys = [str(key).strip() for key in raw.get("itemKeys") or [] if str(key).strip()]
        model = str(raw.get("model") or "").strip()
        execution_constraints = str(raw.get("executionConstraints") or "").strip()
        if len(execution_constraints) > 32 * 1024:
            raise BridgeFailure("任务约束条件说明不能超过 32KB")
        provider = ai_provider_of(raw)
        reasoning_effort = reasoning_effort_of(raw, provider)
        fast_mode = fast_mode_of(raw, provider)
        # 再做一次：允许把已完成任务重新拉进批次，不回滚它们的状态。
        redo = bool(raw.get("redo"))
        if not program_id:
            raise BridgeFailure("缺少项目标识")
        if not requested_keys:
            raise BridgeFailure("请至少选择一个未完成任务")
        if len(set(requested_keys)) != len(requested_keys):
            raise BridgeFailure("批量任务不能重复选择")

        config = request_scoped_config(config, biz_line, program_id)
        biz_line = config_biz_line(config)
        context = planner.project_context(config, program_id)
        items = [item for item in context.get("items") or [] if isinstance(item, dict)]
        by_key = {str(item.get("itemKey") or ""): item for item in items}
        missing = sorted(set(requested_keys) - set(by_key))
        if missing:
            raise BridgeFailure("任务不存在：" + ", ".join(missing))
        completed = sorted(key for key in requested_keys if str(by_key[key].get("status") or "") == "done")
        if completed and not redo:
            raise BridgeFailure("批量启动不能选择已完成任务：" + ", ".join(completed))
        selected = set(requested_keys)
        incomplete_external = {
            key: [
                str(dep) for dep in by_key[key].get("dependsOnItemKeys") or []
                if str(dep) not in selected and by_key.get(str(dep), {}).get("status") != "done"
            ]
            for key in requested_keys
        }
        blocked = [f"{key}（{', '.join(dependencies)}）" for key, dependencies in incomplete_external.items() if dependencies]
        if blocked:
            raise BridgeFailure("批量任务存在未完成的外部前置任务：" + "、".join(blocked))
        remaining = set(requested_keys)
        while remaining:
            ready = {
                key for key in remaining
                if all(str(dep) not in remaining for dep in by_key[key].get("dependsOnItemKeys") or [])
            }
            if not ready:
                raise BridgeFailure("任务依赖关系存在环，无法批量执行")
            remaining.difference_update(ready)

        with self.lock:
            reserved = {task_identity(biz_line, program_id, key) for key in requested_keys}
            active = sorted(key for _, _, key in reserved if task_identity(biz_line, program_id, key) in self.active)
            queued = sorted(key for _, _, key in reserved if task_identity(biz_line, program_id, key) in self.sequence_tasks)
            waiting = sorted(key for _, _, key in reserved if task_identity(biz_line, program_id, key) in self.batch_tasks)
            if active:
                raise BridgeFailure("任务已经在本地执行中：" + ", ".join(active))
            if queued:
                raise BridgeFailure("任务已经在串行队列中：" + ", ".join(queued))
            if waiting:
                raise BridgeFailure("任务正在等待批量启动：" + ", ".join(waiting))
            self.batch_tasks.update(reserved)
        try:
            persisted_batch = self._create_execution_batch(config, program_id, requested_keys, "parallel", provider, redo)
            batch_id = str(persisted_batch["batchId"])
        except Exception:
            with self.lock:
                self.batch_tasks.difference_update(reserved)
            raise
        with self.lock:
            self.batch_satisfied[batch_id] = set()
        threading.Thread(
            target=self._run_batch,
            args=(batch_id, config, program_id, requested_keys, model, provider, execution_constraints, reasoning_effort, fast_mode, redo),
            daemon=True,
        ).start()
        return {
            "accepted": True,
            "batchId": batch_id,
            "bizLine": biz_line,
            "programId": program_id,
            "itemKeys": requested_keys,
            "model": model,
            "provider": provider,
        }

    def _run_batch(
        self,
        batch_id: str,
        config: dict[str, Any],
        program_id: int,
        item_keys: list[str],
        model: str,
        provider: str = "codex",
        execution_constraints: str = "",
        reasoning_effort: str = "",
        fast_mode: bool = False,
        redo: bool = False,
    ) -> None:
        biz_line = config_biz_line(config)
        terminal_status = "completed"
        terminal_summary = "批次内全部任务已完成。"
        attempted_item = ""
        with self.lock:
            self.batch_satisfied.setdefault(batch_id, set())
        self._register_queue(batch_id, program_id)
        try:
            remaining = set(item_keys)
            while remaining:
                self._abort_if_cancelled(batch_id)
                context = planner.project_context(config, program_id)
                items = [item for item in context.get("items") or [] if isinstance(item, dict)]
                by_key = {str(item.get("itemKey") or ""): item for item in items}
                missing = sorted(remaining - set(by_key))
                if missing:
                    raise BridgeFailure("任务不存在：" + ", ".join(missing))

                # 平时已完成的任务直接记完成跳过；「再做一次」正是要重跑它们，所以不跳。
                completed_before_start = set() if redo else {
                    key for key in remaining if str(by_key[key].get("status") or "") == "done"
                }
                for item_key in completed_before_start:
                    self._update_execution_batch_item(
                        config, program_id, batch_id, item_key, "completed", "执行开始前任务已完成。", provider,
                    )
                    with self.lock:
                        self.batch_satisfied.setdefault(batch_id, set()).add(item_key)
                remaining.difference_update(completed_before_start)
                if not remaining:
                    return

                with self.lock:
                    satisfied = set(self.batch_satisfied.get(batch_id, set()))
                ready = sorted(
                    key for key in remaining
                    if all(
                        by_key.get(str(dep), {}).get("status") == "done"
                        or str(dep) in satisfied
                        for dep in by_key[key].get("dependsOnItemKeys") or []
                    )
                )
                if not ready:
                    waiting = []
                    for key in sorted(remaining):
                        dependencies = [
                            str(dep) for dep in by_key[key].get("dependsOnItemKeys") or []
                            if by_key.get(str(dep), {}).get("status") != "done"
                        ]
                        waiting.append(f"{key}（{', '.join(dependencies) or '状态未刷新'}）")
                    raise BridgeFailure("批量队列没有可执行任务，仍在等待前置任务：" + "、".join(waiting))

                for item_key in ready:
                    self._abort_if_cancelled(batch_id)
                    attempted_item = item_key
                    task = self._task_detail(config, program_id, item_key)
                    self.execute(
                        {
                            "bizLine": biz_line,
                            "programId": program_id,
                            "task": task,
                            "model": model,
                            "provider": provider,
                            "batchId": batch_id,
                            "executionBatchId": batch_id,
                            "batchMode": True,
                            **({"redo": True} if redo else {}),
                            **({"executionConstraints": execution_constraints} if execution_constraints else {}),
                            **({"reasoningEffort": reasoning_effort} if reasoning_effort else {}),
                            **({"fastMode": True} if fast_mode else {}),
                        },
                        batch_claim=True,
                        config=config,
                    )
                    attempted_item = ""

                launched_identities = {task_identity(biz_line, program_id, item_key) for item_key in ready}
                while True:
                    with self.lock:
                        still_active = launched_identities & self.active
                    if not still_active:
                        break
                    time.sleep(0.2)

                failed: list[str] = []
                for item_key in ready:
                    reviewed_task = self._task_detail(config, program_id, item_key)
                    outcome, reason = batch_task_outcome(reviewed_task)
                    identity = task_identity(biz_line, program_id, item_key)
                    if outcome == "completed":
                        self._update_execution_batch_item(config, program_id, batch_id, item_key, "completed", reason, provider)
                        with self.lock:
                            self.batch_satisfied.setdefault(batch_id, set()).add(item_key)
                        continue
                    if outcome == "ignorable":
                        self._update_execution_batch_item(config, program_id, batch_id, item_key, "blocked", reason, provider)
                        terminal_status = "blocked"
                        terminal_summary = f"任务 {item_key} 未完全完成：{reason}"
                        with self.lock:
                            self.batch_satisfied.setdefault(batch_id, set()).add(item_key)
                        self.progress.publish(
                            identity,
                            "status",
                            "任务中断已忽略，继续批量队列",
                            reason,
                            "success",
                        )
                        continue
                    self._update_execution_batch_item(config, program_id, batch_id, item_key, "blocked", reason, provider)
                    failed.append(f"{item_key}（{reason}）")
                    self.progress.publish(identity, "error", "批量队列已暂停", reason, "failed")
                if failed:
                    raise BridgeFailure("批量队列已停止，当前并行任务存在需要处理的问题：" + "、".join(failed))
                remaining.difference_update(ready)
        except Exception as exc:
            terminal_status = "blocked"
            terminal_summary = str(exc)
            if attempted_item:
                try:
                    self._update_execution_batch_item(
                        config, program_id, batch_id, attempted_item, "blocked", terminal_summary, provider,
                    )
                except Exception as sync_error:
                    print(f"同步批次任务失败 {program_id}/{batch_id}/{attempted_item}: {sync_error}", file=sys.stderr, flush=True)
            print(f"批量执行失败 {program_id}/{batch_id}: {exc}", file=sys.stderr, flush=True)
        finally:
            try:
                self._finalize_execution_batch(
                    config, program_id, batch_id, terminal_status, terminal_summary, provider,
                )
            except Exception as sync_error:
                print(f"同步批量执行结果失败 {program_id}/{batch_id}: {sync_error}", file=sys.stderr, flush=True)
            with self.lock:
                self.batch_tasks.difference_update(task_identity(biz_line, program_id, key) for key in item_keys)
                self.batch_satisfied.pop(batch_id, None)
            self._release_queue(batch_id)
