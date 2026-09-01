"""定位本机可用的 codex 命令行，必要时从桌面版里取一份出来。

用户机器上的 codex 可能来自 npm 全局、Homebrew、桌面版内置的资源目录，
版本还可能互相落后。这里的职责就是把候选列全、比版本、挑最新的那个，
挑不到就从桌面版资源里复制一份到运行时目录备用。

RUNTIME_DIR 按模块名访问（``runtime.RUNTIME_DIR``），这样测试改写运行时目录时
这里也跟着变；写成 from-import 会绑成本模块的一份拷贝，打桩改不到。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from . import hostinfo, runtime
from .versioning import compare_versions


WINDOWS_CLI_WRAPPER_SUFFIXES = ("", ".cmd", ".bat", ".ps1")


CODEX_DESKTOP_RESOURCE_COMPANIONS = ("codex-code-mode-host",)


def codex_cli_name(host: str = "") -> str:
    return "codex.exe" if (host or hostinfo.host_platform()) == "windows" else "codex"


def codex_cli_cache_path(host: str = "", runtime_dir: Path | None = None) -> Path:
    return (runtime_dir or runtime.RUNTIME_DIR) / "bin" / codex_cli_name(host)


def codex_desktop_resource_paths(host: str = "") -> list[Path]:
    """Known Codex Desktop resource locations, ordered by the per-user install first."""
    system = host or hostinfo.host_platform()
    executable = codex_cli_name(system)
    if system == "windows":
        local_app_data = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
        program_files = Path(os.environ.get("ProgramFiles") or r"C:\Program Files")
        roots = [
            local_app_data / "Programs" / "Codex",
            local_app_data / "Programs" / "Codex Desktop",
            local_app_data / "Codex",
            local_app_data / "Codex Desktop",
            program_files / "Codex",
            program_files / "Codex Desktop",
        ]
        return [root / "resources" / executable for root in roots]
    if system == "macos":
        roots = [
            Path.home() / "Applications" / "Codex.app",
            Path("/Applications/Codex.app"),
            Path.home() / "Applications" / "ChatGPT.app",
            Path("/Applications/ChatGPT.app"),
        ]
        return [root / "Contents" / "Resources" / executable for root in roots]
    return []


def path_codex_cli(host: str = "") -> tuple[str, str]:
    """PATH 上的 codex，拆成「能直接起进程的可执行文件」和「只能兜底的包装脚本」。

    Windows 上 npm 全局安装和 Codex Desktop 都会往 PATH 里塞 `codex.cmd` / `codex.ps1`
    这类包装脚本，CreateProcess 起不动它们（WinError 193），所以宁可退回到本地复制出来的
    codex.exe；实在什么都没有时再拿包装脚本兜底，好过直接报"未找到 CLI"。
    """
    command = shutil.which("codex") or ""
    if not command:
        return "", ""
    if (host or hostinfo.host_platform()) == "windows" and Path(command).suffix.lower() in WINDOWS_CLI_WRAPPER_SUFFIXES:
        return "", command
    return command, ""


CODEX_CLI_VERSIONS: dict[str, str] = {}


def codex_cli_version(command: str) -> str:
    """`codex --version` 的版本号，按可执行文件缓存；问不出来就返回空串。"""
    if command in CODEX_CLI_VERSIONS:
        return CODEX_CLI_VERSIONS[command]
    version = ""
    try:
        result = subprocess.run([command, "--version"], capture_output=True, text=True, timeout=15)
        version = (result.stdout or result.stderr or "").strip().split()[-1] if result.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError, IndexError):
        version = ""
    CODEX_CLI_VERSIONS[command] = version
    return version


def newest_codex_cli(candidates: list[str]) -> str:
    """在几个都能用的 codex 里挑版本最高的那个，问不出版本时保持传入顺序。

    Codex Desktop 自带的 codex 和它捆绑的 MCP 服务（node_repl、browser-use、computer-use）
    是配套发版的：实测 PATH 上的 0.134.0 调 node_repl 会被拒（`sandboxCwd must be an
    absolute file URI`），换成桌面端自带的 0.149.0-alpha，同一段代码同一组参数就能跑通。
    所以这里不能只看"PATH 上有没有 codex"，还得看它够不够新。
    """
    available = [candidate for candidate in candidates if candidate]
    if not available:
        return ""
    best = available[0]
    for candidate in available[1:]:
        if newer_codex_cli(candidate, best):
            best = candidate
    return best


def newer_codex_cli(candidate: str, current: str) -> bool:
    """版本问不出来或者格式不认识，就当它不比现有的新，保持原来的优先级。"""
    candidate_version = codex_cli_version(candidate)
    current_version = codex_cli_version(current)
    if not candidate_version:
        return False
    if not current_version:
        return True
    try:
        return compare_versions(candidate_version, current_version) > 0
    except ValueError:
        return False


def codex_cli_candidates(host: str = "", runtime_dir: Path | None = None) -> list[str]:
    """本机所有可直接拉起的 codex，按原来的优先级排列。"""
    cache_path = codex_cli_cache_path(host, runtime_dir)
    command, _ = path_codex_cli(host)
    candidates = [command, str(cache_path) if cache_path.is_file() else ""]
    candidates.extend(str(path) for path in codex_desktop_resource_paths(host) if path.is_file())
    return [candidate for candidate in candidates if candidate]


def available_codex_cli(host: str = "", runtime_dir: Path | None = None) -> str:
    """Locate a PATH CLI, a previously copied local CLI, or a Desktop resource."""
    cache_path = codex_cli_cache_path(host, runtime_dir)
    # 已经复制到运行时目录的 codex.exe 是确定能跑的，Windows 上优先用它。
    if (host or hostinfo.host_platform()) == "windows" and cache_path.is_file():
        return str(cache_path)
    _, wrapper = path_codex_cli(host)
    return newest_codex_cli(codex_cli_candidates(host, runtime_dir)) or wrapper


def provision_codex_cli(host: str = "", runtime_dir: Path | None = None) -> str:
    """Copy Codex Desktop's bundled CLI locally when it is the newest one on this machine."""
    cache_path = codex_cli_cache_path(host, runtime_dir)
    # 与 available_codex_cli 保持同一优先级，避免"检测到的"和"真正拉起的"不是同一个。
    if (host or hostinfo.host_platform()) == "windows" and cache_path.is_file():
        return str(cache_path)
    command, wrapper = path_codex_cli(host)
    chosen = newest_codex_cli(codex_cli_candidates(host, runtime_dir))
    desktop_paths = {str(path) for path in codex_desktop_resource_paths(host)}
    if chosen and chosen not in desktop_paths:
        return chosen
    source = Path(chosen) if chosen else next((path for path in codex_desktop_resource_paths(host) if path.is_file()), None)
    if source is None:
        return command or wrapper
    # 桌面端资源目录里的可执行文件本来就能直接跑，而且它的同目录伴生文件都在。
    # 复制只是 Windows 那边为了绕开包装脚本才需要的，别为此搬运一个几百 MB 的二进制。
    if (host or hostinfo.host_platform()) != "windows":
        return str(source)
    if cache_path.is_file() and cache_path.stat().st_size == source.stat().st_size:
        return str(cache_path)
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, cache_path)
        source_suffix = ".exe" if source.suffix.lower() == ".exe" else ""
        for companion in CODEX_DESKTOP_RESOURCE_COMPANIONS:
            companion_source = source.with_name(f"{companion}{source_suffix}")
            if companion_source.is_file():
                shutil.copy2(companion_source, cache_path.with_name(companion_source.name))
        # 复制过来的是另一个版本，之前问出来的版本号不能再算数。
        CODEX_CLI_VERSIONS.pop(str(cache_path), None)
        return str(cache_path)
    except OSError:
        # Desktop resources are directly executable too. Keep the board usable
        # when a restrictive filesystem prevents creating the local copy.
        return str(source)
