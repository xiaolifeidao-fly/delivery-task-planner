"""交给执行器子进程的环境变量。

插件在执行器里是通过这几个环境变量认身份的：写权限、项目、令牌、接口地址。
需求梳理的预览轮会把插件降级成只读——提示词之外再加一道工具级的硬拦截。
"""

from __future__ import annotations

from typing import Any

import server as planner

from .payloads import assert_runtime_project
from .providers import ai_provider_of


def codex_environment(
    config: dict[str, Any], program_id: int, write_allowed: bool = True, provider: str = "codex",
) -> dict[str, str]:
    assert_runtime_project(config, program_id)
    # Claude 一律以 bypass 身份启动：CLI 侧带 --dangerously-skip-permissions，
    # 插件侧也不再降级成只读，避免预览轮拿不到写文件（需求大纲）所需的权限。
    if ai_provider_of(provider) == "claude":
        write_allowed = True
    return {
        # 需求梳理的预览轮次把插件降级成只读：提示词之外再加一道工具级的硬拦截。
        planner.RUNTIME_WRITE_MODE_ENV: "write" if write_allowed else "preview",
        planner.RUNTIME_PROJECT_ID_ENV: str(program_id),
        planner.RUNTIME_TOKEN_ENV: str(config.get("key") or ""),
        planner.RUNTIME_TOKEN_HEADER_ENV: str(config.get("key_header") or "token"),
        planner.RUNTIME_USER_ID_ENV: str(config.get("user_id") or "task-executor"),
        planner.RUNTIME_API_URL_ENV: str(config.get("api_url") or ""),
    }
