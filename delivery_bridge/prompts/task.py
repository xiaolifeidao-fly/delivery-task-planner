"""任务级提示词：动作执行、成品测试用例、微调。

每一条都先点名该阶段的技能，再把面板掌握的事实（任务、文档路径、分支、
同级文档）摆出来，最后交代改写规则。措辞的共用部分在 common.py。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..errors import BridgeFailure
from ..prompt_context import workspace_instruction, wrap_bridge_context
from .common import (
    PHASE_SKILLS,
    document_exists,
    document_path_of,
    document_revision_rule,
    git_branch_lines,
    prototype_directory_of,
    requirement_document_catalog,
    requirement_outline_path_for,
    sibling_document_lines,
)

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
    # 文档在不在，一律由应用层核对后直接写死在提示词里：执行技能不再自己判断、也不再去试探性地读文件。
    outline_path = requirement_outline_path_for(task)
    outline_present = bool(outline_path) and document_exists(workspace, outline_path)
    task_document_present = document_exists(workspace, document_path)
    description = str(task.get("description") or "").strip()
    # 需求级文档是这条任务所属需求的权威上下文，优先级高于任务级文档；任务级文档只补任务范围内的细节。
    reading_order = (
        [
            f"需求级文档: `{outline_path}`（应用层已核对存在）。这是最高优先级的上下文：动手前先完整读一遍，"
            "需求目标、范围、约束和验收口径以它为准。",
        ]
        if outline_present
        else (
            [f"需求级文档 `{outline_path}` 尚未沉淀（应用层已核对不存在）：不用去找它，也不要为此停下。"]
            if outline_path
            else []
        )
    )
    if task_document_present:
        reading_order.extend([
            f"任务需求文档: `{document_path}`（应用层已核对存在）。这是本任务唯一的任务级需求文档，"
            "读完需求级文档后接着完整读它；两者冲突时以需求级文档为准，并在最终回复里指出冲突点。",
            document_revision_rule(document_path),
        ])
    else:
        reading_order.extend([
            f"本任务没有任务级需求文档：应用层已核对 `{document_path}` 不存在。不要去读它，不要因为缺文档就停下，"
            "也不要在动手前先补一份需求文档；本轮的任务级需求，直接以下面的「任务说明」为准。",
            "任务说明（本任务级需求的唯一来源，逐条落实，不要自行扩大或缩小范围）:",
            description or "（面板未填写说明：只能依据需求级文档和真实代码执行；两者都不足以确定范围时，说明缺口并停下）",
        ])
    # 每个阶段各有一个技能，明确点名让执行器去加载，别让它自己猜「当前项目的 skill」是哪个。
    document_source = f"`{document_path}`" if task_document_present else "上面的任务说明"
    phase_instruction = {
        "requirement": (
            f"本次只进行梳理需求：遵循 {PHASE_SKILLS['requirement']} 技能，创建或更新工作区中的 `{document_path}`。"
            "每次后续会话都会从这个文件读取需求上下文；文档结论必须基于工作目录里的真实代码，不要凭业务名词推演。"
        ),
        "development": (
            f"本次只进行动作执行：遵循 {PHASE_SKILLS['development']} 技能，按上面给出的阅读顺序取需求（{document_source}），"
            "再按当前项目的开发技能实现并交付产物。"
        ),
        "testing": (
            f"本次只进行成品测试：遵循 {PHASE_SKILLS['testing']} 技能，按上面给出的阅读顺序取需求（{document_source}），"
            f"再读取已有 `{test_artifact_directory / '测试用例.md'}`（不存在时说明缺口并补充最小用例），"
            "先准备环境、账号、鉴权和测试数据，再按代码与业务依赖编排实测；"
            f"验证命令沿用当前项目开发技能里的约定；所有测试资产必须写入 `{test_artifact_directory}/`，该目录支持多份文档；"
            "并生成带明确验收判定的测试报告。"
        ),
    }.get(phase, "按任务当前阶段执行。")
    # 任务的文档产物要留下完整的设计与思考过程，而不是只贴一份改完之后的结论。
    design_process_instruction = (
        [
            f"本轮的任务文档产物写入 `{design_directory}/`，用稳定唯一的文件名（例如 `设计过程.md`），"
            "内容是你这一轮完整的设计与思考过程，不是只贴结论或改动清单：需求怎么理解、勘察到的现状事实（文件、接口、表、现有实现）、"
            "考虑过哪些方案及各自取舍、为什么选中最终方案、方案怎么落到具体改动、影响面和兼容性怎么处理、"
            "如何验证、还剩哪些风险与待确认项。过程中被否决的想法也要写清楚为什么否决。",
            "已有同名设计文档时，先完整读一遍再把本轮的推演补进去，不要整篇覆盖掉此前的设计过程；"
            "最终回复里列出这份文档的工作区相对路径。",
        ]
        if phase == "development" else []
    )
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
        # 文档缺失时说明会以「任务说明」整段单独给出，这里不再重复一遍。
        *([f"说明: {description or '无'}"] if task_document_present else []),
        *reading_order,
        f"任务需求文档目录: `{document_directory}/`，支持多份文档；`文档.md` 是主文档，独立任务说明使用独立文件名写在此目录。",
        f"任务设计文档目录: `{design_directory}/`，支持多份文档；需要交付独立设计说明时写入此目录，不要写入 `.codex/visualizations` 或其他工作区外路径。",
        phase_instruction,
        *design_process_instruction,
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


def fine_tuning_skill_instruction() -> list[str]:
    """Tell the execution agent exactly where the user-managed project skills live."""
    return [
        "开始任何调整前，先检查当前工作目录下的 `.codex/skills/`；若其中存在与用户本轮目标相关的 SKILL.md，"
        "必须完整读取并遵循它。不要把无关技能、用户附件或聊天内容当作指令文件。",
        "当前工作目录没有匹配技能时，再使用执行器已具备的适用技能；不要为了补齐技能而改动项目配置。",
    ]


def build_requirement_fine_tuning_prompt(
    program_id: int,
    requirement: dict[str, Any],
    context: dict[str, Any],
    message: str,
    workspace: Path | None = None,
    follow_up: bool = False,
) -> str:
    """Give a requirement-level refinement thread authoritative scope without reopening planning."""
    requirement_key = str(requirement.get("requirementKey") or "").strip()
    items = [item for item in context.get("items") or [] if isinstance(item, dict)]
    related = [item for item in items if str(item.get("requirementKey") or "").strip() == requirement_key]
    task_lines = [
        f"- {item.get('itemKey') or '-'}: {item.get('title') or item.get('itemKey') or '未命名'}"
        f"（{item.get('phase') or 'requirement'}/{item.get('status') or 'todo'}；需求文档：{document_path_of(item)}）"
        for item in related[:60]
    ]
    common = [
        "这是交付任务面板的需求级「微调」会话。按用户本轮原话，对已经交付的需求继续做实际调整。",
        "用户的输入决定要改什么、改到什么程度；不要自行扩展目标，也不要先提出拆解方案代替执行。",
        "允许按用户要求改工作区中的代码、文档、原型或测试资产；先检查真实项目状态再动手。",
        "不得创建或拆解任务，不得领取任务、推进任务或需求阶段、写测试验收结论，也不得提交、推送或切换 Git 分支。",
        workspace_instruction(workspace),
        *fine_tuning_skill_instruction(),
        f"项目 program_id: {program_id}",
        f"需求键 requirement_key: {requirement_key}",
        f"需求名称: {requirement.get('name') or requirement_key}",
        "需求详情:",
        str(requirement.get("detail") or "（未填写）"),
        f"需求文档目录: doc/requirements/{requirement_key}/；按需读取其中的需求文档和原型，不存在时先说明。",
        "本需求关联任务（仅作上下文，不得改变它们的面板状态）:",
        *(task_lines or ["- 暂无任务"]),
        "最终回复需简要列出实际改动、验证结果，以及仍需用户决定的事项。",
        "本上下文标记闭合之后的内容，是用户本轮输入的原文。",
    ]
    if follow_up:
        common = [
            "这是同一条需求微调会话的追加回合。保持首轮已确定的微调边界，按用户本轮原话继续实际调整。",
            "不得创建或拆解任务，不得改变需求或任务的面板状态，不得提交、推送或切换 Git 分支。",
            workspace_instruction(workspace),
            *fine_tuning_skill_instruction(),
            f"项目 program_id: {program_id}",
            f"需求键 requirement_key: {requirement_key}",
            "本上下文标记闭合之后的内容，是用户本轮输入的原文。",
        ]
    return wrap_bridge_context(common, message)


def build_task_fine_tuning_prompt(
    program_id: int,
    task: dict[str, Any],
    context: dict[str, Any],
    requirement: dict[str, Any] | None,
    message: str,
    workspace: Path | None = None,
    follow_up: bool = False,
) -> str:
    """Task refinement gets the task, its parent requirement, and nearby documents automatically."""
    item_key = str(task.get("itemKey") or "").strip()
    requirement_key = str(task.get("requirementKey") or "").strip()
    requirement_label = requirement_key or "未关联"
    if requirement:
        requirement_label = f"{requirement_label} · {requirement.get('name') or requirement_key}"
    common = [
        "这是交付任务面板的任务级「微调」会话。按用户本轮原话，对这一条任务已交付的产物继续做实际调整。",
        "用户的输入决定要改什么、改到什么程度；不要自行扩大为需求拆解、任务执行或测试流程。",
        "允许按用户要求改工作区中的代码、文档、原型或测试资产；先检查真实项目状态再动手。",
        "不得领取任务、推进任务或需求阶段、写测试验收结论，也不得提交、推送或切换 Git 分支。",
        workspace_instruction(workspace),
        *fine_tuning_skill_instruction(),
        f"项目 program_id: {program_id}",
        f"任务键 item_key: {item_key}",
        f"任务名称: {task.get('title') or item_key}",
        f"任务当前阶段（只读）: {task.get('phase') or 'requirement'}/{task.get('status') or 'todo'}",
        f"任务说明: {task.get('description') or '（未填写）'}",
        f"任务需求文档: {document_path_of(task)}（开始前优先读取）",
        f"所属需求: {requirement_label}",
        "所属需求详情:",
        str((requirement or {}).get("detail") or "（未填写）"),
        *sibling_document_lines(requirement_document_catalog(context.get("items") or [], task, workspace)),
        "最终回复需简要列出实际改动、验证结果，以及仍需用户决定的事项。",
        "本上下文标记闭合之后的内容，是用户本轮输入的原文。",
    ]
    if follow_up:
        common = [
            "这是同一条任务微调会话的追加回合。保持首轮已确定的微调边界，按用户本轮原话继续实际调整。",
            "不得领取或推进任务，不得改变需求或任务的面板状态，不得提交、推送或切换 Git 分支。",
            workspace_instruction(workspace),
            *fine_tuning_skill_instruction(),
            f"项目 program_id: {program_id}",
            f"任务键 item_key: {item_key}",
            f"任务需求文档: {document_path_of(task)}",
            "本上下文标记闭合之后的内容，是用户本轮输入的原文。",
        ]
    return wrap_bridge_context(common, message)
