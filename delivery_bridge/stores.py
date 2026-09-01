"""桥接进程自己要记住的几张小表，都落在运行时目录下的 JSON 里。

进度、待补的会话同步、Git 环境会话——它们都不属于任务面板的数据，
是本机这一个桥接进程的状态，所以不走服务端，重启后从磁盘读回来。
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import runtime

PENDING_SESSION_SYNCS_PATH = runtime.RUNTIME_DIR / "pending-session-syncs.json"
PENDING_BATCH_FINALIZES_PATH = runtime.RUNTIME_DIR / "pending-batch-finalizes.json"
GIT_ENVIRONMENT_SESSIONS_PATH = runtime.RUNTIME_DIR / "git-environment-sessions.json"
MAX_GIT_ENVIRONMENT_CONVERSATIONS = 12

class ProgressStore:
    def __init__(self) -> None:
        self.events: dict[tuple[str, int, str], list[dict[str, Any]]] = {}
        self.sequences: dict[tuple[str, int, str], int] = {}
        self.conditions: dict[tuple[str, int, str], threading.Condition] = {}
        self.lock = threading.Lock()

    def publish(self, identity: tuple[str, int, str], kind: str, title: str, body: str = "", status: str = "running") -> None:
        with self.lock:
            sequence = self.sequences.get(identity, 0) + 1
            self.sequences[identity] = sequence
            event = {
                "id": str(sequence),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "kind": kind,
                "title": title,
                "body": body.strip(),
                "status": status,
            }
            events = self.events.setdefault(identity, [])
            events.append(event)
            del events[:-500]
            condition = self.conditions.setdefault(identity, threading.Condition(self.lock))
            condition.notify_all()

    def snapshot(self, identity: tuple[str, int, str]) -> list[dict[str, Any]]:
        with self.lock:
            return list(self.events.get(identity, []))

    def latest_sequence(self, identity: tuple[str, int, str]) -> int:
        with self.lock:
            return self.sequences.get(identity, 0)

    def wait(self, identity: tuple[str, int, str], cursor: int, timeout: float = 15) -> tuple[list[dict[str, Any]], int]:
        with self.lock:
            condition = self.conditions.setdefault(identity, threading.Condition(self.lock))
        with condition:
            condition.wait_for(lambda: self.sequences.get(identity, 0) > cursor, timeout=timeout)
            events = [event for event in self.events.get(identity, []) if int(event["id"]) > cursor]
            return list(events), self.sequences.get(identity, cursor)

class PendingSessionSyncStore:
    def __init__(self, path: Path = PENDING_SESSION_SYNCS_PATH) -> None:
        self.path = path
        self.lock = threading.Lock()

    @staticmethod
    def key_of(entry: dict[str, Any]) -> str:
        return (
            f"{entry['programId']}/{entry['itemKey']}/{entry['executorType']}/"
            f"{entry.get('phase') or 'requirement'}"
        )

    @staticmethod
    def legacy_key_of(entry: dict[str, Any]) -> str:
        return f"{entry['programId']}/{entry['itemKey']}/{entry['executorType']}/{entry.get('phase') or 'requirement'}"

    def _read(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _write(self, entries: dict[str, dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_suffix(".tmp")
        temp_path.write_text(json.dumps(entries, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, self.path)

    def add(self, entry: dict[str, Any]) -> None:
        with self.lock:
            entries = self._read()
            entries[self.key_of(entry)] = entry
            self._write(entries)

    def remove(self, entry: dict[str, Any]) -> None:
        with self.lock:
            entries = self._read()
            entries.pop(self.key_of(entry), None)
            entries.pop(self.legacy_key_of(entry), None)
            self._write(entries)

    def snapshot(self) -> list[dict[str, Any]]:
        with self.lock:
            return list(self._read().values())

class PendingBatchFinalizeStore:
    """批次收尾请求发不出去时先落在这里，等网络恢复再补。

    收尾是唯一能把执行批次从 running 改成终态的动作，丢一次就意味着批次里的任务
    被永久锁住（任务面板会一直报「任务正在其他执行批次中」）。落盘的只有批次号和结论，
    身份凭证一律不进磁盘，重放时用当次请求带来的身份。
    """

    def __init__(self, path: Path = PENDING_BATCH_FINALIZES_PATH) -> None:
        self.path = path
        self.lock = threading.Lock()

    @staticmethod
    def key_of(entry: dict[str, Any]) -> str:
        return f"{entry['programId']}/{entry['batchId']}"

    def _read(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _write(self, entries: dict[str, dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_suffix(".tmp")
        temp_path.write_text(json.dumps(entries, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, self.path)

    def add(self, entry: dict[str, Any]) -> None:
        with self.lock:
            entries = self._read()
            entries[self.key_of(entry)] = entry
            self._write(entries)

    def remove(self, entry: dict[str, Any]) -> None:
        with self.lock:
            entries = self._read()
            entries.pop(self.key_of(entry), None)
            self._write(entries)

    def snapshot(self) -> list[dict[str, Any]]:
        with self.lock:
            return list(self._read().values())

class GitEnvironmentSessionStore:
    """不挂服务端会话表的本机聊天，其会话目录的落盘实现。

    这类聊天不属于任何项目，服务端没有对应的会话表可绑，所以目录直接落在运行时目录里，
    一个执行器（codex / claude）一份，刷新页面后还能把之前聊过的会话找回来。
    """

    def __init__(self, path: Path = GIT_ENVIRONMENT_SESSIONS_PATH) -> None:
        self.path = path
        self.lock = threading.Lock()

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _write(self, value: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_suffix(".tmp")
        temp_path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, self.path)

    def catalog(self, provider: str) -> list[dict[str, Any]]:
        with self.lock:
            entries = self._read().get(provider) or []
        return [entry for entry in entries if isinstance(entry, dict) and str(entry.get("threadId") or "")]

    def load(self, provider: str, thread_id: str = "") -> dict[str, Any] | None:
        catalog = self.catalog(provider)
        if not catalog:
            return None
        current = next((entry for entry in catalog if entry.get("threadId") == thread_id), catalog[-1])
        return {
            "threadId": str(current.get("threadId") or ""),
            "turnId": str(current.get("turnId") or ""),
            "catalog": catalog,
        }

    def save(self, provider: str, session: dict[str, Any]) -> None:
        thread_id = str(session.get("threadId") or "")
        if not thread_id:
            return
        catalog = [entry for entry in session.get("catalog") or [] if isinstance(entry, dict) and entry.get("threadId")]
        for entry in catalog:
            if entry.get("threadId") == thread_id:
                entry["turnId"] = str(session.get("turnId") or "")
        with self.lock:
            value = self._read()
            value[provider] = catalog[-MAX_GIT_ENVIRONMENT_CONVERSATIONS:]
            self._write(value)


ENVIRONMENT_SETUP_SESSIONS_PATH = runtime.RUNTIME_DIR / "environment-setup-sessions.json"


ENVIRONMENT_SETUP_SESSIONS = GitEnvironmentSessionStore(ENVIRONMENT_SETUP_SESSIONS_PATH)
