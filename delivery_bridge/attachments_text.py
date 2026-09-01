"""聊天正文里那几层不可见标记的读写。

一条消息在磁盘上可能同时裹着三样东西：附件清单标记、面板上下文包装、
以及用户真正写的正文。展示、归档、再发给执行器时要的层次各不相同，
所以加标记和剥标记的规则集中放在这里，两侧共用同一份正则。
"""

from __future__ import annotations

import re
from typing import Any

from .prompt_context import BRIDGE_CONTEXT_RE

ATTACHMENT_MARKER_RE = re.compile(r"<!-- delivery-task-attachments:([A-Za-z0-9_-]+(?:,[A-Za-z0-9_-]+)*) -->")

ATTACHMENT_CONTEXT_RE = re.compile(r"\n?<delivery-task-attachments>.*?</delivery-task-attachments>", re.DOTALL)

def attachment_marker(attachments: list[dict[str, Any]]) -> str:
    attachment_ids = [str(attachment.get("id") or "") for attachment in attachments]
    return f"<!-- delivery-task-attachments:{','.join(attachment_ids)} -->" if attachment_ids else ""

def message_with_attachments(message: str, attachments: list[dict[str, Any]]) -> str:
    """Add file references for Codex without leaking bridge-only context into chat history.

    幂等：各条发送链路里，有的调用方自己先拼过附件段，客户端里还会再统一兜一次。
    已经带了附件标记的正文原样返回，避免同一批文件被描述两遍。
    """
    if ATTACHMENT_MARKER_RE.search(message):
        return message
    text = message.strip() or "请查看随附文件并继续处理。"
    if not attachments:
        return text
    lines = [text, "", "<delivery-task-attachments>", "本条消息随附了以下文件，已经保存到当前工作区，上面那段文字才是用户本轮真正的要求："]
    for attachment in attachments:
        name = str(attachment.get("name") or "附件")
        location = attachment.get("relativePath") or attachment.get("path")
        # 路径对每种附件都要给全：图片虽然可能同时作为图片输入传入，但不是每个执行器都支持，
        # 只写一句「已作为图片输入传入」而不给路径，执行器就只能自己去猜文件在哪。
        kind = "图片" if attachment.get("isImage") else "文件"
        lines.append(f"- {kind}：{name}，路径：{location}")
    lines.extend(["</delivery-task-attachments>", attachment_marker(attachments)])
    return "\n".join(lines)

def attachment_ids_from_text(text: str) -> list[str]:
    match = ATTACHMENT_MARKER_RE.search(text)
    return match.group(1).split(",") if match else []

def text_without_attachment_context(text: str) -> str:
    return ATTACHMENT_MARKER_RE.sub("", BRIDGE_CONTEXT_RE.sub("", ATTACHMENT_CONTEXT_RE.sub("", text))).strip()
