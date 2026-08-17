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
import queue
import re
import secrets
import shutil
import ssl
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import server as planner


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
SESSION_STATUS = {"completed": "completed", "failed": "blocked", "interrupted": "blocked"}
TERMINAL_TURN_STATUSES = set(SESSION_STATUS)


def default_runtime_dir() -> Path:
    if sys.platform == "win32" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "delivery-task-planner"
    return Path.home() / ".local" / "state" / "delivery-task-planner"


RUNTIME_DIR = default_runtime_dir()
TLS_DIRECTORY = RUNTIME_DIR / "tls"
DEFAULT_TLS_CERTIFICATE = TLS_DIRECTORY / "bridge-cert.pem"
DEFAULT_TLS_KEY = TLS_DIRECTORY / "bridge-key.pem"
PENDING_SESSION_SYNCS_PATH = RUNTIME_DIR / "pending-session-syncs.json"
# Claude 是 print 模式的一次性子进程，没有常驻线程服务可读；会话记录只能自己落盘。
CLAUDE_TRANSCRIPTS_DIR = RUNTIME_DIR / "claude-transcripts"
MAX_CLAUDE_TRANSCRIPT_TURNS = 60
MAX_CONVERSATIONS_PER_TASK = 12
MAX_CONVERSATION_ATTACHMENTS = 5
MAX_CONVERSATION_ATTACHMENT_BYTES = 10 * 1024 * 1024
MAX_CONVERSATION_UPLOAD_BYTES = MAX_CONVERSATION_ATTACHMENTS * MAX_CONVERSATION_ATTACHMENT_BYTES + 128 * 1024
MAX_REQUIREMENT_DOCUMENT_BYTES = 2 * 1024 * 1024
MAX_REQUIREMENT_PROTOTYPE_FILES = 30
MAX_REQUIREMENT_PROTOTYPE_FILE_BYTES = 2 * 1024 * 1024
MAX_REQUIREMENT_PROTOTYPE_TOTAL_BYTES = 8 * 1024 * 1024
MAX_WORKSPACE_ARTIFACT_BYTES = 50 * 1024 * 1024
PLANNING_ITEM_KEY = "__project_planning__"
REQUIREMENT_TESTING_ITEM_KEY = "__requirement_testing__"
# 任务生命周期的四个技能都在本插件 skills/ 下；执行时按阶段点名，别让执行器自己猜。
PLANNING_SKILL = "delivery-task-planner"
PHASE_SKILLS = {
    "requirement": "delivery-requirement-grooming",
    "development": "delivery-action-execution",
    "testing": "delivery-testing-report",
}
MAX_PLANNING_CONVERSATIONS = 12
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


class BridgeFailure(Exception):
    pass


def create_https_server(
    host: str,
    port: int,
    workspace: Path,
    allowed_origins: set[str],
    certificate: Path,
    private_key: Path,
) -> ThreadingHTTPServer:
    """Create the loopback bridge listener with TLS required for browser calls."""
    httpd = ThreadingHTTPServer((host, port), BridgeHandler)
    httpd.bridge = ExecutionBridge(workspace)  # type: ignore[attr-defined]
    httpd.allowed_origins = allowed_origins  # type: ignore[attr-defined]
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(certfile=str(certificate), keyfile=str(private_key))
    httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
    return httpd


def ai_provider_of(value: Any) -> str:
    provider = str((value or {}).get("provider") or "codex").strip().lower() if isinstance(value, dict) else str(value or "codex").strip().lower()
    if provider not in AI_PROVIDERS:
        raise BridgeFailure("AI 工具必须是 codex 或 claude")
    return provider


def provider_label(provider: str) -> str:
    return "Claude" if provider == "claude" else "Codex"


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


def progress_event_of(message: dict[str, Any]) -> tuple[str, str, str, str] | None:
    method = str(message.get("method") or "")
    params = message.get("params") or {}
    if method == "turn/started":
        return "status", "任务已开始", "Codex 正在分析任务与项目上下文。", "running"
    if method == "turn/completed":
        status = str((params.get("turn") or {}).get("status") or "completed")
        return "status", "正在同步执行结果", f"Codex 回合状态：{status}", "running"
    if method not in {"item/started", "item/completed"}:
        return None
    item = params.get("item") or {}
    item_type = str(item.get("type") or "")
    completed = method == "item/completed"
    status = "success" if completed else "running"
    if item_type == "agentMessage" and completed:
        text = str(item.get("text") or item.get("content") or "").strip()
        return ("message", "Codex 进度", text, status) if text else None
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


def build_task_prompt(payload: dict[str, Any], workspace: Path | None = None) -> str:
    """`workspace` 是项目管理里绑定的工作目录，也是本轮 cwd；四个阶段都要靠它去读代码和项目技能。"""
    task = payload["task"]
    dependencies = task.get("dependsOnItemKeys") or []
    phase = str(task.get("phase") or "requirement")
    phase_name = {"requirement": "梳理需求", "development": "动作执行", "testing": "成品测试"}.get(phase, phase)
    document_path = document_path_of(task)
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
            f"验证命令沿用当前项目开发技能里的约定；所有测试资产必须写入 `{test_artifact_directory}/`，"
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
        f"需求文档路径: {document_path}（本任务自己的文档，默认加载：开始前先完整读一遍）",
        phase_instruction,
        *prototype_instruction,
        f"阶段: {task.get('stageKey') or '未指定'}",
        f"模块: {task.get('moduleKey') or '未指定'}",
        f"前置任务: {', '.join(dependencies) if dependencies else '无'}",
        "完成后说明修改内容和验证结果；无法完成时明确说明阻塞原因。",
        "如果生成了用户需要查看或下载的文件、文档或图片，请在最终回复中用 Markdown 链接列出其工作区相对路径。",
    ]
    lines.extend(sibling_document_lines(payload.get("requirementDocuments")))
    execution_constraints = str(payload.get("executionConstraints") or "").strip()
    if execution_constraints:
        lines.extend(["", "本次队列的前置任务约束条件说明:", execution_constraints])
    follow_up = str(payload.get("followUp") or "").strip()
    if follow_up:
        lines.append("本上下文标记闭合之后的内容，是用户本轮追加的原话。")
    # 面板组装的这一大段只给执行器看；聊天记录里留一句人话，外加用户自己写的追加要求。
    spoken = f"执行「{phase_name}」阶段：{task['title']}"
    return wrap_bridge_context(lines, f"{spoken}\n\n{follow_up}" if follow_up else spoken)


