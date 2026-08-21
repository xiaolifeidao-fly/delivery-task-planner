"""Restart the bridge after an HTTP response has safely reached the browser."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


LAUNCH_AGENT_LABEL = "com.universe.delivery-task-planner.codex-bridge"


def process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--plugin-root", required=True)
    parser.add_argument("bridge_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    time.sleep(1.0)

    if sys.platform == "darwin":
        plist = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"
        if plist.exists():
            subprocess.run(
                ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{LAUNCH_AGENT_LABEL}"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
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
    command = [sys.executable, str(plugin_root / "http_bridge.py"), *args.bridge_args]
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


if __name__ == "__main__":
    main()
