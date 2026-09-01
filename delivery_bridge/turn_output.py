"""从执行器回合里解析出面板要展示的产物。

一个回合回来的是 items 流：正文、推理摘要、命令执行、文件改动。面板要的是
「这一轮到底干了什么」——有没有真动过工作区、算不算跑完、测试结论是什么、
改了哪些文件几行。这些判定规则集中在这里，和会话怎么跑起来完全无关。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

# 执行器回合状态到面板状态的映射，也界定了哪些状态算「这一轮结束了」。
SESSION_STATUS = {"completed": "completed", "failed": "blocked", "interrupted": "blocked"}
TERMINAL_TURN_STATUSES = set(SESSION_STATUS)


EXECUTION_OUTPUT_LIMIT = 8 * 1024 * 1024

# 这一轮真正动了工作区的条目类型。只有 agentMessage / reasoning 的回合等于什么都没做。
TOOL_CALL_ITEM_TYPES = {"commandExecution", "fileChange", "fileEdit", "mcpToolCall", "dynamicToolCall"}
# 模型没能把工具调用发成调用帧时，会把工具 schema 连同超时毫秒数一起写进正文，
# 而且开头那个 T 经常被吃掉（实测出现过 `ARGET_TOOL_SCHEMA={...}"120000`），
# 所以这里故意只匹配后半截，两种写法都能命中。
LEAKED_TOOL_CALL_MARKER = "ARGET_TOOL_SCHEMA"
# 这两个阶段的产物就是代码和验证结果，不可能只靠一段文字交付。
# 梳理需求阶段允许只写文档，不能套用这条判定。
WORKING_PHASES = {"development", "testing"}


def turn_agent_text(turn: dict[str, Any]) -> str:
    parts = [
        str(item.get("text") or item.get("content") or "").strip()
        for item in turn.get("items") or []
        if str(item.get("type") or "") == "agentMessage"
    ]
    return "\n".join(part for part in parts if part)


def turn_tool_call_count(turn: dict[str, Any]) -> int:
    return sum(
        1 for item in turn.get("items") or []
        if str(item.get("type") or "") in TOOL_CALL_ITEM_TYPES
    )


def corrupted_turn_reason(turn_status: str, turn: dict[str, Any], phase: str) -> str:
    """回合自称成功、实际什么都没干时给出原因，否则返回空串。

    这是 gpt-5.6-terra 走自建中转时实测到的失败形态：模型没有发出任何工具调用，
    把本该是调用的内容写进了最终回复，还顺带自证「本会话没有暴露 shell/exec 工具」。
    这种回复以前会被当成动作执行产物存进任务，任务直接被判成 done。
    """
    if turn_status != "completed":
        # 非正常结束本来就会被判成 blocked，交给既有路径处理。
        return ""
    if LEAKED_TOOL_CALL_MARKER in turn_agent_text(turn):
        return "最终回复里混进了工具调用的 schema 残片，说明这一轮的工具调用没有真正发出去"
    if phase in WORKING_PHASES and turn_tool_call_count(turn) == 0:
        return "整轮没有任何命令执行或文件改动，动作执行/测试阶段不可能只靠一段文字完成"
    return ""


def execution_output(turn_status: str, turn: dict[str, Any]) -> str:
    """Persist a readable Markdown summary instead of exposing protocol JSON."""
    lines = ["# Codex 执行结果", "", f"- 状态：{turn_status}", f"- 完成时间：{datetime.now(timezone.utc).isoformat()}", ""]
    for item in turn.get("items") or []:
        item_type = str(item.get("type") or "")
        if item_type == "agentMessage":
            text = str(item.get("text") or item.get("content") or "").strip()
            if text:
                lines.extend(["## 进度说明", "", text, ""])
        elif item_type == "commandExecution":
            command = item.get("command") or item.get("commands") or ""
            if isinstance(command, list):
                command = "\n".join(str(part) for part in command)
            lines.extend(["## 执行命令", "", "```sh", str(command), "```", ""])
            exit_code = item.get("exitCode")
            if exit_code not in (None, 0):
                lines.extend([f"命令结果：失败（退出码 {exit_code}）", ""])
    raw = "\n".join(lines).strip() + "\n"
    encoded = raw.encode("utf-8")
    if len(encoded) <= EXECUTION_OUTPUT_LIMIT:
        return raw
    truncated = encoded[: EXECUTION_OUTPUT_LIMIT - 128].decode("utf-8", errors="ignore")
    return truncated + "\n\n[执行记录过长，已在 8MB 处截断]"


def merged_execution_output(previous: str, incoming: str) -> str:
    """把本轮产物接在任务已有产物后面，而不是整段覆盖掉。

    面板的「设计文档」和「成品测试报告」页签读的就是 actionOutput / testingReport。
    一次追加对话只会产出增量，直接覆盖等于把前几轮的产物删掉，用户看到的文档
    就只剩最后一次追加的内容。
    """
    previous_text = (previous or "").strip()
    incoming_text = (incoming or "").strip()
    if not previous_text:
        return f"{incoming_text}\n" if incoming_text else ""
    if not incoming_text or incoming_text in previous_text:
        return f"{previous_text}\n"
    merged = f"{previous_text}\n\n---\n\n{incoming_text}\n"
    encoded = merged.encode("utf-8")
    if len(encoded) <= EXECUTION_OUTPUT_LIMIT:
        return merged
    # 超限时丢最早的回合：最近的产物才是用户正在看的那一份。
    note = "[更早的执行记录已按 8MB 上限截断]\n\n"
    kept = encoded[-(EXECUTION_OUTPUT_LIMIT - len(note.encode("utf-8")) - 128):].decode("utf-8", errors="ignore")
    return note + kept


def final_agent_text_from_output(output: str) -> str:
    marker = "## 进度说明\n\n"
    if marker not in output:
        return output.strip()
    sections = [section.strip() for section in output.split(marker)[1:]]
    cleaned = [section.split("\n\n## 执行命令", 1)[0].strip() for section in sections if section.strip()]
    return cleaned[-1] if cleaned else output.strip()


def testing_verdict_from_output(output: str) -> str:
    """Read the exact verdict required by the testing skill from the final reply."""
    final_text = final_agent_text_from_output(output)
    match = re.search(r"(?m)^\s*验收判定\s*[:：]\s*(通过|不通过|受阻)\s*$", final_text)
    return match.group(1) if match else ""


BATCH_OUTCOME_RE = re.compile(r"(?m)^\s*批量判定\s*[:：]\s*(完成|可忽略|需人工处理)\s*$")
BATCH_TURN_STATUS_RE = re.compile(r"(?m)^\s*-?\s*状态\s*[:：]\s*([A-Za-z]+)\s*$")
# These markers are intentionally limited to evidence of a deliverable-level
# problem. A generic "warning" or a command mention must not stop a queue.
BATCH_HARD_PROBLEM_RE = re.compile(
    r"(?:无法(?:完成|实现|继续|验证)|(?:编译|构建|测试|命令).{0,20}(?:失败|错误|不通过)|"
    r"命令结果.{0,12}失败|退出码\s*[1-9]\d*|"
    r"(?:需要|需)(?:人工|处理)|(?:权限|依赖|数据).{0,12}(?:不足|缺少|错误)|阻塞|受阻|冲突)",
    re.IGNORECASE,
)


def batch_task_outcome(task: dict[str, Any]) -> tuple[str, str]:
    """Classify a finished queue item without changing its authoritative task status.

    ``completed`` means the task board already accepted the task. ``ignorable``
    is a queue-local skip for a transient interruption; the task remains
    blocked so the user can inspect and retry it later. Everything else is a
    real queue blocker.
    """
    status = str(task.get("status") or "").strip().lower()
    if status == "done":
        return "completed", "任务已完成"

    output = str(task.get("actionOutput") or task.get("testingReport") or "").strip()
    final_text = final_agent_text_from_output(output)
    explicit = BATCH_OUTCOME_RE.search(final_text)
    if explicit:
        verdict = explicit.group(1)
        if verdict == "完成" and status == "done":
            return "completed", "任务已完成"
        if verdict == "可忽略":
            return "ignorable", "执行回合已中断，但未发现代码、编译、测试或权限阻塞证据。"
        return "hard", "执行器报告存在需要人工处理的实质问题。"

    turn_status_match = BATCH_TURN_STATUS_RE.search(output)
    turn_status = turn_status_match.group(1).lower() if turn_status_match else ""
    if BATCH_HARD_PROBLEM_RE.search(final_text):
        return "hard", "执行结果包含代码、编译、测试、权限、依赖或其他实质阻塞信息。"

    # An interrupted turn with no substantive failure evidence is safe to
    # bypass in the current queue. It is still blocked on the board and will
    # remain visible for a later manual retry.
    if turn_status in {"interrupted", "failed"}:
        return "ignorable", "执行回合意外终止，未发现实质阻塞信息。"

    return "hard", f"任务状态为 {status or 'unknown'}，且没有可忽略判定。"


def text_from_user_item(item: dict[str, Any]) -> str:
    content = item.get("content") or item.get("input") or []
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for part in content:
        if isinstance(part, str):
            parts.append(part)
            continue
        if isinstance(part, dict) and str(part.get("type") or "") == "text":
            parts.append(str(part.get("text") or ""))
    return "\n".join(part.strip() for part in parts if part.strip())


FILE_CHANGE_KINDS = {"add", "added", "create", "created", "delete", "deleted", "remove", "removed", "modify", "modified", "update", "updated", "rename", "renamed"}
FILE_CHANGE_ALIASES = {
    "added": "add",
    "create": "add",
    "created": "add",
    "deleted": "delete",
    "remove": "delete",
    "removed": "delete",
    "modified": "modify",
    "update": "modify",
    "updated": "modify",
    "renamed": "rename",
}


def diff_line_counts(diff: Any) -> tuple[int, int]:
    """数一份 unified diff 的增删行数，面板据此显示 `+74 -4`。"""
    if not isinstance(diff, str) or not diff:
        return 0, 0
    added = removed = 0
    for line in diff.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
    return added, removed


def file_changes_of(item: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize one file-change item into `[{path, kind, added, removed}]`.

    Codex 和 Claude 给的字段名不完全一样，面板只认 path + add/modify/delete/rename。
    Codex 的 `kind` 实测是对象（`{"type": "update", "move_path": null}`），
    当字符串读会一律退化成 modify，新增和删除就分不出来了。
    """
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for change in item.get("changes") or []:
        if not isinstance(change, dict):
            continue
        path = str(change.get("path") or change.get("file") or change.get("filePath") or "").strip()
        if not path or path in seen:
            continue
        seen.add(path)
        kind_value = change.get("kind") or change.get("type") or change.get("changeType") or ""
        if isinstance(kind_value, dict):
            kind_value = kind_value.get("type") or kind_value.get("kind") or ""
        raw_kind = str(kind_value).strip().lower()
        kind = FILE_CHANGE_ALIASES.get(raw_kind, raw_kind if raw_kind in FILE_CHANGE_KINDS else "modify")
        added, removed = diff_line_counts(change.get("diff") or change.get("unifiedDiff") or change.get("unified_diff"))
        normalized.append({"path": path, "kind": kind, "added": added, "removed": removed})
    return normalized

