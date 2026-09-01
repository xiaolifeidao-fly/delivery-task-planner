"""几类非任务实体在会话表里的归属标识与执行器类型。

需求原型、成品测试用例、微调这些都不是任务本身，但要共用同一张会话表，
所以各自有一个固定前缀的 itemKey，以及一个带用途后缀的 executorType。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .documents import (
    HTML_SUFFIXES,
    html_asset_payloads,
    requirement_prototype_directory_of,
)
from .errors import BridgeFailure
from .providers import ai_provider_of

MAX_REQUIREMENT_PROTOTYPE_FILES = 30
MAX_REQUIREMENT_PROTOTYPE_FILE_BYTES = 2 * 1024 * 1024
MAX_REQUIREMENT_PROTOTYPE_TOTAL_BYTES = 8 * 1024 * 1024

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

def task_fine_tuning_executor_type(provider: str) -> str:
    """Keep task-level refinement chats out of both execution and test-case histories."""
    return f"{ai_provider_of(provider)}-fine-tuning"

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
