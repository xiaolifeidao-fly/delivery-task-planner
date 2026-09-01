"""执行器身份的规范化：哪个 AI、哪种用途、推理档位、快速模式。

面板和服务端传过来的是自由文本，这里统一收敛成受支持的取值，
不认识的一律落到默认值，绝不把原样字符串带进后面的分支判断。

推理档位两家不一样（Codex 最高 xhigh，Claude 最高 max），所以按 provider 分开校验。
"""

from __future__ import annotations

from typing import Any

from .errors import BridgeFailure

AI_PROVIDERS = {"codex", "claude"}

CODEX_REASONING_EFFORTS = {"minimal", "low", "medium", "high", "xhigh"}

CLAUDE_REASONING_EFFORTS = {"minimal", "low", "medium", "high", "max"}

def ai_provider_of(value: Any) -> str:
    provider = str((value or {}).get("provider") or "codex").strip().lower() if isinstance(value, dict) else str(value or "codex").strip().lower()
    if provider not in AI_PROVIDERS:
        raise BridgeFailure("AI 工具必须是 codex 或 claude")
    return provider

def provider_label(provider: str) -> str:
    return "Claude" if provider == "claude" else "Codex"

def executor_type_of(value: Any) -> str:
    """会话目录记录里的执行器类型，形如 codex / claude-prototype / codex-testing-cases。"""
    if isinstance(value, dict):
        return str(value.get("executorType") or "").strip().lower()
    return str(value or "").strip().lower()

def executor_provider_of(value: Any, fallback: str = "codex") -> str:
    """这条会话是哪个 AI 工具留下的；读不出来就按调用方当前选的工具兜底。"""
    head = executor_type_of(value).split("-", 1)[0]
    return head if head in AI_PROVIDERS else ai_provider_of(fallback)

def executor_purpose_of(value: Any) -> str:
    """执行器类型里的用途后缀，拆解会话为空、原型是 prototype，跨工具列目录时只比这一段。"""
    parts = executor_type_of(value).split("-", 1)
    return parts[1] if len(parts) > 1 else ""

def same_executor_purpose(row: Any, executor_type: str) -> bool:
    return executor_purpose_of(row) == executor_purpose_of(executor_type)

def reasoning_effort_of(value: Any, provider: str = "codex") -> str:
    effort = str((value or {}).get("reasoningEffort") or "").strip() if isinstance(value, dict) else str(value or "").strip()
    allowed = CLAUDE_REASONING_EFFORTS if provider == "claude" else CODEX_REASONING_EFFORTS
    if effort and effort not in allowed:
        raise BridgeFailure(f"{provider_label(provider)} 推理强度无效")
    return effort

def fast_mode_of(value: Any, provider: str = "codex") -> bool:
    if provider != "claude":
        return False
    raw = (value or {}).get("fastMode", False) if isinstance(value, dict) else value
    if not isinstance(raw, bool):
        raise BridgeFailure("Claude 快速模式必须是布尔值")
    return raw

def program_id_of(value: Any, label: str = "项目标识") -> int:
    if isinstance(value, bool):
        raise BridgeFailure(f"{label}必须是项目表的数值主键")
    try:
        program_id = int(str(value).strip())
    except (TypeError, ValueError):
        raise BridgeFailure(f"{label}必须是项目表的数值主键") from None
    if program_id <= 0:
        raise BridgeFailure(f"{label}必须是项目表的正整数主键")
    return program_id


CODEX_MODEL_CATALOG = [
    {"model": "gpt-5.6-sol", "displayName": "5.6 Sol", "description": ""},
    {"model": "gpt-5.6-terra", "displayName": "5.6 Terra", "description": ""},
    {"model": "gpt-5.6-luna", "displayName": "5.6 Luna", "description": ""},
]


DEFAULT_BIZ_LINE = ""
