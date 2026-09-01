"""Codex app-server 的一条 JSON-RPC 会话。

拉起 `codex app-server` 子进程，按请求号收发消息，跑一个回合并等它结束。
app-server 的 stderr 只在出问题时才有内容（MCP 启动超时、模型列表拉取失败之类），
所以只留最后几行——够定位，又不会把整段堆在内存里。

刚起的会话有一小段时间读不出来：rollout 文件还没落盘，thread/read 直接报
「rollout ... is empty」。这类失败是瞬时的，容忍 THREAD_READ_GRACE_SECONDS
之后仍然读不到才当成真的失败。
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

import server as planner

from ..attachments_text import message_with_attachments
from ..codex_cli import provision_codex_cli
from ..errors import BridgeFailure
from ..turn_output import TERMINAL_TURN_STATUSES
from .journal import THREAD_ITEMS, merge_journal_turns

# 刚起的会话读不出来是瞬时的，忍这么久之后仍然读不到才算失败。
THREAD_READ_GRACE_SECONDS = 30.0
# turn/start 的 summary 取值：auto / concise / detailed / none。auto 由模型自己决定详略，
# 实测常常只给一两句。注意 app-server 会静默忽略不认识的字段，改名字前先实际验一遍。
TURN_REASONING_SUMMARY = "detailed"
# stderr 只在出问题时才有内容，留最后这几行就够定位。
APP_SERVER_STDERR_TAIL = 40

class AppServerClient:
    def __init__(self, workspace: Path, event_callback: Any = None, environment: dict[str, str] | None = None):
        self.workspace = workspace
        self.event_callback = event_callback
        process_environment = os.environ.copy()
        process_environment.update(environment or {})
        codex_command = provision_codex_cli()
        if not codex_command:
            raise BridgeFailure("未找到 Codex CLI 或 Codex Desktop 资源目录中的可执行文件")
        self.process = subprocess.Popen(
            [codex_command, "app-server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
            cwd=workspace,
            env=process_environment,
        )
        # Responses and lifecycle notifications are consumed by different callers.
        # Keeping them separate prevents a progress follower from swallowing the
        # response for a concurrent steer or interrupt request.
        self.messages: queue.Queue[dict[str, Any]] = queue.Queue()
        self.responses: queue.Queue[dict[str, Any]] = queue.Queue()
        self.write_lock = threading.Lock()
        self.response_lock = threading.Lock()
        self.stderr_lock = threading.Lock()
        self.stderr_lines: deque[str] = deque(maxlen=APP_SERVER_STDERR_TAIL)
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._drain_stderr, daemon=True).start()
        self.send(
            "initialize",
            0,
            {"clientInfo": {"name": "delivery_task_planner", "title": "Delivery Task Planner", "version": "0.1.0"}},
        )
        self.wait_response(0)
        self.notify("initialized", {})

    def _read_stdout(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            try:
                message = json.loads(line)
                # 先留痕再分发：`thread/read` 读不回执行过程，只有这条实时流是完整的。
                try:
                    THREAD_ITEMS.record(message)
                except Exception as exc:  # 记录失败不能拖垮读流线程
                    print(f"记录 Codex 会话过程失败：{exc}", file=sys.stderr, flush=True)
                if self.event_callback is not None:
                    self.event_callback(message)
                if "id" in message:
                    self.responses.put(message)
                else:
                    self.messages.put(message)
            except json.JSONDecodeError:
                continue

    def _drain_stderr(self) -> None:
        """留痕而不是丢弃：这一路是 app-server 唯一会说出问题的地方。

        以前这里直接 `pass`，MCP 启动超时、模型列表拉不回来这类告警在桥接器侧
        完全没有痕迹，只能事后去翻 `~/.codex/sessions` 的 rollout。
        """
        assert self.process.stderr is not None
        for line in self.process.stderr:
            text = line.rstrip()
            if not text:
                continue
            with self.stderr_lock:
                self.stderr_lines.append(text)
            print(f"[codex app-server] {text}", file=sys.stderr, flush=True)

    def stderr_tail(self, limit: int = 10) -> str:
        with self.stderr_lock:
            return "\n".join(list(self.stderr_lines)[-limit:])

    def write(self, message: dict[str, Any]) -> None:
        with self.write_lock:
            if self.process.poll() is not None:
                raise BridgeFailure("Codex App Server 已退出")
            assert self.process.stdin is not None
            self.process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
            self.process.stdin.flush()

    def send(self, method: str, request_id: int, params: dict[str, Any]) -> None:
        self.write({"method": method, "id": request_id, "params": params})

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self.write({"method": method, "params": params})

    def wait_response(self, request_id: int, timeout: float = 20) -> dict[str, Any]:
        # 排队时间也算进预算：以前 deadline 是拿到锁之后才起算，一个回合正在跑的
        # client 上并发发请求，实际等待会变成「排队 + timeout」，轻松超过调用方的上限。
        deadline = time.monotonic() + timeout
        if not self.response_lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
            raise BridgeFailure("等待 Codex 响应超时")
        try:
            deferred: list[dict[str, Any]] = []
            while time.monotonic() < deadline:
                try:
                    message = self.responses.get(timeout=min(0.5, deadline - time.monotonic()))
                except queue.Empty:
                    continue
                if message.get("id") == request_id:
                    for later in deferred:
                        self.responses.put(later)
                    if message.get("error"):
                        raise BridgeFailure(str(message["error"].get("message") or "Codex 请求失败"))
                    return message.get("result") or {}
                deferred.append(message)
            for later in deferred:
                self.responses.put(later)
        finally:
            self.response_lock.release()
        raise BridgeFailure("等待 Codex 响应超时")

    def start_task(
        self,
        title: str,
        prompt: str,
        attachments: list[dict[str, Any]] | None = None,
        model: str = "",
        reasoning_effort: str = "",
        fast_mode: bool = False,
    ) -> tuple[str, str]:
        thread_params = {
            "cwd": str(self.workspace),
            "approvalPolicy": "never",
            "sandbox": "danger-full-access",
            "threadSource": "user",
            "ephemeral": False,
        }
        if model:
            thread_params["model"] = model
        self.send(
            "thread/start",
            1,
            thread_params,
        )
        thread_result = self.wait_response(1)
        thread = thread_result.get("thread") or {}
        thread_id = str(thread.get("id") or "")
        if not thread_id:
            raise BridgeFailure("Codex 没有返回 thread id")
        self.thread_id = thread_id
        self.send("thread/name/set", 2, {"threadId": thread_id, "name": title[:128]})
        self.wait_response(2)
        turn_params = {
            "threadId": thread_id,
            "input": self._input_parts(prompt, attachments),
            "cwd": str(self.workspace),
            "approvalPolicy": "never",
            "sandboxPolicy": {"type": "dangerFullAccess"},
        }
        if model:
            turn_params["model"] = model
        if reasoning_effort:
            turn_params["effort"] = reasoning_effort
        # ``detailed`` asks Codex for the fullest supported *summary*, never for
        # raw reasoning. 合法取值是 auto / concise / detailed / none；auto 由模型自己
        # 决定详略，实测经常只给一两句，和桌面版看到的过程对不上。
        turn_params["summary"] = TURN_REASONING_SUMMARY
        self.send(
            "turn/start",
            3,
            turn_params,
        )
        turn_result = self.wait_response(3)
        turn_id = str((turn_result.get("turn") or {}).get("id") or "")
        return thread_id, turn_id

    def set_thread_name(self, thread_id: str, name: str, request_id: int = 2) -> None:
        """Rename an existing Codex thread after its first answer is available."""
        if not thread_id or not name.strip():
            return
        self.send("thread/name/set", request_id, {"threadId": thread_id, "name": name.strip()[:128]})
        self.wait_response(request_id)

    def resume_thread(self, thread_id: str, request_id: int = 10) -> dict[str, Any]:
        self.send("thread/resume", request_id, {"threadId": thread_id, "cwd": str(self.workspace)})
        result = self.wait_response(request_id)
        self.thread_id = thread_id
        return result

    def start_turn(
        self,
        thread_id: str,
        text: str,
        attachments: list[dict[str, Any]] | None = None,
        request_id: int = 11,
        model: str = "",
        reasoning_effort: str = "",
        fast_mode: bool = False,
    ) -> str:
        params = {
            "threadId": thread_id,
            "input": self._input_parts(text, attachments),
            "cwd": str(self.workspace),
            "approvalPolicy": "never",
            "sandboxPolicy": {"type": "dangerFullAccess"},
        }
        if model:
            params["model"] = model
        if reasoning_effort:
            params["effort"] = reasoning_effort
        params["summary"] = TURN_REASONING_SUMMARY
        self.send(
            "turn/start",
            request_id,
            params,
        )
        result = self.wait_response(request_id)
        turn_id = str((result.get("turn") or {}).get("id") or "")
        if not turn_id:
            raise BridgeFailure("Codex 没有返回 turn id")
        return turn_id

    def list_models(self, request_id: int = 20) -> list[dict[str, Any]]:
        self.send("model/list", request_id, {"limit": 100})
        result = self.wait_response(request_id)
        models = result.get("data") or []
        return models if isinstance(models, list) else []

    def steer_turn(
        self,
        thread_id: str,
        turn_id: str,
        text: str,
        attachments: list[dict[str, Any]] | None = None,
        request_id: int = 12,
    ) -> str:
        self.send(
            "turn/steer",
            request_id,
            {
                "threadId": thread_id,
                "expectedTurnId": turn_id,
                "input": self._input_parts(text, attachments),
            },
        )
        result = self.wait_response(request_id)
        return str(result.get("turnId") or turn_id)

    @staticmethod
    def _input_parts(text: str, attachments: list[dict[str, Any]] | None = None) -> list[dict[str, str]]:
        # 正文永远是第一段，附件说明统一在这里兜底补齐：不是每条发送链路都自己拼过附件段，
        # 少了它，非图片附件对 Codex 就等于没传（图片还有 localImage，文件什么都不剩）。
        parts: list[dict[str, str]] = [{"type": "text", "text": message_with_attachments(text, attachments or [])}]
        for attachment in attachments or []:
            path = str(attachment.get("path") or "")
            if attachment.get("isImage") and path:
                parts.append({"type": "localImage", "path": path})
        return parts

    def interrupt_turn(self, thread_id: str, turn_id: str, request_id: int = 13) -> None:
        self.send("turn/interrupt", request_id, {"threadId": thread_id, "turnId": turn_id})
        self.wait_response(request_id)

    def read_thread(self, thread_id: str, request_id: int = 100, timeout: float = 20) -> dict[str, Any]:
        self.send("thread/read", request_id, {"threadId": thread_id, "includeTurns": True})
        result = self.wait_response(request_id, timeout)
        thread = result.get("thread") or {}
        if not isinstance(thread, dict):
            return {}
        return merge_journal_turns(thread, THREAD_ITEMS.read(thread_id))

    def next_request_id(self) -> int:
        request_id = int(getattr(self, "request_sequence", 1000)) + 1
        self.request_sequence = request_id
        return request_id

    def read_turn(self, thread_id: str, turn_id: str, request_id: int = 100) -> dict[str, Any]:
        turns = self.read_thread(thread_id, request_id).get("turns") or []
        turn = next((item for item in turns if str(item.get("id") or "") == turn_id), None)
        return turn if isinstance(turn, dict) else {}

    def read_turn_status(self, thread_id: str, turn_id: str, request_id: int = 100) -> str:
        return str(self.read_turn(thread_id, turn_id, request_id).get("status") or "")

    def wait_turn(self, turn_id: str, poll_interval: float = 2) -> str:
        next_poll = 0.0
        # 第一次读失败的时刻。读不出来不代表这一轮废了：turn/completed 通知照样会到，
        # 会话刚建好的那几秒尤其容易读空，所以先忍一段时间，久读不到才把错抛出去。
        first_failure = 0.0
        while self.process.poll() is None:
            now = time.monotonic()
            if now >= next_poll:
                try:
                    status = self.read_turn_status(self.thread_id, turn_id, self.next_request_id())
                    first_failure = 0.0
                except BridgeFailure:
                    if not first_failure:
                        first_failure = now
                    elif now - first_failure >= THREAD_READ_GRACE_SECONDS:
                        raise
                    status = ""
                if status in TERMINAL_TURN_STATUSES:
                    return status
                next_poll = time.monotonic() + poll_interval
            try:
                message = self.messages.get(timeout=max(0.01, min(0.5, next_poll - time.monotonic())))
            except queue.Empty:
                continue
            if message.get("method") == "turn/completed":
                turn = (message.get("params") or {}).get("turn") or {}
                if not turn_id or str(turn.get("id") or "") == turn_id:
                    return str(turn.get("status") or "failed")
        return "failed"

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
