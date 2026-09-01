"""「预设环境」的清单、探测与安装参数。

装的是本机全局环境（Python / Node / Go / Git 之类），不挂在任何业务仓库上。

版本下限、探测命令、安装命令都写死在这里，是唯一事实来源：前端只传标识。
检测和安装命令按 macOS / Windows 分开写——两个系统的命令名和包管理器都不一样，
交给执行器现猜会猜出 Windows 上根本不存在的 `python3`。
"""

from __future__ import annotations

import re
import shlex
import subprocess
from typing import Any

from . import hostinfo
from .errors import BridgeFailure
from .github_ssh import github_ssh_key_status
from .providers import ai_provider_of, fast_mode_of, reasoning_effort_of


MAX_ENVIRONMENT_SETUP_ITEMS = 12

# 预设环境属于当前电脑，不属于任务面板中的任一项目。
GLOBAL_ENVIRONMENT_SETUP_PROGRAM_ID = 0

# 预设环境的版本下限、探测命令和安装命令：前端只传标识，这份是唯一事实来源。
# 检测和安装命令按 macOS / Windows 分开写死 —— 两个系统的命令名和包管理器都不一样，
# 交给执行器现猜会猜出 Windows 上根本不存在的 `python3`。
ENVIRONMENT_PRESETS: dict[str, dict[str, Any]] = {
    "python": {
        "label": "Python",
        "requirement": "3.11 及以上",
        "minimumVersion": "3.11",
        "probe": {"macos": "python3 --version", "windows": "py -3 --version"},
        "install": {"macos": "brew install python@3.12", "windows": "winget install --id Python.Python.3.12 -e"},
    },
    "node": {
        "label": "Node.js",
        "requirement": "22.0 及以上",
        "minimumVersion": "22.0",
        "probe": {"macos": "node --version", "windows": "node --version"},
        "install": {"macos": "brew install node@22", "windows": "winget install --id OpenJS.NodeJS.LTS -e"},
    },
    "go": {
        "label": "Go",
        "requirement": "1.21 及以上",
        "minimumVersion": "1.21",
        "probe": {"macos": "go version", "windows": "go version"},
        "install": {"macos": "brew install go", "windows": "winget install --id GoLang.Go -e"},
    },
}


GIT_PRESET: dict[str, Any] = {
    "label": "Git",
    "probe": {"macos": "git --version", "windows": "git --version"},
    "install": {
        "macos": "brew install git；没有 Homebrew 就用 xcode-select --install",
        "windows": "winget install --id Git.Git -e",
    },
}


def environment_selection_of(value: Any) -> list[dict[str, Any]]:
    """把前端偏好里选中的环境标识翻成「名称 + 版本要求 + 分系统的检测/安装命令」。

    预设项的版本要求由桥接决定，自定义项照抄用户填的原文——用户自己写的东西
    没有可校验的版本下限，也没法预判它在 macOS 和 Windows 上各叫什么，交给执行器按字面理解。
    """
    if value is None:
        return []
    if not isinstance(value, list):
        raise BridgeFailure("预设环境必须是数组")
    if len(value) > MAX_ENVIRONMENT_SETUP_ITEMS:
        raise BridgeFailure(f"预设环境最多 {MAX_ENVIRONMENT_SETUP_ITEMS} 项")
    selected: list[dict[str, Any]] = []
    for raw in value:
        name = str(raw or "").strip()
        if not name:
            continue
        if len(name) > 64:
            raise BridgeFailure("单个预设环境不能超过 64 个字符")
        preset = ENVIRONMENT_PRESETS.get(name.lower())
        entry = (
            {
                "id": name.lower(),
                "label": preset["label"],
                "requirement": preset["requirement"],
                "minimumVersion": preset["minimumVersion"],
                "probe": dict(preset["probe"]),
                "install": dict(preset["install"]),
            }
            if preset
            else {"id": name, "label": name, "requirement": "", "probe": {}, "install": {}}
        )
        if entry not in selected:
            selected.append(entry)
    return selected


def validate_environment_setup_payload(value: Any) -> tuple[int, str, str, bool, bool, list[dict[str, Any]], str, str, bool]:
    if not isinstance(value, dict):
        raise BridgeFailure("请求体必须是 JSON 对象")
    message = str(value.get("message") or "").strip()
    if len(message) > 32 * 1024:
        raise BridgeFailure("消息不能超过 32KB")
    thread_id = str(value.get("threadId") or "").strip()
    if len(thread_id) > 255:
        raise BridgeFailure("会话标识无效")
    use_git = value.get("useGit", False)
    if not isinstance(use_git, bool):
        raise BridgeFailure("是否使用 Git 必须是布尔值")
    environments = environment_selection_of(value.get("environments"))
    if not use_git and not environments and not message:
        raise BridgeFailure("请先在高级设置里选择要预设的环境")
    model = str(value.get("model") or "").strip()
    if len(model) > 128:
        raise BridgeFailure("模型标识不能超过 128 个字符")
    provider = ai_provider_of(value)
    return (
        GLOBAL_ENVIRONMENT_SETUP_PROGRAM_ID,
        message,
        thread_id,
        bool(value.get("newConversation")),
        use_git,
        environments,
        model,
        reasoning_effort_of(value, provider),
        fast_mode_of(value, provider),
    )


def environment_command_for(entry: dict[str, Any], field: str, host: str) -> str:
    """取某项环境在本机系统上的检测 / 安装命令，自定义项没写就返回空串。"""
    value = entry.get(field)
    if not isinstance(value, dict):
        return ""
    # Linux 没有逐项写死命令，回落到 macOS 那条，让执行器自己换成 apt / yum。
    return str(value.get(host) or value.get("macos") or "").strip()


VERSION_RE = re.compile(r"(?<!\d)(\d+(?:\.\d+)+)")


def version_at_least(version: str, minimum: str) -> bool:
    """比较由固定探测命令返回的数字版本，不接受任意命令或版本表达式。"""
    actual = tuple(int(part) for part in version.split("."))
    expected = tuple(int(part) for part in minimum.split("."))
    length = max(len(actual), len(expected))
    return actual + (0,) * (length - len(actual)) >= expected + (0,) * (length - len(expected))


def environment_probe_status(entry: dict[str, Any], host: str = "") -> dict[str, Any]:
    """执行预设的只读版本命令；绿色状态只给已安装且版本达标的项。"""
    host = host or hostinfo.host_platform()
    probe = environment_command_for(entry, "probe", host)
    result = {
        "id": str(entry.get("id") or ""),
        "installed": False,
        "version": "",
    }
    if result["id"] == "__git__":
        result.update(github_ssh_key_status())
    if not probe:
        return result
    try:
        completed = subprocess.run(
            shlex.split(probe, posix=host != "windows"),
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return result
    if completed.returncode != 0:
        return result
    version_match = VERSION_RE.search(completed.stdout or "")
    version = version_match.group(1) if version_match else ""
    minimum = str(entry.get("minimumVersion") or "")
    result["version"] = version
    result["installed"] = not minimum or bool(version and version_at_least(version, minimum))
    return result


def environment_probe_statuses(use_git: bool, environments: list[dict[str, Any]], host: str = "") -> list[dict[str, Any]]:
    """只检查前端当前列出的预设项；自定义项没有固定命令，不进行臆测。"""
    entries = list(environments)
    if use_git:
        entries.insert(0, {"id": "__git__", **GIT_PRESET})
    return [environment_probe_status(entry, host) for entry in entries]
