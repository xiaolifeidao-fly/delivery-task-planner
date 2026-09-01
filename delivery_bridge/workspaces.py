"""工作目录解析：业务仓库路径与服务端下发的业务空间路径。

业务空间路径来自上游服务，永远不能当成浏览器可以随意指定的绝对路径，
所以解析和越界校验都收在这里，调用方拿到的一定是已经落在允许根目录下的真实目录。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import runtime
from .errors import BridgeFailure


BUSINESS_WORKSPACE_SCOPE = "业务空间"


def workspace_path_of(value: Any) -> Path:
    raw = str(value or "").strip()
    if not raw:
        raise BridgeFailure("未提供 Codex 工作目录，请先在项目管理中确认当前项目的工作目录")
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        raise BridgeFailure("Codex 工作目录必须是绝对路径")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise BridgeFailure(f"Codex 工作目录不存在：{candidate}") from exc
    if not resolved.is_dir():
        raise BridgeFailure(f"Codex 工作目录不是目录：{resolved}")
    return resolved


def business_workspace_path_of(value: Any, root: Path) -> Path:
    """Resolve and create one server-owned business conversation directory.

    The API carries a logical path only: ``{username}/业务空间/{projectName}``.
    It is never treated as an arbitrary path supplied by a browser or upstream
    server, so a business conversation cannot escape the configured root.
    """
    raw = str(value or "").strip().replace("\\", "/")
    parts = [part.strip() for part in raw.split("/")]
    if len(parts) != 3 or parts[1] != BUSINESS_WORKSPACE_SCOPE:
        raise BridgeFailure("业务工作目录必须是 用户名/业务空间/项目名称")
    owner, _scope, project_name = parts
    # The authenticated account name can be Chinese or another Unicode name.
    # It only needs to be a single safe directory segment; the Go service uses
    # the same rule when it builds the logical workspace path.
    if not owner or owner in {".", ".."} or len(owner) > 120:
        raise BridgeFailure("业务工作目录中的用户名无效")
    if any(ord(character) < 0x20 or character in {"/", "\\"} for character in owner):
        raise BridgeFailure("业务工作目录中的用户名无效")
    if not project_name or project_name in {".", ".."} or len(project_name) > 120:
        raise BridgeFailure("业务工作目录中的项目名称无效")
    if any(ord(character) < 0x20 or character in {"/", "\\"} for character in project_name):
        raise BridgeFailure("业务工作目录中的项目名称无效")

    try:
        root = root.expanduser()
        root.mkdir(parents=True, exist_ok=True)
        resolved_root = root.resolve(strict=True)
        workspace = (resolved_root / owner / BUSINESS_WORKSPACE_SCOPE / project_name).resolve()
        workspace.relative_to(resolved_root)
        workspace.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise BridgeFailure(f"无法创建业务工作目录：{exc}") from exc
    except ValueError as exc:
        raise BridgeFailure("业务工作目录超出允许范围") from exc
    return workspace


def environment_setup_workspace() -> Path:
    """「预设环境」的专用工作目录。

    装 Python / Node / Go 走的是本机全局包管理器，和项目代码没有关系，
    所以和初始化 Git 环境一样给一个运行时目录下的空目录当 cwd，别把安装痕迹落进业务仓库。
    """
    root = runtime.RUNTIME_DIR / "environment-setup"
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()