def build_task_testing_cases_prompt(
    program_id: int, task: dict[str, Any], context: dict[str, Any], message: str, workspace: Path | None = None,
) -> str:
    """Build a design-only prompt that remains safe while development is in progress."""
    item_key = str(task.get("itemKey") or "").strip()
    if not item_key:
        raise BridgeFailure("任务测试用例缺少任务标识")
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
            f"测试用例资产目录: doc/test/{item_key}/；必须写入测试用例.md，按需写入测试计划.md。",
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
) -> str:
    """Start an independent Codex thread with enough task context to be useful."""
    dependencies = task.get("dependsOnItemKeys") or []
    phase = str(task.get("phase") or "requirement")
    document_path = document_path_of(task)
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
            f"需求文档路径: {document_path}（本任务自己的文档，默认加载）。开始前请先读取此文件；梳理需求阶段应更新此文件。",
            f"阶段: {task.get('stageKey') or '未指定'}",
            f"模块: {task.get('moduleKey') or '未指定'}",
            f"前置任务: {', '.join(dependencies) if dependencies else '无'}",
            *sibling_document_lines(requirement_documents),
            "如果生成了用户需要查看或下载的文件、文档或图片，请在最终回复中用 Markdown 链接列出其工作区相对路径。",
            "本上下文标记闭合之后的内容，是用户本轮输入的原文。",
        ],
        message,
    )


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
    existing_lines = [
        f"- {item.get('itemKey')}: {item.get('title') or item.get('itemKey')}"
        for item in (context.get("items") or [])[:100]
    ]
    requirement = requirement or {}
    requirement_key = str(requirement.get("requirementKey") or "")
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
            "把上一轮预览过的方案（含用户后续提出的修改）用 create_task_board_tasks 一次性提交。",
            "必须通过插件工具写入，不要用 shell、HTTP 请求、或手工修改文件来创建任务面板数据。",
            "可用工具：get_task_board_context、create_task_board_stage、create_task_board_module、create_task_board_tasks。当前项目已确定，所有工具的 program_id 一律传下面给出的项目表数值主键，不要传项目名称或项目编码。",
            "任务描述应包含目标、范围和验收标准；依赖仅表达真正的前置关系。",
            "每个任务必须传 benefit_tags：用 1-3 个不超过 32 字的简短标签描述该任务完成后带来的收益或作用，不能留空，也不要把任务标题重复写成标签。",
            "任务负责人由写入工具从下面这条需求的主负责人自动继承：任务模型只能保存一位负责人，因此会使用需求的第一位主负责人；不要在任务数组中自行改写负责人。",
            "调用 create_task_board_tasks 时必须原样传入下面给出的 requirement_key 和 phase，让新任务挂回本需求并落在指定的起始阶段。",
            "用户已选择里程碑或模块时，将相同的 stage_key/module_key 传给 create_task_board_tasks 并不要自行改写；未选择时根据当前项目已有选项为每项任务分配归属。",
            "本需求已有任务列表在下方给出：只补齐缺少的部分，不要重建已经存在的任务；若本轮无需新建任务，直接说明原因。",
            "不重复创建与已有任务语义相同的任务。完成后用简洁中文总结实际创建的里程碑、模块和任务。",
        ]
        if write_allowed
        else [
            f"这是交付任务面板的需求梳理会话，请遵循 {PLANNING_SKILL} 技能。本轮只做梳理和预览，禁止写入任何任务面板数据。",
            "禁止调用 create_task_board_tasks、create_task_board_stage、create_task_board_module，也不要借 shell、HTTP 请求或手工改文件绕过任务面板写入限制；未确认前这些写入调用会被工具直接拒绝。",
            "本轮的限制只针对写入：不要修改任何文件，也不要写任务面板数据。已授予项目工作目录及需求指定关联目录的只读勘察权限；可使用终端的只读命令和当前会话可用的读取工具列目录、搜索并读取代码、配置、技能和文档。某个可选读取工具不可用时，改用其他可用的只读工具继续勘察，不要因此停止。",
            "拆解前必须先勘察下方给出的项目工作目录：加载该目录下项目自己的开发技能（如 backend-development、web-development），读相关目录和现有实现，据此判断需求真正的落点。get_task_board_context 只给出面板侧上下文，不包含工程现状，不能拿它替代看代码。",
            "任务要落到勘察出的真实模块、目录或接口上，不要只按业务名词泛化出通用分层；工作区里找不到需求所指的模块时，先向用户说明并确认工作目录或范围，不要硬拆。",
            "请与用户对话把需求问清楚，然后输出一份可评审的拆解预览：先用 Markdown 表格列出「序号 / 任务标题 / 收益标签 / 负责人 / 里程碑 / 模块 / 类型 / 前置依赖」，每项给 1-3 个简短收益或作用标签；负责人统一展示为该需求的第一位主负责人（未指定则标为未指派）；再在表格下方逐条补充目标、范围和验收标准。",
            "里程碑、模块、类型的取值只能来自下方给出的现有选项；预览里也要说明哪些是新建、哪些复用已有任务。",
            "本需求已有任务列表在下方给出：预览里只列本轮打算新增的任务，不要重复已经存在的任务。",
            "回复结尾提示用户：确认无误后点击输入框旁的「确认并写入」按钮，需要调整就直接回复修改意见，本轮继续讨论不会写入任何数据。",
        ]
    )
    prototype_enabled = bool(requirement.get("generatePrototype"))
    prototype_lines = (
        [
            "本需求已启用“拆解后生成原型图”。预览时必须在任务表的最后列出一条“生成需求原型图”任务，"
            "并说明它依赖本轮其余任务；确认写入时，调用 create_task_board_tasks 必须传 generate_prototype: true。"
            "工具会自动创建并标识这条末尾任务，任务执行时将把图片保存到自身文档目录的 prototype/ 中。",
        ]
        if prototype_enabled else []
    )
    instruction = [
        *mode_lines,
        *prototype_lines,
        "",
        f"项目 program_id: {program_id}",
        f"项目名称（仅供理解，不要作为参数）: {context.get('program', {}).get('name') or program_id}",
        workspace_instruction(workspace),
        f"需求键 requirement_key: {requirement_key or '未指定'}",
        f"任务起始阶段 phase: {requirement.get('startPhase') or 'requirement'}",
        f"拆解后生成原型图: {'是' if prototype_enabled else '否'}",
        f"需求名称: {requirement.get('name') or '未命名'}",
        f"主负责人: {requirement.get('owners') or '未指定'}",
        f"辅助人: {requirement.get('assistants') or '未指定'}",
        "需求详细信息:",
        str(requirement.get("detail") or "（未填写）"),
        "",
        f"已选里程碑: {selected_stage or '未选择'}",
        f"已选模块: {selected_module or '未选择'}",
        f"任务类型偏好: {selected_kind or '由你判断'}",
        "现有里程碑:", *(stage_lines or ["- 无"]),
        "现有模块:", *(module_lines or ["- 无"]),
        "本需求已建任务:", *(requirement_item_lines or ["- 无"]),
        "项目全部任务（用于去重与依赖）:", *(existing_lines or ["- 无"]),
        "",
        "本上下文标记闭合之后的内容，是用户本轮输入的原文。",
    ]
    return wrap_bridge_context(instruction, message)


