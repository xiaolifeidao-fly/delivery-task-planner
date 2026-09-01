"""Claude Code 的一次性子进程，外加自己维护的会话正文。

Claude 是 print 模式的一次性子进程，没有常驻线程服务可读，
所以会话记录只能自己落盘：ClaudeTranscriptStore 保存每一轮的条目，
读会话时直接从这份 transcript 还原，而不是问执行器要。
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import time
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from typing import Any

from .. import runtime
from ..attachments_text import message_with_attachments
from ..errors import BridgeFailure
from ..timeutil import utc_now

# 会话正文的落盘位置按模块名取，测试改写运行时目录时这里跟着变。
CLAUDE_TRANSCRIPTS_DIR = runtime.RUNTIME_DIR / "claude-transcripts"
MAX_CLAUDE_TRANSCRIPT_TURNS = 60

# Claude 的工具调用要还原成面板认识的条目类型，才能和 Codex 的会话长得一样。
CLAUDE_FILE_TOOLS = {"Edit", "MultiEdit", "Write", "NotebookEdit"}
CLAUDE_COMMAND_TOOLS = {"Bash", "BashOutput", "KillShell"}
# Codex 的过程是一条条 shell 命令，面板能从命令本身看出"这一步在读文件还是在检索"。
# Claude 用的是具名工具，命令行里没有对应的字面量，所以这里直接把语义标出来。
CLAUDE_READ_TOOLS = {"Read", "NotebookRead"}
CLAUDE_SEARCH_TOOLS = {"Grep", "Glob"}

class ClaudeTranscriptStore:
    """Persist Claude print-mode conversations so the board can reread them later.

    Codex 的 app-server 是常驻进程，`thread/read` 随时能读到完整历史；
    Claude 每一轮都是新起的子进程，回合结束客户端就关掉了，
    不落盘的话面板刷新一次聊天记录就空了。
    """

    def __init__(self, root: Path = CLAUDE_TRANSCRIPTS_DIR) -> None:
        self.root = root
        self.lock = threading.Lock()

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

    def write(self, thread_id: str, turns: list[dict[str, Any]]) -> None:
        if not thread_id:
            return
        path = self._path(thread_id)
        payload = {"threadId": thread_id, "updatedAt": utc_now(), "turns": turns[-MAX_CLAUDE_TRANSCRIPT_TURNS:]}
        with self.lock:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                temp_path = path.with_suffix(".tmp")
                temp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                os.chmod(temp_path, 0o600)
                os.replace(temp_path, path)
            except OSError as exc:
                print(f"保存 Claude 会话记录失败：{thread_id}: {exc}", file=sys.stderr, flush=True)


def text_line_count(value: Any) -> int:
    text = str(value or "")
    return len(text.splitlines()) if text else 0


def claude_edit_line_counts(name: str, payload: dict[str, Any]) -> tuple[int, int]:
    """Claude 不给 diff，只能按替换前后的文本数行，得出和 Codex 同量级的 `+N -M`。"""
    if name == "Write":
        return text_line_count(payload.get("content")), 0
    edits = payload.get("edits") if isinstance(payload.get("edits"), list) else []
    units = [edit for edit in edits if isinstance(edit, dict)] or [payload]
    added = sum(text_line_count(unit.get("new_string")) for unit in units)
    removed = sum(text_line_count(unit.get("old_string")) for unit in units)
    return added, removed


def claude_tool_item(block: dict[str, Any]) -> dict[str, Any]:
    """Map one Claude tool_use block onto the conversation item shape the board renders."""
    name = str(block.get("name") or "工具")
    payload = block.get("input") if isinstance(block.get("input"), dict) else {}
    item: dict[str, Any] = {"id": str(block.get("id") or secrets.token_urlsafe(8)), "status": "running"}
    if name in CLAUDE_COMMAND_TOOLS:
        command = str(payload.get("command") or payload.get("description") or "").strip()
        return {**item, "type": "commandExecution", "command": command or name}
    if name in CLAUDE_FILE_TOOLS:
        edits = payload.get("edits") if isinstance(payload.get("edits"), list) else []
        paths = [str(payload.get("file_path") or payload.get("notebook_path") or "").strip()]
        paths.extend(str(edit.get("file_path") or "").strip() for edit in edits if isinstance(edit, dict))
        kind = "add" if name == "Write" else "modify"
        added, removed = claude_edit_line_counts(name, payload)
        changes = [{"path": path, "kind": kind, "added": added, "removed": removed} for path in dict.fromkeys(paths) if path]
        return {**item, "type": "fileChange", "changes": changes}
    if name in CLAUDE_READ_TOOLS:
        target = str(payload.get("file_path") or payload.get("notebook_path") or "").strip()
        return {**item, "type": "dynamicToolCall", "tool": name, "action": "read", "target": target}
    if name in CLAUDE_SEARCH_TOOLS:
        target = str(payload.get("path") or payload.get("glob") or "").strip()
        pattern = str(payload.get("pattern") or "").strip()
        return {**item, "type": "dynamicToolCall", "tool": name, "action": "search", "target": target, "pattern": pattern}
    if name.startswith("mcp__"):
        # mcp__<服务>__<工具>：拆开才能显示成和 Codex 一样的「服务/工具」。
        parts = name.split("__")
        return {**item, "type": "mcpToolCall", "server": parts[1] if len(parts) > 2 else "", "tool": parts[-1]}
    return {**item, "type": "dynamicToolCall", "tool": name}


class ClaudeCLIClient:
    """Claude Code print-mode adapter exposing the lifecycle used by ExecutionBridge."""

    def __init__(
        self,
        workspace: Path,
        event_callback: Any = None,
        environment: dict[str, str] | None = None,
        transcripts: ClaudeTranscriptStore | None = None,
    ):
        self.workspace = workspace
        self.event_callback = event_callback
        self.environment = os.environ.copy()
        self.environment.update(environment or {})
        self.process: subprocess.Popen[str] | None = None
        self.thread_id = ""
        self.turn_id = ""
        self.turn_status = ""
        self.turns: list[dict[str, Any]] = []
        self.lock = threading.Lock()
        self.transcripts = transcripts or CLAUDE_TRANSCRIPTS
        # 落盘用的键固定成面板认识的那个会话号，即使 Claude 自己换了 session_id 也不换文件。
        self.transcript_key = ""

    def _start(self, prompt: str, model: str = "", resume: str = "", reasoning_effort: str = "", fast_mode: bool = False) -> tuple[str, str]:
        if shutil.which("claude") is None:
            raise BridgeFailure("未找到 Claude CLI")
        command = ["claude", "-p", prompt, "--output-format", "stream-json", "--verbose", "--dangerously-skip-permissions"]
        if model:
            command.extend(["--model", model])
        if reasoning_effort:
            command.extend(["--effort", reasoning_effort])
        if fast_mode:
            command.append("--fast")
        if resume:
            command.extend(["--resume", resume])
            self.thread_id = resume
        else:
            self.thread_id = str(uuid.uuid4())
            command.extend(["--session-id", self.thread_id])
        # 续聊时把之前几轮读回来，面板刷新后聊天记录不能只剩当前这一轮。
        self.transcript_key = self.thread_id
        self.turns = self.transcripts.read(self.transcript_key)
        self.turn_id = secrets.token_urlsafe(16)
        self.turn_status = "running"
        turn = {"id": self.turn_id, "status": "running", "createdAt": utc_now(), "items": [{"id": secrets.token_urlsafe(8), "type": "userMessage", "content": prompt}]}
        self.turns.append(turn)
        self._persist()
        self.process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
            cwd=self.workspace,
            env=self.environment,
        )
        threading.Thread(target=self._consume, args=(turn,), daemon=True).start()
        return self.thread_id, self.turn_id

    def _persist(self) -> None:
        self.transcripts.write(self.transcript_key or self.thread_id, self.turns)

    def _publish(self, item: dict[str, Any]) -> None:
        if self.event_callback:
            self.event_callback({"method": "item/completed", "params": {"item": item}})

    def _consume(self, turn: dict[str, Any]) -> None:
        assert self.process is not None and self.process.stdout is not None
        final_text = ""
        # 鉴权过期这类失败，Claude 会照常收尾并把错误当正文吐出来，只有 result 里的
        # is_error 说明这轮没成。不认它的话，面板会把「登录过期」显示成一条完成的回答。
        result_failed = False
        pending_tools: dict[str, dict[str, Any]] = {}
        for line in self.process.stdout:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            session_id = str(event.get("session_id") or event.get("sessionId") or "")
            # 续聊时 Claude 可能给出新的 session_id，但面板认的是原来那个，别把键换掉。
            if session_id and not self.transcript_key:
                self.thread_id = session_id
            event_type = str(event.get("type") or "")
            if event_type == "assistant":
                content = (event.get("message") or {}).get("content") or []
                for block in content if isinstance(content, list) else []:
                    if not isinstance(block, dict):
                        continue
                    block_type = str(block.get("type") or "")
                    if block_type == "text" and block.get("text"):
                        text = str(block["text"])
                        item = {"id": secrets.token_urlsafe(8), "type": "agentMessage", "text": text, "status": "completed"}
                        turn["items"].append(item)
                        self._publish({"type": "agentMessage", "text": text})
                    elif block_type == "tool_use":
                        # 命令、文件改动、其他工具都要留痕：直接用 Claude 时看到的就是这些。
                        item = claude_tool_item(block)
                        turn["items"].append(item)
                        pending_tools[str(block.get("id") or "")] = item
                        self._publish(item)
                    else:
                        continue
                self._persist()
            if event_type == "user":
                content = (event.get("message") or {}).get("content") or []
                for block in content if isinstance(content, list) else []:
                    if not isinstance(block, dict) or str(block.get("type") or "") != "tool_result":
                        continue
                    item = pending_tools.pop(str(block.get("tool_use_id") or ""), None)
                    if item is None:
                        continue
                    failed = bool(block.get("is_error"))
                    item["status"] = "failed" if failed else "completed"
                    if item.get("type") == "commandExecution":
                        item["exitCode"] = 1 if failed else 0
                self._persist()
            if event_type == "result":
                final_text = str(event.get("result") or final_text)
                result_failed = bool(event.get("is_error"))
                if not self.transcript_key:
                    self.thread_id = str(event.get("session_id") or self.thread_id)
        return_code = self.process.wait()
        failed = return_code != 0 or result_failed
        if final_text and not any(item.get("text") == final_text for item in turn["items"]):
            turn["items"].append({"id": secrets.token_urlsafe(8), "type": "agentMessage", "text": final_text, "status": "failed" if failed else "completed", "phase": "final_answer"})
        elif final_text:
            # 最终回复和最后一条 assistant 文本相同：把它标成终态，面板才认得出这是结论。
            for item in reversed(turn["items"]):
                if item.get("type") == "agentMessage" and item.get("text") == final_text:
                    item["phase"] = "final_answer"
                    if failed:
                        item["status"] = "failed"
                    break
        for item in pending_tools.values():
            item["status"] = "failed" if failed else "completed"
        self.turn_status = "failed" if failed else "completed"
        turn.update({"status": self.turn_status, "completedAt": utc_now()})
        self._persist()

    def start_task(
        self,
        title: str,
        prompt: str,
        attachments: list[dict[str, Any]] | None = None,
        model: str = "",
        reasoning_effort: str = "",
        fast_mode: bool = False,
    ) -> tuple[str, str]:
        text = message_with_attachments(prompt, attachments or [])
        thread_id, turn_id = self._start(text, model=model, reasoning_effort=reasoning_effort, fast_mode=fast_mode)
        return self.thread_id, turn_id

    def set_thread_name(self, thread_id: str, name: str, request_id: int = 2) -> None:
        # Claude CLI has no persistent thread-name endpoint. Its panel-side session
        # metadata is updated by the bridge, which is the title users see here.
        return

    def resume_thread(self, thread_id: str, request_id: int = 10) -> dict[str, Any]:
        self.thread_id = thread_id
        self.transcript_key = thread_id
        return {"thread": {"id": thread_id}}

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
        self.thread_id = thread_id
        return self._start(
            message_with_attachments(text, attachments or []),
            model=model,
            resume=thread_id,
            reasoning_effort=reasoning_effort,
            fast_mode=fast_mode,
        )[1]

    def steer_turn(self, thread_id: str, turn_id: str, text: str, attachments: list[dict[str, Any]] | None = None, request_id: int = 12) -> str:
        raise BridgeFailure("Claude 当前回合运行中，请等待完成后再发送追加要求")

    def interrupt_turn(self, thread_id: str, turn_id: str, request_id: int = 13) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()

    def read_thread(self, thread_id: str, request_id: int = 100, timeout: float = 20) -> dict[str, Any]:
        # 本进程跑过这条会话就用内存里的实时状态，否则回落到落盘的历史记录。
        if self.turns and thread_id in {self.transcript_key, self.thread_id, ""}:
            return {"id": thread_id or self.thread_id, "turns": list(self.turns)}
        return {"id": thread_id, "turns": self.transcripts.read(thread_id)}

    def read_turn(self, thread_id: str, turn_id: str, request_id: int = 100) -> dict[str, Any]:
        turns = self.read_thread(thread_id).get("turns") or []
        return next((turn for turn in turns if turn.get("id") == turn_id), {})

    def wait_turn(self, turn_id: str, poll_interval: float = 0.2) -> str:
        while self.process and self.process.poll() is None:
            time.sleep(poll_interval)
        return self.turn_status or "failed"

    def next_request_id(self) -> int:
        return 1

    def stderr_tail(self, limit: int = 10) -> str:
        # Claude CLI 的诊断信息走 stream-json，stderr 这一路没有可留痕的内容。
        return ""

    def list_models(self, request_id: int = 20) -> list[dict[str, Any]]:
        return [{"model": value, "displayName": label} for value, label in [("opus", "Opus 5"), ("sonnet", "Sonnet 5")]]

    def close(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()


CLAUDE_TRANSCRIPTS = ClaudeTranscriptStore()
