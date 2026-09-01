"""按 provider 造一个执行器客户端。

两个客户端类都按模块名访问（``codex.codex.AppServerClient`` / ``claude.claude.ClaudeCLIClient``），
测试打桩时改这一处就对所有调用方生效——包括只读会话复用池那一路。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..errors import BridgeFailure
from ..providers import ai_provider_of
from . import claude, codex

def create_ai_client(provider: str, workspace: Path, event_callback: Any = None, environment: dict[str, str] | None = None) -> codex.AppServerClient | claude.ClaudeCLIClient:
    if provider == "claude":
        return claude.ClaudeCLIClient(workspace, event_callback, environment)
    return codex.AppServerClient(workspace, event_callback, environment)
