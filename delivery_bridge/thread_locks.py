"""Codex 线程写入锁：认出来、查是谁占着、必要时收掉。

Codex 对每条线程只允许一个写入者——往该线程 rollout 文件追加内容的那个进程，锁挂在
`<CODEX_HOME>/thread-writer-locks/<线程号>.lock` 上。桥接每续一次聊都要先
`thread/resume` 接回原来的线程，抢不到锁时 app-server 只回一句英文
`thread <id> already has an active writer`，面板上就是一条看不懂、也不知道该怎么办的
报错。

这里把那句话翻成「谁占着、能不能收」：先从报错里认出线程号，再顺着锁文件查出持锁
进程，最后按来路分档——桌面端 Codex 是用户自己正开着的窗口，只说明不动它；桥接拉起的
app-server（多半是上一次没退干净留下的孤儿）才允许结束。真正的结束一定要面板上的人
点过按钮才发生，这个模块自己不杀任何进程。
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

from . import hostinfo
from .codex_cli import codex_home
from .errors import BridgeFailure


# app-server 抢不到写入锁时回的原话，线程号就写在这句里。Codex 这条错误没有错误码，
# 只能按原话认；哪天上游把话改了，这里认不出来，面板退回原样显示英文报错，不会误伤
# 别的失败。
WRITER_BUSY_RE = re.compile(
    r"thread\s+([0-9a-fA-F][0-9a-fA-F-]{7,63})\s+already has an active writer",
    re.IGNORECASE,
)

# 面板凭这个码认出「线程被占用」这一类失败，不必让前端去匹配英文原话。
FAILURE_CODE = "thread_writer_busy"

LOCK_DIRECTORY_NAME = "thread-writer-locks"

# 线程号来自浏览器，必须先卡形状：它要拼进锁文件路径，还要决定去结束哪个进程。
THREAD_ID_RE = re.compile(r"[0-9a-fA-F][0-9a-fA-F-]{7,63}")

# 顺着父进程往上找几层就够认出来路了：桌面端是 `ChatGPT → codex → codex app-server`，
# 桥接是 `python → codex app-server`。设上限只是防 ppid 成环时空转。
MAX_ANCESTRY_DEPTH = 8

# 带 GUI 外壳的祖先 = 这条 app-server 是某个桌面应用正开着的会话，不能替用户收掉。
DESKTOP_ANCESTOR_MARKERS = (".app/contents/macos/", "chatgpt.exe", "codex.exe")

# 桌面端自己拉起 app-server 才会带的参数。桥接也可能直接用桌面版资源目录里的那个
# codex 二进制（见 codex_cli.provision_codex_cli），所以光看可执行文件路径分不出来路，
# 父进程链又会在进程被重新挂到 init 名下时断掉，这几个参数是最后一层凭据。
DESKTOP_ARGUMENT_MARKERS = ("--analytics-default-enabled", "features.code_mode_host", "mcp_servers.codex_app=")

HOLDER_LABELS = {
    "desktop": "Codex 桌面端",
    "bridge": "任务面板拉起的 Codex 执行器",
    "unknown": "未知进程",
}

# 查进程只是为了给按钮配一句说明，卡住了就当查不到，别把一次发消息拖死在这里。
PROBE_TIMEOUT_SECONDS = 5.0

# SIGTERM 之后留给它收尾的时间，过了还在就 SIGKILL。app-server 收到信号要把 rollout
# 落盘，太短会把会话正文截在半截。
TERMINATE_GRACE_SECONDS = 3.0


def busy_thread_id(message: str) -> str:
    """从一句失败里认出被占用的线程号，认不出返回空串。"""
    matched = WRITER_BUSY_RE.search(message or "")
    return matched.group(1) if matched else ""


def valid_thread_id(thread_id: str) -> str:
    thread_id = str(thread_id or "").strip()
    if not THREAD_ID_RE.fullmatch(thread_id):
        raise BridgeFailure("线程标识无效")
    return thread_id


def lock_path(thread_id: str) -> Path:
    return codex_home() / LOCK_DIRECTORY_NAME / f"{valid_thread_id(thread_id)}.lock"


def _run(command: list[str]) -> str:
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return completed.stdout.strip() if completed.returncode == 0 else ""


def _process_command(pid: int) -> str:
    """进程的完整命令行；进程已经不在了就是空串。"""
    lines = _run(["ps", "-o", "command=", "-p", str(pid)]).splitlines()
    return lines[0].strip() if lines else ""


def _parent_pid(pid: int) -> int:
    try:
        return int(_run(["ps", "-o", "ppid=", "-p", str(pid)]).strip())
    except ValueError:
        return 0


def _ancestry(pid: int) -> list[str]:
    """从父进程往上的命令行，用来判断这条 app-server 是谁开的。"""
    commands: list[str] = []
    current = _parent_pid(pid)
    for _ in range(MAX_ANCESTRY_DEPTH):
        if current <= 1:
            break
        command = _process_command(current)
        if not command:
            break
        commands.append(command)
        current = _parent_pid(current)
    return commands


def _lock_holder_pids(path: Path) -> list[int]:
    """lsof 认得住 flock：锁文件本身是空的，占没占着只看有没有进程开着它。

    Windows 上没有 lsof，查不出来就当查不到——面板照样会说「线程被占用」，只是给不出
    「是谁」，那个结束进程的按钮也就不会出现。
    """
    if hostinfo.host_platform() == "windows":
        return []
    pids: list[int] = []
    for line in _run(["lsof", "-t", "--", str(path)]).splitlines():
        try:
            pid = int(line.strip())
        except ValueError:
            continue
        if pid > 1 and pid != os.getpid() and pid not in pids:
            pids.append(pid)
    return pids


def _classify(command: str, ancestry: list[str]) -> tuple[str, bool]:
    """判断持锁进程的来路，以及桥接能不能替用户收掉它。

    只有「确实是一条 codex app-server、而且不是桌面端开的」才允许结束：桌面端那条是
    用户自己正开着的窗口，收掉它等于替人家把应用关了；连 app-server 都不是的进程更不
    该碰——锁文件被别的东西开着，只说明我们认错了。
    """
    lowered = command.lower()
    if "app-server" not in lowered or "codex" not in lowered:
        return "unknown", False
    if any(marker in lowered for marker in DESKTOP_ARGUMENT_MARKERS):
        return "desktop", False
    if any(marker in parent.lower() for parent in ancestry for marker in DESKTOP_ANCESTOR_MARKERS):
        return "desktop", False
    return "bridge", True


def _describe(pid: int) -> dict[str, Any] | None:
    command = _process_command(pid)
    if not command:
        return None
    kind, killable = _classify(command, _ancestry(pid))
    return {
        "pid": pid,
        "kind": kind,
        "killable": killable,
        "label": HOLDER_LABELS.get(kind, HOLDER_LABELS["unknown"]),
        # 命令行原样给面板，让点按钮的人自己看清要结束的是什么。
        "command": command[:400],
    }


def holder_of(thread_id: str) -> dict[str, Any] | None:
    """谁正握着这条线程的写入锁；锁已经释放、或本机查不出来时返回 None。

    一把锁可能挂着好几个进程：子进程会继承父进程打开的文件描述符，lsof 就会把它们
    一起列出来。挑哪一个报给面板不能看顺序——只要里面有桌面端，就以桌面端为准，
    拿不准的时候一律往「不能收」的方向靠；没有桌面端才轮到真正该收的那条 app-server。
    """
    try:
        path = lock_path(thread_id)
    except BridgeFailure:
        return None
    if not path.exists():
        return None
    holders = [holder for holder in (_describe(pid) for pid in _lock_holder_pids(path)) if holder]
    if not holders:
        return None
    for kind in ("desktop", "bridge"):
        for holder in holders:
            if holder["kind"] == kind:
                return holder
    return holders[0]


def _explain(thread_id: str, holder: dict[str, Any] | None) -> str:
    if holder is None:
        return "这条会话线程正被另一个 Codex 进程占用，没能查出是哪一个，请稍后重发。"
    label = holder.get("label") or HOLDER_LABELS["unknown"]
    pid = holder.get("pid")
    if holder.get("kind") == "desktop":
        return f"这条会话线程正被 {label}（进程 {pid}）占用。请在桌面端关掉这条会话，再回来重发。"
    if holder.get("killable"):
        return f"这条会话线程正被 {label}（进程 {pid}）占用，多半是上一次没退干净留下的。结束它就能继续。"
    return f"这条会话线程正被 {label}（进程 {pid}）占用，任务面板不会替你结束它。"


def failure_payload(message: str) -> dict[str, Any] | None:
    """把 app-server 的那句英文翻成面板能直接用的结构；不是这类失败就返回 None。"""
    thread_id = busy_thread_id(message)
    if not thread_id:
        return None
    holder = holder_of(thread_id)
    return {
        "error": _explain(thread_id, holder),
        "code": FAILURE_CODE,
        "threadId": thread_id,
        "holder": holder,
        # 原话留一份：出问题时要能对上 Codex 那边的日志。
        "detail": message,
    }


def enrich_failure(value: dict[str, Any]) -> dict[str, Any]:
    """出错应答统一过这一道，认出线程被占用就补上线程号和持锁进程。

    放在应答出口而不是某个调用点，是因为每一路会话都要先 `thread/resume` 才能续聊，
    撞上这个锁的路径有十几条；在这里翻一次，新加的接口也自动照顾到。
    """
    if not isinstance(value, dict) or "code" in value:
        return value
    message = value.get("error")
    if not isinstance(message, str):
        return value
    payload = failure_payload(message)
    return {**value, **payload} if payload else value


def inspect(thread_id: str) -> dict[str, Any]:
    """这条线程此刻还被不被占着——面板点按钮之前会再问一次。"""
    thread_id = valid_thread_id(thread_id)
    holder = holder_of(thread_id)
    return {"threadId": thread_id, "busy": holder is not None, "holder": holder}


def _terminate(pid: int) -> None:
    """先 SIGTERM 让它把 rollout 落盘，真赖着不走再 SIGKILL。"""
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except PermissionError as exc:
        raise BridgeFailure(f"没有权限结束进程 {pid}：{exc}") from exc
    deadline = time.monotonic() + TERMINATE_GRACE_SECONDS
    while time.monotonic() < deadline:
        time.sleep(0.2)
        if not _process_command(pid):
            return
    try:
        os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
    except (ProcessLookupError, PermissionError):
        return


def release(thread_id: str) -> dict[str, Any]:
    """结束占着这条线程的执行器。只有面板上的人点过按钮才会走到这里。"""
    thread_id = valid_thread_id(thread_id)
    holder = holder_of(thread_id)
    if holder is None:
        # 锁本来就没人占：多半是刚才那次失败之后对方自己退了，直接让面板重发。
        return {
            "threadId": thread_id,
            "released": True,
            "holder": None,
            "message": "这条线程已经没有进程占用，可以直接重发。",
        }
    if not holder.get("killable"):
        raise BridgeFailure(_explain(thread_id, holder))
    pid = int(holder["pid"])
    _terminate(pid)
    # 收掉一个还不够就得说实话：子进程继承着同一个文件描述符时，父进程没了锁照样占着，
    # 这时候让面板去重发只会再撞一次墙。
    remaining = holder_of(thread_id)
    if remaining is not None:
        raise BridgeFailure(
            f"已结束进程 {pid}，但线程写入锁仍被进程 {remaining.get('pid')} 占用，请手动确认它的状态。"
        )
    return {
        "threadId": thread_id,
        "released": True,
        "holder": holder,
        "message": f"已结束进程 {pid}，可以重发了。",
    }
