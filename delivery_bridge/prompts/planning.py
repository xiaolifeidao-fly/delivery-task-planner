"""需求拆解及其后续轮次的提示词，含过程摘要临时文档。

拆解上下文只给当前需求：项目里其他需求的任务清单不逐轮塞进提示词，
既省上下文，也避免执行器拿无关需求的任务去做去重和依赖判断。

过程摘要落在插件目录下的 .temp/ 里，是给下一轮读的草稿，不是交付物；
runtime.PLUGIN_ROOT 按模块名访问，测试改写它时这里跟着变。
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Any

from .. import runtime
from ..documents import (
    REQUIREMENT_OUTLINE_FILE_NAME,
    requirement_document_directory_of,
    requirement_outline_path_of,
)
from ..errors import BridgeFailure
from ..prompt_context import workspace_instruction, wrap_bridge_context
from ..timeutil import utc_now
from ..runtime import taskboard_command
from .common import (
    document_path_of,
    PHASE_SKILLS,
    PLANNING_SKILL,
    REQUIREMENT_SCOPE_RULE,
    git_branch_lines,
    requirement_document_rule_lines,
)

# 任务说明是执行阶段的兜底需求输入：任务没有单独的需求文档时，执行器看到的就只有它，
# 所以拆解阶段就得把它写成能独立执行的说明，而不是一句话概括。
# 六段式任务说明的正文在 references/任务拆解与写入.md，本轮提示词已经点名要读它；这里只强调它的作用。
TASK_DESCRIPTION_RULE_LINES = [
    "任务说明（description）是动作执行阶段的兜底需求输入：这条任务没有单独的任务需求文档时，"
    "执行器唯一能看到的需求就是它。写法照 `references/任务拆解与写入.md` 里的六段式要求，不能只给一句话概括。",
]


# 需求聊天是「先聊清楚，再拆」：默认走沟通模式，拆解契约只在真正要拆的那一轮才发。
# 一上来就把任务表格式、六段式描述和依赖分层塞进提示词，执行器会急着出方案，
# 而这时候用户往往还在补需求背景。
PLANNING_MODE_DISCUSSION = "discussion"
PLANNING_MODE_BREAKDOWN = "breakdown"

# 模型主动引导时的固定问法。桥接靠这句话判断「已经问过要不要拆」，
# 用户下一轮回一句「好」才能接上，所以改措辞时两边要一起改。
BREAKDOWN_INVITE_MARK = "是否现在梳理并拆解任务"
BREAKDOWN_INVITE_QUESTION = f"需求已经聊得差不多了，{BREAKDOWN_INVITE_MARK}？"

# 用户主动要拆解的说法。宁可放宽也不要漏：误判进拆解模式时，那一轮提示词里的
# 「用户其实只是在补充需求就继续沟通」兜得住；漏判则要用户再说一遍。
_BREAKDOWN_REQUEST_PATTERN = re.compile(
    r"拆解|拆分|拆成|拆一下|拆吧|拆任务|拆出|梳理任务|梳理并拆|梳理一下任务|生成任务|创建任务|建任务|出任务"
    r"|任务清单|任务列表|任务计划|排任务|break\s*down|split.*task",
    re.IGNORECASE,
)

# 承接上一轮引导的短肯定。只在模型问过之后才认，而且必须是一句纯应答，
# 免得把「可以先不管权限」这种带肯定词的补充说明当成同意拆解。
_AFFIRMATIVE_PATTERN = re.compile(
    r"^(好|好的|好呀|好吧|可以|行|嗯|是|是的|对|开始|开始吧|继续|来吧|走起|同意|没问题|确认|ok|okay|yes|y|sure)"
    r"[的吧呀啊了呗\s，,。.!！~]*$",
    re.IGNORECASE,
)


def planning_round_mode(
    message: str,
    confirm_write: bool = False,
    previous_mode: str = "",
    invited: bool = False,
) -> str:
    """这一轮是继续聊需求，还是该给拆解方案。

    默认聊：需求刚起头时用户还在补背景，先塞一整套拆解契约只会让执行器急着出任务表。
    用户自己开口要拆、或者接住了上一轮的引导，才切到拆解模式；切过去之后不再退回来。
    """
    if confirm_write or previous_mode == PLANNING_MODE_BREAKDOWN:
        return PLANNING_MODE_BREAKDOWN
    text = str(message or "").strip()
    if _BREAKDOWN_REQUEST_PATTERN.search(text):
        return PLANNING_MODE_BREAKDOWN
    if invited and _AFFIRMATIVE_PATTERN.match(text):
        return PLANNING_MODE_BREAKDOWN
    return PLANNING_MODE_DISCUSSION


def planning_invite_offered(reply: str) -> bool:
    """这一轮模型是不是已经问过「要不要开始拆」。"""
    return BREAKDOWN_INVITE_MARK in str(reply or "")


# 沟通轮和拆解轮共用的两组纪律：写入禁令是两边都得守的底线，
# 勘察边界则是「读到能说清落点就停」，跟这一轮出不出任务表无关。
PLANNING_NO_WRITE_LINES = [
    "禁止执行 create-task-board-tasks、create-task-board-stage、create-task-board-module，也不要借 HTTP 请求或手工改文件绕过任务面板写入限制；未确认前这些写入命令会被命令行直接拒绝。",
    "本轮的限制只针对任务面板数据：正式需求大纲保持只读；如果用户明确要求生成或更新独立的流程图、图表、HTML 或其他需求资产，允许写入当前需求文档目录，但只能写该目录。过程总结由桥接器写入插件安装目录。已授予项目工作目录及需求指定关联目录的只读勘察权限；可使用终端的只读命令和当前会话可用的读取工具列目录、搜索并读取代码、配置、技能和文档。某个可选读取工具不可用时，改用其他可用的只读工具继续勘察，不要因此停止。",
]

# 勘察纪律的正文在技能 SKILL.md 第 3 节，那份每轮都会加载；这里只指路，不再复述一遍。
PLANNING_SURVEY_LINES = [
    f"工作区勘察严格按 {PLANNING_SKILL} 技能 SKILL.md 第 3 节「勘察工作区现状」执行：读到能说清落点就停，"
    "同一份文件不要重复读，续聊不要重做首轮已经做过的勘察。",
]


def planning_discussion_lines() -> list[str]:
    """沟通轮只交代本轮的状态和边界；怎么聊的行为规范在技能里。

    「先给判断再问问题」「能自己查的不要问」「用户还在补背景就跟着聊」这几条不随轮次变化，
    正文放在 references/需求沟通.md，这里不再复述——两边各说一遍，等于每一次工具往返都为
    同一条规则付两份钱。这里只保留桥接器才知道的东西：本轮是哪种模式、写入权限、以及
    面板要靠原文匹配的那句引导语。
    """
    return [
        "这是交付任务面板的需求沟通会话。当前阶段的目标是和用户一起把这条需求聊清楚、聊完整，不是拆任务："
        "用户没提之前，不要输出任务表、执行层、依赖图或工期计划，也不要提示「确认并写入」。",
        f"本轮读 {PLANNING_SKILL} 技能的 SKILL.md 和 `references/需求沟通.md`，沟通轮怎么聊按那里的规则来；"
        "`references/任务拆解与写入.md`、`references/需求文档规范.md` 这一轮用不上，不要预读。",
        *PLANNING_NO_WRITE_LINES,
        *PLANNING_SURVEY_LINES,
        f"当需求的目标、范围边界、真实落点和验收口径都已经清楚、够开始拆任务了，在回复末尾主动问一句："
        f"「{BREAKDOWN_INVITE_QUESTION}」——这句按原文写，面板靠它识别引导；还没到这一步就不要问，先把缺的部分补齐。",
        f"用户本轮已经明确要求拆解、或者接住了上面这句引导，就读 {PLANNING_SKILL} 技能的 `references/任务拆解与写入.md`，"
        "按其中的规则给出可评审的拆解预览，并在结尾提示用户点「确认并写入」；不要再反问一遍才拆。",
    ]


def planning_temp_segment(value: str, fallback: str) -> str:
    """Return one readable, traversal-safe directory segment for planning drafts."""
    candidate = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(value or "").strip())
    candidate = re.sub(r"\s+", " ", candidate).strip(" .")
    return (candidate or fallback)[:80]


def planning_temp_document_path(requirement_name: str, requirement_key: str, thread_id: str) -> Path:
    """Return the plugin-local draft file for one requirement chat window."""
    requirement_segment = planning_temp_segment(requirement_name, requirement_key or "未命名需求")
    thread_segment = planning_temp_segment(thread_id, "待分配聊天窗口")
    return runtime.PLUGIN_ROOT / ".temp" / "requirements" / f"req_{requirement_segment}" / thread_segment / "temp.md"


# 过程摘要分两段存：首轮的全量预览是基线，之后每轮只追加增量。
# 续聊回合的回复本身就是增量（见 build_planning_follow_up_prompt 的输出规则），
# 整篇覆盖会把首轮那份全量预览冲掉，恢复上下文时就只剩最后一句改动了。
PLANNING_TEMP_BASELINE_MARK = "<!-- planning-baseline -->"
PLANNING_TEMP_BASELINE_END_MARK = "<!-- /planning-baseline -->"
PLANNING_TEMP_ROUNDS_MARK = "<!-- planning-rounds -->"
PLANNING_TEMP_ROUND_MARK = "<!-- round -->"
# 增量轮次留最近这些轮：再往前的改动早就被后面的轮次覆盖掉了，留着只会把恢复上下文撑大。
MAX_PLANNING_TEMP_ROUNDS = 12


def planning_temp_sections(path: Path) -> tuple[str, list[str]]:
    """把已落盘的过程摘要拆回「基线全量预览」和「历轮增量」。

    读不到、或者是本格式之前写下的旧文件时，基线一律按空处理：调用方会因此
    把这一轮当成新的基线整篇重写，比在一份认不出结构的文件后面接着追加要安全。
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return "", []
    baseline = ""
    if PLANNING_TEMP_BASELINE_MARK in text and PLANNING_TEMP_BASELINE_END_MARK in text:
        baseline = text.split(PLANNING_TEMP_BASELINE_MARK, 1)[1].split(PLANNING_TEMP_BASELINE_END_MARK, 1)[0].strip()
    rounds: list[str] = []
    if PLANNING_TEMP_ROUNDS_MARK in text:
        # 第一段是标记之前的说明文字或占位符，不是增量轮次。
        rounds = [
            block.strip()
            for block in text.split(PLANNING_TEMP_ROUNDS_MARK, 1)[1].split(PLANNING_TEMP_ROUND_MARK)[1:]
            if block.strip()
        ]
    return baseline, rounds


