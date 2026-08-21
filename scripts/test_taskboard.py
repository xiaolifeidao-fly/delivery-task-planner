#!/usr/bin/env python3

import importlib.util
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch


CLI_PATH = Path(__file__).resolve().parents[1] / "taskboard.py"
SPEC = importlib.util.spec_from_file_location("delivery_task_planner_cli", CLI_PATH)
cli = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(cli)


class TaskboardCliTest(unittest.TestCase):
    def run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_every_action_is_reachable_as_a_command(self):
        code, out, _ = self.run_cli(["actions"])
        commands = {entry["command"] for entry in json.loads(out)}
        self.assertEqual(0, code)
        self.assertEqual({name.replace("_", "-") for name in (action["name"] for action in cli.planner.ACTIONS)}, commands)
        self.assertNotIn("set-task-board-api-url", commands)

    def test_scalar_flags_reach_the_action(self):
        with patch.object(cli.planner, "run_action", return_value={"ok": True}) as run_action:
            code, out, _ = self.run_cli(["get-task-board-context", "--program-id", "4", "--module-key", "api"])

        run_action.assert_called_once_with("get_task_board_context", {"program_id": 4, "module_key": "api"})
        self.assertEqual(0, code)
        self.assertEqual({"ok": True}, json.loads(out))

    def test_json_payload_carries_arrays_and_flags_win_over_it(self):
        with patch.object(cli.planner, "run_action", return_value={}) as run_action:
            self.run_cli([
                "create-task-board-tasks",
                "--json", json.dumps({"program_id": 1, "tasks": [{"ref": "a"}]}),
                "--program-id", "4",
            ])

        self.assertEqual({"program_id": 4, "tasks": [{"ref": "a"}]}, run_action.call_args.args[1])

    def test_json_payload_can_be_read_from_a_file(self):
        with tempfile.TemporaryDirectory() as directory:
            payload = Path(directory) / "tasks.json"
            payload.write_text(json.dumps({"tasks": [{"ref": "a"}]}), encoding="utf-8")
            with patch.object(cli.planner, "run_action", return_value={}) as run_action:
                self.run_cli(["create-task-board-tasks", "--json", f"@{payload}"])

        self.assertEqual({"tasks": [{"ref": "a"}]}, run_action.call_args.args[1])

    def test_boolean_flag_is_passed_as_a_boolean(self):
        with patch.object(cli.planner, "run_action", return_value={}) as run_action:
            self.run_cli(["store-task-board-credential", "--key", "token", "--verify-connection", "false"])

        self.assertEqual({"key": "token", "verify_connection": False}, run_action.call_args.args[1])

    def test_failures_go_to_stderr_with_a_nonzero_exit_code(self):
        with patch.object(cli.planner, "run_action", side_effect=cli.planner.ToolFailure("凭证已失效")):
            code, out, err = self.run_cli(["list-task-board-projects"])

        self.assertEqual(1, code)
        self.assertEqual("", out)
        self.assertIn("凭证已失效", err)


if __name__ == "__main__":
    unittest.main()
