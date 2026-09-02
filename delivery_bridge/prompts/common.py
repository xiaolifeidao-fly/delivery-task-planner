"""各阶段提示词共用的上下文行与规则行。

任务生命周期的四个技能都在本插件 skills/ 下，执行时按阶段点名，
别让执行器自己猜；文档路径、改写规则、同级文档清单也都在这里统一措辞。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..errors import BridgeFailure
from ..documents import (
    DOCUMENT_SET_SUFFIXES,
    REQUIREMENT_OUTLINE_FILE_NAME,
    document_set_entries,
    requirement_document_directory_of,
    requirement_outline_path_of,
    requirement_prototype_directory_of,
    testing_asset_directory_of,
)

# 任务生命周期的四个技能都在本插件 skills/ 下；执行时按阶段点名，别让执行器自己猜。
PLANNING_SKILL = "delivery-task-planner"


PHASE_SKILLS = {
    "requirement": "delivery-requirement-grooming",
    "development": "delivery-action-execution",
    "testing": "delivery-testing-report",
}


# 拆解上下文只给当前需求：项目里其他需求的任务清单不再逐轮塞进提示词，
# 既省上下文，也避免执行器拿无关需求的任务去做去重和依赖判断。
REQUIREMENT_SCOPE_RULE = (
    "上面只列出当前需求下的任务。项目里其他需求的任务不在本轮上下文中，"
    "去重、复用和依赖判断都只在本需求范围内进行；需要参考其他需求或任务时，"
    "只能用用户在需求详情或聊天里 @ 引用并单独给出的那些，不要自行假设项目里还存在哪些任务。"
)


def document_path_of(task: dict[str, Any]) -> str:
    """任务需求文档在工作区里的相对路径；面板没给就按 doc/<模块>/<任务键>/文档.md 兜底。"""
    explicit = str(task.get("requirementDocumentPath") or "").strip()
    if explicit:
        return explicit
    return f"doc/{task.get('moduleKey') or 'module'}/{task.get('itemKey') or 'item'}/文档.md"


def requirement_outline_path_for(task: dict[str, Any]) -> str:
    """任务所属需求的需求级文档路径；任务没挂需求或需求键非法时返回空串。"""
    requirement_key = str(task.get("requirementKey") or "").strip()
    if not requirement_key:
        return ""
    try:
        return requirement_outline_path_of(requirement_key).as_posix()
    except BridgeFailure:
        return ""


def document_exists(workspace: Path | None, relative: str) -> bool:
    """文档存在与否一律在应用层判定，执行技能不再自己去猜。

    没有绑定工作目录时（单测和少数只拼提示词的调用）无从核对，按「存在」处理：
    让提示词保持既有的“先读文档”措辞，总比凭空断言文档缺失、把执行器引偏要好。
    """
    if workspace is None:
        return True
    return readable_document(workspace, relative)


def document_revision_rule(document_path: str) -> str:
    """需求文档是跨回合累积的文档，追加需求时最容易被整段覆盖成只剩本轮内容。

    改法固定成「按章节定点编辑」而不是「整篇重写」：一份需求文档动辄一两万字，
    每轮重写一遍就是每轮多烧一份全文的输出 token，而且模型重写长文本时漏章节的
    概率比定点编辑高得多——两个目标是一致的，不需要靠全文重写来防丢失。
    """
    return (
        f"`{document_path}` 是跨回合累积的文档，不是本轮回复的存档。改它的方式是**按章节定点编辑**："
        "先定位到要改的章节（文件长就按标题定位，不必整篇载入），把本轮新增或调整的内容写进对应章节，"
        "需要新增内容时追加新章节；其余章节一个字都不要动。"
        "不要把整篇重写一遍——既没必要，也最容易在重写时漏掉此前几轮的章节。"
        "更不要用只含本轮增量的内容覆盖整份文件，那等于把之前的需求文档整段删掉。"
        "只有用户明确要求删除的内容才能删。"
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


def readable_directory(workspace: Path | None, relative: str) -> bool:
    """目录是否真的存在。没落过盘的任务目录不该出现在清单里。"""
    if not workspace or not relative:
        return False
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts:
        return False
    try:
        return (workspace / candidate).resolve().is_dir()
    except OSError:
        return False


def requirement_document_catalog(
    items: list[Any],
    task: dict[str, Any],
    workspace: Path | None,
    limit: int = 60,
) -> list[str]:
    """List only the directories of the tasks this one depends on.

    给目录不给文件、更不给正文：需要的是「上游产出放在哪」这条线索，不是一份可以顺手全读的清单。
    同需求下的非前置任务一律不列——列出来模型就倾向于挨个打开，上下文会被无关任务占满；
    真需要时按同一套路径规则（doc/<模块>/<任务键>/）自己去翻。
    """
    requirement_key = str(task.get("requirementKey") or "").strip()
    if not requirement_key:
        return []
    current_key = str(task.get("itemKey") or "")
    dependencies = {str(key) for key in task.get("dependsOnItemKeys") or []}
    if not dependencies:
        return []
    lines: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        item_key = str(item.get("itemKey") or "")
        if not item_key or item_key == current_key or item_key not in dependencies:
            continue
        if str(item.get("requirementKey") or "").strip() != requirement_key:
            continue
        directory = Path(document_path_of(item)).parent.as_posix()
        if not readable_directory(workspace, directory):
            continue
        suffix = "（已完成）" if str(item.get("status") or "") == "done" else ""
        lines.append(f"- {item_key}: {item.get('title') or item_key}{suffix} → `{directory}/`")
        if len(lines) >= limit:
            break
    return lines


def sibling_document_lines(catalog: Any) -> list[str]:
    """把前置任务的文档目录渲染成提示词片段，并交代按需加载的规则。"""
    entries = [str(line) for line in catalog or [] if str(line).strip()]
    if not entries:
        return []
    return [
        "",
        "前置任务的文档目录（只给目录，默认不要打开）:",
        *entries,
        "加载规则：现在不要读这些目录里的任何文件。只有在实现过程中真的卡在某个上游产出上"
        "——要对接的接口签名、字段口径、数据结构或已定下的约定——才去对应目录里列一下文件，"
        "只打开那一份、只读需要的章节。读了哪几份、为了解决什么问题，在最终回复里说明。",
    ]


def git_branch_lines(branch: str) -> list[str]:
    """需求启用了 Git 分支时，明确告诉执行器改动应留在这条分支上。"""
    if not branch:
        return []
    return [
        f"Git 需求分支: {branch}（工作目录已切到该分支）。本任务的所有改动都留在这条分支上，"
        "不要切换分支、不要合并回主干，也不要执行 push。",
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
