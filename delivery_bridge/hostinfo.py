"""本机操作系统判定。

检测命令、安装命令、可执行文件后缀在 macOS / Windows / Linux 上都不一样，
好几个模块都要按平台分叉，所以判定收在这里一处。

需要在测试里假装成别的系统时，请打桩 ``hostinfo.host_platform``：
所有调用方都按模块名访问它，一处打桩全局生效。
"""

from __future__ import annotations

import platform


def host_platform() -> str:
    """桥接自己跑在哪个系统上。

    执行器和桥接在同一台机器上，系统由这里说了算，不让提示词去猜——猜出来的
    `python3` 在 Windows 上根本不存在，`winget` 在 macOS 上同理。
    """
    system = platform.system().strip().lower()
    if system == "darwin":
        return "macos"
    if system == "windows":
        return "windows"
    return "linux"


def host_platform_label(value: str = "") -> str:
    return {"macos": "macOS", "windows": "Windows"}.get(value or host_platform(), "Linux")
