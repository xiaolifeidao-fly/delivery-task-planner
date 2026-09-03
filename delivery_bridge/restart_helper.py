"""Restart the bridge after an HTTP response has safely reached the browser."""

from __future__ import annotations

import argparse
import os
import signal
import shutil
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence
from urllib.request import urlopen


LAUNCH_AGENT_LABEL = "com.universe.delivery-task-planner.codex-bridge"
RESTART_WAIT_ATTEMPTS = 100
WINDOWS_READY_TIMEOUT_SECONDS = 30.0


def runtime_directory(home_dir: Path | None = None) -> Path:
    if sys.platform == "win32" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "delivery-task-planner"
    return (home_dir or Path.home()) / ".local" / "state" / "delivery-task-planner"


def restart_log(message: str) -> None:
    try:
        runtime_dir = runtime_directory()
        runtime_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        with (runtime_dir / "restart-helper.log").open("a", encoding="utf-8") as output:
            output.write(f"{timestamp} {message}\n")
    except OSError:
        pass


def process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def parse_arguments(argv: Sequence[str] | None = None) -> tuple[argparse.Namespace, list[str]]:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--plugin-root", required=True)
    # The bridge itself owns options such as --allow-origin and --workspace.
    # parse_known_args preserves them instead of rejecting them as helper options.
    args, bridge_args = parser.parse_known_args(argv)
    return args, bridge_args


