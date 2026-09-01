"""发给执行器的提示词里那层「面板上下文」包装。

面板会往提示词里塞项目、任务、阶段、技能一大堆上下文，但聊天记录里只该回显
用户自己写的那几句；标记标签和包装规则集中在这里，读写两侧共用同一份定义。
"""

from __future__ import annotations

import re
from pathlib import Path

# 真正发给执行器的提示词里裹着一大段面板上下文，聊天记录里只留用户自己写的那几句。
# planning 是需求拆解会话的旧标记名，历史会话里还在，两个都要认。
BRIDGE_CONTEXT_TAG = "delivery-bridge-context"


def wrap_bridge_context(context_lines: list[str], spoken: str) -> str:
    """Put the board's assembled context behind a marker and leave the user's own words after it.

    面板会往提示词里塞项目、任务、阶段、技能一大堆上下文；那是给执行器看的，
    聊天记录里只该回显 `spoken`，也就是用户自己写的内容。
    """
    # 只带附件不写字也是一次有效的输入，补一句可见文案：空文本的条目会被整条丢掉。
    text = spoken.strip() or "请查看随附文件并继续处理。"
    return "\n".join([f"<{BRIDGE_CONTEXT_TAG}>", *context_lines, f"</{BRIDGE_CONTEXT_TAG}>", "", text])


def with_mention_context(message: str, mention_context: list[str]) -> str:
    """Wrap @-selected entities for an in-flight or follow-up turn only when needed."""
    return wrap_bridge_context(mention_context, message) if mention_context else message


def workspace_instruction(workspace: Path | None) -> str:
    """Point every phase at the project's bound working directory and its own dev skills.

    四个阶段（拆解、梳理、执行、测试）都得先看真实代码：面板返回的结构化上下文里没有工程现状，
    不点名工作目录和项目技能，执行器就会照着业务名词泛化出一套和仓库对不上的东西。
    """
    if not workspace:
        return "项目工作目录: 未提供。动手前先向用户确认代码仓库位置，不要拿当前目录或安装目录顶替。"
    return (
        f"项目工作目录（项目管理里为本项目绑定的代码仓库，也是本轮 cwd）: {workspace}。"
        "开始前先加载该目录下项目自己的开发技能（如 backend-development、web-development），"
        "并读相关目录和现有实现；结论要落在真实文件路径上，不要凭业务名词推演。"
    )


BRIDGE_CONTEXT_RE = re.compile(
    r"\n?<delivery-(?:bridge|planning)-context>.*?</delivery-(?:bridge|planning)-context>\n?",
    re.DOTALL,
)
