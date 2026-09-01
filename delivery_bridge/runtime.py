"""桥接进程的运行时目录。

日志、缓存、待同步队列、会话落盘都放在这里，位置按平台惯例选，
并且允许用 DELIVERY_TASK_PLANNER_RUNTIME_DIR 覆盖（测试就是靠它把写入引开的）。

需要在测试里改写运行时目录的模块，请按 ``runtime.RUNTIME_DIR`` 这样带模块名访问，
不要 ``from .runtime import RUNTIME_DIR``——后者会把值绑成自己模块里的一份拷贝，
打桩改不到。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def default_runtime_dir() -> Path:
    configured = os.environ.get("DELIVERY_TASK_PLANNER_RUNTIME_DIR", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    if sys.platform == "win32" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "delivery-task-planner"
    return Path.home() / ".local" / "state" / "delivery-task-planner"


RUNTIME_DIR = default_runtime_dir()
