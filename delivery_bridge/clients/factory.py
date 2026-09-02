"""按 provider 造一个执行器客户端。

两个客户端类都按模块名访问（``codex.codex.AppServerClient`` / ``claude.claude.ClaudeCLIClient``），
测试打桩时改这一处就对所有调用方生效——包括只读会话复用池那一路。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .. import runtime
from ..errors import BridgeFailure
from ..providers import ai_provider_of
from . import claude, codex


def lightweight_workspace() -> Path:
    """内务回合（起标题）的工作目录：运行时目录下一个空目录。

    起名不需要看项目一眼，但只要 cwd 落在项目里，两个执行器都会把项目的
    AGENTS.md / CLAUDE.md、技能清单和目录信息装进第一条请求——为一行标题付一份
    项目上下文。这里给它一个固定的空目录，两边都干净。
    """
    path = runtime.RUNTIME_DIR / "lightweight"
    path.mkdir(parents=True, exist_ok=True)
    return path


def create_ai_client(
    provider: str,
    workspace: Path,
    event_callback: Any = None,
    environment: dict[str, str] | None = None,
    lightweight: bool = False,
    show_reasoning: bool = False,
) -> codex.AppServerClient | claude.ClaudeCLIClient:
    """``show_reasoning`` 只对需求侧会话开：那里的推理摘要是用户要读的产物本身。

    任务执行会话不开：面板展示的是命令、文件改动和最终结论，摘要既不上屏，
    又要按输出 token 付钱。Claude 侧没有这个开关——它的过程一直是工具步骤，
    桥接器从来不把 thinking 块上屏。
    """
    if provider == "claude":
        return claude.ClaudeCLIClient(workspace, event_callback, environment, lightweight=lightweight)
    return codex.AppServerClient(
        workspace, event_callback, environment, lightweight=lightweight, show_reasoning=show_reasoning,
    )
