"""需求大纲、任务文档、设计文档、测试用例这几个栏目的磁盘表示。

这些栏目早期都是「一个固定文件」，现在是「一个目录里的多份文档」：目录下所有
可读的 Markdown / 纯文本 / HTML 都能在面板上选择预览，原来的固定文件名继续作为
默认主文档，存量数据不受影响。

路径一律相对工作区根目录解析，并且必须落在允许的目录里——面板传进来的键名
先过白名单校验，绝不能靠它拼出目录外的路径。
"""

from __future__ import annotations

import mimetypes
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import BridgeFailure

# 需求拆解沉淀下来的需求大纲：每条需求一份，落在该需求的文档目录里。
REQUIREMENT_OUTLINE_FILE_NAME = "需求大纲.md"


MAX_REQUIREMENT_OUTLINE_BYTES = 2 * 1024 * 1024

# 面板直接编辑大纲时走 POST，请求体本身限制在 64KB 级别，这里留出同量级的正文上限。
MAX_EDITABLE_OUTLINE_BYTES = 512 * 1024

# 需求大纲、任务文档、设计文档、测试用例这几个栏目都从「一个固定文件」升级成「一个目录里的多份文档」：
# 目录里所有可读的 Markdown、纯文本和 HTML 文档都能在面板上选择预览，原来的固定文件名继续作为默认主文档，存量数据不受影响。
DOCUMENT_SET_SUFFIXES = {".md", ".markdown", ".txt", ".html", ".htm"}


MAX_DOCUMENT_SET_FILES = 200


MAX_DOCUMENT_SET_FILE_BYTES = 2 * 1024 * 1024

# 测试技能把一条需求或一条任务的全部测试资产写在 doc/test/<键>/ 下。
TESTING_ASSET_ROOT = "test"


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


MAX_REQUIREMENT_DOCUMENT_BYTES = 2 * 1024 * 1024


# 面板还可以直接往栏目目录里放文档（本地选文件或粘贴正文）：什么后缀都收得下，
# 但只有文本类文档能在面板里预览编辑，其余的走附件预览与下载。
MAX_DOCUMENT_UPLOAD_FILES = 10


MAX_DOCUMENT_UPLOAD_FILE_BYTES = 20 * 1024 * 1024


MAX_DOCUMENT_UPLOAD_BYTES = MAX_DOCUMENT_UPLOAD_FILES * MAX_DOCUMENT_UPLOAD_FILE_BYTES + 128 * 1024


TESTING_CASES_FILE_NAME = "测试用例.md"
