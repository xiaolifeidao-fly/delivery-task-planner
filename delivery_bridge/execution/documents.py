"""需求文档与各栏目文档集的读写、上传、附件下载。
"""

from __future__ import annotations


from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from delivery_bridge.documents import (
    MAX_DOCUMENT_SET_FILE_BYTES,
    MAX_DOCUMENT_UPLOAD_FILES,
    MAX_DOCUMENT_UPLOAD_FILE_BYTES,
    MAX_REQUIREMENT_DOCUMENT_BYTES,
    TESTING_CASES_FILE_NAME,
    available_document_name,
    document_in_set,
    document_payload,
    document_set_entries,
    document_upload_name,
    legacy_task_outline_path_of,
    outline_file_in_workspace,
    requirement_outline_path_of,
    testing_asset_directory_of,
)
from delivery_bridge.errors import BridgeFailure
from delivery_bridge.item_keys import document_attachment_item_key
from delivery_bridge.payloads import config_biz_line, request_scoped_config
from delivery_bridge.prompts.common import document_path_of
from delivery_bridge.prompts.requirement import requirement_review_report_relative_path
from delivery_bridge.providers import DEFAULT_BIZ_LINE


class DocumentsMixin:
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
        if scope_value in {"requirement-outline", "requirement-testing", "requirement-review"}:
            self._requirement_for_prototype(config, program_id, key_value)
            if scope_value == "requirement-outline":
                outline = requirement_outline_path_of(key_value)
                # 需求目录下还挂着 prototype/，大纲栏目只列顶层的文本文档。
                return outline.parent, outline.as_posix(), False
            if scope_value == "requirement-review":
                # review 报告固定写在 doc/review/<需求键>/ 下，同一条需求重复生成就覆盖那一份。
                report = requirement_review_report_relative_path(key_value)
                return report.parent, report.as_posix(), False
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
