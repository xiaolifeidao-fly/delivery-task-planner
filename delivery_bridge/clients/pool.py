"""只读会话正文用的执行器复用池 + 极短 TTL 快照缓存。

面板每 3~5 秒就要拉一次整段会话正文，而 Codex 的读法是「拉起一个
`codex app-server` 子进程 → 握手 → thread/read → 关掉」，一次几百毫秒到几秒。
几个弹窗一起轮询时这些子进程互相抢 CPU，单次请求被推到 20 秒开外，前端只能
按超时 abort。这里把只读执行器留着复用，再给正文加一个很短的 TTL，
把同一瞬间的重复轮询合并成一次真实读取。

只服务「没有活跃回合」的线程：回合在跑时调用方用的是那一路自己的 client。
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from typing import Any

import server as planner

from ..errors import BridgeFailure
from . import factory

# 只读快照的有效期。面板 3~5 秒轮询一次，这个值只用来合并「同一瞬间的重复读」，
# 不会让正在跑的回合看起来卡住：回合活跃时正文走的是那一路自己的 client。
THREAD_SNAPSHOT_TTL_SECONDS = 2.0
# 只读执行器闲置多久就回收，别让空转的子进程一直挂着。
THREAD_READER_IDLE_SECONDS = 300.0
# 回合正在跑时读正文用的上限。这条路子共用执行器那一路的 JSON-RPC client，
# app-server 忙着串流时不一定马上答 `thread/read`，必须明显低于面板 20s 的超时，
# 否则浏览器先 abort、桥接还在空等，用户看到的就是一串 `(canceled)`。
ACTIVE_THREAD_READ_TIMEOUT_SECONDS = 8.0
# 兜底快照最多留几条，够覆盖同时开着的几个会话窗口就行。
THREAD_LAST_GOOD_LIMIT = 64

class ThreadReaderPool:
    """只读会话正文用的执行器复用池 + 极短 TTL 快照缓存。

    面板每 3~5 秒就要拉一次整段会话正文，而 Codex 的读法是「拉起一个
    `codex app-server` 子进程 → 握手 → thread/read → 关掉」，一次几百毫秒到几秒。
    几个弹窗一起轮询时这些子进程互相抢 CPU，单次请求被推到 20 秒开外，前端只能
    按超时 abort（DevTools 里就是 `(canceled)`）。这里把只读执行器留着复用，
    再给正文加一个很短的 TTL，把同一瞬间的重复轮询合并成一次真实读取。

    只服务「没有活跃回合」的线程：回合在跑时调用方用的是那一路自己的 client，
    正文直接来自内存，不经过这里，所以 TTL 带来的滞后最多一个 TTL。
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.readers: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.snapshots: dict[tuple[str, str, str], tuple[float, dict[str, Any]]] = {}
        # 最后一次读到正文的快照，没有 TTL：回合跑到一半读不回来时拿它兜底，
        # 总比把空会话甩给面板强（增量内容前端还有 SSE 那一路）。
        self.last_good_snapshots: dict[tuple[str, str, str], dict[str, Any]] = {}

    @staticmethod
    def _environment_signature(environment: dict[str, str] | None) -> str:
        return json.dumps(sorted((environment or {}).items()), ensure_ascii=False)

    @staticmethod
    def _alive(client: Any) -> bool:
        process = getattr(client, "process", None)
        # Claude 适配器平时没有常驻进程，构造本身也不贵，按存活处理即可。
        return process is None or process.poll() is None

    def _take_idle(self, now: float) -> list[dict[str, Any]]:
        """摘出闲置太久的执行器，真正关进程放到锁外做，别让读请求跟着等。"""
        stale: list[dict[str, Any]] = []
        for key, entry in list(self.readers.items()):
            if now - float(entry.get("usedAt") or 0.0) <= THREAD_READER_IDLE_SECONDS:
                continue
            stale.append(self.readers.pop(key))
        # 过期快照顺手清掉，别让 dict 随会话数一直涨。
        for key, (stamp, _) in list(self.snapshots.items()):
            if now - stamp > THREAD_SNAPSHOT_TTL_SECONDS:
                self.snapshots.pop(key, None)
        return stale

    @staticmethod
    def _close_all(entries: list[dict[str, Any]]) -> None:
        for entry in entries:
            try:
                entry["client"].close()
            except Exception:
                pass

    def _reader(self, provider: str, workspace: Path, environment: dict[str, str] | None) -> dict[str, Any]:
        key = (provider, str(workspace), self._environment_signature(environment))
        now = time.time()
        with self.lock:
            stale = self._take_idle(now)
            entry = self.readers.get(key)
            if entry is not None and not self._alive(entry["client"]):
                self.readers.pop(key, None)
                entry = None
            if entry is None:
                entry = {
                    "client": factory.create_ai_client(provider, workspace, environment=environment),
                    "lock": threading.Lock(),
                    "usedAt": now,
                }
                self.readers[key] = entry
            entry["usedAt"] = now
        self._close_all(stale)
        return entry

    def read(
        self,
        provider: str,
        workspace: Path,
        environment: dict[str, str] | None,
        thread_id: str,
    ) -> dict[str, Any]:
        if not thread_id:
            return {}
        cache_key = (provider, str(workspace), thread_id)
        now = time.time()
        with self.lock:
            cached = self.snapshots.get(cache_key)
            if cached is not None and now - cached[0] <= THREAD_SNAPSHOT_TTL_SECONDS:
                return cached[1]
        entry = self._reader(provider, workspace, environment)
        with entry["lock"]:
            # 拿到读锁的这一刻可能别人刚读完，再看一眼缓存，省掉一次真实读取。
            with self.lock:
                cached = self.snapshots.get(cache_key)
                if cached is not None and time.time() - cached[0] <= THREAD_SNAPSHOT_TTL_SECONDS:
                    return cached[1]
            if not self._alive(entry["client"]):
                entry = self._reader(provider, workspace, environment)
            thread = read_thread_or_empty(entry["client"], thread_id)
        with self.lock:
            self.snapshots[cache_key] = (time.time(), thread)
        self.remember(provider, workspace, thread_id, thread)
        return thread

    def remember(self, provider: str, workspace: Path, thread_id: str, thread: dict[str, Any]) -> None:
        """记下最后一次读到的正文，供回合忙时兜底。空会话不记，免得把兜底也污染掉。"""
        if not thread_id or not (thread.get("turns") or []):
            return
        key = (provider, str(workspace), thread_id)
        with self.lock:
            self.last_good_snapshots.pop(key, None)
            self.last_good_snapshots[key] = thread
            while len(self.last_good_snapshots) > THREAD_LAST_GOOD_LIMIT:
                self.last_good_snapshots.pop(next(iter(self.last_good_snapshots)))

    def last_good(self, provider: str, workspace: Path, thread_id: str) -> dict[str, Any]:
        with self.lock:
            return self.last_good_snapshots.get((provider, str(workspace), thread_id)) or {}

    def invalidate(self, thread_id: str = "") -> None:
        """会话正文被改过（发消息、回合结束、停止）时丢掉快照，别让面板多等一个 TTL。"""
        with self.lock:
            if not thread_id:
                self.snapshots.clear()
                return
            for key in [key for key in self.snapshots if key[2] == thread_id]:
                self.snapshots.pop(key, None)

    def shutdown(self) -> None:
        with self.lock:
            readers = list(self.readers.values())
            self.readers.clear()
            self.snapshots.clear()
            self.last_good_snapshots.clear()
        self._close_all(readers)


THREAD_READERS = ThreadReaderPool()


def read_thread_or_empty(client: Any, thread_id: str, timeout: float = 20) -> dict[str, Any]:
    """读不到会话正文时按空会话返回，不把错误抛给需求编辑和任务详情。

    会话正文只落在发起这条聊天的那台机器上（Codex 的 rollout、Claude 的
    transcript）。别人在自己电脑上聊出来的会话，本机自然读不到，这属于常态而不是
    故障，所以只保留目录里的会话条目、正文留空即可。
    """
    if not thread_id:
        return {}
    try:
        return client.read_thread(thread_id, request_id=client.next_request_id(), timeout=timeout)
    except (BridgeFailure, planner.ToolFailure, OSError, ValueError) as exc:
        print(f"本机读取会话正文失败，按空会话处理：{thread_id}: {exc}", file=sys.stderr, flush=True)
        return {}