def write_planning_temp_summary(
    path: Path,
    requirement_name: str,
    requirement_key: str,
    thread_id: str,
    user_message: str,
    summary: str,
    incremental: bool = False,
) -> None:
    """Refresh one planning chat's process artifact, appending incremental rounds.

    `incremental=False`（首轮，或基线还没落盘）把本轮回复整篇写成新的基线；
    `incremental=True`（续聊轮）保留基线，只在后面追加这一轮的增量。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    baseline, rounds = planning_temp_sections(path) if incremental else ("", [])
    # 基线不在（旧格式文件、被人删过、或首轮就没写成）时，这一轮只能自己当基线，
    # 否则恢复上下文时拿到的是一串没有底稿的增量。
    if incremental and not baseline:
        incremental = False
    if incremental:
        rounds.append("\n".join([
            f"### 增量轮次 · {utc_now()}",
            "",
            "**本轮用户输入**",
            "",
            (user_message or "（无文字输入）").strip(),
            "",
            "**本轮 AI 增量**",
            "",
            (summary or "（本轮没有可保存的总结）").strip(),
        ]))
        dropped = max(0, len(rounds) - MAX_PLANNING_TEMP_ROUNDS)
        rounds = rounds[-MAX_PLANNING_TEMP_ROUNDS:]
    else:
        baseline = (summary or "（本轮没有可保存的总结）").strip()
        rounds = []
        dropped = 0
    round_body = (
        "\n\n".join(f"{PLANNING_TEMP_ROUND_MARK}\n{block}" for block in rounds)
        if rounds
        else "（暂无增量轮次，基线就是当前最新方案）"
    )
    body = "\n".join([
        "# 需求梳理过程摘要",
        "",
        "> 临时过程产物，仅用于接续本聊天窗口；不等同于最终需求文档。",
        "> 读法：先读「基线」，再按从旧到新的顺序把「后续增量」叠加上去，后面的结论覆盖前面的。",
        "",
        f"- 需求名称：{requirement_name or requirement_key or '未命名需求'}",
        f"- 需求键：{requirement_key or '未指定'}",
        f"- 聊天窗口 ID：{thread_id}",
        f"- 更新时间：{utc_now()}",
        f"- 增量轮次：{len(rounds)}" + (f"（更早的 {dropped} 轮已被后续轮次覆盖，不再保留）" if dropped else ""),
        "",
        "## 本轮用户输入",
        "",
        (user_message or "（无文字输入）").strip(),
        "",
        "## 基线（本会话首轮的完整内容）",
        "",
        PLANNING_TEMP_BASELINE_MARK,
        baseline,
        PLANNING_TEMP_BASELINE_END_MARK,
        "",
        "## 后续增量（从旧到新，逐轮叠加在基线之上）",
        "",
        PLANNING_TEMP_ROUNDS_MARK,
        round_body,
        "",
    ])
    temporary = path.with_suffix(".tmp")
    temporary.write_text(body, encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def delete_planning_temp_summary(path: Path) -> bool:
    """Delete only one managed planning draft after a successful confirmation."""
    managed_root = (runtime.PLUGIN_ROOT / ".temp" / "requirements").resolve()
    candidate = path.resolve(strict=False)
    try:
        candidate.relative_to(managed_root)
    except ValueError as exc:
        raise BridgeFailure("需求过程摘要路径超出插件临时目录") from exc
    if not path.is_file():
        return False
    path.unlink()
    return True


def planning_temp_rule_lines(temp_path: str, read_required: bool = False) -> list[str]:
    if not temp_path:
        return [
            "本轮过程总结由桥接器在聊天窗口建立后自动写入插件安装目录的 `.temp/requirements/req_<需求名称>/<聊天窗口ID>/temp.md`；"
            "不要为保存过程数据而创建或修改正式需求大纲。",
        ]
    read_rule = (
        "本轮是确认写入回合：写最终需求文档前必须完整读取这份过程总结。"
        if read_required
        else "正常连续对话直接使用当前聊天上下文，不要重复读取这个文件；只有会话被压缩、恢复后上下文缺失，"
        "或当前上下文已找不到此前确认结论时，才读取它作为恢复点。"
    )
    return [
        f"本聊天窗口的过程总结文件: `{temp_path}`（插件安装目录内，不属于项目正式交付文档）。"
        "它由「基线」加上按轮次追加的「后续增量」两段组成：读的时候先读基线，再从旧到新叠加增量，后面的结论覆盖前面的。",
        read_rule,
        "每轮结束后桥接器自动追加本轮内容，你不需要维护这个文件；"
        "也不要把过程记录、未确认方案或聊天流水写入正式需求大纲。",
    ]


def requirement_outline_rule_lines(outline_path: str, write_allowed: bool = False, temp_path: str = "") -> list[str]:
    """Keep the final outline immutable until the board grants write confirmation."""
    if not outline_path:
        return planning_temp_rule_lines(temp_path)
    if not write_allowed:
        return [
            f"最终需求大纲: `{outline_path}`（相对项目工作目录）。预览和讨论阶段它是只读的最终产物：存在时可读取其中已确认内容，"
            "不存在时也不得创建；只有用户点击「确认并写入」后才能改变它。",
            *planning_temp_rule_lines(temp_path),
        ]
    return [
        f"最终需求大纲: `{outline_path}`（相对项目工作目录）。用户已点击「确认并写入」，本轮必须把最终确认结果写入这个文件。",
        *planning_temp_rule_lines(temp_path, read_required=True),
        "写入前先完整读取既有最终大纲（如存在）和本聊天窗口的过程总结，把用户最终确认的方案合并成一份完整需求产物；"
        "过程聊天、被否决方案和未确认草稿不要带入最终文档。",
        "这份总结是分轮累积的：开始拆解之前的轮次是需求沟通，开始拆解那一轮给出完整方案，之后每轮只给增量。"
        "合并时以「基线 + 逐轮增量」的叠加结果为准，不要只按最后一轮的增量下结论，也不要把已经被后续轮次改掉的旧版本写进去。",
        "`temp.md` 只是候选材料，禁止整段复制或按聊天顺序改写。先分析每条信息是否直接有助于需求目标、交付范围、实现约束、"
        "关键业务规则、验收标准、测试准备或最终决策；只把有实际交付价值且已经确认的内容提炼成清晰、可执行、可验证的需求表述。",
        "合并重复内容，删除寒暄、反复确认、讨论过程、未采纳备选、无结论推演、工具日志和关于聊天本身的元信息。"
        "但不得借精简遗漏已确认的非目标、兼容性要求、异常与边界场景、风险约束或仍会影响交付的待确认问题。",
        # 一份大纲动辄上万字，每次确认写入都重写一遍就是每次多烧一份全文的输出 token，
        # 而且重写长文本时漏章节的概率比定点编辑更高。
        "写回的方式是**按章节定点编辑**：先读现有大纲（用户可能在面板上直接编辑过），定位到要改的章节，"
        "把本轮追加或调整的需求写进去，需要新增内容时追加新章节，本轮没聊到的章节一个字都不要动，"
        "只有用户明确要求删除的内容才能删。不要整篇重写，也禁止只把本轮追加的那段需求写进文件覆盖全篇，"
        "那等于把之前几轮的需求大纲整段丢掉。",
        "大纲用 Markdown 组织，开头先写一节「摘要」（三到五行说清需求目标、范围边界和验收口径，"
        "后续阶段靠它决定要不要往下钻，必须能独立读懂），其后至少包含：需求背景与目标、范围与不做的事、关键约束、勘察到的落点（真实模块/目录/接口）、任务拆解表（与预览一致）、验收标准、待确认问题。",
        "把大纲里任务表的最终状态同步成实际落库的那一版。",
    ]


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
    mention_context: list[str] | None = None,
    thread_id: str = "",
    mode: str = PLANNING_MODE_BREAKDOWN,
) -> str:
    """Give a project-level Codex turn the precise planner-tool contract and scope.

    需求聊天分三档：`mode=discussion` 只和用户把需求聊清楚，提示词里不带任何拆解契约；
    用户开口要拆（或接住模型的引导）之后才切到 `mode=breakdown`，出可评审的拆解预览；
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
    requirement = requirement or {}
    requirement_key = str(requirement.get("requirementKey") or "")
    temp_path = (
        planning_temp_document_path(str(requirement.get("name") or ""), requirement_key, thread_id).as_posix()
        if thread_id else ""
    )
    # 同一条需求可能被反复追问，已经拆出来的任务要显式列出来：
    # 不给这份清单，第二轮会把第一轮建过的任务再建一遍。
    requirement_items = [
        item
        for item in context.get("items") or []
        if requirement_key and str(item.get("requirementKey") or "") == requirement_key
    ]
    requirement_item_lines = [planning_item_line(item) for item in requirement_items[:100]]
    # 确认写入必然是拆解轮：面板只会在预览过之后才发这一轮。
    discussion = not write_allowed and mode == PLANNING_MODE_DISCUSSION
    mode_lines = (
        [
            # 需求大纲、独立资产、任务需求文档的规则下面都带着真实路径和开关逐条给出了，
            # 比 references/需求文档规范.md 更准，不要再让执行器把那份也读一遍。
            f"本轮用户已在任务面板点击「确认并写入」，请先读 {PLANNING_SKILL} 技能的 `references/任务拆解与写入.md`，再执行写入："
            f"把此前预览过的方案（含用户后续提出的修改）用 `{taskboard_command('create-task-board-tasks')}` 一次性提交。",
            "预览是分轮给出的：开始拆解那一轮给完整方案，之后每轮只给增量。提交前先把那份完整方案和后续各轮增量叠加成最终一份，"
            "以最后一次改动为准；上下文里已经找不到早期轮次时，读下面给出的过程总结文件核对，不要凭最后一轮的增量反推整份方案。",
            f"任务面板数据只能通过这个命令行写入：`{taskboard_command('<动作>')}`（参数用连字符，数组参数走 `--json`，内容长时先写文件再 `--json @文件`）。不要自己拼 HTTP 请求、也不要手工改文件来创建任务面板数据。",
            "可用动作：get-task-board-context、create-task-board-stage、create-task-board-module、create-task-board-tasks；"
            f"`{taskboard_command('actions')}` 可以打印全部动作和参数，拿不准时先看它。"
            "当前项目已确定，所有动作的 --program-id 一律传下面给出的项目表数值主键，不要传项目名称或项目编码。",
            *TASK_DESCRIPTION_RULE_LINES,
            "依赖仅表达真正的前置关系，"
            "depends_on 只能引用本轮新建的任务或下方「本需求已建任务」里的任务键，不要跨需求建立依赖。",
            "每个任务必须传 benefit_tags：用 1-3 个不超过 32 字的简短标签描述该任务完成后带来的收益或作用，不能留空，也不要把任务标题重复写成标签。",
            "任务负责人由写入命令从下面这条需求的主负责人自动继承：任务模型只能保存一位负责人，因此会使用需求的第一位主负责人；不要在任务数组中自行改写负责人。",
            "执行 create-task-board-tasks 时必须原样传入下面给出的 requirement_key 和 phase，让新任务挂回本需求并落在指定的起始阶段。",
            "用户已选择里程碑或模块时，将相同的 stage_key/module_key 传给 create-task-board-tasks 并不要自行改写；未选择时根据当前项目已有选项为每项任务分配归属。",
            "本需求已有任务列表在下方给出：只补齐缺少的部分，不要重建已经存在的任务；若本轮无需新建任务，直接说明原因。",
            "不重复创建与本需求已有任务语义相同的任务。完成后用简洁中文总结实际创建的里程碑、模块和任务。",
        ]
        if write_allowed
        else [
            f"这是交付任务面板的需求梳理会话，请遵循 {PLANNING_SKILL} 技能，并先读它的 `references/任务拆解与写入.md`（本轮的拆解规则都在那里）。"
            "本轮只做梳理和预览，禁止写入任何任务面板数据。",
            "本轮用户已经要求梳理并拆解任务。但读下来如果发现他其实只是在补充需求背景、并没有要方案，"
            "就继续按需求沟通回应，把还没聊清楚的部分问明白，不要硬凑一份任务表出来。",
            *PLANNING_NO_WRITE_LINES,
            "拆解前必须先勘察下方给出的项目工作目录：加载该目录下项目自己的开发技能（如 backend-development、web-development），读相关目录和现有实现，据此判断需求真正的落点。get-task-board-context 只给出面板侧上下文，不包含工程现状，不能拿它替代看代码。",
            "任务要落到勘察出的真实模块、目录或接口上，不要只按业务名词泛化出通用分层；工作区里找不到需求所指的模块时，先向用户说明并确认工作目录或范围，不要硬拆。",
            *PLANNING_SURVEY_LINES,
            "先把此前聊清楚的需求收拢成结论，再输出一份可评审的拆解预览：先用 Markdown 表格列出「序号 / 任务标题 / 收益标签 / 负责人 / 里程碑 / 模块 / 类型 / 前置依赖」，每项给 1-3 个简短收益或作用标签；负责人统一展示为该需求的第一位主负责人（未指定则标为未指派）；再在表格下方逐条给出完整任务说明，格式和详细程度与实际写入面板的任务说明一致，让用户在确认前就能看到执行器将拿到的原文。",
            *TASK_DESCRIPTION_RULE_LINES,
            "里程碑、模块、类型的取值只能来自下方给出的现有选项；预览里也要说明哪些是新建、哪些复用本需求已有任务。",
            "本需求已有任务列表在下方给出：预览里只列本轮打算新增的任务，不要重复已经存在的任务。",
            "回复结尾提示用户：确认无误后点击输入框旁的「确认并写入」按钮，需要调整就直接回复修改意见，本轮继续讨论不会写入任何数据。",
        ]
        if not discussion
        else planning_discussion_lines()
    )
    # 关掉「拆解成多条任务」时，整条需求只落一条任务：改动本来就不可分的小需求，拆开只会平添依赖和空跑。
    split_tasks = bool(requirement.get("splitTasks", True))
    split_lines = (
        []
        if split_tasks
        else (
            [
                "本需求已关闭「拆解成多条任务」：执行 create-task-board-tasks 时 tasks 数组只能包含一条覆盖整条需求的任务，"
                "该任务的 depends_on 传空数组；启用原型图时命令行自动追加的原型任务不计入这条限制。",
            ]
            if write_allowed
            else [
                "本需求已关闭「拆解成多条任务」：预览里只输出一条覆盖整条需求的任务，任务表只有一行，不要拆成多条，也不要用依赖把它串成多步。"
                "该任务的目标、范围和验收标准要覆盖整条需求。",
            ]
        )
    )
    prototype_enabled = bool(requirement.get("generatePrototype"))
    prototype_lines = (
        [
            "本需求已启用“拆解后生成原型图”。预览时必须在任务表的最后列出一条“生成需求原型图”任务，"
            "并说明它依赖本轮其余任务；确认写入时，执行 create-task-board-tasks 必须传 generate_prototype: true。"
            "命令行会自动创建并标识这条末尾任务，任务执行时将把图片保存到自身文档目录的 prototype/ 中。",
        ]
        if prototype_enabled else []
    )
    # 任务需求文档是任务级的唯一需求沉淀。单任务模式无需额外勾选预生成：
    # 唯一任务就是这条需求的交付载体，确认写入后必须直接收到完整需求文档。
    pre_generate_task_documents = bool(
        requirement.get("preGenerateTaskDocuments", requirement.get("generateTaskOutline", False))
    )
    task_document_required = pre_generate_task_documents or not split_tasks
    task_document_lines = (
        [
            "本需求已关闭“拆解成多条任务”：确认写入后，create-task-board-tasks 返回的唯一业务任务（prototypeTask=false）"
            "就是本条需求的交付载体。必须把本轮梳理出的完整需求文档直接创建或覆盖到该任务返回的 requirementDocumentPath"
            "（即 `doc/<moduleKey>/<itemKey>/文档.md`），不能只留在需求级大纲或任务数据库的简短说明中。",
            "这条规则不依赖“预生成任务需求文档”开关；若同时生成原型图，不要把需求正文写进 prototypeTask=true 的原型任务文档。",
            "正文需完整保留本轮已确认的需求背景与目标、范围与非目标、工程事实与落点、设计要求、验收标准、测试准备及待确认项；"
            "后续任务“梳理需求”和“动作执行”只读取并继续完善这一份文件。",
            "写完后在总结里列出唯一业务任务键和实际写入的需求文档路径。",
        ]
        if write_allowed and not split_tasks
        else [
            "本需求已启用“预生成任务需求文档”。create-task-board-tasks 返回每条任务的 moduleKey 和 itemKey 后，"
            "必须为本轮每条新建任务创建或覆盖 `doc/<moduleKey>/<itemKey>/文档.md`，一条任务一份，不能另建任务需求大纲。",
            "这份文件是后续任务“梳理需求”和“动作执行”共同读取的唯一需求文档；先写可实施初稿，"
            "再由梳理需求阶段基于真实代码增量校正和补全。",
            "初稿用 Markdown 组织，至少包含：任务目标、范围与不做的事、已知落点（真实模块/目录/接口）、实现要点、前置依赖、验收标准与待确认项。",
            "写完后在总结里列出实际写入的任务需求文档路径。",
        ]
        if write_allowed and pre_generate_task_documents
        else (
            [
                "本需求已关闭“拆解成多条任务”：确认写入后会为唯一业务任务直接写入完整需求文档；"
                "本轮仍处于预览，尚未取得任务键，先不要创建任务需求文档。",
            ]
            if not split_tasks
            else [
                "任务确认写入后，会为每条新建任务预生成 `doc/<moduleKey>/<itemKey>/文档.md` 作为需求梳理初稿；"
                "本轮只做预览，先不要创建这些文件。",
            ]
            if pre_generate_task_documents else []
        )
    )
    # 沟通轮不带任何拆解设置：这三段都是「怎么拆」的约束，需求还没聊清楚时提它们只会催着出方案。
    if discussion:
        split_lines = []
        prototype_lines = []
        task_document_lines = []
    # 正式大纲只在确认轮更新；预览轮由插件安装目录里的聊天级 temp.md 接续上下文。
    outline_path = requirement_outline_path_of(requirement_key).as_posix() if requirement_key else ""
    outline_lines = requirement_outline_rule_lines(outline_path, write_allowed, temp_path)
    document_lines = requirement_document_rule_lines(requirement_key)
    # 被 @ 的历史需求：只给大纲产物地址，读不读、读哪一段由执行器按需决定。
    references = requirement.get("references") or []
    reference_lines = (
        [
            "本需求在详情里 @ 引用了下面这些历史需求。它们各自的需求大纲产物地址已列出（相对项目工作目录）：",
            *(
                f"- {item.get('name') or item.get('requirementKey')}"
                f"（requirement_key: {item.get('requirementKey')}）: "
                f"`{requirement_outline_path_of(str(item.get('requirementKey'))).as_posix()}`"
                for item in references
            ),
            "这些文件不会随提示词发给你：需要参考时按上面的路径自行读取，并且只读与本需求真正相关的章节，不要为了凑上下文把它们整段搬进回复。",
            "文件不存在说明那条需求还没沉淀大纲：如实说明，不要臆造它的内容。",
            "引用只作为背景和既有约定的来源，本轮拆解的范围仍然只限当前这条需求。",
        ]
        if references else []
    )
    # 被 @ 的既有任务从当前项目目录重新解析，不能采信浏览器提交的任务标题或文档路径。
    item_references = requirement.get("itemReferences") or []
    items_by_key = {
        str(item.get("itemKey") or ""): item
        for item in context.get("items") or []
        if isinstance(item, dict) and str(item.get("itemKey") or "")
    }
    item_reference_lines: list[str] = []
    if item_references:
        item_reference_lines.append("本需求在详情里 @ 引用了下面这些既有任务。需要参考时先读取对应任务需求文档：")
        for reference in item_references:
            item_key = str(reference.get("itemKey") or "")
            item = items_by_key.get(item_key)
            if item is None:
                item_reference_lines.append(f"- {item_key}：当前项目中已找不到该任务，不能据此推断实现细节。")
                continue
            item_reference_lines.append(
                f"- {item.get('title') or item_key}（item_key: {item_key}）: "
                f"`{document_path_of(item)}`"
            )
        item_reference_lines.extend([
            "这些任务仅作为已有实现和约定的参考，不能改变本轮拆解范围或重复创建同一项工作。",
            "任务文档不存在时如实说明，不要臆造其中的实现细节。",
        ])
    instruction = [
        *mode_lines,
        *split_lines,
        *prototype_lines,
        *outline_lines,
        *document_lines,
        *reference_lines,
        *item_reference_lines,
        *(mention_context or []),
        *task_document_lines,
        "",
        f"项目 program_id: {program_id}",
        f"项目名称（仅供理解，不要作为参数）: {context.get('program', {}).get('name') or program_id}",
        workspace_instruction(workspace),
        f"需求键 requirement_key: {requirement_key or '未指定'}",
        f"任务起始阶段 phase: {requirement.get('startPhase') or 'requirement'}",
        *([] if discussion else [
            f"拆解成多条任务: {'是' if split_tasks else '否（只建一条任务）'}",
            f"预生成任务需求文档: {'是（单任务模式强制写入）' if not split_tasks else '是' if task_document_required else '否（由任务梳理阶段创建）'}",
            f"拆解后生成原型图: {'是' if prototype_enabled else '否'}",
        ]),
        f"需求名称: {requirement.get('name') or '未命名'}",
        f"主负责人: {requirement.get('owners') or '未指定'}",
        f"辅助人: {requirement.get('assistants') or '未指定'}",
        f"@ 引用的历史需求: {'、'.join(str(item.get('name') or item.get('requirementKey')) for item in references) or '无'}",
        "需求详细信息:",
        str(requirement.get("detail") or "（未填写）"),
        "",
        f"已选里程碑: {selected_stage or '未选择'}",
        f"已选模块: {selected_module or '未选择'}",
        f"任务类型偏好: {selected_kind or '由你判断'}",
        "现有里程碑:", *(stage_lines or ["- 无"]),
        "现有模块:", *(module_lines or ["- 无"]),
        "本需求已建任务:", *(requirement_item_lines or ["- 无"]),
        REQUIREMENT_SCOPE_RULE,
        "",
        "本上下文标记闭合之后的内容，是用户本轮输入的原文。",
    ]
    return wrap_bridge_context(instruction, message)


