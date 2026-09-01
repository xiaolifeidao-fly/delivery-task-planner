"""自由聊天的提示词，以及会话标题与需求名称的生成。

新建聊天首回合结束后用一轮短会话补标题。标题既用于需求，也用于任务会话；
保持简短，才能在左侧会话列表完整辨认。

开聊那一刻先用首条消息的前几个字占住需求名称：起名要跑一轮模型，最快也要几秒，
这几秒里面板上只能显示需求编号，用户看着就像没生效。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..attachments_text import text_without_attachment_context
from ..prompt_context import workspace_instruction, wrap_bridge_context
from .common import (
    PHASE_SKILLS,
    document_path_of,
    document_revision_rule,
    git_branch_lines,
    sibling_document_lines,
)

MAX_CONVERSATION_TITLE_CHARS = 30
# 开聊那一刻先用首条消息的前几个字占住需求名称：起名要跑一轮模型，最快也要几秒，
# 这几秒里面板上只能显示需求编号，用户看着就像没生效。占位名等 AI 起好名再换掉。
MAX_REQUIREMENT_PLACEHOLDER_CHARS = 10

# 新建聊天首回合结束后用一轮短会话补标题。标题既用于需求，也用于任务会话；
# 保持简短，才能在左侧会话列表完整辨认。
CONVERSATION_TITLE_TIMEOUT_SECONDS = 3 * 60
# 需求名称留空时仍复用同一套标题生成和清洗规则；保留旧函数名，避免插件扩展脚本失效。
REQUIREMENT_NAME_TIMEOUT_SECONDS = CONVERSATION_TITLE_TIMEOUT_SECONDS
MAX_REQUIREMENT_NAME_CHARS = MAX_CONVERSATION_TITLE_CHARS



def build_conversation_prompt(
    program_id: int,
    task: dict[str, Any],
    message: str,
    workspace: Path | None = None,
    requirement_documents: list[str] | None = None,
    mention_context: list[str] | None = None,
) -> str:
    """Start an independent Codex thread with enough task context to be useful."""
    dependencies = task.get("dependsOnItemKeys") or []
    phase = str(task.get("phase") or "requirement")
    document_path = document_path_of(task)
    document_directory = Path(document_path).parent.as_posix()
    design_directory = (Path(document_path).parent / "design").as_posix()
    return wrap_bridge_context(
        [
            "这是交付任务详情中发起的一条新 Codex 对话。请结合当前项目和任务上下文回应并执行用户的要求。",
            workspace_instruction(workspace),
            "该任务已由 HTTP 执行桥领取并绑定到当前会话。不要调用 claim_next_task、bind_task_execution_session、finish_execution_task 或其他任务状态流转工具；桥接器会根据本回合最终状态自动同步任务面板。",
            f"项目 program_id: {program_id}",
            f"任务键: {task.get('itemKey') or '未指定'}",
            f"任务标题: {task.get('title') or '未指定'}",
            f"任务说明: {task.get('description') or '无'}",
            f"当前执行阶段: {phase}",
            f"当前阶段对应技能: {PHASE_SKILLS.get(phase, '按任务当前阶段处理')}",
            f"需求文档路径: {document_path}（本任务唯一的需求文档，默认加载）。开始前请先读取此文件；梳理需求阶段应在此基础上更新。",
            f"任务需求文档目录: `{document_directory}/`，支持多份文档；`文档.md` 是主文档，独立任务说明使用独立文件名写在此目录。",
            f"任务设计文档目录: `{design_directory}/`，支持多份文档；需要交付独立设计说明时写入此目录，不要写入 `.codex/visualizations` 或其他工作区外路径。",
            document_revision_rule(document_path),
            f"阶段: {task.get('stageKey') or '未指定'}",
            f"模块: {task.get('moduleKey') or '未指定'}",
            f"前置任务: {', '.join(dependencies) if dependencies else '无'}",
            *sibling_document_lines(requirement_documents),
            *(mention_context or []),
            "如果生成了用户需要查看或下载的文件、文档或图片，请在最终回复中用 Markdown 链接列出其工作区相对路径。",
            "本上下文标记闭合之后的内容，是用户本轮输入的原文。",
        ],
        message,
    )


def build_conversation_title_prompt(user_message: str, reply: str) -> str:
    """根据新聊天的首条用户消息和首轮回复，生成一个面板会话标题。

    命名是面板内务，不能出现在用户可见的正式聊天里，也不应让模型读取代码或执行命令。
    """
    return wrap_bridge_context(
        [
            "这是交付任务面板的「聊天自动命名」回合：请根据一条新聊天的首轮沟通内容起标题。",
            "",
            "用户的首条需求或任务说明:",
            (user_message or "").strip() or "（用户本轮没有文字输入）",
            "",
            "AI 首轮回复（可能很长，只取其中的主旨）:",
            (reply or "").strip()[:4000] or "（本轮没有回复正文）",
            "",
            "要求:",
            f"- 只输出标题本身，一行，不超过 {MAX_CONVERSATION_TITLE_CHARS} 个字，用中文。",
            "- 标题要说清本次要做的事，能在聊天列表里被一眼认出，不要写成「需求」「任务」「优化」这类空话。",
            "- 回复正文可能还没生成，这时只按用户的说明起名，不要等也不要追问。",
            "- 不要引号、句号、序号、Markdown 记号，不要任何解释或前后缀。",
            "- 不要读代码、不要执行命令、不要修改任何文件，也不要调用任务面板命令。",
        ],
        "请为这条聊天起一个标题，只回标题本身。",
    )


def conversation_title_of(text: str) -> str:
    """把命名回合的回复收敛成一行标题：模型偶尔会带上引号、前缀或多余的说明。"""
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    # 「标题如下：」这类引导行不是标题，丢掉之后再取第一行。
    lines = [line for line in lines if not line.endswith((":", "："))] or lines
    if not lines:
        return ""
    candidate = re.sub(r"^[#>*\-\d.、\s]+", "", lines[0])
    candidate = re.sub(r"^(?:标题|聊天标题|会话标题|需求标题|需求名称|任务标题)\s*[:：]\s*", "", candidate)
    candidate = candidate.strip("*_`\"'“”‘’「」《》【】 \t").strip()
    candidate = candidate.rstrip("。.！!")
    return candidate[:MAX_CONVERSATION_TITLE_CHARS]


def build_requirement_name_prompt(user_message: str, reply: str) -> str:
    return build_conversation_title_prompt(user_message, reply)


def requirement_name_of(text: str) -> str:
    return conversation_title_of(text)


def placeholder_requirement_name(user_message: str) -> str:
    """用户首条消息的前几个字，去掉附件上下文和 Markdown 记号后取头一段。"""
    text = text_without_attachment_context(str(user_message or ""))
    text = re.sub(r"^[#>*\-\d.、\s]+", "", " ".join(text.split()))
    text = text.strip("*_`\"'“”‘’「」《》【】 \t")
    return text[:MAX_REQUIREMENT_PLACEHOLDER_CHARS].strip()
