"""把面板可见的聊天正文备份到项目工作目录，以及挑出要上云的文件。

每一轮结束后写一份人工可读的副本，按所属需求分组，需求和任务的聊天放在一起。
它可以随项目一起共享，但不替代 Codex / Claude 各自保留的原始执行器会话。

Git 聊天同步和云端同步是两回事：前者决定 chat/ 是否落到工作目录，
后者决定把用户明确选中的内容传给服务端。服务端也会复核类别，桥接绕不过项目设置。
"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .cloud_documents import CloudDocumentIndex
from .documents import DOCUMENT_SET_SUFFIXES
from .errors import BridgeFailure
from .attachments_text import text_without_attachment_context
from .reasoning import reasoning_summary_text
from .timeutil import utc_now
from .turn_output import file_changes_of, text_from_user_item

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
CLOUD_SYNC_SCOPES = {"chat", "requirement", "design", "test", "prototype", "execution", "attachment"}

MAX_CLOUD_SYNC_FILE_BYTES = 8 * 1024 * 1024

MAX_CLOUD_SYNC_FILES_PER_RUN = 500


@dataclass(frozen=True)
class CloudSyncEntry:
    """一份待上传的项目文件：同步类别、工作区相对路径，以及它属于谁的哪个阶段。"""

    category: str
    relative_path: str
    source: Path
    content_type: str
    owner_kind: str = "program"
    owner_key: str = ""
    stage: str = ""

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

def cloud_sync_workspace_entries(
    workspace: Path,
    scopes: set[str],
    program_id: int | str | None = None,
    index: CloudDocumentIndex | None = None,
) -> tuple[list[CloudSyncEntry], int]:
    """Return only configured, workspace-contained files for a project cloud sync.

    `chat/` is isolated from regular project files. Documents, design notes, test material
    and HTML prototypes are classified from their stable workspace directories. Execution
    artifacts and attachments are resolved through their local manifests and only included
    when the manifest belongs to the requested program.

    每条记录除了同步类别，还带上这份文件属于哪条需求或任务、属于哪个阶段，面板据此
    把需求文档和任务文档分开展示。`index` 为空表示这次同步没拿到面板清单，
    所有文件按未归类上传，不去猜归属。
    """
    wanted = scopes & CLOUD_SYNC_SCOPES
    if not wanted:
        return [], 0
    root = workspace.resolve()
    catalog = index or CloudDocumentIndex()
    entries: dict[tuple[str, str], CloudSyncEntry] = {}
    skipped = 0

    def offer(category: str, source: Path, relative_path: str | None = None) -> None:
        nonlocal skipped
        if len(entries) >= MAX_CLOUD_SYNC_FILES_PER_RUN:
            skipped += 1
            return
        try:
            resolved = source.resolve()
            relative = relative_path or resolved.relative_to(root).as_posix()
            if not resolved.is_file() or resolved.stat().st_size > MAX_CLOUD_SYNC_FILE_BYTES:
                skipped += 1
                return
        except (OSError, ValueError):
            skipped += 1
            return
        content_type = mimetypes.guess_type(relative)[0] or "application/octet-stream"
        owner_kind, owner_key, stage = catalog.classify(category, relative)
        entries[(category, relative)] = CloudSyncEntry(
            category=category, relative_path=relative, source=resolved, content_type=content_type,
            owner_kind=owner_kind, owner_key=owner_key, stage=stage,
        )

    if "chat" in wanted:
        chat_root = root / CHAT_ARCHIVE_DIRECTORY_NAME
        if chat_root.is_dir():
            try:
                for source in chat_root.rglob("*.md"):
                    offer("chat", source)
            except OSError:
                skipped += 1

    document_root = root / "doc"
    if document_root.is_dir() and wanted & {"requirement", "design", "test", "prototype"}:
        try:
            for source in document_root.rglob("*"):
                if not source.is_file():
                    continue
                try:
                    relative_to_document = source.resolve().relative_to(document_root.resolve())
                except (OSError, ValueError):
                    skipped += 1
                    continue
                directory_parts = set(relative_to_document.parts[:-1])
                if "prototype" in directory_parts:
                    if "prototype" in wanted:
                        offer("prototype", source)
                elif "test" in directory_parts:
                    if "test" in wanted and source.suffix.lower() in DOCUMENT_SET_SUFFIXES:
                        offer("test", source)
                elif "design" in directory_parts:
                    if "design" in wanted:
                        offer("design", source)
                elif "requirement" in wanted and source.suffix.lower() in DOCUMENT_SET_SUFFIXES:
                    offer("requirement", source)
        except OSError:
            skipped += 1

    if program_id is not None and wanted & {"execution", "attachment"}:
        manifest_root = root / ".codex"
        expected_program_id = str(program_id)

        def safe_display_name(value: Any, fallback: str) -> str:
            name = re.sub(r"[\\x00-\\x1f/\\\\]+", "-", Path(str(value or "")).name).strip(" .-")
            return name if name and name != ".." else fallback

        def manifests(directory_name: str) -> list[dict[str, Any]]:
            directory = manifest_root / directory_name
            rows: list[dict[str, Any]] = []
            if not directory.is_dir():
                return rows
            for manifest_path in sorted(directory.glob("*.json")):
                try:
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    nonlocal_skipped[0] += 1
                    continue
                if isinstance(manifest, dict) and str(manifest.get("programId")) == expected_program_id:
                    rows.append(manifest)
            return rows

        # A list wrapper lets the manifest reader count bad entries without widening
        # the helper's return contract.
        nonlocal_skipped = [0]
        if "attachment" in wanted:
            attachment_root = manifest_root / "delivery-task-attachments"
            for manifest in manifests("delivery-task-attachments"):
                attachment_id = str(manifest.get("id") or "attachment")
                file_name = str(manifest.get("fileName") or "")
                item_key = safe_display_name(manifest.get("itemKey"), "unassigned")
                source = attachment_root / file_name
                display_name = safe_display_name(manifest.get("name"), attachment_id)
                offer("attachment", source, (Path("attachments") / item_key / f"{attachment_id}-{display_name}").as_posix())
        if "execution" in wanted:
            for manifest in manifests("delivery-task-artifacts"):
                item_key = safe_display_name(manifest.get("itemKey"), "unassigned")
                relative = str(manifest.get("relativePath") or "").replace("\\\\", "/").lstrip("/")
                if not relative or ".." in Path(relative).parts:
                    skipped += 1
                    continue
                offer("execution", root / relative, (Path("execution") / item_key / relative).as_posix())
        skipped += nonlocal_skipped[0]

    return [entries[key] for key in sorted(entries)], skipped
