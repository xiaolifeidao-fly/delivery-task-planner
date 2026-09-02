"""桥接进程自己维护的 pid 文件。

以前这个文件只有 `start_http.sh` 的非 Darwin 分支写（`echo $!`），
而 macOS 走的是 LaunchAgent（`install_http_service.py`）那条路，没人写，
于是文件里长期留着某次早年启动的 pid：真实进程换了几轮，文件纹丝不动。
任何按它判断「桥接在不在跑」的逻辑，在 macOS 上都会看走眼。

改由进程自己在启动时写、退出时清，这样不管是 LaunchAgent、nohup、
Windows 服务还是手工 `python3 http_bridge.py` 拉起来的，文件里都是真 pid。
"""

from __future__ import annotations

import atexit
import os
import signal
import sys
from pathlib import Path

from . import runtime


def pid_file_path() -> Path:
    """按模块名取运行时目录：测试改写 runtime.RUNTIME_DIR 时这里要跟着变。"""
    return runtime.RUNTIME_DIR / "http-bridge.pid"


def read_pid() -> int:
    """文件里记的 pid；没有文件或内容不是数字时返回 0。"""
    try:
        return int(pid_file_path().read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0


def write_pid(pid: int | None = None) -> None:
    """把当前进程号写进 pid 文件。写不进去只记一行，不能因此起不来。"""
    pid = os.getpid() if pid is None else pid
    path = pid_file_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{pid}\n", encoding="utf-8")
    except OSError as exc:
        print(f"写入桥接 pid 文件失败：{path}: {exc}", file=sys.stderr, flush=True)


def clear_pid(pid: int | None = None) -> None:
    """只删属于自己的那份记录。

    另一个桥接进程可能已经把文件覆盖成它的 pid 了（比如重启时新旧短暂并存），
    这时候删掉就等于把活着的那个进程从记录里抹掉。
    """
    pid = os.getpid() if pid is None else pid
    if read_pid() != pid:
        return
    try:
        pid_file_path().unlink()
    except OSError:
        pass


def track_current_process() -> None:
    """启动时登记，退出时清理，SIGTERM 也要清。

    LaunchAgent 停服务、restart_helper 重启都是发 SIGTERM，而 Python 默认
    对 SIGTERM 是直接死，`finally` 和 atexit 都跑不到。这里接一下只为把文件
    清掉，随后恢复默认处置并把信号原样再发给自己——进程该怎么死还怎么死，
    重启逻辑等的仍然是「进程消失」这件事，行为不变。
    """
    write_pid()
    atexit.register(clear_pid)

    def handle(signum: int, _frame: object) -> None:
        clear_pid()
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    for name in ("SIGTERM", "SIGINT"):
        number = getattr(signal, name, None)
        if number is None:
            continue
        try:
            signal.signal(number, handle)
        except (OSError, ValueError):
            # 非主线程或平台不支持这个信号：登记本身已经完成，不影响启动。
            continue
