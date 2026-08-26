#!/usr/bin/env python3
"""Loopback HTTP bridge that starts one persisted Codex thread per delivery task."""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import mimetypes
import os
import platform
import queue
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse
from urllib.request import Request, urlopen

import server as planner
from delivery_bridge.update_manager import PluginUpdateManager, UpdateFailure
from delivery_bridge.versioning import compare_versions, manifest_version


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
PLUGIN_MANIFEST_PATH = Path(__file__).resolve().parent / ".codex-plugin" / "plugin.json"
PLUGIN_ROOT = Path(__file__).resolve().parent
# 执行器一律走这个命令行入口写任务面板。
TASKBOARD_CLI = str(Path(__file__).resolve().parent / "taskboard.py")


def taskboard_command(action: str) -> str:
    return f'python3 "{TASKBOARD_CLI}" {action}'

PLUGIN_GITHUB_REPOSITORY = "https://github.com/xiaolifeidao-fly/delivery-task-planner.git"
PLUGIN_GITHUB_RAW_BASE_URL = "https://raw.githubusercontent.com/xiaolifeidao-fly/delivery-task-planner"
PLUGIN_VERSION_CHECK_CACHE_SECONDS = 60
PLUGIN_UPDATE_RESTART_POLL_SECONDS = 2
# This value intentionally lives in the running Python process. Change it in a
# later release to verify that silent installation restarted the bridge and
# loaded the new code instead of only replacing files on disk.
PLUGIN_RUNTIME_TEST_VALUE = "delivery-task-planner-python-runtime-v6"
SESSION_STATUS = {"completed": "completed", "failed": "blocked", "interrupted": "blocked"}
TERMINAL_TURN_STATUSES = set(SESSION_STATUS)

PLUGIN_UPDATES: PluginUpdateManager


def default_runtime_dir() -> Path:
    configured = os.environ.get("DELIVERY_TASK_PLANNER_RUNTIME_DIR", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    if sys.platform == "win32" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "delivery-task-planner"
    return Path.home() / ".local" / "state" / "delivery-task-planner"


def plugin_version_from_manifest(path: Path) -> str:
    try:
        return manifest_version(path)
    except ValueError as exc:
        raise BridgeFailure(str(exc)) from exc


def installed_plugin_version() -> str:
    return plugin_version_from_manifest(PLUGIN_MANIFEST_PATH)


def compare_plugin_versions(left: str, right: str) -> int:
    return compare_versions(left, right)


def remote_plugin_default_branch() -> str:
    try:
        return PLUGIN_UPDATES._resolve_remote().get("branch", "main")
    except (UpdateFailure, NameError):
        return "main"


def fetch_remote_plugin_version() -> str:
    try:
        return PLUGIN_UPDATES._resolve_remote(force=True)["version"]
    except UpdateFailure as exc:
        raise BridgeFailure(str(exc)) from exc


def cached_remote_plugin_version() -> str:
    try:
        return PLUGIN_UPDATES._resolve_remote()["version"]
    except UpdateFailure as exc:
        raise BridgeFailure(str(exc)) from exc


def plugin_update_status() -> dict[str, Any]:
    checked_at = int(time.time())
    try:
        local_version = installed_plugin_version()
    except BridgeFailure as exc:
        return {
            "localVersion": "",
            "remoteVersion": "",
            "updateAvailable": False,
            "checkedAt": checked_at,
            "message": str(exc),
        }
    try:
        remote_version = cached_remote_plugin_version()
        update_available = compare_plugin_versions(remote_version, local_version) > 0
    except (BridgeFailure, ValueError) as exc:
        return {
            "localVersion": local_version,
            "remoteVersion": "",
            "updateAvailable": False,
            "checkedAt": checked_at,
            "message": str(exc),
        }
    result = {
        "localVersion": local_version,
        "remoteVersion": remote_version,
        "updateAvailable": update_available,
        "checkedAt": checked_at,
        "message": "",
    }
    try:
        result["installation"] = PLUGIN_UPDATES.get_job()
    except (UpdateFailure, NameError):
        result["installation"] = None
    return result


RUNTIME_DIR = default_runtime_dir()
PLUGIN_UPDATES = PluginUpdateManager(
    Path(__file__).resolve().parent,
    RUNTIME_DIR,
    PLUGIN_GITHUB_REPOSITORY,
    PLUGIN_GITHUB_RAW_BASE_URL,
    PLUGIN_VERSION_CHECK_CACHE_SECONDS,
)
PENDING_SESSION_SYNCS_PATH = RUNTIME_DIR / "pending-session-syncs.json"
# Claude 是 print 模式的一次性子进程，没有常驻线程服务可读；会话记录只能自己落盘。
CLAUDE_TRANSCRIPTS_DIR = RUNTIME_DIR / "claude-transcripts"
MAX_CLAUDE_TRANSCRIPT_TURNS = 60
# Codex 的 `thread/read` / `thread/resume` 只持久化部分条目：命令执行、文件改动和推理
# 摘要都读不回来（协议里对 thread/rollback 的说明写得很明确）。桌面版能显示完整过程，
# 靠的是自己留着实时通知流，这里做同一件事：把 item/started、item/completed 落盘。
CODEX_THREAD_ITEMS_DIR = RUNTIME_DIR / "codex-thread-items"
MAX_THREAD_JOURNAL_TURNS = 60
MAX_THREAD_JOURNAL_ITEMS = 400
# turn/start 的 summary 取值：auto / concise / detailed / none。auto 由模型自己决定详略，
# 实测常常只给一两句。注意 app-server 会静默忽略不认识的字段，改名字前先实际验一遍。
TURN_REASONING_SUMMARY = "detailed"
# app-server 的 stderr 只在出问题时才有内容（MCP 启动超时、模型列表拉取失败之类），
# 留最后这几行就够定位，全部堆在内存里没有意义。
APP_SERVER_STDERR_TAIL = 40
MAX_CONVERSATIONS_PER_TASK = 12
MAX_CONVERSATION_ATTACHMENTS = 5
MAX_CONVERSATION_REFERENCES = 16
MAX_CONVERSATION_ATTACHMENT_BYTES = 20 * 1024 * 1024
MAX_CONVERSATION_UPLOAD_BYTES = MAX_CONVERSATION_ATTACHMENTS * MAX_CONVERSATION_ATTACHMENT_BYTES + 128 * 1024
MAX_REQUIREMENT_DOCUMENT_BYTES = 2 * 1024 * 1024
MAX_REQUIREMENT_PROTOTYPE_FILES = 30
MAX_REQUIREMENT_PROTOTYPE_FILE_BYTES = 2 * 1024 * 1024
MAX_REQUIREMENT_PROTOTYPE_TOTAL_BYTES = 8 * 1024 * 1024
# 每一轮结束后，把面板可见的聊天正文备份到当前项目工作目录。它是可随项目
# 共享的人工可读副本，不替代 Codex / Claude 保留的原始执行器会话。
CHAT_ARCHIVE_DIRECTORY_NAME = "chat"
# New archives are grouped by the owning requirement, so requirement and task
# conversations for the same delivery stay together in a repository.
CHAT_ARCHIVE_REQUIREMENTS_DIRECTORY_NAME = "requirements"
CHAT_ARCHIVE_TASK_DIRECTORY_NAME = "task"
# 早期版本使用大写目录；只用于恢复既有记录，所有新归档一律写入 chat/。
LEGACY_CHAT_ARCHIVE_DIRECTORY_NAME = "Chat"
CHAT_ARCHIVE_MAX_NAME_BYTES = 96
CHAT_ARCHIVE_MAX_THREAD_ID_BYTES = 72
CHAT_ARCHIVE_MAX_FILES_TO_SCAN = 500
CHAT_ARCHIVE_MAX_FILE_BYTES = 5 * 1024 * 1024
# 云端同步与 Git 本地归档相互独立：Git 聊天同步开关决定 chat/ 是否落到工作目录，云端开关决定
# 是否将用户明确选择的内容传给服务端。服务端也会复核这组类别，桥接不能绕过项目设置。
CLOUD_SYNC_SCOPES = {"chat", "requirement", "design"}
MAX_CLOUD_SYNC_FILE_BYTES = 8 * 1024 * 1024
MAX_CLOUD_SYNC_FILES_PER_RUN = 500
# 需求拆解沉淀下来的需求大纲：每条需求一份，落在该需求的文档目录里。
REQUIREMENT_OUTLINE_FILE_NAME = "需求大纲.md"
MAX_REQUIREMENT_OUTLINE_BYTES = 2 * 1024 * 1024
# 面板直接编辑大纲时走 POST，请求体本身限制在 64KB 级别，这里留出同量级的正文上限。
MAX_EDITABLE_OUTLINE_BYTES = 512 * 1024
MAX_WORKSPACE_ARTIFACT_BYTES = 50 * 1024 * 1024
# 回复正文里的文件链接常常只写文件名（`ShenShiAccessibilityService.kt`），
# 按工作区根目录拼出来的路径并不存在，只能靠一份文件名索引反查真实路径。
WORKSPACE_FILE_INDEX_TTL_SECONDS = 30
MAX_WORKSPACE_FILE_INDEX_ENTRIES = 40000
# 需求大纲、任务文档、设计文档、测试用例这几个栏目都从「一个固定文件」升级成「一个目录里的多份文档」：
# 目录里所有可读的 Markdown、纯文本和 HTML 文档都能在面板上选择预览，原来的固定文件名继续作为默认主文档，存量数据不受影响。
DOCUMENT_SET_SUFFIXES = {".md", ".markdown", ".txt", ".html", ".htm"}
MAX_DOCUMENT_SET_FILES = 200
MAX_DOCUMENT_SET_FILE_BYTES = 2 * 1024 * 1024
# 面板还可以直接往栏目目录里放文档（本地选文件或粘贴正文）：什么后缀都收得下，
# 但只有文本类文档能在面板里预览编辑，其余的走附件预览与下载。
MAX_DOCUMENT_UPLOAD_FILES = 10
MAX_DOCUMENT_UPLOAD_FILE_BYTES = 20 * 1024 * 1024
MAX_DOCUMENT_UPLOAD_BYTES = MAX_DOCUMENT_UPLOAD_FILES * MAX_DOCUMENT_UPLOAD_FILE_BYTES + 128 * 1024
# 测试技能把一条需求或一条任务的全部测试资产写在 doc/test/<键>/ 下。
TESTING_ASSET_ROOT = "test"
TESTING_CASES_FILE_NAME = "测试用例.md"
PLANNING_ITEM_KEY = "__project_planning__"
GIT_ENVIRONMENT_SESSIONS_PATH = RUNTIME_DIR / "git-environment-sessions.json"
MAX_GIT_ENVIRONMENT_CONVERSATIONS = 12
# 项目偏好设置「高级设置 → 预设环境」的聊天：装的是本机全局环境，不挂在任何业务仓库上。
ENVIRONMENT_SETUP_ITEM_KEY = "__environment_setup__"
ENVIRONMENT_SETUP_SESSIONS_PATH = RUNTIME_DIR / "environment-setup-sessions.json"
MAX_ENVIRONMENT_SETUP_CONVERSATIONS = 12
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
GITHUB_SSH_HOST = "github.com"
GITHUB_SSH_KEY_NAME = "id_ed25519_github_delivery_task_planner"
GITHUB_SSH_CONFIG_START = "# >>> delivery-task-planner GitHub SSH key >>>"
GITHUB_SSH_CONFIG_END = "# <<< delivery-task-planner GitHub SSH key <<<"
GITHUB_SSH_CONFIG_BLOCK_RE = re.compile(
    rf"(?ms)^{re.escape(GITHUB_SSH_CONFIG_START)}\n.*?^{re.escape(GITHUB_SSH_CONFIG_END)}\n?",
)
SSH_PUBLIC_KEY_RE = re.compile(
    r"^(?:ssh-(?:ed25519|rsa|dss)|ecdsa-sha2-nistp(?:256|384|521)|sk-(?:ssh-ed25519|ecdsa-sha2-nistp256)@openssh\\.com)\s+[A-Za-z0-9+/=]+(?:\s+.*)?$",
)
REQUIREMENT_TESTING_ITEM_KEY = "__requirement_testing__"
# 任务生命周期的四个技能都在本插件 skills/ 下；执行时按阶段点名，别让执行器自己猜。
PLANNING_SKILL = "delivery-task-planner"
PHASE_SKILLS = {
    "requirement": "delivery-requirement-grooming",
    "development": "delivery-action-execution",
    "testing": "delivery-testing-report",
}
MAX_PLANNING_CONVERSATIONS = 12
# 拆解上下文只给当前需求：项目里其他需求的任务清单不再逐轮塞进提示词，
# 既省上下文，也避免执行器拿无关需求的任务去做去重和依赖判断。
REQUIREMENT_SCOPE_RULE = (
    "上面只列出当前需求下的任务。项目里其他需求的任务不在本轮上下文中，"
    "去重、复用和依赖判断都只在本需求范围内进行；需要参考其他需求或任务时，"
    "只能用用户在需求详情或聊天里 @ 引用并单独给出的那些，不要自行假设项目里还存在哪些任务。"
)
ATTACHMENT_DIRECTORY_NAME = "delivery-task-attachments"
ARTIFACT_DIRECTORY_NAME = "delivery-task-artifacts"
ATTACHMENT_MARKER_RE = re.compile(r"<!-- delivery-task-attachments:([A-Za-z0-9_-]+(?:,[A-Za-z0-9_-]+)*) -->")
ATTACHMENT_CONTEXT_RE = re.compile(r"\n?<delivery-task-attachments>.*?</delivery-task-attachments>", re.DOTALL)
# 真正发给执行器的提示词里裹着一大段面板上下文，聊天记录里只留用户自己写的那几句。
# planning 是需求拆解会话的旧标记名，历史会话里还在，两个都要认。
BRIDGE_CONTEXT_TAG = "delivery-bridge-context"
BRIDGE_CONTEXT_RE = re.compile(
    r"\n?<delivery-(?:bridge|planning)-context>.*?</delivery-(?:bridge|planning)-context>\n?",
    re.DOTALL,
)
IMAGE_SUFFIXES = {".gif", ".jpeg", ".jpg", ".png", ".webp"}
HTML_SUFFIXES = {".html", ".htm"}
# HTML 文档和原型页经常把样式、脚本拆成同目录的独立文件，但预览是把正文塞进 blob 地址的 iframe，
# 相对路径在那里解析不出来。读 HTML 时顺带把它引用到的同目录 css/js 一起带上，前端预览时内联进去。
HTML_ASSET_SUFFIXES = {".css", ".js", ".mjs"}
MAX_HTML_ASSET_FILES = 40
MAX_HTML_ASSET_TOTAL_BYTES = 4 * 1024 * 1024
HTML_ASSET_REFERENCE_RE = re.compile(
    r"""(?:<link\b[^>]*?\bhref|<script\b[^>]*?\bsrc)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))""",
    re.IGNORECASE,
)
MARKDOWN_ARTIFACT_RE = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
EXCLUDED_ARTIFACT_PARTS = {".codex", ".git"}
EXCLUDED_ARTIFACT_NAMES = {".env", ".env.local", ".env.production", "credentials.json", "secrets.json"}
RUNTIME_CONFIG_KEY = "_deliveryRuntimeConfig"
AI_PROVIDERS = {"codex", "claude"}
CODEX_MODEL_CATALOG = [
    {"model": "gpt-5.6-sol", "displayName": "5.6 Sol", "description": ""},
    {"model": "gpt-5.6-terra", "displayName": "5.6 Terra", "description": ""},
    {"model": "gpt-5.6-luna", "displayName": "5.6 Luna", "description": ""},
]
CODEX_REASONING_EFFORTS = {"minimal", "low", "medium", "high", "xhigh"}
CLAUDE_REASONING_EFFORTS = {"minimal", "low", "medium", "high", "max"}
DEFAULT_BIZ_LINE = ""
CODEX_GLOBAL_STATE_PATH = Path.home() / ".codex" / ".codex-global-state.json"
CODEX_DESKTOP_RESOURCE_COMPANIONS = ("codex-code-mode-host",)


class BridgeFailure(Exception):
    pass


# Capture the manifest version exactly once for the lifetime of this Python
# process. Package replacement changes the file on disk, but this value only
# changes after the bridge has genuinely restarted and imported the new code.
try:
    PLUGIN_RUNTIME_VERSION = installed_plugin_version()
except BridgeFailure:
    PLUGIN_RUNTIME_VERSION = ""


def content_disposition_of(name: str, inline: bool = False) -> str:
    """Build a browser-safe Content-Disposition header for arbitrary file names."""
    cleaned = re.sub(r"[\r\n\"]", "", str(name)).strip() or "attachment"
    suffix = Path(cleaned).suffix
    safe_suffix = suffix if re.fullmatch(r"\.[A-Za-z0-9]{1,16}", suffix) else ""
    ascii_stem = re.sub(r"[^A-Za-z0-9_-]", "_", Path(cleaned).stem.encode("ascii", "ignore").decode("ascii")).strip("_-")
    fallback = f"{ascii_stem or 'download'}{safe_suffix}"
    disposition = "inline" if inline else "attachment"
    encoded = quote(cleaned, safe="!#$&+-.^_`|~")
    return f"{disposition}; filename=\"{fallback}\"; filename*=UTF-8''{encoded}"


def create_http_server(
    host: str,
    port: int,
    workspace: Path,
    allowed_origins: set[str],
) -> ThreadingHTTPServer:
    """Create the loopback bridge listener for browser calls over local HTTP."""
    httpd = ThreadingHTTPServer((host, port), BridgeHandler)
    httpd.bridge = ExecutionBridge(workspace)  # type: ignore[attr-defined]
    httpd.allowed_origins = allowed_origins  # type: ignore[attr-defined]
    return httpd


def schedule_bridge_restart() -> None:
    """Hand restart ownership to a detached helper so this request can finish."""
    helper = Path(__file__).resolve().parent / "delivery_bridge" / "restart_helper.py"
    restart_log = RUNTIME_DIR / "restart-helper.log"
    restart_log.parent.mkdir(parents=True, exist_ok=True)
    with restart_log.open("a", encoding="utf-8") as output:
        subprocess.Popen(
            [
                sys.executable,
                str(helper),
                "--pid",
                str(os.getpid()),
                "--plugin-root",
                str(Path(__file__).resolve().parent),
                *sys.argv[1:],
            ],
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=output,
            start_new_session=sys.platform != "win32",
            creationflags=(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)) if sys.platform == "win32" else 0,
            close_fds=True,
        )


def complete_plugin_update_in_background(job_id: str, bridge: Any) -> None:
    """Restart after replacement once every bridge-managed run has finished."""
    def monitor() -> None:
        while True:
            try:
                job = PLUGIN_UPDATES.get_job(job_id)
            except UpdateFailure:
                return
            status = str(job.get("status") or "")
            if status in {"completed", "failed", "restarting"}:
                return
            if status == "restart_required":
                active_runs = bridge.active_run_count()
                if active_runs > 0:
                    try:
                        PLUGIN_UPDATES.mark_waiting_for_runs(job_id, active_runs)
                    except UpdateFailure:
                        return
                    time.sleep(PLUGIN_UPDATE_RESTART_POLL_SECONDS)
                    continue
                try:
                    PLUGIN_UPDATES.mark_restarting(job_id)
                except UpdateFailure:
                    return
                schedule_bridge_restart()
                return
            time.sleep(PLUGIN_UPDATE_RESTART_POLL_SECONDS)

    threading.Thread(target=monitor, daemon=True, name=f"plugin-update-{job_id[:8]}").start()


def ai_provider_of(value: Any) -> str:
    provider = str((value or {}).get("provider") or "codex").strip().lower() if isinstance(value, dict) else str(value or "codex").strip().lower()
    if provider not in AI_PROVIDERS:
        raise BridgeFailure("AI 工具必须是 codex 或 claude")
    return provider


def provider_label(provider: str) -> str:
    return "Claude" if provider == "claude" else "Codex"


def executor_type_of(value: Any) -> str:
    """会话目录记录里的执行器类型，形如 codex / claude-prototype / codex-testing-cases。"""
    if isinstance(value, dict):
        return str(value.get("executorType") or "").strip().lower()
    return str(value or "").strip().lower()


def executor_provider_of(value: Any, fallback: str = "codex") -> str:
    """这条会话是哪个 AI 工具留下的；读不出来就按调用方当前选的工具兜底。"""
    head = executor_type_of(value).split("-", 1)[0]
    return head if head in AI_PROVIDERS else ai_provider_of(fallback)


def executor_purpose_of(value: Any) -> str:
    """执行器类型里的用途后缀，拆解会话为空、原型是 prototype，跨工具列目录时只比这一段。"""
    parts = executor_type_of(value).split("-", 1)
    return parts[1] if len(parts) > 1 else ""


def same_executor_purpose(row: Any, executor_type: str) -> bool:
    return executor_purpose_of(row) == executor_purpose_of(executor_type)


def reasoning_effort_of(value: Any, provider: str = "codex") -> str:
    effort = str((value or {}).get("reasoningEffort") or "").strip() if isinstance(value, dict) else str(value or "").strip()
    allowed = CLAUDE_REASONING_EFFORTS if provider == "claude" else CODEX_REASONING_EFFORTS
    if effort and effort not in allowed:
        raise BridgeFailure(f"{provider_label(provider)} 推理强度无效")
    return effort


def fast_mode_of(value: Any, provider: str = "codex") -> bool:
    if provider != "claude":
        return False
    raw = (value or {}).get("fastMode", False) if isinstance(value, dict) else value
    if not isinstance(raw, bool):
        raise BridgeFailure("Claude 快速模式必须是布尔值")
    return raw


def program_id_of(value: Any, label: str = "项目标识") -> int:
    if isinstance(value, bool):
        raise BridgeFailure(f"{label}必须是项目表的数值主键")
    try:
        program_id = int(str(value).strip())
    except (TypeError, ValueError):
        raise BridgeFailure(f"{label}必须是项目表的数值主键") from None
    if program_id <= 0:
        raise BridgeFailure(f"{label}必须是项目表的正整数主键")
    return program_id


def placeholder_workspace() -> Path:
    """An empty, neutral directory to hold the process-level slot when no workspace is pinned.

    进程启动时不该假定自己属于哪个项目。以前这里落的是安装目录的上级（正好是插件所在的仓库），
    于是那个仓库会悄悄变成"看起来合法"的默认工作目录。现在换成运行时目录下的空目录：
    请求带了 workspace 就按项目路由，没带就在 workspace_path_of 里直接报错，不会误伤到任何真实仓库。
    """
    root = RUNTIME_DIR / "no-workspace"
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


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


def codex_cli_name(host: str = "") -> str:
    return "codex.exe" if (host or host_platform()) == "windows" else "codex"


def codex_cli_cache_path(host: str = "", runtime_dir: Path | None = None) -> Path:
    return (runtime_dir or RUNTIME_DIR) / "bin" / codex_cli_name(host)


def codex_desktop_resource_paths(host: str = "") -> list[Path]:
    """Known Codex Desktop resource locations, ordered by the per-user install first."""
    system = host or host_platform()
    executable = codex_cli_name(system)
    if system == "windows":
        local_app_data = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
        program_files = Path(os.environ.get("ProgramFiles") or r"C:\Program Files")
        roots = [
            local_app_data / "Programs" / "Codex",
            local_app_data / "Programs" / "Codex Desktop",
            local_app_data / "Codex",
            local_app_data / "Codex Desktop",
            program_files / "Codex",
            program_files / "Codex Desktop",
        ]
        return [root / "resources" / executable for root in roots]
    if system == "macos":
        roots = [
            Path.home() / "Applications" / "Codex.app",
            Path("/Applications/Codex.app"),
            Path.home() / "Applications" / "ChatGPT.app",
            Path("/Applications/ChatGPT.app"),
        ]
        return [root / "Contents" / "Resources" / executable for root in roots]
    return []


WINDOWS_CLI_WRAPPER_SUFFIXES = ("", ".cmd", ".bat", ".ps1")


def path_codex_cli(host: str = "") -> tuple[str, str]:
    """PATH 上的 codex，拆成「能直接起进程的可执行文件」和「只能兜底的包装脚本」。

    Windows 上 npm 全局安装和 Codex Desktop 都会往 PATH 里塞 `codex.cmd` / `codex.ps1`
    这类包装脚本，CreateProcess 起不动它们（WinError 193），所以宁可退回到本地复制出来的
    codex.exe；实在什么都没有时再拿包装脚本兜底，好过直接报"未找到 CLI"。
    """
    command = shutil.which("codex") or ""
    if not command:
        return "", ""
    if (host or host_platform()) == "windows" and Path(command).suffix.lower() in WINDOWS_CLI_WRAPPER_SUFFIXES:
        return "", command
    return command, ""


CODEX_CLI_VERSIONS: dict[str, str] = {}


def codex_cli_version(command: str) -> str:
    """`codex --version` 的版本号，按可执行文件缓存；问不出来就返回空串。"""
    if command in CODEX_CLI_VERSIONS:
        return CODEX_CLI_VERSIONS[command]
    version = ""
    try:
        result = subprocess.run([command, "--version"], capture_output=True, text=True, timeout=15)
        version = (result.stdout or result.stderr or "").strip().split()[-1] if result.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError, IndexError):
        version = ""
    CODEX_CLI_VERSIONS[command] = version
    return version


def newest_codex_cli(candidates: list[str]) -> str:
    """在几个都能用的 codex 里挑版本最高的那个，问不出版本时保持传入顺序。

    Codex Desktop 自带的 codex 和它捆绑的 MCP 服务（node_repl、browser-use、computer-use）
    是配套发版的：实测 PATH 上的 0.134.0 调 node_repl 会被拒（`sandboxCwd must be an
    absolute file URI`），换成桌面端自带的 0.149.0-alpha，同一段代码同一组参数就能跑通。
    所以这里不能只看"PATH 上有没有 codex"，还得看它够不够新。
    """
    available = [candidate for candidate in candidates if candidate]
    if not available:
        return ""
    best = available[0]
    for candidate in available[1:]:
        if newer_codex_cli(candidate, best):
            best = candidate
    return best


def newer_codex_cli(candidate: str, current: str) -> bool:
    """版本问不出来或者格式不认识，就当它不比现有的新，保持原来的优先级。"""
    candidate_version = codex_cli_version(candidate)
    current_version = codex_cli_version(current)
    if not candidate_version:
        return False
    if not current_version:
        return True
    try:
        return compare_versions(candidate_version, current_version) > 0
    except ValueError:
        return False


def codex_cli_candidates(host: str = "", runtime_dir: Path | None = None) -> list[str]:
    """本机所有可直接拉起的 codex，按原来的优先级排列。"""
    cache_path = codex_cli_cache_path(host, runtime_dir)
    command, _ = path_codex_cli(host)
    candidates = [command, str(cache_path) if cache_path.is_file() else ""]
    candidates.extend(str(path) for path in codex_desktop_resource_paths(host) if path.is_file())
    return [candidate for candidate in candidates if candidate]


def available_codex_cli(host: str = "", runtime_dir: Path | None = None) -> str:
    """Locate a PATH CLI, a previously copied local CLI, or a Desktop resource."""
    cache_path = codex_cli_cache_path(host, runtime_dir)
    # 已经复制到运行时目录的 codex.exe 是确定能跑的，Windows 上优先用它。
    if (host or host_platform()) == "windows" and cache_path.is_file():
        return str(cache_path)
    _, wrapper = path_codex_cli(host)
    return newest_codex_cli(codex_cli_candidates(host, runtime_dir)) or wrapper


def provision_codex_cli(host: str = "", runtime_dir: Path | None = None) -> str:
    """Copy Codex Desktop's bundled CLI locally when it is the newest one on this machine."""
    cache_path = codex_cli_cache_path(host, runtime_dir)
    # 与 available_codex_cli 保持同一优先级，避免"检测到的"和"真正拉起的"不是同一个。
    if (host or host_platform()) == "windows" and cache_path.is_file():
        return str(cache_path)
    command, wrapper = path_codex_cli(host)
    chosen = newest_codex_cli(codex_cli_candidates(host, runtime_dir))
    desktop_paths = {str(path) for path in codex_desktop_resource_paths(host)}
    if chosen and chosen not in desktop_paths:
        return chosen
    source = Path(chosen) if chosen else next((path for path in codex_desktop_resource_paths(host) if path.is_file()), None)
    if source is None:
        return command or wrapper
    # 桌面端资源目录里的可执行文件本来就能直接跑，而且它的同目录伴生文件都在。
    # 复制只是 Windows 那边为了绕开包装脚本才需要的，别为此搬运一个几百 MB 的二进制。
    if (host or host_platform()) != "windows":
        return str(source)
    if cache_path.is_file() and cache_path.stat().st_size == source.stat().st_size:
        return str(cache_path)
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, cache_path)
        source_suffix = ".exe" if source.suffix.lower() == ".exe" else ""
        for companion in CODEX_DESKTOP_RESOURCE_COMPANIONS:
            companion_source = source.with_name(f"{companion}{source_suffix}")
            if companion_source.is_file():
                shutil.copy2(companion_source, cache_path.with_name(companion_source.name))
        # 复制过来的是另一个版本，之前问出来的版本号不能再算数。
        CODEX_CLI_VERSIONS.pop(str(cache_path), None)
        return str(cache_path)
    except OSError:
        # Desktop resources are directly executable too. Keep the board usable
        # when a restrictive filesystem prevents creating the local copy.
        return str(source)


def environment_setup_workspace() -> Path:
    """「预设环境」的专用工作目录。

    装 Python / Node / Go 走的是本机全局包管理器，和项目代码没有关系，
    所以和初始化 Git 环境一样给一个运行时目录下的空目录当 cwd，别把安装痕迹落进业务仓库。
    """
    root = RUNTIME_DIR / "environment-setup"
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def workspace_path_of(value: Any) -> Path:
    raw = str(value or "").strip()
    if not raw:
        raise BridgeFailure("未提供 Codex 工作目录，请先在项目管理中确认当前项目的工作目录")
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        raise BridgeFailure("Codex 工作目录必须是绝对路径")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise BridgeFailure(f"Codex 工作目录不存在：{candidate}") from exc
    if not resolved.is_dir():
        raise BridgeFailure(f"Codex 工作目录不是目录：{resolved}")
    return resolved


def codex_local_projects() -> list[dict[str, Any]]:
    if not CODEX_GLOBAL_STATE_PATH.exists():
        return []
    try:
        state = json.loads(CODEX_GLOBAL_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BridgeFailure(f"无法读取 Codex 本地项目：{exc}") from exc
    projects = state.get("local-projects") if isinstance(state, dict) else None
    if not isinstance(projects, dict):
        return []
    result: list[dict[str, Any]] = []
    for project_id, value in projects.items():
        if not isinstance(value, dict):
            continue
        roots = []
        for raw_root in value.get("rootPaths") or []:
            try:
                root = workspace_path_of(raw_root)
            except BridgeFailure:
                continue
            roots.append(str(root))
        name = str(value.get("name") or "").strip()
        if name and roots:
            result.append({"id": str(value.get("id") or project_id), "name": name, "rootPaths": roots})
    return sorted(result, key=lambda item: (item["name"].casefold(), item["id"]))


def image_format(data: bytes) -> tuple[str, str]:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", ".jpg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif", ".gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", ".webp"
    return "", ""


def generated_image_from_event(message: dict[str, Any]) -> tuple[str, str] | None:
    """Extract a generated image from either rollout events or app-server notifications."""
    candidates: list[dict[str, Any]] = [message]
    while candidates:
        value = candidates.pop()
        event_type = str(value.get("type") or value.get("method") or "")
        call_id = str(value.get("call_id") or value.get("callId") or "")
        result = value.get("result")
        image_result = result if isinstance(result, str) else value.get("image") or value.get("data")
        normalized_type = event_type.replace("/", "_").replace("-", "_").lower()
        if (
            ("image_generation" in normalized_type or "imagegeneration" in normalized_type)
            and call_id
            and isinstance(image_result, str)
            and image_result
        ):
            return call_id, image_result
        for nested in value.values():
            if isinstance(nested, dict):
                candidates.append(nested)
    return None


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


ENVIRONMENT_SETUP_SESSIONS = GitEnvironmentSessionStore(ENVIRONMENT_SETUP_SESSIONS_PATH)


def reasoning_summary_text(item: Any) -> str:
    """Return only the safe, user-visible summary from a Codex reasoning item.

    The app-server can emit a separate ``reasoning/textDelta`` stream, but that is
    not the display summary and must never be copied into the task board or its
    project-local chat archive. ``summary`` is the protocol field intended for
    explaining the model's reasoning to people.
    """
    if not isinstance(item, dict):
        return ""
    summary = item.get("summary")
    if isinstance(summary, str):
        return summary.strip()
    if not isinstance(summary, list):
        return ""
    parts = [part.strip() for part in summary if isinstance(part, str) and part.strip()]
    return "\n\n".join(parts)


def progress_event_of(message: dict[str, Any]) -> tuple[str, str, str, str] | None:
    method = str(message.get("method") or "")
    params = message.get("params") or {}
    if method == "turn/started":
        return "status", "任务已开始", "Codex 正在分析任务与项目上下文。", "running"
    if method == "turn/completed":
        status = str((params.get("turn") or {}).get("status") or "completed")
        return "status", "正在同步执行结果", f"Codex 回合状态：{status}", "running"
    # Stream the app-server's display-safe reasoning summary. Deliberately do
    # not handle item/reasoning/textDelta: that is not the summary API and is
    # not eligible for task-board storage.
    if method == "item/reasoning/summaryTextDelta":
        delta = str(params.get("delta") or "").strip()
        return ("reasoning", "Codex 推理摘要", delta, "running") if delta else None
    if method == "item/reasoning/summaryPartAdded":
        return "reasoning", "Codex 正在生成推理摘要", "", "running"
    if method not in {"item/started", "item/completed"}:
        return None
    item = params.get("item") or {}
    item_type = str(item.get("type") or "")
    completed = method == "item/completed"
    status = "success" if completed else "running"
    if item_type == "agentMessage" and completed:
        text = str(item.get("text") or item.get("content") or "").strip()
        return ("message", "Codex 进度", text, status) if text else None
    if item_type == "reasoning":
        summary = reasoning_summary_text(item)
        if summary and completed:
            return "reasoning", "Codex 推理摘要", summary, status
        return "reasoning", "Codex 正在生成推理摘要", "", status
    if item_type == "commandExecution":
        command = item.get("command") or item.get("commands") or ""
        if isinstance(command, list):
            command = "\n".join(str(part) for part in command)
        if completed:
            exit_code = item.get("exitCode")
            detail = "命令执行完成" if exit_code in (None, 0) else f"命令执行失败，退出码 {exit_code}"
            return "command", detail, str(command), "success" if exit_code in (None, 0) else "failed"
        return "command", "正在执行命令", str(command), status
    if item_type in {"fileChange", "fileEdit"}:
        return "file", "正在更新项目文件" if not completed else "项目文件已更新", "", status
    if item_type in {"mcpToolCall", "dynamicToolCall"}:
        tool = str(item.get("tool") or item.get("name") or "工具")
        return "tool", f"{'完成' if completed else '调用'} {tool}", "", status
    return None


def wrap_bridge_context(context_lines: list[str], spoken: str) -> str:
    """Put the board's assembled context behind a marker and leave the user's own words after it.

    面板会往提示词里塞项目、任务、阶段、技能一大堆上下文；那是给执行器看的，
    聊天记录里只该回显 `spoken`，也就是用户自己写的内容。
    """
    # 只带附件不写字也是一次有效的输入，补一句可见文案：空文本的条目会被整条丢掉。
    text = spoken.strip() or "请查看随附文件并继续处理。"
    return "\n".join([f"<{BRIDGE_CONTEXT_TAG}>", *context_lines, f"</{BRIDGE_CONTEXT_TAG}>", "", text])


def with_mention_context(message: str, mention_context: list[str]) -> str:
    """Wrap @-selected entities for an in-flight or follow-up turn only when needed."""
    return wrap_bridge_context(mention_context, message) if mention_context else message


def workspace_instruction(workspace: Path | None) -> str:
    """Point every phase at the project's bound working directory and its own dev skills.

    四个阶段（拆解、梳理、执行、测试）都得先看真实代码：面板返回的结构化上下文里没有工程现状，
    不点名工作目录和项目技能，执行器就会照着业务名词泛化出一套和仓库对不上的东西。
    """
    if not workspace:
        return "项目工作目录: 未提供。动手前先向用户确认代码仓库位置，不要拿当前目录或安装目录顶替。"
    return (
        f"项目工作目录（项目管理里为本项目绑定的代码仓库，也是本轮 cwd）: {workspace}。"
        "开始前先加载该目录下项目自己的开发技能（如 backend-development、web-development），"
        "并读相关目录和现有实现；结论要落在真实文件路径上，不要凭业务名词推演。"
    )


def document_path_of(task: dict[str, Any]) -> str:
    """任务需求文档在工作区里的相对路径；面板没给就按 doc/<模块>/<任务键>/文档.md 兜底。"""
    explicit = str(task.get("requirementDocumentPath") or "").strip()
    if explicit:
        return explicit
    return f"doc/{task.get('moduleKey') or 'module'}/{task.get('itemKey') or 'item'}/文档.md"


def document_revision_rule(document_path: str) -> str:
    """需求文档是跨回合累积的文档，追加需求时最容易被整段覆盖成只剩本轮内容。"""
    return (
        f"`{document_path}` 是跨回合累积的文档，不是本轮回复的存档。要改它就必须："
        "先把现有内容完整读一遍，再把本轮新增或调整的部分合并进去，最后整篇写回同一路径；"
        "本轮没有讨论到的章节原样保留，只有用户明确要求删除的内容才能删。"
        "禁止只把本轮追加的需求写进文件，那会把之前几轮的需求文档整段丢掉。"
    )


def follow_up_context_lines(task: dict[str, Any]) -> list[str]:
    """续聊回合也要带上任务、阶段和文档纪律：首轮提示词可能已经被会话压缩掉了。"""
    phase = str(task.get("phase") or "requirement")
    lines = [
        "这是同一条任务上的追加回合，任务和当前阶段都没有变化。",
        f"任务键: {task.get('itemKey') or '未指定'}",
        f"当前执行阶段: {phase}（对应技能：{PHASE_SKILLS.get(phase, '按任务当前阶段处理')}）",
    ]
    # 面板没给出文档路径也没给模块时，document_path_of 只能兜出一个 doc/module/... 的假路径；
    # 那会把执行器引到错误的文件上，不如不提，让它沿用本会话里已经拿到的路径。
    if str(task.get("requirementDocumentPath") or "").strip() or str(task.get("moduleKey") or "").strip():
        document_path = document_path_of(task)
        lines.extend([
            f"需求文档路径: {document_path}（本任务唯一的需求文档）",
            document_revision_rule(document_path),
        ])
    return lines


def prototype_directory_of(task: dict[str, Any]) -> str:
    """Return the fixed task-local directory for generated prototype images."""
    document_path = Path(document_path_of(task))
    return (document_path.parent / "prototype").as_posix()


def readable_document(workspace: Path | None, relative: str) -> bool:
    """文档是否真的落盘了。没写过的任务不该出现在清单里，否则执行器会去读一堆不存在的路径。"""
    if not workspace or not relative:
        return False
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts:
        return False
    try:
        return (workspace / candidate).resolve().is_file()
    except OSError:
        return False


def requirement_document_catalog(
    items: list[Any],
    task: dict[str, Any],
    workspace: Path | None,
    limit: int = 60,
) -> list[str]:
    """List the sibling tasks under the same requirement whose documents are already written.

    只给清单不给正文：一条需求可能拆出几十个任务，把每份文档都塞进提示词会挤掉真正要干的活，
    也会把上下文烧在无关任务上。执行器按标题和依赖关系判断相关性，需要哪份自己去读哪份。
    """
    requirement_key = str(task.get("requirementKey") or "").strip()
    if not requirement_key:
        return []
    current_key = str(task.get("itemKey") or "")
    dependencies = {str(key) for key in task.get("dependsOnItemKeys") or []}
    lines: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        item_key = str(item.get("itemKey") or "")
        if not item_key or item_key == current_key:
            continue
        if str(item.get("requirementKey") or "").strip() != requirement_key:
            continue
        path = document_path_of(item)
        if not readable_document(workspace, path):
            continue
        marks = ["前置依赖"] if item_key in dependencies else []
        if str(item.get("status") or "") == "done":
            marks.append("已完成")
        suffix = f"（{'、'.join(marks)}）" if marks else ""
        lines.append(f"- {item_key}: {item.get('title') or item_key}{suffix} → {path}")
        if len(lines) >= limit:
            break
    return lines


def sibling_document_lines(catalog: Any) -> list[str]:
    """把同需求的文档清单渲染成提示词片段，并交代按需加载的规则。"""
    entries = [str(line) for line in catalog or [] if str(line).strip()]
    if not entries:
        return []
    return [
        "",
        "本需求下其他任务已写好的需求文档（按需加载，不是让你全读）:",
        *entries,
        "加载规则：先看标题和依赖判断相关性——与本任务有接口、数据结构、字段口径或前置产出关系的才打开；"
        "无关的不要读，避免上下文被无关任务占满。读过哪几份、为什么读，在最终回复里说明。",
    ]


def git_branch_lines(branch: str) -> list[str]:
    """需求启用了 Git 分支时，明确告诉执行器改动应留在这条分支上。"""
    if not branch:
        return []
    return [
        f"Git 需求分支: {branch}（工作目录已切到该分支）。本任务的所有改动都留在这条分支上，"
        "不要切换分支、不要合并回主干，也不要执行 push。",
    ]


def build_task_prompt(payload: dict[str, Any], workspace: Path | None = None) -> str:
    """`workspace` 是项目管理里绑定的工作目录，也是本轮 cwd；四个阶段都要靠它去读代码和项目技能。"""
    task = payload["task"]
    dependencies = task.get("dependsOnItemKeys") or []
    phase = str(task.get("phase") or "requirement")
    phase_name = {"requirement": "梳理需求", "development": "动作执行", "testing": "成品测试"}.get(phase, phase)
    document_path = document_path_of(task)
    document_directory = Path(document_path).parent.as_posix()
    design_directory = (Path(document_path).parent / "design").as_posix()
    prototype_directory = prototype_directory_of(task)
    test_artifact_directory = Path("doc") / "test" / str(task.get("itemKey") or "task")
    # 每个阶段各有一个技能，明确点名让执行器去加载，别让它自己猜「当前项目的 skill」是哪个。
    phase_instruction = {
        "requirement": (
            f"本次只进行梳理需求：遵循 {PHASE_SKILLS['requirement']} 技能，创建或更新工作区中的 `{document_path}`。"
            "每次后续会话都会从这个文件读取需求上下文；文档结论必须基于工作目录里的真实代码，不要凭业务名词推演。"
        ),
        "development": (
            f"本次只进行动作执行：遵循 {PHASE_SKILLS['development']} 技能，先读取 `{document_path}`，"
            "再按需求文档和当前项目的开发技能实现并交付产物。"
        ),
        "testing": (
            f"本次只进行成品测试：遵循 {PHASE_SKILLS['testing']} 技能，先读取 `{document_path}`，"
            f"再读取已有 `{test_artifact_directory / '测试用例.md'}`（不存在时说明缺口并补充最小用例），"
            "先准备环境、账号、鉴权和测试数据，再按代码与业务依赖编排实测；"
            f"验证命令沿用当前项目开发技能里的约定；所有测试资产必须写入 `{test_artifact_directory}/`，该目录支持多份文档；"
            "并生成带明确验收判定的测试报告。"
        ),
    }.get(phase, "按任务当前阶段执行。")
    prototype_instruction = (
        [
            "这是需求拆解自动追加的原型图生成任务，不能只写文字说明："
            f"使用可用的图像生成能力产出真实原型图，并保存至少一张 PNG、JPG、WEBP 或 GIF 到 {prototype_directory}/。",
            "原型图应基于本任务、需求文档和全部前置任务产物；完成后在最终回复中列出图片的工作区相对路径。",
            "图片是本任务文档的附属材料，不改业务代码；目录中存在图片后，任务详情会提供“打开原型图目录”按钮。",
        ]
        if bool(task.get("prototypeTask")) else []
    )
    lines = [
        f"执行下面这个交付任务的「{phase_name}」阶段。直接检查当前项目并完成真实工作，不要只给方案。",
        workspace_instruction(workspace),
        "该任务已由 HTTP 执行桥领取并绑定到当前会话。不要调用 claim_next_task、bind_task_execution_session、finish_execution_task 或其他任务状态流转工具；桥接器会根据本回合最终状态自动同步任务面板。",
        f"项目 program_id: {payload['programId']}",
        f"任务键: {task['itemKey']}",
        f"标题: {task['title']}",
        f"说明: {task.get('description') or '无'}",
        f"需求文档路径: {document_path}（本任务唯一的需求文档；默认加载：开始前先完整读一遍）",
        f"任务需求文档目录: `{document_directory}/`，支持多份文档；`文档.md` 是主文档，独立任务说明使用独立文件名写在此目录。",
        f"任务设计文档目录: `{design_directory}/`，支持多份文档；需要交付独立设计说明时写入此目录，不要写入 `.codex/visualizations` 或其他工作区外路径。",
        document_revision_rule(document_path),
        phase_instruction,
        *prototype_instruction,
        f"阶段: {task.get('stageKey') or '未指定'}",
        f"模块: {task.get('moduleKey') or '未指定'}",
        f"前置任务: {', '.join(dependencies) if dependencies else '无'}",
        *git_branch_lines(str(payload.get("gitBranch") or "")),
        "完成后说明修改内容和验证结果；无法完成时明确说明阻塞原因。",
        "如果生成了用户需要查看或下载的文件、文档或图片，请在最终回复中用 Markdown 链接列出其工作区相对路径。",
    ]
    if bool(payload.get("batchMode")):
        lines.extend(
            [
                "这是批量执行队列中的一项任务。完成时在最终回复最后单独输出一行："
                "`批量判定：完成`、`批量判定：可忽略` 或 `批量判定：需人工处理`。",
                "只有短暂的连接/会话中断，或不影响交付物的命令、验收提示噪声，才能判定为可忽略；"
                "代码、编译、测试、权限、依赖、数据或实际实现问题必须判定为需人工处理，并说明原因。",
            ]
        )
    lines.extend(sibling_document_lines(payload.get("requirementDocuments")))
    execution_constraints = str(payload.get("executionConstraints") or "").strip()
    if execution_constraints:
        lines.extend(["", "本次队列的前置任务约束条件说明:", execution_constraints])
    mention_context = payload.get("conversationMentionContext") or []
    if isinstance(mention_context, list):
        lines.extend(str(line) for line in mention_context if isinstance(line, str) and line.strip())
    follow_up = str(payload.get("followUp") or "").strip()
    if follow_up:
        lines.append("本上下文标记闭合之后的内容，是用户本轮追加的原话。")
    # 面板组装的这一大段只给执行器看；聊天记录里留一句人话，外加用户自己写的追加要求。
    spoken = f"执行「{phase_name}」阶段：{task['title']}"
    return wrap_bridge_context(lines, f"{spoken}\n\n{follow_up}" if follow_up else spoken)


def build_task_testing_cases_prompt(
    program_id: int, task: dict[str, Any], context: dict[str, Any], message: str, workspace: Path | None = None,
    follow_up: bool = False,
) -> str:
    """Build a design-only prompt that remains safe while development is in progress."""
    item_key = str(task.get("itemKey") or "").strip()
    if not item_key:
        raise BridgeFailure("任务测试用例缺少任务标识")
    if follow_up:
        # 追加回合不重发技能说明和同需求文档清单，只留红线、会变的任务状态和输出格式。
        return wrap_bridge_context(
            [
                "这是同一条任务测试用例会话的追加回合，模式没变：只设计用例，"
                "绝不调用接口、UI、脚本或构建命令执行真实测试，不得输出验收判定、不得创建测试报告、不得修改业务实现或任务状态。",
                "首轮已交代过技能、同需求文档清单和输出要求，这里不再重复，按本会话已确认的约定继续。",
                workspace_instruction(workspace),
                f"项目 program_id: {program_id}",
                f"任务键 item_key: {item_key}",
                f"当前阶段（仅供了解，不可改变）: {task.get('phase') or 'requirement'}/{task.get('status') or 'todo'}",
                f"任务需求文档: {document_path_of(task)}",
                f"已知动作执行产物: {'有' if task.get('actionOutput') else '无'}",
                f"测试用例资产目录: doc/test/{item_key}/；必须写入测试用例.md，按需写入测试计划.md 或其他补充文档。",
                "研发未完成的部分仍然列为执行前置或待补输入，不得猜造结果。",
                "最终回复第一行必须是“测试用例已生成”，后面给出测试准备、用例表、执行顺序和待确认项。",
                "本会话如果被压缩过，先读上面给出的任务需求文档和已生成的测试用例把上下文接回来，不要凭印象往下接。",
                "本上下文标记闭合之后的内容，是用户额外补充的测试范围、环境、账号来源或数据要求。",
            ],
            message or "请继续完善本任务的测试用例。",
        )
    return wrap_bridge_context(
        [
            "这是交付任务面板的「预先生成测试用例」回合。遵循 delivery-testing-report 技能的测试用例设计模式。",
            "本回合只读取需求、关联任务、代码和已有产物，设计测试范围、输入数据、依赖顺序、步骤、预期和证据。",
            "绝不调用接口、UI、脚本或构建命令执行真实测试；不得输出验收判定、不得创建测试报告、不得修改业务实现或任务状态。",
            workspace_instruction(workspace),
            f"项目 program_id: {program_id}",
            f"任务键 item_key: {item_key}",
            f"任务名称: {task.get('title') or item_key}",
            f"当前阶段（仅供了解，不可改变）: {task.get('phase') or 'requirement'}/{task.get('status') or 'todo'}",
            f"任务需求文档: {document_path_of(task)}",
            f"已知动作执行产物: {'有' if task.get('actionOutput') else '无'}",
            f"测试用例资产目录: doc/test/{item_key}/；该目录支持多份文档，必须写入测试用例.md，按需写入测试计划.md 或其他补充文档。",
            "研发未完成的部分必须列为执行前置或待补输入，不得猜造结果。",
            *sibling_document_lines(requirement_document_catalog(context.get('items') or [], task, workspace)),
            "最终回复第一行必须是“测试用例已生成”，后面给出测试准备、用例表、执行顺序和待确认项。",
            "本上下文标记闭合之后的内容，是用户额外补充的测试范围、环境、账号来源或数据要求。",
        ],
        message or "请根据当前任务预先生成可执行测试用例，等待后续明确指令后再执行真实测试。",
    )


def build_conversation_prompt(
    program_id: int,
    task: dict[str, Any],
    message: str,
    workspace: Path | None = None,
    requirement_documents: list[str] | None = None,
    mention_context: list[str] | None = None,
) -> str:
    """Start an independent Codex thread with enough task context to be useful."""
    dependencies = task.get("dependsOnItemKeys") or []
    phase = str(task.get("phase") or "requirement")
    document_path = document_path_of(task)
    document_directory = Path(document_path).parent.as_posix()
    design_directory = (Path(document_path).parent / "design").as_posix()
    return wrap_bridge_context(
        [
            "这是交付任务详情中发起的一条新 Codex 对话。请结合当前项目和任务上下文回应并执行用户的要求。",
            workspace_instruction(workspace),
            "该任务已由 HTTP 执行桥领取并绑定到当前会话。不要调用 claim_next_task、bind_task_execution_session、finish_execution_task 或其他任务状态流转工具；桥接器会根据本回合最终状态自动同步任务面板。",
            f"项目 program_id: {program_id}",
            f"任务键: {task.get('itemKey') or '未指定'}",
            f"任务标题: {task.get('title') or '未指定'}",
            f"任务说明: {task.get('description') or '无'}",
            f"当前执行阶段: {phase}",
            f"当前阶段对应技能: {PHASE_SKILLS.get(phase, '按任务当前阶段处理')}",
            f"需求文档路径: {document_path}（本任务唯一的需求文档，默认加载）。开始前请先读取此文件；梳理需求阶段应在此基础上更新。",
            f"任务需求文档目录: `{document_directory}/`，支持多份文档；`文档.md` 是主文档，独立任务说明使用独立文件名写在此目录。",
            f"任务设计文档目录: `{design_directory}/`，支持多份文档；需要交付独立设计说明时写入此目录，不要写入 `.codex/visualizations` 或其他工作区外路径。",
            document_revision_rule(document_path),
            f"阶段: {task.get('stageKey') or '未指定'}",
            f"模块: {task.get('moduleKey') or '未指定'}",
            f"前置任务: {', '.join(dependencies) if dependencies else '无'}",
            *sibling_document_lines(requirement_documents),
            *(mention_context or []),
            "如果生成了用户需要查看或下载的文件、文档或图片，请在最终回复中用 Markdown 链接列出其工作区相对路径。",
            "本上下文标记闭合之后的内容，是用户本轮输入的原文。",
        ],
        message,
    )


def planning_temp_segment(value: str, fallback: str) -> str:
    """Return one readable, traversal-safe directory segment for planning drafts."""
    candidate = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(value or "").strip())
    candidate = re.sub(r"\s+", " ", candidate).strip(" .")
    return (candidate or fallback)[:80]


def planning_temp_document_path(requirement_name: str, requirement_key: str, thread_id: str) -> Path:
    """Return the plugin-local draft file for one requirement chat window."""
    requirement_segment = planning_temp_segment(requirement_name, requirement_key or "未命名需求")
    thread_segment = planning_temp_segment(thread_id, "待分配聊天窗口")
    return PLUGIN_ROOT / ".temp" / "requirements" / f"req_{requirement_segment}" / thread_segment / "temp.md"


def write_planning_temp_summary(
    path: Path,
    requirement_name: str,
    requirement_key: str,
    thread_id: str,
    user_message: str,
    summary: str,
) -> None:
    """Atomically refresh the latest process artifact for one planning chat."""
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join([
        "# 需求梳理过程摘要",
        "",
        "> 临时过程产物，仅用于接续本聊天窗口；不等同于最终需求文档。",
        "",
        f"- 需求名称：{requirement_name or requirement_key or '未命名需求'}",
        f"- 需求键：{requirement_key or '未指定'}",
        f"- 聊天窗口 ID：{thread_id}",
        f"- 更新时间：{utc_now()}",
        "",
        "## 本轮用户输入",
        "",
        (user_message or "（无文字输入）").strip(),
        "",
        "## 最新总结性产物",
        "",
        (summary or "（本轮没有可保存的总结）").strip(),
        "",
    ])
    temporary = path.with_suffix(".tmp")
    temporary.write_text(body, encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def delete_planning_temp_summary(path: Path) -> bool:
    """Delete only one managed planning draft after a successful confirmation."""
    managed_root = (PLUGIN_ROOT / ".temp" / "requirements").resolve()
    candidate = path.resolve(strict=False)
    try:
        candidate.relative_to(managed_root)
    except ValueError as exc:
        raise BridgeFailure("需求过程摘要路径超出插件临时目录") from exc
    if not path.is_file():
        return False
    path.unlink()
    return True


def planning_temp_rule_lines(temp_path: str, read_required: bool = False) -> list[str]:
    if not temp_path:
        return [
            "本轮过程总结由桥接器在聊天窗口建立后自动写入插件安装目录的 `.temp/requirements/req_<需求名称>/<聊天窗口ID>/temp.md`；"
            "不要为保存过程数据而创建或修改正式需求大纲。",
        ]
    read_rule = (
        "本轮是确认写入回合：写最终需求文档前必须完整读取这份过程总结。"
        if read_required
        else "正常连续对话直接使用当前聊天上下文，不要重复读取这个文件；只有会话被压缩、恢复后上下文缺失，"
        "或当前上下文已找不到此前确认结论时，才读取它作为恢复点。"
    )
    return [
        f"本聊天窗口的过程总结文件: `{temp_path}`（插件安装目录内，不属于项目正式交付文档）。",
        read_rule,
        "每轮结束后桥接器会用本轮输入和最新总结性产物整篇刷新它，"
        "不要把过程记录、未确认方案或聊天流水写入正式需求大纲。",
    ]


def requirement_outline_rule_lines(outline_path: str, write_allowed: bool = False, temp_path: str = "") -> list[str]:
    """Keep the final outline immutable until the board grants write confirmation."""
    if not outline_path:
        return planning_temp_rule_lines(temp_path)
    if not write_allowed:
        return [
            f"最终需求大纲: `{outline_path}`（相对项目工作目录）。预览和讨论阶段它是只读的最终产物：存在时可读取其中已确认内容，"
            "不存在时也不得创建；只有用户点击「确认并写入」后才能改变它。",
            *planning_temp_rule_lines(temp_path),
        ]
    return [
        f"最终需求大纲: `{outline_path}`（相对项目工作目录）。用户已点击「确认并写入」，本轮必须把最终确认结果写入这个文件。",
        *planning_temp_rule_lines(temp_path, read_required=True),
        "写入前先完整读取既有最终大纲（如存在）和本聊天窗口的过程总结，把用户最终确认的方案合并成一份完整需求产物；"
        "过程聊天、被否决方案和未确认草稿不要带入最终文档。",
        "`temp.md` 只是候选材料，禁止整段复制或按聊天顺序改写。先分析每条信息是否直接有助于需求目标、交付范围、实现约束、"
        "关键业务规则、验收标准、测试准备或最终决策；只把有实际交付价值且已经确认的内容提炼成清晰、可执行、可验证的需求表述。",
        "合并重复内容，删除寒暄、反复确认、讨论过程、未采纳备选、无结论推演、工具日志和关于聊天本身的元信息。"
        "但不得借精简遗漏已确认的非目标、兼容性要求、异常与边界场景、风险约束或仍会影响交付的待确认问题。",
        "写回是「读全文 → 合并本轮增量 → 整篇覆盖」：先完整读一遍现有大纲（用户可能在面板上直接编辑过），"
        "把本轮追加或调整的需求并进对应章节，本轮没聊到的章节原样保留，只有用户明确要求删除的内容才能删。"
        "禁止只把本轮追加的那段需求写进文件，那等于把之前几轮的需求大纲整段丢掉。",
        "大纲用 Markdown 组织，至少包含：需求背景与目标、范围与不做的事、关键约束、勘察到的落点（真实模块/目录/接口）、任务拆解表（与预览一致）、验收标准、待确认问题。",
        "把大纲里任务表的最终状态同步成实际落库的那一版。",
    ]


def requirement_document_rule_lines(requirement_key: str) -> list[str]:
    """Keep standalone requirement files in the requirement document directory."""
    if not requirement_key:
        return []
    document_directory = requirement_document_directory_of(requirement_key).as_posix()
    prototype_directory = requirement_prototype_directory_of(requirement_key).as_posix()
    testing_directory = testing_asset_directory_of(requirement_key).as_posix()
    return [
        f"需求文档目录: `{document_directory}/`。这是一个支持多份文档的目录，`需求大纲.md` 是主文档；用户明确要求独立流程图、图表、HTML 或其他文件时，"
        "不要把完整内容嵌进需求大纲，也不要只在对话里展示，必须使用独立文件名直接写入这个目录。",
        f"需求原型目录: `{prototype_directory}/`，支持多个独立 `.html` / `.htm` 页面；需求测试资产目录: `{testing_directory}/`，支持 `测试用例.md`、`测试计划.md`、`测试报告.md` 及其他补充文档。",
        "独立需求资产是项目交付文件，不是临时可视化：不得写入 `.codex/visualizations`、系统临时目录或其他工作区外路径。"
        "不要把 visualize 工具的默认输出路径当成交付路径；如果工具先生成了临时预览，必须把最终文件复制到上述需求目录后再回复。生成后在最终回复中只列工作区相对路径，确保面板能登记和预览它。",
        "未明确要求独立文件时不要创建额外文件；除当前需求文档目录外仍不得修改工作区其他文件。",
    ]


def build_planning_prompt(
    program_id: int,
    context: dict[str, Any],
    message: str,
    selected_stage: str = "",
    selected_module: str = "",
    selected_kind: str = "",
    requirement: dict[str, Any] | None = None,
    write_allowed: bool = False,
    workspace: Path | None = None,
    mention_context: list[str] | None = None,
    thread_id: str = "",
) -> str:
    """Give a project-level Codex turn the precise planner-tool contract and scope.

    需求梳理分两步：默认只出可评审的拆解预览（`write_allowed=False`），
    用户在面板上点「确认并写入」后才带着 `write_allowed=True` 再来一轮真正落库。
    面板上下文整段包在 <delivery-planning-context> 里，聊天记录只回显用户自己输入的内容。
    `workspace` 是项目管理里绑定的工作目录，也就是本轮的 cwd；写进提示词是为了让执行器
    知道该去哪儿读代码和项目技能，而不是只盯着任务面板返回的那点结构化上下文。
    """
    stage_lines = [
        f"- {item.get('stageKey')}: {item.get('tag') or item.get('title') or item.get('stageKey')}"
        for item in context.get("stages") or []
    ]
    module_lines = [
        f"- {item.get('moduleKey')}: {item.get('name') or item.get('moduleKey')}"
        for item in context.get("modules") or []
    ]
    requirement = requirement or {}
    requirement_key = str(requirement.get("requirementKey") or "")
    temp_path = (
        planning_temp_document_path(str(requirement.get("name") or ""), requirement_key, thread_id).as_posix()
        if thread_id else ""
    )
    # 同一条需求可能被反复追问，已经拆出来的任务要显式列出来：
    # 不给这份清单，第二轮会把第一轮建过的任务再建一遍。
    requirement_items = [
        item
        for item in context.get("items") or []
        if requirement_key and str(item.get("requirementKey") or "") == requirement_key
    ]
    requirement_item_lines = [
        f"- {item.get('itemKey')}: {item.get('title') or item.get('itemKey')}"
        f"（{item.get('phase') or '-'}/{item.get('status') or '-'}；收益：{'、'.join(item.get('benefitTags') or []) or '未标注'}）"
        for item in requirement_items[:100]
    ]
    mode_lines = (
        [
            f"本轮用户已在任务面板点击「确认并写入」，请遵循 {PLANNING_SKILL} 技能执行写入："
            f"把上一轮预览过的方案（含用户后续提出的修改）用 `{taskboard_command('create-task-board-tasks')}` 一次性提交。",
            f"任务面板数据只能通过这个命令行写入：`{taskboard_command('<动作>')}`（参数用连字符，数组参数走 `--json`，内容长时先写文件再 `--json @文件`）。不要自己拼 HTTP 请求、也不要手工改文件来创建任务面板数据。",
            "可用动作：get-task-board-context、create-task-board-stage、create-task-board-module、create-task-board-tasks；"
            f"`{taskboard_command('actions')}` 可以打印全部动作和参数，拿不准时先看它。"
            "当前项目已确定，所有动作的 --program-id 一律传下面给出的项目表数值主键，不要传项目名称或项目编码。",
            "任务描述应包含目标、范围和验收标准；依赖仅表达真正的前置关系，"
            "depends_on 只能引用本轮新建的任务或下方「本需求已建任务」里的任务键，不要跨需求建立依赖。",
            "每个任务必须传 benefit_tags：用 1-3 个不超过 32 字的简短标签描述该任务完成后带来的收益或作用，不能留空，也不要把任务标题重复写成标签。",
            "任务负责人由写入命令从下面这条需求的主负责人自动继承：任务模型只能保存一位负责人，因此会使用需求的第一位主负责人；不要在任务数组中自行改写负责人。",
            "执行 create-task-board-tasks 时必须原样传入下面给出的 requirement_key 和 phase，让新任务挂回本需求并落在指定的起始阶段。",
            "用户已选择里程碑或模块时，将相同的 stage_key/module_key 传给 create-task-board-tasks 并不要自行改写；未选择时根据当前项目已有选项为每项任务分配归属。",
            "本需求已有任务列表在下方给出：只补齐缺少的部分，不要重建已经存在的任务；若本轮无需新建任务，直接说明原因。",
            "不重复创建与本需求已有任务语义相同的任务。完成后用简洁中文总结实际创建的里程碑、模块和任务。",
        ]
        if write_allowed
        else [
            f"这是交付任务面板的需求梳理会话，请遵循 {PLANNING_SKILL} 技能。本轮只做梳理和预览，禁止写入任何任务面板数据。",
            "禁止执行 create-task-board-tasks、create-task-board-stage、create-task-board-module，也不要借 HTTP 请求或手工改文件绕过任务面板写入限制；未确认前这些写入命令会被命令行直接拒绝。",
            "本轮的限制只针对任务面板数据：正式需求大纲保持只读；如果用户明确要求生成或更新独立的流程图、图表、HTML 或其他需求资产，允许写入当前需求文档目录，但只能写该目录。过程总结由桥接器写入插件安装目录。已授予项目工作目录及需求指定关联目录的只读勘察权限；可使用终端的只读命令和当前会话可用的读取工具列目录、搜索并读取代码、配置、技能和文档。某个可选读取工具不可用时，改用其他可用的只读工具继续勘察，不要因此停止。",
            "拆解前必须先勘察下方给出的项目工作目录：加载该目录下项目自己的开发技能（如 backend-development、web-development），读相关目录和现有实现，据此判断需求真正的落点。get-task-board-context 只给出面板侧上下文，不包含工程现状，不能拿它替代看代码。",
            "任务要落到勘察出的真实模块、目录或接口上，不要只按业务名词泛化出通用分层；工作区里找不到需求所指的模块时，先向用户说明并确认工作目录或范围，不要硬拆。",
            "请与用户对话把需求问清楚，然后输出一份可评审的拆解预览：先用 Markdown 表格列出「序号 / 任务标题 / 收益标签 / 负责人 / 里程碑 / 模块 / 类型 / 前置依赖」，每项给 1-3 个简短收益或作用标签；负责人统一展示为该需求的第一位主负责人（未指定则标为未指派）；再在表格下方逐条补充目标、范围和验收标准。",
            "里程碑、模块、类型的取值只能来自下方给出的现有选项；预览里也要说明哪些是新建、哪些复用本需求已有任务。",
            "本需求已有任务列表在下方给出：预览里只列本轮打算新增的任务，不要重复已经存在的任务。",
            "回复结尾提示用户：确认无误后点击输入框旁的「确认并写入」按钮，需要调整就直接回复修改意见，本轮继续讨论不会写入任何数据。",
        ]
    )
    # 关掉「拆解成多条任务」时，整条需求只落一条任务：改动本来就不可分的小需求，拆开只会平添依赖和空跑。
    split_tasks = bool(requirement.get("splitTasks", True))
    split_lines = (
        []
        if split_tasks
        else (
            [
                "本需求已关闭「拆解成多条任务」：执行 create-task-board-tasks 时 tasks 数组只能包含一条覆盖整条需求的任务，"
                "该任务的 depends_on 传空数组；启用原型图时命令行自动追加的原型任务不计入这条限制。",
            ]
            if write_allowed
            else [
                "本需求已关闭「拆解成多条任务」：预览里只输出一条覆盖整条需求的任务，任务表只有一行，不要拆成多条，也不要用依赖把它串成多步。"
                "该任务的目标、范围和验收标准要覆盖整条需求。",
            ]
        )
    )
    prototype_enabled = bool(requirement.get("generatePrototype"))
    prototype_lines = (
        [
            "本需求已启用“拆解后生成原型图”。预览时必须在任务表的最后列出一条“生成需求原型图”任务，"
            "并说明它依赖本轮其余任务；确认写入时，执行 create-task-board-tasks 必须传 generate_prototype: true。"
            "命令行会自动创建并标识这条末尾任务，任务执行时将把图片保存到自身文档目录的 prototype/ 中。",
        ]
        if prototype_enabled else []
    )
    # 任务需求文档是任务级的唯一需求沉淀。单任务模式无需额外勾选预生成：
    # 唯一任务就是这条需求的交付载体，确认写入后必须直接收到完整需求文档。
    pre_generate_task_documents = bool(
        requirement.get("preGenerateTaskDocuments", requirement.get("generateTaskOutline", False))
    )
    task_document_required = pre_generate_task_documents or not split_tasks
    task_document_lines = (
        [
            "本需求已关闭“拆解成多条任务”：确认写入后，create-task-board-tasks 返回的唯一业务任务（prototypeTask=false）"
            "就是本条需求的交付载体。必须把本轮梳理出的完整需求文档直接创建或覆盖到该任务返回的 requirementDocumentPath"
            "（即 `doc/<moduleKey>/<itemKey>/文档.md`），不能只留在需求级大纲或任务数据库的简短说明中。",
            "这条规则不依赖“预生成任务需求文档”开关；若同时生成原型图，不要把需求正文写进 prototypeTask=true 的原型任务文档。",
            "正文需完整保留本轮已确认的需求背景与目标、范围与非目标、工程事实与落点、设计要求、验收标准、测试准备及待确认项；"
            "后续任务“梳理需求”和“动作执行”只读取并继续完善这一份文件。",
            "写完后在总结里列出唯一业务任务键和实际写入的需求文档路径。",
        ]
        if write_allowed and not split_tasks
        else [
            "本需求已启用“预生成任务需求文档”。create-task-board-tasks 返回每条任务的 moduleKey 和 itemKey 后，"
            "必须为本轮每条新建任务创建或覆盖 `doc/<moduleKey>/<itemKey>/文档.md`，一条任务一份，不能另建任务需求大纲。",
            "这份文件是后续任务“梳理需求”和“动作执行”共同读取的唯一需求文档；先写可实施初稿，"
            "再由梳理需求阶段基于真实代码增量校正和补全。",
            "初稿用 Markdown 组织，至少包含：任务目标、范围与不做的事、已知落点（真实模块/目录/接口）、实现要点、前置依赖、验收标准与待确认项。",
            "写完后在总结里列出实际写入的任务需求文档路径。",
        ]
        if write_allowed and pre_generate_task_documents
        else (
            [
                "本需求已关闭“拆解成多条任务”：确认写入后会为唯一业务任务直接写入完整需求文档；"
                "本轮仍处于预览，尚未取得任务键，先不要创建任务需求文档。",
            ]
            if not split_tasks
            else [
                "任务确认写入后，会为每条新建任务预生成 `doc/<moduleKey>/<itemKey>/文档.md` 作为需求梳理初稿；"
                "本轮只做预览，先不要创建这些文件。",
            ]
            if pre_generate_task_documents else []
        )
    )
    # 正式大纲只在确认轮更新；预览轮由插件安装目录里的聊天级 temp.md 接续上下文。
    outline_path = requirement_outline_path_of(requirement_key).as_posix() if requirement_key else ""
    outline_lines = requirement_outline_rule_lines(outline_path, write_allowed, temp_path)
    document_lines = requirement_document_rule_lines(requirement_key)
    # 被 @ 的历史需求：只给大纲产物地址，读不读、读哪一段由执行器按需决定。
    references = requirement.get("references") or []
    reference_lines = (
        [
            "本需求在详情里 @ 引用了下面这些历史需求。它们各自的需求大纲产物地址已列出（相对项目工作目录）：",
            *(
                f"- {item.get('name') or item.get('requirementKey')}"
                f"（requirement_key: {item.get('requirementKey')}）: "
                f"`{requirement_outline_path_of(str(item.get('requirementKey'))).as_posix()}`"
                for item in references
            ),
            "这些文件不会随提示词发给你：需要参考时按上面的路径自行读取，并且只读与本需求真正相关的章节，不要为了凑上下文把它们整段搬进回复。",
            "文件不存在说明那条需求还没沉淀大纲：如实说明，不要臆造它的内容。",
            "引用只作为背景和既有约定的来源，本轮拆解的范围仍然只限当前这条需求。",
        ]
        if references else []
    )
    # 被 @ 的既有任务从当前项目目录重新解析，不能采信浏览器提交的任务标题或文档路径。
    item_references = requirement.get("itemReferences") or []
    items_by_key = {
        str(item.get("itemKey") or ""): item
        for item in context.get("items") or []
        if isinstance(item, dict) and str(item.get("itemKey") or "")
    }
    item_reference_lines: list[str] = []
    if item_references:
        item_reference_lines.append("本需求在详情里 @ 引用了下面这些既有任务。需要参考时先读取对应任务需求文档：")
        for reference in item_references:
            item_key = str(reference.get("itemKey") or "")
            item = items_by_key.get(item_key)
            if item is None:
                item_reference_lines.append(f"- {item_key}：当前项目中已找不到该任务，不能据此推断实现细节。")
                continue
            item_reference_lines.append(
                f"- {item.get('title') or item_key}（item_key: {item_key}）: "
                f"`{document_path_of(item)}`"
            )
        item_reference_lines.extend([
            "这些任务仅作为已有实现和约定的参考，不能改变本轮拆解范围或重复创建同一项工作。",
            "任务文档不存在时如实说明，不要臆造其中的实现细节。",
        ])
    instruction = [
        *mode_lines,
        *split_lines,
        *prototype_lines,
        *outline_lines,
        *document_lines,
        *reference_lines,
        *item_reference_lines,
        *(mention_context or []),
        *task_document_lines,
        "",
        f"项目 program_id: {program_id}",
        f"项目名称（仅供理解，不要作为参数）: {context.get('program', {}).get('name') or program_id}",
        workspace_instruction(workspace),
        f"需求键 requirement_key: {requirement_key or '未指定'}",
        f"任务起始阶段 phase: {requirement.get('startPhase') or 'requirement'}",
        f"拆解成多条任务: {'是' if split_tasks else '否（只建一条任务）'}",
        f"预生成任务需求文档: {'是（单任务模式强制写入）' if not split_tasks else '是' if task_document_required else '否（由任务梳理阶段创建）'}",
        f"拆解后生成原型图: {'是' if prototype_enabled else '否'}",
        f"需求名称: {requirement.get('name') or '未命名'}",
        f"主负责人: {requirement.get('owners') or '未指定'}",
        f"辅助人: {requirement.get('assistants') or '未指定'}",
        f"@ 引用的历史需求: {'、'.join(str(item.get('name') or item.get('requirementKey')) for item in references) or '无'}",
        "需求详细信息:",
        str(requirement.get("detail") or "（未填写）"),
        "",
        f"已选里程碑: {selected_stage or '未选择'}",
        f"已选模块: {selected_module or '未选择'}",
        f"任务类型偏好: {selected_kind or '由你判断'}",
        "现有里程碑:", *(stage_lines or ["- 无"]),
        "现有模块:", *(module_lines or ["- 无"]),
        "本需求已建任务:", *(requirement_item_lines or ["- 无"]),
        REQUIREMENT_SCOPE_RULE,
        "",
        "本上下文标记闭合之后的内容，是用户本轮输入的原文。",
    ]
    return wrap_bridge_context(instruction, message)


def planning_detail_digest(requirement: dict[str, Any] | None) -> str:
    """需求正文的指纹。续聊回合靠它判断用户是不是在面板上改过需求详情。"""
    detail = str((requirement or {}).get("detail") or "")
    return hashlib.sha256(detail.encode("utf-8")).hexdigest()


# 新建聊天首回合结束后用一轮短会话补标题。标题既用于需求，也用于任务会话；
# 保持简短，才能在左侧会话列表完整辨认。
CONVERSATION_TITLE_TIMEOUT_SECONDS = 3 * 60
MAX_CONVERSATION_TITLE_CHARS = 30


def build_conversation_title_prompt(user_message: str, reply: str) -> str:
    """根据新聊天的首条用户消息和首轮回复，生成一个面板会话标题。

    命名是面板内务，不能出现在用户可见的正式聊天里，也不应让模型读取代码或执行命令。
    """
    return wrap_bridge_context(
        [
            "这是交付任务面板的「聊天自动命名」回合：请根据一条新聊天的首轮沟通内容起标题。",
            "",
            "用户的首条需求或任务说明:",
            (user_message or "").strip() or "（用户本轮没有文字输入）",
            "",
            "AI 首轮回复（可能很长，只取其中的主旨）:",
            (reply or "").strip()[:4000] or "（本轮没有回复正文）",
            "",
            "要求:",
            f"- 只输出标题本身，一行，不超过 {MAX_CONVERSATION_TITLE_CHARS} 个字，用中文。",
            "- 标题要说清本次要做的事，能在聊天列表里被一眼认出，不要写成「需求」「任务」「优化」这类空话。",
            "- 回复正文可能还没生成，这时只按用户的说明起名，不要等也不要追问。",
            "- 不要引号、句号、序号、Markdown 记号，不要任何解释或前后缀。",
            "- 不要读代码、不要执行命令、不要修改任何文件，也不要调用任务面板命令。",
        ],
        "请为这条聊天起一个标题，只回标题本身。",
    )


def conversation_title_of(text: str) -> str:
    """把命名回合的回复收敛成一行标题：模型偶尔会带上引号、前缀或多余的说明。"""
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    # 「标题如下：」这类引导行不是标题，丢掉之后再取第一行。
    lines = [line for line in lines if not line.endswith((":", "："))] or lines
    if not lines:
        return ""
    candidate = re.sub(r"^[#>*\-\d.、\s]+", "", lines[0])
    candidate = re.sub(r"^(?:标题|聊天标题|会话标题|需求标题|需求名称|任务标题)\s*[:：]\s*", "", candidate)
    candidate = candidate.strip("*_`\"'“”‘’「」《》【】 \t").strip()
    candidate = candidate.rstrip("。.！!")
    return candidate[:MAX_CONVERSATION_TITLE_CHARS]


# 需求名称留空时仍复用同一套标题生成和清洗规则；保留旧函数名，避免插件扩展脚本失效。
REQUIREMENT_NAME_TIMEOUT_SECONDS = CONVERSATION_TITLE_TIMEOUT_SECONDS
MAX_REQUIREMENT_NAME_CHARS = MAX_CONVERSATION_TITLE_CHARS


def build_requirement_name_prompt(user_message: str, reply: str) -> str:
    return build_conversation_title_prompt(user_message, reply)


def requirement_name_of(text: str) -> str:
    return conversation_title_of(text)


# 开聊那一刻先用首条消息的前几个字占住需求名称：起名要跑一轮模型，最快也要几秒，
# 这几秒里面板上只能显示需求编号，用户看着就像没生效。占位名等 AI 起好名再换掉。
MAX_REQUIREMENT_PLACEHOLDER_CHARS = 10


def placeholder_requirement_name(user_message: str) -> str:
    """用户首条消息的前几个字，去掉附件上下文和 Markdown 记号后取头一段。"""
    text = text_without_attachment_context(str(user_message or ""))
    text = re.sub(r"^[#>*\-\d.、\s]+", "", " ".join(text.split()))
    text = text.strip("*_`\"'“”‘’「」《》【】 \t")
    return text[:MAX_REQUIREMENT_PLACEHOLDER_CHARS].strip()


def build_planning_follow_up_prompt(
    program_id: int,
    context: dict[str, Any],
    message: str,
    selected_stage: str = "",
    selected_module: str = "",
    selected_kind: str = "",
    requirement: dict[str, Any] | None = None,
    workspace: Path | None = None,
    mention_context: list[str] | None = None,
    include_detail: bool = False,
    thread_id: str = "",
) -> str:
    """同一条需求拆解会话的追加回合，只带会变的和丢不起的那几项。

    首轮的角色说明、勘察纪律、文档目录纪律和现有里程碑/模块明细不再逐轮重发；这里保留三类：
    随轮次变化的（已选里程碑/模块、改动过的需求正文、本轮 @ 的实体）、
    被会话压缩掉就会出事的（本需求已建任务清单、需求大纲读写纪律），
    以及取值必须合法的现有里程碑/模块键。确认写入那一轮仍走 build_planning_prompt 全量。
    """
    requirement = requirement or {}
    requirement_key = str(requirement.get("requirementKey") or "")
    requirement_item_lines = [
        f"- {item.get('itemKey')}: {item.get('title') or item.get('itemKey')}"
        f"（{item.get('phase') or '-'}/{item.get('status') or '-'}；收益：{'、'.join(item.get('benefitTags') or []) or '未标注'}）"
        for item in context.get("items") or []
        if requirement_key and str(item.get("requirementKey") or "") == requirement_key
    ][:100]
    stage_keys = [str(item.get("stageKey") or "") for item in context.get("stages") or [] if item.get("stageKey")]
    module_keys = [str(item.get("moduleKey") or "") for item in context.get("modules") or [] if item.get("moduleKey")]
    split_tasks = bool(requirement.get("splitTasks", True))
    outline_path = requirement_outline_path_of(requirement_key).as_posix() if requirement_key else ""
    document_directory = requirement_document_directory_of(requirement_key).as_posix() if requirement_key else ""
    temp_path = (
        planning_temp_document_path(str(requirement.get("name") or ""), requirement_key, thread_id)
        if thread_id else None
    )
    # 正式大纲在预览期不落盘；由聊天级 temp.md 接管压缩后的上下文。
    temp_written = bool(temp_path and temp_path.is_file())
    detail_lines = (
        [
            "需求详细信息（用户已在面板上改过，以这一版为准）:"
            if include_detail
            else "需求详细信息（聊天过程摘要尚未落盘，这里重发一份，避免会话压缩后丢失）:",
            str(requirement.get("detail") or "（未填写）"),
        ]
        if include_detail or not temp_written
        else [
            "需求详细信息与本会话首轮给出的一致，没有变化；"
            f"若上下文里已经找不到，就去读 `{temp_path.as_posix()}` 里的上一轮总结。",
        ]
    )
    prototype_lines = (
        [
            "本需求已启用“拆解后生成原型图”：预览的任务表最后必须固定列出一条“生成需求原型图”任务，"
            "并说明它依赖本轮其余任务。",
        ]
        if bool(requirement.get("generatePrototype")) else []
    )
    document_lines = (
        [
            f"需求文档目录: `{document_directory}/`（`需求大纲.md` 是主文档）。用户要独立流程图、图表、HTML 等资产时，"
            "写成该目录下的独立文件，不要写进 `.codex/visualizations`、临时目录或工作区外路径；除该目录外不修改工作区其他文件。",
        ]
        if document_directory else []
    )
    lines = [
        "这是同一条需求拆解会话的追加回合：需求和梳理模式都没有变化，本轮仍然只做梳理和预览，"
        "禁止执行 create-task-board-tasks、create-task-board-stage、create-task-board-module，也不要绕过任务面板写入限制。",
        "首轮已经交代过角色、技能、勘察纪律和输出格式，这里不再重复，按本会话已确认的约定继续；"
        "拆解结论仍然要落在工作目录里的真实模块、目录或接口上。",
        workspace_instruction(workspace),
        f"项目 program_id: {program_id}",
        f"需求键 requirement_key: {requirement_key or '未指定'}",
        f"需求名称: {requirement.get('name') or '未命名'}",
        f"拆解成多条任务: {'是' if split_tasks else '否（预览里只输出一条覆盖整条需求的任务，不要拆分也不要串依赖）'}",
        *detail_lines,
        f"已选里程碑: {selected_stage or '未选择'}",
        f"已选模块: {selected_module or '未选择'}",
        f"任务类型偏好: {selected_kind or '由你判断'}",
        f"现有里程碑键（取值只能从中选）: {'、'.join(stage_keys) or '无'}",
        f"现有模块键（取值只能从中选）: {'、'.join(module_keys) or '无'}",
        *requirement_outline_rule_lines(outline_path, False, temp_path.as_posix() if temp_path else ""),
        *document_lines,
        *prototype_lines,
        *(mention_context or []),
        "本需求已建任务:",
        *(requirement_item_lines or ["- 无"]),
        REQUIREMENT_SCOPE_RULE,
        "预览里只列本轮打算新增的任务，不要重复上面已经存在的任务；"
        "输出格式沿用首轮：先用 Markdown 表格列出「序号 / 任务标题 / 收益标签 / 负责人 / 里程碑 / 模块 / 类型 / 前置依赖」，"
        "再在表格下方逐条补充目标、范围和验收标准；"
        "回复结尾照旧提示用户：确认无误后点「确认并写入」，要调整就直接回复修改意见。",
        (
            f"本会话如果被压缩过，上面提到的需求背景和既往结论你在上下文里可能已经找不到："
            f"先读 `{temp_path.as_posix()}` 把上下文接回来再动手，不要凭印象往下接。"
            if temp_path
            else "本会话如果被压缩过，先向用户确认需求背景和既往结论，不要凭印象往下接。"
        ),
        "本上下文标记闭合之后的内容，是用户本轮输入的原文。",
    ]
    return wrap_bridge_context(lines, message)


def build_requirement_testing_prompt(
    program_id: int,
    context: dict[str, Any],
    requirement: dict[str, Any],
    message: str,
    workspace: Path | None = None,
    test_case_only: bool = False,
    follow_up: bool = False,
    include_detail: bool = False,
) -> str:
    """Give the requirement-level testing skill one requirement and its real task inventory."""
    requirement_key = str(requirement.get("requirementKey") or "").strip()
    requirement_items = [
        item for item in context.get("items") or []
        if str(item.get("requirementKey") or "") == requirement_key
    ]
    item_lines = [
        f"- {item.get('itemKey')}: {item.get('title') or item.get('itemKey')}"
        f"（{item.get('phase') or '-'}/{item.get('status') or '-'}；"
        f"需求文档：{item.get('requirementDocumentPath') or '未生成'}；"
        f"动作产物：{'有' if item.get('actionOutput') else '无'}；"
        f"任务测试：{'有' if item.get('testingReport') else '无'}；"
        f"测试用例：{item.get('testingCasesStatus') or 'todo'}）"
        for item in requirement_items[:100]
    ]
    mode_lines = (
        [
            "这是交付任务面板的一次需求级「预先生成测试用例」回合。遵循 delivery-requirement-testing 技能的测试用例设计模式。",
            "本回合只能读取需求、关联任务、代码和既有产物，设计范围、准备、顺序、步骤、预期及证据；绝不调用接口、UI、脚本或构建命令执行真实测试。",
            "不得输出验收判定、不得创建或覆盖测试报告、不得修改业务实现。",
        ]
        if test_case_only else [
            "这是交付任务面板的一次需求总体测试。遵循 delivery-requirement-testing 技能执行真实测试，不要执行任务拆解命令或修改业务实现。",
            "先读取已有 doc/test/<需求键>/测试用例.md 并按其中用例真实验证；没有明确执行和证据，不得写通过。",
        ]
    )
    final_instruction = (
        "最终必须把测试用例写入 doc/test/<需求键>/测试用例.md；按需写入测试计划.md，最终回复第一行必须为“测试用例已生成”。"
        if test_case_only else
        "最终必须把完整报告写入 doc/test/<需求键>/测试报告.md，并且最终回复第一行给出“验收判定：通过 / 不通过 / 受阻”。"
    )
    if follow_up:
        # 关联任务清单每轮都要刷新：任务状态、产物和报告在测试过程中会变，那是这类会话真正的工作数据。
        # 重复的技能说明、目录划分和需求正文才是该省的。
        return wrap_bridge_context(
            [
                "这是同一条需求测试会话的追加回合，模式没变："
                + (
                    "仍然只设计用例，绝不调用接口、UI、脚本或构建命令执行真实测试，不得输出验收判定或覆盖测试报告。"
                    if test_case_only
                    else "按已有用例真实验证，没有明确执行和证据不得写通过。"
                ),
                "首轮已交代过技能、目录划分和输出要求，这里不再重复，按本会话已确认的约定继续。",
                workspace_instruction(workspace),
                f"项目 program_id: {program_id}",
                f"需求键 requirement_key: {requirement_key}",
                *(
                    ["需求详情（用户已在面板上改过，以这一版为准）:", str(requirement.get("detail") or "（未填写）")]
                    if include_detail
                    else ["需求详情与本会话首轮一致，没有变化；若上下文里已经找不到，读本需求的大纲和任务文档接回来。"]
                ),
                f"需求总体测试资产目录: doc/test/{requirement_key}/（计划、报告、脚本、夹具和证据都归档到这里）。",
                "关联任务清单（状态每轮刷新；按需读对应文档、产物和代码，清单不是完整上下文）：",
                *(item_lines or ["- 该需求目前没有关联任务；先说明总体测试范围和受阻项，不要假装已覆盖任务链路。"]),
                final_instruction,
                "本会话如果被压缩过，先读上面的测试资产目录和任务文档把上下文接回来，不要凭印象往下接。",
                "本上下文标记闭合之后的内容，是用户本轮补充的测试要求、环境或数据说明。",
            ],
            message,
        )
    return wrap_bridge_context(
        [
            *mode_lines,
            workspace_instruction(workspace),
            f"项目 program_id: {program_id}",
            f"需求键 requirement_key: {requirement_key}",
            f"需求名称: {requirement.get('name') or '未命名'}",
            "需求详情:", str(requirement.get("detail") or "（未填写）"),
            f"需求总体测试资产目录: doc/test/{requirement_key}/（测试计划、报告、脚本、夹具和证据必须归档到此处）",
            f"需求文档目录: doc/requirements/{requirement_key}/（支持多份需求文档）；需求原型目录: doc/requirements/{requirement_key}/prototype/（支持多个 HTML）；需求测试目录: doc/test/{requirement_key}/（支持多份测试文档）。",
            "需求大纲、原型和测试是三个独立栏目：不要把测试计划或报告写进需求大纲/原型目录，也不要把独立流程图写进测试资产目录。",
            "关联任务清单（先按需读对应文档、产物和代码；清单不是完整上下文）：",
            *(item_lines or ["- 该需求目前没有关联任务；先说明总体测试范围和受阻项，不要假装已覆盖任务链路。"]),
            final_instruction,
            "本上下文标记闭合之后的内容，是用户本轮补充的测试要求、环境或数据说明。",
        ],
        message,
    )


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


def github_ssh_paths(home: Path | None = None) -> tuple[Path, Path]:
    root = (home or Path.home()).expanduser()
    ssh_directory = root / ".ssh"
    return ssh_directory, ssh_directory / "config"


def github_identity_files(config_path: Path, home: Path) -> list[Path]:
    """Read only `Host github.com` identity entries from the user's SSH config.

    The UI only claims that a GitHub key is ready when its corresponding public
    key is present. We intentionally do not inspect private-key contents.
    """
    try:
        lines = config_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    identities: list[Path] = []
    host_matches = False
    for line in lines:
        try:
            parts = shlex.split(line, comments=True, posix=True)
        except ValueError:
            continue
        if len(parts) < 2:
            continue
        option = parts[0].lower()
        if option == "host":
            host_matches = any(item.casefold() == GITHUB_SSH_HOST for item in parts[1:])
            continue
        if option != "identityfile" or not host_matches:
            continue
        raw_path = parts[1].replace("%d", str(home)).replace("%h", GITHUB_SSH_HOST)
        if raw_path == "~":
            candidate = home
        elif raw_path.startswith("~/"):
            candidate = home / raw_path[2:]
        else:
            candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            candidate = home / ".ssh" / candidate
        resolved = candidate.resolve(strict=False)
        if resolved not in identities:
            identities.append(resolved)
    return identities


def public_key_from_file(path: Path) -> str:
    try:
        if path.stat().st_size > 16 * 1024:
            return ""
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return ""
    for line in lines:
        candidate = line.strip()
        if candidate and not candidate.startswith("#"):
            return candidate if SSH_PUBLIC_KEY_RE.fullmatch(candidate) else ""
    return ""


def github_ssh_key_status(home: Path | None = None) -> dict[str, Any]:
    """Return only public, display-safe GitHub SSH state for the environment UI."""
    root = (home or Path.home()).expanduser()
    _, config_path = github_ssh_paths(root)
    result = {
        "githubSshConfigured": False,
        "githubSshPublicKey": "",
        "githubSshError": "",
    }
    for identity_path in github_identity_files(config_path, root):
        public_key = public_key_from_file(identity_path.with_name(f"{identity_path.name}.pub"))
        if public_key:
            result.update({"githubSshConfigured": True, "githubSshPublicKey": public_key})
            return result
    return result


def write_github_ssh_config(config_path: Path, home: Path, identity_path: Path) -> None:
    if config_path.exists() and config_path.is_symlink():
        raise BridgeFailure("SSH 配置文件是符号链接，未自动修改；请先手动配置 GitHub 密钥")
    try:
        existing = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    except (OSError, UnicodeDecodeError) as exc:
        raise BridgeFailure(f"无法读取 SSH 配置文件：{exc}") from exc
    relative_identity = identity_path.relative_to(home)
    managed_block = "\n".join((
        GITHUB_SSH_CONFIG_START,
        f"Host {GITHUB_SSH_HOST}",
        f"  HostName {GITHUB_SSH_HOST}",
        "  User git",
        f"  IdentityFile ~/{relative_identity}",
        "  IdentitiesOnly yes",
        GITHUB_SSH_CONFIG_END,
        "",
    ))
    content = GITHUB_SSH_CONFIG_BLOCK_RE.sub("", existing).lstrip()
    temporary = config_path.with_name(f".{config_path.name}.delivery-task-planner.tmp")
    try:
        temporary.write_text(managed_block + content, encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, config_path)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise BridgeFailure(f"无法写入 SSH 配置文件：{exc}") from exc


def ensure_github_ssh_key(home: Path | None = None) -> dict[str, Any]:
    """Create a managed GitHub key only when no configured public key is usable."""
    root = (home or Path.home()).expanduser()
    current = github_ssh_key_status(root)
    if current["githubSshConfigured"]:
        return current
    ssh_directory, config_path = github_ssh_paths(root)
    try:
        ssh_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(ssh_directory, 0o700)
    except OSError as exc:
        current["githubSshError"] = f"无法创建 SSH 目录：{exc}"
        return current
    private_key = ssh_directory / GITHUB_SSH_KEY_NAME
    public_key = private_key.with_name(f"{private_key.name}.pub")
    ssh_keygen = shutil.which("ssh-keygen")
    if not ssh_keygen:
        current["githubSshError"] = "未找到 ssh-keygen；请先完成 Git 安装后重新预设"
        return current
    try:
        if not private_key.exists():
            generated = subprocess.run(
                [ssh_keygen, "-q", "-t", "ed25519", "-f", str(private_key), "-N", "", "-C", "delivery-task-planner-github"],
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=10,
            )
            if generated.returncode != 0:
                current["githubSshError"] = f"GitHub SSH 密钥生成失败：{(generated.stdout or '').strip() or 'ssh-keygen 退出异常'}"
                return current
        elif not public_key_from_file(public_key):
            recovered = subprocess.run(
                [ssh_keygen, "-y", "-f", str(private_key)],
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=10,
            )
            recovered_key = (recovered.stdout or "").strip()
            if recovered.returncode != 0 or not SSH_PUBLIC_KEY_RE.fullmatch(recovered_key):
                current["githubSshError"] = "已有 GitHub 密钥无法恢复公钥，未覆盖原有文件"
                return current
            public_key.write_text(f"{recovered_key}\n", encoding="utf-8")
        os.chmod(private_key, 0o600)
        os.chmod(public_key, 0o644)
        write_github_ssh_config(config_path, root, private_key)
    except (BridgeFailure, OSError, subprocess.SubprocessError) as exc:
        current["githubSshError"] = str(exc)
        return current
    configured = github_ssh_key_status(root)
    if not configured["githubSshConfigured"]:
        configured["githubSshError"] = "GitHub SSH 密钥已生成，但未能完成配置校验"
    return configured


# ---------------------------------------------------------------------------
# 需求分支：面板只记录关联结果，真正的 Git 命令全部在本机工作目录里执行。
# 命令参数一律固定，不拼接用户输入到 shell；分支名先做白名单校验再交给 Git。
# ---------------------------------------------------------------------------

# git check-ref-format 明确禁止的字符，外加空白和 ASCII 控制字符。
# 别再自己另立一套更窄的白名单：仓库里真实存在 feature/issue#duokai 这种合法分支，
# 白名单挡掉它们之后，提交推送这些工程会直接失败在「分支名不合法」上。
GIT_BRANCH_FORBIDDEN_RE = re.compile(r"[\x00-\x20\x7f~^:?*\[\\]")
GIT_REMOTE_PREFIX = "remotes/"
GIT_REMOTE_NAME_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")
# 关联远端仓库时只接受这几种常见写法，挡掉以 - 开头会被 git 当成选项的输入。
GIT_REPOSITORY_URL_RE = re.compile(r"(?:[A-Za-z0-9._-]+@[A-Za-z0-9._-]+:[A-Za-z0-9._~/-]+|(?:ssh|git|https|http)://[A-Za-z0-9._~@:/-]+)")


def valid_git_branch_name(value: str) -> bool:
    """挡掉明显非法的分支名。最终仍由 git check-ref-format 判定，这里只做前置过滤。

    规则对齐 git 自己的 check-ref-format：只挡它禁止的字符和形态，不额外收窄。
    额外挡掉以 - 开头的名字——命令参数是按列表传的，不过 git 会把它当成选项。
    """
    name = str(value or "").strip()
    if not name or len(name) > 255 or name == "@":
        return False
    if GIT_BRANCH_FORBIDDEN_RE.search(name):
        return False
    if name.startswith(("-", "/", ".")) or name.endswith(("/", ".", ".lock")):
        return False
    return ".." not in name and "//" not in name and "@{" not in name


def valid_git_remote_name(value: str) -> bool:
    return bool(GIT_REMOTE_NAME_RE.fullmatch(str(value or "").strip()))


def run_git(workspace: Path, args: list[str], timeout: int = 20) -> subprocess.CompletedProcess:
    """在项目工作目录里执行一条只带固定参数的 Git 命令。"""
    try:
        return subprocess.run(
            ["git", "-C", str(workspace), *args],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise BridgeFailure("本机未安装 Git，请先在环境预设中完成安装") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise BridgeFailure(f"执行 Git 命令失败：{exc}") from exc


def git_output(workspace: Path, args: list[str], failure: str, timeout: int = 20) -> str:
    completed = run_git(workspace, args, timeout=timeout)
    if completed.returncode != 0:
        raise BridgeFailure(f"{failure}：{(completed.stdout or '').strip() or 'git 退出异常'}")
    return (completed.stdout or "").strip()


def git_workspace_probe(workspace: Path) -> tuple[bool, str]:
    """判断目录是否落在某个 Git 工作树里，同时把 git 原文带回去用于报错。"""
    completed = run_git(workspace, ["rev-parse", "--is-inside-work-tree"])
    output = (completed.stdout or "").strip()
    # run_git 把 stderr 并进了 stdout，git 的 warning/hint 会混在结果前面，判定只认最后一行。
    verdict = output.splitlines()[-1].strip() if output else ""
    return (completed.returncode == 0 and verdict == "true"), (output or "git 退出异常")


def require_git_workspace(workspace: Path) -> None:
    inside, detail = git_workspace_probe(workspace)
    if not inside:
        # 带上 git 原文，否则「不是仓库」和「仓库归属存疑」「HOME 不可读」在前端长得一模一样。
        raise BridgeFailure(f"项目工作目录不是 Git 仓库：{workspace}（git: {detail}）")


def git_current_branch(workspace: Path) -> str:
    """游离 HEAD 时返回空串，调用方据此提示用户先切回分支。"""
    completed = run_git(workspace, ["symbolic-ref", "--quiet", "--short", "HEAD"])
    return (completed.stdout or "").strip() if completed.returncode == 0 else ""


def git_default_branch(workspace: Path, branches: list[str]) -> str:
    """基准分支的默认值：优先当前分支，其次远端 HEAD，最后常见主干名。"""
    current = git_current_branch(workspace)
    if current:
        return current
    head = run_git(workspace, ["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"])
    if head.returncode == 0:
        candidate = (head.stdout or "").strip()
        if candidate in branches:
            return candidate
    for candidate in ("main", "master", "develop"):
        if candidate in branches:
            return candidate
    return branches[0] if branches else ""


def git_fetch_all(workspace: Path, remote: str = "origin") -> str:
    """把远端分支引用同步到本机。返回失败说明，空串表示成功或没有远端。

    别人新建的需求分支只有 fetch 过才看得见；离线时不该把整个分支列表也一起废掉。
    """
    if remote not in git_output(workspace, ["remote"], "读取 Git 远端失败").split():
        return ""
    completed = run_git(workspace, ["fetch", "--prune", remote], timeout=180)
    if completed.returncode != 0:
        return (completed.stdout or "").strip() or "git 退出异常"
    return ""


def git_branch_catalog(workspace: Path) -> dict[str, Any]:
    """本地分支加远端分支，去重后按名称排序；origin/HEAD 这类符号引用不列出。"""
    require_git_workspace(workspace)
    # 列分支前先同步远端引用：别人刚推的分支要能直接在建分支表单里选到。
    fetch_error = git_fetch_all(workspace)
    listed = git_output(
        workspace,
        ["for-each-ref", "--format=%(refname:short)", "refs/heads", "refs/remotes"],
        "读取 Git 分支失败",
    )
    # refs/remotes/origin/HEAD 的简写就是 origin，光判 /HEAD 结尾漏得掉，会混进分支下拉里。
    remote_names = set(git_output(workspace, ["remote"], "读取 Git 远端失败").split())
    branches: list[str] = []
    for line in listed.splitlines():
        name = line.strip()
        if not name or name.endswith("/HEAD") or name in remote_names:
            continue
        if name not in branches:
            branches.append(name)
    branches.sort()
    return {
        "branches": branches,
        "defaultBranch": git_default_branch(workspace, branches),
        # 面板要标注项目此刻所处的分支，游离 HEAD 时为空串。
        "currentBranch": git_current_branch(workspace),
        # 拉取远端失败时照样给本地分支，但要让面板能说清列表可能不是最新的。
        "fetchError": fetch_error,
    }


def normalized_git_remote_url(value: str) -> str:
    """用于显示层面的远端比较，忽略协议和 .git 尾缀的等价形式。"""
    text = str(value or "").strip().rstrip("/")
    if text.endswith(".git"):
        text = text[:-4]
    if text.startswith("git@") and ":" in text:
        host, path = text[4:].split(":", 1)
        text = f"{host}/{path}"
    for prefix in ("ssh://git@", "https://", "http://"):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    return text.lower().rstrip("/")


def git_remote_url(workspace: Path, remote: str) -> str:
    if not valid_git_remote_name(remote):
        raise BridgeFailure("Git 远端名称不合法")
    completed = run_git(workspace, ["remote", "get-url", remote])
    if completed.returncode != 0:
        return ""
    return (completed.stdout or "").strip()


def git_worktree_summary(workspace: Path) -> dict[str, int | bool]:
    """把 porcelain 状态压成面板需要的数量，绝不返回文件路径。"""
    # porcelain 的前两位就是暂存区 / 工作区状态，不能复用 git_output：它会 trim
    # 整段输出，恰好会吞掉第一行的前导空格，把 " M" 误读成 "M "。
    completed = run_git(workspace, ["status", "--porcelain=v1"])
    if completed.returncode != 0:
        raise BridgeFailure(f"读取 Git 工作区状态失败：{(completed.stdout or '').strip() or 'git 退出异常'}")
    output = (completed.stdout or "").rstrip()
    changed = 0
    staged = 0
    unstaged = 0
    untracked = 0
    for line in output.splitlines():
        changed += 1
        if line.startswith("??"):
            untracked += 1
            continue
        state = line[:2]
        if state[:1] not in {" ", "?"}:
            staged += 1
        if len(state) > 1 and state[1:2] not in {" ", "?"}:
            unstaged += 1
    return {
        "dirty": bool(output),
        "changed": changed,
        "staged": staged,
        "unstaged": unstaged,
        "untracked": untracked,
    }


# 变更明细面板的两条硬上限：文件太多只列前面一批，单个文件太大就不回正文。
MAX_GIT_CHANGE_FILES = 300
MAX_GIT_CHANGE_FILE_BYTES = 512 * 1024


def run_git_bytes(workspace: Path, args: list[str], timeout: int = 20) -> subprocess.CompletedProcess:
    """要原样拿文件内容时用这个：stderr 单独收，也不做文本解码。"""
    try:
        return subprocess.run(
            ["git", "-C", str(workspace), *args],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise BridgeFailure("本机未安装 Git，请先在环境预设中完成安装") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise BridgeFailure(f"执行 Git 命令失败：{exc}") from exc


def git_has_head(workspace: Path) -> bool:
    return run_git(workspace, ["rev-parse", "--verify", "--quiet", "HEAD"]).returncode == 0


def git_change_kind_of(state: str) -> str:
    """把 porcelain 的两位状态压成面板要的四种：add / modify / delete / rename。"""
    letters = {state[:1], state[1:2]} - {" "}
    if "D" in letters:
        return "delete"
    if "R" in letters:
        return "rename"
    if "A" in letters or "?" in letters:
        return "add"
    return "modify"


def git_numstat_totals(workspace: Path) -> dict[str, tuple[int, int]]:
    """已跟踪文件相对 HEAD 的增删行数；二进制文件 git 给的是 -，按 0 计。"""
    if not git_has_head(workspace):
        return {}
    completed = run_git(workspace, ["diff", "--numstat", "HEAD"], timeout=60)
    if completed.returncode != 0:
        return {}
    totals: dict[str, tuple[int, int]] = {}
    for line in (completed.stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        added = int(parts[0]) if parts[0].isdigit() else 0
        removed = int(parts[1]) if parts[1].isdigit() else 0
        # 重命名在 numstat 里是 "old => new" 这种写法，取最后一段当作现在的路径。
        path = parts[2].split(" => ")[-1].strip("{}")
        totals[path] = (added, removed)
    return totals


def git_untracked_line_count(workspace: Path, path: str) -> int:
    target = workspace / path
    try:
        if not target.is_file() or target.stat().st_size > MAX_GIT_CHANGE_FILE_BYTES:
            return 0
        raw = target.read_bytes()
    except OSError:
        return 0
    if b"\0" in raw:
        return 0
    return len(raw.splitlines())


def git_change_files(workspace: Path) -> dict[str, Any]:
    """列出工作区相对 HEAD 的文件级改动，给「变更」面板点开看明细用。"""
    require_git_workspace(workspace)
    # -uall 让未跟踪目录展开成一条条文件，否则新增的整个目录只会收到一条以 / 结尾的条目，
    # 面板既算不出行数，也读不到正文，点开只剩「没有可对比的内容」。
    completed = run_git(workspace, ["status", "--porcelain=v1", "-z", "-uall"], timeout=60)
    if completed.returncode != 0:
        raise BridgeFailure(f"读取 Git 变更清单失败：{(completed.stdout or '').strip() or 'git 退出异常'}")
    entries = [chunk for chunk in (completed.stdout or "").split("\0") if chunk]
    totals = git_numstat_totals(workspace)
    files: list[dict[str, Any]] = []
    skip_next = False
    for index, entry in enumerate(entries):
        if skip_next:
            # 重命名条目后面紧跟一条旧路径，-z 下没有引号可解析，只能靠位置跳过。
            skip_next = False
            continue
        if len(entry) < 4:
            continue
        state = entry[:2]
        path = entry[3:]
        kind = git_change_kind_of(state)
        if kind == "rename":
            skip_next = index + 1 < len(entries)
        untracked = state == "??"
        added, removed = totals.get(path, (0, 0))
        if untracked:
            added = git_untracked_line_count(workspace, path)
            removed = 0
        files.append({
            "path": path,
            "kind": kind,
            "added": added,
            "removed": removed,
            "staged": state[:1] not in {" ", "?"},
            "untracked": untracked,
        })
    files.sort(key=lambda item: item["path"])
    truncated = len(files) > MAX_GIT_CHANGE_FILES
    return {
        "workspace": str(workspace),
        "branch": git_current_branch(workspace),
        "files": files[:MAX_GIT_CHANGE_FILES],
        "total": len(files),
        "truncated": truncated,
    }


def git_change_text(raw: bytes) -> tuple[str, bool]:
    """返回可展示的正文和「是不是二进制」；二进制一律不回正文。"""
    if b"\0" in raw:
        return "", True
    try:
        return raw.decode("utf-8"), False
    except UnicodeDecodeError:
        return "", True


def git_change_detail(workspace: Path, path: str) -> dict[str, Any]:
    """一个文件改动前后的两份正文，交给前端的 diff 组件对比。

    只接受当前确实有改动的路径：这样既不用自己防目录穿越，也不会变成任意文件读取口子。
    """
    target = str(path or "").strip()
    if not target:
        raise BridgeFailure("缺少文件路径")
    listing = git_change_files(workspace)
    entry = next((item for item in listing["files"] if item["path"] == target), None)
    if entry is None:
        raise BridgeFailure(f"当前工作区没有这个文件的改动：{target}")
    old_raw = b""
    if not entry["untracked"] and entry["kind"] != "add" and git_has_head(workspace):
        completed = run_git_bytes(workspace, ["show", f"HEAD:{target}"], timeout=60)
        if completed.returncode == 0:
            old_raw = completed.stdout or b""
    new_raw = b""
    working = workspace / target
    try:
        if working.is_file():
            new_raw = working.read_bytes()
    except OSError as exc:
        raise BridgeFailure(f"读取文件失败：{exc}") from exc
    if len(old_raw) > MAX_GIT_CHANGE_FILE_BYTES or len(new_raw) > MAX_GIT_CHANGE_FILE_BYTES:
        return {**entry, "oldText": "", "newText": "", "binary": False, "truncated": True}
    old_text, old_binary = git_change_text(old_raw)
    new_text, new_binary = git_change_text(new_raw)
    return {
        **entry,
        "oldText": old_text,
        "newText": new_text,
        "binary": old_binary or new_binary,
        "truncated": False,
    }


def git_local_branch_for_reference(workspace: Path, reference: str, remote: str) -> tuple[str, str]:
    """解析本地或远端分支引用，返回应使用的本地名和可选远端引用。"""
    value = str(reference or "").strip()
    if not valid_git_branch_name(value):
        raise BridgeFailure("需求分支名不合法")
    if git_branch_exists(workspace, value):
        return value, ""
    remote_prefix = f"{remote}/"
    if value.startswith(remote_prefix):
        local = value[len(remote_prefix):]
        remote_ref = value
    else:
        local = value
        remote_ref = f"{remote}/{value}"
    if not valid_git_branch_name(local):
        raise BridgeFailure("远端需求分支名不合法")
    exists = run_git(workspace, ["rev-parse", "--verify", "--quiet", f"refs/remotes/{remote_ref}"])
    if exists.returncode != 0:
        # 分支可能是别人刚推上来的，本机还没 fetch 过；先拉一次远端引用再判断。
        fetched = run_git(workspace, ["fetch", remote, local], timeout=180)
        exists = run_git(workspace, ["rev-parse", "--verify", "--quiet", f"refs/remotes/{remote_ref}"])
        if exists.returncode != 0:
            detail = (fetched.stdout or "").strip()
            raise BridgeFailure(
                f"本机和远端都不存在需求分支 {value}" + (f"。git 输出：{detail}" if fetched.returncode != 0 and detail else "")
            )
    return local, remote_ref


def git_checkout_reference(workspace: Path, reference: str, remote: str) -> str:
    """切到本地分支；只有远端存在时创建受跟踪的本地分支。"""
    local, remote_ref = git_local_branch_for_reference(workspace, reference, remote)
    if git_current_branch(workspace) == local:
        return local
    if git_worktree_dirty(workspace):
        raise BridgeFailure(f"工作目录有未提交改动，无法切换到分支 {local}，请先提交或暂存")
    args = ["checkout", "--recurse-submodules", local] if not remote_ref else [
        "checkout", "--recurse-submodules", "-b", local, "--track", remote_ref,
    ]
    completed = run_git(workspace, args)
    if completed.returncode != 0:
        raise BridgeFailure(f"切换分支 {local} 失败：{(completed.stdout or '').strip() or 'git 退出异常'}")
    return local


def git_workspace_status(workspace: Path, expected_remote_url: str = "", remote: str = "origin") -> dict[str, Any]:
    """读取项目当前 Git 状态。此函数只做本机读取，不 fetch、不切换、不写入。"""
    require_git_workspace(workspace)
    if not valid_git_remote_name(remote):
        raise BridgeFailure("Git 远端名称不合法")
    actual_remote_url = git_remote_url(workspace, remote)
    expected = str(expected_remote_url or "").strip()
    remote_matches = not expected or (
        bool(actual_remote_url) and normalized_git_remote_url(actual_remote_url) == normalized_git_remote_url(expected)
    )
    summary = git_worktree_summary(workspace)
    current = git_current_branch(workspace)
    # 远端地址可能包含嵌入式凭据；浏览器只需要知道是否一致，不能回传具体地址。
    return {
        "workspace": str(workspace),
        "isGitRepository": True,
        "remoteName": remote,
        "remoteMatches": remote_matches,
        "currentBranch": current,
        "detached": not bool(current),
        "checkedAt": int(time.time()),
        **summary,
    }


def git_prepare_branch(
    workspace: Path,
    reference: str,
    strategy: str = "switch",
    commit_message: str = "",
    expected_remote_url: str = "",
    remote: str = "origin",
) -> dict[str, Any]:
    """用户确认后才处理未提交改动并切分支；绝不丢弃改动或自动应用 stash。"""
    if strategy not in {"switch", "commit", "stash"}:
        raise BridgeFailure("未知的 Git 分支处理方式")
    status = git_workspace_status(workspace, expected_remote_url, remote)
    if not status["remoteMatches"]:
        raise BridgeFailure("本机 Git 远端与项目配置不一致，请先确认项目仓库地址或工作目录")
    if status["detached"]:
        raise BridgeFailure("当前工作目录处于游离 HEAD，不能切换需求分支")
    local, _ = git_local_branch_for_reference(workspace, reference, remote)
    if status["currentBranch"] == local:
        # 已经在需求分支上：干净的工作区顺手拉一次最新，脏工作区不动它，交给用户先处理改动。
        pulled_only = False if status["dirty"] else git_pull_branch(workspace, local, remote)
        return {
            "branch": local,
            "previousBranch": status["currentBranch"],
            "pulled": pulled_only,
            "committed": False,
            "stashed": False,
            "status": git_workspace_status(workspace, expected_remote_url, remote) if pulled_only else status,
        }
    # 先把目标分支拉到远端最新，再决定怎么处理当前改动：拉不动就不该先提交一轮。
    pulled = git_pull_branch(workspace, local, remote)
    committed = False
    stashed = False
    if status["dirty"]:
        dirty_submodules = git_dirty_submodule_workspaces(workspace)
        if strategy == "commit":
            message = git_commit_message_of(commit_message, str(status["currentBranch"]))
            for submodule in dirty_submodules:
                submodule_label = git_submodule_label(workspace, submodule)
                if not git_current_branch(submodule):
                    raise BridgeFailure(f"子模块 {submodule_label} 处于游离 HEAD，不能自动提交，请改选暂存后切换")
                if run_git(submodule, ["add", "--all"]).returncode != 0:
                    raise BridgeFailure(f"暂存子模块 {submodule_label} 改动失败")
                completed = run_git(submodule, ["commit", "-m", f"{message} ({submodule_label})"], timeout=120)
                if completed.returncode != 0:
                    raise BridgeFailure(
                        f"提交子模块 {submodule_label} 改动失败：{(completed.stdout or '').strip() or 'git 退出异常'}"
                    )
            if run_git(workspace, ["add", "--all"]).returncode != 0:
                raise BridgeFailure("暂存当前工作区改动失败")
            completed = run_git(workspace, ["commit", "-m", message], timeout=120)
            if completed.returncode != 0:
                raise BridgeFailure(f"提交当前分支改动失败：{(completed.stdout or '').strip() or 'git 退出异常'}")
            committed = True
        elif strategy == "stash":
            label = f"delivery-task-planner: {status['currentBranch']} -> {local}"
            for submodule in dirty_submodules:
                submodule_label = git_submodule_label(workspace, submodule)
                completed = run_git(
                    submodule,
                    ["stash", "push", "--include-untracked", "-m", f"{label} ({submodule_label})"],
                    timeout=120,
                )
                if completed.returncode != 0:
                    raise BridgeFailure(
                        f"暂存子模块 {submodule_label} 改动失败：{(completed.stdout or '').strip() or 'git 退出异常'}"
                    )
            completed = run_git(workspace, ["stash", "push", "--include-untracked", "-m", label], timeout=120)
            if completed.returncode != 0:
                raise BridgeFailure(f"暂存当前分支改动失败：{(completed.stdout or '').strip() or 'git 退出异常'}")
            stashed = True
        else:
            raise BridgeFailure("工作目录有未提交改动，请选择先提交或暂存后再切换")
        if git_worktree_dirty(workspace):
            remaining = git_worktree_summary(workspace)
            raise BridgeFailure(
                f"处理改动后工作目录仍有 {remaining['changed']} 个待提交文件，可能有其它进程正在写入；请停止写入后重试"
            )
    branch = git_checkout_reference(workspace, reference, remote)
    return {
        "branch": branch,
        "pulled": pulled,
        "committed": committed,
        "stashed": stashed,
        "previousBranch": status["currentBranch"],
        "status": git_workspace_status(workspace, expected_remote_url, remote),
    }


def git_branch_exists(workspace: Path, branch: str) -> bool:
    return run_git(workspace, ["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"]).returncode == 0


def git_worktree_dirty(workspace: Path) -> bool:
    return bool(git_output(workspace, ["status", "--porcelain"], "读取 Git 工作区状态失败"))


def git_submodule_workspaces(workspace: Path) -> list[Path]:
    """返回已初始化的子模块，按最内层到最外层排列，便于先处理嵌套工作区。"""
    root = workspace.resolve()
    seen: set[Path] = set()
    result: list[Path] = []

    def collect(parent: Path) -> None:
        completed = run_git(parent, ["config", "--file", ".gitmodules", "--null", "--get-regexp", r"^submodule\..*\.path$"])
        if completed.returncode == 1:
            return
        if completed.returncode != 0:
            raise BridgeFailure(f"读取子模块配置失败：{(completed.stdout or '').strip() or 'git 退出异常'}")
        for record in (completed.stdout or "").split("\0"):
            if not record:
                continue
            _, separator, raw_path = record.partition("\n")
            child = (parent / raw_path.strip()).resolve()
            if not separator or not raw_path.strip():
                raise BridgeFailure("子模块路径配置无效")
            try:
                child.relative_to(root)
            except ValueError as exc:
                raise BridgeFailure("子模块路径超出项目工作目录") from exc
            if child in seen or run_git(child, ["rev-parse", "--is-inside-work-tree"]).returncode != 0:
                continue
            seen.add(child)
            collect(child)
            result.append(child)

    collect(root)
    return result


def git_dirty_submodule_workspaces(workspace: Path) -> list[Path]:
    return [submodule for submodule in git_submodule_workspaces(workspace) if git_worktree_dirty(submodule)]


def git_submodule_label(workspace: Path, submodule: Path) -> str:
    return submodule.resolve().relative_to(workspace.resolve()).as_posix()


# 扫子项目时不进这些目录：依赖和产物目录里也可能躺着 .git，但它们不是这个项目的工程。
GIT_SUBPROJECT_SKIP_DIRS = {
    "node_modules", "vendor", "dist", "build", "out", "target",
    "__pycache__", "venv", ".venv", "tmp", "temp",
}


def git_subproject_workspaces(workspace: Path) -> list[Path]:
    """工作目录下一级子目录里自带 .git 的独立工程，按目录名排序。

    只看一级：再往下扫要为每个候选目录多跑一轮 IO，而实际的多工程布局都是
    「工作目录/工程名」这一层。已注册成 submodule 的目录同样自带 .git，照样列出来。
    """
    root = workspace.resolve()
    try:
        children = sorted(root.iterdir(), key=lambda item: item.name.casefold())
    except OSError as exc:
        raise BridgeFailure(f"读取项目工作目录失败：{exc}") from exc
    result: list[Path] = []
    for child in children:
        if child.name.startswith(".") or child.name in GIT_SUBPROJECT_SKIP_DIRS:
            continue
        if child.is_symlink() or not child.is_dir():
            continue
        if not (child / ".git").exists():
            continue
        result.append(child.resolve())
    return result


def git_subproject_workspace_of(workspace: Path, relative: str) -> Path:
    """把前端传回来的子项目相对路径还原成目录；只认扫描列出来的那一级。"""
    value = str(relative or "").strip().strip("/")
    root = workspace.resolve()
    if not value or value == ".":
        return root
    candidate = (root / value).resolve()
    if candidate == root:
        return root
    if candidate not in git_subproject_workspaces(root):
        raise BridgeFailure(f"子项目不存在或不是 Git 仓库：{value}")
    return candidate


def git_branch_reference_exists(workspace: Path, branch: str, remote: str = "origin") -> bool:
    """本机或已同步的远端引用里有没有这条分支；只读本地引用，不联网 fetch。"""
    if not valid_git_branch_name(branch):
        return False
    if git_branch_exists(workspace, branch):
        return True
    return run_git(workspace, ["rev-parse", "--verify", "--quiet", f"refs/remotes/{remote}/{branch}"]).returncode == 0


def git_project_snapshot(workspace: Path, relative: str, branch: str = "", remote: str = "origin") -> dict[str, Any]:
    """单个工程的 Git 快照。读不动的工程只把原因带回去，不连累同级的其它工程。"""
    record: dict[str, Any] = {
        "path": relative,
        "name": relative or workspace.name,
        "workspace": str(workspace),
        "isGitRepository": False,
        "hasBranch": False,
        "error": "",
        "remoteName": remote,
        "remoteMatches": True,
        "currentBranch": "",
        "detached": False,
        "dirty": False,
        "changed": 0,
        "staged": 0,
        "unstaged": 0,
        "untracked": 0,
        "checkedAt": int(time.time()),
    }
    try:
        record.update(git_workspace_status(workspace, "", remote))
        if branch:
            record["hasBranch"] = git_branch_reference_exists(workspace, branch, remote)
    except BridgeFailure as exc:
        record["error"] = str(exc)
    # git_workspace_status 会覆盖 workspace，但不认识 path / name，这里补回来。
    record["path"] = relative
    record["name"] = relative or workspace.name
    return record


def git_workspace_projects(workspace: Path, branch: str = "", remote: str = "origin") -> dict[str, Any]:
    """根工作目录加一级子项目的 Git 快照，给需求窗口的 Git 面板分工程展示。"""
    projects = [git_project_snapshot(workspace, "", branch, remote)]
    for child in git_subproject_workspaces(workspace):
        projects.append(git_project_snapshot(child, child.name, branch, remote))
    return {"workspace": str(workspace), "projects": projects}


def git_subproject_targets_of(workspace: Path, raw: Any, branch: str = "", remote: str = "origin") -> list[str]:
    """请求里带了 targets 就按它来，没带就自动选出所有已有这条分支的子项目。

    没带 targets 的调用方（需求列表的分支检查、旧版本控制台）也该把子项目一起带上，
    否则切完分支只有根目录跟着走，子工程还停在别的需求上。
    """
    if raw is None:
        if not branch:
            return []
        return [
            child.name for child in git_subproject_workspaces(workspace)
            if git_branch_reference_exists(child, branch, remote)
        ]
    if not isinstance(raw, list):
        raise BridgeFailure("子项目列表格式不正确")
    targets: list[str] = []
    for value in raw:
        name = str(value or "").strip().strip("/")
        if name and name != "." and name not in targets:
            targets.append(name)
    return targets


def git_checkout_branch(workspace: Path, branch: str) -> None:
    """切换到已存在的本地分支；工作区有未提交改动时不强行切，交回给用户处理。"""
    if git_current_branch(workspace) == branch:
        return
    if git_worktree_dirty(workspace):
        raise BridgeFailure(f"工作目录有未提交改动，无法切换到分支 {branch}，请先提交或暂存")
    completed = run_git(workspace, ["checkout", "--recurse-submodules", branch])
    if completed.returncode != 0:
        raise BridgeFailure(f"切换分支 {branch} 失败：{(completed.stdout or '').strip() or 'git 退出异常'}")


def git_default_remote(workspace: Path) -> str:
    """只认 origin：需求分支是给评审用的，推到哪个远端不该由面板猜。"""
    remotes = git_output(workspace, ["remote"], "读取 Git 远端失败").split()
    if "origin" in remotes:
        return "origin"
    raise BridgeFailure("当前仓库没有配置 origin 远端，无法推送")


GIT_PUSH_REPAIR_TIMEOUT_SECONDS = 15 * 60


def git_branch_synced(workspace: Path, branch: str, remote: str = "origin") -> bool:
    """本地分支是否已经全部推到远端。AI 兜底之后用它判定，而不是信 AI 的自述。"""
    run_git(workspace, ["fetch", remote, branch], timeout=180)
    ahead = run_git(workspace, ["rev-list", "--count", f"{remote}/{branch}..{branch}"])
    return ahead.returncode == 0 and (ahead.stdout or "").strip() == "0"


def build_git_push_repair_prompt(workspace: Path, branch: str, remote: str, failure: str, commit_message: str) -> str:
    """推送失败时交给 AI 的修复提示词。只授权它解决推送本身，不允许改业务实现。"""
    return wrap_bridge_context(
        [
            "这是交付任务面板的「推送需求分支」回合：面板已经尝试提交并推送，但失败了，请你在本机把它修好并真正推送成功。",
            workspace_instruction(workspace),
            f"需求分支: {branch}",
            f"远端: {remote}",
            f"面板使用的提交说明: {commit_message}",
            "",
            "面板执行失败的原始输出:",
            failure,
            "",
            "处理要求:",
            "- 面板已经把改动提交成提交点，并自动试过一次 rebase；失败后已经 --abort，仓库现在是干净状态，不在变基中。",
            "- 只解决提交与推送本身：拉取远端、rebase 或 merge、解决冲突、补提交、重新 push。",
            "- 解决冲突时保留双方的真实意图，不要为了让命令通过而删掉别人的改动。",
            "- 不要修改与本次冲突无关的业务实现，不要改动其他分支。",
            f"- 禁止 force push、禁止 push 到 {branch} 以外的分支、禁止改写已经推到远端的历史。",
            "- 处理不了（例如需要凭据、需要人工决策的冲突）就停下来说明原因，不要绕开。",
            "- 最后必须实际执行一次 push，并在回复里贴出 push 命令的真实输出。",
        ],
        f"推送需求分支 {branch} 失败，请解决后重新推送。",
    )


MAX_GIT_COMMIT_MESSAGE_BYTES = 4 * 1024


def git_commit_message_of(value: str, branch: str) -> str:
    """提交说明来自用户输入，只做长度和控制字符限制；命令参数是数组，不存在注入。"""
    message = str(value or "").strip() or f"chore: {branch}"
    if len(message.encode("utf-8")) > MAX_GIT_COMMIT_MESSAGE_BYTES:
        raise BridgeFailure("提交说明过长")
    if "\x00" in message:
        raise BridgeFailure("提交说明不能包含控制字符")
    return message


def git_rebase_onto_remote(workspace: Path, branch: str, remote: str = "origin") -> str:
    """推送前把远端最新并进本地分支。返回 ""/"pulled"/"rebased"。

    这里刻意不用 stash：改动在上一步已经提交成一个提交点，没有需要暂存的东西；
    冲突留在 rebase 里比留在 stash pop 里可控得多，失败也会 --abort 回到干净状态。
    """
    if remote not in git_output(workspace, ["remote"], "读取 Git 远端失败").split():
        return ""
    fetched = run_git(workspace, ["fetch", remote, branch], timeout=180)
    remote_ref = f"{remote}/{branch}"
    if run_git(workspace, ["rev-parse", "--verify", "--quiet", f"refs/remotes/{remote_ref}"]).returncode != 0:
        # 远端还没有这条分支，这次就是首推。
        return ""
    if fetched.returncode != 0:
        raise BridgeFailure(f"拉取分支 {branch} 失败：{(fetched.stdout or '').strip() or 'git 退出异常'}")
    if git_remote_ref_merged(workspace, remote_ref, branch):
        return ""
    if run_git(workspace, ["merge-base", "--is-ancestor", branch, remote_ref]).returncode == 0:
        # 本地只是落后：快进即可，不要平白造一个合并提交。
        completed = run_git(workspace, ["merge", "--ff-only", remote_ref], timeout=120)
        if completed.returncode != 0:
            raise BridgeFailure(f"快进到 {remote_ref} 失败：{(completed.stdout or '').strip() or 'git 退出异常'}")
        return "pulled"
    completed = run_git(workspace, ["rebase", remote_ref], timeout=300)
    if completed.returncode != 0:
        detail = (completed.stdout or "").strip() or "git 退出异常"
        # 冲突不留在半路：先回到干净状态，再把原始输出抛上去交给 AI 兜底那一轮处理。
        run_git(workspace, ["rebase", "--abort"], timeout=120)
        raise BridgeFailure(f"本地分支 {branch} 与 {remote_ref} 有冲突，自动 rebase 没能完成：{detail}")
    return "rebased"


def git_push_branch(workspace: Path, branch: str, message: str = "", push: bool = True) -> dict[str, Any]:
    """先同步远端最新，再把工作区改动提交到需求分支并推到 origin。

    只做普通推送，不带 --force：远端已经跑在前面时报错给用户，不在这里替他决定怎么合。
    push=False 时只提交不推送，给「仅提交」用：本地留一个提交点，什么时候推由用户决定。
    """
    if not valid_git_branch_name(branch):
        raise BridgeFailure("需求分支名不合法")
    require_git_workspace(workspace)
    if not git_branch_exists(workspace, branch):
        raise BridgeFailure(f"本机不存在需求分支 {branch}，请先创建分支")
    # 仅提交在有 origin 时也要先同步最新；纯本地仓库仍可正常落一个提交点。
    remote = git_default_remote(workspace) if push else (
        "origin" if "origin" in git_output(workspace, ["remote"], "读取 Git 远端失败").split() else ""
    )
    commit_message = git_commit_message_of(message, branch)
    current = git_current_branch(workspace)
    dirty = git_worktree_dirty(workspace)
    if current != branch:
        # 改动是在别的分支上做的，提交到需求分支多半是误操作，让用户自己先归位。
        if dirty:
            raise BridgeFailure(
                f"工作目录当前在分支 {current or 'HEAD'} 上且有未提交改动，请先处理后再推送需求分支 {branch}"
            )
        git_checkout_branch(workspace, branch)
    committed = False
    synced = ""
    if dirty:
        # 工作区有改动时不能直接 pull/rebase。先落一个仅供同步使用的临时提交，
        # 同步完成（或失败并 abort）后立即拆回工作区改动，再用用户填写的说明正式提交。
        # 这样远端冲突发生在正式提交之前，也不会把用户改动藏进 stash。
        add = run_git(workspace, ["add", "--all"])
        if add.returncode != 0:
            raise BridgeFailure(f"暂存改动失败：{(add.stdout or '').strip() or 'git 退出异常'}")
        temporary = run_git(workspace, ["commit", "-m", "delivery-task-planner: sync before commit"], timeout=120)
        if temporary.returncode != 0:
            raise BridgeFailure(f"准备拉取前的临时提交失败：{(temporary.stdout or '').strip() or 'git 退出异常'}")
        try:
            if remote:
                synced = git_rebase_onto_remote(workspace, branch, remote)
        finally:
            restored = run_git(workspace, ["reset", "--mixed", "HEAD^"], timeout=120)
            if restored.returncode != 0:
                raise BridgeFailure(
                    f"拉取后恢复待提交改动失败：{(restored.stdout or '').strip() or 'git 退出异常'}"
                )
        commit = run_git(workspace, ["add", "--all"])
        if commit.returncode != 0:
            raise BridgeFailure(f"暂存改动失败：{(commit.stdout or '').strip() or 'git 退出异常'}")
        commit = run_git(workspace, ["commit", "-m", commit_message], timeout=120)
        if commit.returncode != 0:
            raise BridgeFailure(f"提交改动失败：{(commit.stdout or '').strip() or 'git 退出异常'}")
        committed = True
    elif remote:
        # 没有工作区改动也要在“提交/推送”动作开始时同步一次，避免基于旧分支判断已是最新。
        synced = git_rebase_onto_remote(workspace, branch, remote)
    if not push:
        return {
            "pushed": False,
            "branch": branch,
            "remote": remote,
            "committed": committed,
            "commitMessage": commit_message if committed else "",
            "upToDate": False,
            "synced": synced,
            "output": "",
        }
    # 首次同步与 push 之间远端仍可能变化；推送前再确认一次，关闭这段竞态窗口。
    synced = git_rebase_onto_remote(workspace, branch, remote) or synced
    completed = run_git(workspace, ["push", "--set-upstream", remote, f"{branch}:{branch}"], timeout=180)
    output = (completed.stdout or "").strip()
    if completed.returncode != 0:
        raise BridgeFailure(f"推送分支 {branch} 失败：{output or 'git 退出异常'}")
    return {
        "pushed": True,
        "branch": branch,
        "remote": remote,
        "committed": committed,
        "commitMessage": commit_message if committed else "",
        "upToDate": "Everything up-to-date" in output,
        # 推送前是否并过远端最新："pulled" 是快进，"rebased" 是把本地提交挪到远端之后。
        "synced": synced,
        "output": output[-2000:],
    }


def git_remote_ref_merged(workspace: Path, remote_ref: str, local_branch: str) -> bool:
    """远端引用是否已经在本地分支里；领先远端的本地分支不该被当成「拉取失败」。"""
    return run_git(workspace, ["merge-base", "--is-ancestor", remote_ref, local_branch]).returncode == 0


def git_pull_branch(workspace: Path, local: str, remote: str = "origin") -> bool:
    """切到需求分支前先把它拉到远端最新；拉不动就报错，不带着旧代码切过去。

    返回是否真的更新过本地分支。远端还没有这条分支时直接跳过：刚建的需求分支很正常。
    """
    remotes = git_output(workspace, ["remote"], "读取 Git 远端失败").split()
    if remote not in remotes:
        return False
    fetched = run_git(workspace, ["fetch", remote, local], timeout=180)
    remote_ref = f"{remote}/{local}"
    if run_git(workspace, ["rev-parse", "--verify", "--quiet", f"refs/remotes/{remote_ref}"]).returncode != 0:
        return False
    if fetched.returncode != 0:
        raise BridgeFailure(f"拉取需求分支 {local} 失败：{(fetched.stdout or '').strip() or 'git 退出异常'}")
    if not git_branch_exists(workspace, local):
        # 本机还没有这条分支，切换时会基于刚拉到的远端引用创建，已经是最新的。
        return False
    if git_remote_ref_merged(workspace, remote_ref, local):
        return False
    if git_current_branch(workspace) == local:
        completed = run_git(workspace, ["merge", "--ff-only", remote_ref], timeout=120)
    else:
        completed = run_git(workspace, ["fetch", remote, f"{local}:{local}"], timeout=180)
    if completed.returncode != 0:
        raise BridgeFailure(
            f"需求分支 {local} 无法快进到 {remote_ref}：本机的 {local} 上有还没推送的提交，或者已经和远端分叉。"
            f"请先把 {local} 上的改动推送或自行合并，再重新切换。"
            f"git 输出：{(completed.stdout or '').strip() or 'git 退出异常'}"
        )
    return True


def git_sync_base_branch(workspace: Path, base_branch: str, remote: str = "origin") -> str:
    """建需求分支前把基准分支拉到远端最新；否则新分支会切在过时的代码上。

    返回真正用来切分支的引用：能同步就是本地基准分支，只存在于远端时返回 remote/xxx。
    只做快进，分叉了就报错交回给用户，不在这里 merge 或 rebase。
    """
    remotes = git_output(workspace, ["remote"], "读取 Git 远端失败").split()
    remote_prefix = f"{remote}/"
    # 基准分支可能是用户直接选的 origin/xxx；拉取时要用远端那一侧的名字。
    remote_side = base_branch[len(remote_prefix):] if base_branch.startswith(remote_prefix) else base_branch
    local_exists = git_branch_exists(workspace, base_branch)
    if remote not in remotes:
        # 没有 origin 的纯本地仓库没有「最新」可拉，按本地基准分支继续。
        if not local_exists:
            raise BridgeFailure(f"基准分支不存在：{base_branch}")
        return base_branch
    fetched = run_git(workspace, ["fetch", remote, remote_side], timeout=180)
    remote_ref = f"{remote}/{remote_side}"
    remote_exists = run_git(workspace, ["rev-parse", "--verify", "--quiet", f"refs/remotes/{remote_ref}"]).returncode == 0
    if not remote_exists:
        # 远端没有这条分支：只可能是仅存在于本机的基准分支。
        if local_exists:
            return base_branch
        raise BridgeFailure(f"基准分支在本机和 {remote} 都不存在：{base_branch}")
    if fetched.returncode != 0:
        raise BridgeFailure(f"拉取基准分支 {base_branch} 失败：{(fetched.stdout or '').strip() or 'git 退出异常'}")
    if not local_exists:
        # 本机还没有这条基准分支，直接从刚拉到的远端引用切出需求分支。
        return remote_ref
    if git_remote_ref_merged(workspace, remote_ref, base_branch):
        # 本地已经包含远端全部提交（可能还领先），没有可拉的东西。
        return base_branch
    if git_current_branch(workspace) == base_branch:
        completed = run_git(workspace, ["merge", "--ff-only", remote_ref], timeout=120)
    else:
        # 不在基准分支上时用 fetch 的引用更新做快进，避免为了拉一次而来回切分支。
        completed = run_git(workspace, ["fetch", remote, f"{remote_side}:{base_branch}"], timeout=180)
    if completed.returncode != 0:
        raise BridgeFailure(
            f"基准分支 {base_branch} 无法快进到 {remote_ref}，可能已经分叉或有未推送的提交，"
            f"请先自行处理：{(completed.stdout or '').strip() or 'git 退出异常'}"
        )
    return base_branch


def git_create_branch(workspace: Path, base_branch: str, branch: str) -> dict[str, Any]:
    """从基准分支创建并切换到需求分支；分支已存在时只切换，不覆盖已有提交。"""
    if not valid_git_branch_name(base_branch):
        raise BridgeFailure("基准分支名不合法")
    if not valid_git_branch_name(branch):
        raise BridgeFailure("需求分支名不合法")
    require_git_workspace(workspace)
    remote = "origin"
    has_remote = remote in git_output(workspace, ["remote"], "读取 Git 远端失败").split()
    # 「已有分支」下拉给的是 origin/xxx；照原样建会多出一条名叫 origin/xxx 的本地分支。
    local = branch[len(f"{remote}/"):] if has_remote and branch.startswith(f"{remote}/") else branch
    if not local or run_git(workspace, ["check-ref-format", "--branch", local]).returncode != 0:
        raise BridgeFailure(f"需求分支名不符合 Git 规范：{branch}")
    if git_branch_exists(workspace, local):
        # 本机已有这条分支：切过去并拉到远端最新，不覆盖已有提交。
        git_checkout_branch(workspace, local)
        git_pull_branch(workspace, local)
        return {"created": False, "baseBranch": base_branch, "branch": local}
    if git_worktree_dirty(workspace):
        raise BridgeFailure("工作目录有未提交改动，无法创建需求分支，请先提交或暂存")
    if has_remote:
        run_git(workspace, ["fetch", remote, local], timeout=180)
        if run_git(workspace, ["rev-parse", "--verify", "--quiet", f"refs/remotes/{remote}/{local}"]).returncode == 0:
            # 别人已经推过同名分支：必须关联它，从基准分支另起一条会和远端分叉。
            checkout = run_git(workspace, ["checkout", "--recurse-submodules", "-b", local, "--track", f"{remote}/{local}"])
            if checkout.returncode != 0:
                raise BridgeFailure(f"关联远端需求分支 {local} 失败：{(checkout.stdout or '').strip() or 'git 退出异常'}")
            return {"created": False, "baseBranch": base_branch, "branch": local}
    # 先把基准分支拉到最新，再从它切出去；基准分支是否存在也在这一步确认。
    base_reference = git_sync_base_branch(workspace, base_branch)
    # 从远端引用切出来时不要跟踪基准分支：需求分支后面要推到它自己的远端分支。
    from_remote_ref = run_git(
        workspace, ["rev-parse", "--verify", "--quiet", f"refs/remotes/{base_reference}"],
    ).returncode == 0
    track = ["--no-track"] if from_remote_ref else []
    completed = run_git(workspace, ["checkout", "--recurse-submodules", *track, "-b", local, base_reference])
    if completed.returncode != 0:
        raise BridgeFailure(f"创建需求分支失败：{(completed.stdout or '').strip() or 'git 退出异常'}")
    return {"created": True, "baseBranch": base_branch, "branch": local}


def git_effective_base_branch(workspace: Path, base_branch: str, remote: str = "origin") -> str:
    """子项目不一定有同名基准分支：没有就退回它自己的默认分支，而不是直接建不出来。"""
    if git_branch_reference_exists(workspace, base_branch, remote):
        return base_branch
    listed = git_output(
        workspace,
        ["for-each-ref", "--format=%(refname:short)", "refs/heads", "refs/remotes"],
        "读取 Git 分支失败",
    )
    remote_names = set(git_output(workspace, ["remote"], "读取 Git 远端失败").split())
    branches = [
        name for name in (line.strip() for line in listed.splitlines())
        if name and not name.endswith("/HEAD") and name not in remote_names
    ]
    fallback = git_default_branch(workspace, branches)
    if not fallback:
        raise BridgeFailure(f"子项目里没有基准分支 {base_branch}，也找不到可用的默认分支")
    return fallback


def git_create_branch_targets(
    workspace: Path,
    base_branch: str,
    branch: str,
    targets: list[str],
    skip_root: bool = False,
) -> dict[str, Any]:
    """在根工作目录和选中的子项目里创建同一条需求分支。

    根目录失败照旧直接报错（需求还没落库，不该留下半条关联）；子项目失败只记在结果里，
    不回滚已经建好的分支——把已完成的部分撤掉比留着更难收拾。

    skip_root 用于「给已有需求补建子项目分支」：根目录早就在这条分支上，这一轮不该顺手
    切它、拉它，工作区里没提交的改动更不该被牵连。
    """
    if skip_root:
        require_git_workspace(workspace)
        remote = "origin"
        has_remote = remote in git_output(workspace, ["remote"], "读取 Git 远端失败").split()
        local = branch[len(f"{remote}/"):] if has_remote and branch.startswith(f"{remote}/") else branch
        if not git_branch_exists(workspace, local):
            raise BridgeFailure(f"根工作目录还没有需求分支 {local}，请先创建需求分支")
        result: dict[str, Any] = {"created": False, "baseBranch": base_branch, "branch": local}
    else:
        result = git_create_branch(workspace, base_branch, branch)
        local = str(result["branch"])
    records: list[dict[str, Any]] = [{
        "path": "",
        "name": workspace.name,
        "branch": local,
        "baseBranch": str(result["baseBranch"]),
        "created": bool(result["created"]),
        "skipped": skip_root,
        "error": "",
    }]
    for relative in targets:
        record: dict[str, Any] = {
            "path": relative,
            "name": relative,
            "branch": local,
            "baseBranch": "",
            "created": False,
            "error": "",
        }
        try:
            child = git_subproject_workspace_of(workspace, relative)
            if child == workspace.resolve():
                continue
            base = git_effective_base_branch(child, base_branch)
            child_result = git_create_branch(child, base, local)
            record["baseBranch"] = str(child_result["baseBranch"])
            record["created"] = bool(child_result["created"])
        except BridgeFailure as exc:
            record["error"] = str(exc)
        records.append(record)
    result["results"] = records
    return result


def git_prepare_branch_targets(
    workspace: Path,
    reference: str,
    strategy: str,
    commit_message: str,
    expected_remote_url: str,
    remote: str,
    targets: list[str],
) -> dict[str, Any]:
    """根工作目录切完需求分支后，把选中的子项目也切到同一条分支上。

    子项目里没有这条分支时跳过：不是每个工程都参与这条需求，缺分支不算失败。
    """
    result = git_prepare_branch(workspace, reference, strategy, commit_message, expected_remote_url, remote)
    branch = str(result["branch"])
    records: list[dict[str, Any]] = [{
        "path": "",
        "name": workspace.name,
        "branch": branch,
        "switched": True,
        "skipped": False,
        "error": "",
    }]
    for relative in targets:
        record: dict[str, Any] = {
            "path": relative,
            "name": relative,
            "branch": branch,
            "switched": False,
            "skipped": False,
            "error": "",
        }
        try:
            child = git_subproject_workspace_of(workspace, relative)
            if child == workspace.resolve():
                continue
            if not git_branch_reference_exists(child, branch, remote):
                record["skipped"] = True
            else:
                child_result = git_prepare_branch(child, branch, strategy, commit_message, "", remote)
                record["branch"] = str(child_result["branch"])
                record["switched"] = True
        except BridgeFailure as exc:
            record["error"] = str(exc)
        records.append(record)
    result["results"] = records
    return result


def git_repository_url_of(value: Any) -> str:
    """关联远端只接受完整的仓库地址；带空白、换行或以 - 开头的输入直接拒绝。"""
    url = str(value or "").strip()
    if not url:
        raise BridgeFailure("请先填写 Git 仓库地址")
    if len(url) > 512 or any(char.isspace() for char in url) or url.startswith("-"):
        raise BridgeFailure(f"Git 仓库地址不合法：{url}")
    if not GIT_REPOSITORY_URL_RE.fullmatch(url):
        raise BridgeFailure(f"Git 仓库地址不合法：{url}")
    return url


def git_initializable_workspace_of(value: Any) -> Path:
    """关联前目录可以还不存在：父目录必须已存在，缺的那一层由这里补上。"""
    raw = str(value or "").strip()
    if not raw:
        raise BridgeFailure("未提供 Codex 工作目录，请先在项目管理中确认当前项目的工作目录")
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        raise BridgeFailure("Codex 工作目录必须是绝对路径")
    if candidate.exists():
        return workspace_path_of(candidate)
    parent = candidate.parent
    if not parent.is_dir():
        raise BridgeFailure(f"上级目录不存在：{parent}")
    try:
        candidate.mkdir()
    except OSError as exc:
        raise BridgeFailure(f"创建项目工作目录失败：{exc}") from exc
    return workspace_path_of(candidate)


def git_workspace_check(value: Any) -> dict[str, Any]:
    """给「项目偏好设置」判断这个目录要不要初始化 Git，本身不写任何东西。"""
    raw = str(value or "").strip()
    if not raw:
        raise BridgeFailure("未提供 Codex 工作目录，请先在项目管理中确认当前项目的工作目录")
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        raise BridgeFailure("Codex 工作目录必须是绝对路径")
    resolved = candidate.resolve()
    if not resolved.is_dir():
        return {
            "workspace": str(resolved),
            "exists": False,
            "isGitRepository": False,
            "repositoryRoot": "",
            "remoteName": "origin",
            "remoteConfigured": False,
            "empty": False,
            "pendingSubmodules": [],
        }
    inside, _ = git_workspace_probe(resolved)
    if not inside:
        return {
            "workspace": str(resolved),
            "exists": True,
            "isGitRepository": False,
            "repositoryRoot": "",
            "remoteName": "origin",
            "remoteConfigured": False,
            "empty": not any(resolved.iterdir()),
            "pendingSubmodules": [],
        }
    root = run_git(resolved, ["rev-parse", "--show-toplevel"])
    return {
        "workspace": str(resolved),
        "exists": True,
        "isGitRepository": True,
        "repositoryRoot": (root.stdout or "").strip().splitlines()[-1].strip() if root.returncode == 0 else "",
        "remoteName": "origin",
        # 远端地址可能带内嵌凭据，只回传是否已配置。
        "remoteConfigured": bool(git_remote_url(resolved, "origin")),
        "empty": False,
        # 目录早就是仓库、但子模块还是空的，也要能在偏好设置里补一次初始化。
        "pendingSubmodules": git_pending_submodules(resolved),
    }


def git_adopt_remote_branch(workspace: Path, branch: str, remote: str) -> None:
    """目录里已有文件、检出会被拒时的退路。

    索引对齐远端提交，本地已有的同名文件原样留成未提交改动；
    本地缺的那些文件再从索引检出来，这样远端内容仍然完整落到磁盘上，且不覆盖任何本地文件。
    """
    git_output(workspace, ["branch", "--force", branch, f"{remote}/{branch}"], "创建本地分支失败")
    git_output(workspace, ["symbolic-ref", "HEAD", f"refs/heads/{branch}"], "切换本地分支失败")
    git_output(workspace, ["reset", "--mixed"], "对齐远端提交失败", timeout=120)
    run_git(workspace, ["branch", "--set-upstream-to", f"{remote}/{branch}", branch])
    missing = [
        line for line in git_output(workspace, ["ls-files", "-z", "--deleted"], "读取缺失文件失败", timeout=120).split("\0")
        if line
    ]
    # 一次全塞进命令行可能超出系统参数上限，按批检出。
    for start in range(0, len(missing), 200):
        git_output(workspace, ["checkout", "--", *missing[start:start + 200]], "检出远端文件失败", timeout=300)


def git_pending_submodules(workspace: Path) -> list[str]:
    """.gitmodules 里登记了、但本机还没检出内容的子模块路径。

    git submodule status 用行首的 - 标记「还没初始化」，这里只认这个标记，
    不去猜目录空不空——子模块目录本来就可能有被忽略的构建产物。
    """
    if not (workspace / ".gitmodules").is_file():
        return []
    completed = run_git(workspace, ["submodule", "status", "--recursive"], timeout=120)
    if completed.returncode != 0:
        return []
    pending: list[str] = []
    for line in (completed.stdout or "").splitlines():
        if not line.startswith("-"):
            continue
        parts = line[1:].strip().split()
        if len(parts) >= 2:
            pending.append(parts[1])
    return pending


def git_initialize_submodules(workspace: Path) -> dict[str, Any]:
    """把 .gitmodules 里还没初始化的子模块一并拉下来。

    子模块失败不该把主仓库的初始化算作失败：主仓库已经可用了，这里只把原因带回去。
    """
    pending = git_pending_submodules(workspace)
    if not pending:
        return {"submodules": [], "submoduleError": ""}
    completed = run_git(workspace, ["submodule", "update", "--init", "--recursive"], timeout=1800)
    if completed.returncode != 0:
        return {
            "submodules": pending,
            "submoduleError": (completed.stdout or "").strip() or "git 退出异常",
        }
    remaining = git_pending_submodules(workspace)
    return {
        "submodules": [path for path in pending if path not in remaining],
        "submoduleError": (
            f"以下子模块仍未初始化：{'、'.join(remaining)}" if remaining else ""
        ),
    }


def git_initialize_workspace(
    workspace: Path,
    repository_url: str,
    remote: str = "origin",
    base_branch: str = "",
) -> dict[str, Any]:
    """把还不是 Git 仓库的项目目录关联到远端：init + remote + fetch + 检出默认分支。

    目录里已有文件时不覆盖：改成把索引对齐到远端提交，本地文件留作未提交改动，
    由用户自己决定提交还是丢弃。中途失败会把这一步刚建出来的 .git 删掉，方便改地址重试。
    """
    url = git_repository_url_of(repository_url)
    if not valid_git_remote_name(remote):
        raise BridgeFailure("Git 远端名称不合法")
    if base_branch and not valid_git_branch_name(base_branch):
        raise BridgeFailure("基准分支名不合法")
    inside, _ = git_workspace_probe(workspace)
    if inside:
        raise BridgeFailure(f"项目工作目录已经是 Git 仓库：{workspace}")
    git_directory = workspace / ".git"
    created_git_directory = False
    try:
        git_output(workspace, ["init"], "初始化 Git 仓库失败")
        created_git_directory = git_directory.exists()
        git_output(workspace, ["remote", "add", remote, url], "关联 Git 远端失败")
        # 首次关联要把整个仓库拉下来，网络耗时远超普通 Git 命令。
        git_output(workspace, ["fetch", "--prune", remote], "拉取远端仓库失败", timeout=900)
        run_git(workspace, ["remote", "set-head", remote, "-a"], timeout=60)
        branch = base_branch.strip()
        if branch and run_git(workspace, ["rev-parse", "--verify", "--quiet", f"{remote}/{branch}^{{commit}}"]).returncode != 0:
            raise BridgeFailure(f"远端仓库没有基准分支：{branch}")
        if not branch:
            head = run_git(workspace, ["symbolic-ref", "--quiet", "--short", f"refs/remotes/{remote}/HEAD"])
            candidate = (head.stdout or "").strip() if head.returncode == 0 else ""
            prefix = f"{remote}/"
            branch = candidate[len(prefix):] if candidate.startswith(prefix) else ""
        if not branch:
            for candidate in ("main", "master", "develop"):
                if run_git(workspace, ["rev-parse", "--verify", "--quiet", f"{remote}/{candidate}^{{commit}}"]).returncode == 0:
                    branch = candidate
                    break
        if not branch:
            raise BridgeFailure("远端仓库没有可检出的分支，请确认仓库地址是否正确")
        adopted = run_git(workspace, ["checkout", "-b", branch, "--track", f"{remote}/{branch}"], timeout=300).returncode != 0
        if adopted:
            git_adopt_remote_branch(workspace, branch, remote)
    except BaseException:
        # 只删这一步自己建出来的 .git，工作目录里原有的文件一个都不动。
        if created_git_directory and git_directory.is_dir():
            shutil.rmtree(git_directory, ignore_errors=True)
        raise
    # 主仓库已经能用了，子模块是附带的一步：失败只回传原因，不回滚 .git。
    submodules = git_initialize_submodules(workspace)
    return {
        "workspace": str(workspace),
        "initialized": True,
        "branch": branch,
        "remoteName": remote,
        "adopted": adopted,
        "status": git_workspace_status(workspace, url, remote),
        **submodules,
    }

VERSION_RE = re.compile(r"(?<!\d)(\d+(?:\.\d+)+)")


def version_at_least(version: str, minimum: str) -> bool:
    """比较由固定探测命令返回的数字版本，不接受任意命令或版本表达式。"""
    actual = tuple(int(part) for part in version.split("."))
    expected = tuple(int(part) for part in minimum.split("."))
    length = max(len(actual), len(expected))
    return actual + (0,) * (length - len(actual)) >= expected + (0,) * (length - len(expected))


def environment_probe_status(entry: dict[str, Any], host: str = "") -> dict[str, Any]:
    """执行预设的只读版本命令；绿色状态只给已安装且版本达标的项。"""
    host = host or host_platform()
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


def build_environment_setup_prompt(
    use_git: bool, environments: list[dict[str, Any]], message: str, first_turn: bool, host: str = "",
) -> str:
    """项目偏好「预设环境」的提示词：先检测，只补装缺的，装完把版本核一遍。

    macOS 和 Windows 的命令名、包管理器、权限模型都不一样，所以清单按本机系统生成，
    只把该系统那一套命令写进去，不给执行器留自由发挥的余地。
    """
    host = host or host_platform()
    label = host_platform_label(host)
    privilege = "管理员" if host == "windows" else "sudo"
    if not first_turn:
        return wrap_bridge_context(
            [
                f"这是「预设环境」会话的续聊，本机是 {label}，继续按既定顺序把本机全局环境补齐。",
                "已经装好并且版本达标的环境不要重装、不要升级、不要改用户已有的版本管理器配置。",
                f"需要 {privilege} 权限的命令，如果当前拿不到权限，就把命令原样交给用户执行，然后等用户回话。",
                "本上下文标记闭合之后的内容，是用户本轮说的话。",
            ],
            message,
        )
    checklist = []
    if use_git:
        checklist.append(
            f"- Git：先执行 `{environment_command_for(GIT_PRESET, 'probe', host)}` 检测；未安装才装"
            f"（{environment_command_for(GIT_PRESET, 'install', host)}）。"
            "装好后顺带确认 `git config --global user.name` 与 `git config --global user.email` 是否已配置，"
            "缺了就问用户要，不要自己编。"
            "随后检查 `~/.ssh/config` 中 `Host github.com` 的 `IdentityFile`，且对应 `.pub` 文件必须是有效 SSH 公钥。"
            "已有有效 GitHub 密钥时不要重建、不要覆盖；没有有效配置时，生成新的 ed25519 密钥对"
            " `~/.ssh/id_ed25519_github_delivery_task_planner`，并在配置文件最前面写入带"
            " `delivery-task-planner GitHub SSH key` 标记的 `Host github.com` 配置块。"
            "绝不读取、展示或输出私钥；最后只输出公钥，并明确提示用户将它添加到 GitHub 账户的 SSH keys。"
        )
    for entry in environments:
        probe = environment_command_for(entry, "probe", host)
        install = environment_command_for(entry, "install", host)
        probe_text = f"`{probe}`" if probe else f"该环境在 {label} 上对应的版本命令"
        requirement = f"版本要求 {entry['requirement']}" if entry.get("requirement") else "版本由用户在偏好设置里自定义，按字面理解"
        install_text = f"（{label} 上装：{install}）" if install else f"（自定义项，按 {label} 的常规装法安装）"
        checklist.append(
            f"- {entry['label']}：先执行 {probe_text} 检测，{requirement}。"
            f"低于要求或没装才安装/升级到满足要求的版本{install_text}。"
        )
    if host == "windows":
        platform_rules = [
            "6. 命令用 PowerShell 执行；包管理器优先 winget，没有 winget 再退到 scoop / choco 或官网安装包，并把选择理由说清楚。",
            "7. 装完要开新的 PowerShell 会话或先刷新 PATH（`$env:Path = [System.Environment]::GetEnvironmentVariable('Path','Machine') "
            "+ ';' + [System.Environment]::GetEnvironmentVariable('Path','User')`）再复检，"
            "否则复检读到的是旧 PATH，会把装好的环境误判成没装上。",
            "8. Windows 上 Python 的命令是 `py -3` 或 `python`，没有 `python3`；winget 触发 UAC 弹窗时当前会话无法确认，"
            "直接把命令交给用户以管理员身份运行。",
        ]
    elif host == "macos":
        platform_rules = [
            "6. 包管理器用 Homebrew；没有 brew 就先把官方安装命令交给用户，或退到官网安装包，并把选择理由说清楚。",
            "7. Apple Silicon 的 brew 前缀是 /opt/homebrew、Intel 是 /usr/local；装完 `which` 找不到命令时，"
            "先确认对应的 bin 目录在 PATH 里再判定失败。",
            "8. 不要用 sudo 跑 brew。",
        ]
    else:
        platform_rules = [
            "6. 按发行版选包管理器（Debian/Ubuntu 用 apt，RHEL/CentOS 用 yum/dnf）；"
            "官方源版本低于要求时改用官网安装包或版本管理器，并把选择理由说清楚。",
        ]
    return wrap_bridge_context(
        [
            "这是交付任务面板「项目管理 → 偏好设置 → 高级设置 → 预设环境」发起的一次本机环境预设。",
            "它装的是本机全局环境，不属于任何项目：不要读取、修改或提交任何业务仓库的代码，"
            "也不要执行任务面板的任务拆解、执行或测试命令。",
            f"本机系统是 {label}，下面的命令已经按 {label} 给好了，照着执行，不要换成别的系统那一套。",
            f"本轮 cwd 是一个专用空目录：{environment_setup_workspace()}；只在需要落临时文件时用它。",
            "只做下面这份清单，逐项先检测再动手，并把执行过的命令和真实输出讲清楚：",
            *checklist,
            "硬约束：",
            "1. 只装缺的。检测到已安装且版本满足要求的，直接跳过并说明当前版本，"
            "绝不重装、降级或顶掉用户已有的版本管理器（nvm / nvm-windows / pyenv / asdf / conda 等）配置。",
            "2. 全局安装，不要建项目级虚拟环境。",
            f"3. 需要 {privilege} 权限而当前拿不到时不要硬闯，把命令原样交给用户执行，然后等用户回话。",
            "4. 装完再跑一次检测命令核对版本，用一个表格列出每项环境的「安装前状态 / 处理动作 / 安装后版本」。",
            "5. 清单以外的环境一律不装。",
            *platform_rules,
            "最终回复末尾单独给出「下一步」，写清还需要用户自己动手的事项；全部就绪就明说无需额外操作。",
            "本上下文标记闭合之后的内容，是用户本轮补充的说明。",
        ],
        message,
    )

def validate_planning_payload(value: Any) -> tuple[int, str, str, bool, str, str, str, str, str, bool, dict[str, Any], list[str], list[dict[str, str]], bool]:
    if not isinstance(value, dict):
        raise BridgeFailure("请求体必须是 JSON 对象")
    program_id = program_id_of(value.get("programId"))
    message = str(value.get("message") or "").strip()
    thread_id = str(value.get("threadId") or "").strip()
    selected_stage = str(value.get("stageKey") or "").strip()
    selected_module = str(value.get("moduleKey") or "").strip()
    selected_kind = str(value.get("kind") or "").strip()
    model = str(value.get("model") or "").strip()
    provider = ai_provider_of(value)
    reasoning_effort = reasoning_effort_of(value, provider)
    fast_mode = fast_mode_of(value, provider)
    requirement = planning_requirement_of(value)
    attachment_ids = value.get("attachmentIds") or []
    if not isinstance(attachment_ids, list) or len(attachment_ids) > MAX_CONVERSATION_ATTACHMENTS:
        raise BridgeFailure("附件数量无效")
    attachment_ids = [str(attachment_id).strip() for attachment_id in attachment_ids if str(attachment_id).strip()]
    chat_references = conversation_references_of(value.get("chatReferences"))
    # 只带附件不写字也是一次有效的追问，图片本身就是需求说明。
    if not message and not attachment_ids:
        raise BridgeFailure("请输入要拆解的需求")
    if len(message) > 32 * 1024:
        raise BridgeFailure("需求内容不能超过 32KB")
    if len(thread_id) > 255 or len(model) > 128:
        raise BridgeFailure("会话或模型标识无效")
    if selected_kind and selected_kind not in {"gap", "capability", "asset"}:
        raise BridgeFailure("任务类型无效")
    return (
        program_id,
        message,
        thread_id,
        bool(value.get("newConversation")),
        selected_stage,
        selected_module,
        selected_kind,
        model,
        reasoning_effort,
        fast_mode,
        requirement,
        attachment_ids,
        chat_references,
        # 只有面板上的「确认并写入」会带上这个标记，其余轮次一律是只读的预览。
        bool(value.get("confirmWrite")),
    )


def validate_requirement_testing_payload(value: Any) -> tuple[int, str, str, str, bool, str, str, bool, list[str], bool]:
    if not isinstance(value, dict):
        raise BridgeFailure("请求体必须是 JSON 对象")
    program_id = program_id_of(value.get("programId"))
    requirement_key = str(value.get("requirementKey") or "").strip()
    message = str(value.get("message") or "").strip()
    thread_id = str(value.get("threadId") or "").strip()
    model = str(value.get("model") or "").strip()
    provider = ai_provider_of(value)
    reasoning_effort = reasoning_effort_of(value, provider)
    fast_mode = fast_mode_of(value, provider)
    attachment_ids = value.get("attachmentIds") or []
    if not program_id or not requirement_key or len(requirement_key) > 64:
        raise BridgeFailure("缺少或无效的项目、需求标识")
    if not isinstance(attachment_ids, list) or len(attachment_ids) > MAX_CONVERSATION_ATTACHMENTS:
        raise BridgeFailure("附件数量无效")
    attachment_ids = [str(attachment_id).strip() for attachment_id in attachment_ids if str(attachment_id).strip()]
    if not message and not attachment_ids:
        raise BridgeFailure("请输入测试要求或上传测试资料")
    if len(message) > 32 * 1024:
        raise BridgeFailure("测试要求不能超过 32KB")
    if len(thread_id) > 255 or len(model) > 128:
        raise BridgeFailure("会话或模型标识无效")
    return program_id, requirement_key, message, thread_id, bool(value.get("newConversation")), model, reasoning_effort, fast_mode, attachment_ids, bool(value.get("testCaseOnly"))


def validate_task_testing_cases_payload(value: Any) -> tuple[int, str, str, str, bool, str, str, bool]:
    if not isinstance(value, dict):
        raise BridgeFailure("请求体必须是 JSON 对象")
    program_id = program_id_of(value.get("programId"))
    item_key = str(value.get("itemKey") or "").strip()
    message = str(value.get("message") or "").strip()
    thread_id = str(value.get("threadId") or "").strip()
    model = str(value.get("model") or "").strip()
    provider = ai_provider_of(value)
    if not item_key or len(item_key) > 64:
        raise BridgeFailure("缺少或无效的项目、任务标识")
    if len(message) > 32 * 1024:
        raise BridgeFailure("测试要求不能超过 32KB")
    if len(thread_id) > 255 or len(model) > 128:
        raise BridgeFailure("会话或模型标识无效")
    return (
        program_id, item_key, message, thread_id, bool(value.get("newConversation")), model,
        reasoning_effort_of(value, provider), fast_mode_of(value, provider),
    )


def planning_requirement_of(value: dict[str, Any]) -> dict[str, Any]:
    """Normalize the requirement a planning turn belongs to.

    拆解会话按需求分组：同一个项目下不同需求各自一条会话线，
    requirementKey 为空时退回到项目级会话（需求层落地之前的老用法）。
    """
    requirement_key = str(value.get("requirementKey") or "").strip()
    if len(requirement_key) > 64:
        raise BridgeFailure("需求标识无效")
    detail = str(value.get("requirementDetail") or "")
    if len(detail) > 32 * 1024:
        raise BridgeFailure("需求详情不能超过 32KB")
    # 简易模式直接把任务放进动作执行，专业模式由用户选起始阶段，默认梳理需求。
    start_phase = str(value.get("requirementStartPhase") or "").strip() or "requirement"
    if start_phase not in {"requirement", "development", "testing"}:
        raise BridgeFailure("起始阶段无效")
    return {
        "requirementKey": requirement_key,
        "name": str(value.get("requirementName") or "").strip()[:255],
        "detail": detail,
        "owners": str(value.get("requirementOwners") or "").strip()[:512],
        "assistants": str(value.get("requirementAssistants") or "").strip()[:512],
        "startPhase": start_phase,
        # 老客户端不带这个字段时按拆解处理，保持既有行为。
        "splitTasks": bool(value.get("requirementSplitTasks", True)),
        # 兼容已安装的旧面板字段；新面板使用 requirementPreGenerateTaskDocuments。
        "preGenerateTaskDocuments": bool(
            value.get("requirementPreGenerateTaskDocuments", value.get("requirementGenerateTaskOutline", False))
        ),
        "generatePrototype": bool(value.get("requirementGeneratePrototype")),
        "references": planning_requirement_references_of(value.get("requirementReferences")),
        "itemReferences": planning_requirement_item_references_of(value.get("requirementItemReferences")),
    }


def planning_requirement_references_of(value: Any) -> list[dict[str, str]]:
    """Normalize the earlier requirements a new requirement @-mentions.

    面板只传被 @ 的需求键和名字：正文按需从各自的大纲产物地址读取，
    不把历史大纲整段塞进提示词。
    """
    if not isinstance(value, list):
        return []
    references: list[dict[str, str]] = []
    seen: set[str] = set()
    for entry in value[:20]:
        if not isinstance(entry, dict):
            continue
        requirement_key = str(entry.get("requirementKey") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", requirement_key) or requirement_key in seen:
            continue
        seen.add(requirement_key)
        references.append({
            "requirementKey": requirement_key,
            "name": str(entry.get("name") or "").strip()[:255] or requirement_key,
        })
    return references


def planning_requirement_item_references_of(value: Any) -> list[dict[str, str]]:
    """Normalize the existing tasks a requirement detail @-mentions.

    Titles are deliberately not accepted from the browser. The planning prompt resolves
    each task key against the current project catalog before exposing its document path.
    """
    if not isinstance(value, list):
        return []
    references: list[dict[str, str]] = []
    seen: set[str] = set()
    for entry in value[:20]:
        if not isinstance(entry, dict):
            continue
        item_key = str(entry.get("itemKey") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", item_key) or item_key in seen:
            continue
        seen.add(item_key)
        references.append({"itemKey": item_key})
    return references


def conversation_references_of(value: Any) -> list[dict[str, str]]:
    """Normalize objects selected from a task or requirement chat @ menu.

    The browser can only nominate keys. The bridge later fetches every record again,
    so labels or arbitrary text supplied by a browser never become task context.
    """
    if not isinstance(value, list):
        return []
    references: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for entry in value[:MAX_CONVERSATION_REFERENCES]:
        if not isinstance(entry, dict):
            continue
        kind = str(entry.get("kind") or "").strip()
        key = str(entry.get("key") or "").strip()
        if kind == "file":
            scope = str(entry.get("scope") or "").strip()
            path = Path(key)
            if (
                scope not in {"requirement-outline", "requirement-testing", "requirement-prototype"}
                or not key
                or len(key) > 512
                or "\x00" in key
                or "\\" in key
                or path.is_absolute()
                or ".." in path.parts
                or (kind, key) in seen
            ):
                continue
            seen.add((kind, key))
            references.append({"kind": kind, "key": key, "scope": scope})
            continue
        pattern = r"[A-Za-z0-9_-]{1,64}" if kind == "requirement" else r"[A-Za-z0-9._-]{1,64}"
        if kind not in {"requirement", "task"} or not re.fullmatch(pattern, key) or (kind, key) in seen:
            continue
        seen.add((kind, key))
        references.append({"kind": kind, "key": key})
    return references


def requirement_prototype_directory_of(requirement_key: str) -> Path:
    """Return the only workspace-relative directory a requirement prototype may use."""
    value = str(requirement_key or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
        raise BridgeFailure("需求原型标识无效")
    return Path("doc") / "requirements" / value / "prototype"


def requirement_outline_path_of(requirement_key: str) -> Path:
    """Return the one workspace-relative file a requirement breakdown outline may use."""
    value = str(requirement_key or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
        raise BridgeFailure("需求标识无效")
    return Path("doc") / "requirements" / value / REQUIREMENT_OUTLINE_FILE_NAME


def requirement_document_directory_of(requirement_key: str) -> Path:
    """Return the requirement document directory for standalone deliverables."""
    value = str(requirement_key or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
        raise BridgeFailure("需求标识无效")
    return Path("doc") / "requirements" / value


def legacy_task_outline_path_of(requirement_key: str, item_key: str) -> Path:
    """Return the retired per-task outline location for one-time migration only."""
    requirement_value = str(requirement_key or "").strip()
    item_value = str(item_key or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", requirement_value):
        raise BridgeFailure("需求标识无效")
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", item_value):
        raise BridgeFailure("任务标识无效")
    return Path("doc") / "requirements" / requirement_value / item_value / REQUIREMENT_OUTLINE_FILE_NAME


def outline_file_in_workspace(workspace: Path, relative: Path) -> Path:
    """Resolve an outline path and refuse anything that escapes the project workspace."""
    path = (workspace / relative).resolve()
    try:
        path.relative_to(workspace.resolve())
    except ValueError as exc:
        raise BridgeFailure("需求大纲文件超出当前项目") from exc
    return path


def outline_document(workspace: Path, relative: Path) -> dict[str, Any]:
    """Read one outline markdown file, or report that it has not been written yet."""
    path = outline_file_in_workspace(workspace, relative)
    if not path.is_file():
        return {"path": relative.as_posix(), "exists": False, "markdown": "", "updatedAt": ""}
    if path.stat().st_size > MAX_REQUIREMENT_OUTLINE_BYTES:
        raise BridgeFailure("需求大纲超过 2 MB，无法预览")
    try:
        markdown = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise BridgeFailure("需求大纲不是 UTF-8 文本") from exc
    return {
        "path": relative.as_posix(),
        "exists": True,
        "markdown": markdown,
        "updatedAt": datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def write_outline_document(workspace: Path, relative: Path, markdown: str) -> dict[str, Any]:
    """Overwrite one outline markdown file from the task board."""
    if len(markdown.encode("utf-8")) > MAX_EDITABLE_OUTLINE_BYTES:
        raise BridgeFailure("需求大纲不能超过 512 KB")
    path = outline_file_in_workspace(workspace, relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(markdown, encoding="utf-8")
    return outline_document(workspace, relative)


def requirement_outline_document(workspace: Path, requirement_key: str) -> dict[str, Any]:
    """Read the outline markdown without letting the requirement key escape the workspace."""
    return outline_document(workspace, requirement_outline_path_of(requirement_key))


def testing_asset_directory_of(key: str) -> Path:
    """Return the workspace-relative directory the testing skills write for one requirement or task."""
    value = str(key or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", value):
        raise BridgeFailure("测试资产标识无效")
    return Path("doc") / TESTING_ASSET_ROOT / value


def document_set_entries(workspace: Path, relative_directory: Path, recursive: bool) -> list[dict[str, Any]]:
    """List the documents of one column, stable by path.

    目录里真实存在的文件都要列出来：文本文档（Markdown、纯文本、HTML）能在面板里直接预览和编辑，
    面板上传进来的 PDF、Word、图片这类文件同样属于这个栏目，只是 previewable 为假，
    面板会把它们交给附件预览或下载，而不是当文本读。
    """
    root = workspace.resolve()
    directory = (workspace / relative_directory).resolve()
    try:
        directory.relative_to(root)
    except ValueError as exc:
        raise BridgeFailure("文档目录超出当前项目") from exc
    if not directory.is_dir():
        return []
    entries: list[dict[str, Any]] = []
    for path in sorted(directory.rglob("*") if recursive else directory.glob("*")):
        # 隐藏文件是工具自己的中间产物（.DS_Store、.gitkeep），不是这个栏目的文档。
        if not path.is_file() or path.name.startswith("."):
            continue
        resolved = path.resolve()
        try:
            relative = resolved.relative_to(root)
            name = resolved.relative_to(directory).as_posix()
        except ValueError:
            continue
        stat = resolved.stat()
        entries.append({
            "path": relative.as_posix(),
            "name": name,
            "size": stat.st_size,
            "updatedAt": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat().replace("+00:00", "Z"),
            "previewable": resolved.suffix.lower() in DOCUMENT_SET_SUFFIXES,
            "contentType": mimetypes.guess_type(resolved.name)[0] or "application/octet-stream",
        })
        if len(entries) >= MAX_DOCUMENT_SET_FILES:
            break
    return entries


def document_in_set(
    workspace: Path, relative_directory: Path, raw_path: str, previewable_only: bool = True,
) -> Path:
    """Resolve one document of a column and refuse anything outside that column's directory."""
    value = str(raw_path or "").strip()
    candidate = Path(value)
    if not value or candidate.is_absolute() or ".." in candidate.parts:
        raise BridgeFailure("文档路径无效")
    if previewable_only and candidate.suffix.lower() not in DOCUMENT_SET_SUFFIXES:
        raise BridgeFailure("该文档不支持预览")
    path = (workspace / candidate).resolve()
    directory = (workspace / relative_directory).resolve()
    try:
        path.relative_to(directory)
    except ValueError as exc:
        raise BridgeFailure("文档超出当前栏目目录") from exc
    return path


def document_upload_name(raw_name: str) -> str:
    """把浏览器传上来的文件名收敛成栏目目录里的一个安全文件名。

    只取最后一段文件名，去掉路径分隔符和控制字符：上传口子决定的是「写哪个文件」，
    不能让文件名自己带着目录跳出这个栏目。
    """
    cleaned = re.sub(r"[\\/\x00-\x1f]", "", Path(str(raw_name or "").replace("\\", "/")).name).strip()
    if not cleaned or cleaned in {".", ".."} or cleaned.startswith("."):
        raise BridgeFailure("文档文件名无效")
    if len(cleaned) > 120:
        suffix = Path(cleaned).suffix[:20]
        cleaned = cleaned[: 120 - len(suffix)] + suffix
    return cleaned


def available_document_name(directory: Path, name: str) -> Path:
    """同名文档不覆盖：会话写的文档和手动上传的文档共用一个目录，重名时顺延成 名字-2.后缀。"""
    target = directory / name
    if not target.exists():
        return target
    stem, suffix = Path(name).stem, Path(name).suffix
    for index in range(2, 100):
        candidate = directory / f"{stem}-{index}{suffix}"
        if not candidate.exists():
            return candidate
    raise BridgeFailure("同名文档过多，请换一个文件名")


def html_asset_payloads(workspace: Path, boundary: Path, html_path: Path, html: str) -> list[dict[str, str]]:
    """Read the sibling stylesheets and scripts one HTML file references by relative path.

    只认相对路径、只认 css/js、只读边界目录里的文件：外链地址交给 iframe 自己去取，
    绝对路径和越界路径一律跳过，避免预览成为读工作区任意文件的口子。
    返回的 name 就是 HTML 里原样写的引用串，前端据此把对应的 link / script 标签换成内联正文。
    """
    root = workspace.resolve()
    base = (boundary if boundary.is_absolute() else workspace / boundary).resolve()
    try:
        base.relative_to(root)
    except ValueError:
        return []
    directory = html_path.resolve().parent
    assets: list[dict[str, str]] = []
    seen: set[str] = set()
    total_bytes = 0
    for match in HTML_ASSET_REFERENCE_RE.finditer(html):
        reference = next((group for group in match.groups() if group), "")
        value = reference.strip()
        if not value or value in seen:
            continue
        # 协议地址、协议相对地址、站点绝对路径和内联数据都不是同目录文件。
        if "://" in value or value.startswith(("//", "/", "#", "data:", "javascript:")):
            continue
        target = value.split("#", 1)[0].split("?", 1)[0]
        if not target:
            continue
        candidate = Path(target)
        if candidate.is_absolute() or candidate.suffix.lower() not in HTML_ASSET_SUFFIXES:
            continue
        seen.add(value)
        resolved = (directory / candidate).resolve()
        try:
            resolved.relative_to(base)
        except ValueError:
            continue
        if not resolved.is_file():
            continue
        size = resolved.stat().st_size
        if total_bytes + size > MAX_HTML_ASSET_TOTAL_BYTES:
            break
        try:
            content = resolved.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if "\x00" in content:
            continue
        total_bytes += size
        assets.append({"name": value, "content": content})
        if len(assets) >= MAX_HTML_ASSET_FILES:
            break
    return assets


def document_payload(workspace: Path, path: Path, asset_boundary: Path | None = None) -> dict[str, Any]:
    """Read one column document as UTF-8 text, or report that it has not been written yet."""
    relative = path.relative_to(workspace.resolve()).as_posix()
    if not path.exists():
        return {"path": relative, "exists": False, "content": "", "size": 0, "modifiedAt": "", "assets": []}
    if not path.is_file():
        raise BridgeFailure("文档路径不是文件")
    size = path.stat().st_size
    if size > MAX_DOCUMENT_SET_FILE_BYTES:
        raise BridgeFailure("文档超过 2 MB，无法预览")
    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise BridgeFailure("文档不是 UTF-8 文本文件") from exc
    if "\x00" in content:
        raise BridgeFailure("文档不是可预览的文本文件")
    # HTML 文档的样式和脚本可能拆在同目录的独立文件里，一并带上，否则预览只剩裸结构。
    assets = (
        html_asset_payloads(workspace, asset_boundary, path, content)
        if asset_boundary is not None and path.suffix.lower() in HTML_SUFFIXES
        else []
    )
    return {
        "path": relative,
        "exists": True,
        "content": content,
        "size": size,
        "modifiedAt": datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat().replace("+00:00", "Z"),
        "assets": assets,
    }


def document_attachment_item_key(scope: str, key: str) -> str:
    """栏目文档登记成附件时的归属标识，和会话附件、任务产物区分开。"""
    return f"__document__:{str(scope or '').strip()}:{str(key or '').strip()}"


def requirement_prototype_item_key(requirement_key: str) -> str:
    return f"__requirement_prototype__:{requirement_prototype_directory_of(requirement_key).parts[-2]}"


def requirement_prototype_executor_type(provider: str) -> str:
    # 与需求拆解会话共用持久目录表，但用独立执行器类型隔离，避免“编辑原型”续到拆解对话里。
    return f"{ai_provider_of(provider)}-prototype"


def task_testing_cases_executor_type(provider: str) -> str:
    """Keep pre-generated task test-case chats apart from task execution chats."""
    return f"{ai_provider_of(provider)}-testing-cases"


def validate_requirement_prototype_payload(value: Any, message_required: bool = False) -> tuple[int, str, str, str, str, bool]:
    if not isinstance(value, dict):
        raise BridgeFailure("请求体必须是 JSON 对象")
    program_id = program_id_of(value.get("programId"))
    requirement_key = str(value.get("requirementKey") or "").strip()
    requirement_prototype_directory_of(requirement_key)
    message = str(value.get("message") or "").strip()
    if message_required and not message:
        raise BridgeFailure("请输入原型修改要求")
    if len(message) > 32 * 1024:
        raise BridgeFailure("原型修改要求不能超过 32KB")
    thread_id = str(value.get("threadId") or "").strip()
    if len(thread_id) > 255:
        raise BridgeFailure("会话标识无效")
    provider = ai_provider_of(value)
    model = str(value.get("model") or "").strip()
    if len(model) > 128:
        raise BridgeFailure("模型标识不能超过 128 个字符")
    return program_id, requirement_key, message, thread_id, provider, model


def requirement_prototype_files(workspace: Path, requirement_key: str) -> tuple[str, list[dict[str, str]]]:
    """Read a bounded set of UTF-8 HTML files without allowing workspace escapes."""
    relative_directory = requirement_prototype_directory_of(requirement_key)
    directory = (workspace / relative_directory).resolve()
    try:
        directory.relative_to(workspace.resolve())
    except ValueError as exc:
        raise BridgeFailure("需求原型目录超出当前项目") from exc
    if not directory.is_dir():
        return relative_directory.as_posix(), []
    files: list[dict[str, str]] = []
    total_bytes = 0
    for path in sorted(directory.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in HTML_SUFFIXES:
            continue
        resolved = path.resolve()
        try:
            relative = resolved.relative_to(workspace.resolve())
            display_name = resolved.relative_to(directory).as_posix()
        except ValueError as exc:
            raise BridgeFailure("需求原型文件超出当前项目") from exc
        size = resolved.stat().st_size
        if size > MAX_REQUIREMENT_PROTOTYPE_FILE_BYTES:
            raise BridgeFailure(f"需求原型文件过大：{display_name}")
        total_bytes += size
        if total_bytes > MAX_REQUIREMENT_PROTOTYPE_TOTAL_BYTES:
            raise BridgeFailure("需求原型总大小超过 8 MB，无法预览")
        try:
            html = resolved.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise BridgeFailure(f"需求原型不是 UTF-8 HTML：{display_name}") from exc
        if "\x00" in html:
            raise BridgeFailure(f"需求原型不是可预览的 HTML：{display_name}")
        assets = html_asset_payloads(workspace, directory, resolved, html)
        files.append({"path": relative.as_posix(), "name": display_name, "html": html, "assets": assets})
        if len(files) >= MAX_REQUIREMENT_PROTOTYPE_FILES:
            break
    return relative_directory.as_posix(), files


def prototype_session_detail_digest(rows: list[dict[str, Any]], thread_id: str) -> str:
    """取出这条原型会话上次发过的需求正文指纹；取不到就当正文变过，重发一份更安全。"""
    row = next((item for item in rows if str(item.get("threadId") or "") == thread_id), None)
    metadata = row.get("metadata") if isinstance(row, dict) and isinstance(row.get("metadata"), dict) else {}
    return str(metadata.get("detailDigest") or "")


def build_requirement_prototype_prompt(
    program_id: int,
    requirement: dict[str, Any],
    message: str,
    workspace: Path,
    editing: bool = False,
    follow_up: bool = False,
    include_detail: bool = False,
) -> str:
    requirement_key = str(requirement.get("requirementKey") or "").strip()
    prototype_path = requirement_prototype_directory_of(requirement_key).as_posix()
    if follow_up:
        # 续聊只保留写入范围这条红线和会变的需求正文；页面本身就在目录里，模型自己读得到。
        return wrap_bridge_context(
            [
                "这是同一条需求原型会话的追加回合：继续在当前工作区调整这套 HTML 原型，"
                "保留未被本轮要求修改的内容，不要推倒重来。",
                workspace_instruction(workspace),
                f"项目 program_id: {program_id}",
                f"需求键: {requirement_key}",
                *(
                    ["需求详情（用户已在面板上改过，以这一版为准）:", str(requirement.get("detail") or "（未填写）")]
                    if include_detail
                    else ["需求详情与本会话首轮一致，没有变化；若上下文里已经找不到，直接读原型目录下现有页面接回来。"]
                ),
                f"原型目录（唯一允许写入的目录）: `{prototype_path}/`。只能创建或修改该目录下的 UTF-8 `.html` / `.htm` 文件，"
                "不得修改业务代码、配置、依赖或该目录以外的文件。",
                "页面仍需可独立在浏览器打开，使用内联 CSS/JS 或本地无依赖资源，不引用远程资源。",
                "完成后在最终回复列出改动过的相对路径和改动摘要。",
            ],
            message or "请按上述要求调整现有 HTML 原型。",
        )
    context_lines = [
        "这是交付任务面板的需求 HTML 原型任务。直接在当前工作区完成，不要只给建议。",
        workspace_instruction(workspace),
        f"项目 program_id: {program_id}",
        f"需求键: {requirement_key}",
        f"需求名称: {str(requirement.get('name') or '未命名')[:255]}",
        "需求详情:",
        str(requirement.get("detail") or "（未填写）"),
        "",
        f"原型目录（唯一允许写入的目录）: `{prototype_path}/`。",
        "只能创建或修改该目录下的 UTF-8 `.html` / `.htm` 文件，不得修改业务代码、配置、依赖或该目录以外的文件。",
        "按功能模块拆分页面；每个文件应可独立在浏览器打开，使用内联 CSS/JS 或本地无依赖资源，不引用远程资源。",
        "完成后核对至少一个 HTML 文件存在，并在最终回复列出相对路径和改动摘要。",
    ]
    if editing:
        context_lines.insert(0, "这是已有需求原型的编辑回合，应保留未被本轮要求修改的内容。")
    return wrap_bridge_context(context_lines, message or "请根据上述需求生成 HTML 原型。")


def attachment_marker(attachments: list[dict[str, Any]]) -> str:
    attachment_ids = [str(attachment.get("id") or "") for attachment in attachments]
    return f"<!-- delivery-task-attachments:{','.join(attachment_ids)} -->" if attachment_ids else ""


def message_with_attachments(message: str, attachments: list[dict[str, Any]]) -> str:
    """Add file references for Codex without leaking bridge-only context into chat history."""
    text = message.strip() or "请查看随附文件并继续处理。"
    if not attachments:
        return text
    lines = ["", "<delivery-task-attachments>", "随附文件已经保存到当前工作区："]
    for attachment in attachments:
        name = str(attachment.get("name") or "附件")
        if attachment.get("isImage"):
            lines.append(f"- 图片：{name}（已作为图片输入传入）")
        else:
            lines.append(f"- 文件：{name}，路径：{attachment.get('relativePath') or attachment.get('path')}")
    lines.extend(["</delivery-task-attachments>", attachment_marker(attachments)])
    return "\n".join(lines)


def attachment_ids_from_text(text: str) -> list[str]:
    match = ATTACHMENT_MARKER_RE.search(text)
    return match.group(1).split(",") if match else []


def text_without_attachment_context(text: str) -> str:
    return ATTACHMENT_MARKER_RE.sub("", BRIDGE_CONTEXT_RE.sub("", ATTACHMENT_CONTEXT_RE.sub("", text))).strip()


def validate_execute_payload(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BridgeFailure("请求体必须是 JSON 对象")
    program_id = program_id_of(value.get("programId"))
    task = value.get("task")
    if not isinstance(task, dict):
        raise BridgeFailure("缺少项目或任务")
    required = ("itemKey", "title", "version")
    if any(not task.get(key) for key in required):
        raise BridgeFailure("任务缺少 itemKey、title 或 version")
    status = str(task.get("status") or "")
    if status == "done":
        raise BridgeFailure("已完成任务不能再次执行")
    normalized = dict(value)
    normalized.pop("bizLine", None)
    normalized["programId"] = program_id
    normalized["task"] = dict(task)
    model = str(value.get("model") or "").strip()
    if len(model) > 128:
        raise BridgeFailure("模型标识不能超过 128 个字符")
    normalized["model"] = model
    normalized["provider"] = ai_provider_of(value)
    normalized["reasoningEffort"] = reasoning_effort_of(value, normalized["provider"])
    normalized["fastMode"] = fast_mode_of(value, normalized["provider"])
    follow_up = str(value.get("followUp") or "").strip()
    if len(follow_up) > 32 * 1024:
        raise BridgeFailure("追加要求不能超过 32KB")
    normalized["followUp"] = follow_up
    normalized["conversationReferences"] = conversation_references_of(value.get("conversationReferences"))
    execution_constraints = str(value.get("executionConstraints") or "").strip()
    if len(execution_constraints) > 32 * 1024:
        raise BridgeFailure("任务约束条件说明不能超过 32KB")
    normalized["executionConstraints"] = execution_constraints
    attachments = value.get("followUpAttachments") or []
    if not isinstance(attachments, list) or len(attachments) > MAX_CONVERSATION_ATTACHMENTS:
        raise BridgeFailure("附件数量无效")
    normalized["followUpAttachments"] = attachments
    return normalized


def validate_conversation_payload(value: Any) -> tuple[int, str, str, str, bool, list[str], str, str, bool, list[dict[str, str]]]:
    if not isinstance(value, dict):
        raise BridgeFailure("请求体必须是 JSON 对象")
    program_id = program_id_of(value.get("programId"))
    item_key = str(value.get("itemKey") or "").strip()
    message = str(value.get("message") or "").strip()
    if not item_key:
        raise BridgeFailure("缺少项目或任务标识")
    if len(message) > 32 * 1024:
        raise BridgeFailure("消息不能超过 32KB")
    thread_id = str(value.get("threadId") or "").strip()
    if len(thread_id) > 255:
        raise BridgeFailure("会话标识无效")
    raw_attachment_ids = value.get("attachmentIds") or []
    if not isinstance(raw_attachment_ids, list) or len(raw_attachment_ids) > MAX_CONVERSATION_ATTACHMENTS:
        raise BridgeFailure("附件数量无效")
    attachment_ids = [str(attachment_id or "").strip() for attachment_id in raw_attachment_ids]
    if any(not re.fullmatch(r"[A-Za-z0-9_-]{16,80}", attachment_id) for attachment_id in attachment_ids):
        raise BridgeFailure("附件标识无效")
    if len(set(attachment_ids)) != len(attachment_ids):
        raise BridgeFailure("附件不能重复")
    if not message and not attachment_ids:
        raise BridgeFailure("请输入要发送的内容或添加附件")
    model = str(value.get("model") or "").strip()
    if len(model) > 128:
        raise BridgeFailure("模型标识不能超过 128 个字符")
    provider = ai_provider_of(value)
    reasoning_effort = reasoning_effort_of(value, provider)
    fast_mode = fast_mode_of(value, provider)
    references = conversation_references_of(value.get("references"))
    return (
        program_id,
        item_key,
        message,
        thread_id,
        bool(value.get("newConversation")),
        attachment_ids,
        model,
        reasoning_effort,
        fast_mode,
        references,
    )


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def next_conversation_version(binding: dict[str, Any] | None) -> int:
    """Return the suffix number for the next thread, including compacted history."""
    metadata = (binding or {}).get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    stored_version = metadata.get("nextConversationVersion")
    try:
        if int(stored_version) >= 0:
            return int(stored_version)
    except (TypeError, ValueError):
        pass
    # Existing bindings do not have a counter yet. Their retained thread catalog
    # gives the correct next version until the first metadata update persists it.
    return len(conversation_catalog(binding))


def conversation_title(task: dict[str, Any], binding: dict[str, Any] | None = None) -> str:
    """Name the first Codex thread after its task, then use ascending versions."""
    base = " ".join(str(task.get("title") or "Codex 会话").split()) or "Codex 会话"
    version = next_conversation_version(binding)
    if version == 0:
        return base[:80]
    suffix = f" V0.0.{version}"
    return f"{base[: 80 - len(suffix)].rstrip()}{suffix}"


def conversation_catalog(binding: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Read the compact per-task Codex thread directory, including legacy bindings."""
    if not isinstance(binding, dict):
        return []
    metadata = binding.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    raw = metadata.get("conversations")
    catalog: list[dict[str, Any]] = []
    seen: set[str] = set()
    if isinstance(raw, list):
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            thread_id = str(entry.get("threadId") or "").strip()
            if not thread_id or thread_id in seen:
                continue
            seen.add(thread_id)
            catalog.append(
                {
                    "threadId": thread_id,
                    "title": str(entry.get("title") or "Codex 会话")[:80],
                    "createdAt": str(entry.get("createdAt") or ""),
                    "updatedAt": str(entry.get("updatedAt") or ""),
                    "status": str(entry.get("status") or "completed"),
                    "phase": str(entry.get("phase") or binding.get("phase") or "requirement"),
                    "progress": int(entry.get("progress") or binding.get("progress") or 0),
                }
            )
    legacy_thread_id = str(binding.get("externalSessionId") or "").strip()
    if legacy_thread_id and legacy_thread_id not in seen:
        timestamp = str(binding.get("updatedAt") or "")
        catalog.append(
            {
                "threadId": legacy_thread_id,
                "title": "Codex 会话",
                "createdAt": timestamp,
                "updatedAt": timestamp,
                "status": str(binding.get("status") or "completed"),
                "phase": str(binding.get("phase") or "requirement"),
                "progress": int(binding.get("progress") or 0),
            }
        )
    return catalog[:MAX_CONVERSATIONS_PER_TASK]


def turn_already_finished(error: Exception) -> bool:
    """Codex 在回合结束后会拒绝 interrupt。这不是停止失败，只是这一下点晚了。"""
    return "no active turn" in str(error).lower()


def conversation_metadata(
    binding: dict[str, Any] | None,
    thread_id: str,
    turn_id: str = "",
    turn_status: str = "",
    title: str = "",
    phase: str = "",
) -> dict[str, Any]:
    """Merge a thread update without losing the rest of a task's conversation history."""
    raw_metadata = (binding or {}).get("metadata")
    metadata = dict(raw_metadata) if isinstance(raw_metadata, dict) else {}
    now = utc_now()
    catalog = conversation_catalog(binding)
    previous_version = next_conversation_version(binding)
    entry = next((candidate for candidate in catalog if candidate["threadId"] == thread_id), None)
    conversation_phase = phase or str((binding or {}).get("phase") or "requirement")
    if entry is None:
        entry = {
            "threadId": thread_id,
            "title": title or "Codex 会话",
            "createdAt": now,
            "updatedAt": now,
            "status": turn_status or "running",
            "phase": conversation_phase,
            "progress": int((binding or {}).get("progress") or 0),
        }
        catalog.append(entry)
        next_version = previous_version + 1
    else:
        entry["title"] = title or entry["title"]
        entry["updatedAt"] = now
        if turn_status:
            entry["status"] = turn_status
        next_version = previous_version
    entry["phase"] = phase or str((binding or {}).get("phase") or entry.get("phase") or "requirement")
    entry["progress"] = 100 if turn_status == "completed" else int((binding or {}).get("progress") or entry.get("progress") or 0)
    if not entry.get("createdAt"):
        entry["createdAt"] = now
    entry["title"] = str(entry.get("title") or "Codex 会话")[:80]
    entry["updatedAt"] = now
    catalog.sort(key=lambda candidate: str(candidate.get("updatedAt") or ""), reverse=True)
    metadata["conversations"] = catalog[:MAX_CONVERSATIONS_PER_TASK]
    metadata["nextConversationVersion"] = next_version
    metadata["threadId"] = thread_id
    if turn_id:
        metadata["turnId"] = turn_id
    if turn_status:
        metadata["turnStatus"] = turn_status
    metadata["workspace"] = metadata.get("workspace") or ""
    return metadata


def merged_conversation_catalog(bindings: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Merge catalogs from all execution phases while retaining each thread's owner.

    A task can move from requirement grooming to action and then testing. Its
    execution-session rows are phase-scoped, while the task chat sidebar must
    present every retained conversation for that task.
    """
    entries: dict[str, dict[str, Any]] = {}
    owners: dict[str, dict[str, Any]] = {}
    for binding in bindings:
        for entry in conversation_catalog(binding):
            thread_id = str(entry.get("threadId") or "")
            if not thread_id:
                continue
            previous = entries.get(thread_id)
            # 同一条 thread 可能出现在多行会话记录的目录里；真正持有它的是 externalSessionId
            # 指向它的那一行，归属执行器只能按这一行判定，否则跨工具会读错缓存。
            owns_thread = str(binding.get("externalSessionId") or "") == thread_id
            previous_owns_thread = str((owners.get(thread_id) or {}).get("externalSessionId") or "") == thread_id
            if previous is not None and previous_owns_thread and not owns_thread:
                continue
            if (
                previous is None
                or (owns_thread and not previous_owns_thread)
                or str(entry.get("updatedAt") or "") >= str(previous.get("updatedAt") or "")
            ):
                entries[thread_id] = {**entry, "executorType": executor_provider_of(binding)}
                owners[thread_id] = binding
    catalog = sorted(entries.values(), key=lambda entry: str(entry.get("updatedAt") or ""), reverse=True)
    return catalog, owners


def runtime_config_from_payload(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BridgeFailure("请求参数无效")
    runtime_config = value.get(RUNTIME_CONFIG_KEY)
    if not isinstance(runtime_config, dict):
        raise BridgeFailure("任务面板身份上下文缺失")
    return runtime_config


def assert_runtime_project(config: dict[str, Any], program_id: int) -> None:
    runtime_value = config.get("_project_id")
    runtime_program_id = program_id_of(runtime_value) if runtime_value not in (None, "") else 0
    if program_id and runtime_program_id and runtime_program_id != program_id:
        raise BridgeFailure("当前请求项目与任务面板入口项目不一致")


def codex_environment(
    config: dict[str, Any], program_id: int, write_allowed: bool = True, provider: str = "codex",
) -> dict[str, str]:
    assert_runtime_project(config, program_id)
    # Claude 一律以 bypass 身份启动：CLI 侧带 --dangerously-skip-permissions，
    # 插件侧也不再降级成只读，避免预览轮拿不到写文件（需求大纲）所需的权限。
    if ai_provider_of(provider) == "claude":
        write_allowed = True
    return {
        # 需求梳理的预览轮次把插件降级成只读：提示词之外再加一道工具级的硬拦截。
        planner.RUNTIME_WRITE_MODE_ENV: "write" if write_allowed else "preview",
        planner.RUNTIME_PROJECT_ID_ENV: str(program_id),
        planner.RUNTIME_TOKEN_ENV: str(config.get("key") or ""),
        planner.RUNTIME_TOKEN_HEADER_ENV: str(config.get("key_header") or "token"),
        planner.RUNTIME_USER_ID_ENV: str(config.get("user_id") or "task-executor"),
        planner.RUNTIME_API_URL_ENV: str(config.get("api_url") or ""),
    }


def biz_line_of(value: Any) -> str:
    # Accepted for backwards-compatible clients only. Project-scoped work never
    # uses this value to resolve or authorize a project.
    return str(value.get("bizLine") or "") if isinstance(value, dict) else ""


def scoped_config(config: dict[str, Any], biz_line: str = "") -> dict[str, Any]:
    return config


def config_biz_line(config: dict[str, Any]) -> str:
    return ""


def request_scoped_config(config: dict[str, Any] | None, biz_line: str, program_id: int) -> dict[str, Any]:
    if config is None:
        raise BridgeFailure("任务面板身份上下文缺失")
    assert_runtime_project(config, program_id)
    return config


def task_identity(biz_line: str, program_id: int, item_key: str) -> tuple[str, int, str]:
    return "", program_id, item_key


def validate_task_identity(value: Any) -> tuple[str, int, str]:
    if not isinstance(value, dict):
        raise BridgeFailure("请求参数无效")
    program_id = program_id_of(value.get("programId"))
    item_key = str(value.get("itemKey") or "").strip()
    if not item_key:
        raise BridgeFailure("缺少项目或任务标识")
    return "", program_id, item_key


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


REASONING_SUMMARY_METHODS = {"item/reasoning/summaryPartAdded", "item/reasoning/summaryTextDelta"}
JOURNAL_METHODS = {"turn/started", "turn/completed", "item/started", "item/completed"} | REASONING_SUMMARY_METHODS


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


class AppServerClient:
    def __init__(self, workspace: Path, event_callback: Any = None, environment: dict[str, str] | None = None):
        self.workspace = workspace
        self.event_callback = event_callback
        process_environment = os.environ.copy()
        process_environment.update(environment or {})
        codex_cli = provision_codex_cli()
        if not codex_cli:
            raise BridgeFailure("未找到 Codex CLI 或 Codex Desktop 资源目录中的可执行文件")
        self.process = subprocess.Popen(
            [codex_cli, "app-server"],
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
        parts: list[dict[str, str]] = [{"type": "text", "text": text}]
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
        while self.process.poll() is None:
            now = time.monotonic()
            if now >= next_poll:
                status = self.read_turn_status(self.thread_id, turn_id, self.next_request_id())
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


CLAUDE_TRANSCRIPTS = ClaudeTranscriptStore()
# Claude 的工具调用要还原成面板认识的条目类型，才能和 Codex 的会话长得一样。
CLAUDE_FILE_TOOLS = {"Edit", "MultiEdit", "Write", "NotebookEdit"}
CLAUDE_COMMAND_TOOLS = {"Bash", "BashOutput", "KillShell"}
# Codex 的过程是一条条 shell 命令，面板能从命令本身看出"这一步在读文件还是在检索"。
# Claude 用的是具名工具，命令行里没有对应的字面量，所以这里直接把语义标出来。
CLAUDE_READ_TOOLS = {"Read", "NotebookRead"}
CLAUDE_SEARCH_TOOLS = {"Grep", "Glob"}


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


def create_ai_client(provider: str, workspace: Path, event_callback: Any = None, environment: dict[str, str] | None = None) -> AppServerClient | ClaudeCLIClient:
    if provider == "claude":
        return ClaudeCLIClient(workspace, event_callback, environment)
    return AppServerClient(workspace, event_callback, environment)


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
                    "client": create_ai_client(provider, workspace, environment=environment),
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


class ConversationAttachmentStore:
    """Keeps browser uploads inside the workspace so the Codex sandbox can read them."""

    def __init__(self, workspace: Path):
        self.root = workspace / ".codex" / ATTACHMENT_DIRECTORY_NAME
        self.lock = threading.Lock()

    @staticmethod
    def _safe_name(name: str) -> str:
        cleaned = Path(name).name.strip().replace("\x00", "")
        return cleaned[:160] or "attachment"

    @staticmethod
    def _attachment_id(value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,80}", value):
            raise BridgeFailure("附件标识无效")
        return value

    def _manifest_path(self, attachment_id: str) -> Path:
        return self.root / f"{self._attachment_id(attachment_id)}.json"

    def save(self, biz_line: str, program_id: int, item_key: str, uploads: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not uploads or len(uploads) > MAX_CONVERSATION_ATTACHMENTS:
            raise BridgeFailure(f"一次最多上传 {MAX_CONVERSATION_ATTACHMENTS} 个附件")
        stored: list[dict[str, Any]] = []
        with self.lock:
            self.root.mkdir(parents=True, exist_ok=True)
            for upload in uploads:
                name = self._safe_name(str(upload.get("name") or ""))
                data = upload.get("data")
                if not isinstance(data, bytes) or not data:
                    raise BridgeFailure(f"附件 {name} 为空")
                if len(data) > MAX_CONVERSATION_ATTACHMENT_BYTES:
                    raise BridgeFailure(f"附件 {name} 超过 10 MB")
                suffix = Path(name).suffix.lower()
                content_type = str(upload.get("contentType") or mimetypes.guess_type(name)[0] or "application/octet-stream")[:128]
                is_image = content_type.startswith("image/") and suffix in IMAGE_SUFFIXES
                attachment_id = secrets.token_urlsafe(24)
                stored_name = f"{attachment_id}{suffix}" if suffix else attachment_id
                path = self.root / stored_name
                path.write_bytes(data)
                manifest = {
                    "id": attachment_id,
                    "programId": program_id,
                    "itemKey": item_key,
                    "name": name,
                    "contentType": content_type,
                    "size": len(data),
                    "isImage": is_image,
                    "fileName": stored_name,
                    "createdAt": utc_now(),
                }
                self._manifest_path(attachment_id).write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
                stored.append(self._public(manifest))
        return stored

    def save_generated_image(
        self,
        biz_line: str,
        program_id: int,
        item_key: str,
        thread_id: str,
        turn_id: str,
        call_id: str,
        encoded: str,
    ) -> dict[str, Any]:
        attachment_id = hashlib.sha256(
            f"generated\0{program_id}\0{item_key}\0{thread_id}\0{turn_id}\0{call_id}".encode("utf-8")
        ).hexdigest()[:40]
        manifest_path = self._manifest_path(attachment_id)
        if manifest_path.exists():
            try:
                return self._public(json.loads(manifest_path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                pass
        try:
            data = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise BridgeFailure("Codex 生成的图片数据无效") from exc
        content_type, suffix = image_format(data)
        if not content_type or not suffix:
            raise BridgeFailure("Codex 生成的图片格式不受支持")
        if len(data) > MAX_WORKSPACE_ARTIFACT_BYTES:
            raise BridgeFailure("Codex 生成的图片超过 50 MB")
        stored_name = f"{attachment_id}{suffix}"
        manifest = {
            "id": attachment_id,
            "programId": program_id,
            "itemKey": item_key,
            "threadId": thread_id,
            "turnId": turn_id,
            "callId": call_id,
            "name": f"codex-generated-{turn_id[-8:] or attachment_id[:8]}{suffix}",
            "contentType": content_type,
            "size": len(data),
            "isImage": True,
            "fileName": stored_name,
            "source": "codex-image-generation",
            "createdAt": utc_now(),
        }
        with self.lock:
            self.root.mkdir(parents=True, exist_ok=True)
            path = self.root / stored_name
            if not path.exists():
                path.write_bytes(data)
            self._manifest_path(attachment_id).write_text(
                json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
            )
        return self._public(manifest)

    def generated_for_turn(self, program_id: int, item_key: str, thread_id: str, turn_id: str) -> list[dict[str, Any]]:
        if not self.root.exists():
            return []
        attachments: list[dict[str, Any]] = []
        for manifest_path in self.root.glob("*.json"):
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if (
                manifest.get("source") == "codex-image-generation"
                and manifest.get("programId") == program_id
                and manifest.get("itemKey") == item_key
                and manifest.get("threadId") == thread_id
                and manifest.get("turnId") == turn_id
            ):
                attachments.append(self._public(manifest))
        return sorted(attachments, key=lambda item: item["id"])

    def recover_generated_images(self, biz_line: str, program_id: int, item_key: str, thread_id: str) -> None:
        session_path = next((
            path for path in (Path.home() / ".codex" / "sessions").glob(f"**/*{thread_id}.jsonl") if path.is_file()
        ), None)
        if session_path is None:
            return
        current_turn_id = ""
        try:
            lines = session_path.open("r", encoding="utf-8")
        except OSError:
            return
        with lines:
            for line in lines:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = event.get("payload") or {}
                if event.get("type") != "event_msg" or not isinstance(payload, dict):
                    continue
                event_type = str(payload.get("type") or "")
                if event_type == "task_started":
                    current_turn_id = str(payload.get("turn_id") or "")
                    continue
                if event_type != "image_generation_end" or not current_turn_id:
                    continue
                result = str(payload.get("result") or "")
                call_id = str(payload.get("call_id") or "")
                if result and call_id:
                    try:
                        self.save_generated_image(
                            biz_line, program_id, item_key, thread_id, current_turn_id, call_id, result
                        )
                    except BridgeFailure:
                        continue

    def resolve(self, program_id: int, item_key: str, attachment_ids: list[str]) -> list[dict[str, Any]]:
        attachments: list[dict[str, Any]] = []
        for attachment_id in attachment_ids:
            manifest_path = self._manifest_path(attachment_id)
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise BridgeFailure("附件不存在或已失效") from exc
            if manifest.get("programId") != program_id or manifest.get("itemKey") != item_key:
                raise BridgeFailure("附件不属于当前任务")
            file_name = str(manifest.get("fileName") or "")
            path = (self.root / file_name).resolve()
            if path.parent != self.root.resolve() or not path.is_file():
                raise BridgeFailure("附件不存在或已失效")
            attachment = dict(manifest)
            attachment["path"] = str(path)
            attachment["relativePath"] = str(path.relative_to(self.root.parent.parent.resolve()))
            attachments.append(attachment)
        return attachments

    def download(self, attachment_id: str) -> tuple[dict[str, Any], Path]:
        manifest_path = self._manifest_path(attachment_id)
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BridgeFailure("附件不存在或已失效") from exc
        path = (self.root / str(manifest.get("fileName") or "")).resolve()
        if path.parent != self.root.resolve() or not path.is_file():
            raise BridgeFailure("附件不存在或已失效")
        return manifest, path

    @staticmethod
    def _public(manifest: dict[str, Any]) -> dict[str, Any]:
        attachment_id = str(manifest.get("id") or "")
        return {
            "id": attachment_id,
            "name": str(manifest.get("name") or "附件"),
            "contentType": str(manifest.get("contentType") or "application/octet-stream"),
            "size": int(manifest.get("size") or 0),
            "isImage": bool(manifest.get("isImage")),
            "relativePath": str(manifest.get("relativePath") or ""),
            "url": f"/v1/codex/attachments/{attachment_id}",
        }


class WorkspaceArtifactStore:
    """Registers Codex-created workspace files without copying them into the task service."""

    def __init__(self, workspace: Path):
        self.workspace = workspace.resolve()
        self.root = self.workspace / ".codex" / ARTIFACT_DIRECTORY_NAME
        self.lock = threading.Lock()
        self.index_cache: dict[str, list[str]] | None = None
        self.index_at = 0.0

    def _file_index(self) -> dict[str, list[str]]:
        """文件名 → 工作区相对路径。用 git 的清单，天然排除 .gitignore 里的构建产物。"""
        now = time.monotonic()
        if self.index_cache is not None and now - self.index_at < WORKSPACE_FILE_INDEX_TTL_SECONDS:
            return self.index_cache
        index: dict[str, list[str]] = {}
        try:
            completed = run_git(
                self.workspace,
                ["ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                timeout=30,
            )
            entries = (completed.stdout or "").split("\0") if completed.returncode == 0 else []
        except BridgeFailure:
            entries = []
        for entry in entries[:MAX_WORKSPACE_FILE_INDEX_ENTRIES]:
            relative_text = entry.strip()
            if not relative_text:
                continue
            index.setdefault(relative_text.rsplit("/", 1)[-1], []).append(relative_text)
        self.index_cache = index
        self.index_at = now
        return index

    def _locate(self, raw_path: str) -> tuple[Path, Path]:
        """把回复里的链接落到工作区里的真实文件。"""
        candidate = unquote(str(raw_path or "").strip())
        # 链接常带行号（`foo.kt:42`、`foo.kt#L42`），指的还是同一份文件。
        candidate = re.sub(r"(?::\d+){1,2}$", "", candidate.split("#", 1)[0]).strip()
        if not candidate:
            raise BridgeFailure("产物路径为空")
        # 外部链接、锚点不是工作区文件，绝不能靠文件名反查蒙到一份同名文件上。
        if candidate.startswith("//") or re.match(r"^[a-zA-Z][a-zA-Z\d+.-]*:", candidate):
            raise BridgeFailure("链接不是项目内文件")
        try:
            return self._resolve(candidate)
        except BridgeFailure:
            pass
        # 只写了文件名或写了一截尾部路径：全工作区反查，命中唯一一份才认，
        # 同名多份时宁可不给预览，也不能点开另一个目录下的同名文件。
        normalized = re.sub(r"^(?:\./)+", "", candidate.replace("\\", "/")).lstrip("/")
        if not normalized or ".." in normalized.split("/"):
            raise BridgeFailure("产物路径无效")
        matches = self._file_index().get(normalized.rsplit("/", 1)[-1]) or []
        if "/" in normalized:
            matches = [item for item in matches if item == normalized or item.endswith(f"/{normalized}")]
        if len(matches) != 1:
            raise BridgeFailure("产物文件不存在" if not matches else "同名文件不唯一，无法确定预览目标")
        return self._resolve(matches[0])

    def _resolve(self, raw_path: str) -> tuple[Path, Path]:
        candidate = Path(raw_path.strip())
        if not candidate.parts:
            raise BridgeFailure("产物路径为空")
        resolved = candidate.resolve() if candidate.is_absolute() else (self.workspace / candidate).resolve()
        try:
            relative = resolved.relative_to(self.workspace)
        except ValueError as exc:
            raise BridgeFailure("产物路径超出当前项目") from exc
        if any(part in EXCLUDED_ARTIFACT_PARTS for part in relative.parts):
            raise BridgeFailure("该项目路径不允许作为聊天附件")
        if relative.name.lower() in EXCLUDED_ARTIFACT_NAMES or relative.name.lower().startswith(".env."):
            raise BridgeFailure("敏感配置文件不允许作为聊天附件")
        if not resolved.is_file():
            raise BridgeFailure("产物文件不存在")
        size = resolved.stat().st_size
        if size <= 0 or size > MAX_WORKSPACE_ARTIFACT_BYTES:
            raise BridgeFailure("产物文件为空或超过 50 MB")
        return resolved, relative

    def register(self, biz_line: str, program_id: int, item_key: str, paths: list[str]) -> list[dict[str, Any]]:
        registered: list[dict[str, Any]] = []
        seen: set[str] = set()
        with self.lock:
            self.root.mkdir(parents=True, exist_ok=True)
            for raw_path in paths:
                try:
                    path, relative = self._locate(raw_path)
                except BridgeFailure:
                    continue
                relative_text = relative.as_posix()
                if relative_text in seen:
                    continue
                seen.add(relative_text)
                attachment_id = hashlib.sha256(
                    f"{program_id}\0{item_key}\0{relative_text}".encode("utf-8")
                ).hexdigest()[:40]
                content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                manifest = {
                    "id": attachment_id,
                    "programId": program_id,
                    "itemKey": item_key,
                    "name": path.name,
                    "relativePath": relative_text,
                    "contentType": content_type,
                    "size": path.stat().st_size,
                    "isImage": content_type.startswith("image/") and path.suffix.lower() in IMAGE_SUFFIXES,
                    "createdAt": utc_now(),
                }
                (self.root / f"{attachment_id}.json").write_text(
                    json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
                )
                registered.append(self._public(manifest))
        return registered

    def download(self, artifact_id: str) -> tuple[dict[str, Any], Path]:
        if not re.fullmatch(r"[a-f0-9]{40}", artifact_id):
            raise BridgeFailure("产物标识无效")
        try:
            manifest = json.loads((self.root / f"{artifact_id}.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BridgeFailure("产物不存在或已失效") from exc
        path, relative = self._resolve(str(manifest.get("relativePath") or ""))
        if relative.as_posix() != manifest.get("relativePath"):
            raise BridgeFailure("产物路径无效")
        return manifest, path

    @staticmethod
    def _public(manifest: dict[str, Any]) -> dict[str, Any]:
        artifact_id = str(manifest.get("id") or "")
        return {
            "id": artifact_id,
            "name": str(manifest.get("name") or "产物"),
            "contentType": str(manifest.get("contentType") or "application/octet-stream"),
            "size": int(manifest.get("size") or 0),
            "isImage": bool(manifest.get("isImage")),
            "relativePath": str(manifest.get("relativePath") or ""),
            "url": f"/v1/codex/artifacts/{artifact_id}",
        }


class ExecutionBridge:
    def __init__(
        self,
        workspace: Path,
        progress: ProgressStore | None = None,
        pending_session_syncs: PendingSessionSyncStore | None = None,
    ):
        self.workspace = workspace.resolve()
        self.active: set[tuple[str, int, str]] = set()
        self.active_runs: dict[tuple[str, int, str], dict[str, Any]] = {}
        self.active_sequences: set[str] = set()
        self.sequence_tasks: set[tuple[int, str]] = set()
        self.batch_tasks: set[tuple[int, str]] = set()
        # Queue-local dependency overrides are only used after a completed
        # review marks an interrupted task as ignorable. They never affect a
        # direct task execution request.
        self.sequence_satisfied: dict[str, set[str]] = {}
        self.batch_satisfied: dict[str, set[str]] = {}
        # 用户在任务进度里点「全部停止」后，队列线程要能在下一个检查点自己收摊：
        # 中断当前回合只结束正在跑的那一条，后面排队的任务得靠这两张表拦住。
        self.queue_programs: dict[str, int] = {}
        self.cancelled_queues: set[str] = set()
        self.lock = threading.Lock()
        self.progress = progress or ProgressStore()
        self.pending_session_syncs = pending_session_syncs or PendingSessionSyncStore()
        self.attachments = ConversationAttachmentStore(self.workspace)
        self.artifacts = WorkspaceArtifactStore(self.workspace)
        self.workspace_bridges: dict[str, ExecutionBridge] = {str(self.workspace): self}
        self.workspace_bridges_lock = threading.Lock()

    def for_workspace(self, value: Any) -> ExecutionBridge:
        workspace = workspace_path_of(value)
        key = str(workspace)
        with self.workspace_bridges_lock:
            existing = self.workspace_bridges.get(key)
            if existing is not None:
                return existing
            bridge = ExecutionBridge(workspace, self.progress, self.pending_session_syncs)
            self.workspace_bridges[key] = bridge
            return bridge

    @staticmethod
    def _planning_item_key(requirement_key: str = "") -> str:
        """拆解会话在附件仓库里的伪任务键，一条需求一个命名空间。"""
        return f"{PLANNING_ITEM_KEY}:{requirement_key}" if requirement_key else PLANNING_ITEM_KEY

    @staticmethod
    def _planning_identity(program_id: int, requirement_key: str = "") -> tuple[str, int, str]:
        # 每条需求一条独立的拆解线：两个需求同时拆解不该互相判定为「已有运行中的会话」。
        return task_identity("", program_id, ExecutionBridge._planning_item_key(requirement_key))

    @staticmethod
    def _planning_catalog(session: dict[str, Any] | None) -> list[dict[str, Any]]:
        if not session:
            return []
        catalog = session.get("catalog") or []
        return [dict(entry) for entry in catalog if isinstance(entry, dict) and entry.get("threadId")]

    def _project_content_sync_settings(self, config: dict[str, Any], program_id: int) -> dict[str, Any]:
        """Read the authoritative project-level switches; failures deliberately fail closed."""
        if program_id <= 0:
            return {}
        try:
            program = planner.request_api(
                config,
                "GET",
                "/delivery/program",
                query={"programId": program_id},
            )
        except planner.ToolFailure as exc:
            print(f"读取项目内容同步配置失败，跳过本地/云端归档：{program_id}: {exc}", file=sys.stderr, flush=True)
            return {}
        return program if isinstance(program, dict) else {}

    def _project_chat_archive_enabled(self, config: dict[str, Any], program_id: int) -> bool:
        """The explicit project Git chat-sync switch controls workspace chat/ archives."""
        return bool(self._project_content_sync_settings(config, program_id).get("gitChatSyncEnabled"))

    @staticmethod
    def _project_cloud_sync_scopes(program: dict[str, Any]) -> set[str]:
        if not bool(program.get("cloudSyncEnabled")):
            return set()
        raw_scopes = program.get("cloudSyncScopes")
        if not isinstance(raw_scopes, list):
            return set()
        return {str(scope).strip() for scope in raw_scopes if str(scope).strip() in CLOUD_SYNC_SCOPES}

    @staticmethod
    def _upload_cloud_sync_file(
        config: dict[str, Any],
        program_id: int,
        category: str,
        relative_path: str,
        content_type: str,
        content: bytes,
    ) -> None:
        if category not in CLOUD_SYNC_SCOPES:
            raise BridgeFailure("云端同步类别无效")
        if len(content) > MAX_CLOUD_SYNC_FILE_BYTES:
            raise BridgeFailure(f"云端同步文件不能超过 8MB：{relative_path}")
        planner.request_api(
            config,
            "POST",
            "/delivery/cloud-sync/file",
            body={
                "programId": program_id,
                "category": category,
                "relativePath": relative_path,
                "contentType": content_type,
                "contentBase64": base64.b64encode(content).decode("ascii"),
                "actorName": "delivery-http-bridge",
            },
        )

    def _sync_workspace_cloud_files(
        self,
        config: dict[str, Any],
        program_id: int,
        scopes: set[str],
    ) -> dict[str, Any]:
        entries, skipped = cloud_sync_workspace_entries(self.workspace, scopes)
        uploaded: list[str] = []
        for category, relative, source, content_type in entries:
            self._upload_cloud_sync_file(
                config, program_id, category, relative, content_type, source.read_bytes(),
            )
            uploaded.append(relative)
        return {
            "enabled": bool(scopes), "scopes": sorted(scopes),
            "uploaded": len(uploaded), "skipped": skipped, "files": uploaded,
        }

    def sync_cloud_workspace(self, program_id: int, config: dict[str, Any]) -> dict[str, Any]:
        """Manually sync the currently selected project workspace without exposing its absolute path."""
        assert_runtime_project(config, program_id)
        program = self._project_content_sync_settings(config, program_id)
        scopes = self._project_cloud_sync_scopes(program)
        if not scopes:
            return {"enabled": False, "scopes": [], "uploaded": 0, "skipped": 0, "files": []}
        return self._sync_workspace_cloud_files(config, program_id, scopes)

    def _archive_terminal_chat(
        self,
        client: Any,
        *,
        config: dict[str, Any],
        program_id: int,
        resource_kind: str,
        resource_key: str,
        resource_name: str,
        requirement_key: str = "",
        conversation_title: str,
        thread_id: str,
        provider: str,
        phase: str,
        terminal_status: str,
    ) -> None:
        """Best-effort workspace archive; failures must not hide the task result."""
        program = self._project_content_sync_settings(config, program_id)
        archive_to_workspace = bool(program.get("gitChatSyncEnabled"))
        cloud_scopes = self._project_cloud_sync_scopes(program)
        if not archive_to_workspace and not cloud_scopes:
            return
        try:
            if thread_id and (archive_to_workspace or "chat" in cloud_scopes):
                thread = client.read_thread(thread_id, request_id=client.next_request_id())
                turns = thread.get("turns") if isinstance(thread, dict) else []
                relative = chat_archive_relative_path(
                    resource_kind,
                    resource_key,
                    conversation_title or resource_name,
                    thread_id,
                    requirement_key=requirement_key,
                )
                if archive_to_workspace:
                    relative = archive_chat_snapshot(
                        self.workspace,
                        resource_kind=resource_kind,
                        resource_key=resource_key,
                        resource_name=resource_name,
                        requirement_key=requirement_key,
                        conversation_title=conversation_title,
                        thread_id=thread_id,
                        provider=provider,
                        phase=phase,
                        terminal_status=terminal_status,
                        turns=turns,
                    )
                    print(f"聊天记录已归档：{relative.as_posix()}", file=sys.stderr, flush=True)
                if "chat" in cloud_scopes:
                    self._upload_cloud_sync_file(
                        config,
                        program_id,
                        "chat",
                        relative.as_posix(),
                        "text/markdown; charset=utf-8",
                        archived_chat_text(
                            resource_kind=resource_kind,
                            resource_key=resource_key,
                            resource_name=resource_name,
                            requirement_key=requirement_key,
                            conversation_title=conversation_title,
                            thread_id=thread_id,
                            provider=provider,
                            phase=phase,
                            terminal_status=terminal_status,
                            turns=turns,
                        ).encode("utf-8"),
                    )
            document_scopes = cloud_scopes - {"chat"}
            if document_scopes:
                self._sync_workspace_cloud_files(config, program_id, document_scopes)
        except Exception as exc:
            print(f"归档或云端同步失败：{resource_kind}/{resource_key}/{thread_id}: {exc}", file=sys.stderr, flush=True)

    def _release_active_run(self, identity: str) -> dict[str, Any] | None:
        """回合结束就顺手丢掉这条线程的只读快照。

        活跃期正文来自这一路自己的 client，不经过只读池；一旦收尾，面板下一轮
        就会改走池子，这里主动失效可以避免它多等一个 TTL 才看到收尾内容。
        """
        entry = self.active_runs.pop(identity, None)
        THREAD_READERS.invalidate(str((entry or {}).get("threadId") or ""))
        return entry

    def _read_thread_with_workspace_archive(
        self,
        client: Any,
        thread_id: str,
        resource_kind: str,
        resource_key: str,
        config: dict[str, Any],
        program_id: int,
        *,
        provider: str = "codex",
        environment: dict[str, str] | None = None,
        workspace: Path | None = None,
    ) -> dict[str, Any]:
        """Prefer the executor's local history, then fall back to this workspace's Chat archive.

        `client` 传空表示这条线程当前没有活跃回合，正文走只读复用池：不再为每次
        轮询拉起一个执行器子进程，同一瞬间的重复读也会被合并掉。
        """
        reader_workspace = workspace or self.workspace
        if client is None:
            thread = THREAD_READERS.read(provider, reader_workspace, environment, thread_id)
        else:
            # 回合正在跑：共用执行器那一路的 client，给一个明显低于面板超时的上限，
            # 读不回来就用上一次的好快照兜底，别让浏览器等到自己 abort。
            thread = read_thread_or_empty(client, thread_id, timeout=ACTIVE_THREAD_READ_TIMEOUT_SECONDS)
            if thread.get("turns"):
                THREAD_READERS.remember(provider, reader_workspace, thread_id, thread)
            else:
                thread = THREAD_READERS.last_good(provider, reader_workspace, thread_id) or thread
        turns = thread.get("turns") if isinstance(thread, dict) else None
        if isinstance(turns, list) and turns:
            return thread
        if not self._project_chat_archive_enabled(config, program_id):
            return thread
        archived = read_workspace_chat_archive(self.workspace, resource_kind, resource_key, thread_id)
        archived_turns = archived.get("turns") if isinstance(archived, dict) else None
        if isinstance(archived_turns, list) and archived_turns:
            print(
                f"本机执行器未返回会话正文，已从项目聊天归档读取：{resource_kind}/{resource_key}/{thread_id}",
                file=sys.stderr,
                flush=True,
            )
            return archived
        return thread

    def _planning_result(self, config: dict[str, Any], program_id: int, baseline: dict[str, set[str]]) -> dict[str, Any]:
        assert_runtime_project(config, program_id)
        context = planner.project_context(config, program_id)
        items = [item for item in context.get("items") or [] if str(item.get("itemKey") or "") not in baseline["items"]]
        stages = [item for item in context.get("stages") or [] if str(item.get("stageKey") or "") not in baseline["stages"]]
        modules = [item for item in context.get("modules") or [] if str(item.get("moduleKey") or "") not in baseline["modules"]]
        return {
            "items": items,
            "stages": stages,
            "modules": modules,
            "itemKeys": [str(item.get("itemKey") or "") for item in items if item.get("itemKey")],
            "stageKeys": [str(item.get("stageKey") or "") for item in stages if item.get("stageKey")],
            "moduleKeys": [str(item.get("moduleKey") or "") for item in modules if item.get("moduleKey")],
            "updatedAt": utc_now(),
        }

    def _load_planning_session(
        self,
        config: dict[str, Any],
        program_id: int,
        requirement_key: str,
        provider: str,
        thread_id: str = "",
    ) -> dict[str, Any] | None:
        """从任务面板读回这条需求的拆解会话目录。

        桥接是随时会重启的本地进程，聊天列表只能由服务端持有；对话正文仍在执行器
        自己的会话缓存里，这里拿到 threadId 之后再按 thread 读回。
        """
        if not requirement_key:
            return None
        # 目录不按执行器过滤：换了工具也要能看见此前用另一个工具留下的聊天，正文再按线程自己的执行器读。
        rows = planner.request_api(
            config,
            "GET",
            "/delivery/requirement/planning-sessions",
            query={"programId": program_id, "requirementKey": requirement_key},
        )
        # 原型会话与拆解会话共用这张表，靠用途后缀区分；这里只要拆解本身。
        rows = [
            row for row in (rows or [])
            if isinstance(row, dict) and str(row.get("threadId") or "") and same_executor_purpose(row, "")
        ]
        if not rows:
            return None
        catalog = [
            {
                "threadId": str(row.get("threadId") or ""),
                "title": str(row.get("title") or ""),
                "createdAt": str(row.get("createdAt") or ""),
                "updatedAt": str(row.get("updatedAt") or ""),
                "status": str(row.get("status") or "completed"),
                "executorType": executor_provider_of(row, provider),
                "active": False,
            }
            for row in rows
        ]
        current = next((row for row in rows if str(row.get("threadId")) == thread_id), rows[-1])
        metadata = current.get("metadata") if isinstance(current.get("metadata"), dict) else {}
        baseline = metadata.get("baseline") if isinstance(metadata.get("baseline"), dict) else {}
        return {
            "threadId": str(current.get("threadId") or ""),
            "executorType": executor_provider_of(current, provider),
            "turnId": str(metadata.get("turnId") or ""),
            "stageKey": str(metadata.get("stageKey") or ""),
            "moduleKey": str(metadata.get("moduleKey") or ""),
            "kind": str(metadata.get("kind") or ""),
            "detailDigest": str(metadata.get("detailDigest") or ""),
            "requirementKey": requirement_key,
            "baseline": {name: set(baseline.get(name) or []) for name in ("items", "stages", "modules")},
            "result": metadata.get("result") if isinstance(metadata.get("result"), dict) else {},
            "catalog": catalog,
        }

    def _save_planning_session(
        self,
        config: dict[str, Any],
        program_id: int,
        requirement_key: str,
        provider: str,
        session: dict[str, Any],
    ) -> None:
        """把当前这条 thread 的目录项写回任务面板。失败不影响本轮拆解，只是列表少一条。"""
        thread_id = str(session.get("threadId") or "")
        if not requirement_key or not thread_id:
            return
        entry = next(
            (item for item in session.get("catalog") or [] if str(item.get("threadId")) == thread_id),
            {},
        )
        # 线程归属跟着它自己的执行器走，别被当前选中的工具改写。
        provider = executor_provider_of(entry, session.get("executorType") or provider)
        result = session.get("result") or {}
        metadata: dict[str, Any] = {
            "turnId": str(session.get("turnId") or ""),
            "stageKey": str(session.get("stageKey") or ""),
            "moduleKey": str(session.get("moduleKey") or ""),
            "kind": str(session.get("kind") or ""),
            "detailDigest": str(session.get("detailDigest") or ""),
            "baseline": {name: sorted(values) for name, values in (session.get("baseline") or {}).items()},
            "result": result,
        }
        # 服务端给 metadata 留了 256KB；产出对象太多时只留键，前端会回落到看板上的任务明细。
        if len(json.dumps(metadata, ensure_ascii=False).encode("utf-8")) > 200 * 1024:
            metadata["result"] = {
                "items": [],
                "stages": [],
                "modules": [],
                "itemKeys": result.get("itemKeys") or [],
                "stageKeys": result.get("stageKeys") or [],
                "moduleKeys": result.get("moduleKeys") or [],
                "updatedAt": result.get("updatedAt") or "",
            }
        try:
            planner.request_api(
                config,
                "POST",
                "/delivery/requirement/planning-session/bind",
                body={
                    "programId": program_id,
                    "requirementKey": requirement_key,
                    "executorType": provider,
                    "threadId": thread_id,
                    "title": str(entry.get("title") or ""),
                    "status": str(entry.get("status") or "running"),
                    "metadata": metadata,
                },
            )
        except Exception as exc:
            print(f"保存拆解会话目录失败：{program_id}/{requirement_key}: {exc}", file=sys.stderr, flush=True)

    @staticmethod
    def _planning_baseline(context: dict[str, Any]) -> dict[str, set[str]]:
        return {
            "items": {str(item.get("itemKey") or "") for item in context.get("items") or []},
            "stages": {str(item.get("stageKey") or "") for item in context.get("stages") or []},
            "modules": {str(item.get("moduleKey") or "") for item in context.get("modules") or []},
        }

    def planning(
        self,
        program_id: int,
        selected_thread_id: str = "",
        biz_line: str = DEFAULT_BIZ_LINE,
        config: dict[str, Any] | None = None,
        requirement_key: str = "",
        provider: str = "codex",
    ) -> dict[str, Any]:
        provider = ai_provider_of(provider)
        config = request_scoped_config(config, biz_line, program_id)
        biz_line = config_biz_line(config)
        # 目录读服务端，正文读执行器缓存：桥接自己不留状态，重启后照样能把聊天列表列全。
        session = self._load_planning_session(config, program_id, requirement_key, provider, selected_thread_id)
        with self.lock:
            active = self.active_runs.get(self._planning_identity(program_id, requirement_key))
        catalog = self._planning_catalog(session)
        known_thread_ids = {str(entry["threadId"]) for entry in catalog}
        if selected_thread_id and selected_thread_id not in known_thread_ids:
            raise BridgeFailure("所选拆解会话不存在")
        thread_id = selected_thread_id or str((session or {}).get("threadId") or "")
        if not thread_id:
            return {
                "bizLine": biz_line,
                "programId": program_id,
                "requirementKey": requirement_key,
                "threadId": "",
                "executorType": provider,
                "turns": [],
                "conversations": [],
                "active": False,
                "activeTurnId": "",
                "selectedStageKey": "",
                "selectedModuleKey": "",
                "selectedKind": "",
                "result": {"items": [], "stages": [], "modules": [], "itemKeys": [], "stageKeys": [], "moduleKeys": [], "updatedAt": ""},
            }
        thread_entry = next((entry for entry in catalog if str(entry.get("threadId")) == thread_id), {})
        provider = executor_provider_of(thread_entry, session.get("executorType") or provider)
        live_client = active["client"] if active is not None and active.get("threadId") == thread_id else None
        thread = self._read_thread_with_workspace_archive(
            live_client, thread_id, "requirement", requirement_key, config, program_id,
            provider=provider,
            environment=codex_environment(config, program_id, write_allowed=False, provider=provider),
        )
        planning_item_key = self._planning_item_key(requirement_key)
        for entry in catalog:
            entry["active"] = bool(active is not None and entry.get("threadId") == active.get("threadId"))
            # 目录里留着 running，但本进程没有对应的回合：多半是上一次桥接跑一半被重启了。
            if not entry["active"] and entry.get("status") == "running":
                entry["status"] = "interrupted"
        return {
            "bizLine": biz_line,
            "programId": program_id,
            "requirementKey": requirement_key,
            "threadId": thread_id,
            # 选中的这条线程属于哪个工具：面板据此对齐模型下拉和续聊参数。
            "executorType": provider,
            # 附件和产物按需求的伪任务键归档，拆解会话也要能把图片和文件回显出来。
            "turns": serialize_turns(
                thread.get("turns") or [],
                lambda attachment_ids: [
                    ConversationAttachmentStore._public(attachment)
                    for attachment in self.attachments.resolve(program_id, planning_item_key, attachment_ids)
                ],
                lambda paths: self.artifacts.register(config_biz_line(config), program_id, planning_item_key, paths),
            ),
            "conversations": catalog,
            "active": bool(active is not None and active.get("threadId") == thread_id),
            "activeTurnId": str((active or {}).get("turnId") or ""),
            "selectedStageKey": str((session or {}).get("stageKey") or ""),
            "selectedModuleKey": str((session or {}).get("moduleKey") or ""),
            "selectedKind": str((session or {}).get("kind") or ""),
            "result": dict((session or {}).get("result") or {}),
        }

    def send_planning(self, raw: Any, config: dict[str, Any]) -> dict[str, Any]:
        provider = ai_provider_of(raw)
        (
            program_id,
            message,
            requested_thread_id,
            new_conversation,
            selected_stage,
            selected_module,
            selected_kind,
            model,
            reasoning_effort,
            fast_mode,
            requirement,
            attachment_ids,
            chat_references,
            confirm_write,
        ) = validate_planning_payload(raw)
        assert_runtime_project(config, program_id)
        biz_line = config_biz_line(config)
        context = planner.project_context(config, program_id)
        requirement_key = str(requirement.get("requirementKey") or "")
        mention_context = self._conversation_mention_context(
            config, program_id, chat_references, context, requirement_key,
        )
        planner.require_option(selected_stage, context.get("stages") or [], "stageKey", "里程碑")
        planner.require_option(selected_module, context.get("modules") or [], "moduleKey", "模块")
        attachments = self.attachments.resolve(program_id, self._planning_item_key(requirement_key), attachment_ids)
        identity = self._planning_identity(program_id, requirement_key)
        session = self._load_planning_session(config, program_id, requirement_key, provider, requested_thread_id)
        with self.lock:
            active = self.active_runs.get(identity)
        if active is not None:
            if new_conversation or (requested_thread_id and requested_thread_id != active.get("threadId")):
                raise BridgeFailure("当前需求已有正在运行的拆解会话，请先停止或等待完成")
            # 运行中的回合是以预览身份启动的，写入权限改不了，只能等这轮预览结束再确认。
            if confirm_write:
                raise BridgeFailure("当前拆解回合还在运行，请等待本轮梳理结束后再确认写入")
            active["client"].steer_turn(
                str(active["threadId"]),
                str(active["turnId"]),
                message_with_attachments(
                    with_mention_context(
                        message,
                        [
                            *requirement_outline_rule_lines(
                                requirement_outline_path_of(requirement_key).as_posix() if requirement_key else "",
                                False,
                                planning_temp_document_path(
                                    str(requirement.get("name") or ""), requirement_key, str(active["threadId"])
                                ).as_posix(),
                            ),
                            *requirement_document_rule_lines(requirement_key),
                            *mention_context,
                        ],
                    ),
                    attachments,
                ),
                attachments,
                request_id=active["client"].next_request_id(),
            )
            active.setdefault("userMessages", []).append(message)
            self.progress.publish(identity, "message", "已追加拆解要求", message, "running")
            return {"accepted": True, "bizLine": biz_line, "programId": program_id, "requirementKey": requirement_key, "threadId": active["threadId"], "turnId": active["turnId"], "active": True}
        catalog = self._planning_catalog(session)
        known_thread_ids = {str(entry["threadId"]) for entry in catalog}
        if requested_thread_id and requested_thread_id not in known_thread_ids:
            raise BridgeFailure("所选拆解会话不存在")
        started_new_conversation = not session or new_conversation or not session.get("threadId")
        # 这条需求此前一次拆解会话都没有：不管是新增还是编辑进来的，首轮都按用户的问题重定标题。
        first_planning_conversation = started_new_conversation and not catalog
        if started_new_conversation:
            # 一条新会话还没出过预览，没有可确认的方案。
            if confirm_write:
                raise BridgeFailure("请先梳理需求并生成拆解预览，再确认写入")
            if len(catalog) >= MAX_PLANNING_CONVERSATIONS:
                raise BridgeFailure("该需求保留的拆解会话已达上限")
            # 名称留空的新需求先用需求编号占位；标题由开聊时并行跑的那轮自动命名尽快补上。
            title = f"需求拆解 · {requirement.get('name') or requirement_key or context.get('program', {}).get('name') or program_id}"
            if catalog:
                title = f"{title} V0.0.{len(catalog)}"
            client = create_ai_client(
                provider,
                self.workspace,
                lambda event: self._publish_app_server_event(identity, event),
                codex_environment(config, program_id, write_allowed=False, provider=provider),
            )
            try:
                thread_id, turn_id = client.start_task(
                    title,
                    message_with_attachments(
                        build_planning_prompt(program_id, context, message, selected_stage, selected_module, selected_kind, requirement, False, self.workspace, mention_context),
                        attachments,
                    ),
                    attachments,
                    model=model,
                    reasoning_effort=reasoning_effort,
                    fast_mode=fast_mode,
                )
            except Exception:
                client.close()
                raise
            baseline = self._planning_baseline(context)
            session = {
                "threadId": thread_id,
                "turnId": turn_id,
                "stageKey": selected_stage,
                "moduleKey": selected_module,
                "kind": selected_kind,
                "requirementKey": requirement_key,
                "detailDigest": planning_detail_digest(requirement),
                "baseline": baseline,
                "result": {"items": [], "stages": [], "modules": [], "itemKeys": [], "stageKeys": [], "moduleKeys": [], "updatedAt": ""},
                "catalog": [*catalog, {"threadId": thread_id, "title": title, "createdAt": utc_now(), "updatedAt": utc_now(), "status": "running", "active": True}],
            }
        else:
            thread_id = requested_thread_id or str(session.get("threadId") or "")
            # 已有会话只能用它自己的执行器续：线程正文在那个执行器的缓存里，换工具读不到。
            provider = executor_provider_of(
                next((entry for entry in catalog if str(entry.get("threadId")) == thread_id), {}),
                session.get("executorType") or provider,
            )
            detail_digest = planning_detail_digest(requirement)
            client = create_ai_client(
                provider,
                self.workspace,
                lambda event: self._publish_app_server_event(identity, event),
                codex_environment(config, program_id, write_allowed=confirm_write, provider=provider),
            )
            try:
                client.resume_thread(thread_id)
                # 追加回合不重发首轮那整段拆解纪律，只带会变的和丢不起的：本需求已建任务清单
                # （「不要重复建任务」这条约束正是靠它成立，会话被压缩后必须还在）、大纲读写纪律、
                # 本轮的选择与 @ 引用。确认写入是另一套指令（写入契约、命令行动作），仍然走全量。
                follow_up_prompt = (
                    build_planning_prompt(
                        program_id, context, message, selected_stage, selected_module, selected_kind,
                        requirement, True, self.workspace, mention_context, thread_id,
                    )
                    if confirm_write
                    else build_planning_follow_up_prompt(
                        program_id, context, message, selected_stage, selected_module, selected_kind, requirement,
                        self.workspace, mention_context,
                        include_detail=detail_digest != str(session.get("detailDigest") or ""),
                        thread_id=thread_id,
                    )
                )
                turn_id = client.start_turn(
                    thread_id,
                    message_with_attachments(follow_up_prompt, attachments),
                    attachments,
                    request_id=client.next_request_id(),
                    model=model,
                    reasoning_effort=reasoning_effort,
                    fast_mode=fast_mode,
                )
            except Exception:
                client.close()
                raise
            session.update({"threadId": thread_id, "turnId": turn_id, "detailDigest": detail_digest, "stageKey": selected_stage or session.get("stageKey") or "", "moduleKey": selected_module or session.get("moduleKey") or "", "kind": selected_kind or session.get("kind") or ""})
            for entry in session.get("catalog") or []:
                if entry.get("threadId") == thread_id:
                    entry["status"] = "running"
                    entry["active"] = True
                    entry["updatedAt"] = utc_now()
        with self.lock:
            self.active.add(identity)
            self.active_runs[identity] = {
                "client": client,
                "threadId": thread_id,
                "turnId": turn_id,
                "planning": True,
                "provider": provider,
                "config": config,
                "programId": program_id,
                "userMessages": [message],
            }
        # 目录当场写回服务端：这一轮还没跑完桥接就重启，聊天列表里也得留着这条会话。
        self._save_planning_session(config, program_id, requirement_key, provider, session)
        self.progress.publish(
            identity,
            "status",
            "正在写入任务" if confirm_write else "正在梳理需求",
            f"{provider_label(provider)} 正在{'调用任务规划插件写入任务' if confirm_write else '整理拆解预览，确认前不会写入任务'}。",
            "running",
        )
        namer: threading.Thread | None = None
        naming_outcome: dict[str, str] | None = None
        if started_new_conversation:
            namer, naming_outcome = self._start_conversation_naming(
                identity, config, program_id, requirement_key, provider, model, fast_mode,
                message, session, thread_id, first_planning_conversation,
            )
        threading.Thread(
            target=self._follow_planning,
            args=(identity, client, config, program_id, requirement_key, provider, session, thread_id, turn_id,
                  model, reasoning_effort, fast_mode, message, started_new_conversation, confirm_write,
                  namer, naming_outcome, first_planning_conversation),
            daemon=True,
        ).start()
        return {"accepted": True, "bizLine": biz_line, "programId": program_id, "requirementKey": requirement_key, "threadId": thread_id, "turnId": turn_id, "active": True}

    def stop_planning(self, raw: Any, config: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise BridgeFailure("请求体必须是 JSON 对象")
        biz_line = biz_line_of(raw)
        program_id = program_id_of(raw.get("programId"))
        if not program_id:
            raise BridgeFailure("缺少项目标识")
        assert_runtime_project(config, program_id)
        biz_line = config_biz_line(config)
        requirement_key = str(raw.get("requirementKey") or "").strip()
        identity = self._planning_identity(program_id, requirement_key)
        with self.lock:
            active = self.active_runs.get(identity)
        if active is None or not active.get("planning"):
            raise BridgeFailure("该需求当前没有正在运行的拆解会话")
        requested_thread_id = str(raw.get("threadId") or "").strip()
        if requested_thread_id and requested_thread_id != active.get("threadId"):
            raise BridgeFailure("所选拆解会话当前没有正在运行的回合")
        active["client"].interrupt_turn(str(active["threadId"]), str(active["turnId"]), request_id=active["client"].next_request_id())
        self.progress.publish(identity, "status", "已请求停止拆解", "正在等待 Codex 中断当前回合。", "running")
        return {"accepted": True, "bizLine": biz_line, "programId": program_id, "requirementKey": requirement_key, "threadId": active["threadId"], "turnId": active["turnId"], "active": True}

    def _name_conversation(
        self,
        config: dict[str, Any],
        program_id: int,
        provider: str,
        model: str,
        reasoning_effort: str,
        fast_mode: bool,
        user_message: str,
        reply: str,
    ) -> str:
        """起一轮只读短会话，为新聊天生成标题；超时则保留原始占位标题。"""
        client = create_ai_client(
            provider,
            self.workspace,
            None,
            codex_environment(config, program_id, write_allowed=False, provider=provider),
        )
        try:
            thread_id, turn_id = client.start_task(
                "聊天自动命名",
                build_conversation_title_prompt(user_message, reply),
                None,
                model,
                reasoning_effort=reasoning_effort,
                fast_mode=fast_mode,
            )
            outcome: dict[str, str] = {}

            def wait() -> None:
                outcome["status"] = client.wait_turn(turn_id)

            waiter = threading.Thread(target=wait, daemon=True)
            waiter.start()
            waiter.join(CONVERSATION_TITLE_TIMEOUT_SECONDS)
            if waiter.is_alive():
                return ""
            status = outcome.get("status") or "failed"
            if status != "completed":
                return ""
            turn = client.read_turn(thread_id, turn_id, client.next_request_id())
            return conversation_title_of(final_agent_text_from_output(execution_output(status, turn)))
        finally:
            client.close()

    @staticmethod
    def _rename_conversation(client: AppServerClient | ClaudeCLIClient, thread_id: str, title: str) -> None:
        """Keep the provider-native title aligned when that provider supports renaming."""
        if not title:
            return
        try:
            client.set_thread_name(thread_id, title, request_id=client.next_request_id())
        except Exception as exc:
            # 面板的会话目录仍会保存新标题；原生线程重命名失败不能影响交付结果。
            print(f"同步原生会话标题失败：{thread_id}: {exc}", file=sys.stderr, flush=True)

    @staticmethod
    def _write_requirement_name(
        config: dict[str, Any],
        program_id: int,
        requirement_key: str,
        name: str,
        replace: str = "",
    ) -> None:
        """把生成的标题落到需求名称上。

        `replace` 是这次允许覆盖的旧名称：留空表示只写空名称，传占位名表示只换掉那个占位名。
        名称是用户随时能自己改的字段，服务端按同一条件再判一次，两边都不会盖掉用户填的名字。
        """
        name = str(name or "").strip()
        replace = str(replace or "").strip()
        if not requirement_key or not name or name == replace:
            return
        try:
            requirement = planner.requirement_record(config, program_id, requirement_key)
            if str(requirement.get("name") or "").strip() != replace:
                return
            planner.request_api(
                config,
                "POST",
                "/delivery/requirement/name/update",
                body={
                    "programId": program_id,
                    "requirementKey": requirement_key,
                    "name": name,
                    "replaceName": replace,
                },
            )
        except Exception as exc:
            print(f"回写需求名称失败：{program_id}/{requirement_key}: {exc}", file=sys.stderr, flush=True)

    def _start_conversation_naming(
        self,
        identity: tuple[str, int, str],
        config: dict[str, Any],
        program_id: int,
        requirement_key: str,
        provider: str,
        model: str,
        fast_mode: bool,
        user_message: str,
        session: dict[str, Any],
        thread_id: str,
        first_conversation: bool = False,
    ) -> tuple[threading.Thread | None, dict[str, str]]:
        """新开聊天时就先定名字：标题只看用户的首条说明，不等首轮回复。

        面板上一条没名字的需求只能按需求编号显示，等整轮拆解跑完才补名字太晚；
        这一轮命名和拆解并行跑，会话标题和需求名称都在开聊的几十秒内确定下来。
        起名本身也要跑一轮模型，那几秒里名称还是空的，所以先用首条消息的前几个字占位，
        AI 的标题回来再把占位名换掉。整段失败都不影响拆解结果，回合结束时还会兜底补一次。

        `first_conversation` 表示这条需求此前一次拆解会话都没有。这种需求即使已经带着
        名字（从编辑入口进来的手填名），首轮也要按用户的问题重定一次标题：把当前这个名字
        当成允许覆盖的旧值，而不是写占位名去盖掉它。
        """
        outcome: dict[str, str] = {}
        # Git 新需求已经用需求编号作临时名：不要再按用户首句并行起名，
        # 必须等首轮执行器返回反馈后，再根据完整问答生成正式标题。
        try:
            current_name = str((planner.requirement_record(config, program_id, requirement_key) or {}).get("name") or "").strip()
        except Exception:
            current_name = ""
        if current_name == requirement_key:
            outcome["placeholder"] = requirement_key
            return None, outcome
        placeholder = placeholder_requirement_name(user_message)
        if current_name and first_conversation:
            # 已经有名字、但一次都没聊过：不写占位名（面板上先留着用户看得懂的原名），
            # 直接把这个名字作为允许被首轮标题覆盖的旧值。
            placeholder = current_name
            outcome["placeholder"] = current_name
        elif not current_name and placeholder:
            # 占位名同步写：这一步只是一个接口调用，要赶在本次请求返回前落库，用户才会立刻看到。
            self._write_requirement_name(config, program_id, requirement_key, placeholder)
            outcome["placeholder"] = placeholder

        def run() -> None:
            try:
                # 起名只看一句话，用最低推理强度跑：名字要在用户还盯着屏幕的时候就出来。
                title = self._name_conversation(
                    config, program_id, provider, model, "low", fast_mode, user_message, "",
                )
                if not title:
                    return
                outcome["title"] = title
                for entry in session.get("catalog") or []:
                    if str(entry.get("threadId") or "") == thread_id:
                        entry["title"] = title
                        entry["updatedAt"] = utc_now()
                self._save_planning_session(config, program_id, requirement_key, provider, session)
                self._write_requirement_name(config, program_id, requirement_key, title, placeholder)
                self.progress.publish(identity, "status", "已确定需求标题", title, "running")
            except Exception as exc:
                print(f"开聊命名失败：{program_id}/{requirement_key}: {exc}", file=sys.stderr, flush=True)

        namer = threading.Thread(target=run, daemon=True)
        namer.start()
        return namer, outcome

    def _name_requirement_if_empty(
        self,
        identity: tuple[str, int, str],
        config: dict[str, Any],
        program_id: int,
        requirement_key: str,
        provider: str,
        model: str,
        reasoning_effort: str,
        fast_mode: bool,
        user_message: str,
        client: AppServerClient,
        thread_id: str,
        turn_id: str,
        suggested_name: str = "",
        first_conversation: bool = False,
    ) -> None:
        """新建需求允许不填名称：这一轮聊完就按聊天内容补上标题。

        开聊时占位名可能已经写进去了，所以「还没起过名」有两种样子：名称为空，
        或者名称就是本轮首条消息的那个占位名。除此之外一律不动 —— 名称是用户随时能自己
        改的字段，服务端按同一条件再判一次。整段失败都不影响拆解结果。

        `first_conversation` 是这条需求的第一次拆解会话：手填过名字的需求也要按首轮问答
        重定标题，所以此时当前名称本身就是允许被覆盖的旧值（开聊那轮并行命名没能出标题时
        才轮得到这里兜底）。
        """
        if not requirement_key:
            return
        try:
            requirement = planner.requirement_record(config, program_id, requirement_key)
        except Exception as exc:
            print(f"读取需求名称失败：{program_id}/{requirement_key}: {exc}", file=sys.stderr, flush=True)
            return
        current = str(requirement.get("name") or "").strip()
        # 开聊那轮已经把标题写进去了：这里不能再起一个名字把它换掉。
        if current and current == suggested_name.strip():
            return
        placeholder = placeholder_requirement_name(user_message)
        # Git 新需求的临时名称是需求编号；首轮 AI 回复完成后允许把它替换成正式标题。
        allowed_placeholders = {placeholder, requirement_key}
        if current and current not in allowed_placeholders and not first_conversation:
            return
        self.progress.publish(identity, "status", "正在生成需求标题", "需求名称还没定，正在按本轮聊天内容生成标题。", "running")
        try:
            name = suggested_name.strip()
            if not name:
                turn = client.read_turn(thread_id, turn_id, client.next_request_id())
                reply = final_agent_text_from_output(execution_output("completed", turn))
                name = self._name_conversation(config, program_id, provider, model, reasoning_effort, fast_mode, user_message, reply)
            if not name:
                return
            self._write_requirement_name(config, program_id, requirement_key, name, current)
        except Exception as exc:
            print(f"生成需求标题失败：{program_id}/{requirement_key}: {exc}", file=sys.stderr, flush=True)

    def _follow_planning(
        self,
        identity: tuple[str, int, str],
        client: AppServerClient,
        config: dict[str, Any],
        program_id: int,
        requirement_key: str,
        provider: str,
        session: dict[str, Any],
        thread_id: str,
        turn_id: str,
        model: str = "",
        reasoning_effort: str = "",
        fast_mode: bool = False,
        user_message: str = "",
        started_new_conversation: bool = False,
        confirm_write: bool = False,
        namer: threading.Thread | None = None,
        naming_outcome: dict[str, str] | None = None,
        first_conversation: bool = False,
    ) -> None:
        status = "failed"
        try:
            status = client.wait_turn(turn_id)
            entry = next(
                (item for item in session.get("catalog") or [] if str(item.get("threadId") or "") == thread_id),
                {},
            )
            title = str(entry.get("title") or "需求拆解")
            generated_title = ""
            reply = ""
            if status == "completed":
                turn = client.read_turn(thread_id, turn_id, client.next_request_id())
                reply = final_agent_text_from_output(execution_output(status, turn))
            # 只有新开聊天的首回合才自动命名；后续追问不能覆盖用户已经识别出的会话标题。
            if started_new_conversation:
                # 开聊时那轮命名一般早就回来了；万一还在跑，等它一下再决定要不要重命名。
                if namer is not None and namer.is_alive():
                    namer.join(CONVERSATION_TITLE_TIMEOUT_SECONDS)
                generated_title = str((naming_outcome or {}).get("title") or "")
                if not generated_title and status == "completed":
                    generated_title = self._name_conversation(
                        config, program_id, provider, model, reasoning_effort, fast_mode, user_message, reply,
                    )
                if generated_title:
                    title = generated_title
                    entry["title"] = title
                    self._rename_conversation(client, thread_id, title)
            # 命名放在释放本轮之前：面板是靠「回合结束」去取标题的，先放行会取到旧标题。
            if status == "completed":
                self._name_requirement_if_empty(
                    identity, config, program_id, requirement_key, provider, model, reasoning_effort, fast_mode,
                    user_message, client, thread_id, turn_id, generated_title, first_conversation,
                )
                try:
                    requirement = planner.requirement_record(config, program_id, requirement_key)
                    requirement_name = str(requirement.get("name") or "").strip() or generated_title or title
                except Exception:
                    requirement_name = generated_title or title or requirement_key
                temp_path = planning_temp_document_path(requirement_name, requirement_key, thread_id)
                if confirm_write:
                    delete_planning_temp_summary(temp_path)
                    session.pop("tempPath", None)
                else:
                    with self.lock:
                        current_run = self.active_runs.get(identity) or {}
                        round_messages = [
                            str(item).strip() for item in current_run.get("userMessages") or [user_message]
                            if str(item).strip()
                        ]
                    write_planning_temp_summary(
                        temp_path,
                        requirement_name,
                        requirement_key,
                        thread_id,
                        "\n\n".join(round_messages) or user_message,
                        reply,
                    )
                    session["tempPath"] = temp_path.as_posix()
            self._archive_terminal_chat(
                client,
                config=config,
                program_id=program_id,
                resource_kind="requirement",
                resource_key=requirement_key,
                resource_name=title,
                conversation_title=title,
                thread_id=thread_id,
                provider=provider,
                phase="planning",
                terminal_status=status,
            )
            session["result"] = self._planning_result(config, program_id, session["baseline"])
            session["turnId"] = turn_id
            for entry in session.get("catalog") or []:
                if entry.get("threadId") == thread_id:
                    entry["status"] = status
                    entry["active"] = False
                    entry["updatedAt"] = utc_now()
            self._save_planning_session(config, program_id, requirement_key, provider, session)
            self.progress.publish(
                identity,
                "status",
                "拆解已完成" if status == "completed" else "拆解未完成",
                "已同步本次创建的项目结构和任务列表。",
                status,
            )
        except Exception as exc:
            self.progress.publish(identity, "error", "同步拆解结果失败", str(exc), "failed")
            print(f"同步项目拆解结果失败：{program_id}: {exc}", file=sys.stderr, flush=True)
        finally:
            client.close()
            with self.lock:
                current = self.active_runs.get(identity)
                if current is not None and current.get("client") is client:
                    self.active.discard(identity)
                    self._release_active_run(identity)

    # ---------- 预设环境会话 ----------

    @staticmethod
    def _environment_setup_identity(program_id: int = GLOBAL_ENVIRONMENT_SETUP_PROGRAM_ID) -> tuple[str, int, str]:
        """预设环境只有一条本机全局会话，不随项目切换。"""
        return task_identity("", program_id, ENVIRONMENT_SETUP_ITEM_KEY)

    @staticmethod
    def _environment_setup_store_key(provider: str, program_id: int = GLOBAL_ENVIRONMENT_SETUP_PROGRAM_ID) -> str:
        return f"{provider}:{program_id}"

    def environment_setup(
        self,
        program_id: int,
        selected_thread_id: str = "",
        config: dict[str, Any] | None = None,
        provider: str = "codex",
        use_git: bool = False,
        environments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        provider = ai_provider_of(provider)
        config = request_scoped_config(config, "", program_id)
        store_key = self._environment_setup_store_key(provider, program_id)
        session = ENVIRONMENT_SETUP_SESSIONS.load(store_key, selected_thread_id)
        identity = self._environment_setup_identity(program_id)
        with self.lock:
            active = self.active_runs.get(identity)
        environment_statuses = environment_probe_statuses(use_git, environments or [])
        catalog = [dict(entry) for entry in (session or {}).get("catalog") or []]
        known_thread_ids = {str(entry.get("threadId") or "") for entry in catalog}
        if selected_thread_id and selected_thread_id not in known_thread_ids:
            raise BridgeFailure("所选预设环境会话不存在")
        thread_id = selected_thread_id or str((session or {}).get("threadId") or "")
        if not thread_id:
            return {
                "programId": program_id,
                "threadId": "",
                "turns": [],
                "conversations": [],
                "active": False,
                "activeTurnId": "",
                "environmentStatuses": environment_statuses,
            }
        live_client = (
            active["client"]
            if active is not None and active.get("environmentSetup") and active.get("threadId") == thread_id
            else None
        )
        thread = (
            read_thread_or_empty(live_client, thread_id, timeout=ACTIVE_THREAD_READ_TIMEOUT_SECONDS)
            if live_client is not None
            else THREAD_READERS.read(
                provider,
                environment_setup_workspace(),
                codex_environment(config, program_id, write_allowed=False, provider=provider),
                thread_id,
            )
        )
        for entry in catalog:
            entry["active"] = bool(active is not None and entry.get("threadId") == active.get("threadId"))
            # 目录里留着 running 但本进程没有对应回合：多半是上一次桥接跑一半被重启了。
            if not entry["active"] and entry.get("status") == "running":
                entry["status"] = "interrupted"
        return {
            "programId": program_id,
            "threadId": thread_id,
            "turns": serialize_turns(thread.get("turns") or []),
            "conversations": catalog,
            "active": bool(active is not None and active.get("threadId") == thread_id),
            "activeTurnId": str((active or {}).get("turnId") or ""),
            "environmentStatuses": environment_statuses,
        }

    def send_environment_setup(self, raw: Any, config: dict[str, Any]) -> dict[str, Any]:
        provider = ai_provider_of(raw)
        (
            program_id,
            message,
            requested_thread_id,
            new_conversation,
            use_git,
            environments,
            model,
            reasoning_effort,
            fast_mode,
        ) = validate_environment_setup_payload(raw)
        assert_runtime_project(config, program_id)
        github_ssh_status = ensure_github_ssh_key() if use_git else {}
        identity = self._environment_setup_identity(program_id)
        store_key = self._environment_setup_store_key(provider, program_id)
        session = ENVIRONMENT_SETUP_SESSIONS.load(store_key, requested_thread_id)
        with self.lock:
            active = self.active_runs.get(identity)
        if active is not None:
            if new_conversation or (requested_thread_id and requested_thread_id != active.get("threadId")):
                raise BridgeFailure("本机已有正在运行的预设环境会话，请先停止或等待完成")
            active["client"].steer_turn(
                str(active["threadId"]),
                str(active["turnId"]),
                build_environment_setup_prompt(use_git, environments, message, False),
                [],
                request_id=active["client"].next_request_id(),
            )
            self.progress.publish(identity, "message", "已追加预设要求", message, "running")
            return {
                "accepted": True,
                "programId": program_id,
                "threadId": active["threadId"],
                "turnId": active["turnId"],
                "active": True,
                **github_ssh_status,
            }
        catalog = [dict(entry) for entry in (session or {}).get("catalog") or []]
        known_thread_ids = {str(entry.get("threadId") or "") for entry in catalog}
        if requested_thread_id and requested_thread_id not in known_thread_ids:
            raise BridgeFailure("所选预设环境会话不存在")
        workspace = environment_setup_workspace()
        if not session or new_conversation or not session.get("threadId"):
            if len(catalog) >= MAX_ENVIRONMENT_SETUP_CONVERSATIONS:
                raise BridgeFailure("本机保留的预设环境会话已达上限")
            title = "预设环境"
            if catalog:
                title = f"{title} V0.0.{len(catalog)}"
            client = create_ai_client(
                provider,
                workspace,
                lambda event: self._publish_app_server_event(identity, event),
                codex_environment(config, program_id, write_allowed=False, provider=provider),
            )
            try:
                thread_id, turn_id = client.start_task(
                    title,
                    build_environment_setup_prompt(use_git, environments, message, True),
                    [],
                    model=model,
                    reasoning_effort=reasoning_effort,
                    fast_mode=fast_mode,
                )
            except Exception:
                client.close()
                raise
            session = {
                "threadId": thread_id,
                "turnId": turn_id,
                "catalog": [*catalog, {"threadId": thread_id, "title": title, "createdAt": utc_now(), "updatedAt": utc_now(), "status": "running", "active": True}],
            }
        else:
            thread_id = requested_thread_id or str(session.get("threadId") or "")
            client = create_ai_client(
                provider,
                workspace,
                lambda event: self._publish_app_server_event(identity, event),
                codex_environment(config, program_id, write_allowed=False, provider=provider),
            )
            try:
                client.resume_thread(thread_id)
                turn_id = client.start_turn(
                    thread_id,
                    build_environment_setup_prompt(use_git, environments, message, False),
                    [],
                    request_id=client.next_request_id(),
                    model=model,
                    reasoning_effort=reasoning_effort,
                    fast_mode=fast_mode,
                )
            except Exception:
                client.close()
                raise
            session.update({"threadId": thread_id, "turnId": turn_id})
            for entry in session.get("catalog") or []:
                if entry.get("threadId") == thread_id:
                    entry["status"] = "running"
                    entry["active"] = True
                    entry["updatedAt"] = utc_now()
        with self.lock:
            self.active.add(identity)
            self.active_runs[identity] = {
                "client": client, "threadId": thread_id, "turnId": turn_id,
                "environmentSetup": True, "provider": provider, "config": config, "programId": program_id, "useGit": use_git,
            }
        # 目录当场落盘：这一轮还没跑完桥接就重启，会话列表里也得留着这条聊天。
        ENVIRONMENT_SETUP_SESSIONS.save(store_key, session)
        self.progress.publish(
            identity,
            "status",
            "正在预设环境",
            f"{provider_label(provider)} 正在检测本机环境，只补装缺少的部分。",
            "running",
        )
        threading.Thread(
            target=self._follow_environment_setup,
            args=(identity, client, store_key, session, thread_id, turn_id, use_git),
            daemon=True,
        ).start()
        return {
            "accepted": True,
            "programId": program_id,
            "threadId": thread_id,
            "turnId": turn_id,
            "active": True,
            **github_ssh_status,
        }

    def stop_environment_setup(self, raw: Any, config: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise BridgeFailure("请求体必须是 JSON 对象")
        program_id = GLOBAL_ENVIRONMENT_SETUP_PROGRAM_ID
        assert_runtime_project(config, program_id)
        identity = self._environment_setup_identity(program_id)
        with self.lock:
            active = self.active_runs.get(identity)
        if active is None or not active.get("environmentSetup"):
            raise BridgeFailure("本机当前没有正在运行的预设环境会话")
        requested_thread_id = str(raw.get("threadId") or "").strip()
        if requested_thread_id and requested_thread_id != active.get("threadId"):
            raise BridgeFailure("所选预设环境会话当前没有正在运行的回合")
        active["client"].interrupt_turn(str(active["threadId"]), str(active["turnId"]), request_id=active["client"].next_request_id())
        self.progress.publish(identity, "status", "已请求停止预设", "正在等待执行器中断当前回合。", "running")
        return {"accepted": True, "programId": program_id, "threadId": active["threadId"], "turnId": active["turnId"], "active": True}

    def _follow_environment_setup(
        self,
        identity: tuple[str, int, str],
        client: AppServerClient,
        store_key: str,
        session: dict[str, Any],
        thread_id: str,
        turn_id: str,
        use_git: bool,
    ) -> None:
        status = "failed"
        try:
            status = client.wait_turn(turn_id)
            if use_git:
                # Git may only have become available during the preset run.
                ensure_github_ssh_key()
            session["turnId"] = turn_id
            for entry in session.get("catalog") or []:
                if entry.get("threadId") == thread_id:
                    entry["status"] = status
                    entry["active"] = False
                    entry["updatedAt"] = utc_now()
            ENVIRONMENT_SETUP_SESSIONS.save(store_key, session)
            self.progress.publish(
                identity,
                "status",
                "预设环境已完成" if status == "completed" else "预设环境未完成",
                "请查看会话里的环境检测与安装结果。",
                status,
            )
        except Exception as exc:
            self.progress.publish(identity, "error", "预设环境失败", str(exc), "failed")
            print(f"预设环境失败：{identity[1]}: {exc}", file=sys.stderr, flush=True)
        finally:
            client.close()
            with self.lock:
                current = self.active_runs.get(identity)
                if current is not None and current.get("client") is client:
                    self.active.discard(identity)
                    self._release_active_run(identity)

    # ---------- 需求总体测试会话 ----------

    @staticmethod
    def _requirement_testing_item_key(requirement_key: str) -> str:
        return f"{REQUIREMENT_TESTING_ITEM_KEY}:{requirement_key}"

    @staticmethod
    def _requirement_testing_identity(program_id: int, requirement_key: str) -> tuple[str, int, str]:
        return task_identity("", program_id, ExecutionBridge._requirement_testing_item_key(requirement_key))

    def _load_requirement_testing_session(
        self, config: dict[str, Any], program_id: int, requirement_key: str, provider: str, thread_id: str = "",
    ) -> dict[str, Any] | None:
        # 不按执行器过滤：换工具之后也要能看见此前那批聊天。
        rows = planner.request_api(
            config, "GET", "/delivery/requirement/testing-sessions",
            query={"programId": program_id, "requirementKey": requirement_key},
        )
        rows = [
            row for row in (rows or [])
            if isinstance(row, dict) and str(row.get("threadId") or "") and same_executor_purpose(row, "")
        ]
        if not rows:
            return None
        catalog = [
            {
                "threadId": str(row.get("threadId") or ""), "title": str(row.get("title") or ""),
                "createdAt": str(row.get("createdAt") or ""), "updatedAt": str(row.get("updatedAt") or ""),
                "status": str(row.get("status") or "completed"),
                "executorType": executor_provider_of(row, provider), "active": False,
            }
            for row in rows
        ]
        current = next((row for row in rows if str(row.get("threadId") or "") == thread_id), rows[-1])
        metadata = current.get("metadata") if isinstance(current.get("metadata"), dict) else {}
        return {
            "threadId": str(current.get("threadId") or ""), "turnId": str(metadata.get("turnId") or ""),
            "executorType": executor_provider_of(current, provider),
            "detailDigest": str(metadata.get("detailDigest") or ""),
            "requirementKey": requirement_key, "catalog": catalog,
        }

    def _save_requirement_testing_session(
        self, config: dict[str, Any], program_id: int, requirement_key: str, provider: str, session: dict[str, Any],
    ) -> None:
        thread_id = str(session.get("threadId") or "")
        if not requirement_key or not thread_id:
            return
        entry = next((item for item in session.get("catalog") or [] if str(item.get("threadId") or "") == thread_id), {})
        provider = executor_provider_of(entry, session.get("executorType") or provider)
        try:
            planner.request_api(
                config, "POST", "/delivery/requirement/testing-session/bind",
                body={
                    "programId": program_id, "requirementKey": requirement_key, "executorType": provider,
                    "threadId": thread_id, "title": str(entry.get("title") or "")[:120],
                    "status": str(entry.get("status") or "running"),
                    "metadata": {
                        "turnId": str(session.get("turnId") or ""), "kind": "requirement-testing",
                        "detailDigest": str(session.get("detailDigest") or ""),
                        "workspace": self.workspace.name,
                    },
                    "actorName": f"{provider}-http-bridge",
                },
            )
        except Exception as exc:
            print(f"保存需求测试会话目录失败：{program_id}/{requirement_key}: {exc}", file=sys.stderr, flush=True)

    def _update_requirement_testing(
        self, config: dict[str, Any], program_id: int, requirement_key: str,
        testing_status: str | None = None, report: str | None = None,
        testing_cases_status: str | None = None, testing_cases: str | None = None,
    ) -> None:
        body: dict[str, Any] = {
            "programId": program_id, "requirementKey": requirement_key, "actorName": "delivery-http-bridge",
        }
        if testing_status is not None:
            body["testingStatus"] = testing_status
        if report is not None:
            body["testingReport"] = report
        if testing_cases_status is not None:
            body["testingCasesStatus"] = testing_cases_status
        if testing_cases is not None:
            body["testingCases"] = testing_cases
        planner.request_api(config, "POST", "/delivery/requirement/testing/save", body=body)

    def _persist_requirement_testing_report(self, requirement_key: str, report: str) -> Path:
        relative = Path("doc") / "test" / requirement_key / "测试报告.md"
        if ".." in relative.parts or relative.is_absolute():
            raise BridgeFailure("需求测试报告路径无效")
        destination = (self.workspace / relative).resolve()
        try:
            destination.relative_to(self.workspace)
        except ValueError as exc:
            raise BridgeFailure("需求测试报告路径超出当前项目") from exc
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(report.rstrip() + "\n", encoding="utf-8")
        return destination

    def _persist_requirement_testing_cases(self, requirement_key: str, cases: str) -> Path:
        relative = Path("doc") / "test" / requirement_key / "测试用例.md"
        if ".." in relative.parts or relative.is_absolute():
            raise BridgeFailure("需求测试用例路径无效")
        destination = (self.workspace / relative).resolve()
        try:
            destination.relative_to(self.workspace)
        except ValueError as exc:
            raise BridgeFailure("需求测试用例路径超出当前项目") from exc
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(cases.rstrip() + "\n", encoding="utf-8")
        return destination

    def requirement_testing(
        self, program_id: int, requirement_key: str, thread_id: str = "", provider: str = "codex", config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        config = request_scoped_config(config, DEFAULT_BIZ_LINE, program_id)
        provider = ai_provider_of(provider)
        requirement_key = str(requirement_key or "").strip()
        if not requirement_key:
            raise BridgeFailure("缺少需求标识")
        requirement = self._requirement_for_prototype(config, program_id, requirement_key)
        session = self._load_requirement_testing_session(config, program_id, requirement_key, provider, thread_id)
        catalog = list((session or {}).get("catalog") or [])
        selected_thread_id = thread_id or str((session or {}).get("threadId") or "")
        identity = self._requirement_testing_identity(program_id, requirement_key)
        with self.lock:
            active = self.active_runs.get(identity)
        if not selected_thread_id:
            return {
                "programId": program_id, "requirementKey": requirement_key, "threadId": "", "executorType": provider, "turns": [],
                "conversations": catalog, "active": False, "activeTurnId": "", "testingReport": requirement.get("testingReport") or "",
                "testingStatus": requirement.get("testingStatus") or "todo", "testingReportPath": requirement.get("testingReportPath") or "",
                "testingCasesStatus": requirement.get("testingCasesStatus") or "todo", "testingCases": requirement.get("testingCases") or "",
                "testingCasesPath": requirement.get("testingCasesPath") or "",
            }
        provider = executor_provider_of(
            next((entry for entry in catalog if str(entry.get("threadId") or "") == selected_thread_id), {}),
            (session or {}).get("executorType") or provider,
        )
        live_client = active["client"] if active is not None and active.get("threadId") == selected_thread_id else None
        thread = self._read_thread_with_workspace_archive(
            live_client, selected_thread_id, "requirement", requirement_key, config, program_id,
            provider=provider,
            environment=codex_environment(config, program_id, write_allowed=True),
        )
        item_key = self._requirement_testing_item_key(requirement_key)
        for entry in catalog:
            entry["active"] = bool(active is not None and entry.get("threadId") == active.get("threadId"))
            if not entry["active"] and entry.get("status") == "running":
                entry["status"] = "interrupted"
        return {
            "programId": program_id, "requirementKey": requirement_key, "threadId": selected_thread_id,
            "executorType": provider,
            "turns": serialize_turns(
                thread.get("turns") or [],
                lambda attachment_ids: [ConversationAttachmentStore._public(attachment) for attachment in self.attachments.resolve(program_id, item_key, attachment_ids)],
                lambda paths: self.artifacts.register(config_biz_line(config), program_id, item_key, paths),
            ),
            "conversations": catalog,
            "active": bool(active is not None and active.get("threadId") == selected_thread_id),
            "activeTurnId": str((active or {}).get("turnId") or ""),
            "testingReport": requirement.get("testingReport") or "", "testingStatus": requirement.get("testingStatus") or "todo",
            "testingReportPath": requirement.get("testingReportPath") or "",
            "testingCasesStatus": requirement.get("testingCasesStatus") or "todo", "testingCases": requirement.get("testingCases") or "",
            "testingCasesPath": requirement.get("testingCasesPath") or "",
        }

    def send_requirement_testing(self, raw: Any, config: dict[str, Any]) -> dict[str, Any]:
        provider = ai_provider_of(raw)
        program_id, requirement_key, message, requested_thread_id, new_conversation, model, reasoning_effort, fast_mode, attachment_ids, test_case_only = validate_requirement_testing_payload(raw)
        assert_runtime_project(config, program_id)
        requirement = self._requirement_for_prototype(config, program_id, requirement_key)
        context = planner.project_context(config, program_id)
        item_key = self._requirement_testing_item_key(requirement_key)
        attachments = self.attachments.resolve(program_id, item_key, attachment_ids)
        identity = self._requirement_testing_identity(program_id, requirement_key)
        session = self._load_requirement_testing_session(config, program_id, requirement_key, provider, requested_thread_id)
        with self.lock:
            active = self.active_runs.get(identity)
        if active is not None:
            if new_conversation or (requested_thread_id and requested_thread_id != active.get("threadId")):
                raise BridgeFailure("当前需求已有正在运行的总体测试会话，请先停止或等待完成")
            active["client"].steer_turn(
                str(active["threadId"]), str(active["turnId"]), message_with_attachments(message, attachments), attachments,
                request_id=active["client"].next_request_id(),
            )
            self.progress.publish(identity, "message", "已追加测试要求", message, "running")
            return {"accepted": True, "programId": program_id, "requirementKey": requirement_key, "threadId": active["threadId"], "turnId": active["turnId"], "active": True}
        catalog = list((session or {}).get("catalog") or [])
        known_thread_ids = {str(entry.get("threadId") or "") for entry in catalog}
        if requested_thread_id and requested_thread_id not in known_thread_ids:
            raise BridgeFailure("所选需求测试会话不存在")
        if not session or new_conversation or not session.get("threadId"):
            if len(catalog) >= MAX_PLANNING_CONVERSATIONS:
                raise BridgeFailure("该需求保留的测试会话已达上限")
            title = (
                f"{requirement.get('name') or requirement_key} · 测试用例"
                if test_case_only else f"需求总体测试 · {requirement.get('name') or requirement_key}"
            )
            if catalog:
                title = f"{title} V{len(catalog) + 1}"
            client = create_ai_client(
                provider, self.workspace, lambda event: self._publish_app_server_event(identity, event),
                codex_environment(config, program_id, write_allowed=True),
            )
            try:
                thread_id, turn_id = client.start_task(
                    title, message_with_attachments(build_requirement_testing_prompt(program_id, context, requirement, message, self.workspace, test_case_only), attachments), attachments,
                    model=model, reasoning_effort=reasoning_effort, fast_mode=fast_mode,
                )
            except Exception:
                client.close()
                raise
            session = {
                "threadId": thread_id, "turnId": turn_id, "requirementKey": requirement_key,
                "detailDigest": planning_detail_digest(requirement),
                "catalog": [*catalog, {"threadId": thread_id, "title": title, "createdAt": utc_now(), "updatedAt": utc_now(), "status": "running", "active": True}],
            }
        else:
            thread_id = requested_thread_id or str(session.get("threadId") or "")
            # 已有会话只能用它自己的执行器续：线程正文在那个执行器的缓存里，换工具读不到。
            provider = executor_provider_of(
                next((entry for entry in catalog if str(entry.get("threadId") or "") == thread_id), {}),
                session.get("executorType") or provider,
            )
            detail_digest = planning_detail_digest(requirement)
            client = create_ai_client(
                provider, self.workspace, lambda event: self._publish_app_server_event(identity, event),
                codex_environment(config, program_id, write_allowed=True),
            )
            try:
                client.resume_thread(thread_id)
                turn_id = client.start_turn(
                    thread_id, message_with_attachments(build_requirement_testing_prompt(
                        program_id, context, requirement, message, self.workspace, test_case_only,
                        follow_up=True, include_detail=detail_digest != str(session.get("detailDigest") or ""),
                    ), attachments), attachments,
                    request_id=client.next_request_id(), model=model, reasoning_effort=reasoning_effort, fast_mode=fast_mode,
                )
            except Exception:
                client.close()
                raise
            session.update({"threadId": thread_id, "turnId": turn_id, "detailDigest": detail_digest})
            for entry in session.get("catalog") or []:
                if entry.get("threadId") == thread_id:
                    entry.update({"status": "running", "active": True, "updatedAt": utc_now()})
        with self.lock:
            self.active.add(identity)
            self.active_runs[identity] = {"client": client, "threadId": thread_id, "turnId": turn_id, "requirementTesting": True, "testCaseOnly": test_case_only, "provider": provider, "config": config, "programId": program_id, "requirementKey": requirement_key}
        self._save_requirement_testing_session(config, program_id, requirement_key, provider, session)
        self._update_requirement_testing(
            config, program_id, requirement_key,
            testing_cases_status="doing" if test_case_only else None,
            testing_status=None if test_case_only else "doing",
        )
        self.progress.publish(
            identity, "status", "正在生成需求测试用例" if test_case_only else "正在进行需求总体测试",
            f"{provider_label(provider)} 正在{'设计测试用例' if test_case_only else '准备并执行需求级测试'}。", "running",
        )
        threading.Thread(
            target=self._follow_requirement_testing,
            args=(identity, client, config, program_id, requirement_key, provider, session, thread_id, turn_id, test_case_only), daemon=True,
        ).start()
        return {"accepted": True, "programId": program_id, "requirementKey": requirement_key, "threadId": thread_id, "turnId": turn_id, "active": True}

    def stop_requirement_testing(self, raw: Any, config: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise BridgeFailure("请求体必须是 JSON 对象")
        program_id = program_id_of(raw.get("programId"))
        requirement_key = str(raw.get("requirementKey") or "").strip()
        assert_runtime_project(config, program_id)
        identity = self._requirement_testing_identity(program_id, requirement_key)
        with self.lock:
            active = self.active_runs.get(identity)
        if active is None or not active.get("requirementTesting"):
            raise BridgeFailure("该需求当前没有正在运行的总体测试会话")
        requested_thread_id = str(raw.get("threadId") or "").strip()
        if requested_thread_id and requested_thread_id != active.get("threadId"):
            raise BridgeFailure("所选需求测试会话当前没有正在运行的回合")
        active["client"].interrupt_turn(str(active["threadId"]), str(active["turnId"]), request_id=active["client"].next_request_id())
        self.progress.publish(identity, "status", "已请求停止测试", "正在等待测试回合中断。", "running")
        return {"accepted": True, "programId": program_id, "requirementKey": requirement_key, "threadId": active["threadId"], "turnId": active["turnId"], "active": True}

    def _follow_requirement_testing(
        self, identity: tuple[str, int, str], client: AppServerClient, config: dict[str, Any], program_id: int,
        requirement_key: str, provider: str, session: dict[str, Any], thread_id: str, turn_id: str, test_case_only: bool = False,
    ) -> None:
        try:
            turn_status = client.wait_turn(turn_id)
            entry = next(
                (item for item in session.get("catalog") or [] if str(item.get("threadId") or "") == thread_id),
                {},
            )
            title = str(entry.get("title") or ("需求测试用例" if test_case_only else "需求总体测试"))
            self._archive_terminal_chat(
                client,
                config=config,
                program_id=program_id,
                resource_kind="requirement",
                resource_key=requirement_key,
                resource_name=title,
                conversation_title=title,
                thread_id=thread_id,
                provider=provider,
                phase="testing-cases" if test_case_only else "testing",
                terminal_status=turn_status,
            )
            turn = client.read_turn(thread_id, turn_id, request_id=client.next_request_id())
            report = final_agent_text_from_output(execution_output(turn_status, turn))
            verdict = testing_verdict_from_output(report)
            if test_case_only:
                cases_status = "ready" if turn_status == "completed" else "blocked"
                self._persist_requirement_testing_cases(requirement_key, report)
                self._update_requirement_testing(config, program_id, requirement_key, testing_cases_status=cases_status, testing_cases=report)
            else:
                # 回合没有正常结束时，即使输出里碰巧有“通过”，也不能把需求总体测试验收为通过。
                # 这和任务级测试一致：只有完整执行且明确给出通过判定，状态才可进入 passed。
                status = (
                    {"通过": "passed", "不通过": "failed", "受阻": "blocked"}.get(verdict, "blocked")
                    if turn_status == "completed" else "blocked"
                )
                self._persist_requirement_testing_report(requirement_key, report)
                self._update_requirement_testing(config, program_id, requirement_key, testing_status=status, report=report)
            for entry in session.get("catalog") or []:
                if entry.get("threadId") == thread_id:
                    entry.update({"status": turn_status, "active": False, "updatedAt": utc_now()})
            session["turnId"] = turn_id
            self._save_requirement_testing_session(config, program_id, requirement_key, provider, session)
            self.progress.publish(
                identity, "status",
                ("需求测试用例已生成" if turn_status == "completed" else "需求测试用例未完成") if test_case_only else ("需求总体测试已完成" if turn_status == "completed" else "需求总体测试未完成"),
                "测试用例已同步到需求。" if test_case_only else f"验收判定：{verdict or '缺失'}。报告已同步到需求。", turn_status,
            )
        except Exception as exc:
            try:
                self._update_requirement_testing(
                    config, program_id, requirement_key,
                    testing_cases_status="blocked" if test_case_only else None,
                    testing_status=None if test_case_only else "blocked",
                )
            except Exception:
                pass
            self.progress.publish(identity, "error", "同步需求测试结果失败", str(exc), "failed")
            print(f"同步需求测试结果失败：{program_id}/{requirement_key}: {exc}", file=sys.stderr, flush=True)
        finally:
            client.close()
            with self.lock:
                current = self.active_runs.get(identity)
                if current is None or current.get("client") is client:
                    self.active.discard(identity)
                    self._release_active_run(identity)

    def models(self, config: dict[str, Any], provider: str = "codex") -> dict[str, Any]:
        program_id = program_id_of(config.get("_project_id"))
        assert_runtime_project(config, program_id)
        if provider == "codex":
            return {"defaultModel": "gpt-5.6-terra", "models": list(CODEX_MODEL_CATALOG)}
        client = create_ai_client(provider, self.workspace, environment=codex_environment(config, program_id))
        try:
            models = []
            for item in client.list_models():
                model = str(item.get("model") or "").strip()
                if not model or item.get("hidden"):
                    continue
                models.append({
                    "model": model,
                    "displayName": str(item.get("displayName") or model),
                    "description": str(item.get("description") or ""),
                })
            return {"defaultModel": "", "models": models}
        finally:
            client.close()

    def health(self, provider: str = "codex") -> dict[str, Any]:
        provider = ai_provider_of(provider)
        codex_cli = available_codex_cli()
        claude_cli = shutil.which("claude")
        executable_available = bool(codex_cli) if provider == "codex" else claude_cli is not None
        configured = True
        api_reachable = True
        message = "ready"
        if not executable_available:
            message = f"未找到 {provider_label(provider)} CLI"
        ready = executable_available and configured and api_reachable
        return {
            "ready": ready,
            "bridge": True,
            "codex": bool(codex_cli),
            "claude": claude_cli is not None,
            "configured": configured,
            "apiReachable": api_reachable,
            "executorType": provider,
            # 占位目录不是任何项目的仓库，别把它当成"当前工作区"报给面板。
            "workspace": "" if self.workspace == placeholder_workspace() else self.workspace.name,
            "message": message,
            "checkedAt": int(time.time()),
        }

    def active_run_count(self) -> int:
        """Count in-flight runs across every workspace owned by this bridge."""
        with self.workspace_bridges_lock:
            bridges = list(self.workspace_bridges.values())
        count = 0
        for bridge in bridges:
            with bridge.lock:
                count += len(bridge.active_runs)
        return count

    def request_config(self, raw: Any, origin: str, token: str) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise BridgeFailure("请求体必须是 JSON 对象")
        program_id = program_id_of(raw.get("programId"))
        if not program_id:
            raise BridgeFailure("缺少项目标识")
        if not token:
            raise BridgeFailure("当前用户凭证为空")
        api_url = self._resolve_task_board_api(str(raw.get("apiUrl") or "").strip(), origin, token, program_id)
        # 走到这里这个凭证已经被面板验过了（_resolve_task_board_api 打过一次真实接口），
        # 此时才落盘：普通命令行会话没有运行期环境变量，只能读那份文件，切账号后不刷新
        # 就会继续拿旧账号写入，而面板那边报出来只是一句权限不足，排查方向会被带偏。
        planner.remember_browser_identity(token, str(raw.get("userId") or "").strip())
        config = {
            "api_url": api_url,
            "key": token,
            "key_header": "token",
            "user_id": str(raw.get("userId") or "task-executor").strip() or "task-executor",
            "_project_id": program_id,
        }
        context = planner.project_context(config, program_id)
        program = context.get("program") or {}
        if program_id_of(program.get("programId")) != program_id:
            raise BridgeFailure("任务面板项目上下文校验失败")
        return config

    @staticmethod
    def global_environment_config(raw: Any, token: str) -> dict[str, Any]:
        """环境检测只需当前用户凭证，不读取或校验任何任务面板项目。"""
        if not isinstance(raw, dict):
            raise BridgeFailure("请求体必须是 JSON 对象")
        if not token:
            raise BridgeFailure("当前用户凭证为空")
        # 环境检测不打任何面板接口，凭证没被验证过；只认得出用户的面板 JWT 才落盘。
        if planner.token_subject(token):
            planner.remember_browser_identity(token, str(raw.get("userId") or "").strip())
        return {
            "key": token,
            "key_header": "token",
            "user_id": str(raw.get("userId") or "task-executor").strip() or "task-executor",
        }

    @staticmethod
    def _resolve_task_board_api(explicit_url: str, origin: str, token: str, program_id: int) -> str:
        """Use the configured bridge target, never a browser-provided address."""
        del explicit_url, origin
        candidates = [planner.bridge_api_url()]
        last_error: Exception | None = None
        for candidate in candidates:
            try:
                normalized = planner.normalize_api_url(candidate)
            except planner.ToolFailure as exc:
                last_error = exc
                continue
            config = {
                "api_url": normalized,
                "key": token,
                "key_header": "token",
                "user_id": "task-executor",
            }
            try:
                planner.request_api(
                    config,
                    "GET",
                    "/delivery/program",
                    query={"programId": program_id},
                )
                return normalized
            except planner.ToolFailure as exc:
                last_error = exc
        raise BridgeFailure(f"无法连接任务面板接口：{last_error or '没有可用地址'}")

    def _claim_task(self, config: dict[str, Any], program_id: int, task: dict[str, Any], comment: str, provider: str = "codex") -> dict[str, Any]:
        updated = self._request_with_retry(
            config,
            "/delivery/item/patch",
            {
                "programId": program_id,
                "itemKey": str(task["itemKey"]),
                "version": int(task["version"]),
                "status": "doing",
                "progress": max(1, int(task.get("progress") or 0)),
                "ownerName": provider_label(provider),
                "comment": comment,
                "actorName": f"{provider}-http-bridge",
            },
        )
        if not isinstance(updated, dict) or updated.get("status") != "doing":
            raise BridgeFailure(f"任务面板未确认任务已进入进行中，已取消启动 {provider_label(provider)} 会话")
        return {**task, **updated}

    def _register_queue(self, queue_id: str, program_id: int) -> None:
        with self.lock:
            self.queue_programs[queue_id] = program_id

    def _release_queue(self, queue_id: str) -> None:
        with self.lock:
            self.queue_programs.pop(queue_id, None)
            self.cancelled_queues.discard(queue_id)

    def _abort_if_cancelled(self, queue_id: str) -> None:
        """队列每启动一批任务前问一次：用户已经点过停止就别再往下拉了。"""
        with self.lock:
            cancelled = queue_id in self.cancelled_queues
        if cancelled:
            raise BridgeFailure("执行队列已被用户停止")

    @staticmethod
    def _create_execution_batch(
        config: dict[str, Any],
        program_id: int,
        item_keys: list[str],
        mode: str,
        provider: str,
    ) -> dict[str, Any]:
        """Create the authoritative server-side record before the local queue starts."""
        batch = planner.request_api(
            config,
            "POST",
            "/delivery/execution-batch/create",
            body={
                "programId": program_id,
                "itemKeys": item_keys,
                "mode": mode,
                "executorType": provider,
                "actorName": f"{provider}-http-bridge",
            },
        )
        if not isinstance(batch, dict) or not str(batch.get("batchId") or "").strip():
            raise BridgeFailure("任务面板没有返回有效的执行批次标识")
        return batch

    @staticmethod
    def _update_execution_batch_item(
        config: dict[str, Any],
        program_id: int,
        batch_id: str,
        item_key: str,
        status: str,
        message: str = "",
        provider: str = "codex",
    ) -> None:
        if not batch_id:
            return
        ExecutionBridge._request_with_retry(
            config,
            "/delivery/execution-batch/item/status",
            {
                "programId": program_id,
                "batchId": batch_id,
                "itemKey": item_key,
                "status": status,
                "message": message,
                "actorName": f"{provider}-http-bridge",
            },
        )

    @staticmethod
    def _finalize_execution_batch(
        config: dict[str, Any],
        program_id: int,
        batch_id: str,
        status: str,
        summary: str,
        provider: str = "codex",
    ) -> None:
        if not batch_id:
            return
        ExecutionBridge._request_with_retry(
            config,
            "/delivery/execution-batch/finalize",
            {
                "programId": program_id,
                "batchId": batch_id,
                "status": status,
                "summary": summary,
                "actorName": f"{provider}-http-bridge",
            },
        )

    def _release_failed_claim(self, config: dict[str, Any], program_id: int, task: dict[str, Any], provider: str = "codex") -> None:
        try:
            self._request_with_retry(
                config,
                "/delivery/item/patch",
                {
                    "programId": program_id,
                    "itemKey": str(task["itemKey"]),
                    "version": int(task["version"]),
                    "status": "todo",
                    "progress": 0,
                    "comment": f"{provider_label(provider)} 会话启动失败，任务已自动恢复为未开始。",
                    "actorName": f"{provider}-http-bridge",
                },
            )
        except Exception as exc:
            print(f"恢复启动失败任务状态失败：{program_id}/{task.get('itemKey')}: {exc}", file=sys.stderr, flush=True)

    def reconcile(self) -> None:
        # Board operations always receive a current user token and one project ID
        # from the browser. A process-wide recovery scan would require persisting a
        # credential and would violate that scope, so recovery is intentionally UI-led.
        return

    def _reconcile_pending_session_syncs(self, config: dict[str, Any]) -> None:
        for entry in self.pending_session_syncs.snapshot():
            try:
                self._request_with_retry(
                    scoped_config(config, str(entry.get("bizLine") or DEFAULT_BIZ_LINE)),
                    "/delivery/item/execution-session/status",
                    entry,
                )
                self.pending_session_syncs.remove(entry)
            except Exception as exc:
                print(
                    f"重试关闭执行会话失败：{entry.get('programId')}/{entry.get('itemKey')}: {exc}",
                    file=sys.stderr,
                    flush=True,
                )

    def reconcile_forever(self, interval: float = 5) -> None:
        while True:
            self.reconcile()
            time.sleep(interval)

    @staticmethod
    def _task_testing_cases_identity(program_id: int, item_key: str, provider: str = "codex") -> tuple[str, int, str]:
        return task_identity("", program_id, f"__testing_cases__:{ai_provider_of(provider)}:{item_key}")

    def _persist_task_testing_cases(self, item_key: str, cases: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", item_key):
            raise BridgeFailure("任务测试用例路径无效")
        relative = Path("doc") / "test" / item_key / "测试用例.md"
        destination = (self.workspace / relative).resolve()
        try:
            destination.relative_to(self.workspace)
        except ValueError as exc:
            raise BridgeFailure("任务测试用例路径超出当前项目") from exc
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(cases.rstrip() + "\n", encoding="utf-8")
        return destination

    def _task_testing_cases_binding(
        self, config: dict[str, Any], program_id: int, item_key: str, provider: str,
    ) -> dict[str, Any] | None:
        """The task execution-session table also keeps the compact test-case chat directory.

        Query all phases so a useful testing-case chat remains readable if the task itself
        advances while the cases are being designed.
        """
        rows = self._task_testing_cases_bindings(config, program_id, item_key, provider)
        return rows[-1] if rows else None

    def _task_testing_cases_bindings(
        self, config: dict[str, Any], program_id: int, item_key: str, provider: str,
    ) -> list[dict[str, Any]]:
        executor_type = task_testing_cases_executor_type(provider)
        # 只比用途后缀，不比工具：换成另一个工具之后，之前那批用例聊天也要留在列表里。
        sessions = planner.request_api(
            config,
            "GET",
            "/delivery/item/execution-session",
            query={"programId": program_id, "itemKey": item_key},
        ) or []
        rows = [
            session for session in sessions
            if isinstance(session, dict) and same_executor_purpose(session, executor_type)
        ]
        return rows

    @staticmethod
    def _task_testing_cases_title(task: dict[str, Any], binding: dict[str, Any] | None = None) -> str:
        base = f"{' '.join(str(task.get('title') or task.get('itemKey') or '任务').split())} · 测试用例"
        version = next_conversation_version(binding)
        if version:
            suffix = f" V{version + 1}"
            return f"{base[:80 - len(suffix)].rstrip()}{suffix}"
        return base[:80]

    def _bind_task_testing_cases_session(
        self,
        config: dict[str, Any],
        program_id: int,
        item_key: str,
        task: dict[str, Any],
        provider: str,
        binding: dict[str, Any] | None,
        thread_id: str,
        turn_id: str,
        title: str = "",
        status: str = "running",
    ) -> dict[str, Any]:
        task_phase = str(task.get("phase") or "requirement")
        binding_phase = str((binding or {}).get("phase") or task_phase)
        existing_thread_id = str((binding or {}).get("externalSessionId") or "")
        # 任务有可能在整理用例期间进入下一阶段。外部 thread id 在会话表中全局唯一，
        # 不能把同一条 thread 再绑到新阶段；此时仅更新原绑定的目录和运行状态即可。
        phase = binding_phase if existing_thread_id == thread_id else task_phase
        metadata = conversation_metadata(binding, thread_id, turn_id, status, title, phase)
        metadata.update({"workspace": self.workspace.name, "source": "task-testing-cases"})
        if binding and existing_thread_id == thread_id and binding_phase != task_phase:
            version = int(binding.get("version") or 0)
            if version <= 0:
                raise BridgeFailure("任务测试用例会话版本无效，请刷新后重试")
            return self._request_with_retry(
                config,
                "/delivery/item/execution-session/status",
                {
                    "programId": program_id,
                    "itemKey": item_key,
                    "executorType": task_testing_cases_executor_type(provider),
                    "phase": binding_phase,
                    "version": version,
                    "status": SESSION_STATUS.get(status, "running"),
                    "progress": 0,
                    "metadata": metadata,
                    "actorName": f"{provider}-http-bridge",
                },
            )
        return planner.request_api(
            config,
            "POST",
            "/delivery/item/execution-session/bind",
            body={
                "programId": program_id,
                "itemKey": item_key,
                "executorType": task_testing_cases_executor_type(provider),
                "phase": phase,
                "progress": 0,
                "externalSessionId": thread_id,
                "status": SESSION_STATUS.get(status, "running"),
                "metadata": metadata,
                "actorName": f"{provider}-http-bridge",
            },
        )

    def task_testing_cases_conversation(
        self,
        program_id: int,
        item_key: str,
        selected_thread_id: str = "",
        provider: str = "codex",
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        provider = ai_provider_of(provider)
        config = request_scoped_config(config, DEFAULT_BIZ_LINE, program_id)
        task = self._task_detail(config, program_id, item_key)
        bindings = self._task_testing_cases_bindings(config, program_id, item_key, provider)
        binding = bindings[-1] if bindings else None
        catalog, binding_by_thread = merged_conversation_catalog(bindings)
        current_thread_id = str((binding or {}).get("externalSessionId") or "")
        known_thread_ids = {str(entry.get("threadId") or "") for entry in catalog}
        if selected_thread_id and selected_thread_id not in known_thread_ids:
            raise BridgeFailure("所选任务测试用例会话不存在")
        thread_id = selected_thread_id or current_thread_id or (str(catalog[0].get("threadId") or "") if catalog else "")
        binding = binding_by_thread.get(thread_id, binding)
        # 线程正文在它自己那个执行器的缓存里：读跟着线程走，不跟当前选中的工具走。
        provider = executor_provider_of(binding, provider)
        current_thread_id = str((binding or {}).get("externalSessionId") or "")
        identity = self._task_testing_cases_identity(program_id, item_key, provider)
        with self.lock:
            active = self.active_runs.get(identity)
        if not thread_id:
            return {
                "programId": program_id, "itemKey": item_key, "threadId": "", "executorType": provider, "turns": [], "conversations": [],
                "active": False, "activeTurnId": "", "testingCasesStatus": task.get("testingCasesStatus") or "todo",
                "testingCases": task.get("testingCases") or "", "testingCasesPath": task.get("testingCasesPath") or "",
            }
        active_for_thread = active if active is not None and active.get("threadId") == thread_id else None
        metadata = (binding or {}).get("metadata") if isinstance((binding or {}).get("metadata"), dict) else {}
        running_turn_id = str(metadata.get("turnId") or "")
        if (
            active_for_thread is None and binding and binding.get("status") == "running"
            and current_thread_id == thread_id and running_turn_id
        ):
            active_for_thread = self._resume_task_testing_cases_turn(
                config, identity, task, binding, provider, thread_id, running_turn_id,
            )
        live_client = active_for_thread["client"] if active_for_thread is not None else None
        thread = self._read_thread_with_workspace_archive(
            live_client, thread_id, "task", item_key, config, program_id,
            provider=provider,
            environment=codex_environment(config, program_id, write_allowed=True),
        )
        for entry in catalog:
            entry["active"] = bool(active_for_thread is not None and entry.get("threadId") == thread_id)
            if not entry["active"] and entry.get("status") == "running":
                entry["status"] = "interrupted"
        return {
            "programId": program_id, "itemKey": item_key, "threadId": thread_id, "executorType": provider,
            "turns": serialize_turns(thread.get("turns") or []), "conversations": catalog,
            "active": active_for_thread is not None,
            "activeTurnId": str((active_for_thread or {}).get("turnId") or ""),
            "testingCasesStatus": task.get("testingCasesStatus") or "todo",
            "testingCases": task.get("testingCases") or "",
            "testingCasesPath": task.get("testingCasesPath") or "",
        }

    def _resume_task_testing_cases_turn(
        self,
        config: dict[str, Any],
        identity: tuple[str, int, str],
        task: dict[str, Any],
        binding: dict[str, Any],
        provider: str,
        thread_id: str,
        turn_id: str,
    ) -> dict[str, Any]:
        with self.lock:
            existing = self.active_runs.get(identity)
            if existing is not None:
                return existing
            if identity in self.active:
                raise BridgeFailure("该任务测试用例会话正在恢复，请稍后重试")
            self.active.add(identity)
        client = create_ai_client(
            provider, self.workspace, lambda event: self._publish_app_server_event(identity, event),
            codex_environment(config, identity[1], write_allowed=True),
        )
        try:
            client.resume_thread(thread_id)
            active = {
                "client": client, "threadId": thread_id, "turnId": turn_id, "taskTestingCases": True,
                "task": task, "binding": binding, "config": config, "provider": provider,
            }
            with self.lock:
                self.active_runs[identity] = active
            threading.Thread(
                target=self._follow_task_testing_cases,
                args=(identity, client, config, identity[1], identity[2].rsplit(":", 1)[-1], provider, thread_id, turn_id, task, binding),
                daemon=True,
            ).start()
            return active
        except Exception:
            client.close()
            with self.lock:
                self.active.discard(identity)
                self._release_active_run(identity)
            raise

    def generate_task_testing_cases(self, raw: Any, config: dict[str, Any]) -> dict[str, Any]:
        """Start or continue a design-only test-case chat without claiming the task."""
        provider = ai_provider_of(raw)
        program_id, item_key, message, requested_thread_id, new_conversation, model, reasoning_effort, fast_mode = validate_task_testing_cases_payload(raw)
        config = request_scoped_config(config, "", program_id)
        context = planner.project_context(config, program_id)
        task = next((item for item in context.get("items") or [] if str(item.get("itemKey") or "") == item_key), None)
        if task is None:
            raise BridgeFailure("任务不存在")
        if str(task.get("status") or "") == "dropped":
            raise BridgeFailure("已中断的任务不能生成测试用例")
        task = self._task_detail(config, program_id, item_key)
        identity = self._task_testing_cases_identity(program_id, item_key, provider)
        bindings = self._task_testing_cases_bindings(config, program_id, item_key, provider)
        binding = bindings[-1] if bindings else None
        catalog, binding_by_thread = merged_conversation_catalog(bindings)
        known_thread_ids = {str(entry.get("threadId") or "") for entry in catalog}
        if requested_thread_id and requested_thread_id not in known_thread_ids:
            raise BridgeFailure("所选任务测试用例会话不存在")
        if requested_thread_id:
            binding = binding_by_thread.get(requested_thread_id, binding)
        if not new_conversation:
            # 续已有会话只能用这条线程自己的执行器；identity 里带着 provider，要一起改。
            provider = executor_provider_of(binding, provider)
            identity = self._task_testing_cases_identity(program_id, item_key, provider)
        with self.lock:
            active = self.active_runs.get(identity)
        if active is not None:
            if new_conversation or (requested_thread_id and requested_thread_id != active.get("threadId")):
                raise BridgeFailure("该任务已有正在运行的测试用例会话，请先停止或等待完成")
            active["client"].steer_turn(
                str(active["threadId"]), str(active["turnId"]), message,
                request_id=active["client"].next_request_id(),
            )
            self.progress.publish(identity, "message", "已追加测试用例要求", message, "running")
            return {"accepted": True, "programId": program_id, "itemKey": item_key, "threadId": active["threadId"], "turnId": active["turnId"], "active": True}
        current_thread_id = str((binding or {}).get("externalSessionId") or "")
        metadata = (binding or {}).get("metadata") if isinstance((binding or {}).get("metadata"), dict) else {}
        running_turn_id = str(metadata.get("turnId") or "")
        if binding and binding.get("status") == "running" and current_thread_id and running_turn_id:
            if new_conversation or (requested_thread_id and requested_thread_id != current_thread_id):
                raise BridgeFailure("该任务已有正在运行的测试用例会话，请先停止或等待完成")
            active = self._resume_task_testing_cases_turn(
                config, identity, task, binding, provider, current_thread_id, running_turn_id,
            )
            active["client"].steer_turn(
                current_thread_id, running_turn_id, message, request_id=active["client"].next_request_id(),
            )
            self.progress.publish(identity, "message", "已追加测试用例要求", message, "running")
            return {"accepted": True, "programId": program_id, "itemKey": item_key, "threadId": current_thread_id, "turnId": running_turn_id, "active": True}
        with self.lock:
            if identity in self.active:
                raise BridgeFailure("该任务正在生成测试用例")
            self.active.add(identity)
        client = create_ai_client(
            provider, self.workspace, lambda event: self._publish_app_server_event(identity, event),
            codex_environment(config, program_id, write_allowed=True),
        )
        try:
            thread_id = requested_thread_id or current_thread_id
            if not thread_id or new_conversation:
                title = self._task_testing_cases_title(task, binding)
                thread_id, turn_id = client.start_task(
                    title, build_task_testing_cases_prompt(program_id, task, context, message, self.workspace),
                    model=model, reasoning_effort=reasoning_effort, fast_mode=fast_mode,
                )
            else:
                title = ""
                client.resume_thread(thread_id)
                turn_id = client.start_turn(
                    thread_id, build_task_testing_cases_prompt(
                        program_id, task, context, message, self.workspace, follow_up=True,
                    ),
                    request_id=client.next_request_id(), model=model, reasoning_effort=reasoning_effort, fast_mode=fast_mode,
                )
            refreshed_binding = self._bind_task_testing_cases_session(
                config, program_id, item_key, task, provider, binding, thread_id, turn_id, title,
            )
            planner.request_api(
                config, "POST", "/delivery/item/testing-cases/save",
                body={"programId": program_id, "itemKey": item_key, "testingCasesStatus": "doing", "actorName": f"{provider}-http-bridge"},
            )
            with self.lock:
                self.active_runs[identity] = {
                    "client": client, "threadId": thread_id, "turnId": turn_id, "taskTestingCases": True,
                    "task": task, "binding": refreshed_binding, "config": config, "provider": provider,
                }
            self.progress.publish(identity, "status", "正在生成任务测试用例", f"{provider_label(provider)} 正在梳理测试范围和用例。", "running")
            threading.Thread(
                target=self._follow_task_testing_cases,
                args=(identity, client, config, program_id, item_key, provider, thread_id, turn_id, task, refreshed_binding), daemon=True,
            ).start()
            return {"accepted": True, "programId": program_id, "itemKey": item_key, "threadId": thread_id, "turnId": turn_id, "active": True}
        except Exception:
            client.close()
            try:
                planner.request_api(
                    config, "POST", "/delivery/item/testing-cases/save",
                    body={"programId": program_id, "itemKey": item_key, "testingCasesStatus": "blocked", "actorName": f"{provider}-http-bridge"},
                )
            except Exception:
                pass
            with self.lock:
                self.active.discard(identity)
                self._release_active_run(identity)
            raise

    def stop_task_testing_cases(self, raw: Any, config: dict[str, Any]) -> dict[str, Any]:
        provider = ai_provider_of(raw)
        program_id, item_key, _message, requested_thread_id, _new, _model, _effort, _fast = validate_task_testing_cases_payload(raw)
        config = request_scoped_config(config, "", program_id)
        identity = self._task_testing_cases_identity(program_id, item_key, provider)
        with self.lock:
            active = self.active_runs.get(identity)
        if active is None:
            task = self._task_detail(config, program_id, item_key)
            bindings = self._task_testing_cases_bindings(config, program_id, item_key, provider)
            binding = bindings[-1] if bindings else None
            if requested_thread_id:
                _catalog, binding_by_thread = merged_conversation_catalog(bindings)
                binding = binding_by_thread.get(requested_thread_id, binding)
            metadata = (binding or {}).get("metadata") if isinstance((binding or {}).get("metadata"), dict) else {}
            thread_id = str((binding or {}).get("externalSessionId") or "")
            turn_id = str(metadata.get("turnId") or "")
            if not binding or binding.get("status") != "running" or not thread_id or not turn_id:
                raise BridgeFailure("该任务当前没有正在运行的测试用例会话")
            active = self._resume_task_testing_cases_turn(config, identity, task, binding, provider, thread_id, turn_id)
        if requested_thread_id and requested_thread_id != active.get("threadId"):
            raise BridgeFailure("所选任务测试用例会话当前没有正在运行的回合")
        active["client"].interrupt_turn(
            str(active["threadId"]), str(active["turnId"]), request_id=active["client"].next_request_id(),
        )
        self.progress.publish(identity, "status", "已请求停止测试用例生成", "正在等待当前回合中断。", "running")
        return {"accepted": True, "programId": program_id, "itemKey": item_key, "threadId": active["threadId"], "turnId": active["turnId"], "active": True}

    def _follow_task_testing_cases(
        self, identity: tuple[str, int, str], client: AppServerClient, config: dict[str, Any],
        program_id: int, item_key: str, provider: str, thread_id: str, turn_id: str,
        task: dict[str, Any] | None = None, binding: dict[str, Any] | None = None,
    ) -> None:
        try:
            turn_status = client.wait_turn(turn_id)
            task_name = str((task or {}).get("title") or item_key)
            self._archive_terminal_chat(
                client,
                config=config,
                program_id=program_id,
                resource_kind="task",
                resource_key=item_key,
                resource_name=task_name,
                requirement_key=str((task or {}).get("requirementKey") or ""),
                conversation_title=f"{task_name} · 测试用例",
                thread_id=thread_id,
                provider=provider,
                phase="testing-cases",
                terminal_status=turn_status,
            )
            turn = client.read_turn(thread_id, turn_id, request_id=client.next_request_id())
            cases = final_agent_text_from_output(execution_output(turn_status, turn))
            status = "ready" if turn_status == "completed" and cases.strip() else "blocked"
            if status == "ready":
                self._persist_task_testing_cases(item_key, cases)
            planner.request_api(
                config, "POST", "/delivery/item/testing-cases/save",
                body={
                    "programId": program_id, "itemKey": item_key, "testingCasesStatus": status,
                    "testingCases": cases, "actorName": f"{provider}-http-bridge",
                },
            )
            if binding is not None:
                phase = str(binding.get("phase") or (task or {}).get("phase") or "requirement")
                metadata = conversation_metadata(binding, thread_id, turn_id, turn_status, phase=phase)
                metadata.update({"workspace": self.workspace.name, "source": "task-testing-cases"})
                session_sync = {
                    "programId": program_id, "itemKey": item_key,
                    "executorType": task_testing_cases_executor_type(provider), "phase": phase,
                    "version": int(binding.get("version") or 0), "status": SESSION_STATUS.get(turn_status, "blocked"),
                    "progress": 100 if turn_status == "completed" else 0, "metadata": metadata,
                    "actorName": f"{provider}-http-bridge",
                }
                if session_sync["version"] > 0:
                    self._request_with_retry(config, "/delivery/item/execution-session/status", session_sync)
            self.progress.publish(
                identity, "status", "任务测试用例已生成" if status == "ready" else "任务测试用例未完成",
                "测试用例已归档，可继续在该测试用例对话中补充和调整。" if status == "ready" else "未取得可用测试用例，请补充范围或环境后重试。",
                turn_status,
            )
        except Exception as exc:
            try:
                planner.request_api(
                    config, "POST", "/delivery/item/testing-cases/save",
                    body={"programId": program_id, "itemKey": item_key, "testingCasesStatus": "blocked", "actorName": f"{provider}-http-bridge"},
                )
            except Exception:
                pass
            self.progress.publish(identity, "error", "同步任务测试用例失败", str(exc), "failed")
            print(f"同步任务测试用例失败：{program_id}/{item_key}: {exc}", file=sys.stderr, flush=True)
        finally:
            client.close()
            with self.lock:
                current = self.active_runs.get(identity)
                if current is None or current.get("client") is client:
                    self.active.discard(identity)
                    self._release_active_run(identity)

    def _ensure_requirement_git_branch(self, config: dict[str, Any], program_id: int, task: dict[str, Any]) -> str:
        """任务所属需求关联了 Git 分支时，验证工作目录已由用户确认切换。

        自动切分支会在多人和脏工作区场景中吞掉重要上下文，因此这里不再产生副作用；
        用户必须先在需求列表的 Git 检查里确认提交、暂存或直接切换。
        """
        requirement_key = str(task.get("requirementKey") or "").strip()
        if not requirement_key:
            return ""
        try:
            requirement = planner.request_api(
                config,
                "GET",
                "/delivery/requirement",
                query={"programId": program_id, "requirementKey": requirement_key},
            )
        except planner.ToolFailure as exc:
            raise BridgeFailure(f"读取需求 Git 设置失败：{exc}") from exc
        if not isinstance(requirement, dict) or not requirement.get("gitEnabled"):
            return ""
        branch = str(requirement.get("gitBranch") or "").strip()
        if not branch:
            return ""
        if not valid_git_branch_name(branch):
            raise BridgeFailure(f"需求关联的分支名不合法：{branch}")
        require_git_workspace(self.workspace)
        if not git_branch_exists(self.workspace, branch):
            raise BridgeFailure(f"本机不存在需求分支 {branch}，请先在需求窗口创建分支")
        current = git_current_branch(self.workspace)
        if current == branch:
            return branch
        raise BridgeFailure(
            f"当前项目位于分支 {current or 'HEAD'}，与需求分支 {branch} 不一致；请先在需求列表执行 Git 检查并确认切换"
        )

    def prepare_requirement_git_branch(self, raw: Any) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise BridgeFailure("请求体必须是 JSON 对象")
        branch = str(raw.get("branch") or "").strip()
        if not branch:
            raise BridgeFailure("缺少需求分支")
        with self.lock:
            busy = sorted(key for _, _, key in self.active)
        if busy:
            raise BridgeFailure(f"本机仍有任务在执行（{', '.join(busy)}），不能切换项目分支")
        remote = str(raw.get("remoteName") or "origin").strip() or "origin"
        # 没传 targets 的调用方（需求列表的分支检查）也要带上子项目，否则只有根目录跟着切。
        targets = git_subproject_targets_of(self.workspace, raw.get("targets"), branch, remote)
        return git_prepare_branch_targets(
            self.workspace,
            branch,
            str(raw.get("strategy") or "switch").strip(),
            str(raw.get("commitMessage") or ""),
            str(raw.get("expectedRemoteUrl") or ""),
            remote,
            targets,
        )

    def push_requirement_branch(self, raw: Any, config: dict[str, Any] | None = None) -> dict[str, Any]:
        """需求窗口的「推送到 Git」：先推子项目，最后提交并推送主项目。"""
        if not isinstance(raw, dict):
            raise BridgeFailure("请求体必须是 JSON 对象")
        program_id = program_id_of(raw.get("programId"))
        if not program_id:
            raise BridgeFailure("缺少项目标识")
        branch = str(raw.get("branch") or "").strip()
        message = str(raw.get("message") or "")
        provider = ai_provider_of(raw)
        commit_only = bool(raw.get("commitOnly"))
        # 子项目的改动也是这条需求的产物：不带上它们，推完远端仍然缺一半代码。
        targets = git_subproject_targets_of(self.workspace, raw.get("targets"), branch)
        # 子项目必须先落提交：submodule 的新 commit 会表现为主项目里的 gitlink 改动，
        # 主项目最后提交才能把这个指针一并带上。单个子项目失败仍继续其它项目和主项目。
        child_records = self._push_subproject_branches(branch, message, targets, push=not commit_only)
        if commit_only:
            # 仅提交是本机动作，失败原因基本是工作区自身的问题，不值得再起一轮 AI 去修。
            result = git_push_branch(self.workspace, branch, message, push=False)
            result["repaired"] = False
            result["results"] = self._push_branch_results(result, branch, child_records)
            return result
        try:
            result = git_push_branch(self.workspace, branch, message)
            result["repaired"] = False
            result["results"] = self._push_branch_results(result, branch, child_records)
            return result
        except BridgeFailure as exc:
            failure = str(exc)
        config = request_scoped_config(config, "", program_id)
        summary, status = self._repair_git_push(
            config,
            program_id,
            branch,
            git_commit_message_of(message, branch),
            failure,
            provider,
            str(raw.get("model") or "").strip(),
            reasoning_effort_of(raw, provider),
            fast_mode_of(raw, provider),
        )
        remote = git_default_remote(self.workspace)
        # 以仓库的真实状态判定成功与否，不采信 AI 的结论。
        if not git_branch_synced(self.workspace, branch, remote):
            raise BridgeFailure(f"推送失败，{provider_label(provider)} 也没能解决：{failure}\n\n处理说明：{summary or '无'}")
        repaired = {
            "pushed": True,
            "branch": branch,
            "remote": remote,
            "committed": True,
            "commitMessage": "",
            "upToDate": False,
            "synced": "repaired",
            "repaired": True,
            "repairStatus": status,
            "repairSummary": summary,
            "output": failure,
        }
        # 子项目已经在主项目之前处理完成；AI 只修主项目，不能再把子项目重复推一轮。
        repaired["results"] = self._push_branch_results(repaired, branch, child_records)
        return repaired

    def _push_subproject_branches(
        self,
        branch: str,
        message: str,
        targets: list[str],
        push: bool = True,
    ) -> list[dict[str, Any]]:
        """按选择顺序先提交、推送各子项目，并返回各自结果。

        子项目失败只记录原因，不打断其它子项目：一个工程推不动不该让别的也停在本机。
        """
        records: list[dict[str, Any]] = []
        for relative in targets:
            record: dict[str, Any] = {
                "path": relative,
                "name": relative,
                "branch": branch,
                "pushed": False,
                "committed": False,
                "upToDate": False,
                "skipped": False,
                "error": "",
            }
            try:
                child = git_subproject_workspace_of(self.workspace, relative)
                if child == self.workspace.resolve():
                    continue
                # 子项目本机没有这条需求分支时，提交推送它自己当前所处的分支：多工程工作目录里
                # 每个工程有自己的分支节奏，不该因为分支名对不上就把这一轮的改动留在本机。
                child_branch = branch if git_branch_exists(child, branch) else git_current_branch(child)
                record["branch"] = child_branch
                # 游离 HEAD 没有可推送的分支，跳过并如实标出来，不替用户猜该推到哪。
                if not child_branch:
                    record["skipped"] = True
                else:
                    child_result = git_push_branch(child, child_branch, message, push=push)
                    record["pushed"] = bool(child_result.get("pushed"))
                    record["committed"] = bool(child_result.get("committed"))
                    record["upToDate"] = bool(child_result.get("upToDate"))
            except BridgeFailure as exc:
                record["error"] = str(exc)
            records.append(record)
        return records

    def _push_branch_results(
        self,
        root: dict[str, Any],
        branch: str,
        children: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """结果展示仍保持主项目在第一行，但真实执行顺序是 children → root。"""
        return [{
            "path": "",
            "name": self.workspace.name,
            "branch": str(root.get("branch") or branch),
            "pushed": bool(root.get("pushed")),
            "committed": bool(root.get("committed")),
            "upToDate": bool(root.get("upToDate")),
            "skipped": False,
            "error": "",
        }, *children]

    def _repair_git_push(
        self,
        config: dict[str, Any],
        program_id: int,
        branch: str,
        commit_message: str,
        failure: str,
        provider: str,
        model: str,
        reasoning_effort: str,
        fast_mode: bool,
    ) -> tuple[str, str]:
        """起一轮 AI 会话专门修推送。超时就掐掉进程，不让 HTTP 请求无限期挂着。"""
        remote = git_default_remote(self.workspace)
        client = create_ai_client(provider, self.workspace, None, codex_environment(config, program_id))
        try:
            thread_id, turn_id = client.start_task(
                f"推送需求分支 {branch}",
                build_git_push_repair_prompt(self.workspace, branch, remote, failure, commit_message),
                None,
                model,
                reasoning_effort=reasoning_effort,
                fast_mode=fast_mode,
            )
            outcome: dict[str, str] = {}

            def wait() -> None:
                outcome["status"] = client.wait_turn(turn_id)

            waiter = threading.Thread(target=wait, daemon=True)
            waiter.start()
            waiter.join(GIT_PUSH_REPAIR_TIMEOUT_SECONDS)
            if waiter.is_alive():
                return "", "timeout"
            status = outcome.get("status") or "failed"
            turn = client.read_turn(thread_id, turn_id, client.next_request_id())
            return final_agent_text_from_output(execution_output(status, turn)), status
        finally:
            client.close()

    def execute(self, raw: Any, batch_claim: bool = False, config: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = validate_execute_payload(raw)
        provider = payload["provider"]
        label = provider_label(provider)
        biz_line = ""
        program_id = payload["programId"]
        requested_task = payload["task"]
        config = request_scoped_config(config, biz_line, program_id)
        biz_line = config_biz_line(config)
        context = planner.project_context(config, program_id)
        payload["conversationMentionContext"] = self._conversation_mention_context(
            config,
            program_id,
            payload.get("conversationReferences") or [],
            context,
        )
        task = next((item for item in context["items"] if item.get("itemKey") == requested_task["itemKey"]), None)
        if task is None:
            raise BridgeFailure("任务不存在")
        if int(task.get("version") or 0) != int(requested_task["version"]):
            raise BridgeFailure("任务版本已变化，请刷新任务面板")
        phase = str(task.get("phase") or "requirement")
        execution_batch_id = str(payload.get("executionBatchId") or "").strip()
        if task.get("status") == "done":
            raise BridgeFailure("已完成任务不能再次执行")
        by_key = {str(item.get("itemKey")): item for item in context["items"]}
        queue_id = str(payload.get("batchId") or payload.get("sequenceId") or "")
        queue_satisfied: set[str] = set()
        if queue_id:
            with self.lock:
                queue_items = self.batch_tasks if payload.get("batchId") else self.sequence_tasks
                queue_identity = task_identity(biz_line, program_id, str(task.get("itemKey") or ""))
                if queue_identity in queue_items:
                    queue_satisfied = set(
                        (self.batch_satisfied if payload.get("batchId") else self.sequence_satisfied).get(queue_id, set())
                    )
        incomplete = [
            key for key in task.get("dependsOnItemKeys") or []
            if by_key.get(str(key), {}).get("status") != "done" and str(key) not in queue_satisfied
        ]
        if incomplete:
            raise BridgeFailure("前置任务尚未全部完成：" + ", ".join(incomplete))
        # 列表刻意不带大文本；实际启动前单独取详情，将完整需求给 Codex。
        detail = planner.request_api(
            config,
            "GET",
            "/delivery/item",
            query={"programId": program_id, "itemKey": str(task["itemKey"])},
        )
        if isinstance(detail, dict) and detail.get("itemKey"):
            task = detail
        payload["task"] = task
        payload["gitBranch"] = self._ensure_requirement_git_branch(config, program_id, task)
        item_key = str(task["itemKey"])
        identity = task_identity(biz_line, program_id, item_key)
        with self.lock:
            if identity in self.active:
                raise BridgeFailure("该任务已经在本地执行中")
            if identity in self.batch_tasks and not batch_claim:
                raise BridgeFailure("该任务正在等待批量启动")
            if batch_claim:
                self.batch_tasks.discard(identity)
            self.active.add(identity)

        self.progress.publish(identity, "status", "正在领取任务", task["title"])
        client = create_ai_client(
            provider,
            self.workspace,
            lambda message: self._publish_app_server_event(identity, message),
            codex_environment(config, program_id),
        )
        try:
            updated_task = self._claim_task(config, program_id, task, f"{label} 已领取任务，正在创建本地执行会话。", provider)
            self._update_execution_batch_item(
                config,
                program_id,
                execution_batch_id,
                item_key,
                "running",
                provider=provider,
            )
        except Exception:
            client.close()
            with self.lock:
                self.active.discard(identity)
            raise
        payload["task"] = updated_task
        self._migrate_legacy_task_outline(updated_task)
        # 同需求的兄弟任务已经写好的文档：只挂清单，让执行器按相关性自己去读。
        payload["requirementDocuments"] = requirement_document_catalog(
            context.get("items") or [],
            updated_task,
            self.workspace,
        )
        binding: dict[str, Any] | None = None
        try:
            previous_binding = self._session_binding(config, program_id, item_key, phase, provider)
            title = conversation_title(task, previous_binding)
            # 留一份提示词原文：这一轮如果没能发出工具调用，要用同样的输入重试一次。
            task_prompt = build_task_prompt(payload, self.workspace)
            thread_id, turn_id = client.start_task(
                title,
                task_prompt,
                payload.get("followUpAttachments") if isinstance(payload.get("followUpAttachments"), list) else None,
                str(payload.get("model") or ""),
                reasoning_effort=str(payload.get("reasoningEffort") or ""),
                fast_mode=bool(payload.get("fastMode")),
            )
            metadata = conversation_metadata(
                previous_binding,
                thread_id,
                turn_id,
                "running",
                title,
                phase,
            )
            metadata.update({"workspace": self.workspace.name, "source": "task-board-http"})
            binding = planner.request_api(
                config,
                "POST",
                "/delivery/item/execution-session/bind",
                body={
                    "programId": program_id,
                    "itemKey": item_key,
                    "executorType": provider,
                    "phase": phase,
                    "progress": 0,
                    "externalSessionId": thread_id,
                    "status": "running",
                    "metadata": metadata,
                    "actorName": f"{provider}-http-bridge",
                },
            )
            with self.lock:
                self.active_runs[identity] = {
                    "client": client,
                    "threadId": thread_id,
                    "turnId": turn_id,
                    "task": updated_task,
                    "binding": binding,
                    "config": config,
                    "provider": provider,
                }
        except Exception:
            client.close()
            self._release_failed_claim(config, program_id, updated_task, provider)
            if binding is not None:
                try:
                    planner.request_api(
                        config,
                        "POST",
                        "/delivery/item/execution-session/status",
                        body={
                            "programId": program_id,
                            "itemKey": item_key,
                            "executorType": provider,
                            "phase": phase,
                            "progress": 0,
                            "version": int(binding["version"]),
                            "status": "blocked",
                            "metadata": {
                                **conversation_metadata(binding, thread_id, turn_id, "blocked"),
                                "startupFailed": True,
                                "workspace": self.workspace.name,
                            },
                            "actorName": f"{provider}-http-bridge",
                        },
                    )
                except Exception as cleanup_error:
                    print(f"清理启动失败的执行会话失败：{program_id}/{item_key}: {cleanup_error}", file=sys.stderr, flush=True)
            with self.lock:
                self.active.discard(identity)
                self._release_active_run(identity)
            raise

        threading.Thread(
            target=self._follow,
            args=(
                identity, client, config, program_id, item_key, updated_task, binding, turn_id,
                text_without_attachment_context(str(payload.get("followUp") or "")),
                str(payload.get("model") or ""), str(payload.get("reasoningEffort") or ""), bool(payload.get("fastMode")),
                task_prompt,
            ),
            daemon=True,
        ).start()
        return {
            "accepted": True,
            "bizLine": biz_line,
            "programId": program_id,
            "itemKey": item_key,
            "threadId": thread_id,
        }

    def execute_sequence(self, raw: Any, config: dict[str, Any] | None = None) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise BridgeFailure("请求体必须是 JSON 对象")
        biz_line = biz_line_of(raw)
        program_id = program_id_of(raw.get("programId"))
        requested_keys = [str(key).strip() for key in raw.get("itemKeys") or [] if str(key).strip()]
        start_item_key = str(raw.get("startItemKey") or "").strip()
        model = str(raw.get("model") or "").strip()
        execution_constraints = str(raw.get("executionConstraints") or "").strip()
        if len(execution_constraints) > 32 * 1024:
            raise BridgeFailure("任务约束条件说明不能超过 32KB")
        provider = ai_provider_of(raw)
        reasoning_effort = reasoning_effort_of(raw, provider)
        fast_mode = fast_mode_of(raw, provider)
        if not program_id:
            raise BridgeFailure("缺少项目标识")
        config = request_scoped_config(config, biz_line, program_id)
        biz_line = config_biz_line(config)
        context = planner.project_context(config, program_id)
        items = [item for item in context.get("items") or [] if isinstance(item, dict)]
        by_key = {str(item.get("itemKey") or ""): item for item in items}
        if start_item_key:
            if start_item_key not in by_key:
                raise BridgeFailure("起始任务不存在")
            selected = {start_item_key}
            changed = True
            while changed:
                changed = False
                for item in items:
                    key = str(item.get("itemKey") or "")
                    dependencies = {str(value) for value in item.get("dependsOnItemKeys") or []}
                    if key not in selected and dependencies & selected:
                        selected.add(key)
                        changed = True
        else:
            selected = set(requested_keys)
        if not selected:
            raise BridgeFailure("请至少选择一个任务")
        missing = sorted(selected - set(by_key))
        if missing:
            raise BridgeFailure("任务不存在：" + ", ".join(missing))
        pending = {
            key for key in selected
            if str(by_key[key].get("status") or "") != "done"
        }
        if not pending:
            raise BridgeFailure("所选任务中没有可串行执行的未完成任务")
        if not start_item_key:
            completed = sorted(selected - pending)
            if completed:
                raise BridgeFailure("串行执行不能选择已完成任务：" + ", ".join(completed))
        ordered: list[str] = []
        remaining = set(pending)
        while remaining:
            ready = sorted(
                key for key in remaining
                if all(
                    str(dep) not in remaining
                    for dep in by_key[key].get("dependsOnItemKeys") or []
                )
            )
            if not ready:
                raise BridgeFailure("任务依赖关系存在环，无法串行执行")
            ordered.extend(ready)
            remaining.difference_update(ready)
        for key in ordered:
            incomplete_external = [
                str(dep) for dep in by_key[key].get("dependsOnItemKeys") or []
                if str(dep) not in pending and by_key.get(str(dep), {}).get("status") != "done"
            ]
            if incomplete_external:
                raise BridgeFailure(f"任务 {key} 的前置任务尚未完成：" + ", ".join(incomplete_external))
        with self.lock:
            reserved = {task_identity(biz_line, program_id, key) for key in ordered}
            sequence_conflicts = sorted(key for _, _, key in reserved if task_identity(biz_line, program_id, key) in self.sequence_tasks)
            batch_conflicts = sorted(key for _, _, key in reserved if task_identity(biz_line, program_id, key) in self.batch_tasks)
            active_conflicts = sorted(key for _, _, key in reserved if task_identity(biz_line, program_id, key) in self.active)
            if sequence_conflicts:
                raise BridgeFailure("任务已经在其他串行队列中：" + ", ".join(sequence_conflicts))
            if batch_conflicts:
                raise BridgeFailure("任务正在等待批量启动：" + ", ".join(batch_conflicts))
            if active_conflicts:
                raise BridgeFailure("任务已经在本地执行中：" + ", ".join(active_conflicts))
            self.sequence_tasks.update(reserved)
        try:
            persisted_batch = self._create_execution_batch(config, program_id, ordered, "sequence", provider)
            sequence_id = str(persisted_batch["batchId"])
        except Exception:
            with self.lock:
                self.sequence_tasks.difference_update(reserved)
            raise
        with self.lock:
            self.active_sequences.add(sequence_id)
            self.sequence_satisfied[sequence_id] = set()
        threading.Thread(
            target=self._run_sequence,
            args=(sequence_id, config, program_id, ordered, model, provider, execution_constraints, reasoning_effort, fast_mode),
            daemon=True,
        ).start()
        return {
            "accepted": True,
            "sequenceId": sequence_id,
            "batchId": sequence_id,
            "bizLine": biz_line,
            "programId": program_id,
            "itemKeys": ordered,
            "model": model,
            "provider": provider,
        }

    def _run_sequence(
        self,
        sequence_id: str,
        config: dict[str, Any],
        program_id: int,
        item_keys: list[str],
        model: str,
        provider: str,
        execution_constraints: str = "",
        reasoning_effort: str = "",
        fast_mode: bool = False,
    ) -> None:
        biz_line = config_biz_line(config)
        terminal_status = "completed"
        terminal_summary = "批次内全部任务已完成。"
        attempted_item = ""
        with self.lock:
            self.sequence_satisfied.setdefault(sequence_id, set())
        self._register_queue(sequence_id, program_id)
        try:
            for item_key in item_keys:
                self._abort_if_cancelled(sequence_id)
                attempted_item = item_key
                task = self._task_detail(config, program_id, item_key)
                status = str(task.get("status") or "")
                if status == "done":
                    self._update_execution_batch_item(
                        config, program_id, sequence_id, item_key, "completed", "执行开始前任务已完成。", provider,
                    )
                    attempted_item = ""
                    continue
                self.execute(
                    {
                        "bizLine": biz_line,
                        "programId": program_id,
                        "task": task,
                        "model": model,
                        "provider": provider,
                        "sequenceId": sequence_id,
                        "executionBatchId": sequence_id,
                        "batchMode": True,
                        **({"executionConstraints": execution_constraints} if execution_constraints else {}),
                        **({"reasoningEffort": reasoning_effort} if reasoning_effort else {}),
                        **({"fastMode": True} if fast_mode else {}),
                    },
                    config=config,
                )
                identity = task_identity(biz_line, program_id, item_key)
                while True:
                    with self.lock:
                        still_active = identity in self.active
                    if not still_active:
                        break
                    time.sleep(0.2)
                completed_task = self._task_detail(config, program_id, item_key)
                outcome, reason = batch_task_outcome(completed_task)
                if outcome == "ignorable":
                    self._update_execution_batch_item(config, program_id, sequence_id, item_key, "blocked", reason, provider)
                    terminal_status = "blocked"
                    terminal_summary = f"任务 {item_key} 未完全完成：{reason}"
                    with self.lock:
                        self.sequence_satisfied.setdefault(sequence_id, set()).add(item_key)
                    self.progress.publish(
                        identity,
                        "status",
                        "任务中断已忽略，继续串行队列",
                        reason,
                        "success",
                    )
                    attempted_item = ""
                    continue
                if outcome != "completed":
                    self._update_execution_batch_item(config, program_id, sequence_id, item_key, "blocked", reason, provider)
                    attempted_item = ""
                    self.progress.publish(identity, "error", "串行队列已暂停", reason, "failed")
                    raise BridgeFailure(
                        f"任务 {item_key} 未成功完成，队列已停止：{reason}"
                    )
                with self.lock:
                    self.sequence_satisfied.setdefault(sequence_id, set()).add(item_key)
                self._update_execution_batch_item(config, program_id, sequence_id, item_key, "completed", reason, provider)
                attempted_item = ""
        except Exception as exc:
            terminal_status = "blocked"
            terminal_summary = str(exc)
            if attempted_item:
                try:
                    self._update_execution_batch_item(
                        config, program_id, sequence_id, attempted_item, "blocked", terminal_summary, provider,
                    )
                except Exception as sync_error:
                    print(f"同步串行批次任务失败 {program_id}/{sequence_id}/{attempted_item}: {sync_error}", file=sys.stderr, flush=True)
            print(f"串行执行失败 {program_id}/{sequence_id}: {exc}", file=sys.stderr, flush=True)
        finally:
            try:
                self._finalize_execution_batch(
                    config, program_id, sequence_id, terminal_status, terminal_summary, provider,
                )
            except Exception as sync_error:
                print(f"同步串行执行批次结果失败 {program_id}/{sequence_id}: {sync_error}", file=sys.stderr, flush=True)
            with self.lock:
                self.active_sequences.discard(sequence_id)
                self.sequence_tasks.difference_update(task_identity(biz_line, program_id, key) for key in item_keys)
                self.sequence_satisfied.pop(sequence_id, None)
            self._release_queue(sequence_id)

    def execute_batch(self, raw: Any, config: dict[str, Any] | None = None) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise BridgeFailure("请求体必须是 JSON 对象")
        biz_line = biz_line_of(raw)
        program_id = program_id_of(raw.get("programId"))
        requested_keys = [str(key).strip() for key in raw.get("itemKeys") or [] if str(key).strip()]
        model = str(raw.get("model") or "").strip()
        execution_constraints = str(raw.get("executionConstraints") or "").strip()
        if len(execution_constraints) > 32 * 1024:
            raise BridgeFailure("任务约束条件说明不能超过 32KB")
        provider = ai_provider_of(raw)
        reasoning_effort = reasoning_effort_of(raw, provider)
        fast_mode = fast_mode_of(raw, provider)
        if not program_id:
            raise BridgeFailure("缺少项目标识")
        if not requested_keys:
            raise BridgeFailure("请至少选择一个未完成任务")
        if len(set(requested_keys)) != len(requested_keys):
            raise BridgeFailure("批量任务不能重复选择")

        config = request_scoped_config(config, biz_line, program_id)
        biz_line = config_biz_line(config)
        context = planner.project_context(config, program_id)
        items = [item for item in context.get("items") or [] if isinstance(item, dict)]
        by_key = {str(item.get("itemKey") or ""): item for item in items}
        missing = sorted(set(requested_keys) - set(by_key))
        if missing:
            raise BridgeFailure("任务不存在：" + ", ".join(missing))
        completed = sorted(key for key in requested_keys if str(by_key[key].get("status") or "") == "done")
        if completed:
            raise BridgeFailure("批量启动不能选择已完成任务：" + ", ".join(completed))
        selected = set(requested_keys)
        incomplete_external = {
            key: [
                str(dep) for dep in by_key[key].get("dependsOnItemKeys") or []
                if str(dep) not in selected and by_key.get(str(dep), {}).get("status") != "done"
            ]
            for key in requested_keys
        }
        blocked = [f"{key}（{', '.join(dependencies)}）" for key, dependencies in incomplete_external.items() if dependencies]
        if blocked:
            raise BridgeFailure("批量任务存在未完成的外部前置任务：" + "、".join(blocked))
        remaining = set(requested_keys)
        while remaining:
            ready = {
                key for key in remaining
                if all(str(dep) not in remaining for dep in by_key[key].get("dependsOnItemKeys") or [])
            }
            if not ready:
                raise BridgeFailure("任务依赖关系存在环，无法批量执行")
            remaining.difference_update(ready)

        with self.lock:
            reserved = {task_identity(biz_line, program_id, key) for key in requested_keys}
            active = sorted(key for _, _, key in reserved if task_identity(biz_line, program_id, key) in self.active)
            queued = sorted(key for _, _, key in reserved if task_identity(biz_line, program_id, key) in self.sequence_tasks)
            waiting = sorted(key for _, _, key in reserved if task_identity(biz_line, program_id, key) in self.batch_tasks)
            if active:
                raise BridgeFailure("任务已经在本地执行中：" + ", ".join(active))
            if queued:
                raise BridgeFailure("任务已经在串行队列中：" + ", ".join(queued))
            if waiting:
                raise BridgeFailure("任务正在等待批量启动：" + ", ".join(waiting))
            self.batch_tasks.update(reserved)
        try:
            persisted_batch = self._create_execution_batch(config, program_id, requested_keys, "parallel", provider)
            batch_id = str(persisted_batch["batchId"])
        except Exception:
            with self.lock:
                self.batch_tasks.difference_update(reserved)
            raise
        with self.lock:
            self.batch_satisfied[batch_id] = set()
        threading.Thread(
            target=self._run_batch,
            args=(batch_id, config, program_id, requested_keys, model, provider, execution_constraints, reasoning_effort, fast_mode),
            daemon=True,
        ).start()
        return {
            "accepted": True,
            "batchId": batch_id,
            "bizLine": biz_line,
            "programId": program_id,
            "itemKeys": requested_keys,
            "model": model,
            "provider": provider,
        }

    def _run_batch(
        self,
        batch_id: str,
        config: dict[str, Any],
        program_id: int,
        item_keys: list[str],
        model: str,
        provider: str = "codex",
        execution_constraints: str = "",
        reasoning_effort: str = "",
        fast_mode: bool = False,
    ) -> None:
        biz_line = config_biz_line(config)
        terminal_status = "completed"
        terminal_summary = "批次内全部任务已完成。"
        attempted_item = ""
        with self.lock:
            self.batch_satisfied.setdefault(batch_id, set())
        self._register_queue(batch_id, program_id)
        try:
            remaining = set(item_keys)
            while remaining:
                self._abort_if_cancelled(batch_id)
                context = planner.project_context(config, program_id)
                items = [item for item in context.get("items") or [] if isinstance(item, dict)]
                by_key = {str(item.get("itemKey") or ""): item for item in items}
                missing = sorted(remaining - set(by_key))
                if missing:
                    raise BridgeFailure("任务不存在：" + ", ".join(missing))

                completed_before_start = {
                    key for key in remaining if str(by_key[key].get("status") or "") == "done"
                }
                for item_key in completed_before_start:
                    self._update_execution_batch_item(
                        config, program_id, batch_id, item_key, "completed", "执行开始前任务已完成。", provider,
                    )
                    with self.lock:
                        self.batch_satisfied.setdefault(batch_id, set()).add(item_key)
                remaining.difference_update(completed_before_start)
                if not remaining:
                    return

                with self.lock:
                    satisfied = set(self.batch_satisfied.get(batch_id, set()))
                ready = sorted(
                    key for key in remaining
                    if all(
                        by_key.get(str(dep), {}).get("status") == "done"
                        or str(dep) in satisfied
                        for dep in by_key[key].get("dependsOnItemKeys") or []
                    )
                )
                if not ready:
                    waiting = []
                    for key in sorted(remaining):
                        dependencies = [
                            str(dep) for dep in by_key[key].get("dependsOnItemKeys") or []
                            if by_key.get(str(dep), {}).get("status") != "done"
                        ]
                        waiting.append(f"{key}（{', '.join(dependencies) or '状态未刷新'}）")
                    raise BridgeFailure("批量队列没有可执行任务，仍在等待前置任务：" + "、".join(waiting))

                for item_key in ready:
                    self._abort_if_cancelled(batch_id)
                    attempted_item = item_key
                    task = self._task_detail(config, program_id, item_key)
                    self.execute(
                        {
                            "bizLine": biz_line,
                            "programId": program_id,
                            "task": task,
                            "model": model,
                            "provider": provider,
                            "batchId": batch_id,
                            "executionBatchId": batch_id,
                            "batchMode": True,
                            **({"executionConstraints": execution_constraints} if execution_constraints else {}),
                            **({"reasoningEffort": reasoning_effort} if reasoning_effort else {}),
                            **({"fastMode": True} if fast_mode else {}),
                        },
                        batch_claim=True,
                        config=config,
                    )
                    attempted_item = ""

                launched_identities = {task_identity(biz_line, program_id, item_key) for item_key in ready}
                while True:
                    with self.lock:
                        still_active = launched_identities & self.active
                    if not still_active:
                        break
                    time.sleep(0.2)

                failed: list[str] = []
                for item_key in ready:
                    reviewed_task = self._task_detail(config, program_id, item_key)
                    outcome, reason = batch_task_outcome(reviewed_task)
                    identity = task_identity(biz_line, program_id, item_key)
                    if outcome == "completed":
                        self._update_execution_batch_item(config, program_id, batch_id, item_key, "completed", reason, provider)
                        with self.lock:
                            self.batch_satisfied.setdefault(batch_id, set()).add(item_key)
                        continue
                    if outcome == "ignorable":
                        self._update_execution_batch_item(config, program_id, batch_id, item_key, "blocked", reason, provider)
                        terminal_status = "blocked"
                        terminal_summary = f"任务 {item_key} 未完全完成：{reason}"
                        with self.lock:
                            self.batch_satisfied.setdefault(batch_id, set()).add(item_key)
                        self.progress.publish(
                            identity,
                            "status",
                            "任务中断已忽略，继续批量队列",
                            reason,
                            "success",
                        )
                        continue
                    self._update_execution_batch_item(config, program_id, batch_id, item_key, "blocked", reason, provider)
                    failed.append(f"{item_key}（{reason}）")
                    self.progress.publish(identity, "error", "批量队列已暂停", reason, "failed")
                if failed:
                    raise BridgeFailure("批量队列已停止，当前并行任务存在需要处理的问题：" + "、".join(failed))
                remaining.difference_update(ready)
        except Exception as exc:
            terminal_status = "blocked"
            terminal_summary = str(exc)
            if attempted_item:
                try:
                    self._update_execution_batch_item(
                        config, program_id, batch_id, attempted_item, "blocked", terminal_summary, provider,
                    )
                except Exception as sync_error:
                    print(f"同步批次任务失败 {program_id}/{batch_id}/{attempted_item}: {sync_error}", file=sys.stderr, flush=True)
            print(f"批量执行失败 {program_id}/{batch_id}: {exc}", file=sys.stderr, flush=True)
        finally:
            try:
                self._finalize_execution_batch(
                    config, program_id, batch_id, terminal_status, terminal_summary, provider,
                )
            except Exception as sync_error:
                print(f"同步批量执行结果失败 {program_id}/{batch_id}: {sync_error}", file=sys.stderr, flush=True)
            with self.lock:
                self.batch_tasks.difference_update(task_identity(biz_line, program_id, key) for key in item_keys)
                self.batch_satisfied.pop(batch_id, None)
            self._release_queue(batch_id)

    def conversation(
        self,
        program_id: int,
        item_key: str,
        selected_thread_id: str = "",
        biz_line: str = DEFAULT_BIZ_LINE,
        config: dict[str, Any] | None = None,
        provider: str = "codex",
    ) -> dict[str, Any]:
        provider = ai_provider_of(provider)
        config = request_scoped_config(config, biz_line, program_id)
        biz_line = config_biz_line(config)
        identity = task_identity(biz_line, program_id, item_key)
        task = self._task_detail(config, program_id, item_key)
        current_binding = self._session_binding(config, program_id, item_key, str(task.get("phase") or "requirement"), provider)
        bindings = self._task_session_bindings(config, program_id, item_key, provider)
        catalog, binding_by_thread = merged_conversation_catalog(bindings)
        current_thread_id = str((current_binding or {}).get("externalSessionId") or "")
        known_thread_ids = {str(entry["threadId"]) for entry in catalog}
        if selected_thread_id and selected_thread_id not in known_thread_ids:
            raise BridgeFailure("所选 Codex 会话不存在")
        thread_id = selected_thread_id or current_thread_id or (catalog[0]["threadId"] if catalog else "")
        binding = binding_by_thread.get(thread_id, current_binding)
        # 线程正文在它自己那个执行器的会话缓存里：读和续都跟着线程走，不跟当前选中的工具走。
        provider = executor_provider_of(binding, provider)
        current_thread_id = str((binding or {}).get("externalSessionId") or "")
        if not thread_id:
            return {
                "bizLine": biz_line,
                "programId": program_id,
                "itemKey": item_key,
                "threadId": "",
                "executorType": provider,
                "turns": [],
                "conversations": catalog,
                "active": False,
                "taskHasActiveConversation": any(session.get("status") == "running" for session in bindings),
                "taskStatus": str(task.get("status") or "todo"),
                "taskPhase": str(task.get("phase") or "requirement"),
                "taskProgress": int(task.get("progress") or 0),
                "sessionPhase": str((current_binding or {}).get("phase") or task.get("phase") or "requirement"),
                "sessionProgress": int((current_binding or {}).get("progress") or 0),
            }
        with self.lock:
            active = self.active_runs.get(identity)
        task_has_active_conversation = active is not None or any(session.get("status") == "running" for session in bindings)
        active_for_thread = active if active is not None and str(active.get("threadId") or "") == thread_id else None
        if active_for_thread is None:
            metadata = (binding or {}).get("metadata") or {}
            turn_id = str(metadata.get("turnId") or "") if isinstance(metadata, dict) else ""
            if binding and binding.get("status") == "running" and current_thread_id == thread_id and turn_id:
                try:
                    active_for_thread = self._resume_active_turn(config, identity, task, binding, thread_id, turn_id, provider)
                except Exception as exc:
                    print(f"恢复 Codex 执行会话失败：{program_id}/{item_key}: {exc}", file=sys.stderr, flush=True)
        live_client = active_for_thread["client"] if active_for_thread is not None else None
        thread = self._read_thread_with_workspace_archive(
            live_client, thread_id, "task", item_key, config, program_id,
            provider=provider,
            environment=codex_environment(config, program_id),
        )
        self.attachments.recover_generated_images(config_biz_line(config), program_id, item_key, thread_id)
        turns = ensure_terminal_result(
            serialize_turns(
                thread.get("turns") or [],
                lambda attachment_ids: [
                    ConversationAttachmentStore._public(attachment)
                    for attachment in self.attachments.resolve(program_id, item_key, attachment_ids)
                ],
                lambda paths: self.artifacts.register(config_biz_line(config), program_id, item_key, paths),
                lambda turn_id: self.attachments.generated_for_turn(
                    program_id, item_key, thread_id, turn_id
                ),
            ),
            task,
            binding,
        )
        for entry in catalog:
            entry["active"] = bool(
                entry["threadId"] == str((active or {}).get("threadId") or "")
                or bool(
                    (binding_by_thread.get(str(entry.get("threadId") or "")) or {}).get("status") == "running"
                    and str((binding_by_thread.get(str(entry.get("threadId") or "")) or {}).get("externalSessionId") or "") == entry["threadId"]
                )
            )
        return {
            "bizLine": biz_line,
            "programId": program_id,
            "itemKey": item_key,
            "threadId": thread_id,
            "executorType": provider,
            "turns": turns,
            "conversations": catalog,
            "active": active_for_thread is not None,
            "taskHasActiveConversation": task_has_active_conversation,
            "activeTurnId": str((active_for_thread or {}).get("turnId") or ""),
            "taskStatus": str(task.get("status") or "todo"),
            "taskPhase": str(task.get("phase") or "requirement"),
            "taskProgress": int(task.get("progress") or 0),
            "sessionPhase": str((binding or {}).get("phase") or task.get("phase") or "requirement"),
            "sessionProgress": int((binding or {}).get("progress") or 0),
        }

    def upload_conversation_attachments(
        self,
        biz_line: str,
        program_id: int,
        item_key: str,
        uploads: list[dict[str, Any]],
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not program_id or not item_key:
            raise BridgeFailure("缺少项目或任务标识")
        config = request_scoped_config(config, biz_line, program_id)
        biz_line = config_biz_line(config)
        return {"bizLine": biz_line, "attachments": self.attachments.save(biz_line, program_id, item_key, uploads)}

    def requirement_document(
        self,
        program_id: int,
        item_key: str,
        biz_line: str = DEFAULT_BIZ_LINE,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        config = request_scoped_config(config, biz_line, program_id)
        task = self._task_detail(config, program_id, item_key)
        raw_path = str(task.get("requirementDocumentPath") or "").strip()
        relative = Path(raw_path)
        if not raw_path or relative.is_absolute() or ".." in relative.parts:
            raise BridgeFailure("任务需求文档路径无效")
        path = (self.workspace / relative).resolve()
        try:
            normalized = path.relative_to(self.workspace)
        except ValueError as exc:
            raise BridgeFailure("任务需求文档路径超出当前项目") from exc
        if not path.exists():
            return {"path": normalized.as_posix(), "exists": False, "content": "", "size": 0, "modifiedAt": ""}
        if not path.is_file():
            raise BridgeFailure("任务需求文档路径不是文件")
        size = path.stat().st_size
        if size > MAX_REQUIREMENT_DOCUMENT_BYTES:
            raise BridgeFailure("需求文档超过 2 MB，无法预览")
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise BridgeFailure("需求文档不是 UTF-8 文本文件") from exc
        if "\x00" in content:
            raise BridgeFailure("需求文档不是可预览的文本文件")
        return {
            "path": normalized.as_posix(),
            "exists": True,
            "content": content,
            "size": size,
            "modifiedAt": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
        }

    def save_requirement_document(
        self,
        program_id: int,
        item_key: str,
        content: str,
        biz_line: str = DEFAULT_BIZ_LINE,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Overwrite the task's sole requirement document through the board editor."""
        if len(content.encode("utf-8")) > MAX_REQUIREMENT_DOCUMENT_BYTES:
            raise BridgeFailure("需求文档不能超过 2 MB")
        if "\x00" in content:
            raise BridgeFailure("需求文档不能包含空字符")
        config = request_scoped_config(config, biz_line, program_id)
        task = self._task_detail(config, program_id, item_key)
        raw_path = str(task.get("requirementDocumentPath") or "").strip()
        relative = Path(raw_path)
        if not raw_path or relative.is_absolute() or ".." in relative.parts:
            raise BridgeFailure("任务需求文档路径无效")
        path = (self.workspace / relative).resolve()
        try:
            normalized = path.relative_to(self.workspace)
        except ValueError as exc:
            raise BridgeFailure("任务需求文档路径超出当前项目") from exc
        text = content if content.endswith("\n") or not content.strip() else content + "\n"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        stat = path.stat()
        return {
            "path": normalized.as_posix(),
            "exists": True,
            "content": text,
            "size": stat.st_size,
            "modifiedAt": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
        }

    def _document_set_layout(
        self, config: dict[str, Any], program_id: int, scope: str, key: str,
    ) -> tuple[Path, str, bool]:
        """把栏目解析成 (目录, 默认主文档, 是否递归)，顺带校验这个键真属于当前项目。

        面板不能只凭一个字符串就去读工作区里的任意目录：需求键走需求详情校验，任务键走任务详情校验，
        与需求大纲、需求文档两个老接口保持同一条防线。
        """
        scope_value = str(scope or "").strip()
        key_value = str(key or "").strip()
        if not key_value:
            raise BridgeFailure("缺少文档栏目标识")
        if scope_value in {"requirement-outline", "requirement-testing"}:
            self._requirement_for_prototype(config, program_id, key_value)
            if scope_value == "requirement-outline":
                outline = requirement_outline_path_of(key_value)
                # 需求目录下还挂着 prototype/，大纲栏目只列顶层的文本文档。
                return outline.parent, outline.as_posix(), False
            testing = testing_asset_directory_of(key_value)
            return testing, (testing / TESTING_CASES_FILE_NAME).as_posix(), True
        if scope_value in {"task-document", "task-design", "task-testing"}:
            task = self._task_detail(config, program_id, key_value)
            document = Path(document_path_of(task))
            if document.is_absolute() or ".." in document.parts:
                raise BridgeFailure("任务需求文档路径无效")
            if scope_value == "task-document":
                return document.parent, document.as_posix(), False
            if scope_value == "task-design":
                return document.parent / "design", "", True
            testing = testing_asset_directory_of(key_value)
            return testing, (testing / TESTING_CASES_FILE_NAME).as_posix(), True
        raise BridgeFailure("未知的文档栏目")

    def document_set(
        self,
        program_id: int,
        scope: str,
        key: str,
        biz_line: str = DEFAULT_BIZ_LINE,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """List every document of one column so the board can offer them in a picker."""
        config = request_scoped_config(config, biz_line, program_id)
        directory, primary, recursive = self._document_set_layout(config, program_id, scope, key)
        files = document_set_entries(self.workspace, directory, recursive)
        paths = {entry["path"] for entry in files}
        # 主文档还没落盘时退回目录里的第一份可预览文档，面板打开就有东西可看；
        # 上传进来的 PDF、图片这类文件不做默认选中，它们打开的是附件预览而不是正文。
        previewable = [entry["path"] for entry in files if entry["previewable"]]
        selected = primary if primary in paths else (previewable[0] if previewable else "")
        return {
            "scope": str(scope or "").strip(),
            "key": str(key or "").strip(),
            "directory": directory.as_posix(),
            "primaryPath": selected,
            "files": files,
        }

    def upload_documents(
        self,
        program_id: int,
        scope: str,
        key: str,
        uploads: list[dict[str, Any]],
        biz_line: str = DEFAULT_BIZ_LINE,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Save files the board picked (local files, or pasted text turned into a file) into one column.

        面板本来只编辑会话产出的文档，需求目录还需要能放人手里的资料（PDF、Word、截图、粘过来的一段说明），
        所以这里允许任意后缀落盘，但文件名一律收敛过、重名顺延，绝不覆盖会话已经写好的文档。
        """
        if not uploads:
            raise BridgeFailure("没有要上传的文档")
        if len(uploads) > MAX_DOCUMENT_UPLOAD_FILES:
            raise BridgeFailure(f"一次最多上传 {MAX_DOCUMENT_UPLOAD_FILES} 份文档")
        config = request_scoped_config(config, biz_line, program_id)
        directory, _, _ = self._document_set_layout(config, program_id, scope, key)
        prepared: list[tuple[str, bytes]] = []
        for upload in uploads:
            name = document_upload_name(str(upload.get("name") or ""))
            data = upload.get("data")
            if not isinstance(data, bytes) or not data:
                raise BridgeFailure(f"文档 {name} 为空")
            if len(data) > MAX_DOCUMENT_UPLOAD_FILE_BYTES:
                raise BridgeFailure(f"文档 {name} 超过 20 MB")
            prepared.append((name, data))
        target_directory = (self.workspace / directory).resolve()
        try:
            target_directory.relative_to(self.workspace)
        except ValueError as exc:
            raise BridgeFailure("文档目录超出当前项目") from exc
        target_directory.mkdir(parents=True, exist_ok=True)
        uploaded: list[str] = []
        for name, data in prepared:
            target = available_document_name(target_directory, name)
            target.write_bytes(data)
            uploaded.append(target.resolve().relative_to(self.workspace).as_posix())
        result = self.document_set(program_id, scope, key, config=config)
        # 上传完就停在刚放进去的第一份上，用户不用自己再去列表里找。
        result["primaryPath"] = uploaded[0] if uploaded else result["primaryPath"]
        result["uploaded"] = uploaded
        return result

    def document_attachment(
        self,
        program_id: int,
        scope: str,
        key: str,
        path: str,
        biz_line: str = DEFAULT_BIZ_LINE,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Register one non-text document of a column as an artifact so the board can preview or download it."""
        config = request_scoped_config(config, biz_line, program_id)
        directory, _, _ = self._document_set_layout(config, program_id, scope, key)
        target = document_in_set(self.workspace, directory, path, previewable_only=False)
        if not target.is_file():
            raise BridgeFailure("文档不存在")
        relative = target.relative_to(self.workspace).as_posix()
        registered = self.artifacts.register(
            config_biz_line(config), program_id, document_attachment_item_key(scope, key), [relative],
        )
        if not registered:
            raise BridgeFailure("该文档无法预览或下载")
        return registered[0]

    def document_file(
        self,
        program_id: int,
        scope: str,
        key: str,
        path: str,
        biz_line: str = DEFAULT_BIZ_LINE,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Read one document the picker selected."""
        config = request_scoped_config(config, biz_line, program_id)
        directory, _, _ = self._document_set_layout(config, program_id, scope, key)
        return document_payload(
            self.workspace, document_in_set(self.workspace, directory, path), asset_boundary=directory,
        )

    def save_document_file(
        self,
        program_id: int,
        scope: str,
        key: str,
        path: str,
        content: str,
        biz_line: str = DEFAULT_BIZ_LINE,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Overwrite one existing document of a column from the board editor.

        面板只编辑已有文档：新增文档一律由会话产出，避免在面板上凭空造出执行器不认识的文件。
        """
        if not isinstance(content, str):
            raise BridgeFailure("文档正文必须是字符串")
        if len(content.encode("utf-8")) > MAX_DOCUMENT_SET_FILE_BYTES:
            raise BridgeFailure("文档不能超过 2 MB")
        if "\x00" in content:
            raise BridgeFailure("文档不能包含空字符")
        config = request_scoped_config(config, biz_line, program_id)
        directory, _, _ = self._document_set_layout(config, program_id, scope, key)
        target = document_in_set(self.workspace, directory, path)
        if not target.is_file():
            raise BridgeFailure("文档不存在，请先由会话生成后再编辑")
        text = content if content.endswith("\n") or not content.strip() else content + "\n"
        target.write_text(text, encoding="utf-8")
        return document_payload(self.workspace, target, asset_boundary=directory)

    @staticmethod
    def _requirement_prototype_identity(program_id: int, requirement_key: str) -> tuple[str, int, str]:
        return task_identity("", program_id, requirement_prototype_item_key(requirement_key))

    def _requirement_for_prototype(self, config: dict[str, Any], program_id: int, requirement_key: str) -> dict[str, Any]:
        requirement = planner.request_api(
            config,
            "GET",
            "/delivery/requirement",
            query={"programId": program_id, "requirementKey": requirement_key},
        )
        if not isinstance(requirement, dict) or str(requirement.get("requirementKey") or "") != requirement_key:
            raise BridgeFailure("需求不存在或无法读取")
        return requirement

    def _prototype_session_rows(
        self, config: dict[str, Any], program_id: int, requirement_key: str, provider: str,
    ) -> list[dict[str, Any]]:
        # 只比用途后缀，不比工具：换成另一个工具之后，之前那批原型会话也要留在列表里。
        rows = planner.request_api(
            config,
            "GET",
            "/delivery/requirement/planning-sessions",
            query={"programId": program_id, "requirementKey": requirement_key},
        )
        return [
            row for row in (rows or [])
            if isinstance(row, dict) and str(row.get("threadId") or "")
            and same_executor_purpose(row, requirement_prototype_executor_type(provider))
        ]

    def _save_prototype_session(
        self,
        config: dict[str, Any],
        program_id: int,
        requirement_key: str,
        provider: str,
        thread_id: str,
        turn_id: str,
        title: str,
        status: str,
        detail_digest: str = "",
    ) -> None:
        planner.request_api(
            config,
            "POST",
            "/delivery/requirement/planning-session/bind",
            body={
                "programId": program_id,
                "requirementKey": requirement_key,
                "executorType": requirement_prototype_executor_type(provider),
                "threadId": thread_id,
                "title": title[:120],
                "status": status,
                "metadata": {
                    "turnId": turn_id,
                    "kind": "requirement-prototype",
                    "detailDigest": detail_digest,
                    "workspace": self.workspace.name,
                },
                "actorName": f"{provider}-http-bridge",
            },
        )

    def requirement_outline(
        self,
        program_id: int,
        requirement_key: str,
        biz_line: str = DEFAULT_BIZ_LINE,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Read the breakdown outline the planning session keeps for one requirement."""
        config = request_scoped_config(config, biz_line, program_id)
        # 走一遍需求校验：面板不能靠猜一个需求键就读到工作区里的任意文档。
        self._requirement_for_prototype(config, program_id, requirement_key)
        document = requirement_outline_document(self.workspace, requirement_key)
        identity = self._planning_identity(program_id, requirement_key)
        with self.lock:
            active = self.active_runs.get(identity)
        return {"programId": program_id, "requirementKey": requirement_key, **document, "active": active is not None}

    def save_requirement_outline(
        self,
        program_id: int,
        requirement_key: str,
        markdown: str,
        biz_line: str = DEFAULT_BIZ_LINE,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Overwrite one requirement's breakdown outline from the task board editor."""
        config = request_scoped_config(config, biz_line, program_id)
        # 与读取同一条校验：需求键必须真的属于当前项目，才允许落盘。
        self._requirement_for_prototype(config, program_id, requirement_key)
        text = markdown if markdown.endswith("\n") or not markdown.strip() else markdown + "\n"
        document = write_outline_document(self.workspace, requirement_outline_path_of(requirement_key), text)
        identity = self._planning_identity(program_id, requirement_key)
        with self.lock:
            active = self.active_runs.get(identity)
        return {"programId": program_id, "requirementKey": requirement_key, **document, "active": active is not None}

    def requirement_prototype(
        self,
        program_id: int,
        requirement_key: str,
        biz_line: str = DEFAULT_BIZ_LINE,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        config = request_scoped_config(config, biz_line, program_id)
        requirement_prototype_directory_of(requirement_key)
        self._requirement_for_prototype(config, program_id, requirement_key)
        metadata = planner.request_api(
            config,
            "GET",
            "/delivery/requirement/prototype",
            query={"programId": program_id, "requirementKey": requirement_key},
        )
        metadata = metadata if isinstance(metadata, dict) else {}
        path, files = requirement_prototype_files(self.workspace, requirement_key)
        identity = self._requirement_prototype_identity(program_id, requirement_key)
        with self.lock:
            active = self.active_runs.get(identity)
        return {
            "requirementKey": requirement_key,
            "path": path,
            "exists": bool(files),
            "files": files,
            "generatedAt": str(metadata.get("generatedAt") or ""),
            "active": bool(active is not None and active.get("prototype")),
        }

    def _start_requirement_prototype(
        self,
        config: dict[str, Any],
        program_id: int,
        requirement_key: str,
        requirement: dict[str, Any],
        provider: str,
        model: str,
        reasoning_effort: str,
        fast_mode: bool,
        message: str = "",
        editing: bool = False,
        thread_id: str = "",
        previous_detail_digest: str = "",
    ) -> dict[str, Any]:
        identity = self._requirement_prototype_identity(program_id, requirement_key)
        detail_digest = planning_detail_digest(requirement)
        title = f"需求原型 · {str(requirement.get('name') or requirement_key).strip()}"[:120]
        client = create_ai_client(
            provider,
            self.workspace,
            lambda event: self._publish_app_server_event(identity, event),
            codex_environment(config, program_id),
        )
        try:
            prompt = build_requirement_prototype_prompt(
                program_id, requirement, message, self.workspace, editing=editing,
                follow_up=bool(thread_id),
                include_detail=detail_digest != previous_detail_digest,
            )
            if thread_id:
                client.resume_thread(thread_id)
                turn_id = client.start_turn(
                    thread_id,
                    prompt,
                    request_id=client.next_request_id(),
                    model=model,
                    reasoning_effort=reasoning_effort,
                    fast_mode=fast_mode,
                )
            else:
                thread_id, turn_id = client.start_task(
                    title,
                    prompt,
                    model=model,
                    reasoning_effort=reasoning_effort,
                    fast_mode=fast_mode,
                )
            self._save_prototype_session(
                config, program_id, requirement_key, provider, thread_id, turn_id, title, "running", detail_digest,
            )
        except Exception:
            client.close()
            raise
        with self.lock:
            self.active.add(identity)
            self.active_runs[identity] = {
                "client": client,
                "threadId": thread_id,
                "turnId": turn_id,
                "prototype": True,
                "provider": provider,
                "config": config,
                "programId": program_id,
                "title": title,
            }
        self.progress.publish(identity, "status", "正在生成需求 HTML 原型" if not editing else "正在修改需求 HTML 原型", title, "running")
        threading.Thread(
            target=self._follow_requirement_prototype,
            args=(identity, client, config, program_id, requirement_key, provider, thread_id, turn_id, title, detail_digest),
            daemon=True,
        ).start()
        return {
            "accepted": True,
            "programId": program_id,
            "requirementKey": requirement_key,
            "threadId": thread_id,
            "turnId": turn_id,
            "active": True,
        }

    def generate_requirement_prototype(self, raw: Any, config: dict[str, Any] | None = None) -> dict[str, Any]:
        program_id, requirement_key, _message, _thread_id, provider, model = validate_requirement_prototype_payload(raw)
        config = request_scoped_config(config, biz_line_of(raw), program_id)
        requirement = self._requirement_for_prototype(config, program_id, requirement_key)
        if not bool(requirement.get("generatePrototype")):
            raise BridgeFailure("当前需求未启用 HTML 原型生成")
        identity = self._requirement_prototype_identity(program_id, requirement_key)
        with self.lock:
            if identity in self.active:
                raise BridgeFailure("该需求已有正在运行的原型会话，请稍后再试")
        return self._start_requirement_prototype(
            config,
            program_id,
            requirement_key,
            requirement,
            provider,
            model,
            reasoning_effort_of(raw, provider),
            fast_mode_of(raw, provider),
        )

    def requirement_prototype_conversation(
        self,
        program_id: int,
        requirement_key: str,
        thread_id: str = "",
        provider: str = "codex",
        biz_line: str = DEFAULT_BIZ_LINE,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        provider = ai_provider_of(provider)
        config = request_scoped_config(config, biz_line, program_id)
        requirement_prototype_directory_of(requirement_key)
        self._requirement_for_prototype(config, program_id, requirement_key)
        rows = self._prototype_session_rows(config, program_id, requirement_key, provider)
        known_thread_ids = {str(row.get("threadId") or "") for row in rows}
        if thread_id and thread_id not in known_thread_ids:
            raise BridgeFailure("所选原型编辑会话不存在")
        selected_thread_id = thread_id or str((rows[-1] if rows else {}).get("threadId") or "")
        identity = self._requirement_prototype_identity(program_id, requirement_key)
        with self.lock:
            active = self.active_runs.get(identity)
        if not selected_thread_id:
            return {"programId": program_id, "requirementKey": requirement_key, "threadId": "", "executorType": provider, "turns": [], "active": False, "activeTurnId": ""}
        # 线程正文在它自己那个执行器的缓存里：读跟着线程走，不跟当前选中的工具走。
        provider = executor_provider_of(
            next((row for row in rows if str(row.get("threadId") or "") == selected_thread_id), {}), provider,
        )
        live_client = active["client"] if active is not None and active.get("threadId") == selected_thread_id else None
        thread = self._read_thread_with_workspace_archive(
            live_client, selected_thread_id, "requirement", requirement_key, config, program_id,
            provider=provider,
            environment=codex_environment(config, program_id),
        )
        item_key = requirement_prototype_item_key(requirement_key)
        return {
            "programId": program_id,
            "requirementKey": requirement_key,
            "threadId": selected_thread_id,
            "executorType": provider,
            "turns": serialize_turns(
                thread.get("turns") or [],
                artifact_resolver=lambda paths: self.artifacts.register(config_biz_line(config), program_id, item_key, paths),
            ),
            "active": bool(active is not None and active.get("threadId") == selected_thread_id and active.get("prototype")),
            "activeTurnId": str((active or {}).get("turnId") or ""),
        }

    def send_requirement_prototype_message(self, raw: Any, config: dict[str, Any] | None = None) -> dict[str, Any]:
        program_id, requirement_key, message, requested_thread_id, provider, model = validate_requirement_prototype_payload(raw, message_required=True)
        config = request_scoped_config(config, biz_line_of(raw), program_id)
        requirement = self._requirement_for_prototype(config, program_id, requirement_key)
        identity = self._requirement_prototype_identity(program_id, requirement_key)
        with self.lock:
            active = self.active_runs.get(identity)
        if active is not None:
            if requested_thread_id and requested_thread_id != active.get("threadId"):
                raise BridgeFailure("该需求已有正在运行的原型会话，请稍后再试")
            active["client"].steer_turn(
                str(active["threadId"]), str(active["turnId"]), message, request_id=active["client"].next_request_id(),
            )
            return {"accepted": True, "programId": program_id, "requirementKey": requirement_key, "threadId": active["threadId"], "turnId": active["turnId"], "active": True}
        rows = self._prototype_session_rows(config, program_id, requirement_key, provider)
        known_thread_ids = {str(row.get("threadId") or "") for row in rows}
        if requested_thread_id and requested_thread_id not in known_thread_ids:
            raise BridgeFailure("所选原型编辑会话不存在")
        selected_thread_id = requested_thread_id or str((rows[-1] if rows else {}).get("threadId") or "")
        if selected_thread_id:
            # 续已有原型会话只能用这条线程自己的执行器。
            provider = executor_provider_of(
                next((row for row in rows if str(row.get("threadId") or "") == selected_thread_id), {}), provider,
            )
        return self._start_requirement_prototype(
            config,
            program_id,
            requirement_key,
            requirement,
            provider,
            model,
            reasoning_effort_of(raw, provider),
            fast_mode_of(raw, provider),
            message=message,
            editing=True,
            thread_id=selected_thread_id,
            previous_detail_digest=prototype_session_detail_digest(rows, selected_thread_id),
        )

    def _follow_requirement_prototype(
        self,
        identity: tuple[str, int, str],
        client: AppServerClient,
        config: dict[str, Any],
        program_id: int,
        requirement_key: str,
        provider: str,
        thread_id: str,
        turn_id: str,
        title: str,
        detail_digest: str = "",
    ) -> None:
        status = "failed"
        try:
            status = client.wait_turn(turn_id)
            self._archive_terminal_chat(
                client,
                config=config,
                program_id=program_id,
                resource_kind="requirement",
                resource_key=requirement_key,
                resource_name=title,
                conversation_title=title,
                thread_id=thread_id,
                provider=provider,
                phase="prototype",
                terminal_status=status,
            )
            if status == "completed":
                path, files = requirement_prototype_files(self.workspace, requirement_key)
                if not files:
                    raise BridgeFailure("未生成 HTML 原型文件")
                planner.request_api(
                    config,
                    "POST",
                    "/delivery/requirement/prototype/save",
                    body={"programId": program_id, "requirementKey": requirement_key, "path": path, "actorName": f"{provider}-http-bridge"},
                )
            self._save_prototype_session(config, program_id, requirement_key, provider, thread_id, turn_id, title, status, detail_digest)
            self.progress.publish(
                identity,
                "status",
                "需求 HTML 原型已更新" if status == "completed" else "需求 HTML 原型未完成",
                title,
                status,
            )
        except Exception as exc:
            status = "failed"
            try:
                self._save_prototype_session(config, program_id, requirement_key, provider, thread_id, turn_id, title, status, detail_digest)
            except Exception:
                pass
            self.progress.publish(identity, "error", "同步需求 HTML 原型失败", str(exc), status)
            print(f"同步需求 HTML 原型失败：{program_id}/{requirement_key}: {exc}", file=sys.stderr, flush=True)
        finally:
            client.close()
            with self.lock:
                current = self.active_runs.get(identity)
                if current is not None and current.get("client") is client:
                    self.active.discard(identity)
                    self._release_active_run(identity)

    def prototype_directory(
        self,
        program_id: int,
        item_key: str,
        biz_line: str = DEFAULT_BIZ_LINE,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return the absolute, task-scoped prototype directory when it has images."""
        config = request_scoped_config(config, biz_line, program_id)
        task = self._task_detail(config, program_id, item_key)
        if not bool(task.get("prototypeTask")):
            raise BridgeFailure("当前任务不是原型图生成任务")
        relative = Path(prototype_directory_of(task))
        if relative.is_absolute() or ".." in relative.parts:
            raise BridgeFailure("原型图目录无效")
        directory = (self.workspace / relative).resolve()
        try:
            directory.relative_to(self.workspace)
        except ValueError as exc:
            raise BridgeFailure("原型图目录超出当前项目") from exc
        image_count = 0
        if directory.is_dir():
            image_count = sum(
                1 for path in directory.rglob("*")
                if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES
            )
        return {"path": str(directory), "exists": image_count > 0, "imageCount": image_count}

    def open_prototype_directory(
        self,
        program_id: int,
        item_key: str,
        biz_line: str = DEFAULT_BIZ_LINE,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        directory = self.prototype_directory(program_id, item_key, biz_line=biz_line, config=config)
        if not directory["exists"]:
            raise BridgeFailure("原型图尚未生成，暂时不能打开目录")
        opener = shutil.which("open")
        if not opener:
            raise BridgeFailure("当前系统不支持打开本机原型图目录")
        try:
            subprocess.Popen([opener, directory["path"]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as exc:
            raise BridgeFailure(f"打开原型图目录失败：{exc}") from exc
        return directory

    def send_conversation(self, raw: Any, config: dict[str, Any] | None = None) -> dict[str, Any]:
        provider = ai_provider_of(raw)
        biz_line = biz_line_of(raw)
        program_id, item_key, text, requested_thread_id, new_conversation, attachment_ids, model, reasoning_effort, fast_mode, references = validate_conversation_payload(raw)
        config = request_scoped_config(config, biz_line, program_id)
        biz_line = config_biz_line(config)
        attachments = self.attachments.resolve(program_id, item_key, attachment_ids)
        message = message_with_attachments(text, attachments)
        mention_context = self._conversation_mention_context(config, program_id, references)
        identity = task_identity(biz_line, program_id, item_key)
        with self.lock:
            active = self.active_runs.get(identity)
        if active is not None:
            if new_conversation or (requested_thread_id and requested_thread_id != active["threadId"]):
                raise BridgeFailure("该任务已有正在运行的 Codex 会话，请先停止或等待当前回合结束")
            client = active["client"]
            client.steer_turn(
                active["threadId"],
                active["turnId"],
                with_mention_context(
                    message, [*follow_up_context_lines(active.get("task") or {"itemKey": item_key}), *mention_context]
                ),
                attachments,
                request_id=client.next_request_id(),
            )
            self.progress.publish(identity, "message", "已追加要求", text or "已添加附件", "running")
            return {
                "accepted": True,
                "bizLine": biz_line,
                "programId": program_id,
                "itemKey": item_key,
                "threadId": active["threadId"],
                "turnId": active["turnId"],
                "active": True,
            }

        task = self._task_detail(config, program_id, item_key)
        mentioned_message = with_mention_context(message, [*follow_up_context_lines(task), *mention_context])
        binding = self._session_binding(config, program_id, item_key, str(task.get("phase") or "requirement"), provider)
        current_thread_id = str((binding or {}).get("externalSessionId") or "")
        catalog = conversation_catalog(binding)
        known_thread_ids = {str(entry["threadId"]) for entry in catalog}
        if requested_thread_id and requested_thread_id not in known_thread_ids:
            raise BridgeFailure("所选 Codex 会话不存在")
        if new_conversation:
            if binding and binding.get("status") == "running":
                raise BridgeFailure("该任务已有正在运行的 Codex 会话，请先停止或等待当前回合结束")
            return self._start_new_conversation(
                config, program_id, item_key, task, binding, message, attachments, model, provider, reasoning_effort, fast_mode, mention_context
            )
        thread_id = requested_thread_id or current_thread_id
        # 续已有会话只能用这条线程自己的执行器，换工具读不到它的正文。
        provider = executor_provider_of(binding, provider)
        metadata = (binding or {}).get("metadata") or {}
        running_turn_id = str(metadata.get("turnId") or "") if isinstance(metadata, dict) else ""
        if binding and binding.get("status") == "running" and thread_id == current_thread_id and running_turn_id:
            active = self._resume_active_turn(config, identity, task, binding, thread_id, running_turn_id, provider)
            client = active["client"]
            client.steer_turn(thread_id, running_turn_id, mentioned_message, attachments, request_id=client.next_request_id())
            self.progress.publish(identity, "message", "已追加要求", text or "已添加附件", "running")
            return {
                "accepted": True,
                "bizLine": biz_line,
                "programId": program_id,
                "itemKey": item_key,
                "threadId": thread_id,
                "turnId": running_turn_id,
                "active": True,
            }
        if not thread_id:
            return self.execute(
                {
                    "bizLine": biz_line,
                    "programId": program_id,
                    "task": task,
                    "followUp": message,
                    "conversationReferences": references,
                    "followUpAttachments": attachments,
                    "model": model,
                    "provider": provider,
                    **({"reasoningEffort": reasoning_effort} if reasoning_effort else {}),
                    **({"fastMode": True} if fast_mode else {}),
                },
                config=config,
            )
        return self._start_follow_up_turn(
            config, program_id, item_key, task, binding, thread_id, mentioned_message, attachments, model, provider, reasoning_effort, fast_mode
        )

    def stop_conversation(self, raw: Any, config: dict[str, Any] | None = None) -> dict[str, Any]:
        provider = ai_provider_of(raw)
        biz_line, program_id, item_key = validate_task_identity(raw)
        config = request_scoped_config(config, biz_line, program_id)
        biz_line = config_biz_line(config)
        requested_thread_id = str(raw.get("threadId") or "").strip() if isinstance(raw, dict) else ""
        identity = task_identity(biz_line, program_id, item_key)
        with self.lock:
            active = self.active_runs.get(identity)
        if active is not None and requested_thread_id and requested_thread_id != active["threadId"]:
            raise BridgeFailure("所选 Codex 会话当前没有正在运行的回合")
        if active is None:
            task = self._task_detail(config, program_id, item_key)
            binding = self._session_binding(config, program_id, item_key, str(task.get("phase") or "requirement"), provider)
            metadata = (binding or {}).get("metadata") or {}
            thread_id = str((binding or {}).get("externalSessionId") or "")
            turn_id = str(metadata.get("turnId") or "") if isinstance(metadata, dict) else ""
            if requested_thread_id and requested_thread_id != thread_id:
                raise BridgeFailure("所选 Codex 会话当前没有正在运行的回合")
            if not binding or binding.get("status") != "running" or not thread_id or not turn_id:
                raise BridgeFailure("该任务当前没有正在运行的 Codex 回合")
            active = self._resume_active_turn(
                config, identity, task, binding, thread_id, turn_id, executor_provider_of(binding, provider),
            )
        client = active["client"]
        try:
            client.interrupt_turn(active["threadId"], active["turnId"], request_id=client.next_request_id())
        except Exception as error:
            if not turn_already_finished(error):
                raise
            # 回合早就跑完了，本地记录是残留；跟随线程会把会话状态收尾，这里只把事实告诉调用方。
            self.progress.publish(
                identity, "status", "任务已经结束", "该任务当前没有正在运行的回合，状态稍后自动同步。", "success",
            )
            return {
                "accepted": True,
                "alreadyFinished": True,
                "bizLine": biz_line,
                "programId": program_id,
                "itemKey": item_key,
                "threadId": active["threadId"],
                "turnId": active["turnId"],
            }
        self.progress.publish(identity, "status", "已请求停止任务", "正在等待 Codex 中断当前回合。", "running")
        return {
            "accepted": True,
            "alreadyFinished": False,
            "bizLine": biz_line,
            "programId": program_id,
            "itemKey": item_key,
            "threadId": active["threadId"],
            "turnId": active["turnId"],
        }

    def stop_all_executions(self, raw: Any, config: dict[str, Any] | None = None) -> dict[str, Any]:
        """停掉一个项目下所有任务执行：中断在跑的回合，并取消还在排队的批量/串行队列。

        只针对任务执行本身，需求拆解、测试、环境预设这些会话各有各的停止入口，不在这里连坐。
        """
        if not isinstance(raw, dict):
            raise BridgeFailure("请求体必须是 JSON 对象")
        biz_line = biz_line_of(raw)
        program_id = program_id_of(raw.get("programId"))
        config = request_scoped_config(config, biz_line, program_id)
        biz_line = config_biz_line(config)
        with self.lock:
            queue_ids = sorted(qid for qid, pid in self.queue_programs.items() if pid == program_id)
            self.cancelled_queues.update(queue_ids)
            runs = [
                (identity, run) for identity, run in self.active_runs.items()
                if identity[0] == biz_line and identity[1] == program_id and run.get("task")
                and not run.get("taskTestingCases")
            ]
        stopped: list[str] = []
        finished: list[str] = []
        for identity, run in runs:
            client = run.get("client")
            thread_id = str(run.get("threadId") or "")
            turn_id = str(run.get("turnId") or "")
            if client is None or not thread_id or not turn_id:
                continue
            try:
                client.interrupt_turn(thread_id, turn_id, request_id=client.next_request_id())
            except Exception as error:
                if turn_already_finished(error):
                    finished.append(identity[2])
                    continue
                print(f"停止任务失败 {program_id}/{identity[2]}: {error}", file=sys.stderr, flush=True)
                continue
            stopped.append(identity[2])
            self.progress.publish(identity, "status", "已请求停止任务", "正在等待中断当前回合。", "running")
        return {
            "accepted": True,
            "bizLine": biz_line,
            "programId": program_id,
            "itemKeys": sorted(stopped),
            "finishedItemKeys": sorted(finished),
            "queueIds": queue_ids,
        }

    def _task_detail(self, config: dict[str, Any], program_id: int, item_key: str) -> dict[str, Any]:
        task = planner.request_api(
            config, "GET", "/delivery/item", query={"programId": program_id, "itemKey": item_key}
        )
        if not isinstance(task, dict) or not task.get("itemKey"):
            raise BridgeFailure("任务不存在")
        return task

    def _conversation_mention_context(
        self,
        config: dict[str, Any],
        program_id: int,
        references: list[dict[str, str]],
        project_context: dict[str, Any] | None = None,
        current_requirement_key: str = "",
    ) -> list[str]:
        """Load authoritative @ references and the requirement/task that connects them."""
        if not references:
            return []
        context = project_context or planner.project_context(config, program_id)
        items = [item for item in context.get("items") or [] if isinstance(item, dict)]
        items_by_key = {str(item.get("itemKey") or ""): item for item in items}
        requirement_cache: dict[str, dict[str, Any]] = {}
        task_cache: dict[str, dict[str, Any]] = {}

        def requirement_of(key: str) -> dict[str, Any]:
            if key not in requirement_cache:
                requirement_cache[key] = planner.requirement_record(config, program_id, key)
            return requirement_cache[key]

        def task_of(key: str) -> dict[str, Any]:
            if key not in task_cache:
                task_cache[key] = self._task_detail(config, program_id, key)
            return task_cache[key]

        def readable_detail(value: Any, limit: int = 6000) -> str:
            text = str(value or "").strip()
            return text if len(text) <= limit else f"{text[:limit]}…（已截断）"

        lines = ["用户在本轮消息中 @ 了以下关联对象。它们是本轮的补充上下文，按需参考，不能改写当前任务或需求的边界："]
        for reference in references:
            kind = reference["kind"]
            key = reference["key"]
            if kind == "file":
                if not current_requirement_key:
                    raise BridgeFailure("文件引用只能用于需求编辑聊天")
                scope = str(reference.get("scope") or "")
                if scope == "requirement-prototype":
                    _, prototype_files = requirement_prototype_files(self.workspace, current_requirement_key)
                    allowed_files = {str(file.get("path") or ""): str(file.get("name") or "") for file in prototype_files}
                else:
                    directory, _, recursive = self._document_set_layout(
                        config, program_id, scope, current_requirement_key,
                    )
                    allowed_files = {
                        str(file.get("path") or ""): str(file.get("name") or "")
                        for file in document_set_entries(self.workspace, directory, recursive)
                    }
                if key not in allowed_files:
                    raise BridgeFailure("引用文件不存在或不属于当前需求")
                lines.extend([
                    f"@文件 {allowed_files[key] or Path(key).name}",
                    f"文件路径: {key}",
                    "这是当前需求的相关文档；需要正文时从工作区按上述路径读取。",
                ])
                continue
            if kind == "requirement":
                requirement = requirement_of(key)
                related_items = [item for item in items if str(item.get("requirementKey") or "") == key]
                related_lines = [
                    f"- {item.get('itemKey')}: {item.get('title') or item.get('itemKey')}"
                    f"（{item.get('phase') or '-'}/{item.get('status') or '-'}；需求文档：{document_path_of(item)}）"
                    for item in related_items[:30]
                ]
                lines.extend([
                    f"@需求 {key}: {requirement.get('name') or key}",
                    "需求详情:",
                    readable_detail(requirement.get("detail")) or "（未填写）",
                    "该需求关联的任务:",
                    *(related_lines or ["- 暂无任务"]),
                ])
                continue
            task = task_of(key)
            requirement_key = str(task.get("requirementKey") or "").strip()
            lines.extend([
                f"@任务 {key}: {task.get('title') or key}",
                f"任务说明: {readable_detail(task.get('description'), 4000) or '（未填写）'}",
                f"当前阶段: {task.get('phase') or 'requirement'}/{task.get('status') or 'todo'}",
                f"需求文档: {document_path_of(task)}",
            ])
            if requirement_key:
                requirement = requirement_of(requirement_key)
                lines.extend([
                    f"所属需求 {requirement_key}: {requirement.get('name') or requirement_key}",
                    "所属需求详情:",
                    readable_detail(requirement.get("detail")) or "（未填写）",
                ])
            elif key in items_by_key:
                lines.append("所属需求: 未关联")
        return lines

    def _session_binding(
        self,
        config: dict[str, Any],
        program_id: int,
        item_key: str,
        phase: str | None = None,
        provider: str = "codex",
    ) -> dict[str, Any] | None:
        if phase is None:
            task = self._task_detail(config, program_id, item_key)
            phase = str(task.get("phase") or "requirement")
        sessions = planner.request_api(
            config,
            "GET",
            "/delivery/item/execution-session",
            query={"programId": program_id, "itemKey": item_key, "phase": phase},
        ) or []
        if not isinstance(sessions, list):
            return None
        candidates = [
            session
            for session in sessions
            if isinstance(session, dict)
            and same_executor_purpose(session, "")
            and str(session.get("phase") or "requirement") == phase
        ]
        # 优先当前选中的工具；没有就回落到同阶段另一个工具留下的会话，别让列表凭空空掉。
        return next(
            (session for session in candidates if session.get("executorType") == provider),
            next(iter(candidates), None),
        )

    def _task_session_bindings(
        self,
        config: dict[str, Any],
        program_id: int,
        item_key: str,
        provider: str,
    ) -> list[dict[str, Any]]:
        """Return this task's execution sessions from every delivery phase.

        执行器不参与过滤：换成另一个工具之后，之前那批聊天也要留在列表里，正文再按线程
        自己的执行器去读。测试用例会话用的是带后缀的执行器类型，仍然要排除掉。
        """
        sessions = planner.request_api(
            config,
            "GET",
            "/delivery/item/execution-session",
            query={"programId": program_id, "itemKey": item_key},
        ) or []
        return [
            session
            for session in sessions
            if isinstance(session, dict) and same_executor_purpose(session, "")
        ]

    def _start_new_conversation(
        self,
        config: dict[str, Any],
        program_id: int,
        item_key: str,
        task: dict[str, Any],
        binding: dict[str, Any] | None,
        text: str,
        attachments: list[dict[str, Any]],
        model: str = "",
        provider: str = "codex",
        reasoning_effort: str = "",
        fast_mode: bool = False,
        mention_context: list[str] | None = None,
    ) -> dict[str, Any]:
        identity = task_identity(config_biz_line(config), program_id, item_key)
        with self.lock:
            if identity in self.active:
                raise BridgeFailure("该任务已经在本地执行中")
            self.active.add(identity)
        title = conversation_title(task, binding)
        client = create_ai_client(
            provider,
            self.workspace,
            lambda message: self._publish_app_server_event(identity, message),
            codex_environment(config, program_id),
        )
        try:
            updated_task = self._claim_task(config, program_id, task, f"{provider_label(provider)} 已领取任务，正在创建新的执行会话。", provider)
        except Exception:
            client.close()
            self._release_failed_claim(config, program_id, updated_task, provider)
            with self.lock:
                self.active.discard(identity)
            raise
        try:
            self._migrate_legacy_task_outline(updated_task)
            catalog = requirement_document_catalog(
                (planner.project_context(config, program_id).get("items") or []),
                updated_task,
                self.workspace,
            )
            thread_id, turn_id = client.start_task(
                title,
                build_conversation_prompt(program_id, updated_task, text, self.workspace, catalog, mention_context),
                attachments,
                model,
                reasoning_effort=reasoning_effort,
                fast_mode=fast_mode,
            )
            metadata = conversation_metadata(
                binding,
                thread_id,
                turn_id,
                "running",
                title,
                str(task.get("phase") or "requirement"),
            )
            metadata.update({"workspace": self.workspace.name, "source": "task-board-conversation"})
            refreshed_binding = planner.request_api(
                config,
                "POST",
                "/delivery/item/execution-session/bind",
                body={
                    "programId": program_id,
                    "itemKey": item_key,
                    "executorType": provider,
                    "phase": str(task.get("phase") or "requirement"),
                    "progress": 0,
                    "externalSessionId": thread_id,
                    "status": "running",
                    "metadata": metadata,
                    "actorName": f"{provider}-http-bridge",
                },
            )
            with self.lock:
                self.active_runs[identity] = {
                    "client": client,
                    "threadId": thread_id,
                    "turnId": turn_id,
                    "task": updated_task,
                    "binding": refreshed_binding,
                    "config": config,
                    "provider": provider,
                }
        except Exception:
            client.close()
            self._release_failed_claim(config, program_id, updated_task, provider)
            with self.lock:
                self.active.discard(identity)
                self._release_active_run(identity)
            raise
        self.progress.publish(identity, "status", "已创建新的 Codex 会话", title, "running")
        threading.Thread(
            target=self._follow,
            args=(
                identity, client, config, program_id, item_key, updated_task, refreshed_binding, turn_id,
                text_without_attachment_context(text), model, reasoning_effort, fast_mode,
            ),
            daemon=True,
        ).start()
        return {
            "accepted": True,
            "bizLine": config_biz_line(config),
            "programId": program_id,
            "itemKey": item_key,
            "threadId": thread_id,
            "turnId": turn_id,
            "active": True,
        }

    def _start_follow_up_turn(
        self,
        config: dict[str, Any],
        program_id: int,
        item_key: str,
        task: dict[str, Any],
        binding: dict[str, Any],
        thread_id: str,
        text: str,
        attachments: list[dict[str, Any]],
        model: str = "",
        provider: str = "codex",
        reasoning_effort: str = "",
        fast_mode: bool = False,
    ) -> dict[str, Any]:
        identity = task_identity(config_biz_line(config), program_id, item_key)
        with self.lock:
            if identity in self.active:
                raise BridgeFailure("该任务已经在本地执行中")
            self.active.add(identity)
        client = create_ai_client(
            provider,
            self.workspace,
            lambda message: self._publish_app_server_event(identity, message),
            codex_environment(config, program_id),
        )
        try:
            updated_task = self._claim_task(config, program_id, task, f"{provider_label(provider)} 已领取任务，正在现有会话中继续执行。", provider)
        except Exception:
            client.close()
            with self.lock:
                self.active.discard(identity)
            raise
        try:
            client.resume_thread(thread_id)
            turn_id = client.start_turn(
                thread_id,
                text,
                attachments,
                model=model,
                reasoning_effort=reasoning_effort,
                fast_mode=fast_mode,
            )
            metadata = conversation_metadata(
                binding,
                thread_id,
                turn_id,
                "running",
                phase=str(task.get("phase") or "requirement"),
            )
            metadata.update({"workspace": self.workspace.name, "source": "task-board-conversation"})
            refreshed_binding = planner.request_api(
                config,
                "POST",
                "/delivery/item/execution-session/bind",
                body={
                    "programId": program_id,
                    "itemKey": item_key,
                    "executorType": provider,
                    "phase": str(task.get("phase") or "requirement"),
                    "progress": 0,
                    "externalSessionId": thread_id,
                    "status": "running",
                    "metadata": metadata,
                    "actorName": f"{provider}-http-bridge",
                },
            )
            with self.lock:
                self.active_runs[identity] = {
                    "client": client,
                    "threadId": thread_id,
                    "turnId": turn_id,
                    "task": updated_task,
                    "binding": refreshed_binding,
                    "config": config,
                    "provider": provider,
                }
        except Exception:
            client.close()
            with self.lock:
                self.active.discard(identity)
                self._release_active_run(identity)
            raise
        self.progress.publish(identity, "status", "Codex 正在处理追加要求", text, "running")
        threading.Thread(
            target=self._follow,
            args=(identity, client, config, program_id, item_key, updated_task, refreshed_binding, turn_id),
            daemon=True,
        ).start()
        return {
            "accepted": True,
            "bizLine": config_biz_line(config),
            "programId": program_id,
            "itemKey": item_key,
            "threadId": thread_id,
            "turnId": turn_id,
            "active": True,
        }

    def _resume_active_turn(
        self,
        config: dict[str, Any],
        identity: tuple[str, int, str],
        task: dict[str, Any],
        binding: dict[str, Any],
        thread_id: str,
        turn_id: str,
        provider: str = "codex",
    ) -> dict[str, Any]:
        with self.lock:
            current = self.active_runs.get(identity)
            if current is not None:
                return current
            if identity in self.active:
                raise BridgeFailure("该任务正在恢复执行状态，请稍后重试")
            self.active.add(identity)
        client = create_ai_client(
            provider,
            self.workspace,
            lambda message: self._publish_app_server_event(identity, message),
            codex_environment(config, identity[1]),
        )
        try:
            client.resume_thread(thread_id)
            active = {
                "client": client,
                "threadId": thread_id,
                "turnId": turn_id,
                "task": task,
                "binding": binding,
                "config": config,
                "provider": provider,
            }
            with self.lock:
                self.active_runs[identity] = active
        except Exception:
            client.close()
            with self.lock:
                self.active.discard(identity)
                self._release_active_run(identity)
            raise
        threading.Thread(
            target=self._follow,
            args=(identity, client, config, identity[1], identity[2], task, binding, turn_id),
            daemon=True,
        ).start()
        return active

    def _publish_app_server_event(self, identity: tuple[str, int, str], message: dict[str, Any]) -> None:
        generated = generated_image_from_event(message)
        if generated is not None:
            with self.lock:
                active = self.active_runs.get(identity)
            if active is not None:
                try:
                    self.attachments.save_generated_image(
                        config_biz_line(active.get("config") or {}),
                        identity[1],
                        identity[2],
                        str(active.get("threadId") or ""),
                        str(active.get("turnId") or ""),
                        generated[0],
                        generated[1],
                    )
                    self.progress.publish(identity, "file", "图片已生成", "可在聊天记录中预览", "success")
                except BridgeFailure as exc:
                    print(f"保存 Codex 生成图片失败：{identity[1]}/{identity[2]}: {exc}", file=sys.stderr, flush=True)
        event = progress_event_of(message)
        if event is not None:
            self.progress.publish(identity, *event)

    def _retry_corrupted_turn(
        self,
        identity: tuple[str, int, str],
        client: AppServerClient | ClaudeCLIClient,
        turn_id: str,
        turn_status: str,
        turn: dict[str, Any],
        phase: str,
        turn_prompt: str,
        model: str = "",
        reasoning_effort: str = "",
        fast_mode: bool = False,
    ) -> tuple[str, str, dict[str, Any], str]:
        """一轮没能发出任何工具调用就用同样的输入重跑一次，仍然不行就判失败。

        返回 `(turn_id, turn_status, turn, corrupted_reason)`。`corrupted_reason`
        非空表示重试后依然无效，此时状态已经被改成 `failed`，调用方不要把这一轮
        的文字当成产物存下去。
        """
        reason = corrupted_turn_reason(turn_status, turn, phase)
        if not reason:
            return turn_id, turn_status, turn, ""
        diagnostics = getattr(client, "stderr_tail", lambda limit=10: "")()
        print(
            f"检测到无效执行回合：{identity} {reason}" + (f"\napp-server stderr:\n{diagnostics}" if diagnostics else ""),
            file=sys.stderr,
            flush=True,
        )
        with self.lock:
            current = self.active_runs.get(identity)
            # 用户已经追加了新回合，那一轮有自己的 _follow，这里不该再插一轮进去。
            has_newer_turn = current is not None and str(current.get("turnId") or "") != turn_id
        if has_newer_turn or not turn_prompt.strip():
            return turn_id, "failed", turn, reason
        self.progress.publish(identity, "status", "本轮执行无效，正在自动重试", reason, "running")
        retried_id = client.start_turn(
            str(client.thread_id or ""),
            turn_prompt,
            request_id=client.next_request_id(),
            model=model,
            reasoning_effort=reasoning_effort,
            fast_mode=fast_mode,
        )
        with self.lock:
            current = self.active_runs.get(identity)
            if current is not None:
                current["turnId"] = retried_id
        retried_status = client.wait_turn(retried_id)
        retried_turn = client.read_turn(client.thread_id, retried_id, request_id=client.next_request_id())
        retried_reason = corrupted_turn_reason(retried_status, retried_turn, phase)
        if retried_reason:
            return retried_id, "failed", retried_turn, f"重试后依然无效：{retried_reason}"
        return retried_id, retried_status, retried_turn, ""

    def _follow(
        self,
        identity: tuple[str, int, str],
        client: AppServerClient,
        config: dict[str, Any],
        program_id: int,
        item_key: str,
        task: dict[str, Any],
        binding: dict[str, Any],
        turn_id: str,
        initial_message: str = "",
        model: str = "",
        reasoning_effort: str = "",
        fast_mode: bool = False,
        turn_prompt: str = "",
    ) -> None:
        provider = str((self.active_runs.get(identity) or {}).get("provider") or "codex")
        try:
            turn_status = client.wait_turn(turn_id)
            turn = client.read_turn(client.thread_id, turn_id, request_id=client.next_request_id())
            task_name = str(task.get("title") or item_key)
            phase = str(task.get("phase") or "requirement")
            turn_id, turn_status, turn, corrupted_reason = self._retry_corrupted_turn(
                identity, client, turn_id, turn_status, turn, phase, turn_prompt,
                model, reasoning_effort, fast_mode,
            )
            thread_id = str(client.thread_id or binding.get("externalSessionId") or "")
            entry = next((item for item in conversation_catalog(binding) if item.get("threadId") == thread_id), {})
            title = str(entry.get("title") or task_name)
            # 任务聊天也只在新开窗口的首回合命名，避免后续追问把既有标题改掉。
            if turn_status == "completed" and initial_message.strip():
                reply = final_agent_text_from_output(execution_output(turn_status, turn))
                generated_title = self._name_conversation(
                    config, program_id, provider, model, reasoning_effort, fast_mode, initial_message, reply,
                )
                if generated_title:
                    title = generated_title
                    self._rename_conversation(client, thread_id, title)
            self._archive_terminal_chat(
                client,
                config=config,
                program_id=program_id,
                resource_kind="task",
                resource_key=item_key,
                resource_name=task_name,
                requirement_key=str(task.get("requirementKey") or ""),
                conversation_title=title,
                thread_id=thread_id,
                provider=provider,
                phase=phase,
                terminal_status=turn_status,
            )
            with self.lock:
                current = self.active_runs.get(identity)
                has_newer_turn = current is not None and str(current.get("turnId") or "") != turn_id
            if not has_newer_turn:
                self._sync_result(
                    config,
                    program_id,
                    item_key,
                    task,
                    binding,
                    turn_id,
                    turn_status,
                    # 无效回合不入库：那段自称完成的文字既不是产物，也会污染累积的产物文档。
                    "" if corrupted_reason else execution_output(turn_status, turn),
                    provider,
                    title,
                    corrupted_reason,
                )
            # Closing app-server flushes the final turn to the shared Codex session
            # store. Consumers notified before this point can observe 100% progress
            # while still reading the previous conversation snapshot.
            client.close()
            self.progress.publish(
                identity,
                "error" if corrupted_reason else "status",
                "任务已完成" if turn_status == "completed" else "任务执行未完成",
                corrupted_reason or f"结果已同步到任务面板，状态：{turn_status}",
                turn_status,
            )
        except Exception as exc:
            self.progress.publish(identity, "error", "同步执行结果失败", str(exc), "failed")
            print(f"同步 Codex 执行结果失败：{program_id}/{item_key}: {exc}", file=sys.stderr, flush=True)
        finally:
            client.close()
            with self.lock:
                current = self.active_runs.get(identity)
                if current is None or current.get("client") is client:
                    self.active.discard(identity)
                    self._release_active_run(identity)

    def _sync_result(
        self,
        config: dict[str, Any],
        program_id: int,
        item_key: str,
        task: dict[str, Any],
        binding: dict[str, Any],
        turn_id: str,
        turn_status: str,
        execution_output_text: str = "",
        provider: str = "codex",
        conversation_title: str = "",
        failure_reason: str = "",
    ) -> None:
        session_status = SESSION_STATUS.get(turn_status, "blocked")
        phase = str(task.get("phase") or "requirement")
        task_status = "done" if turn_status == "completed" else "blocked"
        testing_verdict = testing_verdict_from_output(execution_output_text) if phase == "testing" else ""
        if phase == "testing" and testing_verdict != "通过":
            # A completed Codex turn means the report was produced. The task is
            # done only when that report explicitly accepts the deliverable.
            task_status = "blocked"
        # Keep the task authoritative. If session closing fails, reconciliation can
        # retry it without leaving the task stuck in its current phase.
        current_task = self._task_detail(config, program_id, item_key)
        if current_task.get("status") not in {"dropped", "done"}:
            output_field = {"development": "actionOutput", "testing": "testingReport"}.get(phase)
            patch_body = {
                "programId": program_id,
                "itemKey": item_key,
                "version": int(current_task["version"]),
                "status": task_status,
                "progress": 100 if task_status == "done" else int(current_task.get("progress") or 0),
                "comment": (
                    f"{provider_label(provider)} {phase} 阶段结束，状态：{turn_status}。"
                    + (f"验收判定：{testing_verdict or '缺失'}。" if phase == "testing" else "")
                    + (f"本轮判定为无效执行：{failure_reason}。" if failure_reason else "")
                ),
                "actorName": f"{provider}-http-bridge",
            }
            if output_field:
                # 追加回合只产出增量：覆盖会把同一阶段前几轮的产物文档整段丢掉。
                patch_body[output_field] = merged_execution_output(
                    str(current_task.get(output_field) or ""), execution_output_text
                )
            if phase == "requirement" and turn_status == "completed":
                requirement_text = final_agent_text_from_output(execution_output_text)
                self._persist_requirement_document(current_task, requirement_text)
            self._request_with_retry(
                config,
                "/delivery/item/patch",
                patch_body,
            )
        session_sync = {
            "bizLine": config_biz_line(config),
            "programId": program_id,
            "itemKey": item_key,
            "executorType": provider,
            "phase": phase,
            "version": int(binding["version"]),
            "status": session_status,
            "progress": 100 if turn_status == "completed" else 0,
            "metadata": {
                **conversation_metadata(
                    binding,
                    str(binding.get("externalSessionId") or ""),
                    turn_id,
                    turn_status,
                    conversation_title,
                    phase=phase,
                ),
                "workspace": self.workspace.name,
            },
            "actorName": f"{provider}-http-bridge",
        }
        self.pending_session_syncs.add(session_sync)
        try:
            self._request_with_retry(config, "/delivery/item/execution-session/status", session_sync)
        except Exception as exc:
            print(
                f"关闭执行会话失败，已加入后台重试：{program_id}/{item_key}: {exc}",
                file=sys.stderr,
                flush=True,
            )
        else:
            self.pending_session_syncs.remove(session_sync)

    def _persist_requirement_document(self, task: dict[str, Any], content: str) -> Path:
        relative = Path(str(task.get("requirementDocumentPath") or ""))
        if not relative.parts or relative.is_absolute() or ".." in relative.parts:
            raise BridgeFailure("任务需求文档路径无效")
        destination = (self.workspace / relative).resolve()
        try:
            destination.relative_to(self.workspace)
        except ValueError as exc:
            raise BridgeFailure("任务需求文档路径超出当前项目") from exc
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.is_file() or not destination.read_text(encoding="utf-8").strip():
            if not content.strip():
                raise BridgeFailure("Codex 已结束，但没有生成可写入需求文档的最终结果")
            destination.write_text(content.strip() + "\n", encoding="utf-8")
        return destination

    def _migrate_legacy_task_outline(self, task: dict[str, Any]) -> Path | None:
        """Copy a retired task outline only when its canonical document does not exist.

        The old file remains untouched so existing links and historical evidence stay valid.
        """
        requirement_key = str(task.get("requirementKey") or "").strip()
        item_key = str(task.get("itemKey") or "").strip()
        if not requirement_key or not item_key:
            return None
        legacy_relative = legacy_task_outline_path_of(requirement_key, item_key)
        document_relative = Path(document_path_of(task))
        if not document_relative.parts or document_relative.is_absolute() or ".." in document_relative.parts:
            raise BridgeFailure("任务需求文档路径无效")
        destination = (self.workspace / document_relative).resolve()
        try:
            destination.relative_to(self.workspace)
        except ValueError as exc:
            raise BridgeFailure("任务需求文档路径超出当前项目") from exc
        if destination.is_file():
            return None
        source = outline_file_in_workspace(self.workspace, legacy_relative)
        if not source.is_file():
            return None
        if source.stat().st_size > MAX_REQUIREMENT_DOCUMENT_BYTES:
            return None
        try:
            content = source.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")
        return destination

    @staticmethod
    def _request_with_retry(config: dict[str, Any], path: str, body: dict[str, Any]) -> Any:
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                return planner.request_api(config, "POST", path, body=body)
            except Exception as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(1 << attempt)
        assert last_error is not None
        raise last_error


EXECUTION_OUTPUT_LIMIT = 8 * 1024 * 1024

# 这一轮真正动了工作区的条目类型。只有 agentMessage / reasoning 的回合等于什么都没做。
TOOL_CALL_ITEM_TYPES = {"commandExecution", "fileChange", "fileEdit", "mcpToolCall", "dynamicToolCall"}
# 模型没能把工具调用发成调用帧时，会把工具 schema 连同超时毫秒数一起写进正文，
# 而且开头那个 T 经常被吃掉（实测出现过 `ARGET_TOOL_SCHEMA={...}"120000`），
# 所以这里故意只匹配后半截，两种写法都能命中。
LEAKED_TOOL_CALL_MARKER = "ARGET_TOOL_SCHEMA"
# 这两个阶段的产物就是代码和验证结果，不可能只靠一段文字交付。
# 梳理需求阶段允许只写文档，不能套用这条判定。
WORKING_PHASES = {"development", "testing"}


def turn_agent_text(turn: dict[str, Any]) -> str:
    parts = [
        str(item.get("text") or item.get("content") or "").strip()
        for item in turn.get("items") or []
        if str(item.get("type") or "") == "agentMessage"
    ]
    return "\n".join(part for part in parts if part)


def turn_tool_call_count(turn: dict[str, Any]) -> int:
    return sum(
        1 for item in turn.get("items") or []
        if str(item.get("type") or "") in TOOL_CALL_ITEM_TYPES
    )


def corrupted_turn_reason(turn_status: str, turn: dict[str, Any], phase: str) -> str:
    """回合自称成功、实际什么都没干时给出原因，否则返回空串。

    这是 gpt-5.6-terra 走自建中转时实测到的失败形态：模型没有发出任何工具调用，
    把本该是调用的内容写进了最终回复，还顺带自证「本会话没有暴露 shell/exec 工具」。
    这种回复以前会被当成动作执行产物存进任务，任务直接被判成 done。
    """
    if turn_status != "completed":
        # 非正常结束本来就会被判成 blocked，交给既有路径处理。
        return ""
    if LEAKED_TOOL_CALL_MARKER in turn_agent_text(turn):
        return "最终回复里混进了工具调用的 schema 残片，说明这一轮的工具调用没有真正发出去"
    if phase in WORKING_PHASES and turn_tool_call_count(turn) == 0:
        return "整轮没有任何命令执行或文件改动，动作执行/测试阶段不可能只靠一段文字完成"
    return ""


def execution_output(turn_status: str, turn: dict[str, Any]) -> str:
    """Persist a readable Markdown summary instead of exposing protocol JSON."""
    lines = ["# Codex 执行结果", "", f"- 状态：{turn_status}", f"- 完成时间：{datetime.now(timezone.utc).isoformat()}", ""]
    for item in turn.get("items") or []:
        item_type = str(item.get("type") or "")
        if item_type == "agentMessage":
            text = str(item.get("text") or item.get("content") or "").strip()
            if text:
                lines.extend(["## 进度说明", "", text, ""])
        elif item_type == "commandExecution":
            command = item.get("command") or item.get("commands") or ""
            if isinstance(command, list):
                command = "\n".join(str(part) for part in command)
            lines.extend(["## 执行命令", "", "```sh", str(command), "```", ""])
            exit_code = item.get("exitCode")
            if exit_code not in (None, 0):
                lines.extend([f"命令结果：失败（退出码 {exit_code}）", ""])
    raw = "\n".join(lines).strip() + "\n"
    encoded = raw.encode("utf-8")
    if len(encoded) <= EXECUTION_OUTPUT_LIMIT:
        return raw
    truncated = encoded[: EXECUTION_OUTPUT_LIMIT - 128].decode("utf-8", errors="ignore")
    return truncated + "\n\n[执行记录过长，已在 8MB 处截断]"


def merged_execution_output(previous: str, incoming: str) -> str:
    """把本轮产物接在任务已有产物后面，而不是整段覆盖掉。

    面板的「设计文档」和「成品测试报告」页签读的就是 actionOutput / testingReport。
    一次追加对话只会产出增量，直接覆盖等于把前几轮的产物删掉，用户看到的文档
    就只剩最后一次追加的内容。
    """
    previous_text = (previous or "").strip()
    incoming_text = (incoming or "").strip()
    if not previous_text:
        return f"{incoming_text}\n" if incoming_text else ""
    if not incoming_text or incoming_text in previous_text:
        return f"{previous_text}\n"
    merged = f"{previous_text}\n\n---\n\n{incoming_text}\n"
    encoded = merged.encode("utf-8")
    if len(encoded) <= EXECUTION_OUTPUT_LIMIT:
        return merged
    # 超限时丢最早的回合：最近的产物才是用户正在看的那一份。
    note = "[更早的执行记录已按 8MB 上限截断]\n\n"
    kept = encoded[-(EXECUTION_OUTPUT_LIMIT - len(note.encode("utf-8")) - 128):].decode("utf-8", errors="ignore")
    return note + kept


def final_agent_text_from_output(output: str) -> str:
    marker = "## 进度说明\n\n"
    if marker not in output:
        return output.strip()
    sections = [section.strip() for section in output.split(marker)[1:]]
    cleaned = [section.split("\n\n## 执行命令", 1)[0].strip() for section in sections if section.strip()]
    return cleaned[-1] if cleaned else output.strip()


def testing_verdict_from_output(output: str) -> str:
    """Read the exact verdict required by the testing skill from the final reply."""
    final_text = final_agent_text_from_output(output)
    match = re.search(r"(?m)^\s*验收判定\s*[:：]\s*(通过|不通过|受阻)\s*$", final_text)
    return match.group(1) if match else ""


BATCH_OUTCOME_RE = re.compile(r"(?m)^\s*批量判定\s*[:：]\s*(完成|可忽略|需人工处理)\s*$")
BATCH_TURN_STATUS_RE = re.compile(r"(?m)^\s*-?\s*状态\s*[:：]\s*([A-Za-z]+)\s*$")
# These markers are intentionally limited to evidence of a deliverable-level
# problem. A generic "warning" or a command mention must not stop a queue.
BATCH_HARD_PROBLEM_RE = re.compile(
    r"(?:无法(?:完成|实现|继续|验证)|(?:编译|构建|测试|命令).{0,20}(?:失败|错误|不通过)|"
    r"命令结果.{0,12}失败|退出码\s*[1-9]\d*|"
    r"(?:需要|需)(?:人工|处理)|(?:权限|依赖|数据).{0,12}(?:不足|缺少|错误)|阻塞|受阻|冲突)",
    re.IGNORECASE,
)


def batch_task_outcome(task: dict[str, Any]) -> tuple[str, str]:
    """Classify a finished queue item without changing its authoritative task status.

    ``completed`` means the task board already accepted the task. ``ignorable``
    is a queue-local skip for a transient interruption; the task remains
    blocked so the user can inspect and retry it later. Everything else is a
    real queue blocker.
    """
    status = str(task.get("status") or "").strip().lower()
    if status == "done":
        return "completed", "任务已完成"

    output = str(task.get("actionOutput") or task.get("testingReport") or "").strip()
    final_text = final_agent_text_from_output(output)
    explicit = BATCH_OUTCOME_RE.search(final_text)
    if explicit:
        verdict = explicit.group(1)
        if verdict == "完成" and status == "done":
            return "completed", "任务已完成"
        if verdict == "可忽略":
            return "ignorable", "执行回合已中断，但未发现代码、编译、测试或权限阻塞证据。"
        return "hard", "执行器报告存在需要人工处理的实质问题。"

    turn_status_match = BATCH_TURN_STATUS_RE.search(output)
    turn_status = turn_status_match.group(1).lower() if turn_status_match else ""
    if BATCH_HARD_PROBLEM_RE.search(final_text):
        return "hard", "执行结果包含代码、编译、测试、权限、依赖或其他实质阻塞信息。"

    # An interrupted turn with no substantive failure evidence is safe to
    # bypass in the current queue. It is still blocked on the board and will
    # remain visible for a later manual retry.
    if turn_status in {"interrupted", "failed"}:
        return "ignorable", "执行回合意外终止，未发现实质阻塞信息。"

    return "hard", f"任务状态为 {status or 'unknown'}，且没有可忽略判定。"


def text_from_user_item(item: dict[str, Any]) -> str:
    content = item.get("content") or item.get("input") or []
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for part in content:
        if isinstance(part, str):
            parts.append(part)
            continue
        if isinstance(part, dict) and str(part.get("type") or "") == "text":
            parts.append(str(part.get("text") or ""))
    return "\n".join(part.strip() for part in parts if part.strip())


FILE_CHANGE_KINDS = {"add", "added", "create", "created", "delete", "deleted", "remove", "removed", "modify", "modified", "update", "updated", "rename", "renamed"}
FILE_CHANGE_ALIASES = {
    "added": "add",
    "create": "add",
    "created": "add",
    "deleted": "delete",
    "remove": "delete",
    "removed": "delete",
    "modified": "modify",
    "update": "modify",
    "updated": "modify",
    "renamed": "rename",
}


def diff_line_counts(diff: Any) -> tuple[int, int]:
    """数一份 unified diff 的增删行数，面板据此显示 `+74 -4`。"""
    if not isinstance(diff, str) or not diff:
        return 0, 0
    added = removed = 0
    for line in diff.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
    return added, removed


def file_changes_of(item: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize one file-change item into `[{path, kind, added, removed}]`.

    Codex 和 Claude 给的字段名不完全一样，面板只认 path + add/modify/delete/rename。
    Codex 的 `kind` 实测是对象（`{"type": "update", "move_path": null}`），
    当字符串读会一律退化成 modify，新增和删除就分不出来了。
    """
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for change in item.get("changes") or []:
        if not isinstance(change, dict):
            continue
        path = str(change.get("path") or change.get("file") or change.get("filePath") or "").strip()
        if not path or path in seen:
            continue
        seen.add(path)
        kind_value = change.get("kind") or change.get("type") or change.get("changeType") or ""
        if isinstance(kind_value, dict):
            kind_value = kind_value.get("type") or kind_value.get("kind") or ""
        raw_kind = str(kind_value).strip().lower()
        kind = FILE_CHANGE_ALIASES.get(raw_kind, raw_kind if raw_kind in FILE_CHANGE_KINDS else "modify")
        added, removed = diff_line_counts(change.get("diff") or change.get("unifiedDiff") or change.get("unified_diff"))
        normalized.append({"path": path, "kind": kind, "added": added, "removed": removed})
    return normalized


def chat_archive_component(value: Any, fallback: str, max_bytes: int) -> str:
    """Return one human-readable, cross-platform-safe filename component."""
    text = re.sub(r"[\x00-\x1f\x7f/\\:*?\"<>|]+", "-", str(value or ""))
    text = re.sub(r"\s+", " ", text).strip(" .-")
    if not text:
        text = fallback
    while len(text.encode("utf-8")) > max_bytes:
        text = text[:-1]
    return text.rstrip(" .-") or fallback


def chat_archive_relative_path(
    resource_kind: str,
    resource_key: str,
    conversation_title: str,
    thread_id: str,
    requirement_key: str = "",
) -> Path:
    """Build a stable, readable workspace-relative path for one conversation."""
    if resource_kind not in {"requirement", "task"}:
        raise BridgeFailure("聊天归档类型无效")
    owning_requirement_key = resource_key if resource_kind == "requirement" else requirement_key
    owning_requirement_key = chat_archive_component(owning_requirement_key, "unassigned", 64)
    # All current requirement keys are already `req-*`. Keep the expected
    # directory convention for pre-migration or manually supplied keys too.
    requirement_directory = (
        owning_requirement_key
        if owning_requirement_key.startswith("req-")
        else f"req-{owning_requirement_key}"
    )
    title = chat_archive_component(conversation_title, resource_key or requirement_directory, CHAT_ARCHIVE_MAX_NAME_BYTES)
    thread = chat_archive_component(thread_id, "thread", CHAT_ARCHIVE_MAX_THREAD_ID_BYTES)
    directory = Path(CHAT_ARCHIVE_DIRECTORY_NAME) / CHAT_ARCHIVE_REQUIREMENTS_DIRECTORY_NAME / requirement_directory
    if resource_kind == "task":
        directory /= CHAT_ARCHIVE_TASK_DIRECTORY_NAME
    return directory / f"{title}--{thread}.md"


def visible_chat_archive_turns(turns: Any) -> list[dict[str, Any]]:
    """Keep visible dialogue and display-safe reasoning summaries for restoration."""
    if not isinstance(turns, list):
        return []
    visible: list[dict[str, Any]] = []
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        items: list[dict[str, Any]] = []
        for item in turn.get("items") or []:
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("type") or "")
            if item_type == "userMessage":
                text = text_without_attachment_context(text_from_user_item(item)).strip()
                if text:
                    items.append({"type": "userMessage", "content": [{"type": "text", "text": text}]})
            elif item_type == "agentMessage":
                text = str(item.get("text") or item.get("content") or "").strip()
                if text:
                    items.append({
                        "type": "agentMessage", "text": text,
                        "status": str(item.get("status") or ""),
                        "phase": str(item.get("phase") or ""),
                    })
            elif item_type == "reasoning":
                summary = reasoning_summary_text(item)
                if summary:
                    items.append({
                        "type": "reasoning", "summary": summary.split("\n\n"),
                        "status": str(item.get("status") or ""),
                    })
        visible.append({
            "id": str(turn.get("id") or ""),
            "status": str(turn.get("status") or ""),
            "createdAt": turn.get("createdAt") or turn.get("startedAt") or "",
            "completedAt": turn.get("completedAt") or "",
            "items": items,
        })
    return visible


def archived_chat_text(
    *,
    resource_kind: str,
    resource_key: str,
    resource_name: str,
    requirement_key: str = "",
    conversation_title: str,
    thread_id: str,
    provider: str,
    phase: str,
    terminal_status: str,
    turns: Any,
) -> str:
    """Render visible dialogue and reasoning summaries, never raw tool output."""
    kind_label = "需求" if resource_kind == "requirement" else "任务"
    metadata = {
        "format": "delivery-task-planner-chat/v1",
        "resourceType": resource_kind,
        "resourceKey": resource_key,
        "requirementKey": resource_key if resource_kind == "requirement" else requirement_key,
        "resourceName": resource_name,
        "conversationTitle": conversation_title,
        "threadId": thread_id,
        "provider": provider,
        "phase": phase,
        "lastTurnStatus": terminal_status,
        "archivedAt": utc_now(),
    }
    lines = ["---"]
    lines.extend(f"{key}: {json.dumps(value, ensure_ascii=False)}" for key, value in metadata.items())
    lines.extend(["---", "", f"# {kind_label}聊天 · {conversation_title or resource_name or resource_key}", ""])

    visible_turns = visible_chat_archive_turns(turns)
    for index, turn in enumerate(visible_turns, start=1):
        if not isinstance(turn, dict):
            continue
        status = str(turn.get("status") or "")
        turn_id = str(turn.get("id") or "")
        suffix = f" · {status}" if status else ""
        if turn_id:
            suffix += f" · {turn_id}"
        lines.extend([f"## 第 {index} 轮{suffix}", ""])
        message_count = 0
        for item in turn.get("items") or []:
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("type") or "")
            if item_type == "userMessage":
                text = text_without_attachment_context(text_from_user_item(item)).strip()
                label = "用户"
            elif item_type == "agentMessage":
                text = str(item.get("text") or item.get("content") or "").strip()
                label = "助手"
            elif item_type == "reasoning":
                text = reasoning_summary_text(item)
                label = "推理摘要"
            else:
                # Raw tool/command payloads can expose hidden context or secrets.
                # Reasoning is included only through the summary-only branch above.
                continue
            if not text:
                continue
            lines.extend([f"### {label}", "", text, ""])
            message_count += 1
        if message_count == 0:
            lines.extend(["_本轮没有可归档的用户或助手消息。_", ""])

    if not visible_turns:
        lines.extend(["_会话未返回可归档的回合记录。_", ""])
    # The visible Markdown above is for people. This data block is a compact, lossless
    # representation of the same safe messages so a different machine can restore the
    # project-local backup without trying to parse arbitrary Markdown written by an AI.
    payload = json.dumps({"turns": visible_turns}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    lines.extend(["<!-- delivery-task-planner-chat-data", base64.b64encode(payload).decode("ascii"), "-->", ""])
    return "\n".join(lines).rstrip() + "\n"


def archive_chat_snapshot(
    workspace: Path,
    *,
    resource_kind: str,
    resource_key: str,
    resource_name: str,
    requirement_key: str = "",
    conversation_title: str,
    thread_id: str,
    provider: str,
    phase: str,
    terminal_status: str,
    turns: Any,
) -> Path:
    """Atomically replace one thread's project-local Markdown snapshot."""
    if not thread_id.strip():
        raise BridgeFailure("聊天归档缺少会话标识")
    relative = chat_archive_relative_path(
        resource_kind,
        resource_key,
        conversation_title or resource_name,
        thread_id,
        requirement_key=requirement_key,
    )
    root = workspace.resolve()
    destination = (root / relative).resolve()
    try:
        destination.relative_to(root)
    except ValueError as exc:
        raise BridgeFailure("聊天归档路径超出当前项目") from exc
    content = archived_chat_text(
        resource_kind=resource_kind,
        resource_key=resource_key,
        resource_name=resource_name,
        requirement_key=requirement_key,
        conversation_title=conversation_title,
        thread_id=thread_id,
        provider=provider,
        phase=phase,
        terminal_status=terminal_status,
        turns=turns,
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{secrets.token_hex(6)}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
    return relative


def chat_archive_metadata(content: str) -> dict[str, Any]:
    """Read the small JSON-valued front matter written by `archived_chat_text`."""
    if not content.startswith("---\n"):
        return {}
    end = content.find("\n---\n", 4)
    if end < 0:
        return {}
    metadata: dict[str, Any] = {}
    for line in content[4:end].splitlines():
        key, separator, raw = line.partition(": ")
        if not key or not separator:
            continue
        try:
            metadata[key] = json.loads(raw)
        except json.JSONDecodeError:
            return {}
    return metadata


def archived_chat_turns(content: str) -> list[dict[str, Any]]:
    marker = "<!-- delivery-task-planner-chat-data\n"
    start = content.rfind(marker)
    if start < 0:
        return []
    end = content.find("\n-->", start + len(marker))
    if end < 0:
        return []
    encoded = "".join(content[start + len(marker):end].split())
    try:
        payload = json.loads(base64.b64decode(encoded, validate=True).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return []
    turns = payload.get("turns") if isinstance(payload, dict) else None
    return [turn for turn in turns or [] if isinstance(turn, dict)]


def read_workspace_chat_archive(
    workspace: Path,
    resource_kind: str,
    resource_key: str,
    thread_id: str,
) -> dict[str, Any]:
    """Find a thread snapshot by its immutable metadata, not by a mutable display name."""
    if resource_kind not in {"requirement", "task"} or not resource_key or not thread_id:
        return {}
    workspace_root = workspace.resolve()
    archive_roots = [
        workspace_root / CHAT_ARCHIVE_DIRECTORY_NAME / CHAT_ARCHIVE_REQUIREMENTS_DIRECTORY_NAME,
        # Keep restoring already-synced archives after moving to the English layout.
        workspace_root / CHAT_ARCHIVE_DIRECTORY_NAME / ("需求" if resource_kind == "requirement" else "任务"),
        workspace_root / LEGACY_CHAT_ARCHIVE_DIRECTORY_NAME / ("需求" if resource_kind == "requirement" else "任务"),
    ]
    for archive_root in archive_roots:
        root = archive_root.resolve()
        try:
            root.relative_to(workspace_root)
        except ValueError:
            continue
        if not root.is_dir():
            continue
        try:
            candidates = root.rglob("*.md")
            for count, candidate in enumerate(candidates, start=1):
                if count > CHAT_ARCHIVE_MAX_FILES_TO_SCAN:
                    break
                try:
                    resolved = candidate.resolve()
                    resolved.relative_to(root)
                    if not resolved.is_file() or resolved.stat().st_size > CHAT_ARCHIVE_MAX_FILE_BYTES:
                        continue
                    content = resolved.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError, ValueError):
                    continue
                metadata = chat_archive_metadata(content)
                if (
                    metadata.get("resourceType") != resource_kind
                    or metadata.get("resourceKey") != resource_key
                    or metadata.get("threadId") != thread_id
                ):
                    continue
                turns = archived_chat_turns(content)
                if turns:
                    return {"id": thread_id, "turns": turns, "source": "workspaceArchive"}
        except OSError:
            continue
    return {}


def cloud_sync_workspace_entries(workspace: Path, scopes: set[str]) -> tuple[list[tuple[str, str, Path, str]], int]:
    """Return only configured, workspace-contained files for a project cloud sync.

    `chat/` is isolated from regular project files. Requirement documents are readable
    document formats under `doc/` excluding prototype directories; design documents are
    all regular files under a `prototype/` directory, including referenced images.
    """
    wanted = scopes & CLOUD_SYNC_SCOPES
    if not wanted:
        return [], 0
    root = workspace.resolve()
    entries: dict[str, tuple[str, str, Path, str]] = {}
    skipped = 0

    def offer(category: str, source: Path) -> None:
        nonlocal skipped
        if len(entries) >= MAX_CLOUD_SYNC_FILES_PER_RUN:
            skipped += 1
            return
        try:
            resolved = source.resolve()
            relative = resolved.relative_to(root).as_posix()
            if not resolved.is_file() or resolved.stat().st_size > MAX_CLOUD_SYNC_FILE_BYTES:
                skipped += 1
                return
        except (OSError, ValueError):
            skipped += 1
            return
        content_type = mimetypes.guess_type(relative)[0] or "application/octet-stream"
        entries[relative] = (category, relative, resolved, content_type)

    if "chat" in wanted:
        chat_root = root / CHAT_ARCHIVE_DIRECTORY_NAME
        if chat_root.is_dir():
            try:
                for source in chat_root.rglob("*.md"):
                    offer("chat", source)
            except OSError:
                skipped += 1

    document_root = root / "doc"
    if document_root.is_dir() and ("requirement" in wanted or "design" in wanted):
        try:
            for source in document_root.rglob("*"):
                if not source.is_file():
                    continue
                try:
                    relative_to_document = source.resolve().relative_to(document_root.resolve())
                except (OSError, ValueError):
                    skipped += 1
                    continue
                in_prototype = "prototype" in relative_to_document.parts
                if in_prototype:
                    if "design" in wanted:
                        offer("design", source)
                elif "requirement" in wanted and source.suffix.lower() in DOCUMENT_SET_SUFFIXES:
                    offer("requirement", source)
        except OSError:
            skipped += 1

    return [entries[key] for key in sorted(entries)], skipped


def serialize_turns(
    turns: Any,
    attachment_resolver: Any = None,
    artifact_resolver: Any = None,
    turn_attachment_resolver: Any = None,
) -> list[dict[str, Any]]:
    """Return a small, browser-safe conversation projection of Codex thread history."""
    if not isinstance(turns, list):
        return []
    serialized: list[dict[str, Any]] = []
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        turn_id = str(turn.get("id") or "")
        turn_attachments = turn_attachment_resolver(turn_id) if turn_attachment_resolver else []
        messages: list[dict[str, Any]] = []
        for item in turn.get("items") or []:
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("type") or "")
            text = ""
            action = ""
            attachments: list[dict[str, Any]] = []
            changes: list[dict[str, Any]] = []
            if item_type == "userMessage":
                text = text_from_user_item(item)
                attachment_ids = attachment_ids_from_text(text)
                if attachment_ids and attachment_resolver:
                    try:
                        attachments = attachment_resolver(attachment_ids)
                    except BridgeFailure:
                        attachments = []
                text = text_without_attachment_context(text)
            elif item_type in {"agentMessage", "plan"}:
                text = str(item.get("text") or item.get("content") or item.get("summary") or "").strip()
                if artifact_resolver and item_type == "agentMessage" and str(item.get("phase") or "") == "final_answer":
                    linked_paths = [match.strip().split("#", 1)[0] for match in MARKDOWN_ARTIFACT_RE.findall(text)]
                    attachments = artifact_resolver(linked_paths[:20])
            elif item_type == "reasoning":
                # Do not fall back to `content`: it can contain a non-display
                # reasoning payload. The protocol's `summary` is intentional
                # user-facing content and is safe for the browser projection.
                text = reasoning_summary_text(item)
            elif item_type == "commandExecution":
                command = item.get("command") or item.get("commands") or ""
                text = "\n".join(str(part) for part in command) if isinstance(command, list) else str(command)
            elif item_type in {"mcpToolCall", "dynamicToolCall"}:
                # Claude 的读文件、检索是具名工具，命令行里没有可解析的字面量：
                # 语义在 action/target 上，面板据此显示成「已读取 X」而不是「已调用 Read」。
                action = str(item.get("action") or "")
                text = str(item.get("pattern") or item.get("tool") or item.get("name") or item.get("server") or "")
            elif item_type in {"fileChange", "fileEdit"}:
                changes = file_changes_of(item)
                paths = [change["path"] for change in changes]
                text = "\n".join(paths)
                if artifact_resolver and paths:
                    attachments = artifact_resolver(paths)
            if not text and item_type not in {"fileChange", "fileEdit"}:
                continue
            messages.append(
                {
                    "id": str(item.get("id") or ""),
                    "type": item_type,
                    "text": text,
                    "action": action,
                    "target": str(item.get("target") or ""),
                    "status": str(item.get("status") or ""),
                    "exitCode": item.get("exitCode"),
                    "phase": str(item.get("phase") or ""),
                    "attachments": attachments,
                    # 结构化的改动清单：面板据此在回合末尾汇总「本次改动」，和直接用 CLI 时看到的一致。
                    "changes": changes,
                }
            )
        if turn_attachments:
            target = next(
                (
                    item for item in reversed(messages)
                    if item.get("type") == "agentMessage" and item.get("phase") == "final_answer"
                ),
                next((item for item in reversed(messages) if item.get("type") == "agentMessage"), None),
            )
            if target is not None:
                known_ids = {str(item.get("id") or "") for item in target["attachments"]}
                target["attachments"].extend(
                    item for item in turn_attachments if str(item.get("id") or "") not in known_ids
                )
        serialized.append(
            {
                "id": turn_id,
                "status": str(turn.get("status") or ""),
                "createdAt": turn.get("createdAt") or turn.get("startedAt") or "",
                "completedAt": turn.get("completedAt") or "",
                "items": messages,
            }
        )
    return serialized


def ensure_terminal_result(
    turns: list[dict[str, Any]],
    task: dict[str, Any],
    binding: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Use the task board's persisted result while another Codex process has a stale thread snapshot."""
    if str(task.get("status") or "") != "done":
        return turns
    for turn in turns:
        for item in turn.get("items") or []:
            if item.get("type") == "agentMessage" and item.get("phase") == "final_answer" and str(item.get("text") or "").strip():
                return turns
    phase = str(task.get("phase") or "requirement")
    result_field = {"requirement": "requirementDocument", "development": "actionOutput", "testing": "testingReport"}.get(phase, "")
    result = str(task.get(result_field) or "").strip() if result_field else ""
    if not result:
        return turns
    metadata = (binding or {}).get("metadata") or {}
    turn_id = str(metadata.get("turnId") or "task-board-result") if isinstance(metadata, dict) else "task-board-result"
    if not turns:
        turns.append({"id": turn_id, "status": "completed", "createdAt": 0, "completedAt": 0, "items": []})
    turns[-1]["status"] = "completed"
    turns[-1].setdefault("items", []).append(
        {
            "id": f"{turn_id}-persisted-result",
            "type": "agentMessage",
            "text": result,
            "status": "completed",
            "exitCode": None,
            "phase": "final_answer",
            "attachments": [],
        }
    )
    return turns


class BridgeHandler(BaseHTTPRequestHandler):
    server_version = "DeliveryAIAppServer/0.2"

    @property
    def bridge(self) -> ExecutionBridge:
        return self.server.bridge  # type: ignore[attr-defined]

    @property
    def allowed_origins(self) -> set[str]:
        return self.server.allowed_origins  # type: ignore[attr-defined]

    def allows_all_origins(self) -> bool:
        return "*" in self.allowed_origins

    def allowed_origin(self) -> str:
        # The bridge listens only on loopback and the board may be served from any
        # origin. Direct browser navigations do not include Origin, so use the
        # standard opaque-origin value instead of rejecting those requests.
        return self.headers.get("Origin", "").strip() or "null"

    def cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, token")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        # 面板可能部署在公网origin，浏览器把到 127.0.0.1 的请求当作私有网络访问，
        # preflight 会带 Access-Control-Request-Private-Network，缺了这个应答头就直接报跨域。
        if self.headers.get("Access-Control-Request-Private-Network", "").strip().lower() == "true":
            self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Access-Control-Max-Age", "600")

    def json_response(self, status: int, value: dict[str, Any]) -> None:
        raw = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def attachment_response(self, manifest: dict[str, Any], path: Path) -> None:
        content_type = str(manifest.get("contentType") or "application/octet-stream")
        self.send_response(200)
        self.cors()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(path.stat().st_size))
        self.send_header(
            "Content-Disposition",
            content_disposition_of(str(manifest.get("name") or "attachment"), bool(manifest.get("isImage"))),
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        with path.open("rb") as source:
            shutil.copyfileobj(source, self.wfile)

    def do_OPTIONS(self) -> None:
        if not self.allowed_origin():
            self.json_response(403, {"error": "origin not allowed"})
            return
        self.send_response(204)
        self.cors()
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/healthz":
            self.json_response(200, self.bridge.health())
            return
        if parsed.path == "/v1/plugin/info":
            self.json_response(200, {
                "installed": bool(PLUGIN_RUNTIME_VERSION),
                "version": PLUGIN_RUNTIME_VERSION,
            })
            return
        if parsed.path == "/v1/plugin/runtime-test":
            self.json_response(200, {"value": PLUGIN_RUNTIME_TEST_VALUE})
            return
        if parsed.path == "/v1/plugin/update":
            force = str((parse_qs(parsed.query).get("force") or [""])[0]).lower() in {"1", "true", "yes"}
            status = PLUGIN_UPDATES.status(force=force)
            if isinstance(status.get("installation"), dict):
                status["installation"]["activeRuns"] = self.bridge.active_run_count()
            self.json_response(200, status)
            return
        if parsed.path in {"/v1/codex/workspaces", "/v1/codex/workspace/validate"}:
            if not self.allowed_origin():
                self.json_response(403, {"error": "origin not allowed"})
                return
            query = parse_qs(parsed.query)
            program_id = program_id_of((query.get("programId") or [""])[0])
            try:
                self.bridge.request_config(
                    {"programId": program_id},
                    self.allowed_origin() or "",
                    self.headers.get("token", "").strip(),
                )
                if parsed.path.endswith("/validate"):
                    selected_bridge = self.bridge.for_workspace((query.get("workspace") or [""])[0])
                    self.json_response(200, {
                        "valid": True,
                        "workspace": str(selected_bridge.workspace),
                        "name": selected_bridge.workspace.name,
                    })
                    return
                self.json_response(200, {
                    "projects": codex_local_projects(),
                })
            except (BridgeFailure, planner.ToolFailure, ValueError) as exc:
                self.json_response(400, {"error": str(exc)})
            except Exception as exc:
                self.json_response(500, {"error": f"读取 Codex 工作目录失败：{exc}"})
            return
        if parsed.path == "/v1/codex/git/workspace-check":
            if not self.allowed_origin():
                self.json_response(403, {"error": "origin not allowed"})
                return
            query = parse_qs(parsed.query)
            program_id = program_id_of((query.get("programId") or [""])[0])
            try:
                self.bridge.request_config(
                    {"programId": program_id},
                    self.allowed_origin() or "",
                    self.headers.get("token", "").strip(),
                )
                self.json_response(200, git_workspace_check((query.get("workspace") or [""])[0]))
            except (BridgeFailure, planner.ToolFailure, ValueError) as exc:
                self.json_response(400, {"error": str(exc)})
            except Exception as exc:
                self.json_response(500, {"error": f"检查工作目录 Git 状态失败：{exc}"})
            return
        if parsed.path in {"/v1/codex/git/changes", "/v1/codex/git/change"}:
            if not self.allowed_origin():
                self.json_response(403, {"error": "origin not allowed"})
                return
            query = parse_qs(parsed.query)
            program_id = program_id_of((query.get("programId") or [""])[0])
            try:
                self.bridge.request_config(
                    {"programId": program_id},
                    self.allowed_origin() or "",
                    self.headers.get("token", "").strip(),
                )
                selected_bridge = self.bridge.for_workspace((query.get("workspace") or [""])[0])
                if parsed.path.endswith("/changes"):
                    self.json_response(200, git_change_files(selected_bridge.workspace))
                else:
                    self.json_response(200, git_change_detail(
                        selected_bridge.workspace,
                        str((query.get("path") or [""])[0]),
                    ))
            except (BridgeFailure, planner.ToolFailure, ValueError) as exc:
                self.json_response(400, {"error": str(exc)})
            except Exception as exc:
                self.json_response(500, {"error": f"读取 Git 变更失败：{exc}"})
            return
        if parsed.path == "/v1/codex/git/projects":
            if not self.allowed_origin():
                self.json_response(403, {"error": "origin not allowed"})
                return
            query = parse_qs(parsed.query)
            program_id = program_id_of((query.get("programId") or [""])[0])
            try:
                self.bridge.request_config(
                    {"programId": program_id},
                    self.allowed_origin() or "",
                    self.headers.get("token", "").strip(),
                )
                selected_bridge = self.bridge.for_workspace((query.get("workspace") or [""])[0])
                self.json_response(200, git_workspace_projects(
                    selected_bridge.workspace,
                    str((query.get("branch") or [""])[0]).strip(),
                    str((query.get("remoteName") or ["origin"])[0]),
                ))
            except (BridgeFailure, planner.ToolFailure, ValueError) as exc:
                self.json_response(400, {"error": str(exc)})
            except Exception as exc:
                self.json_response(500, {"error": f"读取子项目 Git 状态失败：{exc}"})
            return
        if parsed.path in {"/v1/codex/git/branches", "/v1/codex/git/status"}:
            if not self.allowed_origin():
                self.json_response(403, {"error": "origin not allowed"})
                return
            query = parse_qs(parsed.query)
            program_id = program_id_of((query.get("programId") or [""])[0])
            try:
                self.bridge.request_config(
                    {"programId": program_id},
                    self.allowed_origin() or "",
                    self.headers.get("token", "").strip(),
                )
                selected_bridge = self.bridge.for_workspace((query.get("workspace") or [""])[0])
                if parsed.path.endswith("/status"):
                    self.json_response(200, git_workspace_status(
                        selected_bridge.workspace,
                        str((query.get("expectedRemoteUrl") or [""])[0]),
                        str((query.get("remoteName") or ["origin"])[0]),
                    ))
                else:
                    self.json_response(200, git_branch_catalog(selected_bridge.workspace))
            except (BridgeFailure, planner.ToolFailure, ValueError) as exc:
                self.json_response(400, {"error": str(exc)})
            except Exception as exc:
                self.json_response(500, {"error": f"读取 Git 分支失败：{exc}"})
            return
        if parsed.path in {"/v1/codex/health", "/v1/codex/models", "/v1/ai/health", "/v1/ai/models"}:
            if not self.allowed_origin():
                self.json_response(403, {"error": "origin not allowed"})
                return
            query = parse_qs(parsed.query)
            provider = ai_provider_of((query.get("provider") or ["codex"])[0])
            program_id_value = (query.get("programId") or [""])[0]
            if parsed.path.endswith("/health") and not str(program_id_value).strip():
                self.json_response(200, self.bridge.health(provider))
                return
            try:
                program_id = program_id_of(program_id_value)
            except BridgeFailure as exc:
                self.json_response(400, {"error": str(exc)})
                return
            try:
                config = self.bridge.request_config(
                    {"programId": program_id},
                    self.allowed_origin() or "",
                    self.headers.get("token", "").strip(),
                )
                selected_bridge = self.bridge.for_workspace((query.get("workspace") or [""])[0])
                if parsed.path.endswith("/health"):
                    self.json_response(200, selected_bridge.health(provider))
                    return
                self.json_response(200, selected_bridge.models(config, provider))
            except (BridgeFailure, planner.ToolFailure, ValueError) as exc:
                self.json_response(400, {"error": str(exc)})
            except Exception as exc:
                action = f"读取 {provider_label(provider)} 模型" if parsed.path.endswith("/models") else f"检查 {provider_label(provider)} 环境"
                self.json_response(500, {"error": f"{action}失败：{exc}"})
            return
        attachment_match = re.fullmatch(r"/v1/codex/attachments/([A-Za-z0-9_-]{16,80})", parsed.path)
        if attachment_match:
            if not self.allowed_origin():
                self.json_response(403, {"error": "origin not allowed"})
                return
            try:
                query = parse_qs(parsed.query)
                selected_bridge = self.bridge.for_workspace((query.get("workspace") or [""])[0])
                manifest, attachment_path = selected_bridge.attachments.download(attachment_match.group(1))
                program_id = program_id_of((query.get("programId") or [""])[0])
                if program_id != program_id_of(manifest.get("programId")):
                    raise BridgeFailure("附件项目上下文不一致")
                config = self.bridge.request_config(
                    {"programId": program_id},
                    self.allowed_origin() or "",
                    self.headers.get("token", "").strip(),
                )
                assert_runtime_project(config, program_id_of(manifest.get("programId")))
                self.attachment_response(manifest, attachment_path)
            except BridgeFailure as exc:
                self.json_response(404, {"error": str(exc)})
            return
        artifact_match = re.fullmatch(r"/v1/codex/artifacts/([a-f0-9]{40})", parsed.path)
        if artifact_match:
            if not self.allowed_origin():
                self.json_response(403, {"error": "origin not allowed"})
                return
            try:
                query = parse_qs(parsed.query)
                selected_bridge = self.bridge.for_workspace((query.get("workspace") or [""])[0])
                manifest, artifact_path = selected_bridge.artifacts.download(artifact_match.group(1))
                program_id = program_id_of((query.get("programId") or [""])[0])
                if program_id != program_id_of(manifest.get("programId")):
                    raise BridgeFailure("产物项目上下文不一致")
                config = self.bridge.request_config(
                    {"programId": program_id},
                    self.allowed_origin() or "",
                    self.headers.get("token", "").strip(),
                )
                assert_runtime_project(config, program_id_of(manifest.get("programId")))
                self.attachment_response(manifest, artifact_path)
            except BridgeFailure as exc:
                self.json_response(404, {"error": str(exc)})
            return
        if parsed.path == "/v1/codex/requirement-outline":
            if not self.allowed_origin():
                self.json_response(403, {"error": "origin not allowed"})
                return
            query = parse_qs(parsed.query)
            try:
                program_id = program_id_of((query.get("programId") or [""])[0])
                requirement_key = str((query.get("requirementKey") or [""])[0]).strip()
                if not requirement_key:
                    raise BridgeFailure("缺少需求标识")
                selected_bridge = self.bridge.for_workspace((query.get("workspace") or [""])[0])
                config = self.bridge.request_config(
                    {"programId": program_id}, self.allowed_origin() or "", self.headers.get("token", "").strip(),
                )
                self.json_response(200, selected_bridge.requirement_outline(program_id, requirement_key, config=config))
            except (BridgeFailure, planner.ToolFailure, ValueError) as exc:
                self.json_response(400, {"error": str(exc)})
            except Exception as exc:
                self.json_response(500, {"error": f"读取需求大纲失败：{exc}"})
            return
        if parsed.path == "/v1/codex/requirement-prototype":
            if not self.allowed_origin():
                self.json_response(403, {"error": "origin not allowed"})
                return
            query = parse_qs(parsed.query)
            try:
                program_id = program_id_of((query.get("programId") or [""])[0])
                requirement_key = str((query.get("requirementKey") or [""])[0]).strip()
                if not requirement_key:
                    raise BridgeFailure("缺少需求标识")
                selected_bridge = self.bridge.for_workspace((query.get("workspace") or [""])[0])
                config = self.bridge.request_config(
                    {"programId": program_id}, self.allowed_origin() or "", self.headers.get("token", "").strip(),
                )
                self.json_response(200, selected_bridge.requirement_prototype(program_id, requirement_key, config=config))
            except (BridgeFailure, planner.ToolFailure, ValueError) as exc:
                self.json_response(400, {"error": str(exc)})
            except Exception as exc:
                self.json_response(500, {"error": f"读取需求 HTML 原型失败：{exc}"})
            return
        if parsed.path == "/v1/codex/requirement-prototype/conversation":
            if not self.allowed_origin():
                self.json_response(403, {"error": "origin not allowed"})
                return
            query = parse_qs(parsed.query)
            try:
                program_id = program_id_of((query.get("programId") or [""])[0])
                requirement_key = str((query.get("requirementKey") or [""])[0]).strip()
                thread_id = str((query.get("threadId") or [""])[0]).strip()
                provider = ai_provider_of((query.get("provider") or ["codex"])[0])
                if not requirement_key:
                    raise BridgeFailure("缺少需求标识")
                selected_bridge = self.bridge.for_workspace((query.get("workspace") or [""])[0])
                config = self.bridge.request_config(
                    {"programId": program_id}, self.allowed_origin() or "", self.headers.get("token", "").strip(),
                )
                self.json_response(200, selected_bridge.requirement_prototype_conversation(
                    program_id, requirement_key, thread_id, provider, config=config,
                ))
            except (BridgeFailure, planner.ToolFailure, ValueError) as exc:
                self.json_response(400, {"error": str(exc)})
            except Exception as exc:
                self.json_response(500, {"error": f"读取原型编辑会话失败：{exc}"})
            return
        if parsed.path == "/v1/codex/requirement-testing":
            if not self.allowed_origin():
                self.json_response(403, {"error": "origin not allowed"})
                return
            query = parse_qs(parsed.query)
            try:
                program_id = program_id_of((query.get("programId") or [""])[0])
                requirement_key = str((query.get("requirementKey") or [""])[0]).strip()
                thread_id = str((query.get("threadId") or [""])[0]).strip()
                provider = ai_provider_of((query.get("provider") or ["codex"])[0])
                if not requirement_key:
                    raise BridgeFailure("缺少需求标识")
                selected_bridge = self.bridge.for_workspace((query.get("workspace") or [""])[0])
                config = self.bridge.request_config(
                    {"programId": program_id}, self.allowed_origin() or "", self.headers.get("token", "").strip(),
                )
                self.json_response(200, selected_bridge.requirement_testing(
                    program_id, requirement_key, thread_id, provider, config=config,
                ))
            except (BridgeFailure, planner.ToolFailure, ValueError) as exc:
                self.json_response(400, {"error": str(exc)})
            except Exception as exc:
                self.json_response(500, {"error": f"读取需求总体测试会话失败：{exc}"})
            return
        if parsed.path == "/v1/codex/task-testing-cases":
            if not self.allowed_origin():
                self.json_response(403, {"error": "origin not allowed"})
                return
            query = parse_qs(parsed.query)
            try:
                program_id = program_id_of((query.get("programId") or [""])[0])
                item_key = str((query.get("itemKey") or [""])[0]).strip()
                thread_id = str((query.get("threadId") or [""])[0]).strip()
                provider = ai_provider_of((query.get("provider") or ["codex"])[0])
                if not item_key:
                    raise BridgeFailure("缺少任务标识")
                selected_bridge = self.bridge.for_workspace((query.get("workspace") or [""])[0])
                config = self.bridge.request_config(
                    {"programId": program_id}, self.allowed_origin() or "", self.headers.get("token", "").strip(),
                )
                self.json_response(200, selected_bridge.task_testing_cases_conversation(
                    program_id, item_key, thread_id, provider, config=config,
                ))
            except (BridgeFailure, planner.ToolFailure, ValueError) as exc:
                self.json_response(400, {"error": str(exc)})
            except Exception as exc:
                self.json_response(500, {"error": f"读取任务测试用例会话失败：{exc}"})
            return
        if parsed.path in {"/v1/codex/document-set", "/v1/codex/document-file"}:
            if not self.allowed_origin():
                self.json_response(403, {"error": "origin not allowed"})
                return
            query = parse_qs(parsed.query)
            try:
                program_id = program_id_of((query.get("programId") or [""])[0])
                scope = str((query.get("scope") or [""])[0]).strip()
                key = str((query.get("key") or [""])[0]).strip()
                if not program_id or not scope or not key:
                    raise BridgeFailure("programId、scope 和 key 都是必填项")
                selected_bridge = self.bridge.for_workspace((query.get("workspace") or [""])[0])
                config = self.bridge.request_config(
                    {"programId": program_id},
                    self.allowed_origin() or "",
                    self.headers.get("token", "").strip(),
                )
                if parsed.path == "/v1/codex/document-set":
                    self.json_response(200, selected_bridge.document_set(program_id, scope, key, config=config))
                else:
                    self.json_response(200, selected_bridge.document_file(
                        program_id, scope, key, str((query.get("path") or [""])[0]), config=config,
                    ))
            except (BridgeFailure, planner.ToolFailure, ValueError) as exc:
                self.json_response(400, {"error": str(exc)})
            except Exception as exc:
                self.json_response(500, {"error": f"读取文档失败：{exc}"})
            return
        if parsed.path == "/v1/codex/requirement-document":
            if not self.allowed_origin():
                self.json_response(403, {"error": "origin not allowed"})
                return
            query = parse_qs(parsed.query)
            program_id = program_id_of((query.get("programId") or [""])[0])
            item_key = str((query.get("itemKey") or [""])[0]).strip()
            if not program_id or not item_key:
                self.json_response(400, {"error": "programId and itemKey are required"})
                return
            try:
                selected_bridge = self.bridge.for_workspace((query.get("workspace") or [""])[0])
                config = self.bridge.request_config(
                    {"programId": program_id},
                    self.allowed_origin() or "",
                    self.headers.get("token", "").strip(),
                )
                self.json_response(200, selected_bridge.requirement_document(program_id, item_key, config=config))
            except (BridgeFailure, planner.ToolFailure, ValueError) as exc:
                self.json_response(400, {"error": str(exc)})
            except Exception as exc:
                self.json_response(500, {"error": f"读取需求文档失败：{exc}"})
            return
        if parsed.path == "/v1/codex/prototype-directory":
            if not self.allowed_origin():
                self.json_response(403, {"error": "origin not allowed"})
                return
            query = parse_qs(parsed.query)
            program_id = program_id_of((query.get("programId") or [""])[0])
            item_key = str((query.get("itemKey") or [""])[0]).strip()
            if not program_id or not item_key:
                self.json_response(400, {"error": "programId and itemKey are required"})
                return
            try:
                selected_bridge = self.bridge.for_workspace((query.get("workspace") or [""])[0])
                config = self.bridge.request_config(
                    {"programId": program_id},
                    self.allowed_origin() or "",
                    self.headers.get("token", "").strip(),
                )
                self.json_response(200, selected_bridge.prototype_directory(program_id, item_key, config=config))
            except (BridgeFailure, planner.ToolFailure, ValueError) as exc:
                self.json_response(400, {"error": str(exc)})
            except Exception as exc:
                self.json_response(500, {"error": f"读取原型图目录失败：{exc}"})
            return
        if parsed.path == "/v1/codex/conversation":
            if not self.allowed_origin():
                self.json_response(403, {"error": "origin not allowed"})
                return
            query = parse_qs(parsed.query)
            program_id = program_id_of((query.get("programId") or [""])[0])
            item_key = str((query.get("itemKey") or [""])[0]).strip()
            thread_id = str((query.get("threadId") or [""])[0]).strip()
            provider = ai_provider_of((query.get("provider") or ["codex"])[0])
            if not program_id or not item_key:
                self.json_response(400, {"error": "programId and itemKey are required"})
                return
            try:
                selected_bridge = self.bridge.for_workspace((query.get("workspace") or [""])[0])
                config = self.bridge.request_config(
                    {"programId": program_id},
                    self.allowed_origin() or "",
                    self.headers.get("token", "").strip(),
                )
                self.json_response(200, selected_bridge.conversation(program_id, item_key, thread_id, config=config, provider=provider))
            except (BridgeFailure, planner.ToolFailure, ValueError) as exc:
                self.json_response(400, {"error": str(exc)})
            except Exception as exc:
                self.json_response(500, {"error": f"读取 Codex 会话失败：{exc}"})
            return
        if parsed.path == "/v1/codex/environment-setup":
            if not self.allowed_origin():
                self.json_response(403, {"error": "origin not allowed"})
                return
            query = parse_qs(parsed.query)
            try:
                thread_id = str((query.get("threadId") or [""])[0]).strip()
                provider = ai_provider_of((query.get("provider") or ["codex"])[0])
                use_git = str((query.get("useGit") or [""])[0]).strip().lower() == "true"
                environments_raw = str((query.get("environments") or ["[]"])[0])
                try:
                    environments = environment_selection_of(json.loads(environments_raw))
                except (json.JSONDecodeError, TypeError):
                    raise BridgeFailure("预设环境参数无效")
                config = self.bridge.global_environment_config({}, self.headers.get("token", "").strip())
                self.json_response(200, self.bridge.environment_setup(
                    GLOBAL_ENVIRONMENT_SETUP_PROGRAM_ID,
                    thread_id,
                    config=config,
                    provider=provider,
                    use_git=use_git,
                    environments=environments,
                ))
            except (BridgeFailure, planner.ToolFailure, ValueError) as exc:
                self.json_response(400, {"error": str(exc)})
            except Exception as exc:
                self.json_response(500, {"error": f"读取预设环境会话失败：{exc}"})
            return
        if parsed.path == "/v1/codex/planning":
            if not self.allowed_origin():
                self.json_response(403, {"error": "origin not allowed"})
                return
            query = parse_qs(parsed.query)
            program_id = program_id_of((query.get("programId") or [""])[0])
            thread_id = str((query.get("threadId") or [""])[0]).strip()
            requirement_key = str((query.get("requirementKey") or [""])[0]).strip()
            provider = ai_provider_of((query.get("provider") or ["codex"])[0])
            if not program_id:
                self.json_response(400, {"error": "programId is required"})
                return
            try:
                selected_bridge = self.bridge.for_workspace((query.get("workspace") or [""])[0])
                config = self.bridge.request_config(
                    {"programId": program_id},
                    self.allowed_origin() or "",
                    self.headers.get("token", "").strip(),
                )
                self.json_response(200, selected_bridge.planning(program_id, thread_id, config=config, requirement_key=requirement_key, provider=provider))
            except (BridgeFailure, planner.ToolFailure, ValueError) as exc:
                self.json_response(400, {"error": str(exc)})
            except Exception as exc:
                self.json_response(500, {"error": f"读取拆解会话失败：{exc}"})
            return
        else:
            self.json_response(404, {"error": "not found"})
            return

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/v1/session/heartbeat":
            if not self.allowed_origin():
                self.json_response(403, {"error": "origin not allowed"})
                return
            self.handle_heartbeat()
            return
        if path in {"/v1/plugin/update/install", "/v1/plugin/update/restart"}:
            if not self.allowed_origin():
                self.json_response(403, {"error": "origin not allowed"})
                return
            try:
                token = self.headers.get("token", "").strip()
                if not token or not planner.token_subject(token):
                    raise BridgeFailure("当前用户凭证无效")
                if self.headers.get_content_type() != "application/json":
                    raise BridgeFailure("application/json required")
                length = int(self.headers.get("Content-Length") or 0)
                if length < 0 or length > 8 * 1024:
                    raise BridgeFailure("请求体大小无效")
                payload = json.loads(self.rfile.read(length)) if length else {}
                if not isinstance(payload, dict):
                    raise BridgeFailure("请求体必须是 JSON 对象")
                if path.endswith("/install"):
                    job = PLUGIN_UPDATES.start(str(payload.get("expectedVersion") or "").strip())
                    job["activeRuns"] = self.bridge.active_run_count()
                    complete_plugin_update_in_background(str(job.get("jobId") or ""), self.bridge)
                    self.json_response(202, job)
                    return
                active_runs = self.bridge.active_run_count()
                if active_runs:
                    self.json_response(409, {"error": f"当前还有 {active_runs} 个执行会话运行中，请等待完成后再重启"})
                    return
                job = PLUGIN_UPDATES.mark_restarting(str(payload.get("jobId") or "").strip())
                job["activeRuns"] = 0
                self.json_response(202, job)
                schedule_bridge_restart()
                return
            except (BridgeFailure, UpdateFailure, json.JSONDecodeError, ValueError) as exc:
                self.json_response(400, {"error": str(exc)})
            except Exception as exc:
                self.json_response(500, {"error": f"更新插件失败：{exc}"})
            return
        if path not in {
            "/v1/codex/execute",
            "/v1/codex/task-testing-cases",
            "/v1/codex/task-testing-cases/stop",
            "/v1/codex/execute-batch",
            "/v1/codex/execute-sequence",
            "/v1/codex/conversation",
            "/v1/codex/planning",
            "/v1/codex/planning/stop",
            "/v1/codex/environment-setup",
            "/v1/codex/environment-setup/stop",
            "/v1/codex/requirement-prototype/generate",
            "/v1/codex/requirement-prototype/conversation",
            "/v1/codex/requirement-testing",
            "/v1/codex/requirement-testing/stop",
            "/v1/codex/attachments",
            "/v1/codex/prototype-directory/open",
            "/v1/codex/git/branch",
            "/v1/codex/git/init",
            "/v1/codex/git/submodules",
            "/v1/codex/git/prepare",
            "/v1/codex/git/push",
            "/v1/codex/cloud-sync",
            "/v1/codex/requirement-document",
            "/v1/codex/requirement-outline",
            "/v1/codex/document-file",
            "/v1/codex/document-upload",
            "/v1/codex/document-attachment",
            "/v1/codex/stop",
            "/v1/codex/stop-all",
        }:
            self.json_response(404, {"error": "not found"})
            return
        if not self.allowed_origin():
            self.json_response(403, {"error": "origin not allowed"})
            return
        try:
            if path == "/v1/codex/attachments":
                self.handle_attachment_upload()
                return
            if path == "/v1/codex/document-upload":
                self.handle_document_upload()
                return
            if self.headers.get_content_type() != "application/json":
                self.json_response(415, {"error": "application/json required"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            # 需求文档与需求大纲编辑提交的是整篇 Markdown，比控制类请求大一个量级，单独放宽。
            limit = (
                MAX_REQUIREMENT_DOCUMENT_BYTES + 4 * 1024
                if path == "/v1/codex/requirement-document"
                else MAX_EDITABLE_OUTLINE_BYTES + 4 * 1024
                if path == "/v1/codex/requirement-outline"
                else MAX_DOCUMENT_SET_FILE_BYTES + 4 * 1024
                if path == "/v1/codex/document-file"
                else 64 * 1024
            )
            if length <= 0 or length > limit:
                raise BridgeFailure("请求体大小无效")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise BridgeFailure("请求体必须是 JSON 对象")
            if path == "/v1/codex/git/submodules":
                # 目录早就是仓库、只是子模块没拉下来：单独补这一步，不碰主仓库的分支和改动。
                self.bridge.request_config(
                    payload,
                    self.allowed_origin() or "",
                    self.headers.get("token", "").strip(),
                )
                submodule_workspace = self.bridge.for_workspace(payload.get("workspace")).workspace
                require_git_workspace(submodule_workspace)
                self.json_response(200, {
                    "workspace": str(submodule_workspace),
                    **git_initialize_submodules(submodule_workspace),
                })
                return
            if path == "/v1/codex/git/init":
                # 初始化时目录还不是仓库、甚至可能还没建出来，不能先走 for_workspace 的存在性校验。
                self.bridge.request_config(
                    payload,
                    self.allowed_origin() or "",
                    self.headers.get("token", "").strip(),
                )
                self.json_response(200, git_initialize_workspace(
                    git_initializable_workspace_of(payload.get("workspace")),
                    str(payload.get("repositoryUrl") or ""),
                    str(payload.get("remoteName") or "origin").strip() or "origin",
                    str(payload.get("baseBranch") or "").strip(),
                ))
                return
            if path in {"/v1/codex/environment-setup", "/v1/codex/environment-setup/stop"}:
                config = self.bridge.global_environment_config(payload, self.headers.get("token", "").strip())
                selected_bridge = self.bridge
            else:
                config = self.bridge.request_config(
                    payload,
                    self.allowed_origin() or "",
                    self.headers.get("token", "").strip(),
                )
                selected_bridge = self.bridge.for_workspace(payload.get("workspace"))
            if path == "/v1/codex/execute":
                self.json_response(202, selected_bridge.execute(payload, config=config))
            elif path == "/v1/codex/task-testing-cases":
                self.json_response(202, selected_bridge.generate_task_testing_cases(payload, config))
            elif path == "/v1/codex/task-testing-cases/stop":
                self.json_response(202, selected_bridge.stop_task_testing_cases(payload, config))
            elif path == "/v1/codex/execute-batch":
                self.json_response(202, selected_bridge.execute_batch(payload, config=config))
            elif path == "/v1/codex/execute-sequence":
                self.json_response(202, selected_bridge.execute_sequence(payload, config=config))
            elif path == "/v1/codex/conversation":
                self.json_response(202, selected_bridge.send_conversation(payload, config=config))
            elif path == "/v1/codex/planning":
                self.json_response(202, selected_bridge.send_planning(payload, config))
            elif path == "/v1/codex/planning/stop":
                self.json_response(202, selected_bridge.stop_planning(payload, config))
            elif path == "/v1/codex/environment-setup":
                self.json_response(202, self.bridge.send_environment_setup(payload, config))
            elif path == "/v1/codex/environment-setup/stop":
                self.json_response(202, self.bridge.stop_environment_setup(payload, config))
            elif path == "/v1/codex/requirement-prototype/generate":
                self.json_response(202, selected_bridge.generate_requirement_prototype(payload, config))
            elif path == "/v1/codex/requirement-prototype/conversation":
                self.json_response(202, selected_bridge.send_requirement_prototype_message(payload, config))
            elif path == "/v1/codex/requirement-testing":
                self.json_response(202, selected_bridge.send_requirement_testing(payload, config))
            elif path == "/v1/codex/requirement-testing/stop":
                self.json_response(202, selected_bridge.stop_requirement_testing(payload, config))
            elif path == "/v1/codex/requirement-document":
                item_key = str(payload.get("itemKey") or "").strip()
                if not item_key:
                    raise BridgeFailure("缺少任务标识")
                content = payload.get("content")
                if not isinstance(content, str):
                    raise BridgeFailure("需求文档正文必须是字符串")
                self.json_response(200, selected_bridge.save_requirement_document(
                    program_id_of(payload.get("programId")), item_key, content, config=config,
                ))
            elif path == "/v1/codex/requirement-outline":
                requirement_key = str(payload.get("requirementKey") or "").strip()
                if not requirement_key:
                    raise BridgeFailure("缺少需求标识")
                markdown = payload.get("markdown")
                if not isinstance(markdown, str):
                    raise BridgeFailure("需求大纲正文必须是字符串")
                self.json_response(200, selected_bridge.save_requirement_outline(
                    program_id_of(payload.get("programId")), requirement_key, markdown, config=config,
                ))
            elif path == "/v1/codex/document-file":
                self.json_response(200, selected_bridge.save_document_file(
                    program_id_of(payload.get("programId")),
                    str(payload.get("scope") or "").strip(),
                    str(payload.get("key") or "").strip(),
                    str(payload.get("path") or "").strip(),
                    payload.get("content"),
                    config=config,
                ))
            elif path == "/v1/codex/document-attachment":
                self.json_response(200, selected_bridge.document_attachment(
                    program_id_of(payload.get("programId")),
                    str(payload.get("scope") or "").strip(),
                    str(payload.get("key") or "").strip(),
                    str(payload.get("path") or "").strip(),
                    config=config,
                ))
            elif path == "/v1/codex/cloud-sync":
                self.json_response(200, selected_bridge.sync_cloud_workspace(
                    program_id_of(payload.get("programId")), config,
                ))
            elif path == "/v1/codex/git/push":
                self.json_response(200, selected_bridge.push_requirement_branch(payload, config))
            elif path == "/v1/codex/git/branch":
                self.json_response(200, git_create_branch_targets(
                    selected_bridge.workspace,
                    str(payload.get("baseBranch") or "").strip(),
                    str(payload.get("branch") or "").strip(),
                    # 建分支不做「没传就全建」：勾选哪些子项目由创建表单说了算。
                    git_subproject_targets_of(selected_bridge.workspace, payload.get("targets") or []),
                    bool(payload.get("skipRoot")),
                ))
            elif path == "/v1/codex/git/prepare":
                self.json_response(200, selected_bridge.prepare_requirement_git_branch(payload))
            elif path == "/v1/codex/prototype-directory/open":
                item_key = str(payload.get("itemKey") or "").strip()
                if not item_key:
                    raise BridgeFailure("缺少任务标识")
                self.json_response(202, selected_bridge.open_prototype_directory(program_id_of(payload.get("programId")), item_key, config=config))
            elif path == "/v1/codex/stop-all":
                self.json_response(202, selected_bridge.stop_all_executions(payload, config=config))
            else:
                self.json_response(202, selected_bridge.stop_conversation(payload, config=config))
        except (BridgeFailure, planner.ToolFailure, json.JSONDecodeError, ValueError) as exc:
            self.json_response(400, {"error": str(exc)})
        except Exception as exc:
            self.json_response(500, {"error": f"启动 AI 工具失败：{exc}"})

    def handle_heartbeat(self) -> None:
        """控制台每分钟送一次当前账号的 token 和 user_id，插件存下来当作凭证来源。

        配置文件只维护接口地址；普通命令行会话没有面板注入的环境变量，全靠这里
        存下来的凭证。心跳不打任何面板接口，凭证真伪由后续真实请求判定。
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length < 0 or length > 8 * 1024:
                raise BridgeFailure("请求体大小无效")
            payload = json.loads(self.rfile.read(length)) if length else {}
            if not isinstance(payload, dict):
                raise BridgeFailure("请求体必须是 JSON 对象")
            token = self.headers.get("token", "").strip() or str(payload.get("token") or "").strip()
            if not token:
                raise BridgeFailure("当前用户凭证为空")
            user_id = str(payload.get("userId") or "").strip()
            subject = planner.token_subject(token)
            if not subject:
                raise BridgeFailure("凭证不是任务面板登录凭证")
            if user_id and user_id != subject:
                raise BridgeFailure("凭证与用户标识不一致")
            planner.save_credential(token, user_id or subject)
            self.json_response(200, {"stored": True, "userId": user_id or subject})
        except (BridgeFailure, planner.ToolFailure, json.JSONDecodeError, ValueError) as exc:
            self.json_response(400, {"error": str(exc)})
        except Exception as exc:
            self.json_response(500, {"error": f"保存任务面板凭证失败：{exc}"})

    def handle_document_upload(self) -> None:
        """把面板选的本地文件（或粘贴正文生成的文件）写进某个栏目的目录。"""
        content_length = int(self.headers.get("Content-Length") or 0)
        if content_length <= 0 or content_length > MAX_DOCUMENT_UPLOAD_BYTES:
            raise BridgeFailure("文档请求体大小无效")
        if self.headers.get_content_type() != "multipart/form-data":
            raise BridgeFailure("文档必须使用 multipart/form-data 上传")
        fields, uploads = self.read_multipart(content_length)
        program_id = program_id_of(fields.get("programId"))
        selected_bridge = self.bridge.for_workspace(fields.get("workspace"))
        config = self.bridge.request_config(
            {"programId": program_id},
            self.allowed_origin() or "",
            self.headers.get("token", "").strip(),
        )
        self.json_response(
            201,
            selected_bridge.upload_documents(
                program_id,
                fields.get("scope", ""),
                fields.get("key", ""),
                uploads,
                config_biz_line(config),
                config,
            ),
        )

    def read_multipart(self, content_length: int) -> tuple[dict[str, str], list[dict[str, Any]]]:
        """Split one multipart body into plain fields and uploaded files."""
        content_type = self.headers.get("Content-Type", "")
        raw = self.rfile.read(content_length)
        message = BytesParser(policy=policy.default).parsebytes(
            f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("ascii") + raw
        )
        if not message.is_multipart():
            raise BridgeFailure("请求体不是有效的 multipart/form-data")
        fields: dict[str, str] = {}
        uploads: list[dict[str, Any]] = []
        for part in message.iter_parts():
            name = str(part.get_param("name", header="content-disposition") or "")
            filename = str(part.get_filename() or "")
            data = part.get_payload(decode=True) or b""
            if not filename:
                fields[name] = data.decode(part.get_content_charset() or "utf-8", errors="replace").strip()
                continue
            uploads.append({"name": filename, "contentType": part.get_content_type(), "data": data})
        return fields, uploads

    def handle_attachment_upload(self) -> None:
        content_length = int(self.headers.get("Content-Length") or 0)
        if content_length <= 0 or content_length > MAX_CONVERSATION_UPLOAD_BYTES:
            raise BridgeFailure("附件请求体大小无效")
        if self.headers.get_content_type() != "multipart/form-data":
            raise BridgeFailure("附件必须使用 multipart/form-data 上传")
        fields, uploads = self.read_multipart(content_length)
        program_id = program_id_of(fields.get("programId"))
        selected_bridge = self.bridge.for_workspace(fields.get("workspace"))
        config = self.bridge.request_config(
            {"programId": program_id},
            self.allowed_origin() or "",
            self.headers.get("token", "").strip(),
        )
        self.json_response(
            201,
            selected_bridge.upload_conversation_attachments(
                config_biz_line(config),
                program_id,
                fields.get("itemKey", ""),
                uploads,
                config,
            ),
        )

    def log_message(self, format: str, *args: Any) -> None:
        return


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    # 进程级工作目录是可选的：真正干活的目录由每个请求带的 workspace 决定（见 for_workspace）。
    # 不给就落到一个空的中性占位目录，绝不拿安装目录或启动目录冒充某个项目的仓库。
    parser.add_argument("--workspace", default="")
    parser.add_argument("--allow-origin", action="append", default=[])
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "::1", "localhost"}:
        raise SystemExit("HTTP bridge must listen on loopback")
    if args.workspace:
        workspace = Path(args.workspace).resolve()
        if not workspace.is_dir():
            raise SystemExit(f"workspace does not exist: {workspace}")
    else:
        workspace = placeholder_workspace()
    origins = set(args.allow_origin or ["*"])
    httpd = create_http_server(args.host, args.port, workspace, origins)
    threading.Thread(target=httpd.bridge.reconcile_forever, daemon=True).start()  # type: ignore[attr-defined]
    try:
        httpd.serve_forever()
    finally:
        # 只读执行器是常驻子进程，退出时收干净，别留孤儿。
        THREAD_READERS.shutdown()


if __name__ == "__main__":
    main()
