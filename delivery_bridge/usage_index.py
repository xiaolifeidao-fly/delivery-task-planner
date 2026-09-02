"""按线程查用量，并按执行器归类。

每轮的用量已经落在各自执行器的会话缓存里：Codex 在过程记录（journal）里，
Claude 在自己的会话正文（transcript）里。两边都是「一个线程一个文件」，
而且互不重叠——所以线程属于哪个执行器不必去问会话表，哪边有正文就算哪边的。

这么定的原因是会话表里的 executorType 是发起那一轮时选的工具，同一条线程
换过工具、或者老数据没写这个字段时都会对不上；正文在哪就是在哪，不会骗人。
"""

from __future__ import annotations

from typing import Any

from .clients.claude import CLAUDE_TRANSCRIPTS
from .clients.journal import THREAD_ITEMS
from .providers import AI_PROVIDERS
from .token_usage import empty_usage, merge_usage, turns_usage_total


def thread_usage(thread_id: str) -> tuple[str, dict[str, Any]]:
    """这条线程属于哪个执行器、烧了多少；两边都查不到就返回空。"""
    thread_id = str(thread_id or "").strip()
    if not thread_id:
        return "", empty_usage()
    for provider, turns in (("codex", THREAD_ITEMS.read(thread_id)), ("claude", CLAUDE_TRANSCRIPTS.read(thread_id))):
        if turns:
            return provider, turns_usage_total(turns)
    return "", empty_usage()


def empty_provider_usage() -> dict[str, Any]:
    """两家各一份 + 合计。执行器没跑过的那一家给零值，面板照样能画两行。"""
    return {provider: empty_usage() for provider in sorted(AI_PROVIDERS)} | {"total": empty_usage()}


def usage_by_provider(thread_ids: Any) -> dict[str, Any]:
    """一组线程按执行器分开合计，另给一份总和。"""
    collected: dict[str, list[dict[str, Any]]] = {provider: [] for provider in AI_PROVIDERS}
    seen: set[str] = set()
    for thread_id in thread_ids or []:
        key = str(thread_id or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        provider, usage = thread_usage(key)
        if provider:
            collected[provider].append(usage)
    result = {provider: merge_usage(usages) for provider, usages in collected.items()}
    result["total"] = merge_usage(result.values())
    return result
