"""每个回合烧掉多少 token，两家执行器收敛成同一份形状。

面板要回答的是「这条任务花了多少」，所以两边都归一到同一组字段：

- ``inputTokens``：本回合送进模型的全部输入，**含**命中缓存的那部分。
- ``cachedInputTokens``：其中命中提示缓存的部分（计价通常只有一折）。
- ``outputTokens``：模型生成的全部输出，含推理 token。
- ``reasoningOutputTokens``：输出里属于推理的部分，问不出来时留 0。
- ``totalTokens``：输入 + 输出。
- ``costUsd``：执行器自己算出来的钱，只有 Claude 给，Codex 侧为 None。

两家给的原始口径不一样，差异都在这一层抹平：Codex 的 ``inputTokens`` 本来就把
缓存命中算在内，Claude 则把三段（新输入、写缓存、读缓存）分开列，得自己加起来。
"""

from __future__ import annotations

from typing import Any

from .context_window import turn_context, turns_context

USAGE_FIELDS = ("inputTokens", "cachedInputTokens", "outputTokens", "reasoningOutputTokens", "totalTokens")


def empty_usage() -> dict[str, Any]:
    return {field: 0 for field in USAGE_FIELDS} | {"costUsd": None}


def _int(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def has_usage(usage: Any) -> bool:
    """全零的用量等于没测到，不值得往面板上放一行 0。"""
    return isinstance(usage, dict) and any(_int(usage.get(field)) for field in USAGE_FIELDS)


def codex_usage_breakdown(breakdown: Any) -> dict[str, Any]:
    """`thread/tokenUsage/updated` 里的一段 TokenUsageBreakdown。"""
    if not isinstance(breakdown, dict):
        return empty_usage()
    usage = {field: _int(breakdown.get(field)) for field in USAGE_FIELDS}
    usage["costUsd"] = None
    # totalTokens 服务端给了就用它的，缺了自己补，别让面板显示成 0。
    if not usage["totalTokens"]:
        usage["totalTokens"] = usage["inputTokens"] + usage["outputTokens"]
    return usage


def codex_turn_usage(params: Any, base: Any = None) -> dict[str, Any]:
    """把一条 Codex 用量通知折算成「本回合到目前为止」的用量。

    Codex 报两个数：``last`` 是最近一次模型请求，``total`` 是整条线程累计。
    一个回合里模型会被请求很多次（每次工具往返都是一次），所以 ``last`` 不等于
    回合用量；回合用量只能拿 ``total`` 去减这一轮开始前的累计值。

    ``base`` 是这一轮开始前的线程累计值。第一次收到通知时还没有 base，
    用 ``total - last`` 反推：那时的 total 恰好只多了本轮第一次请求。
    """
    payload = params.get("tokenUsage") if isinstance(params, dict) else None
    if not isinstance(payload, dict):
        return empty_usage()
    total = codex_usage_breakdown(payload.get("total"))
    last = codex_usage_breakdown(payload.get("last"))
    if not isinstance(base, dict) or not has_usage(base):
        base = {field: max(0, total[field] - last[field]) for field in USAGE_FIELDS}
    usage = {field: max(0, total[field] - _int(base.get(field))) for field in USAGE_FIELDS}
    usage["costUsd"] = None
    return usage


def codex_usage_base(params: Any) -> dict[str, Any]:
    """这一轮开始前的线程累计值，`codex_turn_usage` 的减数，记在回合上重复使用。"""
    payload = params.get("tokenUsage") if isinstance(params, dict) else None
    if not isinstance(payload, dict):
        return empty_usage()
    total = codex_usage_breakdown(payload.get("total"))
    last = codex_usage_breakdown(payload.get("last"))
    return {field: max(0, total[field] - last[field]) for field in USAGE_FIELDS} | {"costUsd": None}


def codex_turn_context(params: Any, model: str = "") -> dict[str, Any]:
    """同一条用量通知里还带着「现在占了多少上下文」。

    ``last`` 是最近一次模型请求：Responses API 每次都要把整段对话重新送进去，
    所以它的输入就是此刻的上下文，加上这次的输出就是下一次请求的起点。
    窗口大小由 app-server 按当前模型给出（``modelContextWindow``），比查表准。
    """
    payload = params.get("tokenUsage") if isinstance(params, dict) else None
    if not isinstance(payload, dict):
        return turn_context(0, 0, "codex", model)
    last = codex_usage_breakdown(payload.get("last"))
    return turn_context(last["totalTokens"], payload.get("modelContextWindow"), "codex", model)


def claude_turn_usage(event: Any) -> dict[str, Any]:
    """Claude 的 stream-json `result` 事件；它一条就是整轮的合计。"""
    if not isinstance(event, dict):
        return empty_usage()
    raw = event.get("usage") if isinstance(event.get("usage"), dict) else {}
    cached = _int(raw.get("cache_read_input_tokens"))
    # Claude 把新输入、写缓存、读缓存分三个字段报；面板要的是「这轮送进去多少」，
    # 所以三段相加才是 inputTokens，cachedInputTokens 只是其中命中缓存的那段。
    input_tokens = _int(raw.get("input_tokens")) + _int(raw.get("cache_creation_input_tokens")) + cached
    output_tokens = _int(raw.get("output_tokens"))
    details = raw.get("output_tokens_details") if isinstance(raw.get("output_tokens_details"), dict) else {}
    cost = event.get("total_cost_usd")
    return {
        "inputTokens": input_tokens,
        "cachedInputTokens": cached,
        "outputTokens": output_tokens,
        "reasoningOutputTokens": _int(details.get("thinking_tokens")),
        "totalTokens": input_tokens + output_tokens,
        "costUsd": float(cost) if isinstance(cost, (int, float)) and cost > 0 else None,
    }


def claude_message_context(event: Any, window_tokens: Any = 0, model: str = "") -> dict[str, Any]:
    """Claude 的 stream-json `assistant` 事件；一条就是一次模型请求。

    这次请求的输入（新输入 + 写缓存 + 读缓存）就是此刻的上下文，加上它的输出
    正好是下一次请求要带的量——和 Codex 侧的口径对齐。

    子代理（Task）的消息带 ``parent_tool_use_id``，它跑在自己那份小上下文里，
    算进来会让主会话的读数无缘无故掉下去，所以整条跳过。
    """
    if isinstance(event, dict) and event.get("parent_tool_use_id"):
        return turn_context(0, window_tokens, "claude", model)
    message = event.get("message") if isinstance(event, dict) else None
    raw = message.get("usage") if isinstance(message, dict) and isinstance(message.get("usage"), dict) else {}
    used = (
        _int(raw.get("input_tokens"))
        + _int(raw.get("cache_creation_input_tokens"))
        + _int(raw.get("cache_read_input_tokens"))
        + _int(raw.get("output_tokens"))
    )
    name = str((message or {}).get("model") or model or "")
    return turn_context(used, window_tokens, "claude", name)


def claude_context_window(event: Any, model: str = "") -> int:
    """`result` 事件里 Claude 自己报的窗口大小，按模型分列，比查表准。

    一轮里可能不止一个模型（子代理、起标题都会用小模型），所以按主模型取；
    取不到就退回最大的那个——主模型的窗口不会比顺带用过的小模型更小。
    """
    usage = event.get("modelUsage") if isinstance(event, dict) else None
    if not isinstance(usage, dict) or not usage:
        return 0
    entry = usage.get(model) if model else None
    if isinstance(entry, dict) and _int(entry.get("contextWindow")):
        return _int(entry["contextWindow"])
    return max((_int(value.get("contextWindow")) for value in usage.values() if isinstance(value, dict)), default=0)


def merge_usage(usages: Any) -> dict[str, Any]:
    """把若干回合的用量加成一条任务的合计。"""
    total = empty_usage()
    cost = 0.0
    counted = False
    for usage in usages or []:
        if not isinstance(usage, dict):
            continue
        for field in USAGE_FIELDS:
            total[field] += _int(usage.get(field))
        value = usage.get("costUsd")
        if isinstance(value, (int, float)) and value > 0:
            cost += float(value)
            counted = True
    total["costUsd"] = round(cost, 6) if counted else None
    return total


def turns_usage_total(turns: Any) -> dict[str, Any]:
    """一条会话里所有回合的合计；面板的「本任务累计消耗」就是它。"""
    if not isinstance(turns, list):
        return empty_usage()
    return merge_usage(turn.get("usage") for turn in turns if isinstance(turn, dict))


def with_usage(payload: dict[str, Any]) -> dict[str, Any]:
    """给一份带 `turns` 的会话返回补上这条会话的用量合计和当前上下文占用。

    面板每种会话（任务、需求分析、需求拆解、需求原型、review、测试、微调）都有自己的返回结构，
    但都带同一份 `turns`；两个数都只依赖它，所以统一在出口补，不必在每处重复写一遍。
    """
    turns = payload.get("turns")
    return {**payload, "usage": turns_usage_total(turns), "context": turns_context(turns)}
