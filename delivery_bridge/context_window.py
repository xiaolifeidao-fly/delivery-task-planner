"""这条会话还剩多少上下文，两家执行器收敛成同一份形状。

和 ``token_usage`` 的分工：那边算「这条会话一共烧了多少 token」，是一笔累加的账；
这边回答的是**此刻**模型手里拿着的那份提示词有多长——它会随着对话变长而涨，
压缩（compaction）之后又会掉回去，所以两个数没有加减关系，也不能互相换算。

面板每个对话窗口顶上要的就是这三个数：

- ``usedTokens``：最近一次模型请求占住的上下文（输入含缓存命中那段 + 这次的输出）。
- ``windowTokens``：这个模型的上下文窗口，也就是「总共多少」。
- ``remainingTokens`` / ``usedPercent``：由前两个算出来，面板不必各算一遍。

两家的来源不一样：Codex 在 ``thread/tokenUsage/updated`` 里就带
``modelContextWindow``；Claude 只有回合结束的 ``result`` 事件里才有
``modelUsage[model].contextWindow``，所以回合跑到一半时先按模型查表兜底，
等 ``result`` 到了再用执行器给的真值覆盖。
"""

from __future__ import annotations

from typing import Any

# 查不到真值时按模型兜底的窗口大小。这是「显示成多少」的下限，不是判据：
# 执行器报了 modelContextWindow 就一律以它为准。
CODEX_DEFAULT_CONTEXT_WINDOW = 272_000
# 面板上能选的两档（opus / sonnet）实测都开着 1M，所以 Claude 一侧默认按 1M 兜。
# 实测：claude -p --model opus|sonnet 的 result.modelUsage[模型].contextWindow 都是 1000000。
CLAUDE_DEFAULT_CONTEXT_WINDOW = 1_000_000
# 还停在 200K 的是 haiku 这类小模型：面板选不到它，但起标题、子代理会用上。
CLAUDE_SMALL_CONTEXT_WINDOW = 200_000
CLAUDE_SMALL_CONTEXT_MODELS = ("haiku",)
# 长上下文要显式开启的那些档位，模型名里带 [1m] 标记——带了就一定是 1M，不再按小模型算。
CLAUDE_LONG_CONTEXT_MARKER = "[1m]"

CONTEXT_FIELDS = ("usedTokens", "windowTokens")


def _int(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def default_context_window(provider: str, model: str = "") -> int:
    """执行器没报窗口时按模型猜一个；只用于显示，不参与任何决策。"""
    if str(provider or "").strip().lower() != "claude":
        return CODEX_DEFAULT_CONTEXT_WINDOW
    name = str(model or "").lower()
    if CLAUDE_LONG_CONTEXT_MARKER not in name and any(small in name for small in CLAUDE_SMALL_CONTEXT_MODELS):
        return CLAUDE_SMALL_CONTEXT_WINDOW
    return CLAUDE_DEFAULT_CONTEXT_WINDOW


def turn_context(used_tokens: Any, window_tokens: Any, provider: str = "", model: str = "") -> dict[str, Any]:
    """一个回合结束时的上下文占用，落在回合上，读会话时取最后一条。"""
    used = _int(used_tokens)
    window = _int(window_tokens) or default_context_window(provider, model)
    return {
        "usedTokens": used,
        "windowTokens": window,
        "provider": str(provider or ""),
        "model": str(model or ""),
    }


def has_context(context: Any) -> bool:
    """没测出占用的回合不值得覆盖前一轮的读数——0 会被面板画成「一格没用」。"""
    return isinstance(context, dict) and _int(context.get("usedTokens")) > 0


def empty_context() -> dict[str, Any]:
    """一轮都没跑过的会话：面板照样要显示「总共多少」，窗口大小由它自己按选中的模型补。"""
    return {"usedTokens": 0, "windowTokens": 0, "remainingTokens": 0, "usedPercent": 0.0, "provider": "", "model": ""}


def context_snapshot(context: Any) -> dict[str, Any]:
    """把回合上记的那份读数补成面板直接能画的形状。"""
    if not has_context(context):
        return empty_context()
    used = _int(context.get("usedTokens"))
    window = _int(context.get("windowTokens")) or default_context_window(str(context.get("provider") or ""), str(context.get("model") or ""))
    # 压缩前的那一刻可能超出窗口（执行器按上一轮的读数报），剩余不能为负。
    remaining = max(0, window - used)
    return {
        "usedTokens": used,
        "windowTokens": window,
        "remainingTokens": remaining,
        "usedPercent": round(min(100.0, used * 100 / window), 1) if window else 0.0,
        "provider": str(context.get("provider") or ""),
        "model": str(context.get("model") or ""),
    }


def turns_context(turns: Any) -> dict[str, Any]:
    """一条会话当前的上下文占用：最后一个测到读数的回合说了算。

    倒着找而不是取最后一轮：起标题这类内务回合、以及执行器没报用量的回合都没有读数，
    取最后一轮会把整条会话显示成「没用过上下文」。
    """
    if not isinstance(turns, list):
        return empty_context()
    for turn in reversed(turns):
        if isinstance(turn, dict) and has_context(turn.get("context")):
            return context_snapshot(turn["context"])
    return empty_context()