def planning_detail_digest(requirement: dict[str, Any] | None) -> str:
    """需求正文的指纹。续聊回合靠它判断用户是不是在面板上改过需求详情。"""
    detail = str((requirement or {}).get("detail") or "")
    return hashlib.sha256(detail.encode("utf-8")).hexdigest()


def planning_item_line(item: dict[str, Any]) -> str:
    """本需求已建任务在提示词里的那一行。"""
    return (
        f"- {item.get('itemKey')}: {item.get('title') or item.get('itemKey')}"
        f"（{item.get('phase') or '-'}/{item.get('status') or '-'}；收益：{'、'.join(item.get('benefitTags') or []) or '未标注'}）"
    )


def requirement_item_keys(context: dict[str, Any], requirement_key: str) -> list[str]:
    """当前需求下已经落库的任务键，顺序与提示词里的清单一致。"""
    if not requirement_key:
        return []
    return [
        str(item.get("itemKey") or "")
        for item in context.get("items") or []
        if str(item.get("requirementKey") or "") == requirement_key and item.get("itemKey")
    ]


def build_planning_follow_up_prompt(
    program_id: int,
    context: dict[str, Any],
    message: str,
    selected_stage: str = "",
    selected_module: str = "",
    selected_kind: str = "",
    requirement: dict[str, Any] | None = None,
    workspace: Path | None = None,
    mention_context: list[str] | None = None,
    include_detail: bool = False,
    thread_id: str = "",
    known_item_keys: list[str] | None = None,
    mode: str = PLANNING_MODE_BREAKDOWN,
) -> str:
    """同一条需求聊天的追加回合，只带会变的和丢不起的那几项。

    `mode=discussion` 时这一轮仍然只聊需求：不重发拆解契约，也不要求增量预览。

    首轮的角色说明、勘察纪律、文档目录纪律和现有里程碑/模块明细不再逐轮重发；这里保留三类：
    随轮次变化的（已选里程碑/模块、改动过的需求正文、本轮 @ 的实体）、
    被会话压缩掉就会出事的（本需求已建任务清单、需求大纲读写纪律），
    以及取值必须合法的现有里程碑/模块键。确认写入那一轮仍走 build_planning_prompt 全量。

    `known_item_keys` 是本会话此前轮次已经发过的任务键：给了就只列增量，
    整份清单留在会话历史里，不必每轮重发几十行。
    """
    requirement = requirement or {}
    requirement_key = str(requirement.get("requirementKey") or "")
    requirement_items = [
        item
        for item in context.get("items") or []
        if requirement_key and str(item.get("requirementKey") or "") == requirement_key
    ]
    # 已经发过整份清单的会话只补增量：任务清单是「不要重复建任务」这条约束的依据，
    # 但它逐轮重发就是每轮几十行的固定开销，而会话历史里那一份并不会消失。
    known = [str(key) for key in known_item_keys or [] if str(key)]
    incremental_items = bool(known)
    listed_items = (
        [item for item in requirement_items if str(item.get("itemKey") or "") not in set(known)]
        if incremental_items
        else requirement_items
    )
    requirement_item_lines = [planning_item_line(item) for item in listed_items[:100]]
    if not incremental_items:
        item_lines = ["本需求已建任务:", *(requirement_item_lines or ["- 无"])]
    elif requirement_item_lines:
        item_lines = [
            f"本需求已建任务（本会话此前轮次已列过 {len(known)} 条，这里只补新增的部分）:",
            *requirement_item_lines,
        ]
    else:
        item_lines = [
            f"本需求已建任务: 与此前轮次给出的那 {len(known)} 条相同，没有新增。",
        ]
    stage_keys = [str(item.get("stageKey") or "") for item in context.get("stages") or [] if item.get("stageKey")]
    module_keys = [str(item.get("moduleKey") or "") for item in context.get("modules") or [] if item.get("moduleKey")]
    split_tasks = bool(requirement.get("splitTasks", True))
    outline_path = requirement_outline_path_of(requirement_key).as_posix() if requirement_key else ""
    document_directory = requirement_document_directory_of(requirement_key).as_posix() if requirement_key else ""
    temp_path = (
        planning_temp_document_path(str(requirement.get("name") or ""), requirement_key, thread_id)
        if thread_id else None
    )
    # 正式大纲在预览期不落盘；由聊天级 temp.md 接管压缩后的上下文。
    temp_written = bool(temp_path and temp_path.is_file())
    detail_lines = (
        [
            "需求详细信息（用户已在面板上改过，以这一版为准）:"
            if include_detail
            else "需求详细信息（聊天过程摘要尚未落盘，这里重发一份，避免会话压缩后丢失）:",
            str(requirement.get("detail") or "（未填写）"),
        ]
        if include_detail or not temp_written
        else [
            "需求详细信息与本会话首轮给出的一致，没有变化；"
            f"若上下文里已经找不到，就去读 `{temp_path.as_posix()}` 里的上一轮总结。",
        ]
    )
    # 沟通轮不提原型任务：它是任务表里的一行，属于拆解契约。
    prototype_lines = (
        [
            "本需求已启用“拆解后生成原型图”：预览的任务表最后必须固定列出一条“生成需求原型图”任务，"
            "并说明它依赖本轮其余任务。",
        ]
        if bool(requirement.get("generatePrototype")) and mode != PLANNING_MODE_DISCUSSION else []
    )
    document_lines = (
        [
            f"需求文档目录: `{document_directory}/`（`需求大纲.md` 是主文档）。用户要独立流程图、图表、HTML 等资产时，"
            "写成该目录下的独立文件，不要写进 `.codex/visualizations`、临时目录或工作区外路径；除该目录外不修改工作区其他文件。",
        ]
        if document_directory else []
    )
    discussion = mode == PLANNING_MODE_DISCUSSION
    mode_lines = [
        "这是同一条需求沟通会话的追加回合：本轮仍然只聊需求，"
        "禁止执行 create-task-board-tasks、create-task-board-stage、create-task-board-module，也不要绕过任务面板写入限制；"
        "用户没提之前不要输出任务表、执行层或依赖图。",
        "首轮已经交代过角色、勘察纪律和沟通方式，这里不再重复，按本会话已确认的约定继续。",
        "勘察也不要从头再来：首轮已经看过的目录和文件不用重读，结论直接沿用，只补这一轮真正还缺的那部分事实。",
        "接着上一轮的结论往下聊：已经聊清楚的部分不要再复述一遍，只处理用户本轮提出的这部分，"
        "并说清它对需求范围、技术落点或验收口径的影响；还需要用户定的，一次问最关键的 1-3 个。",
        f"需求的目标、范围边界、真实落点和验收口径都已经清楚时，在回复末尾主动问一句："
        f"「{BREAKDOWN_INVITE_QUESTION}」——这句按原文写；还没到这一步就继续把缺的部分补齐。",
        f"用户本轮已经明确要求拆解、或者接住了上一轮这句引导，就读 {PLANNING_SKILL} 技能的 `references/任务拆解与写入.md`，"
        "按其中的规则给出可评审的拆解预览，并在结尾提示用户点「确认并写入」。",
    ] if discussion else [
        "这是同一条需求拆解会话的追加回合：需求和梳理模式都没有变化，本轮仍然只做梳理和预览，"
        "禁止执行 create-task-board-tasks、create-task-board-stage、create-task-board-module，也不要绕过任务面板写入限制。",
        "首轮已经交代过角色、技能、勘察纪律和输出格式，这里不再重复，按本会话已确认的约定继续；"
        "拆解结论仍然要落在工作目录里的真实模块、目录或接口上。",
        "勘察也不要从头再来：首轮已经看过的目录和文件不用重读，结论直接沿用，只补这一轮真正还缺的那部分事实。",
        # 追加回合最贵的不是提示词，是回复：把整份预览连同每条 300-1500 字的任务说明再打一遍，
        # 既要花本轮的输出，又会留在会话历史里，让后面每一轮都跟着重读一遍。
        "输出增量，不要重印整份预览：任务表里只列本轮新增或被改动的任务行（表头沿用首轮的"
        "「序号 / 任务标题 / 收益标签 / 负责人 / 里程碑 / 模块 / 类型 / 前置依赖」，并在序号后标明「新增」或「修改」），"
        "表格下方也只给这几条的完整任务说明，详细程度与首轮一致（目标、真实落点、改动分条、边界、依赖与输入、验收标准）。",
        "没有改动的任务用一行「其余 N 条不变」带过，不要重复它们的标题、说明或表格行；"
        "被删掉的任务单独用一行写清楚。用户明确要求「再给一份完整预览」时才整份重印。",
        "最新的完整方案 = 开始拆解那一轮的全量预览 + 后续各轮增量，桥接器已经把它们逐轮存进本聊天窗口的过程总结文件，"
        "确认写入那一轮会据此合并，所以这里不必为了留档而重复。",
        "回复结尾照旧提示用户：确认无误后点「确认并写入」，要调整就直接回复修改意见。",
    ]
    lines = [
        *mode_lines,
        workspace_instruction(workspace),
        f"项目 program_id: {program_id}",
        f"需求键 requirement_key: {requirement_key or '未指定'}",
        f"需求名称: {requirement.get('name') or '未命名'}",
        *([] if discussion else [
            f"拆解成多条任务: {'是' if split_tasks else '否（预览里只输出一条覆盖整条需求的任务，不要拆分也不要串依赖）'}",
        ]),
        *requirement_outline_rule_lines(outline_path, False, temp_path.as_posix() if temp_path else ""),
        *document_lines,
        *prototype_lines,
        *detail_lines,
        f"已选里程碑: {selected_stage or '未选择'}",
        f"已选模块: {selected_module or '未选择'}",
        f"任务类型偏好: {selected_kind or '由你判断'}",
        f"现有里程碑键（取值只能从中选）: {'、'.join(stage_keys) or '无'}",
        f"现有模块键（取值只能从中选）: {'、'.join(module_keys) or '无'}",
        *(mention_context or []),
        *item_lines,
        REQUIREMENT_SCOPE_RULE,
        "上面列的是本需求已经建好的任务：聊需求时可以据此说明哪些工作已经排过，不要重复提议。"
        if discussion
        else "预览里只列本轮打算新增的任务，不要重复上面已经存在的任务。",
        (
            f"本会话如果被压缩过，上面提到的需求背景和既往结论你在上下文里可能已经找不到："
            f"先读 `{temp_path.as_posix()}` 把上下文接回来再动手，不要凭印象往下接。"
            if temp_path
            else "本会话如果被压缩过，先向用户确认需求背景和既往结论，不要凭印象往下接。"
        ),
        "本上下文标记闭合之后的内容，是用户本轮输入的原文。",
    ]
    return wrap_bridge_context(lines, message)