def build_requirement_testing_prompt(
    program_id: int,
    context: dict[str, Any],
    requirement: dict[str, Any],
    message: str,
    workspace: Path | None = None,
    test_case_only: bool = False,
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
            "这是交付任务面板的一次需求总体测试。遵循 delivery-requirement-testing 技能执行真实测试，不要调用任务拆解工具或修改业务实现。",
            "先读取已有 doc/test/<需求键>/测试用例.md 并按其中用例真实验证；没有明确执行和证据，不得写通过。",
        ]
    )
    final_instruction = (
        "最终必须把测试用例写入 doc/test/<需求键>/测试用例.md；按需写入测试计划.md，最终回复第一行必须为“测试用例已生成”。"
        if test_case_only else
        "最终必须把完整报告写入 doc/test/<需求键>/测试报告.md，并且最终回复第一行给出“验收判定：通过 / 不通过 / 受阻”。"
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
            "关联任务清单（先按需读对应文档、产物和代码；清单不是完整上下文）：",
            *(item_lines or ["- 该需求目前没有关联任务；先说明总体测试范围和受阻项，不要假装已覆盖任务链路。"]),
            final_instruction,
            "本上下文标记闭合之后的内容，是用户本轮补充的测试要求、环境或数据说明。",
        ],
        message,
    )


def validate_planning_payload(value: Any) -> tuple[int, str, str, bool, str, str, str, str, str, bool, dict[str, Any], list[str], bool]:
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
        "generatePrototype": bool(value.get("requirementGeneratePrototype")),
    }


