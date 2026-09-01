"""需求级提示词：总体测试、评审、原型。

这三件事的对象都是一整条需求而不是单条任务，所以上下文里要带上它下面的
全部任务与交付物。评审另外固定下发一份通用准则，并跳过文档和聊天归档目录——
它们不是代码，评它们只会稀释真正该看的改动。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..documents import (
    REQUIREMENT_OUTLINE_FILE_NAME,
    requirement_prototype_directory_of,
    testing_asset_directory_of,
)
from ..errors import BridgeFailure
from ..prompt_context import workspace_instruction, wrap_bridge_context
from .common import PHASE_SKILLS, REQUIREMENT_SCOPE_RULE, git_branch_lines

# review 永远跳过的目录：文档和聊天归档不是代码，评它们只会稀释真正该看的改动。
REVIEW_EXCLUDED_DIRECTORIES = ("doc", "chat")

# 通用评审准则：面板固定下发给每一次首轮 review，和范围、项目技能那几条规则叠加执行。
REVIEW_GUIDELINES = """Code review guidelines:
Review Guidelines
You are acting as a reviewer for a proposed code change made by another engineer.

Review the change and respond in normal Markdown. Do not return JSON, XML, a findings object, or any structured review schema.

When feedback should be attached directly to a changed line, emit one ::code-comment{...} directive for that issue. The directive creates an inline code comment in the review UI; keep the visible response as normal Markdown. Emit no directives when there are no actionable inline comments.

Required code-comment attributes: title, body, and file. Optional attributes: start, end, and priority. Use the shortest useful line range. file should be an absolute path or include the workspace folder segment.

Focus on discrete, actionable issues the original author would likely fix if they knew about them. Prefer no issues over speculative or low-signal feedback.

General guidelines for whether to call out an issue:

It meaningfully impacts correctness, performance, security, or maintainability.
It is discrete and actionable.
It was introduced by the change under review.
The author would likely fix it once aware.
It does not rely on unstated assumptions about intent.
It identifies the affected behavior clearly rather than speculating broadly.
Repository Rule Attribution
Use the root and scoped project instruction files applicable to changed files, respecting normal project-document precedence (AGENTS.override.md, AGENTS.md, then configured fallback filenames) and selecting at most one file per directory. Guidance may use headings, checklists, bullets, tables, or concise prose; do not require formal IDs or schemas. More-specific guidance wins on conflict, and user instructions about review scope or style take precedence.

Review the diff independently and deduplicate findings by changed location and defect/remedy. A finding is rule-supported only when applicable guidance materially contributes repository-specific scope, an invariant, remedy, convention, or confirmation behavior beyond generic correctness advice. Preserve and union rule support when candidates merge, then check every final candidate against the applicable rules. Do not omit ordinary findings or invent findings solely because a rule file exists.

When collaboration is available, use at most one focused investigator per applicable rule. For each rule-supported finding, verify the applicable project instruction file that supplies the rule and its smallest supporting line range, then include one compact Markdown or local-file reference in the visible comment body. Do not fabricate citations or add hidden metadata.

When you call out an issue, include the relevant file and line or function in prose, explain the scenario where it matters, and keep the explanation concise. Use priority labels such as [P1] or [P2] only when helpful to communicate severity.

