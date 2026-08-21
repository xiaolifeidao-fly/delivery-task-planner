#!/usr/bin/env python3

import importlib.util
import base64
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


BRIDGE_PATH = Path(__file__).resolve().parents[1] / "http_bridge.py"
PLUGIN_ROOT = BRIDGE_PATH.parent
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))
SPEC = importlib.util.spec_from_file_location("delivery_task_http_bridge", BRIDGE_PATH)
bridge = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(bridge)


class HttpBridgeTest(unittest.TestCase):
    @staticmethod
    def runtime_config() -> dict[str, str]:
        return {
            "api_url": "http://test/api",
            "key": "current-user-token",
            "key_header": "token",
            "user_id": "current-user",
            "_biz_line": "whatsapp",
            "_project_id": 1,
        }

    def test_wildcard_cors_allows_any_origin(self):
        handler = object.__new__(bridge.BridgeHandler)
        handler.server = SimpleNamespace(allowed_origins={"*"})
        handler.headers = {"Origin": "http://47.110.3.214:7893"}
        headers = []
        handler.send_header = lambda name, value: headers.append((name, value))

        self.assertEqual("http://47.110.3.214:7893", handler.allowed_origin())
        handler.cors()

        self.assertIn(("Access-Control-Allow-Origin", "*"), headers)
        self.assertNotIn(("Vary", "Origin"), headers)

    def test_cors_allows_a_direct_request_without_an_origin_header(self):
        handler = object.__new__(bridge.BridgeHandler)
        handler.server = SimpleNamespace(allowed_origins={"http://restricted.example"})
        handler.headers = {}
        headers = []
        handler.send_header = lambda name, value: headers.append((name, value))

        self.assertEqual("null", handler.allowed_origin())
        handler.cors()

        self.assertIn(("Access-Control-Allow-Origin", "*"), headers)

    def test_content_disposition_encodes_non_latin_file_names(self):
        header = bridge.content_disposition_of("需求大纲.md")

        self.assertEqual(
            "attachment; filename=\"download.md\"; filename*=UTF-8''%E9%9C%80%E6%B1%82%E5%A4%A7%E7%BA%B2.md",
            header,
        )
        self.assertTrue(header.encode("latin-1"))

    def test_content_disposition_preserves_ascii_image_file_name(self):
        self.assertEqual(
            "inline; filename=\"result.png\"; filename*=UTF-8''result.png",
            bridge.content_disposition_of("result.png", inline=True),
        )

    def test_http_server_uses_the_loopback_listener_without_tls(self):
        server = unittest.mock.MagicMock()
        workspace = Path("/workspace")

        with patch.object(bridge, "ThreadingHTTPServer", return_value=server) as http_server:
            result = bridge.create_http_server("127.0.0.1", 8765, workspace, {"*"})

        self.assertIs(server, result)
        http_server.assert_called_once_with(("127.0.0.1", 8765), bridge.BridgeHandler)
        self.assertEqual(workspace, server.bridge.workspace)
        self.assertEqual({"*"}, server.allowed_origins)

    def test_plugin_version_comparison_ignores_cachebuster_and_uses_semver_order(self):
        self.assertEqual(0, bridge.compare_plugin_versions("0.2.0+codex.1", "0.2.0+codex.2"))
        self.assertGreater(bridge.compare_plugin_versions("1.0.0", "0.9.9"), 0)
        self.assertLess(bridge.compare_plugin_versions("1.0.0-beta.2", "1.0.0-beta.11"), 0)
        self.assertGreater(bridge.compare_plugin_versions("1.0.0", "1.0.0-rc.1"), 0)

    def test_plugin_update_status_only_reports_a_newer_remote_release(self):
        with (
            patch.object(bridge, "installed_plugin_version", return_value="0.2.0+codex.local"),
            patch.object(bridge, "cached_remote_plugin_version", return_value="0.3.0+codex.remote"),
        ):
            status = bridge.plugin_update_status()

        self.assertTrue(status["updateAvailable"])
        self.assertEqual("0.2.0+codex.local", status["localVersion"])
        self.assertEqual("0.3.0+codex.remote", status["remoteVersion"])

    def test_plugin_update_status_does_not_report_a_newer_local_release(self):
        with (
            patch.object(bridge, "installed_plugin_version", return_value="0.3.0+codex.local"),
            patch.object(bridge, "cached_remote_plugin_version", return_value="0.2.0+codex.remote"),
        ):
            status = bridge.plugin_update_status()

        self.assertFalse(status["updateAvailable"])
        self.assertEqual("", status["message"])

    def test_plugin_update_status_treats_remote_lookup_failures_as_non_updates(self):
        with (
            patch.object(bridge, "installed_plugin_version", return_value="0.2.0+codex.local"),
            patch.object(bridge, "cached_remote_plugin_version", side_effect=bridge.BridgeFailure("network unavailable")),
        ):
            status = bridge.plugin_update_status()

        self.assertFalse(status["updateAvailable"])
        self.assertEqual("0.2.0+codex.local", status["localVersion"])
        self.assertIn("network unavailable", status["message"])

    def test_planning_result_only_contains_records_created_after_the_session_started(self):
        executor = bridge.ExecutionBridge(Path.cwd())
        baseline = {"items": {"existing"}, "stages": {"s1"}, "modules": {"api"}}
        context = {
            "items": [{"itemKey": "existing"}, {"itemKey": "new-task"}],
            "stages": [{"stageKey": "s1"}, {"stageKey": "s2"}],
            "modules": [{"moduleKey": "api"}, {"moduleKey": "web"}],
        }
        with patch.object(bridge.planner, "project_context", return_value=context):
            result = executor._planning_result({"api_url": "http://example.test/api"}, 1, baseline)

        self.assertEqual(["new-task"], result["itemKeys"])
        self.assertEqual(["s2"], result["stageKeys"])
        self.assertEqual(["web"], result["moduleKeys"])

    def test_planning_uses_numeric_project_and_requirement_key_without_business_line(self):
        executor = bridge.ExecutionBridge(Path.cwd())

        with patch.object(bridge.planner, "request_api", return_value=[]):
            result = executor.planning(2, biz_line="whatsapp", config={"_project_id": 2}, requirement_key="req-a")

        self.assertEqual(2, result["programId"])
        self.assertEqual("req-a", result["requirementKey"])
        self.assertEqual("__project_planning__:req-a", executor._planning_item_key("req-a"))
        self.assertNotEqual(
            executor._planning_identity(2, "req-a"),
            executor._planning_identity(2, "req-b"),
        )

    def test_requirement_outline_reads_the_one_file_the_planning_session_may_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            missing = bridge.requirement_outline_document(workspace, "req-a")
            directory = workspace / "doc/requirements/req-a"
            directory.mkdir(parents=True)
            (directory / "需求大纲.md").write_text("# 需求大纲\n\n背景与目标", encoding="utf-8")

            document = bridge.requirement_outline_document(workspace, "req-a")

        self.assertEqual("doc/requirements/req-a/需求大纲.md", missing["path"])
        self.assertFalse(missing["exists"])
        self.assertTrue(document["exists"])
        self.assertIn("背景与目标", document["markdown"])
        self.assertTrue(document["updatedAt"])

    def test_requirement_outline_rejects_a_key_that_escapes_the_workspace(self):
        with self.assertRaisesRegex(bridge.BridgeFailure, "需求标识无效"):
            bridge.requirement_outline_path_of("../../etc")

    def test_planning_prompt_always_carries_the_requirement_outline_path(self):
        prompt = bridge.build_planning_prompt(
            1, {"program": {"name": "Universe"}}, "拆一下", requirement={"requirementKey": "req-a"},
        )

        self.assertIn("doc/requirements/req-a/需求大纲.md", prompt)

    def test_planning_prompt_lists_mentioned_requirement_outline_paths_without_their_content(self):
        prompt = bridge.build_planning_prompt(
            1, {"program": {"name": "Universe"}}, "拆一下",
            requirement={
                "requirementKey": "req-b",
                "references": [{"requirementKey": "req-a", "name": "任务面板"}],
            },
        )

        self.assertIn("doc/requirements/req-a/需求大纲.md", prompt)
        self.assertIn("@ 引用的历史需求: 任务面板", prompt)
        self.assertIn("需要参考时按上面的路径自行读取", prompt)

    def test_planning_prompt_lists_mentioned_task_document_paths(self):
        prompt = bridge.build_planning_prompt(
            1,
            {
                "program": {"name": "Universe"},
                "items": [{"itemKey": "task-a", "title": "存量任务", "moduleKey": "web"}],
            },
            "拆一下",
            requirement={
                "requirementKey": "req-b",
                "itemReferences": [{"itemKey": "task-a"}],
            },
        )

        self.assertIn("item_key: task-a", prompt)
        self.assertIn("doc/web/task-a/文档.md", prompt)
        self.assertIn("已有实现和约定的参考", prompt)

    def test_planning_payload_keeps_only_valid_and_unique_requirement_references(self):
        references = bridge.planning_requirement_of({
            "requirementKey": "req-b",
            "requirementReferences": [
                {"requirementKey": "req-a", "name": "任务面板"},
                {"requirementKey": "req-a", "name": "重复"},
                {"requirementKey": "../../etc", "name": "越界"},
                {"requirementKey": "req-c"},
                "不是对象",
            ],
        })["references"]

        self.assertEqual(
            [
                {"requirementKey": "req-a", "name": "任务面板"},
                {"requirementKey": "req-c", "name": "req-c"},
            ],
            references,
        )

    def test_planning_payload_keeps_only_valid_and_unique_task_references(self):
        references = bridge.planning_requirement_of({
            "requirementKey": "req-b",
            "requirementItemReferences": [
                {"itemKey": "task-a", "title": "不信任的标题"},
                {"itemKey": "task-a"},
                {"itemKey": "task.v1"},
                {"itemKey": "../../etc"},
                "不是对象",
            ],
        })["itemReferences"]

        self.assertEqual(
            [{"itemKey": "task-a"}, {"itemKey": "task.v1"}],
            references,
        )

    def test_planning_prompt_pre_generates_task_documents_only_when_enabled(self):
        prompt = bridge.build_planning_prompt(
            1, {"program": {"name": "Universe"}}, "确认并写入",
            requirement={"requirementKey": "req-a", "preGenerateTaskDocuments": True}, write_allowed=True,
        )
        without = bridge.build_planning_prompt(
            1, {"program": {"name": "Universe"}}, "确认并写入",
            requirement={"requirementKey": "req-a"}, write_allowed=True,
        )

        self.assertIn("doc/<moduleKey>/<itemKey>/文档.md", prompt)
        self.assertIn("预生成任务需求文档: 是", prompt)
        self.assertNotIn("doc/<moduleKey>/<itemKey>/文档.md", without)
        self.assertIn("预生成任务需求文档: 否（由任务梳理阶段创建）", without)

    def test_planning_payload_defaults_to_no_pre_generated_task_documents(self):
        self.assertFalse(bridge.planning_requirement_of({"requirementKey": "req-a"})["preGenerateTaskDocuments"])
        self.assertTrue(
            bridge.planning_requirement_of(
                {"requirementKey": "req-a", "requirementPreGenerateTaskDocuments": True},
            )["preGenerateTaskDocuments"]
        )
        self.assertTrue(
            bridge.planning_requirement_of(
                {"requirementKey": "req-a", "requirementGenerateTaskOutline": True},
            )["preGenerateTaskDocuments"]
        )

    def test_planning_prompt_forces_a_single_task_when_splitting_is_off(self):
        preview = bridge.build_planning_prompt(
            1, {"program": {"name": "Universe"}}, "拆一下",
            requirement={"requirementKey": "req-a", "splitTasks": False},
        )
        write = bridge.build_planning_prompt(
            1, {"program": {"name": "Universe"}}, "确认并写入",
            requirement={"requirementKey": "req-a", "splitTasks": False}, write_allowed=True,
        )
        split = bridge.build_planning_prompt(
            1, {"program": {"name": "Universe"}}, "拆一下", requirement={"requirementKey": "req-a"},
        )

        self.assertIn("只输出一条覆盖整条需求的任务", preview)
        self.assertIn("tasks 数组只能包含一条覆盖整条需求的任务", write)
        self.assertIn("拆解成多条任务: 否（只建一条任务）", write)
        self.assertNotIn("只输出一条覆盖整条需求的任务", split)
        self.assertIn("拆解成多条任务: 是", split)

    def test_single_task_planning_writes_the_complete_requirement_to_its_task_document(self):
        prompt = bridge.build_planning_prompt(
            1, {"program": {"name": "Universe"}}, "确认并写入",
            requirement={"requirementKey": "req-a", "splitTasks": False}, write_allowed=True,
        )

        self.assertIn("唯一业务任务（prototypeTask=false）", prompt)
        self.assertIn("直接创建或覆盖到该任务返回的 requirementDocumentPath", prompt)
        self.assertIn("不能只留在需求级大纲", prompt)
        self.assertIn("预生成任务需求文档: 是（单任务模式强制写入）", prompt)

    def test_planning_payload_defaults_to_splitting_for_older_clients(self):
        self.assertTrue(bridge.planning_requirement_of({"requirementKey": "req-a"})["splitTasks"])
        self.assertFalse(
            bridge.planning_requirement_of({"requirementKey": "req-a", "requirementSplitTasks": False})["splitTasks"]
        )

    def test_requirement_prototype_reads_only_bounded_html_files_in_its_fixed_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            directory = workspace / "doc/requirements/req-a/prototype"
            directory.mkdir(parents=True)
            (directory / "overview.html").write_text("<h1>Overview</h1>", encoding="utf-8")
            (directory / "notes.txt").write_text("not an HTML prototype", encoding="utf-8")

            path, files = bridge.requirement_prototype_files(workspace, "req-a")

        self.assertEqual("doc/requirements/req-a/prototype", path)
        self.assertEqual(["overview.html"], [entry["name"] for entry in files])
        self.assertEqual("doc/requirements/req-a/prototype/overview.html", files[0]["path"])

    def test_requirement_prototype_uses_project_scoped_backend_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            directory = workspace / "doc/requirements/req-a/prototype"
            directory.mkdir(parents=True)
            (directory / "overview.html").write_text("<h1>Overview</h1>", encoding="utf-8")
            executor = bridge.ExecutionBridge(workspace)
            requests = []

            def request_api(_config, method, path, query=None, body=None):
                requests.append((method, path, query, body))
                if path == "/delivery/requirement":
                    return {"requirementKey": "req-a", "generatePrototype": True}
                if path == "/delivery/requirement/prototype":
                    return {"path": "doc/requirements/req-a/prototype", "generatedAt": "2026-08-15T00:00:00Z"}
                self.fail(f"unexpected request: {path}")

            with patch.object(bridge.planner, "request_api", side_effect=request_api):
                result = executor.requirement_prototype(2, "req-a", config={"_project_id": 2})

        self.assertTrue(result["exists"])
        self.assertFalse(result["active"])
        self.assertEqual("2026-08-15T00:00:00Z", result["generatedAt"])
        self.assertEqual(["/delivery/requirement", "/delivery/requirement/prototype"], [request[1] for request in requests])

    def test_requirement_prototype_generation_persists_a_separate_session(self):
        with tempfile.TemporaryDirectory() as temporary:
            executor = bridge.ExecutionBridge(Path(temporary))
            client = unittest.mock.MagicMock()
            client.start_task.return_value = ("prototype-thread", "prototype-turn")
            requests = []

            def request_api(_config, method, path, query=None, body=None):
                requests.append((method, path, query, body))
                if path == "/delivery/requirement":
                    return {"requirementKey": "req-a", "name": "需求 A", "detail": "创建工作台", "generatePrototype": True}
                if path == "/delivery/requirement/planning-session/bind":
                    return None
                self.fail(f"unexpected request: {path}")

            with (
                patch.object(bridge.planner, "request_api", side_effect=request_api),
                patch.object(bridge, "create_ai_client", return_value=client),
                patch.object(bridge.threading, "Thread") as thread,
            ):
                result = executor.generate_requirement_prototype(
                    {"programId": 2, "requirementKey": "req-a", "provider": "codex"},
                    {"_project_id": 2},
                )

        self.assertTrue(result["accepted"])
        self.assertEqual("prototype-thread", result["threadId"])
        client.start_task.assert_called_once()
        self.assertEqual("codex-prototype", requests[-1][3]["executorType"])
        thread.return_value.start.assert_called_once()

    def test_planning_can_create_and_continue_a_requirement_conversation(self):
        executor = bridge.ExecutionBridge(Path.cwd())
        client = unittest.mock.MagicMock()
        client.start_task.return_value = ("thread-1", "turn-1")
        context = {"program": {"programId": 2, "name": "项目 2"}, "items": [], "stages": [], "modules": []}
        payload = {
            "programId": 2,
            "message": "拆解这个需求",
            "newConversation": True,
            "requirementKey": "req-a",
            "requirementName": "需求 A",
        }

        with (
            patch.object(bridge.planner, "project_context", return_value=context),
            patch.object(bridge.planner, "request_api", return_value=[]),
            patch.object(bridge, "create_ai_client", return_value=client),
            patch.object(bridge.threading, "Thread") as thread,
        ):
            created = executor.send_planning(payload, {"_project_id": 2})
            continued = executor.send_planning({**payload, "message": "补充验收标准", "newConversation": False}, {"_project_id": 2})

        self.assertTrue(created["accepted"])
        self.assertEqual(2, created["programId"])
        self.assertEqual("req-a", created["requirementKey"])
        self.assertEqual("thread-1", continued["threadId"])
        client.steer_turn.assert_called_once()
        thread.return_value.start.assert_called_once()

    def test_planning_keeps_the_conversation_list_when_the_transcript_is_not_on_this_machine(self):
        # 别人在自己电脑上聊出来的会话，本机读不到正文，需求编辑不该整页报错。
        executor = bridge.ExecutionBridge(Path.cwd())
        client = unittest.mock.MagicMock()
        client.read_thread.side_effect = bridge.BridgeFailure("thread not found")
        rows = [{"threadId": "thread-remote", "title": "别人的拆解", "status": "completed"}]

        with (
            patch.object(bridge.planner, "request_api", return_value=rows),
            patch.object(bridge, "create_ai_client", return_value=client),
        ):
            conversation = executor.planning(2, "thread-remote", config=self.runtime_config() | {"_project_id": 2}, requirement_key="req-a")

        self.assertEqual("thread-remote", conversation["threadId"])
        self.assertEqual([], conversation["turns"])
        self.assertEqual(["thread-remote"], [entry["threadId"] for entry in conversation["conversations"]])
        client.close.assert_called_once()

    def test_read_thread_or_empty_swallows_only_the_missing_transcript(self):
        client = unittest.mock.MagicMock()
        client.read_thread.return_value = {"turns": [{"id": "turn-1"}]}
        self.assertEqual({"turns": [{"id": "turn-1"}]}, bridge.read_thread_or_empty(client, "thread-1"))
        self.assertEqual({}, bridge.read_thread_or_empty(client, ""))
        client.read_thread.side_effect = bridge.BridgeFailure("thread not found")
        self.assertEqual({}, bridge.read_thread_or_empty(client, "thread-1"))

    def test_environment_selection_resolves_presets_and_keeps_custom_entries(self):
        selected = bridge.environment_selection_of(["python", "Node", "rust 1.79"])

        self.assertEqual(
            [("python", "3.11 及以上"), ("node", "22.0 及以上"), ("rust 1.79", "")],
            [(entry["id"], entry["requirement"]) for entry in selected],
        )

    def test_environment_selection_rejects_an_oversized_list(self):
        with self.assertRaises(bridge.BridgeFailure):
            bridge.environment_selection_of([f"custom-{index}" for index in range(bridge.MAX_ENVIRONMENT_SETUP_ITEMS + 1)])

    def test_environment_setup_payload_requires_something_to_install(self):
        with self.assertRaises(bridge.BridgeFailure):
            bridge.validate_environment_setup_payload({"programId": 1, "useGit": False, "environments": []})

    def test_environment_setup_prompt_only_lists_the_selected_environments(self):
        prompt = bridge.build_environment_setup_prompt(True, bridge.environment_selection_of(["go"]), "", True, host="macos")

        self.assertIn("git --version", prompt)
        self.assertIn("Host github.com", prompt)
        self.assertIn("id_ed25519_github_delivery_task_planner", prompt)
        self.assertIn("go version", prompt)
        self.assertNotIn("python3 --version", prompt)
        self.assertIn("只装缺的", prompt)

    def test_github_ssh_status_accepts_a_configured_public_key(self):
        public_key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDeliveryTaskPlanner github@example.test"
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            ssh_directory = home / ".ssh"
            ssh_directory.mkdir()
            (ssh_directory / "config").write_text("Host github.com\n  IdentityFile ~/.ssh/id_github\n", encoding="utf-8")
            (ssh_directory / "id_github.pub").write_text(f"{public_key}\n", encoding="utf-8")

            status = bridge.github_ssh_key_status(home)

        self.assertTrue(status["githubSshConfigured"])
        self.assertEqual(public_key, status["githubSshPublicKey"])
        self.assertFalse(status["githubSshError"])

    def test_github_ssh_status_rejects_missing_or_invalid_public_key(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            ssh_directory = home / ".ssh"
            ssh_directory.mkdir()
            (ssh_directory / "config").write_text("Host github.com\n  IdentityFile ~/.ssh/id_github\n", encoding="utf-8")
            (ssh_directory / "id_github.pub").write_text("not an ssh public key\n", encoding="utf-8")

            status = bridge.github_ssh_key_status(home)

        self.assertFalse(status["githubSshConfigured"])
        self.assertFalse(status["githubSshPublicKey"])

    def test_ensure_github_ssh_key_generates_a_managed_key_only_when_missing(self):
        public_key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDeliveryTaskPlanner generated"

        def generate_key(command, **_kwargs):
            private_key = Path(command[5])
            private_key.write_text("private key", encoding="utf-8")
            private_key.with_name(f"{private_key.name}.pub").write_text(f"{public_key}\n", encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "")

        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            with (
                patch.object(bridge.shutil, "which", return_value="/usr/bin/ssh-keygen"),
                patch.object(bridge.subprocess, "run", side_effect=generate_key) as run,
            ):
                status = bridge.ensure_github_ssh_key(home)

            config = (home / ".ssh" / "config").read_text(encoding="utf-8")
            private_key = home / ".ssh" / bridge.GITHUB_SSH_KEY_NAME
            private_key_exists = private_key.exists()

        self.assertTrue(status["githubSshConfigured"])
        self.assertEqual(public_key, status["githubSshPublicKey"])
        self.assertIn(bridge.GITHUB_SSH_CONFIG_START, config)
        self.assertIn("Host github.com", config)
        self.assertTrue(private_key_exists)
        self.assertEqual(1, run.call_count)

    def test_ensure_github_ssh_key_reuses_an_existing_valid_configuration(self):
        public_key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDeliveryTaskPlanner existing"
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            ssh_directory = home / ".ssh"
            ssh_directory.mkdir()
            config = ssh_directory / "config"
            original = "Host github.com\n  IdentityFile ~/.ssh/id_existing\n"
            config.write_text(original, encoding="utf-8")
            (ssh_directory / "id_existing.pub").write_text(f"{public_key}\n", encoding="utf-8")
            with patch.object(bridge.shutil, "which") as which:
                status = bridge.ensure_github_ssh_key(home)

            self.assertEqual(original, config.read_text(encoding="utf-8"))

        self.assertTrue(status["githubSshConfigured"])
        which.assert_not_called()

    def test_environment_setup_prompt_gives_macos_commands_on_a_mac(self):
        prompt = bridge.build_environment_setup_prompt(True, bridge.environment_selection_of(["python"]), "", True, host="macos")

        self.assertIn("本机系统是 macOS", prompt)
        self.assertIn("python3 --version", prompt)
        self.assertIn("brew install python@3.12", prompt)
        self.assertIn("不要用 sudo 跑 brew", prompt)
        self.assertNotIn("winget", prompt)
        self.assertNotIn("PowerShell", prompt)

    def test_environment_setup_prompt_gives_windows_commands_on_windows(self):
        prompt = bridge.build_environment_setup_prompt(True, bridge.environment_selection_of(["python"]), "", True, host="windows")

        self.assertIn("本机系统是 Windows", prompt)
        # Windows 上没有 python3 这个命令，检测命令必须换成 py -3。
        self.assertIn("py -3 --version", prompt)
        self.assertNotIn("python3 --version", prompt)
        self.assertIn("winget install --id Python.Python.3.12 -e", prompt)
        self.assertIn("winget install --id Git.Git -e", prompt)
        self.assertIn("PowerShell", prompt)
        self.assertIn("管理员 权限", prompt)
        self.assertNotIn("brew", prompt)

    def test_environment_setup_follow_up_prompt_names_the_host_privilege(self):
        self.assertIn("管理员 权限", bridge.build_environment_setup_prompt(True, [], "继续", False, host="windows"))
        self.assertIn("sudo 权限", bridge.build_environment_setup_prompt(True, [], "继续", False, host="macos"))

    def test_host_platform_maps_python_platform_names(self):
        for system, expected in [("Darwin", "macos"), ("Windows", "windows"), ("Linux", "linux")]:
            with patch.object(bridge.platform, "system", return_value=system):
                self.assertEqual(expected, bridge.host_platform())

    def test_environment_setup_runs_outside_any_project_workspace(self):
        executor = bridge.ExecutionBridge(Path.cwd())
        client = unittest.mock.MagicMock()
        client.start_task.return_value = ("thread-env", "turn-env")

        with (
            patch.object(bridge, "create_ai_client", return_value=client) as create_client,
            patch.object(bridge, "ensure_github_ssh_key", return_value={"githubSshConfigured": True, "githubSshPublicKey": "public", "githubSshError": ""}),
            patch.object(bridge.ENVIRONMENT_SETUP_SESSIONS, "load", return_value=None),
            patch.object(bridge.ENVIRONMENT_SETUP_SESSIONS, "save") as save,
            patch.object(bridge.threading, "Thread") as thread,
        ):
            result = executor.send_environment_setup(
                {"useGit": True, "environments": ["python"], "provider": "codex"},
                {"key": "current-user-token"},
            )

        self.assertEqual("thread-env", result["threadId"])
        self.assertEqual(bridge.GLOBAL_ENVIRONMENT_SETUP_PROGRAM_ID, result["programId"])
        self.assertEqual(bridge.environment_setup_workspace(), create_client.call_args.args[1])
        self.assertEqual("codex:0", save.call_args.args[0])
        self.assertEqual("0", create_client.call_args.args[3][bridge.planner.RUNTIME_PROJECT_ID_ENV])
        thread.return_value.start.assert_called_once()

    def test_environment_setup_conversation_is_empty_before_the_first_run(self):
        executor = bridge.ExecutionBridge(Path.cwd())

        with patch.object(bridge.ENVIRONMENT_SETUP_SESSIONS, "load", return_value=None):
            conversation = executor.environment_setup(bridge.GLOBAL_ENVIRONMENT_SETUP_PROGRAM_ID, config={"key": "current-user-token"})

        self.assertEqual(
            {
                "programId": bridge.GLOBAL_ENVIRONMENT_SETUP_PROGRAM_ID,
                "threadId": "",
                "turns": [],
                "conversations": [],
                "active": False,
                "activeTurnId": "",
                "environmentStatuses": [],
            },
            conversation,
        )

    def test_environment_probe_status_marks_only_supported_versions_as_installed(self):
        completed = subprocess.CompletedProcess(["python3", "--version"], 0, "Python 3.12.1\n")
        with patch.object(bridge.subprocess, "run", return_value=completed):
            status = bridge.environment_probe_status(bridge.environment_selection_of(["python"])[0], "macos")

        self.assertEqual({"id": "python", "installed": True, "version": "3.12.1"}, status)

    def test_environment_probe_status_does_not_mark_outdated_version_as_installed(self):
        completed = subprocess.CompletedProcess(["node", "--version"], 0, "v20.19.0\n")
        with patch.object(bridge.subprocess, "run", return_value=completed):
            status = bridge.environment_probe_status(bridge.environment_selection_of(["node"])[0], "macos")

        self.assertEqual({"id": "node", "installed": False, "version": "20.19.0"}, status)

    def test_planning_previews_before_confirmation_and_writes_after(self):
        executor = bridge.ExecutionBridge(Path.cwd())
        client = unittest.mock.MagicMock()
        client.start_task.return_value = ("thread-1", "turn-1")
        client.start_turn.return_value = "turn-2"
        context = {"program": {"programId": 2, "name": "项目 2"}, "items": [], "stages": [], "modules": []}
        config = {"api_url": "http://test/api", "key": "k", "_project_id": 2}
        payload = {"programId": 2, "message": "拆解这个需求", "newConversation": True, "requirementKey": "req-a"}
        environments = []
        planning_sessions = []

        def make_client(provider, workspace, listener=None, environment=None):
            environments.append(environment or {})
            return client

        def request_api(_config, method, path, query=None, body=None):
            if method == "GET" and path == "/delivery/requirement/planning-sessions":
                return list(planning_sessions)
            if method == "POST" and path == "/delivery/requirement/planning-session/bind":
                row = {
                    "threadId": body["threadId"],
                    "title": body["title"],
                    "status": body["status"],
                    "metadata": body["metadata"],
                }
                planning_sessions[:] = [entry for entry in planning_sessions if entry["threadId"] != row["threadId"]]
                planning_sessions.append(row)
                return None
            self.fail(f"unexpected request: {method} {path}")

        with (
            patch.object(bridge.planner, "project_context", return_value=context),
            patch.object(bridge.planner, "request_api", side_effect=request_api),
            patch.object(bridge, "create_ai_client", side_effect=make_client),
            patch.object(bridge.threading, "Thread"),
        ):
            executor.send_planning(payload, config)
            # 第一轮跑完之后才轮到确认：模拟回合结束。
            with executor.lock:
                executor.active_runs.clear()
                executor.active.clear()
            executor.send_planning({**payload, "message": "确认", "newConversation": False, "confirmWrite": True}, config)

        preview_prompt = client.start_task.call_args[0][1]
        write_prompt = client.start_turn.call_args[0][1]
        self.assertIn("禁止调用 create_task_board_tasks", preview_prompt)
        self.assertIn("已授予项目工作目录及需求指定关联目录的只读勘察权限", preview_prompt)
        self.assertIn("终端的只读命令", preview_prompt)
        self.assertIn("收益标签 / 负责人", preview_prompt)
        self.assertIn("preview", environments[0][bridge.planner.RUNTIME_WRITE_MODE_ENV])
        self.assertIn("确认并写入", write_prompt)
        self.assertIn("任务负责人由写入工具", write_prompt)
        self.assertEqual("write", environments[1][bridge.planner.RUNTIME_WRITE_MODE_ENV])
        # 面板上下文整段裹在标记里，聊天记录只回显用户自己输入的那句。
        self.assertEqual("拆解这个需求", bridge.text_without_attachment_context(preview_prompt))

    def test_planning_rejects_confirmation_without_a_previous_preview(self):
        executor = bridge.ExecutionBridge(Path.cwd())
        context = {"program": {"programId": 2, "name": "项目 2"}, "items": [], "stages": [], "modules": []}
        with (
            patch.object(bridge.planner, "project_context", return_value=context),
            patch.object(bridge.planner, "request_api", return_value=[]),
            self.assertRaisesRegex(bridge.BridgeFailure, "先梳理需求"),
        ):
            executor.send_planning(
                {"programId": 2, "message": "确认", "newConversation": True, "requirementKey": "req-a", "confirmWrite": True},
                {"api_url": "http://test/api", "key": "k", "_project_id": 2},
            )

    def test_transcript_shows_only_what_the_user_typed(self):
        task = {"itemKey": "t1", "title": "导出功能", "phase": "development", "moduleKey": "api", "dependsOnItemKeys": []}
        execution = bridge.build_task_prompt({"programId": 2, "task": task, "followUp": "兼容旧格式"})
        plain_execution = bridge.build_task_prompt({"programId": 2, "task": task})
        conversation = bridge.build_conversation_prompt(2, task, "帮我看下这个报错")

        # 组装出来的面板上下文只给执行器，聊天记录里不该出现。
        self.assertIn("delivery-action-execution", execution)
        self.assertEqual("执行「动作执行」阶段：导出功能\n\n兼容旧格式", bridge.text_without_attachment_context(execution))
        self.assertEqual("执行「动作执行」阶段：导出功能", bridge.text_without_attachment_context(plain_execution))
        self.assertEqual("帮我看下这个报错", bridge.text_without_attachment_context(conversation))

    def test_prototype_task_prompt_requires_a_real_image_in_the_task_directory(self):
        task = {
            "itemKey": "prototype-1",
            "title": "生成需求原型图",
            "phase": "requirement",
            "moduleKey": "web",
            "prototypeTask": True,
        }

        prompt = bridge.build_task_prompt({"programId": 2, "task": task})

        self.assertIn("图像生成能力", prompt)
        self.assertIn("doc/web/prototype-1/prototype/", prompt)

    def test_prototype_directory_is_task_scoped_and_openable_only_after_image_exists(self):
        with tempfile.TemporaryDirectory() as workspace:
            root = Path(workspace)
            image_path = root / "doc" / "web" / "prototype-1" / "prototype" / "screen.png"
            image_path.parent.mkdir(parents=True)
            image_path.write_bytes(b"png")
            executor = bridge.ExecutionBridge(root)
            task = {
                "itemKey": "prototype-1",
                "moduleKey": "web",
                "requirementDocumentPath": "doc/web/prototype-1/文档.md",
                "prototypeTask": True,
            }
            with (
                patch.object(executor, "_task_detail", return_value=task),
                patch.object(bridge.shutil, "which", return_value="/usr/bin/open"),
                patch.object(bridge.subprocess, "Popen") as open_directory,
            ):
                directory = executor.prototype_directory(1, "prototype-1", config=self.runtime_config())
                opened = executor.open_prototype_directory(1, "prototype-1", config=self.runtime_config())

        self.assertTrue(directory["exists"])
        self.assertEqual(1, directory["imageCount"])
        self.assertEqual(image_path.parent.resolve(), Path(directory["path"]).resolve())
        self.assertEqual(directory, opened)
        open_directory.assert_called_once_with(
            ["/usr/bin/open", directory["path"]],
            stdout=bridge.subprocess.DEVNULL,
            stderr=bridge.subprocess.DEVNULL,
        )

    def test_legacy_planning_context_marker_is_still_stripped(self):
        legacy = "<delivery-planning-context>\n项目 program_id: 2\n</delivery-planning-context>\n\n拆解这个需求"

        self.assertEqual("拆解这个需求", bridge.text_without_attachment_context(legacy))

    def test_claude_stream_maps_tool_calls_and_survives_a_new_client(self):
        events = [
            {"type": "system", "subtype": "init", "session_id": "s-1"},
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "先看一眼配置"},
                        {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "go build ./..."}},
                        {"type": "tool_use", "id": "t2", "name": "Edit", "input": {"file_path": "server/main.go"}},
                        {"type": "tool_use", "id": "t3", "name": "Write", "input": {"file_path": "doc/new.md"}},
                    ]
                },
            },
            {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "is_error": True}]}},
            {"type": "result", "result": "改完了：修改 server/main.go，新增 doc/new.md"},
        ]
        with tempfile.TemporaryDirectory() as workspace:
            store = bridge.ClaudeTranscriptStore(Path(workspace) / "transcripts")
            client = bridge.ClaudeCLIClient(Path(workspace), transcripts=store)
            client.transcript_key = "thread-1"
            client.thread_id = "thread-1"
            turn = {"id": "turn-1", "status": "running", "items": []}
            client.turns = [turn]
            client.process = unittest.mock.MagicMock()
            client.process.stdout = [json.dumps(event) + "\n" for event in events]
            client.process.wait.return_value = 0

            client._consume(turn)

            # 换一个客户端实例（面板轮询时每次都是新的），历史必须还能读回来。
            reader = bridge.ClaudeCLIClient(Path(workspace), transcripts=store)
            turns = reader.read_thread("thread-1")["turns"]

        items = turns[0]["items"]
        types = [item["type"] for item in items]
        self.assertEqual(["agentMessage", "commandExecution", "fileChange", "fileChange", "agentMessage"], types)
        self.assertEqual("failed", items[1]["status"])
        self.assertEqual(1, items[1]["exitCode"])
        self.assertEqual([{"path": "server/main.go", "kind": "modify"}], items[2]["changes"])
        self.assertEqual([{"path": "doc/new.md", "kind": "add"}], items[3]["changes"])
        self.assertEqual("final_answer", items[4]["phase"])
        self.assertEqual("completed", turns[0]["status"])

    def test_serialized_file_changes_carry_normalized_kinds(self):
        turns = bridge.serialize_turns(
            [
                {
                    "id": "turn-1",
                    "status": "completed",
                    "items": [
                        {"id": "i1", "type": "fileChange", "changes": [
                            {"path": "a.go", "kind": "added"},
                            {"path": "b.go", "type": "deleted"},
                            {"path": "c.go"},
                        ]},
                    ],
                }
            ]
        )

        self.assertEqual(
            [{"path": "a.go", "kind": "add"}, {"path": "b.go", "kind": "delete"}, {"path": "c.go", "kind": "modify"}],
            turns[0]["items"][0]["changes"],
        )

    def test_validate_planning_payload_rejects_unknown_task_kind(self):
        with self.assertRaisesRegex(bridge.BridgeFailure, "任务类型无效"):
            bridge.validate_planning_payload({"bizLine": "whatsapp", "programId": 1, "message": "build", "kind": "other"})

    def test_ai_provider_defaults_to_codex_and_rejects_unknown_provider(self):
        self.assertEqual("codex", bridge.ai_provider_of({}))
        self.assertEqual("claude", bridge.ai_provider_of({"provider": " Claude "}))
        with self.assertRaisesRegex(bridge.BridgeFailure, "codex 或 claude"):
            bridge.ai_provider_of({"provider": "other"})

    def test_reasoning_effort_supports_provider_specific_levels(self):
        self.assertEqual("medium", bridge.reasoning_effort_of({"reasoningEffort": "medium"}))
        self.assertEqual("max", bridge.reasoning_effort_of({"reasoningEffort": "max"}, "claude"))
        with self.assertRaisesRegex(bridge.BridgeFailure, "推理强度无效"):
            bridge.reasoning_effort_of({"reasoningEffort": "max"})
        with self.assertRaisesRegex(bridge.BridgeFailure, "推理强度无效"):
            bridge.reasoning_effort_of({"reasoningEffort": "xhigh"}, "claude")

    def test_fast_mode_is_only_enabled_for_claude(self):
        self.assertTrue(bridge.fast_mode_of({"fastMode": True}, "claude"))
        self.assertFalse(bridge.fast_mode_of({"fastMode": True}, "codex"))
        with self.assertRaisesRegex(bridge.BridgeFailure, "布尔值"):
            bridge.fast_mode_of({"fastMode": "yes"}, "claude")

    def test_claude_models_use_cli_aliases(self):
        client = bridge.ClaudeCLIClient(Path.cwd())
        self.assertEqual(["opus", "sonnet"], [item["model"] for item in client.list_models()])

    def test_claude_cli_receives_model_effort_and_fast_mode(self):
        client = bridge.ClaudeCLIClient(Path.cwd())
        process = unittest.mock.MagicMock()
        with patch.object(bridge.shutil, "which", return_value="/bin/claude"), patch.object(
            bridge.subprocess, "Popen", return_value=process,
        ) as popen, patch.object(bridge.threading, "Thread"):
            client.start_task("Task", "Prompt", model="opus", reasoning_effort="high", fast_mode=True)

        command = popen.call_args.args[0]
        self.assertIn("opus", command)
        self.assertEqual("high", command[command.index("--effort") + 1])
        self.assertIn("--fast", command)
        self.assertEqual("utf-8", popen.call_args.kwargs["encoding"])

    def test_codex_models_are_limited_to_the_product_catalog(self):
        executor = bridge.ExecutionBridge(Path.cwd())

        catalog = executor.models(self.runtime_config(), "codex")

        self.assertEqual("gpt-5.6-terra", catalog["defaultModel"])
        self.assertEqual(
            ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"],
            [item["model"] for item in catalog["models"]],
        )

    def test_health_reports_each_provider_independently(self):
        executor = bridge.ExecutionBridge(Path.cwd())
        with patch.object(bridge.shutil, "which", side_effect=lambda name: "/bin/tool" if name == "codex" else None):
            self.assertTrue(executor.health("codex")["ready"])
            claude_health = executor.health("claude")
        self.assertFalse(claude_health["ready"])
        self.assertEqual("claude", claude_health["executorType"])
        self.assertIn("Claude CLI", claude_health["message"])

    def test_windows_health_recognizes_a_copied_codex_executable(self):
        with tempfile.TemporaryDirectory() as directory:
            copied_cli = Path(directory) / "bin" / "codex.exe"
            copied_cli.parent.mkdir()
            copied_cli.write_bytes(b"codex")
            executor = bridge.ExecutionBridge(Path.cwd())
            with patch.object(bridge.shutil, "which", return_value=None):
                with patch.object(bridge, "host_platform", return_value="windows"):
                    with patch.object(bridge, "RUNTIME_DIR", Path(directory)):
                        self.assertTrue(executor.health("codex")["ready"])

    def test_codex_desktop_resource_is_copied_when_the_cli_is_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "resources" / "codex.exe"
            source.parent.mkdir()
            source.write_bytes(b"desktop-codex")
            runtime = root / "runtime"
            with patch.object(bridge.shutil, "which", return_value=None):
                with patch.object(bridge, "codex_desktop_resource_paths", return_value=[source]):
                    copied = bridge.provision_codex_cli("windows", runtime)

            self.assertEqual(runtime / "bin" / "codex.exe", Path(copied))
            self.assertEqual(b"desktop-codex", Path(copied).read_bytes())

    def test_bridge_rejects_project_code_as_program_id(self):
        with self.assertRaisesRegex(bridge.BridgeFailure, "数值主键"):
            bridge.program_id_of("universe")

    def test_workspace_path_requires_an_existing_absolute_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(Path(directory).resolve(), bridge.workspace_path_of(directory))
        with self.assertRaisesRegex(bridge.BridgeFailure, "项目管理中确认"):
            bridge.workspace_path_of("")
        with self.assertRaisesRegex(bridge.BridgeFailure, "绝对路径"):
            bridge.workspace_path_of("relative/project")
        with self.assertRaisesRegex(bridge.BridgeFailure, "不存在"):
            bridge.workspace_path_of("/path/that/does/not/exist")

    def test_execution_bridge_reuses_and_isolates_workspace_contexts(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            executor = bridge.ExecutionBridge(Path(first))
            selected = executor.for_workspace(second)

            self.assertIs(selected, executor.for_workspace(second))
            self.assertEqual(Path(second).resolve(), selected.workspace)
            self.assertIs(executor.progress, selected.progress)
            self.assertIs(executor.pending_session_syncs, selected.pending_session_syncs)
            self.assertIsNot(executor.attachments, selected.attachments)

    def test_codex_local_projects_returns_existing_roots(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            workspace = Path(directory) / "universe"
            workspace.mkdir()
            state_path.write_text(json.dumps({
                "local-projects": {
                    "project-1": {"id": "project-1", "name": "universe", "rootPaths": [str(workspace)]},
                    "project-2": {"id": "project-2", "name": "missing", "rootPaths": [str(workspace / "missing")]},
                }
            }), encoding="utf-8")
            with patch.object(bridge, "CODEX_GLOBAL_STATE_PATH", state_path):
                projects = bridge.codex_local_projects()

            self.assertEqual([{"id": "project-1", "name": "universe", "rootPaths": [str(workspace.resolve())]}], projects)

    def test_generated_image_event_supports_rollout_and_app_server_shapes(self):
        encoded = base64.b64encode(b"\x89PNG\r\n\x1a\nimage").decode("ascii")

        self.assertEqual(
            ("ig-1", encoded),
            bridge.generated_image_from_event({
                "type": "event_msg",
                "payload": {"type": "image_generation_end", "call_id": "ig-1", "result": encoded},
            }),
        )
        self.assertEqual(
            ("ig-2", encoded),
            bridge.generated_image_from_event({
                "method": "item/completed",
                "params": {"item": {"type": "imageGeneration", "callId": "ig-2", "result": encoded}},
            }),
        )

    def test_progress_event_formats_agent_message_without_protocol_json(self):
        event = bridge.progress_event_of(
            {
                "method": "item/completed",
                "params": {"item": {"type": "agentMessage", "text": "正在检查数据同步实现。"}},
            }
        )

        self.assertEqual(("message", "Codex 进度", "正在检查数据同步实现。", "success"), event)

    def test_completed_command_does_not_look_like_terminal_turn(self):
        event = bridge.progress_event_of(
            {
                "method": "item/completed",
                "params": {"item": {"type": "commandExecution", "command": "go test ./...", "exitCode": 0}},
            }
        )

        self.assertEqual("success", event[3])

    def test_progress_store_keeps_readable_events(self):
        store = bridge.ProgressStore()
        store.publish(("whatsapp", 1, "a"), "command", "正在执行命令", "go test ./...")

        self.assertEqual("go test ./...", store.snapshot(("whatsapp", 1, "a"))[0]["body"])

    def test_progress_store_cursor_advances_after_retention_limit(self):
        store = bridge.ProgressStore()
        identity = ("whatsapp", 1, "a")
        for index in range(501):
            store.publish(identity, "message", "progress", str(index))

        events, cursor = store.wait(identity, 500, timeout=0)

        self.assertEqual(["501"], [event["id"] for event in events])
        self.assertEqual(501, cursor)
        self.assertEqual(500, len(store.snapshot(identity)))

    def test_turn_completed_waits_for_board_sync_before_terminal_event(self):
        event = bridge.progress_event_of(
            {"method": "turn/completed", "params": {"turn": {"status": "completed"}}}
        )

        self.assertEqual("running", event[3])

    def test_pending_session_sync_store_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pending.json"
            entry = {"programId": 1, "itemKey": "a", "executorType": "codex", "version": 2}
            bridge.PendingSessionSyncStore(path).add(entry)

            restored = bridge.PendingSessionSyncStore(path)

            self.assertEqual([entry], restored.snapshot())
            restored.remove(entry)
            self.assertEqual([], restored.snapshot())

    def test_pending_session_sync_store_removes_legacy_entry_after_business_line_upgrade(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pending.json"
            entry = {"programId": 1, "itemKey": "a", "executorType": "codex", "version": 2}
            path.write_text(
                json.dumps({bridge.PendingSessionSyncStore.legacy_key_of(entry): entry}),
                encoding="utf-8",
            )

            store = bridge.PendingSessionSyncStore(path)
            store.remove({**entry, "bizLine": "whatsapp"})

            self.assertEqual([], store.snapshot())

    def test_app_server_requests_use_full_access_and_default_model(self):
        client = bridge.AppServerClient.__new__(bridge.AppServerClient)
        client.workspace = Path("/tmp/delivery-workspace")
        requests = []

        def send(method, request_id, params):
            requests.append((method, request_id, params))

        client.send = send
        client.wait_response = unittest.mock.MagicMock(
            side_effect=[
                {"thread": {"id": "thread-1", "sessionId": "session-1"}},
                {},
                {"turn": {"id": "turn-1"}},
            ]
        )

        thread_id, turn_id = client.start_task("Task title", "Task prompt")

        self.assertEqual("thread-1", thread_id)
        self.assertEqual("turn-1", turn_id)
        self.assertEqual("danger-full-access", requests[0][2]["sandbox"])
        self.assertNotIn("model", requests[0][2])
        self.assertEqual("user", requests[0][2]["threadSource"])
        self.assertFalse(requests[0][2]["ephemeral"])
        self.assertNotIn("serviceName", requests[0][2])
        self.assertEqual("dangerFullAccess", requests[2][2]["sandboxPolicy"]["type"])
        self.assertNotIn("model", requests[2][2])

    def test_codex_environment_is_limited_to_the_current_board_project(self):
        config = self.runtime_config()

        environment = bridge.codex_environment(config, 1)

        self.assertEqual("1", environment[bridge.planner.RUNTIME_PROJECT_ID_ENV])
        self.assertEqual("current-user-token", environment[bridge.planner.RUNTIME_TOKEN_ENV])
        self.assertNotIn("DELIVERY_TASK_BOARD_BIZ_LINE", environment)
        self.assertNotIn("_project_id", environment)

    def test_codex_environment_keeps_claude_in_write_mode_even_in_a_preview_turn(self):
        config = self.runtime_config()

        preview = bridge.codex_environment(config, 1, write_allowed=False)
        claude_preview = bridge.codex_environment(config, 1, write_allowed=False, provider="claude")

        self.assertEqual("preview", preview[bridge.planner.RUNTIME_WRITE_MODE_ENV])
        self.assertEqual("write", claude_preview[bridge.planner.RUNTIME_WRITE_MODE_ENV])

    def test_codex_environment_rejects_a_different_project(self):
        with self.assertRaisesRegex(bridge.BridgeFailure, "项目不一致"):
            bridge.codex_environment(self.runtime_config(), 2)

    def test_app_server_passes_selected_model_to_thread_and_turn(self):
        client = bridge.AppServerClient.__new__(bridge.AppServerClient)
        client.workspace = Path("/tmp/delivery-workspace")
        requests = []
        client.send = lambda method, request_id, params: requests.append((method, request_id, params))
        client.wait_response = unittest.mock.MagicMock(
            side_effect=[{"thread": {"id": "thread-1"}}, {}, {"turn": {"id": "turn-1"}}]
        )

        client.start_task("Task title", "Task prompt", model="gpt-5.6-sol", reasoning_effort="high")

        self.assertEqual("gpt-5.6-sol", requests[0][2]["model"])
        self.assertEqual("gpt-5.6-sol", requests[2][2]["model"])
        self.assertNotIn("effort", requests[0][2])
        self.assertEqual("high", requests[2][2]["effort"])

    def test_app_server_can_resume_start_steer_and_interrupt_a_thread(self):
        client = bridge.AppServerClient.__new__(bridge.AppServerClient)
        client.workspace = Path("/tmp/delivery-workspace")
        requests = []
        client.send = lambda method, request_id, params: requests.append((method, request_id, params))
        client.wait_response = unittest.mock.MagicMock(
            side_effect=[{"thread": {"id": "thread-1"}}, {"turn": {"id": "turn-2"}}, {"turnId": "turn-2"}, {}]
        )

        client.resume_thread("thread-1")
        turn_id = client.start_turn("thread-1", "Please also cover retries.", reasoning_effort="xhigh")
        client.steer_turn("thread-1", turn_id, "Focus on the failing test.")
        client.interrupt_turn("thread-1", turn_id)

        self.assertEqual("thread-1", client.thread_id)
        self.assertEqual("thread/resume", requests[0][0])
        self.assertEqual("turn/start", requests[1][0])
        self.assertEqual("turn/steer", requests[2][0])
        self.assertEqual("turn/interrupt", requests[3][0])
        self.assertEqual("turn-2", requests[3][2]["turnId"])
        self.assertEqual("dangerFullAccess", requests[1][2]["sandboxPolicy"]["type"])
        self.assertEqual("xhigh", requests[1][2]["effort"])

    def test_app_server_sends_images_as_local_image_inputs(self):
        client = bridge.AppServerClient.__new__(bridge.AppServerClient)
        client.workspace = Path("/tmp/delivery-workspace")
        requests = []
        client.send = lambda method, request_id, params: requests.append((method, request_id, params))
        client.wait_response = unittest.mock.MagicMock(side_effect=[{"turn": {"id": "turn-1"}}])

        client.start_turn(
            "thread-1",
            "Review this screenshot",
            [{"path": "/tmp/attachment.png", "isImage": True}, {"path": "/tmp/spec.pdf", "isImage": False}],
        )

        self.assertEqual(
            [{"type": "text", "text": "Review this screenshot"}, {"type": "localImage", "path": "/tmp/attachment.png"}],
            requests[0][2]["input"],
        )

    def test_serialize_turns_projects_only_browser_safe_conversation_items(self):
        turns = bridge.serialize_turns(
            [{
                "id": "turn-1",
                "status": "completed",
                "items": [
                    {"id": "u1", "type": "userMessage", "content": [{"type": "text", "text": "Implement it"}]},
                    {"id": "a1", "type": "agentMessage", "text": "Implemented and verified."},
                    {"id": "c1", "type": "commandExecution", "command": ["go test ./..."], "exitCode": 0},
                    {"id": "f1", "type": "fileChange", "changes": [{"path": "service/item.go", "kind": "modify"}]},
                ],
            }]
        )

        self.assertEqual("Implement it", turns[0]["items"][0]["text"])
        self.assertEqual("agentMessage", turns[0]["items"][1]["type"])
        self.assertEqual("go test ./...", turns[0]["items"][2]["text"])
        self.assertEqual("service/item.go", turns[0]["items"][3]["text"])

    def test_attachment_store_scopes_uploads_to_the_owning_task(self):
        with tempfile.TemporaryDirectory() as directory:
            store = bridge.ConversationAttachmentStore(Path(directory))
            saved = store.save("whatsapp", 1, "a", [{"name": "design.png", "contentType": "image/png", "data": b"image"}])

            resolved = store.resolve(1, "a", [saved[0]["id"]])
            self.assertTrue(resolved[0]["isImage"])
            self.assertEqual("design.png", saved[0]["name"])
            self.assertEqual(b"image", Path(resolved[0]["path"]).read_bytes())
            with self.assertRaises(bridge.BridgeFailure):
                store.resolve(1, "other-task", [saved[0]["id"]])

    def test_workspace_artifacts_are_attached_to_file_change_items(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            image_path = workspace / "output" / "result.png"
            image_path.parent.mkdir()
            image_path.write_bytes(b"png-data")
            store = bridge.WorkspaceArtifactStore(workspace)

            turns = bridge.serialize_turns(
                [{
                    "id": "turn-1",
                    "items": [{
                        "id": "file-1",
                        "type": "fileChange",
                        "changes": [{"path": "output/result.png", "kind": "add"}],
                    }],
                }],
            artifact_resolver=lambda paths: store.register("whatsapp", 1, "a", paths),
            )

            attachment = turns[0]["items"][0]["attachments"][0]
            self.assertEqual("result.png", attachment["name"])
            self.assertTrue(attachment["isImage"])
            manifest, downloaded = store.download(attachment["id"])
            self.assertEqual("output/result.png", manifest["relativePath"])
            self.assertEqual(image_path.resolve(), downloaded)

    def test_final_markdown_file_link_becomes_workspace_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            report = workspace / "output" / "report.pdf"
            report.parent.mkdir()
            report.write_bytes(b"pdf-data")
            store = bridge.WorkspaceArtifactStore(workspace)
            turns = bridge.serialize_turns(
                [{"items": [{
                    "id": "answer-1",
                    "type": "agentMessage",
                    "phase": "final_answer",
                    "text": "已生成 [报告](output/report.pdf)。",
                }]}],
            artifact_resolver=lambda paths: store.register("whatsapp", 1, "a", paths),
            )

            attachment = turns[0]["items"][0]["attachments"][0]
            self.assertEqual("report.pdf", attachment["name"])
            self.assertEqual("output/report.pdf", attachment["relativePath"])

    def test_workspace_artifacts_hide_sensitive_and_outside_files(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / ".env").write_text("SECRET=x", encoding="utf-8")
            outside = workspace.parent / "outside-delivery-artifact.txt"
            outside.write_text("outside", encoding="utf-8")
            try:
                store = bridge.WorkspaceArtifactStore(workspace)
                self.assertEqual([], store.register("whatsapp", 1, "a", [".env", str(outside)]))
            finally:
                outside.unlink(missing_ok=True)

    def test_generated_image_is_recovered_and_attached_to_its_turn(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as codex_home:
            workspace = Path(directory)
            thread_id = "019ff91e-87e2-7bf2-802a-6d8be7d0d87f"
            turn_id = "turn-image"
            encoded = base64.b64encode(b"\x89PNG\r\n\x1a\nimage-data").decode("ascii")
            session = Path(codex_home) / ".codex/sessions/2026/08/13" / f"rollout-{thread_id}.jsonl"
            session.parent.mkdir(parents=True)
            events = [
                {"type": "event_msg", "payload": {"type": "task_started", "turn_id": turn_id}},
                {"type": "event_msg", "payload": {
                    "type": "image_generation_end", "call_id": "ig-1", "result": encoded,
                }},
            ]
            session.write_text("\n".join(json.dumps(event) for event in events), encoding="utf-8")
            store = bridge.ConversationAttachmentStore(workspace)
            with patch.object(bridge.Path, "home", return_value=Path(codex_home)):
                store.recover_generated_images("whatsapp", 1, "a", thread_id)
            turns = bridge.serialize_turns(
                [{"id": turn_id, "items": [{
                    "id": "answer", "type": "agentMessage", "phase": "final_answer", "text": "图片已生成",
                }]}],
                turn_attachment_resolver=lambda current_turn: store.generated_for_turn(
                    1, "a", thread_id, current_turn
                ),
            )

            attachment = turns[0]["items"][0]["attachments"][0]
            self.assertTrue(attachment["isImage"])
            self.assertTrue(attachment["url"].startswith("/v1/codex/attachments/"))

    def test_serialized_user_message_keeps_attachments_but_hides_bridge_context(self):
        attachment_id = "abcdefghijklmnop"
        turns = bridge.serialize_turns(
            [{
                "id": "turn-1",
                "items": [{
                    "id": "u1",
                    "type": "userMessage",
                    "content": [{
                        "type": "text",
                        "text": "Review this\n<delivery-task-attachments>hidden</delivery-task-attachments>\n<!-- delivery-task-attachments:abcdefghijklmnop -->",
                    }],
                }],
            }],
            lambda ids: [{"id": attachment_id, "name": "design.png", "isImage": True}] if ids == [attachment_id] else [],
        )

        self.assertEqual("Review this", turns[0]["items"][0]["text"])
        self.assertEqual("design.png", turns[0]["items"][0]["attachments"][0]["name"])

    def test_conversation_catalog_keeps_legacy_thread_and_persists_multiple_threads(self):
        binding = {
            "externalSessionId": "thr_legacy",
            "status": "completed",
            "metadata": {
                "conversations": [
                    {"threadId": "thr_older", "title": "Earlier", "status": "completed", "updatedAt": "2026-08-10T00:00:00+00:00"},
                ],
            },
        }

        metadata = bridge.conversation_metadata(binding, "thr_new", "turn_new", "running", "New request")

        self.assertEqual("thr_new", metadata["threadId"])
        self.assertEqual("turn_new", metadata["turnId"])
        self.assertEqual({"thr_older", "thr_legacy", "thr_new"}, {entry["threadId"] for entry in metadata["conversations"]})
        newest = next(entry for entry in metadata["conversations"] if entry["threadId"] == "thr_new")
        self.assertEqual("New request", newest["title"])
        self.assertEqual("running", newest["status"])

    def test_conversation_titles_use_task_name_then_ascending_versions(self):
        task = {"title": "优化任务状态面板"}

        first_title = bridge.conversation_title(task)
        first_metadata = bridge.conversation_metadata(None, "thr_first", "turn_first", "running", first_title)
        first_binding = {"metadata": first_metadata}
        second_title = bridge.conversation_title(task, first_binding)
        second_metadata = bridge.conversation_metadata(first_binding, "thr_second", "turn_second", "running", second_title)
        second_binding = {"metadata": second_metadata}

        self.assertEqual("优化任务状态面板", first_title)
        self.assertEqual("优化任务状态面板 V0.0.1", second_title)
        self.assertEqual("优化任务状态面板 V0.0.2", bridge.conversation_title(task, second_binding))
        self.assertEqual(2, second_metadata["nextConversationVersion"])

    def test_conversation_version_remains_unique_after_history_is_compacted(self):
        task = {"title": "实现会话标题"}
        binding = {
            "metadata": {
                "nextConversationVersion": bridge.MAX_CONVERSATIONS_PER_TASK,
                "conversations": [
                    {"threadId": f"thr_{index}", "title": f"历史 {index}"}
                    for index in range(bridge.MAX_CONVERSATIONS_PER_TASK)
                ],
            }
        }

        title = bridge.conversation_title(task, binding)
        metadata = bridge.conversation_metadata(binding, "thr_new", "turn_new", "running", title)

        self.assertEqual("实现会话标题 V0.0.12", title)
        self.assertEqual(bridge.MAX_CONVERSATIONS_PER_TASK, len(metadata["conversations"]))
        self.assertEqual(13, metadata["nextConversationVersion"])
        self.assertEqual("实现会话标题 V0.0.13", bridge.conversation_title(task, {"metadata": metadata}))

    def test_validate_conversation_payload_accepts_selected_or_new_thread(self):
        result = bridge.validate_conversation_payload(
            {"bizLine": "whatsapp", "programId": 1, "itemKey": "a", "message": "Start fresh", "threadId": "thr_old", "newConversation": True}
        )

        self.assertEqual((1, "a", "Start fresh", "thr_old", True, [], "", "", False, []), result)

    def test_validate_conversation_payload_accepts_attachment_only_message(self):
        attachment_id = "abcdefghijklmnop"
        result = bridge.validate_conversation_payload(
            {"bizLine": "whatsapp", "programId": 1, "itemKey": "a", "message": "", "attachmentIds": [attachment_id]}
        )

        self.assertEqual((1, "a", "", "", False, [attachment_id], "", "", False, []), result)

    def test_conversation_references_only_accept_known_entity_kinds_and_keys(self):
        references = bridge.conversation_references_of([
            {"kind": "requirement", "key": "req-a"},
            {"kind": "task", "key": "task.v1"},
            {"kind": "requirement", "key": "req-a"},
            {"kind": "unknown", "key": "ignored"},
            {"kind": "requirement", "key": "bad key"},
        ])

        self.assertEqual(
            [{"kind": "requirement", "key": "req-a"}, {"kind": "task", "key": "task.v1"}],
            references,
        )

    def test_conversation_mention_context_loads_the_selected_requirement_and_task(self):
        executor = bridge.ExecutionBridge(Path.cwd())
        context = {
            "items": [
                {"itemKey": "task-a", "title": "关联任务", "requirementKey": "req-a", "phase": "development", "status": "doing"},
            ]
        }
        requirement = {"requirementKey": "req-a", "name": "关联需求", "detail": "需求详情"}
        task = {
            "itemKey": "task-a", "title": "关联任务", "description": "任务说明",
            "requirementKey": "req-a", "phase": "development", "status": "doing",
            "moduleKey": "web",
        }
        with (
            patch.object(bridge.planner, "requirement_record", return_value=requirement),
            patch.object(executor, "_task_detail", return_value=task),
        ):
            lines = executor._conversation_mention_context(
                {"api_url": "http://test/api", "key": "k"},
                1,
                [{"kind": "requirement", "key": "req-a"}, {"kind": "task", "key": "task-a"}],
                context,
            )

        rendered = "\n".join(lines)
        self.assertIn("@需求 req-a: 关联需求", rendered)
        self.assertIn("关联任务", rendered)
        self.assertIn("@任务 task-a: 关联任务", rendered)
        self.assertIn("所属需求 req-a: 关联需求", rendered)

    def test_conversation_marks_the_running_thread_even_when_reading_history(self):
        executor = bridge.ExecutionBridge(Path.cwd())
        executor.active_runs[("whatsapp", 1, "a")] = {"threadId": "thr_running", "turnId": "turn_1", "client": unittest.mock.MagicMock()}
        binding = {
            "externalSessionId": "thr_running",
            "status": "running",
            "metadata": {
                "turnId": "turn_1",
                "conversations": [
                    {"threadId": "thr_running", "title": "Working", "status": "running"},
                    {"threadId": "thr_history", "title": "History", "status": "completed"},
                ],
            },
        }
        reader = unittest.mock.MagicMock()
        reader.next_request_id.return_value = 101
        reader.read_thread.return_value = {"turns": []}

        with (
            patch.object(executor, "_task_detail", return_value={"itemKey": "a", "phase": "development", "status": "doing"}),
            patch.object(executor, "_session_binding", return_value=binding),
            patch.object(executor, "_task_session_bindings", return_value=[binding]),
            patch.object(bridge, "AppServerClient", return_value=reader),
        ):
            result = executor.conversation(1, "a", "thr_history", config=self.runtime_config())

        self.assertFalse(result["active"])
        self.assertTrue(result["taskHasActiveConversation"])
        running = next(entry for entry in result["conversations"] if entry["threadId"] == "thr_running")
        self.assertTrue(running["active"])

    def test_merged_conversation_catalog_keeps_threads_from_each_task_phase(self):
        requirement_binding = {
            "phase": "requirement",
            "metadata": {"conversations": [{"threadId": "thr_requirement", "title": "梳理", "updatedAt": "2026-08-01T00:00:00+00:00"}]},
        }
        development_binding = {
            "phase": "development",
            "metadata": {"conversations": [{"threadId": "thr_development", "title": "行动", "updatedAt": "2026-08-02T00:00:00+00:00"}]},
        }

        catalog, owners = bridge.merged_conversation_catalog([requirement_binding, development_binding])

        self.assertEqual(["thr_development", "thr_requirement"], [entry["threadId"] for entry in catalog])
        self.assertEqual("requirement", owners["thr_requirement"]["phase"])
        self.assertEqual("development", owners["thr_development"]["phase"])

    def test_request_config_uses_current_token_without_persisting_it(self):
        executor = bridge.ExecutionBridge(Path.cwd())
        with (
            patch.object(bridge.planner, "bridge_api_url", return_value="http://47.110.3.214:8691/api"),
            patch.object(bridge.planner, "request_api", return_value=[]),
            patch.object(bridge.planner, "project_context", return_value={"program": {"programId": 1, "bizLine": "whatsapp"}}),
        ):
            config = executor.request_config(
                {"programId": 1, "userId": "local-admin", "apiUrl": "https://untrusted.example.test"},
                "https://untrusted.example.test",
                "current-user-token",
            )
        self.assertEqual("http://47.110.3.214:8691/api", config["api_url"])
        self.assertEqual("current-user-token", config["key"])
        self.assertEqual(1, config["_project_id"])
        self.assertNotIn("_biz_line", config)

    def test_health_reports_ready_without_persisted_configuration(self):
        executor = bridge.ExecutionBridge(Path.cwd())
        with patch.object(bridge.shutil, "which", return_value="/usr/local/bin/codex"):
            health = executor.health()
        self.assertTrue(health["ready"])
        self.assertTrue(health["bridge"])
        self.assertTrue(health["codex"])
        self.assertTrue(health["claude"])
        self.assertTrue(health["configured"])
        self.assertTrue(health["apiReachable"])

    def test_health_reports_codex_and_claude_availability_independently(self):
        executor = bridge.ExecutionBridge(Path.cwd())
        with patch.object(
            bridge.shutil,
            "which",
            side_effect=lambda command: "/usr/local/bin/codex" if command == "codex" else None,
        ):
            health = executor.health()
        self.assertTrue(health["codex"])
        self.assertFalse(health["claude"])

    def test_health_requires_reachable_task_board(self):
        executor = bridge.ExecutionBridge(Path.cwd())
        with (
            patch.object(bridge.shutil, "which", return_value="/usr/local/bin/codex"),
            patch.object(bridge.planner, "request_api", return_value=[]),
        ):
            health = executor.health()
        self.assertTrue(health["ready"])
        self.assertTrue(health["apiReachable"])

    def test_payload_rejects_completed_task(self):
        with self.assertRaisesRegex(bridge.BridgeFailure, "已完成"):
            bridge.validate_execute_payload(
                {"bizLine": "whatsapp", "programId": 1, "task": {"itemKey": "a", "title": "A", "version": 1, "status": "done"}}
            )

    def test_payload_accepts_every_incomplete_task_status(self):
        for status in ("todo", "doing", "blocked", "dropped"):
            with self.subTest(status=status):
                result = bridge.validate_execute_payload(
                    {"bizLine": "whatsapp", "programId": 1, "task": {"itemKey": "a", "title": "A", "version": 1, "status": status}}
                )
                self.assertEqual(status, result["task"]["status"])
                self.assertNotIn("bizLine", result)

    def test_payload_allows_a_project_id_without_business_line(self):
        result = bridge.validate_execute_payload(
            {
                "programId": 1,
                "task": {"itemKey": "a", "title": "A", "version": 1, "status": "todo"},
            }
        )

        self.assertNotIn("bizLine", result)

    def test_execute_batch_accepts_selected_dependency_chain(self):
        context = {
            "items": [
                {"itemKey": "a", "status": "todo", "dependsOnItemKeys": []},
                {"itemKey": "b", "status": "todo", "dependsOnItemKeys": ["a"]},
            ],
        }
        executor = bridge.ExecutionBridge(Path.cwd())
        with (
            patch.object(bridge.planner, "project_context", return_value=context),
            patch.object(bridge.threading, "Thread") as thread,
        ):
            result = executor.execute_batch({"bizLine": "whatsapp", "programId": 1, "itemKeys": ["a", "b"]}, config=self.runtime_config())

        self.assertTrue(result["accepted"])
        self.assertEqual(["a", "b"], result["itemKeys"])
        thread.return_value.start.assert_called_once()

    def test_execute_batch_rejects_unfinished_external_prerequisites(self):
        context = {
            "items": [
                {"itemKey": "a", "status": "todo", "dependsOnItemKeys": []},
                {"itemKey": "b", "status": "todo", "dependsOnItemKeys": ["a"]},
            ],
        }
        executor = bridge.ExecutionBridge(Path.cwd())
        with (
            patch.object(bridge.planner, "project_context", return_value=context),
        ):
            with self.assertRaisesRegex(bridge.BridgeFailure, "外部前置任务"):
                executor.execute_batch({"bizLine": "whatsapp", "programId": 1, "itemKeys": ["b"]}, config=self.runtime_config())

    def test_run_batch_releases_successors_after_parallel_prerequisites_complete(self):
        statuses = {"a": "todo", "b": "todo", "c": "todo", "d": "todo"}
        dependencies = {"a": [], "b": [], "c": ["a", "b"], "d": ["c"]}
        started: list[str] = []
        executor = bridge.ExecutionBridge(Path.cwd())

        def project_context(*_args):
            return {
                "items": [
                    {"itemKey": key, "status": status, "dependsOnItemKeys": dependencies[key]}
                    for key, status in statuses.items()
                ],
            }

        def task_detail(_config, _program_id, item_key):
            return {"itemKey": item_key, "title": item_key, "version": 1, "status": statuses[item_key]}

        def execute(payload, batch_claim=False, config=None):
            item_key = payload["task"]["itemKey"]
            self.assertTrue(batch_claim)
            self.assertEqual("仅修改 API 模块", payload["executionConstraints"])
            self.assertEqual("high", payload["reasoningEffort"])
            self.assertTrue(all(statuses[dependency] == "done" for dependency in dependencies[item_key]))
            started.append(item_key)
            statuses[item_key] = "done"
            return {"accepted": True}

        with (
            patch.object(bridge.planner, "project_context", side_effect=project_context),
            patch.object(executor, "_task_detail", side_effect=task_detail),
            patch.object(executor, "execute", side_effect=execute),
        ):
            executor._run_batch(
                "batch-1",
                self.runtime_config(),
                1,
                ["a", "b", "c", "d"],
                "",
                execution_constraints="仅修改 API 模块",
                reasoning_effort="high",
            )

        self.assertEqual(["a", "b", "c", "d"], started)

    def test_batch_continues_after_an_unsubstantive_interruption(self):
        statuses = {"a": "blocked", "b": "todo"}
        dependencies = {"a": [], "b": ["a"]}
        started: list[str] = []
        executor = bridge.ExecutionBridge(Path.cwd())

        def project_context(*_args):
            return {
                "items": [
                    {"itemKey": key, "status": status, "dependsOnItemKeys": dependencies[key]}
                    for key, status in statuses.items()
                ],
            }

        def task_detail(_config, _program_id, item_key):
            output = "# Codex 执行结果\n\n- 状态：interrupted\n"
            return {
                "itemKey": item_key,
                "title": item_key,
                "version": 1,
                "status": statuses[item_key],
                "actionOutput": output if item_key == "a" else "# Codex 执行结果\n\n- 状态：completed\n",
            }

        def execute(payload, batch_claim=False, config=None):
            self.assertTrue(batch_claim)
            item_key = payload["task"]["itemKey"]
            started.append(item_key)
            if item_key == "b":
                statuses[item_key] = "done"
            return {"accepted": True}

        with (
            patch.object(bridge.planner, "project_context", side_effect=project_context),
            patch.object(executor, "_task_detail", side_effect=task_detail),
            patch.object(executor, "execute", side_effect=execute),
        ):
            executor._run_batch("batch-soft", self.runtime_config(), 1, ["a", "b"], "")

        self.assertEqual(["a", "b"], started)
        events = executor.progress.snapshot(("", 1, "a"))
        self.assertTrue(any("已忽略" in event["title"] for event in events))

    def test_batch_stops_and_reports_a_substantive_problem(self):
        statuses = {"a": "blocked", "b": "todo"}
        dependencies = {"a": [], "b": ["a"]}
        started: list[str] = []
        executor = bridge.ExecutionBridge(Path.cwd())

        def project_context(*_args):
            return {
                "items": [
                    {"itemKey": key, "status": status, "dependsOnItemKeys": dependencies[key]}
                    for key, status in statuses.items()
                ],
            }

        def task_detail(_config, _program_id, item_key):
            output = (
                "# Codex 执行结果\n\n- 状态：interrupted\n\n"
                "批量判定：需人工处理\n"
            )
            return {"itemKey": item_key, "title": item_key, "version": 1, "status": statuses[item_key], "actionOutput": output}

        def execute(payload, batch_claim=False, config=None):
            started.append(payload["task"]["itemKey"])
            return {"accepted": True}

        with (
            patch.object(bridge.planner, "project_context", side_effect=project_context),
            patch.object(executor, "_task_detail", side_effect=task_detail),
            patch.object(executor, "execute", side_effect=execute),
        ):
            executor._run_batch("batch-hard", self.runtime_config(), 1, ["a", "b"], "")

        self.assertEqual(["a"], started)
        events = executor.progress.snapshot(("", 1, "a"))
        self.assertTrue(any(event["kind"] == "error" for event in events))

    def test_execute_batch_accepts_ready_not_started_items(self):
        context = {
            "items": [
                {"itemKey": "a", "status": "done", "dependsOnItemKeys": []},
                {"itemKey": "b", "status": "todo", "dependsOnItemKeys": ["a"]},
                {"itemKey": "c", "status": "todo", "dependsOnItemKeys": []},
            ],
        }
        executor = bridge.ExecutionBridge(Path.cwd())
        with (
            patch.object(bridge.planner, "project_context", return_value=context),
            patch.object(bridge.threading, "Thread") as thread,
        ):
            result = executor.execute_batch({"bizLine": "whatsapp", "programId": 1, "itemKeys": ["b", "c"]}, config=self.runtime_config())

        self.assertTrue(result["accepted"])
        self.assertEqual(["b", "c"], result["itemKeys"])
        self.assertEqual({("", 1, "b"), ("", 1, "c")}, executor.batch_tasks)
        thread.return_value.start.assert_called_once()

    def test_execute_sequence_accepts_selected_not_started_dependency_chain(self):
        context = {
            "items": [
                {"itemKey": "a", "status": "todo", "dependsOnItemKeys": []},
                {"itemKey": "b", "status": "todo", "dependsOnItemKeys": ["a"]},
            ],
        }
        executor = bridge.ExecutionBridge(Path.cwd())
        with (
            patch.object(bridge.planner, "project_context", return_value=context),
            patch.object(bridge.threading, "Thread") as thread,
        ):
            result = executor.execute_sequence({"bizLine": "whatsapp", "programId": 1, "itemKeys": ["b", "a"]}, config=self.runtime_config())

        self.assertTrue(result["accepted"])
        self.assertEqual(["a", "b"], result["itemKeys"])
        thread.return_value.start.assert_called_once()

    def test_run_sequence_passes_constraints_to_every_task(self):
        statuses = {"a": "todo", "b": "todo"}
        received: list[tuple[str, str, str]] = []
        executor = bridge.ExecutionBridge(Path.cwd())

        def task_detail(_config, _program_id, item_key):
            return {"itemKey": item_key, "title": item_key, "version": 1, "status": statuses[item_key]}

        def execute(payload, config=None):
            item_key = payload["task"]["itemKey"]
            received.append((item_key, payload["executionConstraints"], payload["reasoningEffort"]))
            statuses[item_key] = "done"
            return {"accepted": True}

        with (
            patch.object(executor, "_task_detail", side_effect=task_detail),
            patch.object(executor, "execute", side_effect=execute),
        ):
            executor._run_sequence(
                "sequence-1",
                self.runtime_config(),
                1,
                ["a", "b"],
                "",
                "codex",
                "先兼容现有接口",
                "xhigh",
            )

        self.assertEqual(
            [("a", "先兼容现有接口", "xhigh"), ("b", "先兼容现有接口", "xhigh")],
            received,
        )

    def test_execute_sequence_accepts_selected_blocked_task(self):
        context = {"items": [{"itemKey": "a", "status": "blocked", "dependsOnItemKeys": []}]}
        executor = bridge.ExecutionBridge(Path.cwd())
        with (
            patch.object(bridge.planner, "project_context", return_value=context),
            patch.object(bridge.threading, "Thread") as thread,
        ):
            result = executor.execute_sequence({"bizLine": "whatsapp", "programId": 1, "itemKeys": ["a"]}, config=self.runtime_config())

        self.assertTrue(result["accepted"])
        self.assertEqual(["a"], result["itemKeys"])
        thread.return_value.start.assert_called_once()

    def test_execute_batch_accepts_selected_incomplete_task_statuses(self):
        context = {
            "items": [
                {"itemKey": "doing", "status": "doing", "dependsOnItemKeys": []},
                {"itemKey": "blocked", "status": "blocked", "dependsOnItemKeys": []},
                {"itemKey": "dropped", "status": "dropped", "dependsOnItemKeys": []},
            ],
        }
        executor = bridge.ExecutionBridge(Path.cwd())
        with (
            patch.object(bridge.planner, "project_context", return_value=context),
            patch.object(bridge.threading, "Thread") as thread,
        ):
            result = executor.execute_batch(
                {"bizLine": "whatsapp", "programId": 1, "itemKeys": ["doing", "blocked", "dropped"]},
                config=self.runtime_config(),
            )

        self.assertTrue(result["accepted"])
        self.assertEqual(["doing", "blocked", "dropped"], result["itemKeys"])
        thread.return_value.start.assert_called_once()

    def test_execute_sequence_rejects_tasks_reserved_for_batch_start(self):
        context = {"items": [{"itemKey": "a", "status": "todo", "dependsOnItemKeys": []}]}
        executor = bridge.ExecutionBridge(Path.cwd())
        executor.batch_tasks.add(("", 1, "a"))
        with (
            patch.object(bridge.planner, "project_context", return_value=context),
        ):
            with self.assertRaisesRegex(bridge.BridgeFailure, "批量启动"):
                executor.execute_sequence({"bizLine": "whatsapp", "programId": 1, "itemKeys": ["a"]}, config=self.runtime_config())

    def test_execute_batch_rejects_tasks_reserved_for_another_batch(self):
        context = {"items": [{"itemKey": "a", "status": "todo", "dependsOnItemKeys": []}]}
        executor = bridge.ExecutionBridge(Path.cwd())
        executor.batch_tasks.add(("", 1, "a"))
        with (
            patch.object(bridge.planner, "project_context", return_value=context),
        ):
            with self.assertRaisesRegex(bridge.BridgeFailure, "批量启动"):
                executor.execute_batch({"bizLine": "whatsapp", "programId": 1, "itemKeys": ["a"]}, config=self.runtime_config())

    def test_prompt_contains_task_context(self):
        prompt = bridge.build_task_prompt(
            {
                "programId": 1,
                "task": {
                    "itemKey": "a",
                    "title": "Build API",
                    "description": "Implement it",
                    "stageKey": "s1",
                    "moduleKey": "api",
                    "dependsOnItemKeys": ["base"],
                },
            }
        )
        self.assertIn("Build API", prompt)
        self.assertIn("base", prompt)
        self.assertIn("已由 HTTP 执行桥领取", prompt)
        self.assertIn("不要调用 claim_next_task", prompt)

    def test_prompt_includes_non_empty_group_execution_constraints(self):
        prompt = bridge.build_task_prompt(
            {
                "programId": 1,
                "task": {"itemKey": "a", "title": "Build API", "version": 1, "status": "todo"},
                "executionConstraints": "  仅修改 API 模块；每个任务完成后运行测试。  ",
            }
        )

        self.assertIn("本次队列的前置任务约束条件说明", prompt)
        self.assertIn("仅修改 API 模块；每个任务完成后运行测试。", prompt)

    def test_prompt_omits_empty_group_execution_constraints(self):
        prompt = bridge.build_task_prompt(
            {
                "programId": 1,
                "task": {"itemKey": "a", "title": "Build API", "version": 1, "status": "todo"},
                "executionConstraints": "   ",
            }
        )

        self.assertNotIn("前置任务约束条件说明", prompt)

    def test_prompt_uses_fixed_requirement_document_path(self):
        prompt = bridge.build_task_prompt(
            {
                "programId": 1,
                "task": {
                    "itemKey": "a", "title": "Build API", "version": 1, "status": "todo",
                    "requirementDocument": "# API\n\n## 验收\n- 通过测试",
                },
            }
        )
        self.assertIn("doc/module/a/文档.md", prompt)
        self.assertNotIn("## 验收", prompt)

    def test_task_prompt_forbids_writing_only_the_appended_requirement(self):
        prompt = bridge.build_task_prompt(
            {"programId": 1, "task": {"itemKey": "a", "title": "Build API", "moduleKey": "svc"}}
        )

        self.assertIn("doc/svc/a/文档.md` 是跨回合累积的文档", prompt)
        self.assertIn("整篇写回同一路径", prompt)

    def test_conversation_prompt_carries_the_document_revision_rule(self):
        prompt = bridge.build_conversation_prompt(1, {"itemKey": "a", "moduleKey": "svc", "title": "Build API"}, "再加一条需求")

        self.assertIn("跨回合累积的文档", prompt)
        self.assertIn("禁止只把本轮追加的需求写进文件", prompt)

    def test_follow_up_context_repeats_the_phase_document_and_merge_rule(self):
        lines = bridge.follow_up_context_lines({"itemKey": "a", "moduleKey": "svc", "phase": "development"})
        context = "\n".join(lines)

        self.assertIn("doc/svc/a/文档.md", context)
        self.assertIn(bridge.PHASE_SKILLS["development"], context)
        self.assertIn("整篇写回同一路径", context)

    def test_follow_up_context_omits_the_document_path_when_the_board_did_not_give_one(self):
        context = "\n".join(bridge.follow_up_context_lines({"itemKey": "a"}))

        self.assertIn("追加回合", context)
        self.assertNotIn("doc/module/a/文档.md", context)

    def test_planning_prompt_requires_merging_the_existing_outline_before_writing_it_back(self):
        prompt = bridge.build_planning_prompt(
            1, {"program": {"name": "Universe"}}, "再追加一条需求", requirement={"requirementKey": "req-a"},
        )

        self.assertIn("读全文 → 合并本轮增量 → 整篇覆盖", prompt)
        self.assertIn("禁止只把本轮追加的那段需求写进文件", prompt)

    def test_testing_prompt_uses_task_scoped_test_artifact_directory(self):
        prompt = bridge.build_task_prompt(
            {
                "programId": 1,
                "task": {"itemKey": "api-smoke-123", "title": "API smoke test", "phase": "testing"},
            }
        )

        self.assertIn("doc/test/api-smoke-123/", prompt)

    def test_requirement_testing_prompt_lists_linked_tasks_and_requirement_scoped_artifacts(self):
        prompt = bridge.build_requirement_testing_prompt(
            1,
            {
                "items": [
                    {"itemKey": "api-1", "title": "Create API", "requirementKey": "req-a", "phase": "testing", "status": "doing", "testingReport": "task report"},
                    {"itemKey": "other", "title": "Unrelated", "requirementKey": "req-b"},
                ],
            },
            {"requirementKey": "req-a", "name": "Requirement A", "detail": "Verify the complete flow"},
            "Use the staging account.",
            Path("/tmp/workspace"),
        )

        self.assertIn("需求键 requirement_key: req-a", prompt)
        self.assertIn("doc/test/req-a/", prompt)
        self.assertIn("api-1: Create API", prompt)
        self.assertNotIn("other: Unrelated", prompt)
        self.assertIn("Use the staging account.", prompt)

    def test_requirement_testing_cases_prompt_forbids_real_execution(self):
        prompt = bridge.build_requirement_testing_prompt(
            1,
            {"items": [{"itemKey": "api-1", "title": "Create API", "requirementKey": "req-a", "testingCasesStatus": "ready"}]},
            {"requirementKey": "req-a", "name": "Requirement A", "detail": "Verify the complete flow"},
            "Prepare cases while development is running.",
            Path("/tmp/workspace"),
            test_case_only=True,
        )

        self.assertIn("测试用例.md", prompt)
        self.assertIn("绝不调用接口、UI、脚本或构建命令执行真实测试", prompt)
        self.assertNotIn("验收判定：通过 / 不通过 / 受阻", prompt)

    def test_task_testing_cases_prompt_is_design_only(self):
        prompt = bridge.build_task_testing_cases_prompt(
            1,
            {"itemKey": "api-1", "title": "Create API", "phase": "development", "status": "doing"},
            {"items": []},
            "Use a staging account.",
            Path("/tmp/workspace"),
        )

        self.assertIn("测试用例.md", prompt)
        self.assertIn("不得输出验收判定", prompt)
        self.assertIn("Use a staging account.", prompt)

    def test_requirement_testing_starts_session_and_marks_requirement_doing(self):
        with tempfile.TemporaryDirectory() as directory:
            executor = bridge.ExecutionBridge(Path(directory))
            client = unittest.mock.MagicMock()
            client.start_task.return_value = ("testing-thread", "testing-turn")
            requests = []

            def request_api(_config, method, path, query=None, body=None):
                requests.append((method, path, query, body))
                if path == "/delivery/requirement":
                    return {"requirementKey": "req-a", "name": "Requirement A", "detail": "Complete checkout"}
                if path == "/delivery/requirement/testing-sessions":
                    return []
                if path in {"/delivery/requirement/testing-session/bind", "/delivery/requirement/testing/save"}:
                    return None
                self.fail(f"unexpected request: {path}")

            with (
                patch.object(bridge.planner, "request_api", side_effect=request_api),
                patch.object(bridge.planner, "project_context", return_value={"items": []}),
                patch.object(bridge, "create_ai_client", return_value=client),
                patch.object(bridge.threading, "Thread") as thread,
            ):
                result = executor.send_requirement_testing(
                    {"programId": 1, "requirementKey": "req-a", "message": "Test the staging flow", "newConversation": True},
                    self.runtime_config(),
                )

        self.assertTrue(result["accepted"])
        self.assertEqual("testing-thread", result["threadId"])
        self.assertEqual("testing-turn", result["turnId"])
        client.start_task.assert_called_once()
        self.assertIn("doc/test/req-a/", client.start_task.call_args.args[1])
        bind = next(request for request in requests if request[1] == "/delivery/requirement/testing-session/bind")
        self.assertEqual("codex", bind[3]["executorType"])
        update = next(request for request in requests if request[1] == "/delivery/requirement/testing/save")
        self.assertEqual("doing", update[3]["testingStatus"])
        thread.return_value.start.assert_called_once()

    def test_requirement_testing_cases_marks_only_cases_status(self):
        with tempfile.TemporaryDirectory() as directory:
            executor = bridge.ExecutionBridge(Path(directory))
            client = unittest.mock.MagicMock()
            client.start_task.return_value = ("testing-thread", "testing-turn")
            requests = []

            def request_api(_config, method, path, query=None, body=None):
                requests.append((method, path, query, body))
                if path == "/delivery/requirement":
                    return {"requirementKey": "req-a", "name": "Requirement A", "detail": "Complete checkout"}
                if path == "/delivery/requirement/testing-sessions":
                    return []
                if path in {"/delivery/requirement/testing-session/bind", "/delivery/requirement/testing/save"}:
                    return None
                self.fail(f"unexpected request: {path}")

            with (
                patch.object(bridge.planner, "request_api", side_effect=request_api),
                patch.object(bridge.planner, "project_context", return_value={"items": []}),
                patch.object(bridge, "create_ai_client", return_value=client),
                patch.object(bridge.threading, "Thread") as thread,
            ):
                executor.send_requirement_testing(
                    {"programId": 1, "requirementKey": "req-a", "message": "Prepare test cases", "newConversation": True, "testCaseOnly": True},
                    self.runtime_config(),
                )

        self.assertEqual("Requirement A · 测试用例", client.start_task.call_args.args[0])
        update = next(request for request in requests if request[1] == "/delivery/requirement/testing/save")
        self.assertEqual("doing", update[3]["testingCasesStatus"])
        self.assertNotIn("testingStatus", update[3])
        thread.return_value.start.assert_called_once()

    def test_task_testing_cases_does_not_claim_or_change_task_status(self):
        with tempfile.TemporaryDirectory() as directory:
            executor = bridge.ExecutionBridge(Path(directory))
            client = unittest.mock.MagicMock()
            client.start_task.return_value = ("cases-thread", "cases-turn")
            requests = []
            task = {"itemKey": "api-1", "title": "Create API", "version": 3, "phase": "development", "status": "doing"}

            def request_api(_config, method, path, query=None, body=None):
                requests.append((method, path, query, body))
                if path == "/delivery/item" and method == "GET":
                    return task
                if path == "/delivery/item/execution-session" and method == "GET":
                    return []
                if path == "/delivery/item/execution-session/bind" and method == "POST":
                    return {
                        "programId": 1, "itemKey": "api-1", "executorType": "codex-testing-cases",
                        "phase": "development", "externalSessionId": "cases-thread", "status": "running",
                        "metadata": body.get("metadata") or {}, "version": 1,
                    }
                if path == "/delivery/item/testing-cases/save":
                    return None
                self.fail(f"unexpected request: {method} {path}")

            with (
                patch.object(bridge.planner, "project_context", return_value={"items": [task]}),
                patch.object(bridge.planner, "request_api", side_effect=request_api),
                patch.object(bridge, "create_ai_client", return_value=client),
                patch.object(bridge.threading, "Thread") as thread,
            ):
                result = executor.generate_task_testing_cases(
                    {"programId": 1, "itemKey": "api-1"}, self.runtime_config(),
                )

        self.assertTrue(result["accepted"])
        self.assertEqual("cases-thread", result["threadId"])
        self.assertEqual("Create API · 测试用例", client.start_task.call_args.args[0])
        self.assertFalse(any(path == "/delivery/item/patch" for _, path, _, _ in requests))
        bind = next(request for request in requests if request[1] == "/delivery/item/execution-session/bind")
        self.assertEqual("codex-testing-cases", bind[3]["executorType"])
        update = next(request for request in requests if request[1] == "/delivery/item/testing-cases/save")
        self.assertEqual("doing", update[3]["testingCasesStatus"])
        thread.return_value.start.assert_called_once()

    def test_task_testing_cases_conversation_reads_its_dedicated_history(self):
        executor = bridge.ExecutionBridge(Path.cwd())
        client = unittest.mock.MagicMock()
        client.read_thread.return_value = {"turns": []}
        task = {"itemKey": "api-1", "title": "Create API", "phase": "development", "testingCasesStatus": "ready"}
        binding = {
            "executorType": "codex-testing-cases", "phase": "development", "externalSessionId": "cases-thread",
            "status": "completed", "metadata": {
                "conversations": [{"threadId": "cases-thread", "title": "Create API · 测试用例", "status": "completed"}],
            },
        }
        requests = []

        def request_api(_config, method, path, query=None, body=None):
            requests.append((method, path, query, body))
            if path == "/delivery/item" and method == "GET":
                return task
            if path == "/delivery/item/execution-session" and method == "GET":
                return [binding]
            self.fail(f"unexpected request: {method} {path}")

        with (
            patch.object(bridge.planner, "request_api", side_effect=request_api),
            patch.object(bridge, "create_ai_client", return_value=client),
        ):
            result = executor.task_testing_cases_conversation(1, "api-1", config=self.runtime_config())

        self.assertEqual("cases-thread", result["threadId"])
        self.assertEqual("Create API · 测试用例", result["conversations"][0]["title"])
        self.assertEqual("ready", result["testingCasesStatus"])
        query = next(request for request in requests if request[1] == "/delivery/item/execution-session")[2]
        self.assertEqual("codex-testing-cases", query["executorType"])

    def test_task_testing_cases_continues_selected_chat_without_claiming_task(self):
        executor = bridge.ExecutionBridge(Path.cwd())
        client = unittest.mock.MagicMock()
        client.start_turn.return_value = "cases-turn-2"
        task = {"itemKey": "api-1", "title": "Create API", "phase": "development", "status": "doing"}
        binding = {
            "executorType": "codex-testing-cases", "phase": "development", "externalSessionId": "cases-thread",
            "status": "completed", "version": 4,
            "metadata": {
                "conversations": [{"threadId": "cases-thread", "title": "Create API · 测试用例", "status": "completed"}],
                "nextConversationVersion": 1,
            },
        }
        requests = []

        def request_api(_config, method, path, query=None, body=None):
            requests.append((method, path, query, body))
            if path == "/delivery/item" and method == "GET":
                return task
            if path == "/delivery/item/execution-session" and method == "GET":
                return [binding]
            if path == "/delivery/item/execution-session/bind" and method == "POST":
                return {**binding, "status": "running", "version": 5, "metadata": body.get("metadata") or {}}
            if path == "/delivery/item/testing-cases/save" and method == "POST":
                return None
            self.fail(f"unexpected request: {method} {path}")

        with (
            patch.object(bridge.planner, "project_context", return_value={"items": [task]}),
            patch.object(bridge.planner, "request_api", side_effect=request_api),
            patch.object(bridge, "create_ai_client", return_value=client),
            patch.object(bridge.threading, "Thread") as thread,
        ):
            result = executor.generate_task_testing_cases(
                {"programId": 1, "itemKey": "api-1", "threadId": "cases-thread", "message": "补充异常分支"},
                self.runtime_config(),
            )

        self.assertEqual("cases-thread", result["threadId"])
        client.resume_thread.assert_called_once_with("cases-thread")
        self.assertIn("补充异常分支", client.start_turn.call_args.args[1])
        self.assertFalse(any(path == "/delivery/item/patch" for _, path, _, _ in requests))
        thread.return_value.start.assert_called_once()

    def test_task_testing_cases_keeps_existing_thread_binding_when_task_phase_changes(self):
        executor = bridge.ExecutionBridge(Path.cwd())
        binding = {
            "phase": "development", "externalSessionId": "cases-thread", "version": 6,
            "metadata": {"conversations": [{"threadId": "cases-thread", "title": "Create API · 测试用例"}]},
        }
        calls = []

        def request_with_retry(_config, path, body):
            calls.append((path, body))
            return {**binding, "metadata": body["metadata"], "status": "running", "version": 7}

        with patch.object(executor, "_request_with_retry", side_effect=request_with_retry):
            refreshed = executor._bind_task_testing_cases_session(
                self.runtime_config(), 1, "api-1", {"itemKey": "api-1", "phase": "testing"},
                "codex", binding, "cases-thread", "cases-turn-2",
            )

        self.assertEqual(7, refreshed["version"])
        self.assertEqual("/delivery/item/execution-session/status", calls[0][0])
        self.assertEqual("development", calls[0][1]["phase"])
        self.assertEqual("codex-testing-cases", calls[0][1]["executorType"])

    def test_requirement_testing_completion_maps_verdict_to_requirement_status(self):
        cases = (("通过", "completed", "passed"), ("不通过", "completed", "failed"), ("受阻", "completed", "blocked"), ("通过", "interrupted", "blocked"))
        for verdict, turn_status, expected_status in cases:
            with self.subTest(verdict=verdict, turn_status=turn_status), tempfile.TemporaryDirectory() as directory:
                executor = bridge.ExecutionBridge(Path(directory))
                client = unittest.mock.MagicMock()
                client.wait_turn.return_value = turn_status
                client.read_turn.return_value = {"items": [{"type": "agentMessage", "text": f"验收判定：{verdict}\n\n测试结论"}]}
                requests = []

                def request_api(_config, method, path, query=None, body=None):
                    requests.append((method, path, query, body))
                    return None

                with patch.object(bridge.planner, "request_api", side_effect=request_api):
                    executor._follow_requirement_testing(
                        executor._requirement_testing_identity(1, "req-a"), client, self.runtime_config(), 1, "req-a", "codex",
                        {"turnId": "testing-turn", "catalog": [{"threadId": "testing-thread", "status": "running"}]}, "testing-thread", "testing-turn",
                    )

                report_path = Path(directory) / "doc/test/req-a/测试报告.md"
                self.assertTrue(report_path.is_file())
                self.assertIn(f"验收判定：{verdict}", report_path.read_text(encoding="utf-8"))
                update = next(request for request in requests if request[1] == "/delivery/requirement/testing/save")
                self.assertEqual(expected_status, update[3]["testingStatus"])

    def test_requirement_testing_cases_completion_writes_cases_without_report(self):
        with tempfile.TemporaryDirectory() as directory:
            executor = bridge.ExecutionBridge(Path(directory))
            client = unittest.mock.MagicMock()
            client.wait_turn.return_value = "completed"
            client.read_turn.return_value = {"items": [{"type": "agentMessage", "text": "测试用例已生成\n\n| 用例 | 预期 |\n| --- | --- |\n| API | 200 |"}]}
            requests = []

            def request_api(_config, method, path, query=None, body=None):
                requests.append((method, path, query, body))
                return None

            with patch.object(bridge.planner, "request_api", side_effect=request_api):
                executor._follow_requirement_testing(
                    executor._requirement_testing_identity(1, "req-a"), client, self.runtime_config(), 1, "req-a", "codex",
                    {"turnId": "testing-turn", "catalog": [{"threadId": "testing-thread", "status": "running"}], "threadId": "testing-thread"},
                    "testing-thread", "testing-turn", True,
                )

            self.assertTrue((Path(directory) / "doc/test/req-a/测试用例.md").is_file())
            self.assertFalse((Path(directory) / "doc/test/req-a/测试报告.md").exists())
            update = next(request for request in requests if request[1] == "/delivery/requirement/testing/save")
            self.assertEqual("ready", update[3]["testingCasesStatus"])
            self.assertNotIn("testingStatus", update[3])

    def test_task_testing_cases_completion_writes_cases_without_task_patch(self):
        with tempfile.TemporaryDirectory() as directory:
            executor = bridge.ExecutionBridge(Path(directory))
            client = unittest.mock.MagicMock()
            client.wait_turn.return_value = "completed"
            client.read_turn.return_value = {"items": [{"type": "agentMessage", "text": "测试用例已生成\n\n- API 正常路径"}]}
            requests = []

            def request_api(_config, method, path, query=None, body=None):
                requests.append((method, path, query, body))
                return None

            with patch.object(bridge.planner, "request_api", side_effect=request_api):
                executor._follow_task_testing_cases(
                    executor._task_testing_cases_identity(1, "api-1"), client, self.runtime_config(), 1, "api-1", "codex", "cases-thread", "cases-turn",
                )

            self.assertTrue((Path(directory) / "doc/test/api-1/测试用例.md").is_file())
            self.assertFalse(any(path == "/delivery/item/patch" for _, path, _, _ in requests))
            update = next(request for request in requests if request[1] == "/delivery/item/testing-cases/save")
            self.assertEqual("ready", update[3]["testingCasesStatus"])

    def test_execute_marks_task_doing_before_starting_and_binding_thread(self):
        context = {
            "program": {"programId": 1},
            "stages": [],
            "modules": [],
            "items": [
                {
                    "itemKey": "a", "title": "A", "version": 2, "phase": "development", "status": "todo",
                    "progress": 0, "dependsOnItemKeys": [],
                }
            ],
        }
        fake_client = unittest.mock.MagicMock()
        fake_client.start_task.return_value = ("thr_1", "turn_1")
        requests = []

        def request_api(_config, method, path, **kwargs):
            requests.append((method, path, kwargs.get("body")))
            if path.endswith("/bind"):
                return {"version": 1}
            return {"version": 3, "phase": "development", "status": "doing"}

        executor = bridge.ExecutionBridge(Path.cwd())
        with (
            patch.object(bridge.planner, "project_context", return_value=context),
            patch.object(bridge.planner, "request_api", side_effect=request_api),
            patch.object(bridge, "AppServerClient", return_value=fake_client),
            patch.object(bridge.threading, "Thread") as thread,
            patch.object(executor, "_migrate_legacy_task_outline") as migrate_legacy_outline,
        ):
            result = executor.execute(
                {"bizLine": "whatsapp", "programId": 1, "task": {"itemKey": "a", "title": "A", "version": 2, "phase": "development", "status": "todo"}},
                config=self.runtime_config(),
            )

        self.assertEqual("thr_1", result["threadId"])
        self.assertNotIn("threadUrl", result)
        self.assertTrue(requests[0][1].endswith("/delivery/item"))
        patch_index = next(index for index, request in enumerate(requests) if request[1].endswith("/delivery/item/patch"))
        bind_index = next(index for index, request in enumerate(requests) if request[1].endswith("/bind"))
        self.assertLess(patch_index, bind_index)
        self.assertEqual("doing", requests[patch_index][2]["status"])
        self.assertEqual("development", requests[bind_index][2]["phase"])
        self.assertEqual(0, requests[bind_index][2]["progress"])
        migrate_legacy_outline.assert_called_once()
        thread.return_value.start.assert_called_once()

    def test_completed_turn_marks_session_and_task_completed(self):
        requests = []
        executor = bridge.ExecutionBridge(Path.cwd())
        executor.pending_session_syncs = unittest.mock.MagicMock()
        with (
            patch.object(executor, "_task_detail", return_value={"version": 4, "phase": "development", "status": "doing"}),
            patch.object(bridge.planner, "request_api", side_effect=lambda _config, method, path, **kwargs: requests.append((method, path, kwargs["body"]))),
        ):
            executor._sync_result(
                {"api_url": "http://test/api", "key": "x"},
                1,
                "a",
                {"version": 3, "phase": "development", "progress": 25, "status": "doing"},
                {"version": 2},
                "turn-1",
                "completed",
            )

        self.assertEqual("done", requests[0][2]["status"])
        self.assertEqual(4, requests[0][2]["version"])
        self.assertEqual("completed", requests[1][2]["status"])

    def test_result_sync_writes_execution_output_to_task(self):
        requests = []
        executor = bridge.ExecutionBridge(Path.cwd())
        executor.pending_session_syncs = unittest.mock.MagicMock()
        with (
            patch.object(executor, "_task_detail", return_value={"version": 4, "phase": "development", "status": "doing"}),
            patch.object(bridge.planner, "request_api", side_effect=lambda _config, method, path, **kwargs: requests.append((method, path, kwargs["body"]))),
        ):
            executor._sync_result(
                {"api_url": "http://test/api", "key": "x"}, 1, "a",
                {"version": 3, "phase": "development", "progress": 25, "status": "doing"}, {"version": 2}, "turn-1", "completed", "full output",
        )
        self.assertEqual("full output\n", requests[0][2]["actionOutput"])

    def test_result_sync_appends_a_follow_up_round_to_the_existing_action_output(self):
        requests = []
        executor = bridge.ExecutionBridge(Path.cwd())
        executor.pending_session_syncs = unittest.mock.MagicMock()
        with (
            patch.object(
                executor,
                "_task_detail",
                return_value={"version": 4, "phase": "development", "status": "doing", "actionOutput": "第一轮产物"},
            ),
            patch.object(bridge.planner, "request_api", side_effect=lambda _config, method, path, **kwargs: requests.append((method, path, kwargs["body"]))),
        ):
            executor._sync_result(
                {"api_url": "http://test/api", "key": "x"}, 1, "a",
                {"version": 3, "phase": "development", "progress": 25, "status": "doing"}, {"version": 2}, "turn-1", "completed", "追加轮产物",
            )

        action_output = requests[0][2]["actionOutput"]
        self.assertIn("第一轮产物", action_output)
        self.assertIn("追加轮产物", action_output)

    def test_merged_execution_output_keeps_earlier_rounds_and_skips_repeats(self):
        self.assertEqual("只有本轮\n", bridge.merged_execution_output("", "只有本轮"))
        self.assertEqual("已有产物\n", bridge.merged_execution_output("已有产物", ""))
        self.assertEqual("已有产物\n", bridge.merged_execution_output("已有产物", "已有产物"))
        merged = bridge.merged_execution_output("第一轮", "第二轮")
        self.assertEqual("第一轮\n\n---\n\n第二轮\n", merged)

    def test_batch_task_outcome_distinguishes_interruption_from_real_blocker(self):
        self.assertEqual(
            "ignorable",
            bridge.batch_task_outcome(
                {"status": "blocked", "actionOutput": "# Codex 执行结果\n\n- 状态：interrupted\n"}
            )[0],
        )
        self.assertEqual(
            "hard",
            bridge.batch_task_outcome(
                {
                    "status": "blocked",
                    "actionOutput": "# Codex 执行结果\n\n- 状态：interrupted\n\n编译失败，需人工处理。",
                } 
            )[0],
        )
        self.assertEqual(
            "ignorable",
            bridge.batch_task_outcome(
                {"status": "blocked", "actionOutput": "# Codex 执行结果\n\n- 状态：failed\n"}
            )[0],
        )

    def test_merged_execution_output_drops_the_oldest_rounds_at_the_size_limit(self):
        merged = bridge.merged_execution_output("旧" * bridge.EXECUTION_OUTPUT_LIMIT, "最新一轮产物")

        self.assertLessEqual(len(merged.encode("utf-8")), bridge.EXECUTION_OUTPUT_LIMIT)
        self.assertTrue(merged.startswith("[更早的执行记录已按 8MB 上限截断]"))
        self.assertIn("最新一轮产物", merged)

    def test_completed_testing_turn_requires_explicit_passing_verdict(self):
        requests = []
        executor = bridge.ExecutionBridge(Path.cwd())
        executor.pending_session_syncs = unittest.mock.MagicMock()
        with (
            patch.object(executor, "_task_detail", return_value={"version": 4, "phase": "testing", "status": "doing", "progress": 68}),
            patch.object(bridge.planner, "request_api", side_effect=lambda _config, method, path, **kwargs: requests.append((method, path, kwargs["body"]))),
        ):
            executor._sync_result(
                {"api_url": "http://test/api", "key": "x"}, 1, "a",
                {"version": 3, "phase": "testing", "progress": 68, "status": "doing"}, {"version": 2}, "turn-1", "completed",
                "# Codex 执行结果\n\n## 进度说明\n\n验收判定：不通过\n\n接口返回 500。\n",
            )

        self.assertEqual("blocked", requests[0][2]["status"])
        self.assertEqual(68, requests[0][2]["progress"])
        self.assertEqual("completed", requests[1][2]["status"])

    def test_completed_testing_turn_with_passing_verdict_marks_task_done(self):
        requests = []
        executor = bridge.ExecutionBridge(Path.cwd())
        executor.pending_session_syncs = unittest.mock.MagicMock()
        with (
            patch.object(executor, "_task_detail", return_value={"version": 4, "phase": "testing", "status": "doing", "progress": 68}),
            patch.object(bridge.planner, "request_api", side_effect=lambda _config, method, path, **kwargs: requests.append((method, path, kwargs["body"]))),
        ):
            executor._sync_result(
                {"api_url": "http://test/api", "key": "x"}, 1, "a",
                {"version": 3, "phase": "testing", "progress": 68, "status": "doing"}, {"version": 2}, "turn-1", "completed",
                "# Codex 执行结果\n\n## 进度说明\n\n验收判定：通过\n\n所有验收项通过。\n",
            )

        self.assertEqual("done", requests[0][2]["status"])
        self.assertEqual(100, requests[0][2]["progress"])

    def test_testing_verdict_must_be_an_exact_standalone_line(self):
        self.assertEqual("受阻", bridge.testing_verdict_from_output("验收判定：受阻"))
        self.assertEqual("", bridge.testing_verdict_from_output("验收判定：通过（待复测）"))

    def test_session_close_failure_does_not_revert_completed_task(self):
        executor = bridge.ExecutionBridge(Path.cwd())
        executor.pending_session_syncs = unittest.mock.MagicMock()
        requests = []

        def request_api(_config, _method, path, **kwargs):
            requests.append((path, kwargs["body"]))
            if path.endswith("/execution-session/status"):
                raise bridge.planner.ToolFailure("temporary")
            return {"ok": True}

        with (
            patch.object(executor, "_task_detail", return_value={"version": 4, "phase": "development", "status": "doing"}),
            patch.object(bridge.planner, "request_api", side_effect=request_api),
            patch.object(bridge.time, "sleep"),
        ):
            executor._sync_result(
                {"api_url": "http://test/api", "key": "x"},
                1, "a", {"version": 3, "phase": "development", "status": "doing"},
                {"version": 2}, "turn-1", "completed", "done output",
            )

        self.assertEqual("done", requests[0][1]["status"])
        executor.pending_session_syncs.add.assert_called_once()
        executor.pending_session_syncs.remove.assert_not_called()

    def test_execution_output_is_markdown_instead_of_protocol_json(self):
        output = bridge.execution_output(
            "completed",
            {
                "items": [
                    {"type": "agentMessage", "text": "实现完成并通过测试。"},
                    {"type": "commandExecution", "command": "go test ./..."},
                ]
            },
        )

        self.assertIn("# Codex 执行结果", output)
        self.assertIn("实现完成并通过测试。", output)
        self.assertIn("```sh\ngo test ./...", output)
        self.assertNotIn('"items"', output)

    def test_terminal_conversation_uses_persisted_task_result_when_codex_snapshot_is_stale(self):
        turns = [
            {
                "id": "turn-1",
                "status": "completed",
                "items": [{"type": "agentMessage", "phase": "commentary", "text": "still working"}],
            }
        ]

        result = bridge.ensure_terminal_result(
            turns,
            {"status": "done", "phase": "requirement", "requirementDocument": "最终需求结果"},
            {"metadata": {"turnId": "turn-1"}},
        )

        self.assertEqual("final_answer", result[-1]["items"][-1]["phase"])
        self.assertEqual("最终需求结果", result[-1]["items"][-1]["text"])

    def test_terminal_conversation_does_not_duplicate_existing_final_answer(self):
        turns = [{"id": "turn-1", "status": "completed", "items": [{"type": "agentMessage", "phase": "final_answer", "text": "Codex final"}]}]

        result = bridge.ensure_terminal_result(
            turns,
            {"status": "done", "phase": "requirement", "requirementDocument": "task result"},
            None,
        )

        self.assertEqual(1, len(result[-1]["items"]))
        self.assertEqual("Codex final", result[-1]["items"][0]["text"])

    def test_requirement_result_is_written_to_the_fixed_workspace_document(self):
        with tempfile.TemporaryDirectory() as directory:
            executor = bridge.ExecutionBridge(Path(directory))
            executor.pending_session_syncs = unittest.mock.MagicMock()
            task = {
                "itemKey": "a", "version": 4, "phase": "requirement", "status": "doing",
                "requirementDocumentPath": "doc/api/a/文档.md",
            }
            requests = []
            output = bridge.execution_output("completed", {"items": [{"type": "agentMessage", "text": "# API 需求\n\n- 支持幂等"}]})
            with (
                patch.object(executor, "_task_detail", return_value=task),
                patch.object(bridge.planner, "request_api", side_effect=lambda _config, method, path, **kwargs: requests.append((method, path, kwargs["body"]))),
            ):
                executor._sync_result({}, 1, "a", task, {"version": 2}, "turn-1", "completed", output)

            document = Path(directory) / "doc/api/a/文档.md"
            self.assertEqual("# API 需求\n\n- 支持幂等\n", document.read_text(encoding="utf-8"))
            self.assertNotIn("requirementDocument", requests[0][2])

    def test_requirement_document_reads_workspace_file_without_backend_content(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            document = workspace / "doc/api/a/文档.md"
            document.parent.mkdir(parents=True)
            document.write_text("# Workspace requirement\n", encoding="utf-8")
            executor = bridge.ExecutionBridge(workspace)
            with patch.object(
                executor,
                "_task_detail",
                return_value={"requirementDocumentPath": "doc/api/a/文档.md"},
            ):
                result = executor.requirement_document(1, "a", config=self.runtime_config())

            self.assertTrue(result["exists"])
            self.assertEqual("doc/api/a/文档.md", result["path"])
            self.assertEqual("# Workspace requirement\n", result["content"])

    def test_requirement_document_rejects_path_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            executor = bridge.ExecutionBridge(Path(directory))
            with patch.object(
                executor,
                "_task_detail",
                return_value={"requirementDocumentPath": "../secret.txt"},
            ):
                with self.assertRaises(bridge.BridgeFailure):
                    executor.requirement_document(1, "a", config=self.runtime_config())

    def test_requirement_document_editor_writes_the_same_workspace_file(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            executor = bridge.ExecutionBridge(workspace)
            with patch.object(
                executor,
                "_task_detail",
                return_value={"requirementDocumentPath": "doc/api/a/文档.md"},
            ):
                result = executor.save_requirement_document(1, "a", "# API 需求\n\n待梳理", config=self.runtime_config())

            saved = workspace / "doc/api/a/文档.md"
            self.assertTrue(result["exists"])
            self.assertEqual("doc/api/a/文档.md", result["path"])
            self.assertEqual("# API 需求\n\n待梳理\n", saved.read_text(encoding="utf-8"))

    def test_document_set_lists_every_document_of_the_task_document_column(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            column = workspace / "doc/api/a"
            column.mkdir(parents=True)
            (column / "文档.md").write_text("# 需求\n", encoding="utf-8")
            (column / "接口设计.md").write_text("# 接口\n", encoding="utf-8")
            (column / "截图.png").write_bytes(b"\x89PNG")
            executor = bridge.ExecutionBridge(workspace)
            with patch.object(executor, "_task_detail", return_value={"requirementDocumentPath": "doc/api/a/文档.md"}):
                result = executor.document_set(1, "task-document", "a", config=self.runtime_config())

            self.assertEqual("doc/api/a", result["directory"])
            self.assertEqual("doc/api/a/文档.md", result["primaryPath"])
            self.assertEqual(
                {"doc/api/a/文档.md", "doc/api/a/接口设计.md"},
                {entry["path"] for entry in result["files"]},
            )

    def test_document_set_falls_back_to_the_first_document_when_the_primary_one_is_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            column = workspace / "doc/test/a"
            column.mkdir(parents=True)
            (column / "测试计划.md").write_text("# 计划\n", encoding="utf-8")
            executor = bridge.ExecutionBridge(workspace)
            with patch.object(executor, "_task_detail", return_value={"requirementDocumentPath": "doc/api/a/文档.md"}):
                result = executor.document_set(1, "task-testing", "a", config=self.runtime_config())

            self.assertEqual("doc/test/a/测试计划.md", result["primaryPath"])

    def test_requirement_outline_column_ignores_the_prototype_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            column = workspace / "doc/requirements/req-a"
            (column / "prototype").mkdir(parents=True)
            (column / "需求大纲.md").write_text("# 大纲\n", encoding="utf-8")
            (column / "补充说明.md").write_text("# 补充\n", encoding="utf-8")
            (column / "prototype" / "说明.md").write_text("# 原型\n", encoding="utf-8")
            executor = bridge.ExecutionBridge(workspace)
            with patch.object(executor, "_requirement_for_prototype", return_value={"requirementKey": "req-a"}):
                result = executor.document_set(1, "requirement-outline", "req-a", config=self.runtime_config())

            self.assertEqual(
                {"doc/requirements/req-a/需求大纲.md", "doc/requirements/req-a/补充说明.md"},
                {entry["path"] for entry in result["files"]},
            )

    def test_document_file_refuses_a_path_outside_its_column(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / "doc/api/a").mkdir(parents=True)
            (workspace / "doc/secret.md").write_text("# 机密\n", encoding="utf-8")
            executor = bridge.ExecutionBridge(workspace)
            with patch.object(executor, "_task_detail", return_value={"requirementDocumentPath": "doc/api/a/文档.md"}):
                with self.assertRaises(bridge.BridgeFailure):
                    executor.document_file(1, "task-document", "a", "doc/secret.md", config=self.runtime_config())

    def test_saving_a_column_document_only_overwrites_an_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            column = workspace / "doc/api/a"
            column.mkdir(parents=True)
            (column / "接口设计.md").write_text("# 接口\n", encoding="utf-8")
            executor = bridge.ExecutionBridge(workspace)
            with patch.object(executor, "_task_detail", return_value={"requirementDocumentPath": "doc/api/a/文档.md"}):
                saved = executor.save_document_file(
                    1, "task-document", "a", "doc/api/a/接口设计.md", "# 接口\n\n新增字段", config=self.runtime_config(),
                )
                with self.assertRaisesRegex(bridge.BridgeFailure, "文档不存在"):
                    executor.save_document_file(
                        1, "task-document", "a", "doc/api/a/新文档.md", "# 新", config=self.runtime_config(),
                    )

            self.assertTrue(saved["exists"])
            self.assertEqual("# 接口\n\n新增字段\n", (column / "接口设计.md").read_text(encoding="utf-8"))

    def test_unknown_document_column_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            executor = bridge.ExecutionBridge(Path(directory))
            with self.assertRaisesRegex(bridge.BridgeFailure, "未知的文档栏目"):
                executor.document_set(1, "task-unknown", "a", config=self.runtime_config())

    def test_legacy_task_outline_is_migrated_without_overwriting_the_new_document(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            executor = bridge.ExecutionBridge(workspace)
            task = {
                "requirementKey": "req-a",
                "itemKey": "api-1",
                "moduleKey": "api",
                "requirementDocumentPath": "doc/api/api-1/文档.md",
            }
            legacy = workspace / "doc/requirements/req-a/api-1/需求大纲.md"
            legacy.parent.mkdir(parents=True)
            legacy.write_text("# 旧任务大纲\n\n保留这段内容", encoding="utf-8")

            destination = executor._migrate_legacy_task_outline(task)

            self.assertEqual((workspace / "doc/api/api-1/文档.md").resolve(), destination)
            self.assertEqual("# 旧任务大纲\n\n保留这段内容", destination.read_text(encoding="utf-8"))
            self.assertTrue(legacy.is_file())
            destination.write_text("# 已完成的新文档\n", encoding="utf-8")
            self.assertIsNone(executor._migrate_legacy_task_outline(task))
            self.assertEqual("# 已完成的新文档\n", destination.read_text(encoding="utf-8"))

    def test_interrupted_turn_marks_session_and_task_blocked(self):
        requests = []
        executor = bridge.ExecutionBridge(Path.cwd())
        executor.pending_session_syncs = unittest.mock.MagicMock()
        with (
            patch.object(executor, "_task_detail", return_value={"version": 4, "phase": "development", "status": "doing"}),
            patch.object(bridge.planner, "request_api", side_effect=lambda _config, method, path, **kwargs: requests.append((method, path, kwargs["body"]))),
        ):
            executor._sync_result(
                {"api_url": "http://test/api", "key": "x"},
                1,
                "a",
                {"version": 3, "phase": "development", "progress": 25, "status": "doing"},
                {"version": 2},
                "turn-1",
                "interrupted",
            )

        self.assertEqual("blocked", requests[0][2]["status"])
        self.assertEqual("blocked", requests[1][2]["status"])

    def test_result_sync_retries_transient_task_board_failure(self):
        attempts = 0

        def request_api(_config, _method, _path, **_kwargs):
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise bridge.planner.ToolFailure("temporary")
            return {"ok": True}

        with (
            patch.object(bridge.planner, "request_api", side_effect=request_api),
            patch.object(bridge.time, "sleep"),
        ):
            result = bridge.ExecutionBridge._request_with_retry({}, "/test", {})

        self.assertEqual({"ok": True}, result)
        self.assertEqual(3, attempts)

    def test_wait_turn_polls_even_when_notifications_keep_arriving(self):
        client = bridge.AppServerClient.__new__(bridge.AppServerClient)
        client.thread_id = "thread-1"
        client.process = unittest.mock.MagicMock()
        client.process.poll.return_value = None
        client.messages = bridge.queue.Queue()
        client.messages.put({"method": "unrelated/notification"})
        client.read_turn_status = unittest.mock.MagicMock(side_effect=["inProgress", "interrupted"])

        status = client.wait_turn("turn-1", poll_interval=0)

        self.assertEqual("interrupted", status)
        self.assertEqual(2, client.read_turn_status.call_count)

    def test_follow_flushes_codex_thread_before_publishing_terminal_event(self):
        executor = bridge.ExecutionBridge(Path.cwd())
        client = unittest.mock.MagicMock()
        client.thread_id = "thread-1"
        client.wait_turn.return_value = "completed"
        client.read_turn.return_value = {"items": [{"type": "agentMessage", "text": "done"}]}
        order = []
        client.close.side_effect = lambda: order.append("close")
        executor.progress.publish = unittest.mock.MagicMock(side_effect=lambda *_args: order.append("publish"))

        with patch.object(executor, "_sync_result", side_effect=lambda *_args: order.append("sync")):
            executor._follow(
                ("whatsapp", 1, "a"), client, {}, 1, "a",
                {"phase": "development"}, {"version": 2}, "turn-1",
            )

        self.assertEqual(["sync", "close", "publish", "close"], order)

    def test_reconcile_does_not_scan_projects_without_a_current_user_token(self):
        executor = bridge.ExecutionBridge(Path.cwd())
        executor.active.add(("whatsapp", 1, "a"))
        requests = []

        def request_api(_config, _method, path, **_kwargs):
            requests.append(path)
            if path == "/bizline/lines":
                return [{"code": "whatsapp"}]
            if path == "/delivery/programs":
                return [{"programId": 1}]
            raise AssertionError(f"unexpected request: {path}")

        with (
            patch.object(bridge.planner, "load_config", return_value={"api_url": "http://test/api", "key": "x"}),
            patch.object(bridge.planner, "request_api", side_effect=request_api),
            patch.object(
                bridge.planner,
                "project_context",
                return_value={"items": [{"itemKey": "a", "phase": "development", "status": "doing"}]},
            ),
            patch.object(bridge, "AppServerClient") as app_server,
        ):
            executor.reconcile()

        self.assertEqual([], requests)
        app_server.assert_not_called()

    def test_reconcile_forever_runs_periodically(self):
        executor = bridge.ExecutionBridge(Path.cwd())
        executor.reconcile = unittest.mock.MagicMock(side_effect=[None, RuntimeError("stop")])
        with patch.object(bridge.time, "sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "stop"):
                executor.reconcile_forever(interval=7)

        self.assertEqual(2, executor.reconcile.call_count)
        sleep.assert_called_once_with(7)


    def test_local_session_catalog_store_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            store = bridge.GitEnvironmentSessionStore(Path(directory) / "sessions.json")
            store.save("codex", {
                "threadId": "thread-1", "turnId": "turn-1",
                "catalog": [{"threadId": "thread-1", "title": "会话一", "status": "running"}],
            })
            store.save("claude", {
                "threadId": "thread-2", "turnId": "turn-2",
                "catalog": [{"threadId": "thread-2", "title": "会话二", "status": "running"}],
            })

            # 两个执行器各自一份目录，不互相串会话。
            self.assertEqual("thread-1", store.load("codex")["threadId"])
            self.assertEqual("turn-1", store.load("codex")["turnId"])
            self.assertEqual("thread-2", store.load("claude")["threadId"])
            # 指定的会话不在目录里时回落到最近一条，不至于把聊天面板打空。
            self.assertEqual("thread-1", store.load("codex", "missing")["threadId"])


class GitBranchTest(unittest.TestCase):
    """需求分支相关的命令全部落在真实仓库上，用临时仓库跑，不 mock 掉 Git 本身。"""

    def setUp(self):
        if not subprocess.run(["git", "--version"], capture_output=True).returncode == 0:
            self.skipTest("本机没有 Git")
        self.directory = tempfile.TemporaryDirectory()
        self.workspace = Path(self.directory.name)
        self.addCleanup(self.directory.cleanup)
        for args in (
            ["init", "--initial-branch=main"],
            ["config", "user.email", "bridge@test"],
            ["config", "user.name", "bridge"],
        ):
            subprocess.run(["git", "-C", str(self.workspace), *args], check=True, capture_output=True)
        (self.workspace / "README.md").write_text("bridge", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.workspace), "add", "."], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.workspace), "commit", "-m", "init"], check=True, capture_output=True)

    def test_branch_names_reject_shell_and_git_unsafe_values(self):
        self.assertTrue(bridge.valid_git_branch_name("feature/issue_req-1787112353409"))
        self.assertTrue(bridge.valid_git_branch_name("feature/req-1"))
        for value in ("", "-branch", "feature..1", "feature//1", "feature/", "branch;rm -rf /", "branch name", "a" * 256):
            self.assertFalse(bridge.valid_git_branch_name(value), value)

    def test_branch_catalog_lists_local_branches_and_defaults_to_current(self):
        subprocess.run(["git", "-C", str(self.workspace), "branch", "feature/a"], check=True, capture_output=True)
        catalog = bridge.git_branch_catalog(self.workspace)
        self.assertEqual(["feature/a", "main"], catalog["branches"])
        self.assertEqual("main", catalog["defaultBranch"])

    def test_branch_catalog_rejects_a_directory_without_a_repository(self):
        with tempfile.TemporaryDirectory() as plain:
            with self.assertRaises(bridge.BridgeFailure):
                bridge.git_branch_catalog(Path(plain))

    def test_workspace_status_reports_current_branch_dirty_state_and_expected_remote(self):
        remote = self.add_origin()
        expected = str(remote)
        status = bridge.git_workspace_status(self.workspace, expected)

        self.assertEqual("main", status["currentBranch"])
        self.assertTrue(status["remoteMatches"])
        self.assertFalse(status["dirty"])
        self.assertEqual(0, status["changed"])
        self.assertNotIn("remoteUrl", status)
        self.assertNotIn("expectedRemoteUrl", status)
        (self.workspace / "README.md").write_text("dirty", encoding="utf-8")

        dirty = bridge.git_workspace_status(self.workspace, expected)
        self.assertTrue(dirty["dirty"])
        self.assertEqual(1, dirty["changed"])
        self.assertGreaterEqual(dirty["unstaged"], 1)

        subprocess.run(["git", "-C", str(self.workspace), "add", "README.md"], check=True, capture_output=True)
        (self.workspace / "README.md").write_text("partially staged", encoding="utf-8")
        partially_staged = bridge.git_workspace_status(self.workspace, expected)
        self.assertEqual(1, partially_staged["changed"])
        self.assertGreaterEqual(partially_staged["staged"], 1)
        self.assertGreaterEqual(partially_staged["unstaged"], 1)

    def test_workspace_status_detects_a_different_expected_remote(self):
        self.add_origin()
        status = bridge.git_workspace_status(self.workspace, "git@github.com:example/other.git")

        self.assertFalse(status["remoteMatches"])

    def test_create_branch_switches_to_the_new_requirement_branch(self):
        result = bridge.git_create_branch(self.workspace, "main", "feature/issue_req-1787112353409")
        self.assertTrue(result["created"])
        self.assertEqual("feature/issue_req-1787112353409", bridge.git_current_branch(self.workspace))

    def test_create_branch_reuses_an_existing_branch_instead_of_overwriting_it(self):
        bridge.git_create_branch(self.workspace, "main", "feature/issue_req-1787112353409")
        bridge.git_checkout_branch(self.workspace, "main")
        result = bridge.git_create_branch(self.workspace, "main", "feature/issue_req-1787112353409")
        self.assertFalse(result["created"])
        self.assertEqual("feature/issue_req-1787112353409", bridge.git_current_branch(self.workspace))

    def test_create_branch_rejects_an_unknown_base_branch(self):
        with self.assertRaises(bridge.BridgeFailure):
            bridge.git_create_branch(self.workspace, "release/none", "feature/issue_req-1787112353409")

    def test_create_branch_refuses_to_move_a_dirty_worktree(self):
        (self.workspace / "README.md").write_text("dirty", encoding="utf-8")
        with self.assertRaises(bridge.BridgeFailure):
            bridge.git_create_branch(self.workspace, "main", "feature/issue_req-1787112353410")

    def test_prepare_branch_stashes_uncommitted_changes_before_switching(self):
        self.add_origin()
        branch = "feature/issue_req-1"
        bridge.git_create_branch(self.workspace, "main", branch)
        bridge.git_checkout_branch(self.workspace, "main")
        (self.workspace / "README.md").write_text("dirty", encoding="utf-8")

        result = bridge.git_prepare_branch(self.workspace, branch, "stash")

        self.assertEqual(branch, result["branch"])
        self.assertTrue(result["stashed"])
        self.assertFalse(bridge.git_worktree_dirty(self.workspace))
        stash = subprocess.run(
            ["git", "-C", str(self.workspace), "stash", "list"], check=True, capture_output=True, text=True,
        )
        self.assertIn("delivery-task-planner", stash.stdout)

    def test_prepare_branch_commits_uncommitted_changes_before_switching(self):
        self.add_origin()
        branch = "feature/issue_req-1"
        bridge.git_create_branch(self.workspace, "main", branch)
        bridge.git_checkout_branch(self.workspace, "main")
        (self.workspace / "README.md").write_text("dirty", encoding="utf-8")

        result = bridge.git_prepare_branch(self.workspace, branch, "commit", "chore: preserve main")

        self.assertEqual(branch, result["branch"])
        self.assertTrue(result["committed"])
        log = subprocess.run(
            ["git", "-C", str(self.workspace), "log", "main", "-1", "--format=%s"],
            check=True, capture_output=True, text=True,
        )
        self.assertEqual("chore: preserve main", log.stdout.strip())

    def test_prepare_branch_stashes_dirty_submodules_before_switching(self):
        submodule = self.add_submodule()
        branch = "feature/issue_req-submodule-stash"
        bridge.git_create_branch(self.workspace, "main", branch)
        bridge.git_checkout_branch(self.workspace, "main")
        (submodule / "README.md").write_text("dirty", encoding="utf-8")

        result = bridge.git_prepare_branch(self.workspace, branch, "stash")

        self.assertEqual(branch, result["branch"])
        self.assertTrue(result["stashed"])
        self.assertFalse(bridge.git_worktree_dirty(self.workspace))
        stash = subprocess.run(
            ["git", "-C", str(submodule), "stash", "list"], check=True, capture_output=True, text=True,
        )
        self.assertIn("delivery-task-planner", stash.stdout)

    def test_prepare_branch_commits_dirty_submodules_before_switching(self):
        submodule = self.add_submodule()
        branch = "feature/issue_req-submodule-commit"
        bridge.git_create_branch(self.workspace, "main", branch)
        bridge.git_checkout_branch(self.workspace, "main")
        (submodule / "README.md").write_text("dirty", encoding="utf-8")

        result = bridge.git_prepare_branch(self.workspace, branch, "commit", "chore: preserve work")

        self.assertEqual(branch, result["branch"])
        self.assertTrue(result["committed"])
        self.assertFalse(bridge.git_worktree_dirty(self.workspace))
        submodule_log = subprocess.run(
            ["git", "-C", str(submodule), "log", "main", "-1", "--format=%s"], check=True, capture_output=True, text=True,
        )
        self.assertEqual("chore: preserve work (plugin/test-submodule)", submodule_log.stdout.strip())
        parent_log = subprocess.run(
            ["git", "-C", str(self.workspace), "log", "main", "-1", "--format=%s"],
            check=True, capture_output=True, text=True,
        )
        self.assertEqual("chore: preserve work", parent_log.stdout.strip())

    def test_prepare_branch_returns_a_consistent_status_shape_when_already_on_target(self):
        result = bridge.git_prepare_branch(self.workspace, "main")

        self.assertEqual("main", result["branch"])
        self.assertIn("status", result)
        self.assertEqual("main", result["status"]["currentBranch"])

    def test_prepare_branch_rejects_an_unknown_change_strategy(self):
        with self.assertRaises(bridge.BridgeFailure):
            bridge.git_prepare_branch(self.workspace, "main", "discard")

    def add_origin(self) -> Path:
        remote = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(remote, ignore_errors=True))
        subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.workspace), "remote", "add", "origin", str(remote)], check=True, capture_output=True)
        return remote

    def add_submodule(self) -> Path:
        source = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(source, ignore_errors=True))
        for args in (
            ["init", "--initial-branch=main"],
            ["config", "user.email", "submodule@test"],
            ["config", "user.name", "submodule"],
        ):
            subprocess.run(["git", "-C", str(source), *args], check=True, capture_output=True)
        (source / "README.md").write_text("submodule", encoding="utf-8")
        subprocess.run(["git", "-C", str(source), "add", "."], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(source), "commit", "-m", "init"], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(self.workspace), "-c", "protocol.file.allow=always", "submodule", "add", str(source), "plugin/test-submodule"],
            check=True,
            capture_output=True,
        )
        subprocess.run(["git", "-C", str(self.workspace), "commit", "-am", "add submodule"], check=True, capture_output=True)
        submodule = self.workspace / "plugin/test-submodule"
        subprocess.run(["git", "-C", str(submodule), "config", "user.email", "submodule@test"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(submodule), "config", "user.name", "submodule"], check=True, capture_output=True)
        return submodule

    def test_push_commits_the_worktree_before_pushing(self):
        self.add_origin()
        bridge.git_create_branch(self.workspace, "main", "feature/issue_req-1")
        (self.workspace / "README.md").write_text("changed", encoding="utf-8")
        result = bridge.git_push_branch(self.workspace, "feature/issue_req-1", "feat: 需求改动")
        self.assertTrue(result["committed"])
        self.assertEqual("feat: 需求改动", result["commitMessage"])
        self.assertEqual("origin", result["remote"])
        self.assertFalse(bridge.git_worktree_dirty(self.workspace))
        log = subprocess.run(
            ["git", "-C", str(self.workspace), "log", "-1", "--format=%s", "origin/feature/issue_req-1"],
            check=True, capture_output=True, text=True,
        )
        self.assertEqual("feat: 需求改动", log.stdout.strip())

    def test_push_without_local_changes_only_pushes(self):
        self.add_origin()
        bridge.git_create_branch(self.workspace, "main", "feature/issue_req-1")
        result = bridge.git_push_branch(self.workspace, "feature/issue_req-1", "feat: 需求改动")
        self.assertFalse(result["committed"])
        self.assertTrue(result["pushed"])

    def test_branch_sync_check_reflects_the_remote_state(self):
        self.add_origin()
        bridge.git_create_branch(self.workspace, "main", "feature/issue_req-1")
        (self.workspace / "README.md").write_text("changed", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.workspace), "commit", "-am", "wip"], check=True, capture_output=True)
        self.assertFalse(bridge.git_branch_synced(self.workspace, "feature/issue_req-1"))
        bridge.git_push_branch(self.workspace, "feature/issue_req-1", "feat: 推送")
        self.assertTrue(bridge.git_branch_synced(self.workspace, "feature/issue_req-1"))

    def test_push_repair_prompt_carries_the_failure_and_its_limits(self):
        prompt = bridge.build_git_push_repair_prompt(
            self.workspace, "feature/issue_req-1", "origin", "! [rejected] non-fast-forward", "feat: 需求改动",
        )
        self.assertIn("! [rejected] non-fast-forward", prompt)
        self.assertIn("feature/issue_req-1", prompt)
        self.assertIn("force push", prompt)
        self.assertIn("最后必须实际执行一次 push", prompt)

    def test_push_refuses_dirty_changes_made_on_another_branch(self):
        self.add_origin()
        bridge.git_create_branch(self.workspace, "main", "feature/issue_req-1")
        bridge.git_checkout_branch(self.workspace, "main")
        (self.workspace / "README.md").write_text("changed", encoding="utf-8")
        with self.assertRaises(bridge.BridgeFailure):
            bridge.git_push_branch(self.workspace, "feature/issue_req-1", "feat: 需求改动")

    def test_push_requires_an_origin_remote(self):
        bridge.git_create_branch(self.workspace, "main", "feature/issue_req-1")
        with self.assertRaises(bridge.BridgeFailure):
            bridge.git_push_branch(self.workspace, "feature/issue_req-1", "")

    def test_push_rejects_an_unknown_branch(self):
        self.add_origin()
        with self.assertRaises(bridge.BridgeFailure):
            bridge.git_push_branch(self.workspace, "feature/issue_missing", "")

    def test_task_prompt_pins_changes_to_the_requirement_branch(self):
        prompt = bridge.build_task_prompt({
            "programId": 1,
            "gitBranch": "feature/issue_req-1787112353409",
            "task": {"itemKey": "T-1", "title": "任务", "phase": "development"},
        })
        self.assertIn("feature/issue_req-1787112353409", prompt)
        self.assertNotIn("Git 需求分支", bridge.build_task_prompt({
            "programId": 1,
            "task": {"itemKey": "T-1", "title": "任务", "phase": "development"},
        }))


if __name__ == "__main__":
    unittest.main()
