#!/usr/bin/env python3

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from delivery_bridge import restart_helper


class RestartHelperTest(unittest.TestCase):
    def test_bridge_options_are_preserved_instead_of_rejected(self):
        args, bridge_args = restart_helper.parse_arguments([
            "--pid", "123",
            "--plugin-root", "/tmp/plugin",
            "--allow-origin", "*",
            "--workspace", "/tmp/workspace",
        ])

        self.assertEqual(123, args.pid)
        self.assertEqual("/tmp/plugin", args.plugin_root)
        self.assertEqual(["--allow-origin", "*", "--workspace", "/tmp/workspace"], bridge_args)

    def test_macos_launch_agent_restart_accepts_bridge_options(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            plist = home / "Library" / "LaunchAgents" / f"{restart_helper.LAUNCH_AGENT_LABEL}.plist"
            plist.parent.mkdir(parents=True)
            plist.write_text("plist", encoding="utf-8")
            completed = subprocess.CompletedProcess(["launchctl"], 0, "", "")
            with (
                patch.object(restart_helper.sys, "platform", "darwin"),
                patch.object(restart_helper.time, "sleep"),
                patch.object(restart_helper, "restart_log"),
                patch.object(restart_helper.subprocess, "run", return_value=completed) as run,
            ):
                restart_helper.main([
                    "--pid", "123",
                    "--plugin-root", "/tmp/plugin",
                    "--allow-origin", "*",
                ], home_dir=home)

        command = run.call_args.args[0]
        self.assertEqual(["launchctl", "kickstart", "-k"], command[:3])
        self.assertIn(restart_helper.LAUNCH_AGENT_LABEL, command[-1])

    def test_windows_restart_reinstalls_supervised_task_and_waits_for_health(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            plugin_root = Path(directory) / "plugin"
            plugin_root.mkdir()
            (plugin_root / "http_bridge.py").write_text("# bridge\n", encoding="utf-8")

            with (
                patch.object(restart_helper.sys, "platform", "win32"),
                patch.object(restart_helper.Path, "home", return_value=home),
                patch.object(restart_helper.time, "sleep"),
                patch.object(restart_helper, "restart_log"),
                patch.object(restart_helper, "terminate_bridge") as terminate,
                patch.object(restart_helper, "reinstall_windows_scheduled_task", return_value=True) as reinstall,
                patch.object(restart_helper, "wait_for_bridge_ready", return_value=True) as ready,
                patch.object(restart_helper, "start_windows_supervisor") as fallback,
            ):
                restart_helper.main([
                    "--pid", "123",
                    "--plugin-root", str(plugin_root),
                    "--allow-origin", "*",
                ])

        terminate.assert_called_once_with(123)
        reinstall.assert_called_once_with(plugin_root.resolve(), ["--allow-origin", "*"])
        ready.assert_called_once_with(["--allow-origin", "*"])
        fallback.assert_not_called()

    def test_windows_restart_starts_supervisor_when_task_reinstall_fails(self):
        plugin_root = Path("/tmp/plugin").resolve()
        bridge_args = ["--port", "9876"]
        with (
            patch.object(restart_helper, "terminate_bridge") as terminate,
            patch.object(restart_helper, "reinstall_windows_scheduled_task", return_value=False),
            patch.object(restart_helper, "start_windows_supervisor") as start_supervisor,
            patch.object(restart_helper, "wait_for_bridge_ready", return_value=True) as ready,
        ):
            restart_helper.restart_windows_bridge(456, plugin_root, bridge_args)

        terminate.assert_called_once_with(456)
        start_supervisor.assert_called_once_with(plugin_root, bridge_args)
        ready.assert_called_once_with(bridge_args)

    def test_windows_restart_does_not_start_duplicate_supervisor_when_registered_task_is_unhealthy(self):
        with (
            patch.object(restart_helper, "terminate_bridge"),
            patch.object(restart_helper, "reinstall_windows_scheduled_task", return_value=True),
            patch.object(restart_helper, "wait_for_bridge_ready", return_value=False),
            patch.object(restart_helper, "start_windows_supervisor") as fallback,
        ):
            with self.assertRaisesRegex(RuntimeError, "did not become healthy"):
                restart_helper.restart_windows_bridge(789, Path("/tmp/plugin"), [])

        fallback.assert_not_called()

    def test_bridge_health_url_preserves_custom_host_and_port(self):
        self.assertEqual(
            "http://[::1]:9876/healthz",
            restart_helper.bridge_health_url(["--host", "::1", "--port=9876"]),
        )

    def test_bridge_health_url_dials_loopback_for_a_wildcard_bind(self):
        for host in ("0.0.0.0", "::", ""):
            with self.subTest(host=host):
                self.assertEqual(
                    "http://127.0.0.1:8765/healthz",
                    restart_helper.bridge_health_url(["--host", host]),
                )

    def test_windows_service_arguments_preserve_workspace_and_origin(self):
        self.assertEqual(
            ("C:\\work tree", "https://console.example"),
            restart_helper.windows_service_arguments([
                "--workspace", "C:\\work tree",
                "--allow-origin=https://console.example",
            ]),
        )


if __name__ == "__main__":
    unittest.main()
