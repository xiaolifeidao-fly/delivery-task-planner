"""一条需求花了多少：需求侧会话 + 它下面每条任务，按执行器分开算。

线程清单来自任务面板（需求会话表和任务的执行会话表），用量来自执行器自己的
会话缓存；这一层只做「把线程归到需求或任务名下，再按执行器加起来」。

需求侧不是一整块：需求分析、拆解、原型、review、需求测试、微调各算各的——它们是需求窗口里
几个独立的入口，「这条需求贵在哪一步」只有分块列出来才答得上。

按需求整体算一次，顺带就把每条任务的分账算出来了，所以进度页和「消耗」按钮
共用同一个返回，不必为每条任务各问一次。
"""

from __future__ import annotations

import sys
from typing import Any

import server as planner

from delivery_bridge.errors import BridgeFailure
from delivery_bridge.item_keys import (
    REQUIREMENT_ANALYSIS_SESSION_KIND,
    REQUIREMENT_FINE_TUNING_SESSION_KIND,
    REQUIREMENT_REVIEW_SESSION_KIND,
)
from delivery_bridge.payloads import (
    assert_runtime_project,
    config_biz_line,
    request_scoped_config,
    session_kind_of,
)
from delivery_bridge.providers import executor_purpose_of, program_id_of
from delivery_bridge.timeutil import utc_now
from delivery_bridge.token_usage import merge_usage
from delivery_bridge.usage_index import empty_provider_usage, usage_by_provider

# 一条需求下的任务再多，也不至于要为几百条任务逐个问会话表；超过就先算前面这些。
MAX_USAGE_TASKS = 200

# 需求侧会话分成这几块，顺序就是面板上从上到下的顺序（跟需求窗口的入口顺序一致）。
REQUIREMENT_SESSION_GROUPS = ("analysis", "planning", "prototype", "review", "testing", "fineTuning")

# 原型会话跟拆解共用一张表，靠执行器类型的用途后缀区分，不是靠 metadata.kind——
# 早期的原型会话没写 kind，只有这个后缀一直都在。
PROTOTYPE_PURPOSE = "prototype"


def _planning_session_group(row: dict[str, Any]) -> str:
    """拆解会话表里的一行归哪块：原型带用途后缀，历史上落到这张表的 review 按 review 认。"""
    if executor_purpose_of(row) == PROTOTYPE_PURPOSE:
        return "prototype"
    return "review" if session_kind_of(row) == REQUIREMENT_REVIEW_SESSION_KIND else "planning"


def _testing_session_group(row: dict[str, Any]) -> str:
    """测试会话表装了四块，靠 metadata.kind 分流；老数据没写 kind，按需求测试算。"""
    kind = session_kind_of(row)
    if kind == REQUIREMENT_REVIEW_SESSION_KIND:
        return "review"
    if kind == REQUIREMENT_FINE_TUNING_SESSION_KIND:
        return "fineTuning"
    if kind == REQUIREMENT_ANALYSIS_SESSION_KIND:
        return "analysis"
    return "testing"


class UsageMixin:
    def _requirement_session_threads(
        self, config: dict[str, Any], program_id: int, requirement_key: str,
    ) -> dict[str, list[str]]:
        """需求侧的线程按块归类。

        需求侧的会话落在两张表里：拆解和原型在拆解会话表，review、需求测试、微调在
        测试会话表。两张都要读——少读一张不只是缺一块分账，需求总账也会跟着少算。
        """
        groups: dict[str, list[str]] = {group: [] for group in REQUIREMENT_SESSION_GROUPS}
        # 同一条线程只算一块：两张表都登记过它的话，重复计入会让需求总账凭空多一份。
        seen: set[str] = set()
        for path, classify in (
            ("/delivery/requirement/planning-sessions", _planning_session_group),
            ("/delivery/requirement/testing-sessions", _testing_session_group),
        ):
            rows = planner.request_api(
                config, "GET", path,
                query={"programId": program_id, "requirementKey": requirement_key},
            ) or []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                thread_id = str(row.get("threadId") or "").strip()
                if thread_id and thread_id not in seen:
                    seen.add(thread_id)
                    groups[classify(row)].append(thread_id)
        return groups

    def _task_thread_ids(self, config: dict[str, Any], program_id: int, item_key: str) -> list[str]:
        """一条任务的全部线程：各阶段的执行会话、测试用例会话、任务微调都算。"""
        rows = planner.request_api(
            config, "GET", "/delivery/item/execution-session",
            query={"programId": program_id, "itemKey": item_key},
        ) or []
        thread_ids: list[str] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            thread_ids.append(str(row.get("externalSessionId") or "").strip())
            metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
            for entry in metadata.get("conversations") or []:
                if isinstance(entry, dict):
                    thread_ids.append(str(entry.get("threadId") or "").strip())
        return [thread_id for thread_id in thread_ids if thread_id]

    def requirement_usage(
        self,
        program_id: int,
        requirement_key: str,
        biz_line: str = "",
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """这条需求到目前为止的消耗，按执行器分开，并给出每条任务的分账。"""
        program_id = program_id_of(program_id)
        requirement_key = str(requirement_key or "").strip()
        if not requirement_key or len(requirement_key) > 64:
            raise BridgeFailure("缺少或无效的需求标识")
        config = request_scoped_config(config, biz_line, program_id)
        assert_runtime_project(config, program_id)
        context = planner.project_context(config, program_id)
        items = [
            item for item in context.get("items") or []
            if isinstance(item, dict) and str(item.get("requirementKey") or "").strip() == requirement_key
        ]
        session_threads = self._requirement_session_threads(config, program_id, requirement_key)
        conversation_groups = {group: usage_by_provider(threads) for group, threads in session_threads.items()}
        conversations = _sum_provider_usage(list(conversation_groups.values()))
        tasks: list[dict[str, Any]] = []
        for item in items[:MAX_USAGE_TASKS]:
            item_key = str(item.get("itemKey") or "").strip()
            if not item_key:
                continue
            try:
                usage = usage_by_provider(self._task_thread_ids(config, program_id, item_key))
            except Exception as exc:
                # 单条任务的会话表读不回来不该让整份分账失败：那条按零算，其余照常给。
                print(f"读取任务会话用量失败：{program_id}/{item_key}: {exc}", file=sys.stderr, flush=True)
                usage = empty_provider_usage()
            tasks.append(
                {
                    "itemKey": item_key,
                    "title": str(item.get("title") or item_key),
                    "phase": str(item.get("phase") or "requirement"),
                    "status": str(item.get("status") or "todo"),
                    "usage": usage,
                }
            )
        return {
            "bizLine": config_biz_line(config),
            "programId": program_id,
            "requirementKey": requirement_key,
            # 需求总账 = 需求侧会话 + 每条任务；面板上那颗按钮显示的就是它。
            "usage": _sum_provider_usage([conversations, *(task["usage"] for task in tasks)]),
            "conversations": conversations,
            # 需求会话再按块拆开。没跑过的块也留一行零值：面板要能回答「review 一分没花」，
            # 少一行会被当成漏算。
            "conversationGroups": [
                {
                    "key": group,
                    "threads": len(session_threads[group]),
                    "usage": conversation_groups[group],
                }
                for group in REQUIREMENT_SESSION_GROUPS
            ],
            "tasks": tasks,
            "updatedAt": utc_now(),
        }


def _sum_provider_usage(groups: list[dict[str, Any]]) -> dict[str, Any]:
    result = empty_provider_usage()
    for provider in result:
        result[provider] = merge_usage([group.get(provider) for group in groups if isinstance(group, dict)])
    return result