If there are no actionable issues, say that directly and briefly. Review the current code changes (staged, unstaged, and untracked files) and provide concise, actionable feedback in a normal Markdown response."""

def build_requirement_testing_prompt(
    program_id: int,
    context: dict[str, Any],
    requirement: dict[str, Any],
    message: str,
    workspace: Path | None = None,
    test_case_only: bool = False,
    follow_up: bool = False,
    include_detail: bool = False,
    mention_context: list[str] | None = None,
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
                *(mention_context or []),
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
            *(mention_context or []),
            "本上下文标记闭合之后的内容，是用户本轮补充的测试要求、环境或数据说明。",
        ],
        message,
    )


def review_scope_of(value: Any) -> list[dict[str, Any]]:
    """前端勾选的 review 范围：一个 Git 工程一条，files 为空表示这个工程整体都要看。"""
    if value is None:
        return []
    if not isinstance(value, list):
        raise BridgeFailure("review 范围必须是数组")
    if len(value) > 64:
        raise BridgeFailure("review 范围最多 64 个工程")
    scope: list[dict[str, Any]] = []
    for raw in value:
        if not isinstance(raw, dict):
            raise BridgeFailure("review 范围里的工程必须是对象")
        path = str(raw.get("path") or "").strip().strip("/")
        name = str(raw.get("name") or "").strip() or (path or "根工程")
        if len(path) > 255 or ".." in Path(path).parts:
            raise BridgeFailure("review 范围里的工程路径无效")
        files = raw.get("files") or []
        if not isinstance(files, list) or len(files) > 500:
            raise BridgeFailure("单个工程的 review 文件最多 500 个")
        cleaned = []
        for item in files:
            file_path = str(item or "").strip()
            if not file_path:
                continue
            if len(file_path) > 512 or ".." in Path(file_path).parts:
                raise BridgeFailure("review 范围里的文件路径无效")
            cleaned.append(file_path)
        scope.append({
            "path": path, "name": name, "files": cleaned,
            "changed": max(int(raw.get("changed") or 0), len(cleaned)),
        })
    return scope


def review_scope_lines(scope: list[dict[str, Any]]) -> list[str]:
    """把勾选范围铺成提示词里的清单：多工程时逐个列，单工程时直接列文件。"""
    if not scope:
        return ["- 用户没有勾选任何变更范围；先说明这一点并让用户确认范围，不要自行扩大到整个仓库。"]
    lines: list[str] = []
    for project in scope:
        location = project["path"] or "（根工程）"
        lines.append(f"- 工程 {project['name']}（相对工作目录路径：{location}）：变更 {project['changed']} 个文件")
        for file_path in project["files"][:200]:
            lines.append(f"  - {file_path}")
        if len(project["files"]) > 200:
            lines.append(f"  - …… 还有 {len(project['files']) - 200} 个文件，按同一范围自行读取")
        if not project["files"]:
            lines.append("  - 用户按工程整体勾选，这个工程里所有未提交改动都在范围内")
    return lines


def requirement_review_report_relative_path(requirement_key: str) -> Path:
    return Path("doc") / "review" / requirement_key / "review报告.md"


def build_requirement_review_prompt(
    program_id: int,
    requirement: dict[str, Any],
    message: str,
    workspace: Path | None,
    scope: list[dict[str, Any]],
    follow_up: bool = False,
    generate_report: bool = False,
    mention_context: list[str] | None = None,
) -> str:
    """代码 review 会话的提示词：范围来自用户勾选，规则是固定三条加用户本轮补充。"""
    requirement_key = str(requirement.get("requirementKey") or "").strip()
    excluded = "、".join(f"{name}/" for name in REVIEW_EXCLUDED_DIRECTORIES)
    rules = [
        f"规则一（范围排除）：{excluded} 目录下的内容一律不 review，即使它们出现在变更清单里也跳过，也不要因为它们没更新而提意见。",
        "规则二（项目技能）：动手前先加载当前工作目录里项目自己的技能（如 backend-development、web-development 等），"
        "按技能里写明的分层、依赖方向、命名、封装和接口约定来判断对错；技能没覆盖的地方再看仓库现有实现的通行写法，不要套用通用最佳实践下结论。",
        "规则三（用户规则）：用户在本轮聊天里写的检查重点和额外规则，优先级最高，与上面两条叠加执行。",
        "规则四（评审准则）：下面这份通用评审准则与上面三条叠加执行；准则里的输出格式要求，"
        "与本轮「输出要求」冲突时以本轮输出要求为准。",
        REVIEW_GUIDELINES,
    ]
    report_path = requirement_review_report_relative_path(requirement_key).as_posix()
    output = (
        (
            "本轮是「确认生成 review 报告」：按上面的范围和规则把完整评审结论写成报告，"
            f"必须写入 `{report_path}`（同一条需求重复生成就覆盖这一份），"
            "报告里先写本轮范围和结论摘要，再按工程和文件列问题：每条写明文件路径、行号或函数、问题、影响、"
            "具体可执行的修改建议，并标注严重级别（阻断 / 建议 / 提示）；最终回复第一行必须是“review 报告已生成”。"
            "本轮的产出是这份报告：用户没有在本轮明确要求改代码，就不要动业务文件；"
            "无论是否改代码，都不要提交、推送或切换分支。"
        )
        if generate_report else
        "输出要求：先说明本轮实际读过的范围，再按工程和文件给出问题清单；"
        "每条写明文件路径、行号或函数、问题、影响，以及具体可执行的修改建议，并标注严重级别（阻断 / 建议 / 提示）；"
        "最后给一句总体结论。本轮不写报告文件。"
        "用户没有要求动代码时就只给意见；一旦用户要求修改（例如「按上面的意见改」或点名其中几条），"
        "就直接改工作区里的业务文件，改完逐条说明改了哪些文件、对应哪条意见。"
        "无论是否改代码，都不要提交、推送或切换分支。"
    )
    if follow_up:
        return wrap_bridge_context(
            [
                "这是同一条需求 review 会话的追加回合：默认只读代码给意见；"
                "用户要求修改时可以直接改工作区里的业务文件，改完说明改了哪些文件、对应哪条意见。"
                "始终不提交、不推送、不切换分支。",
                "首轮已交代过规则和输出格式，这里不再重复；下面是本轮最新的变更范围（可能和上一轮不同，以这份为准）。",
                output,
                workspace_instruction(workspace),
                f"项目 program_id: {program_id}",
                f"需求键 requirement_key: {requirement_key}",
                "本轮 review 范围：",
                *review_scope_lines(scope),
                f"提醒：{excluded} 目录始终不在 review 范围内。",
                *(mention_context or []),
                "本上下文标记闭合之后的内容，是用户本轮补充的 review 规则或检查重点。",
            ],
            message,
        )
    return wrap_bridge_context(
        [
            "这是交付任务面板的一次代码 review 会话：读当前工作区里这条需求的未提交改动，给出评审意见。",
            "这个会话对工作区有写权限：用户要求按 review 意见修改，或在聊天里直接提出改动要求时，就动手改业务文件；"
            "用户没提修改要求的回合只给意见，不要自作主张改代码。",
            "任何情况下都不要执行任务拆解、提交、推送或切分支，也不要生成任务或测试用例。",
            workspace_instruction(workspace),
            f"项目 program_id: {program_id}",
            f"需求键 requirement_key: {requirement_key}",
            f"需求名称: {requirement.get('name') or '未命名'}",
            "需求详情:", str(requirement.get("detail") or "（未填写）"),
            "本轮 review 范围（用户在面板上勾选，逐个工程列出）：",
            *review_scope_lines(scope),
            "review 规则：",
            *rules,
            output,
            *(mention_context or []),
            "本上下文标记闭合之后的内容，是用户本轮补充的 review 规则或检查重点。",
        ],
        message,
    )


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
