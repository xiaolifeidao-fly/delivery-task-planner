"""Restart the bridge after an HTTP response has safely reached the browser."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence


LAUNCH_AGENT_LABEL = "com.universe.delivery-task-planner.codex-bridge"
RESTART_WAIT_ATTEMPTS = 100


def restart_log(message: str) -> None:
    try:
        runtime_dir = Path.home() / ".local" / "state" / "delivery-task-planner"
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

    try:
        os.kill(args.pid, signal.SIGTERM)
    except OSError:
        pass
    for _ in range(50):
        if not process_exists(args.pid):
            break
        time.sleep(0.1)

    plugin_root = Path(args.plugin_root).resolve()
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
