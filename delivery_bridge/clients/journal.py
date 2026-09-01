"""Codex 回合条目的本地流水。

Codex 的 `thread/read` 只持久化一部分条目：命令执行、文件改动、推理摘要
都读不回来。桌面版能显示完整过程，靠的是自己留着实时通知流——这里做同一件事，
把 item/started、item/completed 落盘，读会话时再合并回去。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import threading
from pathlib import Path
from typing import Any

from .. import runtime
from ..reasoning import reasoning_summary_text
from ..timeutil import utc_now
from ..turn_output import file_changes_of, text_from_user_item

# 落盘目录按模块名取，测试改写运行时目录时这里跟着变。
CODEX_THREAD_ITEMS_DIR = runtime.RUNTIME_DIR / "codex-thread-items"

MAX_THREAD_JOURNAL_TURNS = 60
MAX_THREAD_JOURNAL_ITEMS = 400
REASONING_SUMMARY_METHODS = {"item/reasoning/summaryPartAdded", "item/reasoning/summaryTextDelta"}
JOURNAL_METHODS = {"turn/started", "turn/completed", "item/started", "item/completed"} | REASONING_SUMMARY_METHODS

def journal_item(item: dict[str, Any]) -> dict[str, Any]:
    """把一条实时条目收敛成可落盘、可回放的形状。

    面板从来只展示推理的 `summary`，原始推理正文（`content` / `encryptedContent`）
    既不上屏也不归档；日志是同一份数据的另一种存放方式，同样不能把它留下来。
    命令输出体积大且面板不展示，一并丢掉。
    """
    kept = {key: value for key, value in item.items() if key not in {"aggregatedOutput", "output", "stdout", "stderr"}}
    if str(item.get("type") or "") == "reasoning":
        summary = item.get("summary")
        kept = {
            "id": item.get("id"),
            "type": "reasoning",
            "status": item.get("status"),
            "summary": summary if isinstance(summary, (str, list)) else [],
        }
    return kept


class ThreadItemJournal:
    """记录 app-server 的实时条目流，补上 `thread/read` 读不回来的执行过程。

    协议里对 `thread/rollback` 的说明写明了这点：Turn 里存的 ThreadItems 是有损的，
    命令执行这类交互不会被持久化，`thread/resume` 同理。实测一个「先跑 echo 再回答」
    的回合，实时流里有 reasoning 和 commandExecution，`thread/read` 只剩首尾两条消息。
    """

    def __init__(self, root: Path = CODEX_THREAD_ITEMS_DIR) -> None:
        self.root = root
        self.lock = threading.Lock()
        # item 事件不一定带 turnId，按线程记住当前回合，落到正确的那一轮上。
        self.current_turns: dict[str, str] = {}

    def _path(self, thread_id: str) -> Path:
        return self.root / f"{hashlib.sha256(thread_id.encode('utf-8')).hexdigest()[:32]}.json"

    def read(self, thread_id: str) -> list[dict[str, Any]]:
        if not thread_id:
            return []
        path = self._path(thread_id)
        with self.lock:
            if not path.is_file():
                return []
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return []
        turns = value.get("turns") if isinstance(value, dict) else None
        return [turn for turn in turns or [] if isinstance(turn, dict)]

    def _write(self, thread_id: str, turns: list[dict[str, Any]]) -> None:
        path = self._path(thread_id)
        payload = {"threadId": thread_id, "updatedAt": utc_now(), "turns": turns[-MAX_THREAD_JOURNAL_TURNS:]}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = path.with_suffix(".tmp")
            temp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.chmod(temp_path, 0o600)
            os.replace(temp_path, path)
        except OSError as exc:
            print(f"保存 Codex 会话过程记录失败：{thread_id}: {exc}", file=sys.stderr, flush=True)

    def record(self, message: dict[str, Any]) -> None:
        """吃一条 app-server 通知；不认识的方法直接忽略，绝不打断读流线程。"""
        method = str(message.get("method") or "")
        if method not in JOURNAL_METHODS:
            return
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        thread_id = str(params.get("threadId") or "")
        if not thread_id:
            return
        turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
        item = params.get("item") if isinstance(params.get("item"), dict) else {}
        turn_id = str(params.get("turnId") or turn.get("id") or item.get("turnId") or "")
        with self.lock:
            if method in {"turn/started", "turn/completed"} and turn_id:
                self.current_turns[thread_id] = turn_id
            turn_id = turn_id or self.current_turns.get(thread_id, "")
            if not turn_id:
                return
            turns = self.read_unlocked(thread_id)
            entry = next((value for value in turns if str(value.get("id") or "") == turn_id), None)
            if entry is None:
                entry = {"id": turn_id, "status": "inProgress", "createdAt": utc_now(), "completedAt": "", "items": []}
                turns.append(entry)
            if method == "turn/started":
                entry["status"] = str(turn.get("status") or entry.get("status") or "inProgress")
            elif method == "turn/completed":
                entry["status"] = str(turn.get("status") or "completed")
                entry["completedAt"] = utc_now()
                self.current_turns.pop(thread_id, None)
            elif method in REASONING_SUMMARY_METHODS:
                # 推理摘要不在 item 上，它是单独流出来的：item/completed 里的 summary 实测是空的。
                item_id = str(params.get("itemId") or params.get("targetItemId") or item.get("id") or "")
                if not item_id:
                    return
                target = self._reasoning_entry(entry, item_id)
                index = params.get("summaryIndex")
                if not (isinstance(index, int) and index >= 0):
                    # 没给下标时：新分片就是往后追一段，文本增量落在当前这一段上。
                    index = len(target["summary"]) if method == "item/reasoning/summaryPartAdded" else max(0, len(target["summary"]) - 1)
                while len(target["summary"]) <= index:
                    target["summary"].append("")
                target["summary"][index] += str(params.get("delta") or params.get("text") or "")
            else:
                item_id = str(item.get("id") or "")
                if not item_id:
                    return
                recorded = journal_item(item)
                items = entry.setdefault("items", [])
                existing = next((index for index, value in enumerate(items) if str(value.get("id") or "") == item_id), -1)
                if existing >= 0:
                    # 摘要是流式攒出来的，别被终态那条空 summary 覆盖掉。
                    if not recorded.get("summary") and items[existing].get("summary"):
                        recorded["summary"] = items[existing]["summary"]
                    items[existing] = recorded
                else:
                    items.append(recorded)
                del items[:-MAX_THREAD_JOURNAL_ITEMS]
            self._write(thread_id, turns)

    @staticmethod
    def _reasoning_entry(turn: dict[str, Any], item_id: str) -> dict[str, Any]:
        """摘要分片可能先于 item/started 到达，需要时就地建一条推理条目。"""
        items = turn.setdefault("items", [])
        target = next((value for value in items if str(value.get("id") or "") == item_id), None)
        if target is None:
            target = {"id": item_id, "type": "reasoning", "status": "inProgress", "summary": []}
            items.append(target)
        if not isinstance(target.get("summary"), list):
            target["summary"] = [target["summary"]] if isinstance(target.get("summary"), str) and target["summary"] else []
        return target

    def read_unlocked(self, thread_id: str) -> list[dict[str, Any]]:
        path = self._path(thread_id)
        if not path.is_file():
            return []
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        turns = value.get("turns") if isinstance(value, dict) else None
        return [turn for turn in turns or [] if isinstance(turn, dict)]


THREAD_ITEMS = ThreadItemJournal()


def journal_item_signature(item: dict[str, Any]) -> tuple[str, str]:
    """按内容认条目，不按 id 认。

    实测同一条消息在实时流和 `thread/read` 里 id 并不相同（服务端落库时会重新分配），
    只按 id 去重会把整轮消息重复一遍。
    """
    item_type = str(item.get("type") or "")
    if item_type == "userMessage":
        text = text_from_user_item(item)
    elif item_type == "reasoning":
        text = reasoning_summary_text(item)
    elif item_type == "commandExecution":
        command = item.get("command") or item.get("commands") or ""
        text = "\n".join(str(part) for part in command) if isinstance(command, list) else str(command)
    elif item_type in {"fileChange", "fileEdit"}:
        text = "\n".join(change["path"] for change in file_changes_of(item))
    else:
        text = str(item.get("text") or item.get("content") or item.get("tool") or item.get("name") or "")
    return item_type, " ".join(text.split())


def reasoning_summary_parts(item: dict[str, Any]) -> list[str]:
    """推理条目里的一段段摘要。字符串形式的按空行拆开，和流式分片对齐。"""
    summary = item.get("summary") if isinstance(item, dict) else None
    parts = summary if isinstance(summary, list) else re.split(r"\n{2,}", summary) if isinstance(summary, str) else []
    return [part.strip() for part in parts if isinstance(part, str) and part.strip()]


def normalized_reasoning_part(part: str) -> str:
    return " ".join(part.split())


def deduped_reasoning_item(item: dict[str, Any], known: set[str]) -> dict[str, Any] | None:
    """`thread/read` 事后会把整轮摘要合成一条还回来，实时流里已经有的段落要去掉。

    两边 id 对不上（服务端落库时重新分配），只能按内容认；全都见过就整条丢掉，
    否则回合末尾会把前面的「分析」原样重放一遍。
    """
    remaining = [part for part in reasoning_summary_parts(item) if normalized_reasoning_part(part) not in known]
    if not remaining:
        return None
    known.update(normalized_reasoning_part(part) for part in remaining)
    return {**item, "summary": remaining}


def merge_journal_turns(thread: dict[str, Any], journal_turns: list[dict[str, Any]]) -> dict[str, Any]:
    """用实时记录补全 `thread/read` 的回合正文。

    以服务端返回的回合顺序为准（它才知道整个线程的历史），逐轮把记录里的条目铺开；
    服务端有、记录里没有的条目（比如桥接中途才接上的那一轮）按原样补在后面。
    """
    if not isinstance(thread, dict) or not journal_turns:
        return thread
    journal = {str(turn.get("id") or ""): turn for turn in journal_turns if str(turn.get("id") or "")}
    if not journal:
        return thread
    turns = thread.get("turns") if isinstance(thread.get("turns"), list) else []
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        turn_id = str(turn.get("id") or "")
        seen.add(turn_id)
        recorded = journal.get(turn_id)
        if recorded is None:
            merged.append(turn)
            continue
        items = [item for item in recorded.get("items") or [] if isinstance(item, dict)]
        known = {journal_item_signature(item) for item in items}
        # 摘要按段去重，不按整条：实时流一段一条，服务端事后给的是合在一起的全文。
        known_parts = {
            normalized_reasoning_part(part)
            for item in items if str(item.get("type") or "") == "reasoning"
            for part in reasoning_summary_parts(item)
        }
        for item in turn.get("items") or []:
            if not isinstance(item, dict):
                continue
            if str(item.get("type") or "") == "reasoning":
                deduped = deduped_reasoning_item(item, known_parts)
                if deduped is not None:
                    items.append(deduped)
                continue
            if journal_item_signature(item) not in known:
                items.append(item)
        merged.append({**turn, "items": items})
    # 线程刚跑完就读，服务端有时还没把这一轮写进历史；记录里已经有了就直接补上。
    merged.extend(turn for turn in journal_turns if str(turn.get("id") or "") not in seen)
    return {**thread, "turns": merged}
