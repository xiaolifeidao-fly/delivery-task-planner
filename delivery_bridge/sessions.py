"""一条任务或需求下的会话目录：版本号、标题、条目合并。

同一个任务可以聊很多轮不同的会话，面板左侧列的就是这份目录。
条目来自服务端的会话绑定记录，正文来自执行器；这里只负责把两边对齐，
并按版本倒序给出前端要展示的那一份。
"""

from __future__ import annotations

from typing import Any

from .providers import executor_provider_of
from .timeutil import utc_now

MAX_CONVERSATIONS_PER_TASK = 12

def next_conversation_version(binding: dict[str, Any] | None) -> int:
    """Return the suffix number for the next thread, including compacted history."""
    metadata = (binding or {}).get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    stored_version = metadata.get("nextConversationVersion")
    try:
        if int(stored_version) >= 0:
            return int(stored_version)
    except (TypeError, ValueError):
        pass
    # Existing bindings do not have a counter yet. Their retained thread catalog
    # gives the correct next version until the first metadata update persists it.
    return len(conversation_catalog(binding))

def conversation_title(task: dict[str, Any], binding: dict[str, Any] | None = None) -> str:
    """Name the first Codex thread after its task, then use ascending versions."""
    base = " ".join(str(task.get("title") or "Codex 会话").split()) or "Codex 会话"
    version = next_conversation_version(binding)
    if version == 0:
        return base[:80]
    suffix = f" V0.0.{version}"
    return f"{base[: 80 - len(suffix)].rstrip()}{suffix}"

def conversation_catalog(binding: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Read the compact per-task Codex thread directory, including legacy bindings."""
    if not isinstance(binding, dict):
        return []
    metadata = binding.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    raw = metadata.get("conversations")
    catalog: list[dict[str, Any]] = []
    seen: set[str] = set()
    if isinstance(raw, list):
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            thread_id = str(entry.get("threadId") or "").strip()
            if not thread_id or thread_id in seen:
                continue
            seen.add(thread_id)
            catalog.append(
                {
                    "threadId": thread_id,
                    "title": str(entry.get("title") or "Codex 会话")[:80],
                    "createdAt": str(entry.get("createdAt") or ""),
                    "updatedAt": str(entry.get("updatedAt") or ""),
                    "status": str(entry.get("status") or "completed"),
                    "phase": str(entry.get("phase") or binding.get("phase") or "requirement"),
                    "progress": int(entry.get("progress") or binding.get("progress") or 0),
                }
            )
    legacy_thread_id = str(binding.get("externalSessionId") or "").strip()
    if legacy_thread_id and legacy_thread_id not in seen:
        timestamp = str(binding.get("updatedAt") or "")
        catalog.append(
            {
                "threadId": legacy_thread_id,
                "title": "Codex 会话",
                "createdAt": timestamp,
                "updatedAt": timestamp,
                "status": str(binding.get("status") or "completed"),
                "phase": str(binding.get("phase") or "requirement"),
                "progress": int(binding.get("progress") or 0),
            }
        )
    return catalog[:MAX_CONVERSATIONS_PER_TASK]

def turn_already_finished(error: Exception) -> bool:
    """Codex 在回合结束后会拒绝 interrupt。这不是停止失败，只是这一下点晚了。"""
    return "no active turn" in str(error).lower()

def conversation_metadata(
    binding: dict[str, Any] | None,
    thread_id: str,
    turn_id: str = "",
    turn_status: str = "",
    title: str = "",
    phase: str = "",
) -> dict[str, Any]:
    """Merge a thread update without losing the rest of a task's conversation history."""
    raw_metadata = (binding or {}).get("metadata")
    metadata = dict(raw_metadata) if isinstance(raw_metadata, dict) else {}
    now = utc_now()
    catalog = conversation_catalog(binding)
    previous_version = next_conversation_version(binding)
    entry = next((candidate for candidate in catalog if candidate["threadId"] == thread_id), None)
    conversation_phase = phase or str((binding or {}).get("phase") or "requirement")
    if entry is None:
        entry = {
            "threadId": thread_id,
            "title": title or "Codex 会话",
            "createdAt": now,
            "updatedAt": now,
            "status": turn_status or "running",
            "phase": conversation_phase,
            "progress": int((binding or {}).get("progress") or 0),
        }
        catalog.append(entry)
        next_version = previous_version + 1
    else:
        entry["title"] = title or entry["title"]
        entry["updatedAt"] = now
        if turn_status:
            entry["status"] = turn_status
        next_version = previous_version
    entry["phase"] = phase or str((binding or {}).get("phase") or entry.get("phase") or "requirement")
    entry["progress"] = 100 if turn_status == "completed" else int((binding or {}).get("progress") or entry.get("progress") or 0)
    if not entry.get("createdAt"):
        entry["createdAt"] = now
    entry["title"] = str(entry.get("title") or "Codex 会话")[:80]
    entry["updatedAt"] = now
    catalog.sort(key=lambda candidate: str(candidate.get("updatedAt") or ""), reverse=True)
    metadata["conversations"] = catalog[:MAX_CONVERSATIONS_PER_TASK]
    metadata["nextConversationVersion"] = next_version
    metadata["threadId"] = thread_id
    if turn_id:
        metadata["turnId"] = turn_id
    if turn_status:
        metadata["turnStatus"] = turn_status
    metadata["workspace"] = metadata.get("workspace") or ""
    return metadata

def merged_conversation_catalog(bindings: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Merge catalogs from all execution phases while retaining each thread's owner.

    A task can move from requirement grooming to action and then testing. Its
    execution-session rows are phase-scoped, while the task chat sidebar must
    present every retained conversation for that task.
    """
    entries: dict[str, dict[str, Any]] = {}
    owners: dict[str, dict[str, Any]] = {}
    for binding in bindings:
        for entry in conversation_catalog(binding):
            thread_id = str(entry.get("threadId") or "")
            if not thread_id:
                continue
            previous = entries.get(thread_id)
            # 同一条 thread 可能出现在多行会话记录的目录里；真正持有它的是 externalSessionId
            # 指向它的那一行，归属执行器只能按这一行判定，否则跨工具会读错缓存。
            owns_thread = str(binding.get("externalSessionId") or "") == thread_id
            previous_owns_thread = str((owners.get(thread_id) or {}).get("externalSessionId") or "") == thread_id
            if previous is not None and previous_owns_thread and not owns_thread:
                continue
            if (
                previous is None
                or (owns_thread and not previous_owns_thread)
                or str(entry.get("updatedAt") or "") >= str(previous.get("updatedAt") or "")
            ):
                entries[thread_id] = {**entry, "executorType": executor_provider_of(binding)}
                owners[thread_id] = binding
    catalog = sorted(entries.values(), key=lambda entry: str(entry.get("updatedAt") or ""), reverse=True)
    return catalog, owners
