"""云端文档的归属与阶段划分。

云端同步过去只有一个「类别」维度（聊天 / 需求文档 / 设计文档 …），面板上拿到的
是一堆扁平文件，看不出这份文档属于哪条需求、哪条任务、哪个阶段。这里把工作区里
既有的目录约定翻译成两条稳定的元数据：

- 归属：`requirement:<需求键>` 或 `task:<任务键>`，认不出来的落到 `program`。
- 阶段：需求分「需求拆解 / 原型 / 评审 / 测试 / 微调 / 会话」，任务分
  「需求 / 设计 / 测试 / 微调 / 执行产物 / 附件 / 会话」。

判断不靠猜路径里的中文名，而是用面板给的任务清单（任务需求文档路径、任务键）和
需求清单做精确匹配，匹配不上就老实标成未归类，宁可少归类也不要归错。
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any, Iterable

# 归属类型。program 表示这份文件认不到具体的需求或任务。
CLOUD_DOCUMENT_OWNER_KINDS = ("requirement", "task", "program")

# 需求的阶段，顺序即面板 tab 的顺序。
REQUIREMENT_DOCUMENT_STAGES = ("outline", "prototype", "review", "testing", "fine-tuning", "chat")

# 任务的阶段，顺序即面板 tab 的顺序。
TASK_DOCUMENT_STAGES = ("document", "design", "testing", "fine-tuning", "prototype", "execution", "attachment", "chat")

CLOUD_DOCUMENT_STAGES = tuple(dict.fromkeys(REQUIREMENT_DOCUMENT_STAGES + TASK_DOCUMENT_STAGES))

# 微调会话把这一轮实际改了什么写在这里，需求和任务共用一个目录树，键区分归属。
FINE_TUNING_DOCUMENT_ROOT = "fine-tuning"

# review 报告固定写在 doc/review/<需求键>/ 下。
REVIEW_DOCUMENT_ROOT = "review"

# 聊天归档的目录树：chat/requirements/<需求键>/[task/<任务键>/]<标题>--<线程>.md。
# 归档路径和这里的归属判断必须读同一组常量，写进去的目录才认得回来。
CHAT_ARCHIVE_DIRECTORY_NAME = "chat"

CHAT_ARCHIVE_REQUIREMENTS_DIRECTORY_NAME = "requirements"

CHAT_ARCHIVE_TASK_DIRECTORY_NAME = "task"

_KEY_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")

# 任务文档目录里的子目录到阶段的映射；没列出来的子目录仍归到这条任务的需求文档。
_TASK_SUBDIRECTORY_STAGES = {
    "design": "design",
    "test": "testing",
    "prototype": "prototype",
    FINE_TUNING_DOCUMENT_ROOT: "fine-tuning",
}


def fine_tuning_document_directory_of(key: str) -> PurePosixPath:
    """微调记录目录：需求键或任务键都走这一个约定。"""
    value = str(key or "").strip()
    if not _KEY_RE.fullmatch(value):
        raise ValueError("微调文档标识无效")
    return PurePosixPath("doc") / FINE_TUNING_DOCUMENT_ROOT / value


def _synthetic_key_tail(item_key: str) -> str:
    """`__document__:task-design:xxx` 这类合成键的最后一段才是真正的业务键。"""
    value = str(item_key or "").strip()
    if not value.startswith("__"):
        return value
    return value.rsplit(":", 1)[-1].strip()


def cloud_document_key_component(key: str) -> str:
    """归属键拿去当目录名时的安全形式，认不了就返回空串。

    目录名要能被 `CloudDocumentIndex.classify` 原样读回来，所以这里不做转义、
    不做替换：不满足键格式就交空串，让调用方退回上一层归属，而不是造一个
    面板认不回来的目录。
    """
    value = _synthetic_key_tail(key)
    return value if _KEY_RE.fullmatch(value) else ""


def chat_archive_owner_of(resource_kind: str, resource_key: str, requirement_key: str = "") -> tuple[str, str]:
    """一条聊天归档的归属：任务会话归任务，需求会话归需求。

    归档当时用，键直接来自正在跑的那条任务，是可信的；`CloudDocumentIndex.classify`
    是事后照目录名反推，只认面板报过的键。两者对同一条路径的结论通常一致，任务后来被
    删掉时会不一致：那时清单里已经没有这条任务，重传按需求归档，聊天反而还找得到。

    任务键当不了目录名时归档路径停在 task/ 那一层，这里跟着退回需求，别让归属指向
    一个路径里根本没有的目录。
    """
    if str(resource_kind or "") == "task":
        task_key = cloud_document_key_component(resource_key)
        if task_key:
            return "task", task_key
        resource_key = requirement_key
    owning_requirement = cloud_document_key_component(resource_key)
    return ("requirement", owning_requirement) if owning_requirement else ("program", "")


class CloudDocumentIndex:
    """把工作区相对路径翻译成 (归属类型, 归属键, 阶段)。

    需求键和任务键都来自面板，不从路径里现编：路径里出现一个没见过的目录名时，
    宁可标成未归类，也不要造出一条面板上不存在的需求或任务。
    """

    def __init__(self, requirements: Iterable[Any] = (), items: Iterable[Any] = ()) -> None:
        self.requirement_keys: set[str] = set()
        self.task_keys: set[str] = set()
        # 任务需求文档所在目录 -> 任务键；设计文档就在它下面的 design/。
        self.task_document_directories: dict[str, str] = {}

        for requirement in requirements or ():
            key = self._key_of(requirement, "requirementKey")
            if key:
                self.requirement_keys.add(key)
        for item in items or ():
            item_key = self._key_of(item, "itemKey")
            if not item_key:
                continue
            self.task_keys.add(item_key)
            requirement_key = self._key_of(item, "requirementKey")
            if requirement_key:
                self.requirement_keys.add(requirement_key)
            directory = self._task_document_directory(item)
            if directory:
                self.task_document_directories.setdefault(directory, item_key)

    @staticmethod
    def _key_of(row: Any, field: str) -> str:
        if not isinstance(row, dict):
            return ""
        value = str(row.get(field) or "").strip()
        return value if _KEY_RE.fullmatch(value) else ""

    def _task_document_directory(self, item: dict[str, Any]) -> str:
        """任务需求文档目录，面板没给显式路径时按 doc/<模块>/<任务键>/ 兜底。"""
        explicit = str(item.get("requirementDocumentPath") or "").strip().replace("\\", "/")
        if explicit and not explicit.startswith("/") and ".." not in explicit.split("/"):
            parent = PurePosixPath(explicit).parent.as_posix()
            if parent not in {"", "."}:
                return parent
        module_key = str(item.get("moduleKey") or "").strip() or "module"
        item_key = self._key_of(item, "itemKey")
        if not item_key or not _KEY_RE.fullmatch(module_key):
            return ""
        return f"doc/{module_key}/{item_key}"

    def _owner_of_key(self, key: str) -> tuple[str, str]:
        """一个目录名到底是需求还是任务，只认面板给过的键。"""
        value = _synthetic_key_tail(key)
        if value in self.task_keys:
            return "task", value
        if value in self.requirement_keys:
            return "requirement", value
        # 历史需求键一律 req-*，面板清单没覆盖到（比如已删除的需求）时仍按需求归类。
        if value.startswith("req-"):
            return "requirement", value
        return "program", ""

    def classify(self, category: str, relative_path: str) -> tuple[str, str, str]:
        """返回 (归属类型, 归属键, 阶段)；认不出来时归属 program、阶段用类别兜底。"""
        parts = [part for part in str(relative_path or "").split("/") if part]
        if not parts:
            return "program", "", ""

        if parts[0] == CHAT_ARCHIVE_DIRECTORY_NAME:
            # 归档路径是 chat/requirements/<需求键>/[task/<任务键>/]<标题>--<线程>.md。
            if len(parts) >= 3 and parts[1] == CHAT_ARCHIVE_REQUIREMENTS_DIRECTORY_NAME:
                kind, key = self._owner_of_key(parts[2])
                if kind == "requirement":
                    # 任务会话在 task/ 下多带一段任务键，归到那条任务自己的会话栏。
                    # 旧归档没有这一段（task/ 下直接是文件），仍然归到所属需求。
                    if len(parts) >= 6 and parts[3] == CHAT_ARCHIVE_TASK_DIRECTORY_NAME:
                        task_kind, task_key = self._owner_of_key(parts[4])
                        if task_kind == "task":
                            return "task", task_key, "chat"
                    return "requirement", key, "chat"
            return "program", "", "chat"

        # 执行产物与附件的相对路径由本机清单拼出来，第二段就是归属键。
        if parts[0] in {"execution", "attachments"} and len(parts) >= 2:
            stage = "execution" if parts[0] == "execution" else "attachment"
            kind, key = self._owner_of_key(parts[1])
            return kind, key, stage

        if parts[0] != "doc" or len(parts) < 3:
            return "program", "", category

        if parts[1] == "requirements":
            kind, key = self._owner_of_key(parts[2])
            if kind != "requirement":
                return "program", "", category
            stage = "prototype" if len(parts) >= 4 and parts[3] == "prototype" else "outline"
            return "requirement", key, stage

        if parts[1] == REVIEW_DOCUMENT_ROOT:
            kind, key = self._owner_of_key(parts[2])
            return (kind, key, "review") if kind == "requirement" else ("program", "", category)

        if parts[1] == "test":
            kind, key = self._owner_of_key(parts[2])
            return (kind, key, "testing") if kind != "program" else ("program", "", category)

        if parts[1] == FINE_TUNING_DOCUMENT_ROOT:
            kind, key = self._owner_of_key(parts[2])
            return (kind, key, "fine-tuning") if kind != "program" else ("program", "", category)

        # 剩下的都是任务自己的文档目录：doc/<模块>/<任务键>/ 下是需求文档，design/ 下是设计文档。
        # 这个目录里的其他子目录同样属于这条任务，按第一段子目录归到对应阶段，认不出来的算需求文档。
        relative = "/".join(parts)
        for candidate, item_key in self.task_document_directories.items():
            if not relative.startswith(f"{candidate}/"):
                continue
            tail = relative[len(candidate) + 1:].split("/")
            if len(tail) == 1:
                return "task", item_key, "document"
            return "task", item_key, _TASK_SUBDIRECTORY_STAGES.get(tail[0], "document")
        return "program", "", category
