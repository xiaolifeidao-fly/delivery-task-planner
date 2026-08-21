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

    def test_windows_restart_terminates_and_detaches_the_bridge(self):
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
                patch.object(restart_helper.os, "kill") as kill,
                patch.object(restart_helper, "process_exists", return_value=False),
                patch.object(restart_helper.subprocess, "Popen") as popen,
            ):
                restart_helper.main([
                    "--pid", "123",
                    "--plugin-root", str(plugin_root),
                    "--allow-origin", "*",
                ])

        kill.assert_called_once_with(123, restart_helper.signal.SIGTERM)
        command = popen.call_args.args[0]
        self.assertEqual([sys.executable, str(plugin_root.resolve() / "http_bridge.py"), "--allow-origin", "*"], command)
        self.assertTrue(popen.call_args.kwargs["close_fds"])


if __name__ == "__main__":
    unittest.main()