def requirement_prototype_directory_of(requirement_key: str) -> Path:
    """Return the only workspace-relative directory a requirement prototype may use."""
    value = str(requirement_key or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
        raise BridgeFailure("需求原型标识无效")
    return Path("doc") / "requirements" / value / "prototype"


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
        files.append({"path": relative.as_posix(), "name": display_name, "html": html})
        if len(files) >= MAX_REQUIREMENT_PROTOTYPE_FILES:
            break
    return relative_directory.as_posix(), files


def build_requirement_prototype_prompt(
    program_id: int,
    requirement: dict[str, Any],
    message: str,
    workspace: Path,
    editing: bool = False,
) -> str:
    requirement_key = str(requirement.get("requirementKey") or "").strip()
    prototype_path = requirement_prototype_directory_of(requirement_key).as_posix()
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
    execution_constraints = str(value.get("executionConstraints") or "").strip()
    if len(execution_constraints) > 32 * 1024:
        raise BridgeFailure("任务约束条件说明不能超过 32KB")
    normalized["executionConstraints"] = execution_constraints
    attachments = value.get("followUpAttachments") or []
    if not isinstance(attachments, list) or len(attachments) > MAX_CONVERSATION_ATTACHMENTS:
        raise BridgeFailure("附件数量无效")
    normalized["followUpAttachments"] = attachments
    return normalized


def validate_conversation_payload(value: Any) -> tuple[int, str, str, str, bool, list[str], str, str, bool]:
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
            if previous is None or str(entry.get("updatedAt") or "") >= str(previous.get("updatedAt") or ""):
                entries[thread_id] = dict(entry)
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
    if runtime_program_id and runtime_program_id != program_id:
        raise BridgeFailure("当前请求项目与任务面板入口项目不一致")


def codex_environment(config: dict[str, Any], program_id: int, write_allowed: bool = True) -> dict[str, str]:
    assert_runtime_project(config, program_id)
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


class AppServerClient:
    def __init__(self, workspace: Path, event_callback: Any = None, environment: dict[str, str] | None = None):
        self.workspace = workspace
        self.event_callback = event_callback
        process_environment = os.environ.copy()
        process_environment.update(environment or {})
        self.process = subprocess.Popen(
            ["codex", "app-server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
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
                if self.event_callback is not None:
                    self.event_callback(message)
                if "id" in message:
                    self.responses.put(message)
                else:
                    self.messages.put(message)
            except json.JSONDecodeError:
                continue

    def _drain_stderr(self) -> None:
        assert self.process.stderr is not None
        for _ in self.process.stderr:
            pass

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
        with self.response_lock:
            deadline = time.monotonic() + timeout
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
        self.send(
            "turn/start",
            3,
            turn_params,
        )
        turn_result = self.wait_response(3)
        turn_id = str((turn_result.get("turn") or {}).get("id") or "")
        return thread_id, turn_id

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

    def read_thread(self, thread_id: str, request_id: int = 100) -> dict[str, Any]:
        self.send("thread/read", request_id, {"threadId": thread_id, "includeTurns": True})
        result = self.wait_response(request_id)
        thread = result.get("thread") or {}
        return thread if isinstance(thread, dict) else {}

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
        changes = [{"path": path, "kind": kind} for path in dict.fromkeys(paths) if path]
        return {**item, "type": "fileChange", "changes": changes}
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
        self.process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1, cwd=self.workspace, env=self.environment)
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
                if not self.transcript_key:
                    self.thread_id = str(event.get("session_id") or self.thread_id)
        return_code = self.process.wait()
        if final_text and not any(item.get("text") == final_text for item in turn["items"]):
            turn["items"].append({"id": secrets.token_urlsafe(8), "type": "agentMessage", "text": final_text, "status": "completed", "phase": "final_answer"})
        elif final_text:
            # 最终回复和最后一条 assistant 文本相同：把它标成终态，面板才认得出这是结论。
            for item in reversed(turn["items"]):
                if item.get("type") == "agentMessage" and item.get("text") == final_text:
                    item["phase"] = "final_answer"
                    break
        for item in pending_tools.values():
            item["status"] = "completed" if return_code == 0 else "failed"
        self.turn_status = "completed" if return_code == 0 else "failed"
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

    def read_thread(self, thread_id: str, request_id: int = 100) -> dict[str, Any]:
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

    def list_models(self, request_id: int = 20) -> list[dict[str, Any]]:
        return [{"model": value, "displayName": label} for value, label in [("opus", "Opus 5"), ("sonnet", "Sonnet 5")]]

    def close(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()


def create_ai_client(provider: str, workspace: Path, event_callback: Any = None, environment: dict[str, str] | None = None) -> AppServerClient | ClaudeCLIClient:
    if provider == "claude":
        return ClaudeCLIClient(workspace, event_callback, environment)
    return AppServerClient(workspace, event_callback, environment)


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
            "url": f"/v1/codex/attachments/{attachment_id}",
        }


class WorkspaceArtifactStore:
    """Registers Codex-created workspace files without copying them into the task service."""

    def __init__(self, workspace: Path):
        self.workspace = workspace.resolve()
        self.root = self.workspace / ".codex" / ARTIFACT_DIRECTORY_NAME
        self.lock = threading.Lock()

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
                    path, relative = self._resolve(raw_path)
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
        rows = planner.request_api(
            config,
            "GET",
            "/delivery/requirement/planning-sessions",
            query={"programId": program_id, "requirementKey": requirement_key, "executorType": provider},
        )
        rows = [row for row in (rows or []) if isinstance(row, dict) and str(row.get("threadId") or "")]
        if not rows:
            return None
        catalog = [
            {
                "threadId": str(row.get("threadId") or ""),
                "title": str(row.get("title") or ""),
                "createdAt": str(row.get("createdAt") or ""),
                "updatedAt": str(row.get("updatedAt") or ""),
                "status": str(row.get("status") or "completed"),
                "active": False,
            }
            for row in rows
        ]
        current = next((row for row in rows if str(row.get("threadId")) == thread_id), rows[-1])
        metadata = current.get("metadata") if isinstance(current.get("metadata"), dict) else {}
        baseline = metadata.get("baseline") if isinstance(metadata.get("baseline"), dict) else {}
        return {
            "threadId": str(current.get("threadId") or ""),
            "turnId": str(metadata.get("turnId") or ""),
            "stageKey": str(metadata.get("stageKey") or ""),
            "moduleKey": str(metadata.get("moduleKey") or ""),
            "kind": str(metadata.get("kind") or ""),
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
        result = session.get("result") or {}
        metadata: dict[str, Any] = {
            "turnId": str(session.get("turnId") or ""),
            "stageKey": str(session.get("stageKey") or ""),
            "moduleKey": str(session.get("moduleKey") or ""),
            "kind": str(session.get("kind") or ""),
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
                "turns": [],
                "conversations": [],
                "active": False,
                "activeTurnId": "",
                "selectedStageKey": "",
                "selectedModuleKey": "",
                "selectedKind": "",
                "result": {"items": [], "stages": [], "modules": [], "itemKeys": [], "stageKeys": [], "moduleKeys": [], "updatedAt": ""},
            }
        client = (
            active["client"]
            if active is not None and active.get("threadId") == thread_id
            else create_ai_client(provider, self.workspace, environment=codex_environment(config, program_id, write_allowed=False))
        )
        close_after = active is None or active.get("threadId") != thread_id
        try:
            thread = client.read_thread(thread_id, request_id=client.next_request_id())
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
        finally:
            if close_after:
                client.close()

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
            confirm_write,
        ) = validate_planning_payload(raw)
        assert_runtime_project(config, program_id)
        biz_line = config_biz_line(config)
        context = planner.project_context(config, program_id)
        planner.require_option(selected_stage, context.get("stages") or [], "stageKey", "里程碑")
        planner.require_option(selected_module, context.get("modules") or [], "moduleKey", "模块")
        requirement_key = str(requirement.get("requirementKey") or "")
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
                message_with_attachments(message, attachments),
                attachments,
                request_id=active["client"].next_request_id(),
            )
            self.progress.publish(identity, "message", "已追加拆解要求", message, "running")
            return {"accepted": True, "bizLine": biz_line, "programId": program_id, "requirementKey": requirement_key, "threadId": active["threadId"], "turnId": active["turnId"], "active": True}
        catalog = self._planning_catalog(session)
        known_thread_ids = {str(entry["threadId"]) for entry in catalog}
        if requested_thread_id and requested_thread_id not in known_thread_ids:
            raise BridgeFailure("所选拆解会话不存在")
        if not session or new_conversation or not session.get("threadId"):
            # 一条新会话还没出过预览，没有可确认的方案。
            if confirm_write:
                raise BridgeFailure("请先梳理需求并生成拆解预览，再确认写入")
            if len(catalog) >= MAX_PLANNING_CONVERSATIONS:
                raise BridgeFailure("该需求保留的拆解会话已达上限")
            title = f"需求拆解 · {requirement.get('name') or context.get('program', {}).get('name') or program_id}"
            if catalog:
                title = f"{title} V0.0.{len(catalog)}"
            client = create_ai_client(
                provider,
                self.workspace,
                lambda event: self._publish_app_server_event(identity, event),
                codex_environment(config, program_id, write_allowed=False),
            )
            try:
                thread_id, turn_id = client.start_task(
                    title,
                    message_with_attachments(
                        build_planning_prompt(program_id, context, message, selected_stage, selected_module, selected_kind, requirement, False, self.workspace),
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
                "baseline": baseline,
                "result": {"items": [], "stages": [], "modules": [], "itemKeys": [], "stageKeys": [], "moduleKeys": [], "updatedAt": ""},
                "catalog": [*catalog, {"threadId": thread_id, "title": title, "createdAt": utc_now(), "updatedAt": utc_now(), "status": "running", "active": True}],
            }
        else:
            thread_id = requested_thread_id or str(session.get("threadId") or "")
            client = create_ai_client(
                provider,
                self.workspace,
                lambda event: self._publish_app_server_event(identity, event),
                codex_environment(config, program_id, write_allowed=confirm_write),
            )
            try:
                client.resume_thread(thread_id)
                # 续聊也要重新带上需求上下文和该需求已建任务：会话可能已经被压缩，
                # 而「不要重复建任务」这条约束正是靠这份清单成立的。
                turn_id = client.start_turn(
                    thread_id,
                    message_with_attachments(
                        build_planning_prompt(program_id, context, message, selected_stage, selected_module, selected_kind, requirement, confirm_write, self.workspace),
                        attachments,
                    ),
                    attachments,
                    request_id=client.next_request_id(),
                    model=model,
                    reasoning_effort=reasoning_effort,
                    fast_mode=fast_mode,
                )
            except Exception:
                client.close()
                raise
            session.update({"threadId": thread_id, "turnId": turn_id, "stageKey": selected_stage or session.get("stageKey") or "", "moduleKey": selected_module or session.get("moduleKey") or "", "kind": selected_kind or session.get("kind") or ""})
            for entry in session.get("catalog") or []:
                if entry.get("threadId") == thread_id:
                    entry["status"] = "running"
                    entry["active"] = True
                    entry["updatedAt"] = utc_now()
        with self.lock:
            self.active.add(identity)
            self.active_runs[identity] = {"client": client, "threadId": thread_id, "turnId": turn_id, "planning": True, "provider": provider, "config": config, "programId": program_id}
        # 目录当场写回服务端：这一轮还没跑完桥接就重启，聊天列表里也得留着这条会话。
        self._save_planning_session(config, program_id, requirement_key, provider, session)
        self.progress.publish(
            identity,
            "status",
            "正在写入任务" if confirm_write else "正在梳理需求",
            f"{provider_label(provider)} 正在{'调用任务规划插件写入任务' if confirm_write else '整理拆解预览，确认前不会写入任务'}。",
            "running",
        )
        threading.Thread(
            target=self._follow_planning,
            args=(identity, client, config, program_id, requirement_key, provider, session, thread_id, turn_id),
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
    ) -> None:
        status = "failed"
        try:
            status = client.wait_turn(turn_id)
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
                    self.active_runs.pop(identity, None)

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
        rows = planner.request_api(
            config, "GET", "/delivery/requirement/testing-sessions",
            query={"programId": program_id, "requirementKey": requirement_key, "executorType": provider},
        )
        rows = [row for row in (rows or []) if isinstance(row, dict) and str(row.get("threadId") or "")]
        if not rows:
            return None
        catalog = [
            {
                "threadId": str(row.get("threadId") or ""), "title": str(row.get("title") or ""),
                "createdAt": str(row.get("createdAt") or ""), "updatedAt": str(row.get("updatedAt") or ""),
                "status": str(row.get("status") or "completed"), "active": False,
            }
            for row in rows
        ]
        current = next((row for row in rows if str(row.get("threadId") or "") == thread_id), rows[-1])
        metadata = current.get("metadata") if isinstance(current.get("metadata"), dict) else {}
        return {
            "threadId": str(current.get("threadId") or ""), "turnId": str(metadata.get("turnId") or ""),
            "requirementKey": requirement_key, "catalog": catalog,
        }

    def _save_requirement_testing_session(
        self, config: dict[str, Any], program_id: int, requirement_key: str, provider: str, session: dict[str, Any],
    ) -> None:
        thread_id = str(session.get("threadId") or "")
        if not requirement_key or not thread_id:
            return
        entry = next((item for item in session.get("catalog") or [] if str(item.get("threadId") or "") == thread_id), {})
        try:
            planner.request_api(
                config, "POST", "/delivery/requirement/testing-session/bind",
                body={
                    "programId": program_id, "requirementKey": requirement_key, "executorType": provider,
                    "threadId": thread_id, "title": str(entry.get("title") or "")[:120],
                    "status": str(entry.get("status") or "running"),
                    "metadata": {
                        "turnId": str(session.get("turnId") or ""), "kind": "requirement-testing",
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
                "programId": program_id, "requirementKey": requirement_key, "threadId": "", "turns": [],
                "conversations": catalog, "active": False, "activeTurnId": "", "testingReport": requirement.get("testingReport") or "",
                "testingStatus": requirement.get("testingStatus") or "todo", "testingReportPath": requirement.get("testingReportPath") or "",
                "testingCasesStatus": requirement.get("testingCasesStatus") or "todo", "testingCases": requirement.get("testingCases") or "",
                "testingCasesPath": requirement.get("testingCasesPath") or "",
            }
        client = active["client"] if active is not None and active.get("threadId") == selected_thread_id else create_ai_client(
            provider, self.workspace, environment=codex_environment(config, program_id, write_allowed=True),
        )
        close_after = active is None or active.get("threadId") != selected_thread_id
        try:
            thread = client.read_thread(selected_thread_id, request_id=client.next_request_id())
            item_key = self._requirement_testing_item_key(requirement_key)
            for entry in catalog:
                entry["active"] = bool(active is not None and entry.get("threadId") == active.get("threadId"))
                if not entry["active"] and entry.get("status") == "running":
                    entry["status"] = "interrupted"
            return {
                "programId": program_id, "requirementKey": requirement_key, "threadId": selected_thread_id,
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
        finally:
            if close_after:
                client.close()

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
                "catalog": [*catalog, {"threadId": thread_id, "title": title, "createdAt": utc_now(), "updatedAt": utc_now(), "status": "running", "active": True}],
            }
        else:
            thread_id = requested_thread_id or str(session.get("threadId") or "")
            client = create_ai_client(
                provider, self.workspace, lambda event: self._publish_app_server_event(identity, event),
                codex_environment(config, program_id, write_allowed=True),
            )
            try:
                client.resume_thread(thread_id)
                turn_id = client.start_turn(
                    thread_id, message_with_attachments(build_requirement_testing_prompt(program_id, context, requirement, message, self.workspace, test_case_only), attachments), attachments,
                    request_id=client.next_request_id(), model=model, reasoning_effort=reasoning_effort, fast_mode=fast_mode,
                )
            except Exception:
                client.close()
                raise
            session.update({"threadId": thread_id, "turnId": turn_id})
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
                    self.active_runs.pop(identity, None)

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
        executable_available = shutil.which(provider) is not None
        configured = True
        api_reachable = True
        message = "ready"
        if not executable_available:
            message = f"未找到 {provider_label(provider)} CLI"
        ready = executable_available and configured and api_reachable
        return {
            "ready": ready,
            "bridge": True,
            "codex": shutil.which("codex") is not None,
            "claude": shutil.which("claude") is not None,
            "configured": configured,
            "apiReachable": api_reachable,
            "executorType": provider,
            # 占位目录不是任何项目的仓库，别把它当成"当前工作区"报给面板。
            "workspace": "" if self.workspace == placeholder_workspace() else self.workspace.name,
            "message": message,
            "checkedAt": int(time.time()),
        }

    def request_config(self, raw: Any, origin: str, token: str) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise BridgeFailure("请求体必须是 JSON 对象")
        program_id = program_id_of(raw.get("programId"))
        if not program_id:
            raise BridgeFailure("缺少项目标识")
        if not token:
            raise BridgeFailure("当前用户凭证为空")
        api_url = self._resolve_task_board_api(str(raw.get("apiUrl") or "").strip(), origin, token, program_id)
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
        sessions = planner.request_api(
            config,
            "GET",
            "/delivery/item/execution-session",
            query={"programId": program_id, "itemKey": item_key, "executorType": executor_type},
        ) or []
        rows = [
            session for session in sessions
            if isinstance(session, dict) and str(session.get("executorType") or "") == executor_type
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
        current_thread_id = str((binding or {}).get("externalSessionId") or "")
        identity = self._task_testing_cases_identity(program_id, item_key, provider)
        with self.lock:
            active = self.active_runs.get(identity)
        if not thread_id:
            return {
                "programId": program_id, "itemKey": item_key, "threadId": "", "turns": [], "conversations": [],
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
        if active_for_thread is not None:
            client = active_for_thread["client"]
            close_after = False
        else:
            client = create_ai_client(
                provider, self.workspace, environment=codex_environment(config, program_id, write_allowed=True),
            )
            close_after = True
        try:
            thread = client.read_thread(thread_id, request_id=client.next_request_id())
            for entry in catalog:
                entry["active"] = bool(active_for_thread is not None and entry.get("threadId") == thread_id)
                if not entry["active"] and entry.get("status") == "running":
                    entry["status"] = "interrupted"
            return {
                "programId": program_id, "itemKey": item_key, "threadId": thread_id,
                "turns": serialize_turns(thread.get("turns") or []), "conversations": catalog,
                "active": active_for_thread is not None,
                "activeTurnId": str((active_for_thread or {}).get("turnId") or ""),
                "testingCasesStatus": task.get("testingCasesStatus") or "todo",
                "testingCases": task.get("testingCases") or "",
                "testingCasesPath": task.get("testingCasesPath") or "",
            }
        finally:
            if close_after:
                client.close()

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
                self.active_runs.pop(identity, None)
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
                    thread_id, build_task_testing_cases_prompt(program_id, task, context, message, self.workspace),
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
                self.active_runs.pop(identity, None)
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
                    self.active_runs.pop(identity, None)

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
        task = next((item for item in context["items"] if item.get("itemKey") == requested_task["itemKey"]), None)
        if task is None:
            raise BridgeFailure("任务不存在")
        if int(task.get("version") or 0) != int(requested_task["version"]):
            raise BridgeFailure("任务版本已变化，请刷新任务面板")
        phase = str(task.get("phase") or "requirement")
        if task.get("status") == "done":
            raise BridgeFailure("已完成任务不能再次执行")
        by_key = {str(item.get("itemKey")): item for item in context["items"]}
        incomplete = [
            key for key in task.get("dependsOnItemKeys") or [] if by_key.get(str(key), {}).get("status") != "done"
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
        except Exception:
            client.close()
            with self.lock:
                self.active.discard(identity)
            raise
        payload["task"] = updated_task
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
            thread_id, turn_id = client.start_task(
                title,
                build_task_prompt(payload, self.workspace),
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
                self.active_runs.pop(identity, None)
            raise

        threading.Thread(
            target=self._follow,
            args=(identity, client, config, program_id, item_key, updated_task, binding, turn_id),
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
        sequence_id = secrets.token_urlsafe(12)
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
            self.active_sequences.add(sequence_id)
            self.sequence_tasks.update(reserved)
        threading.Thread(
            target=self._run_sequence,
            args=(sequence_id, config, program_id, ordered, model, provider, execution_constraints, reasoning_effort, fast_mode),
            daemon=True,
        ).start()
        return {
            "accepted": True,
            "sequenceId": sequence_id,
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
        try:
            for item_key in item_keys:
                task = self._task_detail(config, program_id, item_key)
                status = str(task.get("status") or "")
                if status == "done":
                    continue
                self.execute(
                    {
                        "bizLine": biz_line,
                        "programId": program_id,
                        "task": task,
                        "model": model,
                        "provider": provider,
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
                if completed_task.get("status") != "done":
                    raise BridgeFailure(
                        f"任务 {item_key} 未成功完成，队列已停止：{completed_task.get('status') or 'unknown'}"
                    )
        except Exception as exc:
            print(f"串行执行失败 {program_id}/{sequence_id}: {exc}", file=sys.stderr, flush=True)
        finally:
            with self.lock:
                self.active_sequences.discard(sequence_id)
                self.sequence_tasks.difference_update(task_identity(biz_line, program_id, key) for key in item_keys)

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

        batch_id = secrets.token_urlsafe(12)
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
        try:
            remaining = set(item_keys)
            while remaining:
                context = planner.project_context(config, program_id)
                items = [item for item in context.get("items") or [] if isinstance(item, dict)]
                by_key = {str(item.get("itemKey") or ""): item for item in items}
                missing = sorted(remaining - set(by_key))
                if missing:
                    raise BridgeFailure("任务不存在：" + ", ".join(missing))

                remaining.difference_update(
                    key for key in remaining if str(by_key[key].get("status") or "") == "done"
                )
                if not remaining:
                    return

                ready = sorted(
                    key for key in remaining
                    if all(by_key.get(str(dep), {}).get("status") == "done" for dep in by_key[key].get("dependsOnItemKeys") or [])
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
                    task = self._task_detail(config, program_id, item_key)
                    self.execute(
                        {
                            "bizLine": biz_line,
                            "programId": program_id,
                            "task": task,
                            "model": model,
                            "provider": provider,
                            **({"executionConstraints": execution_constraints} if execution_constraints else {}),
                            **({"reasoningEffort": reasoning_effort} if reasoning_effort else {}),
                            **({"fastMode": True} if fast_mode else {}),
                        },
                        batch_claim=True,
                        config=config,
                    )

                launched_identities = {task_identity(biz_line, program_id, item_key) for item_key in ready}
                while True:
                    with self.lock:
                        still_active = launched_identities & self.active
                    if not still_active:
                        break
                    time.sleep(0.2)

                completed_context = planner.project_context(config, program_id)
                completed_by_key = {
                    str(item.get("itemKey") or ""): item
                    for item in completed_context.get("items") or []
                    if isinstance(item, dict)
                }
                failed = sorted(
                    f"{item_key}（{str(completed_by_key.get(item_key, {}).get('status') or 'missing')}）"
                    for item_key in ready
                    if completed_by_key.get(item_key, {}).get("status") != "done"
                )
                if failed:
                    raise BridgeFailure("批量队列已停止，当前并行任务未成功完成：" + "、".join(failed))
                remaining.difference_update(ready)
        except Exception as exc:
            print(f"批量执行失败 {program_id}/{batch_id}: {exc}", file=sys.stderr, flush=True)
        finally:
            with self.lock:
                self.batch_tasks.difference_update(task_identity(biz_line, program_id, key) for key in item_keys)

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
        current_thread_id = str((binding or {}).get("externalSessionId") or "")
        if not thread_id:
            return {
                "bizLine": biz_line,
                "programId": program_id,
                "itemKey": item_key,
                "threadId": "",
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
        if active_for_thread is not None:
            client = active_for_thread["client"]
            close_after = False
        else:
            client = create_ai_client(provider, self.workspace, environment=codex_environment(config, program_id))
            close_after = True
        try:
            thread = client.read_thread(thread_id, request_id=client.next_request_id())
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
        finally:
            if close_after:
                client.close()

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
        rows = planner.request_api(
            config,
            "GET",
            "/delivery/requirement/planning-sessions",
            query={
                "programId": program_id,
                "requirementKey": requirement_key,
                "executorType": requirement_prototype_executor_type(provider),
            },
        )
        return [row for row in (rows or []) if isinstance(row, dict) and str(row.get("threadId") or "")]

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
                "metadata": {"turnId": turn_id, "kind": "requirement-prototype", "workspace": self.workspace.name},
                "actorName": f"{provider}-http-bridge",
            },
        )

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
    ) -> dict[str, Any]:
        identity = self._requirement_prototype_identity(program_id, requirement_key)
        title = f"需求原型 · {str(requirement.get('name') or requirement_key).strip()}"[:120]
        client = create_ai_client(
            provider,
            self.workspace,
            lambda event: self._publish_app_server_event(identity, event),
            codex_environment(config, program_id),
        )
        try:
            prompt = build_requirement_prototype_prompt(program_id, requirement, message, self.workspace, editing=editing)
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
                config, program_id, requirement_key, provider, thread_id, turn_id, title, "running",
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
            args=(identity, client, config, program_id, requirement_key, provider, thread_id, turn_id, title),
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
            return {"programId": program_id, "requirementKey": requirement_key, "threadId": "", "turns": [], "active": False, "activeTurnId": ""}
        client = active["client"] if active is not None and active.get("threadId") == selected_thread_id else create_ai_client(
            provider, self.workspace, environment=codex_environment(config, program_id),
        )
        close_after = active is None or active.get("threadId") != selected_thread_id
        try:
            thread = client.read_thread(selected_thread_id, request_id=client.next_request_id())
            item_key = requirement_prototype_item_key(requirement_key)
            return {
                "programId": program_id,
                "requirementKey": requirement_key,
                "threadId": selected_thread_id,
                "turns": serialize_turns(
                    thread.get("turns") or [],
                    artifact_resolver=lambda paths: self.artifacts.register(config_biz_line(config), program_id, item_key, paths),
                ),
                "active": bool(active is not None and active.get("threadId") == selected_thread_id and active.get("prototype")),
                "activeTurnId": str((active or {}).get("turnId") or ""),
            }
        finally:
            if close_after:
                client.close()

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
            thread_id=requested_thread_id or str((rows[-1] if rows else {}).get("threadId") or ""),
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
    ) -> None:
        status = "failed"
        try:
            status = client.wait_turn(turn_id)
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
            self._save_prototype_session(config, program_id, requirement_key, provider, thread_id, turn_id, title, status)
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
                self._save_prototype_session(config, program_id, requirement_key, provider, thread_id, turn_id, title, status)
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
                    self.active_runs.pop(identity, None)

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
        program_id, item_key, text, requested_thread_id, new_conversation, attachment_ids, model, reasoning_effort, fast_mode = validate_conversation_payload(raw)
        config = request_scoped_config(config, biz_line, program_id)
        biz_line = config_biz_line(config)
        attachments = self.attachments.resolve(program_id, item_key, attachment_ids)
        message = message_with_attachments(text, attachments)
        identity = task_identity(biz_line, program_id, item_key)
        with self.lock:
            active = self.active_runs.get(identity)
        if active is not None:
            if new_conversation or (requested_thread_id and requested_thread_id != active["threadId"]):
                raise BridgeFailure("该任务已有正在运行的 Codex 会话，请先停止或等待当前回合结束")
            client = active["client"]
            client.steer_turn(
                active["threadId"], active["turnId"], message, attachments, request_id=client.next_request_id()
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
                config, program_id, item_key, task, binding, message, attachments, model, provider, reasoning_effort, fast_mode
            )
        thread_id = requested_thread_id or current_thread_id
        metadata = (binding or {}).get("metadata") or {}
        running_turn_id = str(metadata.get("turnId") or "") if isinstance(metadata, dict) else ""
        if binding and binding.get("status") == "running" and thread_id == current_thread_id and running_turn_id:
            active = self._resume_active_turn(config, identity, task, binding, thread_id, running_turn_id, provider)
            client = active["client"]
            client.steer_turn(thread_id, running_turn_id, message, attachments, request_id=client.next_request_id())
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
                    "followUpAttachments": attachments,
                    "model": model,
                    "provider": provider,
                    **({"reasoningEffort": reasoning_effort} if reasoning_effort else {}),
                    **({"fastMode": True} if fast_mode else {}),
                },
                config=config,
            )
        return self._start_follow_up_turn(
            config, program_id, item_key, task, binding, thread_id, message, attachments, model, provider, reasoning_effort, fast_mode
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
            active = self._resume_active_turn(config, identity, task, binding, thread_id, turn_id, provider)
        client = active["client"]
        client.interrupt_turn(active["threadId"], active["turnId"], request_id=client.next_request_id())
        self.progress.publish(identity, "status", "已请求停止任务", "正在等待 Codex 中断当前回合。", "running")
        return {
            "accepted": True,
            "bizLine": biz_line,
            "programId": program_id,
            "itemKey": item_key,
            "threadId": active["threadId"],
            "turnId": active["turnId"],
        }

    def _task_detail(self, config: dict[str, Any], program_id: int, item_key: str) -> dict[str, Any]:
        task = planner.request_api(
            config, "GET", "/delivery/item", query={"programId": program_id, "itemKey": item_key}
        )
        if not isinstance(task, dict) or not task.get("itemKey"):
            raise BridgeFailure("任务不存在")
        return task

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
            query={"programId": program_id, "itemKey": item_key, "executorType": provider, "phase": phase},
        ) or []
        if not isinstance(sessions, list):
            return None
        return next(
            (
                session
                for session in sessions
                if isinstance(session, dict)
                and session.get("executorType") == provider
                and str(session.get("phase") or "requirement") == phase
            ),
            None,
        )

    def _task_session_bindings(
        self,
        config: dict[str, Any],
        program_id: int,
        item_key: str,
        provider: str,
    ) -> list[dict[str, Any]]:
        """Return this task's execution sessions from every delivery phase."""
        sessions = planner.request_api(
            config,
            "GET",
            "/delivery/item/execution-session",
            query={"programId": program_id, "itemKey": item_key, "executorType": provider},
        ) or []
        return [
            session
            for session in sessions
            if isinstance(session, dict) and str(session.get("executorType") or "") == provider
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
            catalog = requirement_document_catalog(
                (planner.project_context(config, program_id).get("items") or []),
                updated_task,
                self.workspace,
            )
            thread_id, turn_id = client.start_task(
                title,
                build_conversation_prompt(program_id, updated_task, text, self.workspace, catalog),
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
                self.active_runs.pop(identity, None)
            raise
        self.progress.publish(identity, "status", "已创建新的 Codex 会话", title, "running")
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
                self.active_runs.pop(identity, None)
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
                self.active_runs.pop(identity, None)
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
    ) -> None:
        provider = str((self.active_runs.get(identity) or {}).get("provider") or "codex")
        try:
            turn_status = client.wait_turn(turn_id)
            turn = client.read_turn(client.thread_id, turn_id, request_id=client.next_request_id())
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
                    execution_output(turn_status, turn),
                    provider,
                )
            # Closing app-server flushes the final turn to the shared Codex session
            # store. Consumers notified before this point can observe 100% progress
            # while still reading the previous conversation snapshot.
            client.close()
            self.progress.publish(
                identity,
                "status",
                "任务已完成" if turn_status == "completed" else "任务执行未完成",
                f"结果已同步到任务面板，状态：{turn_status}",
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
                    self.active_runs.pop(identity, None)

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
                ),
                "actorName": f"{provider}-http-bridge",
            }
            if output_field:
                patch_body[output_field] = execution_output_text
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
    raw = "\n".join(lines).strip() + "\n"
    limit = 8 * 1024 * 1024
    encoded = raw.encode("utf-8")
    if len(encoded) <= limit:
        return raw
    truncated = encoded[: limit - 128].decode("utf-8", errors="ignore")
    return truncated + "\n\n[执行记录过长，已在 8MB 处截断]"


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


def file_changes_of(item: dict[str, Any]) -> list[dict[str, str]]:
    """Normalize one file-change item into `[{path, kind}]`.

    Codex 和 Claude 给的字段名不完全一样，面板只认 path + add/modify/delete/rename。
    """
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for change in item.get("changes") or []:
        if not isinstance(change, dict):
            continue
        path = str(change.get("path") or change.get("file") or change.get("filePath") or "").strip()
        if not path or path in seen:
            continue
        seen.add(path)
        raw_kind = str(change.get("kind") or change.get("type") or change.get("changeType") or "").strip().lower()
        kind = FILE_CHANGE_ALIASES.get(raw_kind, raw_kind if raw_kind in FILE_CHANGE_KINDS else "modify")
        normalized.append({"path": path, "kind": kind})
    return normalized


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
            attachments: list[dict[str, Any]] = []
            changes: list[dict[str, str]] = []
            if item_type == "userMessage":
                text = text_from_user_item(item)
                attachment_ids = attachment_ids_from_text(text)
                if attachment_ids and attachment_resolver:
                    try:
                        attachments = attachment_resolver(attachment_ids)
                    except BridgeFailure:
                        attachments = []
                text = text_without_attachment_context(text)
            elif item_type in {"agentMessage", "plan", "reasoning"}:
                text = str(item.get("text") or item.get("content") or item.get("summary") or "").strip()
                if artifact_resolver and item_type == "agentMessage" and str(item.get("phase") or "") == "final_answer":
                    linked_paths = [match.strip().split("#", 1)[0] for match in MARKDOWN_ARTIFACT_RE.findall(text)]
                    attachments = artifact_resolver(linked_paths[:20])
            elif item_type == "commandExecution":
                command = item.get("command") or item.get("commands") or ""
                text = "\n".join(str(part) for part in command) if isinstance(command, list) else str(command)
            elif item_type in {"mcpToolCall", "dynamicToolCall"}:
                text = str(item.get("tool") or item.get("name") or item.get("server") or "")
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

    def allowed_origin(self) -> str | None:
        origin = self.headers.get("Origin", "")
        if origin and (self.allows_all_origins() or origin in self.allowed_origins):
            return origin
        return None

    def cors(self) -> None:
        origin = self.allowed_origin()
        if origin:
            self.send_header("Access-Control-Allow-Origin", "*" if self.allows_all_origins() else origin)
            if not self.allows_all_origins():
                self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, token")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

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
        name = str(manifest.get("name") or "attachment").replace('"', "")
        self.send_response(200)
        self.cors()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(path.stat().st_size))
        disposition = "inline" if manifest.get("isImage") else "attachment"
        self.send_header("Content-Disposition", f'{disposition}; filename="{name}"')
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
        if path not in {
            "/v1/codex/execute",
            "/v1/codex/task-testing-cases",
            "/v1/codex/task-testing-cases/stop",
            "/v1/codex/execute-batch",
            "/v1/codex/execute-sequence",
            "/v1/codex/conversation",
            "/v1/codex/planning",
            "/v1/codex/planning/stop",
            "/v1/codex/requirement-prototype/generate",
            "/v1/codex/requirement-prototype/conversation",
            "/v1/codex/requirement-testing",
            "/v1/codex/requirement-testing/stop",
            "/v1/codex/attachments",
            "/v1/codex/prototype-directory/open",
            "/v1/codex/stop",
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
            if self.headers.get_content_type() != "application/json":
                self.json_response(415, {"error": "application/json required"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > 64 * 1024:
                raise BridgeFailure("请求体大小无效")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise BridgeFailure("请求体必须是 JSON 对象")
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
            elif path == "/v1/codex/requirement-prototype/generate":
                self.json_response(202, selected_bridge.generate_requirement_prototype(payload, config))
            elif path == "/v1/codex/requirement-prototype/conversation":
                self.json_response(202, selected_bridge.send_requirement_prototype_message(payload, config))
            elif path == "/v1/codex/requirement-testing":
                self.json_response(202, selected_bridge.send_requirement_testing(payload, config))
            elif path == "/v1/codex/requirement-testing/stop":
                self.json_response(202, selected_bridge.stop_requirement_testing(payload, config))
            elif path == "/v1/codex/prototype-directory/open":
                item_key = str(payload.get("itemKey") or "").strip()
                if not item_key:
                    raise BridgeFailure("缺少任务标识")
                self.json_response(202, selected_bridge.open_prototype_directory(program_id_of(payload.get("programId")), item_key, config=config))
            else:
                self.json_response(202, selected_bridge.stop_conversation(payload, config=config))
        except (BridgeFailure, planner.ToolFailure, json.JSONDecodeError, ValueError) as exc:
            self.json_response(400, {"error": str(exc)})
        except Exception as exc:
            self.json_response(500, {"error": f"启动 AI 工具失败：{exc}"})

    def handle_attachment_upload(self) -> None:
        content_length = int(self.headers.get("Content-Length") or 0)
        if content_length <= 0 or content_length > MAX_CONVERSATION_UPLOAD_BYTES:
            raise BridgeFailure("附件请求体大小无效")
        if self.headers.get_content_type() != "multipart/form-data":
            raise BridgeFailure("附件必须使用 multipart/form-data 上传")
        content_type = self.headers.get("Content-Type", "")
        raw = self.rfile.read(content_length)
        message = BytesParser(policy=policy.default).parsebytes(
            f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("ascii") + raw
        )
        if not message.is_multipart():
            raise BridgeFailure("附件请求体不是有效的 multipart/form-data")
        fields: dict[str, str] = {}
        uploads: list[dict[str, Any]] = []
        for part in message.iter_parts():
            name = str(part.get_param("name", header="content-disposition") or "")
            filename = str(part.get_filename() or "")
            data = part.get_payload(decode=True) or b""
            if not filename:
                fields[name] = data.decode(part.get_content_charset() or "utf-8", errors="replace").strip()
                continue
            uploads.append(
                {
                    "name": filename,
                    "contentType": part.get_content_type(),
                    "data": data,
                }
            )
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
    parser.add_argument("--cert-file", default=str(DEFAULT_TLS_CERTIFICATE))
    parser.add_argument("--key-file", default=str(DEFAULT_TLS_KEY))
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
    certificate = Path(args.cert_file).expanduser()
    private_key = Path(args.key_file).expanduser()
    if not certificate.is_file() or not private_key.is_file():
        raise SystemExit("HTTPS bridge certificate is missing. Run scripts/start_http.sh to initialize it.")
    httpd = create_https_server(args.host, args.port, workspace, origins, certificate, private_key)
    threading.Thread(target=httpd.bridge.reconcile_forever, daemon=True).start()  # type: ignore[attr-defined]
    httpd.serve_forever()


if __name__ == "__main__":
    main()
