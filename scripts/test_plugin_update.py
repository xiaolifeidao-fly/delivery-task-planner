#!/usr/bin/env python3

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from delivery_bridge.update_manager import PluginUpdateManager, UpdateFailure, replace_package
from delivery_bridge.versioning import compare_versions


def make_package(root: Path, version: str = "0.3.0") -> Path:
    (root / ".codex-plugin").mkdir(parents=True)
    (root / ".claude-plugin").mkdir(parents=True)
    (root / "skills" / "delivery-task-planner").mkdir(parents=True)
    for package in ("clients", "prompts", "execution"):
        (root / "delivery_bridge" / package).mkdir(parents=True)
    for folder in (".codex-plugin", ".claude-plugin"):
        (root / folder / "plugin.json").write_text(
            json.dumps({"name": "delivery-task-planner", "version": version}),
            encoding="utf-8",
        )
    (root / "skills" / "delivery-task-planner" / "SKILL.md").write_text("# skill\n", encoding="utf-8")
    for name in ("http_bridge.py", "server.py", "taskboard.py"):
        (root / name).write_text(f"# {version}\n", encoding="utf-8")
    for name in (
        "update_manager.py",
        "restart_helper.py",
        "windows_supervisor.py",
        "errors.py",
        "prompt_context.py",
        "workspaces.py",
        "git_ops.py",
        "turn_output.py",
        "turn_view.py",
        "github_ssh.py",
        "documents.py",
        "runtime.py",
        "hostinfo.py",
        "codex_cli.py",
        "providers.py",
        "environments.py",
        "attachments_text.py",
        "timeutil.py",
        "reasoning.py",
        "payloads.py",
        "stores.py",
        "chat_archive.py",
        "artifacts.py",
        "sessions.py",
        "progress_events.py",
        "item_keys.py",
        "executor_env.py",
    ):
        (root / "delivery_bridge" / name).write_text(f"# {version}\n", encoding="utf-8")
    for package, names in {
        "clients": ("journal.py", "codex.py", "claude.py", "factory.py", "pool.py"),
        "prompts": ("common.py", "task.py", "planning.py", "conversation.py", "requirement.py", "environment.py"),
        "execution": tuple(f"{name}.py" for name in (
            "core", "sync", "naming", "planning", "environment", "requirement_testing",
            "requirement_review", "fine_tuning", "task_testing", "git", "queue",
            "conversation", "documents", "prototype", "turns",
        )),
    }.items():
        for name in names:
            (root / "delivery_bridge" / package / name).write_text(f"# {version}\n", encoding="utf-8")
    return root