def terminate_for_launch_agent(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
        restart_log(f"Sent SIGTERM to bridge pid {pid}; LaunchAgent KeepAlive will relaunch it.")
    except OSError as exc:
        restart_log(f"Bridge pid {pid} was already gone: {exc}")
    for _ in range(RESTART_WAIT_ATTEMPTS):
        if not process_exists(pid):
            return
        time.sleep(0.1)
    raise RuntimeError(f"bridge pid {pid} did not stop")


def terminate_bridge(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
        restart_log(f"Sent SIGTERM to bridge pid {pid}.")
    except OSError as exc:
        restart_log(f"Bridge pid {pid} was already gone: {exc}")
    for _ in range(RESTART_WAIT_ATTEMPTS):
        if not process_exists(pid):
            return
        time.sleep(0.1)
    raise RuntimeError(f"bridge pid {pid} did not stop")


def bridge_health_url(bridge_args: Sequence[str]) -> str:
    host = "127.0.0.1"
    port = 8765
    for index, value in enumerate(bridge_args):
        if value == "--host" and index + 1 < len(bridge_args):
            host = bridge_args[index + 1]
        elif value.startswith("--host="):
            host = value.split("=", 1)[1]
        elif value == "--port" and index + 1 < len(bridge_args):
            port = int(bridge_args[index + 1])
        elif value.startswith("--port="):
            port = int(value.split("=", 1)[1])
    if host in {"localhost", "0.0.0.0", "::", "*", ""}:
        # 通配绑定不是一个可拨的地址；健康检查始终走回环。
        host = "127.0.0.1"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{port}/healthz"


def wait_for_bridge_ready(bridge_args: Sequence[str], timeout: float = WINDOWS_READY_TIMEOUT_SECONDS) -> bool:
    url = bridge_health_url(bridge_args)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urlopen(url, timeout=1) as response:
                if response.status == 200:
                    restart_log(f"Bridge health check succeeded at {url}.")
                    return True
        except OSError:
            pass
        time.sleep(0.25)
    restart_log(f"Bridge health check timed out after {timeout:.1f}s at {url}.")
    return False


def windows_service_arguments(bridge_args: Sequence[str]) -> tuple[str, str, str]:
    """重装计划任务时要把启动参数原样带回去。

    漏掉 ``--command-api-url`` 不会报错，只会让远程命令 Worker 在下次重启后静默
    禁用——任务面板上就变成「未登记执行电脑」，而日志里什么都查不到。
    """
    workspace = ""
    allow_origin = "*"
    command_api_url = ""
    for index, value in enumerate(bridge_args):
        if value == "--workspace" and index + 1 < len(bridge_args):
            workspace = bridge_args[index + 1]
        elif value.startswith("--workspace="):
            workspace = value.split("=", 1)[1]
        elif value == "--allow-origin" and index + 1 < len(bridge_args):
            allow_origin = bridge_args[index + 1]
        elif value.startswith("--allow-origin="):
            allow_origin = value.split("=", 1)[1]
        elif value == "--command-api-url" and index + 1 < len(bridge_args):
            command_api_url = bridge_args[index + 1]
        elif value.startswith("--command-api-url="):
            command_api_url = value.split("=", 1)[1]
    return workspace, allow_origin, command_api_url


def reinstall_windows_scheduled_task(plugin_root: Path, bridge_args: Sequence[str]) -> bool:
    installer = plugin_root / "scripts" / "install_http_service.ps1"
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if not installer.is_file() or not powershell:
        restart_log("Windows scheduled-task installer or PowerShell was not found; using detached supervisor fallback.")
        return False
    workspace, allow_origin, command_api_url = windows_service_arguments(bridge_args)
    command = [
        powershell,
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(installer),
        "-PluginRoot",
        str(plugin_root),
        "-AllowOrigin",
        allow_origin,
    ]
    if workspace:
        command.extend(["-Workspace", workspace])
    if command_api_url:
        command.extend(["-CommandApiUrl", command_api_url])
    completed = subprocess.run(command, check=False, capture_output=True, text=True, timeout=45)
    restart_log(
        f"Windows scheduled task reinstall exited {completed.returncode}: "
        f"{(completed.stderr or completed.stdout).strip() or 'no output'}"
    )
    return completed.returncode == 0


def start_windows_supervisor(plugin_root: Path, bridge_args: Sequence[str]) -> subprocess.Popen[bytes]:
    supervisor = plugin_root / "delivery_bridge" / "windows_supervisor.py"
    command = [sys.executable, str(supervisor), "--plugin-root", str(plugin_root), *bridge_args]
    directory = runtime_directory()
    directory.mkdir(parents=True, exist_ok=True)
    output = (directory / "windows-supervisor.log").open("ab")
    creation_flags = (
        getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        | getattr(subprocess, "DETACHED_PROCESS", 0)
        | getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)
    )
    process = subprocess.Popen(
        command,
        cwd=plugin_root,
        stdin=subprocess.DEVNULL,
        stdout=output,
        stderr=output,
        creationflags=creation_flags,
        close_fds=True,
    )
    output.close()
    restart_log(f"Detached Windows supervisor started with pid {process.pid}.")
    return process


def restart_windows_bridge(pid: int, plugin_root: Path, bridge_args: Sequence[str]) -> None:
    terminate_bridge(pid)
    if reinstall_windows_scheduled_task(plugin_root, bridge_args):
        if wait_for_bridge_ready(bridge_args):
            return
        raise RuntimeError("Windows scheduled task started but the bridge did not become healthy")
    start_windows_supervisor(plugin_root, bridge_args)
    if not wait_for_bridge_ready(bridge_args):
        raise RuntimeError("Windows bridge did not become healthy after restart")


def main(argv: Sequence[str] | None = None, home_dir: Path | None = None) -> None:
    args, bridge_args = parse_arguments(argv)
    restart_log(f"Restart helper started for bridge pid {args.pid}.")
    time.sleep(1.0)

    if sys.platform == "darwin":
        plist = (home_dir or Path.home()) / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"
        if plist.exists():
            command = ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{LAUNCH_AGENT_LABEL}"]
            try:
                completed = subprocess.run(command, check=False, capture_output=True, text=True, timeout=20)
                restart_log(
                    f"launchctl kickstart exited {completed.returncode}: "
                    f"{(completed.stderr or completed.stdout).strip() or 'no output'}"
                )
                if completed.returncode == 0:
                    return
            except (OSError, subprocess.SubprocessError) as exc:
                restart_log(f"launchctl kickstart failed: {exc}")
            terminate_for_launch_agent(args.pid)
            return

    plugin_root = Path(args.plugin_root).resolve()
    if sys.platform == "win32":
        restart_windows_bridge(args.pid, plugin_root, bridge_args)
        return

    terminate_bridge(args.pid)
    runtime_dir = Path.home() / ".local" / "state" / "delivery-task-planner"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    log = (runtime_dir / "http-bridge.log").open("a", encoding="utf-8")
    command = [sys.executable, str(plugin_root / "http_bridge.py"), *bridge_args]
    creation_flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    subprocess.Popen(
        command,
        cwd=plugin_root,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=log,
        start_new_session=sys.platform != "win32",
        creationflags=creation_flags if sys.platform == "win32" else 0,
        close_fds=True,
    )
    restart_log(f"Detached bridge relaunch started with {len(bridge_args)} preserved arguments.")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        restart_log(f"Restart helper failed:\n{traceback.format_exc()}")
        raise
