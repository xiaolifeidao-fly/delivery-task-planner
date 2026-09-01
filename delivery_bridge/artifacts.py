"""会话附件与工作区产物的落盘。

附件是用户在聊天里带上来的文件，落在工作目录下的专用目录里；
产物是执行器写出来的文件，回复正文里的链接常常只写文件名
（`ShenShiAccessibilityService.kt`），按工作区根目录拼出来的路径并不存在，
只能靠一份带 TTL 的文件名索引反查真实路径。

索引扫描会跳过依赖目录和明显不该外发的文件（.env、credentials.json 之类）。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import mimetypes
import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from .errors import BridgeFailure
from .git_ops import run_git
from .payloads import MAX_CONVERSATION_ATTACHMENTS
from .timeutil import utc_now

MAX_CONVERSATION_ATTACHMENT_BYTES = 20 * 1024 * 1024

MAX_WORKSPACE_ARTIFACT_BYTES = 50 * 1024 * 1024

# 回复正文里的文件链接常常只写文件名（`ShenShiAccessibilityService.kt`），
# 按工作区根目录拼出来的路径并不存在，只能靠一份文件名索引反查真实路径。
WORKSPACE_FILE_INDEX_TTL_SECONDS = 30

MAX_WORKSPACE_FILE_INDEX_ENTRIES = 40000

ATTACHMENT_DIRECTORY_NAME = "delivery-task-attachments"

ARTIFACT_DIRECTORY_NAME = "delivery-task-artifacts"

IMAGE_SUFFIXES = {".gif", ".jpeg", ".jpg", ".png", ".webp"}

MARKDOWN_ARTIFACT_RE = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")

EXCLUDED_ARTIFACT_PARTS = {".codex", ".git"}

EXCLUDED_ARTIFACT_NAMES = {".env", ".env.local", ".env.production", "credentials.json", "secrets.json"}

MAX_CONVERSATION_UPLOAD_BYTES = MAX_CONVERSATION_ATTACHMENTS * MAX_CONVERSATION_ATTACHMENT_BYTES + 128 * 1024


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