class PluginUpdateTest(unittest.TestCase):
    def test_version_comparison_ignores_client_cachebusters(self):
        self.assertEqual(0, compare_versions("0.3.0+codex.a", "0.3.0+codex.b"))
        self.assertGreater(compare_versions("0.4.0", "0.3.9"), 0)

    def test_status_reports_local_manifest_update_time_and_check_time(self):
        with tempfile.TemporaryDirectory() as directory:
            plugin_root = make_package(Path(directory) / "installed", "0.4.4")
            manifest = plugin_root / ".codex-plugin" / "plugin.json"
            os.utime(manifest, (1_700_000_000, 1_700_000_000))
            manager = PluginUpdateManager(
                plugin_root,
                Path(directory) / "runtime",
                "https://example.test/plugin.git",
                "https://example.test/plugin",
            )

            with patch.object(manager, "_resolve_remote", return_value={"version": "0.4.4", "commit": "a" * 40}):
                status = manager.status()

            self.assertEqual("2023-11-14T22:13:20Z", status["localUpdatedAt"])
            self.assertGreater(status["checkedAt"], 0)

    def test_package_validation_requires_matching_dual_client_release(self):
        with tempfile.TemporaryDirectory() as directory:
            root = make_package(Path(directory) / "package")
            manifest = root / ".claude-plugin" / "plugin.json"
            manifest.write_text(json.dumps({"name": "delivery-task-planner", "version": "0.2.0"}), encoding="utf-8")

            with self.assertRaises(UpdateFailure):
                PluginUpdateManager._validate_package(root, "0.3.0")

    def test_replace_package_keeps_enclosing_git_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            source = make_package(temporary / "source")
            destination = temporary / "destination"
            make_package(destination, "0.2.0")
            (destination / ".git").mkdir()
            (destination / "obsolete.txt").write_text("old", encoding="utf-8")

            replace_package(source, destination)

            self.assertTrue((destination / ".git").is_dir())
            self.assertFalse((destination / "obsolete.txt").exists())
            self.assertIn("0.3.0", (destination / "http_bridge.py").read_text(encoding="utf-8"))

    def test_claude_cache_install_is_versioned_and_updates_index_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            plugin_root = make_package(Path(directory) / "installed", "0.2.0")
            staged = make_package(Path(directory) / "staged", "0.3.0")
            metadata_path = home / ".claude" / "plugins" / "installed_plugins.json"
            old_cache = home / ".claude" / "plugins" / "cache" / "team" / "delivery-task-planner" / "0.2.0"
            old_cache.mkdir(parents=True)
            metadata_path.parent.mkdir(parents=True, exist_ok=True)
            metadata_path.write_text(json.dumps({
                "version": 2,
                "plugins": {
                    "delivery-task-planner@team": [{
                        "scope": "user",
                        "installPath": str(old_cache),
                        "version": "0.2.0",
                    }],
                },
            }), encoding="utf-8")
            manager = PluginUpdateManager(
                plugin_root,
                Path(directory) / "runtime",
                "https://example.test/plugin.git",
                "https://example.test/plugin",
                home_dir=home,
            )

            self.assertTrue(manager._install_claude_cache(staged, "a" * 40))

            installed = json.loads(metadata_path.read_text(encoding="utf-8"))
            record = installed["plugins"]["delivery-task-planner@team"][0]
            self.assertEqual("0.3.0", record["version"])
            self.assertTrue(Path(record["installPath"]).is_dir())
            self.assertTrue(old_cache.is_dir())
            self.assertTrue(list(metadata_path.parent.glob("installed_plugins.json.backup-*")))

    def test_stale_restarting_job_recovers_to_retryable_state(self):
        with tempfile.TemporaryDirectory() as directory:
            plugin_root = make_package(Path(directory) / "installed", "0.4.3")
            manager = PluginUpdateManager(
                plugin_root,
                Path(directory) / "runtime",
                "https://example.test/plugin.git",
                "https://example.test/plugin",
                home_dir=Path(directory) / "home",
            )
            manager.job = {
                "jobId": "job-1",
                "status": "restarting",
                "progress": 98,
                "restartRequired": True,
                "restartRequestedAt": "2020-01-01T00:00:00Z",
                "logs": [],
            }

            job = manager.get_job("job-1")

            self.assertEqual("restart_required", job["status"], job)
            self.assertEqual(96, job["progress"])
            self.assertIn("重新尝试", job["message"])
            self.assertEqual("warning", job["logs"][-1]["level"])

    def test_successful_install_always_requires_a_safe_bridge_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            plugin_root = make_package(temporary / "installed", "0.2.0")
            staged = make_package(temporary / "staged", "0.3.0")
            manager = PluginUpdateManager(
                plugin_root,
                temporary / "runtime",
                "https://example.test/plugin.git",
                "https://example.test/plugin",
                home_dir=temporary / "home",
            )
            manager.job = {"jobId": "job-1", "logs": []}
            remote = {
                "version": "0.3.0",
                "commit": "a" * 40,
                "revision": "a" * 40,
            }

            with (
                patch.object(manager, "_resolve_remote", return_value=remote),
                patch.object(manager, "_download_archive", return_value=temporary / "release.zip"),
                patch.object(manager, "_sha256", return_value="a" * 64),
                patch.object(manager, "_extract_archive", return_value=staged),
                patch.object(manager, "_install_codex", return_value=False),
                patch.object(manager, "_install_claude_cache", return_value=False),
            ):
                manager._install("job-1", "0.3.0")

            job = manager.get_job("job-1")
            self.assertEqual("restart_required", job["status"], job)
            self.assertTrue(job["restartRequired"])
            self.assertEqual(96, job["progress"])
            self.assertEqual("", job["finishedAt"])

    def test_waiting_for_runs_is_persisted_without_leaving_restart_required(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = PluginUpdateManager(
                make_package(Path(directory) / "installed", "0.4.4"),
                Path(directory) / "runtime",
                "https://example.test/plugin.git",
                "https://example.test/plugin",
                home_dir=Path(directory) / "home",
            )
            manager.job = {
                "jobId": "job-1",
                "status": "restart_required",
                "progress": 96,
                "restartRequired": True,
                "logs": [],
            }

            job = manager.mark_waiting_for_runs("job-1", 2)

            self.assertEqual("restart_required", job["status"])
            self.assertEqual(2, job["activeRuns"])
            self.assertIn("等待 2 个执行会话", job["message"])
            self.assertIn("暂缓重启", job["logs"][-1]["message"])
            persisted = json.loads(manager.state_path.read_text(encoding="utf-8"))
            self.assertEqual(2, persisted["activeRuns"])


if __name__ == "__main__":
    unittest.main()
