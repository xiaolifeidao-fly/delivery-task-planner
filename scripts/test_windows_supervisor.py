#!/usr/bin/env python3

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from delivery_bridge import windows_supervisor


class WindowsSupervisorTest(unittest.TestCase):
    def test_bridge_command_preserves_worker_arguments(self):
        plugin_root = Path("C:/plugins/delivery-task-planner")
        self.assertEqual(
            [
                sys.executable,
                str(plugin_root / "http_bridge.py"),
                "--allow-origin",
                "*",
                "--port",
                "9876",
            ],
            windows_supervisor.bridge_command(
                plugin_root,
                ["--allow-origin", "*", "--port", "9876"],
            ),
        )

    def test_restart_delay_is_fast_then_bounded_for_crash_loops(self):
        self.assertEqual(0.25, windows_supervisor.restart_delay(0))
        self.assertEqual(1.0, windows_supervisor.restart_delay(1))
        self.assertEqual(4.0, windows_supervisor.restart_delay(3))
        self.assertEqual(10.0, windows_supervisor.restart_delay(20))

    def test_runtime_directory_uses_local_app_data(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"LOCALAPPDATA": directory}):
            self.assertEqual(
                Path(directory) / "delivery-task-planner",
                windows_supervisor.runtime_directory(),
            )

    def test_windows_task_runs_the_supervisor_instead_of_the_bridge(self):
        installer = (PLUGIN_ROOT / "scripts" / "install_http_service.ps1").read_text(encoding="utf-8")
        self.assertIn('"delivery_bridge\\windows_supervisor.py"', installer)
        self.assertIn('$arguments += "--plugin-root"', installer)
        self.assertIn("-MultipleInstances IgnoreNew", installer)


if __name__ == "__main__":
    unittest.main()
