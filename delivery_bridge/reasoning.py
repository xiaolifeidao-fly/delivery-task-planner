"""推理摘要条目的取文与去重。

Codex 的推理摘要会分成好几段陆续回来，同一段还可能重复推送；
面板要的是「按出现顺序、去掉重复」的那一份，规则集中在这里。
"""

from __future__ import annotations

from typing import Any

def reasoning_summary_text(item: Any) -> str:
    """Return only the safe, user-visible summary from a Codex reasoning item.

    The app-server can emit a separate ``reasoning/textDelta`` stream, but that is
    not the display summary and must never be copied into the task board or its
    project-local chat archive. ``summary`` is the protocol field intended for
    explaining the model's reasoning to people.
    """
    if not isinstance(item, dict):
        return ""
    summary = item.get("summary")
    if isinstance(summary, str):
        return summary.strip()
    if not isinstance(summary, list):
        return ""
    parts = [part.strip() for part in summary if isinstance(part, str) and part.strip()]
    return "\n\n".join(parts)
