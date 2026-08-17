#!/usr/bin/env python3
"""Install the Codex HTTP bridge as a per-user macOS LaunchAgent."""

from __future__ import annotations

import argparse
import os
import plistlib
import subprocess
import sys
from pathlib import Path


LABEL = "com.universe.delivery-task-planner.codex-bridge"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plugin-root", required=True)
    # 工作目录是可选的：每个项目的工作目录由面板请求带过来，进程本身不需要认领某个仓库。
    parser.add_argument("--workspace", default="")
    parser.add_argument("--allow-origin", action="append", default=[])
    args = parser.parse_args()

    plugin_root = Path(args.plugin_root).resolve()
    workspace = Path(args.workspace).resolve() if args.workspace else None
    runtime_dir = Path.home() / ".local" / "state" / "delivery-task-planner"
    agent_dir = Path.home() / "Library" / "LaunchAgents"
    plist_path = agent_dir / f"{LABEL}.plist"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    agent_dir.mkdir(parents=True, exist_ok=True)

    arguments = [
        sys.executable,
        str(plugin_root / "http_bridge.py"),
    ]
    if workspace:
        (runtime_dir / "workspace").write_text(str(workspace) + "\n", encoding="utf-8")
        arguments.extend(["--workspace", str(workspace)])
    for origin in args.allow_origin:
        arguments.extend(["--allow-origin", origin])

    document = {
        "Label": LABEL,
        "ProgramArguments": arguments,
        "WorkingDirectory": str(plugin_root),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Interactive",
        "EnvironmentVariables": {
            "HOME": str(Path.home()),
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin"),
        },
        "StandardOutPath": str(runtime_dir / "http-bridge.log"),
        "StandardErrorPath": str(runtime_dir / "http-bridge.log"),
    }
    with plist_path.open("wb") as handle:
        plistlib.dump(document, handle, sort_keys=False)

    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", domain, str(plist_path)], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["launchctl", "bootstrap", domain, str(plist_path)], check=True)
    subprocess.run(["launchctl", "kickstart", "-k", f"{domain}/{LABEL}"], check=True)


if __name__ == "__main__":
    main()
