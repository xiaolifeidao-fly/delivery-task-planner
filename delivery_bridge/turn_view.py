"""把执行器回合整理成面板要的那份 JSON。

和 turn_output 的分工：那边判定「这一轮到底干了什么」，这边负责把条目
铺成前端能直接渲染的结构，并保证最后一条一定有个终态结果——回合断在
半路时也不能让面板一直转圈。
"""

from __future__ import annotations

from typing import Any

from .artifacts import MARKDOWN_ARTIFACT_RE
from .attachments_text import attachment_ids_from_text, text_without_attachment_context
from .errors import BridgeFailure
from .reasoning import reasoning_summary_text
from .token_usage import has_usage
from .turn_output import file_changes_of, text_from_user_item

def serialize_turns(
    turns: Any,
    attachment_resolver: Any = None,
    artifact_resolver: Any = None,
    turn_attachment_resolver: Any = None,
) -> list[dict[str, Any]]:
    """Return a small, browser-safe conversation projection of Codex thread history."""
    if not isinstance(turns, list):
        return []
    serialized: list[dict[str, Any]] = []
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        turn_id = str(turn.get("id") or "")
        turn_attachments = turn_attachment_resolver(turn_id) if turn_attachment_resolver else []
        messages: list[dict[str, Any]] = []
        for item in turn.get("items") or []:
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("type") or "")
            text = ""
            action = ""
            attachments: list[dict[str, Any]] = []
            changes: list[dict[str, Any]] = []
            if item_type == "userMessage":
                text = text_from_user_item(item)
                attachment_ids = attachment_ids_from_text(text)
                if attachment_ids and attachment_resolver:
                    try:
                        attachments = attachment_resolver(attachment_ids)
                    except BridgeFailure:
                        attachments = []
                text = text_without_attachment_context(text)
            elif item_type in {"agentMessage", "plan"}:
                text = str(item.get("text") or item.get("content") or item.get("summary") or "").strip()
                if artifact_resolver and item_type == "agentMessage" and str(item.get("phase") or "") == "final_answer":
                    linked_paths = [match.strip().split("#", 1)[0] for match in MARKDOWN_ARTIFACT_RE.findall(text)]
                    attachments = artifact_resolver(linked_paths[:20])
            elif item_type == "reasoning":
                # Do not fall back to `content`: it can contain a non-display
                # reasoning payload. The protocol's `summary` is intentional
                # user-facing content and is safe for the browser projection.
                text = reasoning_summary_text(item)
            elif item_type == "commandExecution":
                command = item.get("command") or item.get("commands") or ""
                text = "\n".join(str(part) for part in command) if isinstance(command, list) else str(command)
            elif item_type in {"mcpToolCall", "dynamicToolCall"}:
                # Claude 的读文件、检索是具名工具，命令行里没有可解析的字面量：
                # 语义在 action/target 上，面板据此显示成「已读取 X」而不是「已调用 Read」。
                action = str(item.get("action") or "")
                text = str(item.get("pattern") or item.get("tool") or item.get("name") or item.get("server") or "")
            elif item_type in {"fileChange", "fileEdit"}:
                changes = file_changes_of(item)
                paths = [change["path"] for change in changes]
                text = "\n".join(paths)
                if artifact_resolver and paths:
                    attachments = artifact_resolver(paths)
            if not text and item_type not in {"fileChange", "fileEdit"}:
                continue
            messages.append(
                {
                    "id": str(item.get("id") or ""),
                    "type": item_type,
                    "text": text,
                    "action": action,
                    "target": str(item.get("target") or ""),
                    "status": str(item.get("status") or ""),
                    "exitCode": item.get("exitCode"),
                    "phase": str(item.get("phase") or ""),
                    "attachments": attachments,
                    # 结构化的改动清单：面板据此在回合末尾汇总「本次改动」，和直接用 CLI 时看到的一致。
                    "changes": changes,
                }
            )
        if turn_attachments:
            target = next(
                (
                    item for item in reversed(messages)
                    if item.get("type") == "agentMessage" and item.get("phase") == "final_answer"
                ),
                next((item for item in reversed(messages) if item.get("type") == "agentMessage"), None),
            )
            if target is not None:
                known_ids = {str(item.get("id") or "") for item in target["attachments"]}
                target["attachments"].extend(
                    item for item in turn_attachments if str(item.get("id") or "") not in known_ids
                )
        usage = turn.get("usage") if isinstance(turn.get("usage"), dict) else None
        serialized.append(
            {
                "id": turn_id,
                "status": str(turn.get("status") or ""),
                "createdAt": turn.get("createdAt") or turn.get("startedAt") or "",
                "completedAt": turn.get("completedAt") or "",
                "items": messages,
                # 用量问不出来的回合（老会话、执行器没报）不给字段，面板据此不显示这一行。
                **({"usage": usage} if has_usage(usage) else {}),
            }
        )
    return serialized

def ensure_terminal_result(
    turns: list[dict[str, Any]],
    task: dict[str, Any],
    binding: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Use the task board's persisted result while another Codex process has a stale thread snapshot."""
    if str(task.get("status") or "") != "done":
        return turns
    for turn in turns:
        for item in turn.get("items") or []:
            if item.get("type") == "agentMessage" and item.get("phase") == "final_answer" and str(item.get("text") or "").strip():
                return turns
    phase = str(task.get("phase") or "requirement")
    result_field = {"requirement": "requirementDocument", "development": "actionOutput", "testing": "testingReport"}.get(phase, "")
    result = str(task.get(result_field) or "").strip() if result_field else ""
    if not result:
        return turns
    metadata = (binding or {}).get("metadata") or {}
    turn_id = str(metadata.get("turnId") or "task-board-result") if isinstance(metadata, dict) else "task-board-result"
    if not turns:
        turns.append({"id": turn_id, "status": "completed", "createdAt": 0, "completedAt": 0, "items": []})
    turns[-1]["status"] = "completed"
    turns[-1].setdefault("items", []).append(
        {
            "id": f"{turn_id}-persisted-result",
            "type": "agentMessage",
            "text": result,
            "status": "completed",
            "exitCode": None,
            "phase": "final_answer",
            "attachments": [],
        }
    )
    return turns
