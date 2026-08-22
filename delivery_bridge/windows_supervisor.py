"""Keep the Windows bridge worker alive without waiting for Task Scheduler retries."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence


FAST_EXIT_SECONDS = 5.0
MAX_RESTART_DELAY_SECONDS = 10.0


def runtime_directory() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if local_app_data:
        return Path(local_app_data) / "delivery-task-planner"
    return Path.home() / ".local" / "state" / "delivery-task-planner"


def supervisor_log(message: str) -> None:
    try:
        directory = runtime_directory()
        directory.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        with (directory / "windows-supervisor.log").open("a", encoding="utf-8") as output:
            output.write(f"{timestamp} {message}\n")
    except OSError:
        pass


def parse_arguments(argv: Sequence[str] | None = None) -> tuple[argparse.Namespace, list[str]]:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plugin-root", required=True)
    args, bridge_args = parser.parse_known_args(argv)
    return args, bridge_args


def bridge_command(plugin_root: Path, bridge_args: Sequence[str]) -> list[str]:
    return [sys.executable, str(plugin_root / "http_bridge.py"), *bridge_args]


def restart_delay(fast_exit_count: int) -> float:
    if fast_exit_count <= 0:
        return 0.25
    return min(MAX_RESTART_DELAY_SECONDS, float(2 ** min(fast_exit_count - 1, 4)))


def supervise(argv: Sequence[str] | None = None, max_launches: int | None = None) -> None:
    args, bridge_args = parse_arguments(argv)
    plugin_root = Path(args.plugin_root).resolve()
    bridge_script = plugin_root / "http_bridge.py"
    if not bridge_script.is_file():
        raise RuntimeError(f"bridge script not found: {bridge_script}")

    directory = runtime_directory()
    directory.mkdir(parents=True, exist_ok=True)
    supervisor_pid_path = directory / "windows-supervisor.pid"
    bridge_pid_path = directory / "http-bridge.pid"
    supervisor_pid_path.write_text(str(os.getpid()), encoding="utf-8")
    stopping = False
    child: subprocess.Popen[bytes] | None = None

    def request_stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True
        if child and child.poll() is None:
            child.terminate()

    signal.signal(signal.SIGTERM, request_stop)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, request_stop)

    launches = 0
    fast_exit_count = 0
    creation_flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    supervisor_log(f"Windows bridge supervisor started with pid {os.getpid()}.")
    try:
        while not stopping and (max_launches is None or launches < max_launches):
            started_at = time.monotonic()
            with (directory / "http-bridge.log").open("ab") as output:
                child = subprocess.Popen(
                    bridge_command(plugin_root, bridge_args),
                    cwd=plugin_root,
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=output,
                    creationflags=creation_flags,
                    close_fds=True,
                )
                launches += 1
                bridge_pid_path.write_text(str(child.pid), encoding="utf-8")
                supervisor_log(f"Bridge worker started with pid {child.pid} (attempt {launches}).")
                exit_code = child.wait()
            child = None
            if stopping:
                break
            lifetime = time.monotonic() - started_at
            fast_exit_count = fast_exit_count + 1 if lifetime < FAST_EXIT_SECONDS else 0
            delay = restart_delay(fast_exit_count)
            supervisor_log(
                f"Bridge worker exited with code {exit_code} after {lifetime:.2f}s; restarting in {delay:.2f}s."
            )
            time.sleep(delay)
    finally:
        bridge_pid_path.unlink(missing_ok=True)
        try:
            if supervisor_pid_path.read_text(encoding="utf-8").strip() == str(os.getpid()):
                supervisor_pid_path.unlink(missing_ok=True)
        except OSError:
            pass
        supervisor_log("Windows bridge supervisor stopped.")


if __name__ == "__main__":
    try:
        supervise()
    except Exception as exc:
        supervisor_log(f"Windows bridge supervisor failed: {exc}")
        raise
