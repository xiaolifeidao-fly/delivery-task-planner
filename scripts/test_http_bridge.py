#!/usr/bin/env python3

import importlib.util
import base64
import collections
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


BRIDGE_PATH = Path(__file__).resolve().parents[1] / "http_bridge.py"
PLUGIN_ROOT = BRIDGE_PATH.parent
TEST_RUNTIME_DIRECTORY = tempfile.TemporaryDirectory()
os.environ["DELIVERY_TASK_PLANNER_RUNTIME_DIR"] = TEST_RUNTIME_DIRECTORY.name
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))
SPEC = importlib.util.spec_from_file_location("delivery_task_http_bridge", BRIDGE_PATH)
bridge = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(bridge)


class HttpBridgeTest(unittest.TestCase):
    def setUp(self) -> None:
        # 桥接请求会顺手刷新凭证文件；测试绝不能写到本机真实凭证上。
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        patcher = patch.object(bridge.planner, "CREDENTIAL_PATH", Path(directory.name) / "credential.json")
        patcher.start()
        self.addCleanup(patcher.stop)

        # 批次本身现在由服务端持久化；队列算法单测不需要真的访问 HTTP 服务。
        self.execution_batches: list[dict[str, object]] = []

        def create_execution_batch(_config, _program_id, item_keys, mode, provider, redo=False):
            batch_id = f"batch-test-{len(self.execution_batches) + 1}"
            batch = {"batchId": batch_id, "itemKeys": list(item_keys), "mode": mode, "provider": provider, "redo": bool(redo)}
            self.execution_batches.append(batch)
            return batch

        batch_create_patcher = patch.object(bridge.ExecutionBridge, "_create_execution_batch", side_effect=create_execution_batch)
        batch_item_patcher = patch.object(bridge.ExecutionBridge, "_update_execution_batch_item", return_value=None)
        batch_finalize_patcher = patch.object(bridge.ExecutionBridge, "_finalize_execution_batch", return_value=None)
        batch_create_patcher.start()
        batch_item_patcher.start()
        batch_finalize_patcher.start()
        self.addCleanup(batch_create_patcher.stop)
        self.addCleanup(batch_item_patcher.stop)
        self.addCleanup(batch_finalize_patcher.stop)

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
        business_root = Path("/business-workspaces")

        with patch.object(bridge, "ThreadingHTTPServer", return_value=server) as http_server:
            result = bridge.create_http_server(
                "127.0.0.1", 8765, workspace, {"*"}, business_root,
            )

        self.assertIs(server, result)
        http_server.assert_called_once_with(("127.0.0.1", 8765), bridge.BridgeHandler)
        self.assertEqual(workspace, server.bridge.workspace)
        self.assertEqual({"*"}, server.allowed_origins)
        self.assertEqual(business_root, server.business_workspace_root)

    def test_http_server_uses_default_business_workspace_root(self):
        server = unittest.mock.MagicMock()

        with patch.object(bridge, "ThreadingHTTPServer", return_value=server):
            bridge.create_http_server("127.0.0.1", 8765, Path("/workspace"), {"*"})

        self.assertEqual(bridge.DEFAULT_BUSINESS_WORKSPACE_ROOT.resolve(), server.business_workspace_root)

    def test_business_workspace_is_created_under_its_configured_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "business-root"
            workspace = bridge.business_workspace_path_of("alice/业务空间/客户运营平台", root)

            self.assertEqual((root / "alice" / "业务空间" / "客户运营平台").resolve(), workspace)
            self.assertTrue(workspace.is_dir())
            chinese_owner_workspace = bridge.business_workspace_path_of("业务用户/业务空间/客户运营平台", root)
            self.assertEqual((root / "业务用户" / "业务空间" / "客户运营平台").resolve(), chinese_owner_workspace)
            self.assertTrue(chinese_owner_workspace.is_dir())
            with self.assertRaises(bridge.BridgeFailure):
                bridge.business_workspace_path_of("../../etc", root)

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

    def test_plugin_runtime_endpoints_report_manifest_version_and_running_python_value(self):
        handler = object.__new__(bridge.BridgeHandler)
        responses = []
        handler.json_response = lambda status, payload: responses.append((status, payload))

        with (
            patch.object(bridge, "PLUGIN_RUNTIME_VERSION", "0.4.0+codex.test"),
            patch.object(bridge, "installed_plugin_version", return_value="0.5.0+codex.disk") as disk_version,
        ):
            handler.path = "/v1/plugin/info"
            handler.do_GET()
            handler.path = "/v1/plugin/runtime-test"
            handler.do_GET()

        self.assertEqual((200, {"installed": True, "version": "0.4.0+codex.test"}), responses[0])
        self.assertEqual((200, {"value": "delivery-task-planner-python-runtime-v6"}), responses[1])
        disk_version.assert_not_called()

    def test_silent_update_waits_for_active_runs_before_restarting(self):
        local_bridge = unittest.mock.MagicMock()
        local_bridge.active_run_count.side_effect = [2, 0]
        job = {"jobId": "job-1", "status": "restart_required"}

        with (
            patch.object(bridge.PLUGIN_UPDATES, "get_job", return_value=job),
            patch.object(bridge.PLUGIN_UPDATES, "mark_waiting_for_runs") as mark_waiting,
            patch.object(bridge.PLUGIN_UPDATES, "mark_restarting") as mark_restarting,
            patch.object(bridge, "schedule_bridge_restart") as schedule_restart,
            patch.object(bridge.time, "sleep") as sleep,
            patch.object(bridge.threading, "Thread") as thread,
        ):
            bridge.complete_plugin_update_in_background("job-1", local_bridge)
            monitor = thread.call_args.kwargs["target"]
            monitor()

        self.assertEqual(2, local_bridge.active_run_count.call_count)
        mark_waiting.assert_called_once_with("job-1", 2)
        sleep.assert_called_once_with(bridge.PLUGIN_UPDATE_RESTART_POLL_SECONDS)
        mark_restarting.assert_called_once_with("job-1")
        schedule_restart.assert_called_once_with()

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

    def test_planning_temp_path_is_plugin_local_and_sanitizes_names(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(bridge, "PLUGIN_ROOT", Path(temporary)):
            path = bridge.planning_temp_document_path("导入/审核", "req-a", "thread/one")

        self.assertEqual(
            Path(temporary) / ".temp/requirements/req_导入_审核/thread_one/temp.md",
            path,
        )

    def test_planning_temp_summary_overwrites_the_latest_chat_artifact(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "temp.md"
            bridge.write_planning_temp_summary(path, "审核", "req-a", "thread-1", "第一次", "初稿")
            bridge.write_planning_temp_summary(path, "审核", "req-a", "thread-1", "第二次", "最新方案")
            content = path.read_text(encoding="utf-8")

        self.assertIn("第二次", content)
        self.assertIn("最新方案", content)
        self.assertNotIn("第一次", content)
        self.assertNotIn("初稿", content)

    def test_planning_temp_summary_is_deleted_only_inside_the_managed_directory(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(bridge, "PLUGIN_ROOT", Path(temporary)):
            path = bridge.planning_temp_document_path("审核", "req-a", "thread-1")
            bridge.write_planning_temp_summary(path, "审核", "req-a", "thread-1", "确认", "最终方案")

            deleted = bridge.delete_planning_temp_summary(path)
            deleted_again = bridge.delete_planning_temp_summary(path)
            with self.assertRaisesRegex(bridge.BridgeFailure, "超出插件临时目录"):
                bridge.delete_planning_temp_summary(Path(temporary) / "outside.md")

        self.assertTrue(deleted)
        self.assertFalse(deleted_again)
        self.assertFalse(path.exists())

    def test_planning_outline_is_read_only_until_confirmation(self):
        preview = bridge.build_planning_prompt(
            1, {"program": {}}, "先预览", requirement={"requirementKey": "req-a", "name": "审核"},
        )
        confirmed = bridge.build_planning_prompt(
            1, {"program": {}}, "确认并写入", requirement={"requirementKey": "req-a", "name": "审核"},
            write_allowed=True, thread_id="thread-1",
        )

        self.assertIn("不存在时也不得创建", preview)
        self.assertNotIn("本轮必须把最终确认结果写入这个文件", preview)
        self.assertIn("本轮必须把最终确认结果写入这个文件", confirmed)
        self.assertIn(".temp/requirements/req_审核/thread-1/temp.md", confirmed)
        self.assertIn("写最终需求文档前必须完整读取这份过程总结", confirmed)
        self.assertIn("`temp.md` 只是候选材料，禁止整段复制", confirmed)
        self.assertIn("只把有实际交付价值且已经确认的内容", confirmed)
        self.assertIn("删除寒暄、反复确认、讨论过程、未采纳备选", confirmed)
        self.assertIn("不得借精简遗漏已确认的非目标、兼容性要求", confirmed)
        self.assertNotIn("`temp.md` 只是候选材料，禁止整段复制", preview)

    def test_requirement_document_directory_is_scoped_to_the_requirement(self):
        self.assertEqual(
            Path("doc/requirements/req-a"),
            bridge.requirement_document_directory_of("req-a"),
        )
        with self.assertRaisesRegex(bridge.BridgeFailure, "需求标识无效"):
            bridge.requirement_document_directory_of("../../etc")

    def test_planning_prompt_routes_explicit_standalone_assets_to_the_workspace(self):
        prompt = bridge.build_planning_prompt(
            1,
            {"program": {"name": "Universe"}},
            "再生成一份独立的流程图",
            requirement={"requirementKey": "req-a"},
        )

        self.assertIn("doc/requirements/req-a/", prompt)
        self.assertIn("独立流程图、图表、HTML 或其他文件", prompt)
        self.assertIn("doc/requirements/req-a/prototype/", prompt)
        self.assertIn("doc/test/req-a/", prompt)
        self.assertIn("不得写入 `.codex/visualizations`", prompt)
        self.assertIn("除当前需求文档目录外仍不得修改工作区其他文件", prompt)

    def test_planning_prompt_only_lists_tasks_of_the_current_requirement(self):
        context = {
            "program": {"name": "Universe"},
            "items": [
                {"itemKey": "task-a", "title": "本需求任务", "requirementKey": "req-a"},
                {"itemKey": "task-z", "title": "别的需求任务", "requirementKey": "req-z"},
            ],
        }

        prompt = bridge.build_planning_prompt(
            1, context, "拆一下", requirement={"requirementKey": "req-a"},
        )

        self.assertIn("task-a", prompt)
        self.assertNotIn("task-z", prompt)
        self.assertNotIn("项目全部任务", prompt)
        self.assertIn("去重、复用和依赖判断都只在本需求范围内进行", prompt)

    def test_planning_follow_up_prompt_keeps_only_the_context_that_changes_or_matters(self):
        context = {
            "program": {"name": "Universe"},
            "stages": [{"stageKey": "m1", "tag": "里程碑一"}],
            "modules": [{"moduleKey": "web", "name": "控制台"}],
            "items": [
                {"itemKey": "task-a", "title": "本需求任务", "requirementKey": "req-a"},
                {"itemKey": "task-z", "title": "别的需求任务", "requirementKey": "req-z"},
            ],
        }
        requirement = {"requirementKey": "req-a", "name": "需求一", "detail": "正文"}

        follow_up = bridge.build_planning_follow_up_prompt(
            1, context, "再加一条", "m1", "web", "", requirement, thread_id="thread-1",
        )

        self.assertIn("task-a", follow_up)
        self.assertNotIn("task-z", follow_up)
        self.assertIn("doc/requirements/req-a/需求大纲.md", follow_up)
        self.assertIn("预览和讨论阶段它是只读的最终产物", follow_up)
        self.assertIn(".temp/requirements/req_需求一/thread-1/temp.md", follow_up)
        self.assertIn("正常连续对话直接使用当前聊天上下文，不要重复读取这个文件", follow_up)
        self.assertIn("才读取它作为恢复点", follow_up)
        self.assertIn("现有里程碑键（取值只能从中选）: m1", follow_up)
        self.assertIn("禁止执行 create-task-board-tasks", follow_up)
        self.assertIn("再加一条", follow_up)
        # 首轮讲过的角色说明、勘察纪律和现有选项明细不再逐轮重发。
        self.assertNotIn("这是交付任务面板的需求梳理会话", follow_up)
        self.assertNotIn("拆解前必须先勘察", follow_up)
        self.assertLess(
            len(follow_up),
            len(bridge.build_planning_prompt(1, context, "再加一条", "m1", "web", "", requirement)),
        )

    def test_planning_follow_up_prompt_resends_the_detail_only_after_it_changes(self):
        requirement = {"requirementKey": "req-a", "detail": "改过的正文"}
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            with patch.object(bridge, "PLUGIN_ROOT", workspace):
                draft = bridge.planning_temp_document_path("", "req-a", "thread-1")
                draft.parent.mkdir(parents=True, exist_ok=True)
                draft.write_text("# 已沉淀的过程摘要\n", encoding="utf-8")

                unchanged = bridge.build_planning_follow_up_prompt(
                    1, {"program": {}}, "继续", requirement=requirement, workspace=workspace,
                    thread_id="thread-1",
                )
                changed = bridge.build_planning_follow_up_prompt(
                    1, {"program": {}}, "继续", requirement=requirement, workspace=workspace,
                    include_detail=True, thread_id="thread-1",
                )

        self.assertNotIn("改过的正文", unchanged)
        self.assertIn("需求详细信息与本会话首轮给出的一致", unchanged)
        self.assertIn("改过的正文", changed)
        self.assertNotEqual(
            bridge.planning_detail_digest(requirement),
            bridge.planning_detail_digest({"detail": "另一版正文"}),
        )

    def test_planning_follow_up_prompt_repeats_the_detail_until_the_temp_summary_lands(self):
        """过程摘要没落盘前，需求正文只活在首轮那条消息里，压缩掉就没了。"""
        requirement = {"requirementKey": "req-a", "detail": "还没沉淀的正文"}
        with tempfile.TemporaryDirectory() as temporary:
            follow_up = bridge.build_planning_follow_up_prompt(
                1, {"program": {}}, "继续", requirement=requirement, workspace=Path(temporary),
            )

        self.assertIn("还没沉淀的正文", follow_up)
        self.assertIn("聊天过程摘要尚未落盘", follow_up)

    def test_requirement_testing_follow_up_prompt_drops_the_boilerplate_but_refreshes_the_inventory(self):
        context = {
            "items": [
                {"itemKey": "task-a", "title": "接口", "requirementKey": "req-a", "status": "done", "actionOutput": "x"},
            ],
        }
        requirement = {"requirementKey": "req-a", "name": "需求一", "detail": "很长的需求正文"}

        follow_up = bridge.build_requirement_testing_prompt(
            1, context, requirement, "继续测", follow_up=True,
        )
        first = bridge.build_requirement_testing_prompt(1, context, requirement, "继续测")

        self.assertIn("task-a", follow_up)
        self.assertIn("doc/test/req-a/", follow_up)
        self.assertIn("验收判定：通过 / 不通过 / 受阻", follow_up)
        self.assertNotIn("很长的需求正文", follow_up)
        self.assertIn("本会话如果被压缩过", follow_up)
        self.assertLess(len(follow_up), len(first))

    def test_requirement_testing_follow_up_prompt_keeps_the_design_only_red_line(self):
        follow_up = bridge.build_requirement_testing_prompt(
            1, {}, {"requirementKey": "req-a"}, "再补几条", test_case_only=True, follow_up=True,
        )

        self.assertIn("绝不调用接口、UI、脚本或构建命令执行真实测试", follow_up)
        self.assertIn("测试用例已生成", follow_up)

    def test_requirement_testing_follow_up_prompt_resends_a_changed_detail(self):
        requirement = {"requirementKey": "req-a", "detail": "改过的正文"}

        follow_up = bridge.build_requirement_testing_prompt(
            1, {}, requirement, "继续测", follow_up=True, include_detail=True,
        )

        self.assertIn("改过的正文", follow_up)

    def test_requirement_review_prompt_states_the_three_rules_and_the_selected_scope(self):
        requirement = {"requirementKey": "req-a", "name": "需求一", "detail": "需求正文"}
        scope = bridge.review_scope_of([
            {"path": "", "name": "根工程", "changed": 3, "files": ["server/a.go", "client/b.ts"]},
            {"path": "sub", "name": "子工程", "changed": 5, "files": []},
        ])

        prompt = bridge.build_requirement_review_prompt(1, requirement, "重点看并发", None, scope)

        self.assertIn("doc/、chat/ 目录下的内容一律不 review", prompt)
        self.assertIn("backend-development", prompt)
        self.assertIn("用户在本轮聊天里写的检查重点和额外规则，优先级最高", prompt)
        self.assertIn("server/a.go", prompt)
        self.assertIn("子工程", prompt)
        self.assertIn("这个工程里所有未提交改动都在范围内", prompt)
        self.assertIn("重点看并发", prompt)

    def test_requirement_review_prompt_carries_the_general_review_guidelines(self):
        """通用评审准则是固定下发的第四条规则，首轮必须完整出现。"""
        requirement = {"requirementKey": "req-a", "name": "需求一", "detail": "需求正文"}
        scope = bridge.review_scope_of([{"path": "", "name": "根工程", "changed": 1, "files": ["server/a.go"]}])

        prompt = bridge.build_requirement_review_prompt(1, requirement, "先看看", None, scope)

        self.assertIn("规则四（评审准则）", prompt)
        self.assertIn("You are acting as a reviewer", prompt)
        self.assertIn("::code-comment{...}", prompt)
        self.assertIn("Prefer no issues over speculative or low-signal feedback.", prompt)

    def test_requirement_review_follow_up_prompt_keeps_the_scope_but_drops_the_boilerplate(self):
        requirement = {"requirementKey": "req-a", "name": "需求一", "detail": "很长的需求正文"}
        scope = bridge.review_scope_of([{"path": "", "name": "根工程", "changed": 1, "files": ["server/a.go"]}])

        follow_up = bridge.build_requirement_review_prompt(1, requirement, "再看一遍", None, scope, follow_up=True)

        self.assertIn("server/a.go", follow_up)
        self.assertIn("doc/、chat/ 目录始终不在 review 范围内", follow_up)
        self.assertNotIn("很长的需求正文", follow_up)

    def test_requirement_review_report_round_names_the_report_file_and_the_first_line(self):
        requirement = {"requirementKey": "req-a", "name": "需求一"}
        scope = bridge.review_scope_of([{"path": "", "name": "根工程", "changed": 1, "files": ["server/a.go"]}])

        chat_only = bridge.build_requirement_review_prompt(1, requirement, "先聊聊", None, scope)
        report = bridge.build_requirement_review_prompt(1, requirement, "出报告", None, scope, generate_report=True)

        self.assertIn("本轮不写报告文件", chat_only)
        self.assertIn("doc/review/req-a/review报告.md", report)
        self.assertIn("review 报告已生成", report)
        self.assertNotIn("不写报告文件", report)

    def test_requirement_review_may_change_code_only_when_the_user_asks(self):
        """review 会话对工作区有写权限：用户要求改就改，没要求的回合只给意见。"""
        requirement = {"requirementKey": "req-a", "name": "需求一"}
        scope = bridge.review_scope_of([{"path": "", "name": "根工程", "changed": 1, "files": ["server/a.go"]}])

        first = bridge.build_requirement_review_prompt(1, requirement, "先看看", None, scope)
        follow_up = bridge.build_requirement_review_prompt(1, requirement, "按意见改", None, scope, follow_up=True)

        for prompt in (first, follow_up):
            self.assertIn("用户", prompt)
            self.assertNotIn("不要修改业务实现", prompt)
            # 提交、推送、切分支始终由面板的 Git 流程负责，review 会话不碰。
            self.assertIn("不提交", prompt.replace("不要提交", "不提交"))
        self.assertIn("写权限", first)

    def test_requirement_review_scope_rejects_paths_that_escape_the_workspace(self):
        with self.assertRaises(bridge.BridgeFailure):
            bridge.review_scope_of([{"path": "../outside", "name": "x", "files": []}])
        with self.assertRaises(bridge.BridgeFailure):
            bridge.review_scope_of([{"path": "", "name": "x", "files": ["../../etc/passwd"]}])

    def test_requirement_review_payload_requires_a_message(self):
        with self.assertRaises(bridge.BridgeFailure):
            bridge.validate_requirement_review_payload({"programId": 1, "requirementKey": "req-a", "message": "  "})

    def test_task_testing_cases_follow_up_prompt_drops_the_sibling_catalog(self):
        task = {"itemKey": "task-a", "title": "接口", "requirementKey": "req-a", "moduleKey": "web", "phase": "testing"}
        context = {"items": [{"itemKey": "task-b", "title": "兄弟任务", "requirementKey": "req-a", "moduleKey": "web"}]}

        follow_up = bridge.build_task_testing_cases_prompt(1, task, context, "再补一条", follow_up=True)

        self.assertIn("task-a", follow_up)
        self.assertIn("doc/test/task-a/", follow_up)
        self.assertIn("绝不调用接口、UI、脚本或构建命令执行真实测试", follow_up)
        self.assertIn("测试用例已生成", follow_up)
        self.assertNotIn("本需求下其他任务已写好的需求文档", follow_up)

    def test_requirement_prototype_follow_up_prompt_keeps_the_write_scope_red_line(self):
        requirement = {"requirementKey": "req-a", "name": "需求一", "detail": "很长的需求正文"}

        follow_up = bridge.build_requirement_prototype_prompt(
            1, requirement, "把按钮改成蓝色", Path("/tmp"), editing=True, follow_up=True,
        )

        self.assertIn("doc/requirements/req-a/prototype/", follow_up)
        self.assertIn("不得修改业务代码、配置、依赖或该目录以外的文件", follow_up)
        self.assertIn("保留未被本轮要求修改的内容", follow_up)
        self.assertNotIn("很长的需求正文", follow_up)

    def test_requirement_prototype_follow_up_prompt_resends_a_changed_detail(self):
        requirement = {"requirementKey": "req-a", "detail": "改过的正文"}

        follow_up = bridge.build_requirement_prototype_prompt(
            1, requirement, "再改", Path("/tmp"), editing=True, follow_up=True, include_detail=True,
        )

        self.assertIn("改过的正文", follow_up)

    def test_prototype_session_detail_digest_defaults_to_resending_the_detail(self):
        rows = [{"threadId": "t1", "metadata": {"detailDigest": "abc"}}]

        self.assertEqual("abc", bridge.prototype_session_detail_digest(rows, "t1"))
        self.assertEqual("", bridge.prototype_session_detail_digest(rows, "t2"))
        self.assertEqual("", bridge.prototype_session_detail_digest([], "t1"))

    def test_planning_follow_up_prompt_tells_a_compacted_session_to_reload_the_outline(self):
        follow_up = bridge.build_planning_follow_up_prompt(
            1, {"program": {}}, "继续", requirement={"requirementKey": "req-a", "generatePrototype": True},
            thread_id="thread-1",
        )

        self.assertIn("本会话如果被压缩过", follow_up)
        self.assertIn(".temp/requirements/req_req-a/thread-1/temp.md", follow_up)
        self.assertIn("生成需求原型图", follow_up)

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
        self.assertIn("doc/requirements/req-a/", client.steer_turn.call_args.args[2])
        self.assertIn("不得写入 `.codex/visualizations`", client.steer_turn.call_args.args[2])
        # 新开聊天要起两条后台线程：一条跟进本轮拆解，一条并行给会话和需求起名字。
        self.assertEqual(2, thread.return_value.start.call_count)

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
        self.assertIn("禁止执行 create-task-board-tasks", preview_prompt)
        # 提示词必须给出命令行入口，不能再点名已经撤掉的工具。
        self.assertNotIn("create_task_board_tasks", preview_prompt)
        self.assertNotIn("create_task_board_tasks", write_prompt)
        self.assertIn("taskboard.py", write_prompt)
        self.assertIn("已授予项目工作目录及需求指定关联目录的只读勘察权限", preview_prompt)
        self.assertIn("终端的只读命令", preview_prompt)
        self.assertIn("收益标签 / 负责人", preview_prompt)
        self.assertIn("preview", environments[0][bridge.planner.RUNTIME_WRITE_MODE_ENV])
        self.assertIn("确认并写入", write_prompt)
        self.assertIn("任务负责人由写入命令", write_prompt)
        self.assertEqual("write", environments[1][bridge.planner.RUNTIME_WRITE_MODE_ENV])
        # 面板上下文整段裹在标记里，聊天记录只回显用户自己输入的那句。
        self.assertEqual("拆解这个需求", bridge.text_without_attachment_context(preview_prompt))

    def test_planning_lists_and_reads_chats_left_by_the_other_tool(self):
        """换工具不该让聊天记录消失：目录跨执行器列全，正文按线程自己的执行器读。"""
        executor = bridge.ExecutionBridge(Path.cwd())
        client = unittest.mock.MagicMock()
        client.read_thread.return_value = {"turns": []}
        providers = []
        rows = [
            {
                "threadId": "codex-thread", "title": "需求拆解 · A", "status": "completed",
                "executorType": "codex", "updatedAt": "2026-08-01T00:00:00Z", "metadata": {},
            },
            {
                "threadId": "prototype-thread", "title": "原型", "status": "completed",
                "executorType": "codex-prototype", "updatedAt": "2026-08-02T00:00:00Z", "metadata": {},
            },
        ]

        def request_api(_config, method, path, query=None, body=None):
            if method == "GET" and path == "/delivery/requirement/planning-sessions":
                self.assertNotIn("executorType", query)
                return list(rows)
            if method == "GET" and path == "/delivery/program":
                return {"gitEnabled": False}
            self.fail(f"unexpected request: {method} {path}")

        def make_client(provider, workspace, listener=None, environment=None):
            providers.append(provider)
            return client

        with (
            patch.object(bridge.planner, "request_api", side_effect=request_api),
            patch.object(bridge, "create_ai_client", side_effect=make_client),
        ):
            result = executor.planning(
                2, "", config={"api_url": "http://test/api", "key": "k", "_project_id": 2},
                requirement_key="req-a", provider="claude",
            )

        # 原型会话与拆解会话同表不同用途，不能混进拆解列表。
        self.assertEqual(["codex-thread"], [entry["threadId"] for entry in result["conversations"]])
        self.assertEqual("codex", result["conversations"][0]["executorType"])
        self.assertEqual("codex-thread", result["threadId"])
        self.assertEqual(["codex"], providers)

    def test_planning_follow_up_keeps_the_thread_own_tool(self):
        executor = bridge.ExecutionBridge(Path.cwd())
        client = unittest.mock.MagicMock()
        client.start_turn.return_value = "turn-2"
        context = {"program": {"programId": 2, "name": "项目 2"}, "items": [], "stages": [], "modules": []}
        providers = []
        binds = []
        rows = [{
            "threadId": "codex-thread", "title": "需求拆解 · A", "status": "completed",
            "executorType": "codex", "updatedAt": "2026-08-01T00:00:00Z", "metadata": {},
        }]

        def request_api(_config, method, path, query=None, body=None):
            if method == "GET" and path == "/delivery/requirement/planning-sessions":
                return list(rows)
            if method == "POST" and path == "/delivery/requirement/planning-session/bind":
                binds.append(body)
                return None
            self.fail(f"unexpected request: {method} {path}")

        def make_client(provider, workspace, listener=None, environment=None):
            providers.append(provider)
            return client

        with (
            patch.object(bridge.planner, "project_context", return_value=context),
            patch.object(bridge.planner, "request_api", side_effect=request_api),
            patch.object(bridge, "create_ai_client", side_effect=make_client),
            patch.object(bridge.threading, "Thread"),
        ):
            executor.send_planning(
                {
                    "programId": 2, "message": "继续", "requirementKey": "req-a",
                    "threadId": "codex-thread", "provider": "claude",
                },
                {"api_url": "http://test/api", "key": "k", "_project_id": 2},
            )

        client.resume_thread.assert_called_once_with("codex-thread")
        self.assertEqual(["codex"], providers)
        self.assertEqual("codex", binds[-1]["executorType"])

    def test_task_conversation_reads_a_chat_left_by_the_other_tool(self):
        executor = bridge.ExecutionBridge(Path.cwd())
        client = unittest.mock.MagicMock()
        client.read_thread.return_value = {"turns": []}
        providers = []
        task = {"itemKey": "api-1", "title": "Create API", "phase": "development", "status": "doing"}
        binding = {
            "executorType": "codex", "phase": "development", "externalSessionId": "codex-thread",
            "status": "completed", "metadata": {
                "conversations": [{"threadId": "codex-thread", "title": "Create API", "status": "completed"}],
            },
        }

        def request_api(_config, method, path, query=None, body=None):
            if path == "/delivery/item" and method == "GET":
                return task
            if path == "/delivery/item/execution-session" and method == "GET":
                self.assertNotIn("executorType", query)
                return [binding]
            if path == "/delivery/program" and method == "GET":
                return {"gitEnabled": False}
            self.fail(f"unexpected request: {method} {path}")

        def make_client(provider, workspace, listener=None, environment=None):
            providers.append(provider)
            return client

        with (
            patch.object(bridge.planner, "request_api", side_effect=request_api),
            patch.object(bridge, "create_ai_client", side_effect=make_client),
        ):
            result = executor.conversation(1, "api-1", config=self.runtime_config(), provider="claude")

        self.assertEqual("codex-thread", result["threadId"])
        self.assertEqual("codex", result["conversations"][0]["executorType"])
        self.assertEqual(["codex"], providers)

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
                        {"type": "tool_use", "id": "t2", "name": "Edit", "input": {"file_path": "server/main.go", "old_string": "a\nb", "new_string": "a\nb\nc\nd"}},
                        {"type": "tool_use", "id": "t3", "name": "Write", "input": {"file_path": "doc/new.md", "content": "# 标题\n正文\n"}},
                        {"type": "tool_use", "id": "t4", "name": "Read", "input": {"file_path": "client/web/src/app/globals.css"}},
                        {"type": "tool_use", "id": "t5", "name": "Grep", "input": {"pattern": "delivery-session", "path": "client/web/src"}},
                        {"type": "tool_use", "id": "t6", "name": "mcp__playwright__browser_navigate", "input": {"url": "http://localhost:7893"}},
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
        self.assertEqual(
            ["agentMessage", "commandExecution", "fileChange", "fileChange", "dynamicToolCall", "dynamicToolCall", "mcpToolCall", "agentMessage"],
            types,
        )
        self.assertEqual("failed", items[1]["status"])
        self.assertEqual(1, items[1]["exitCode"])
        # Claude 不给 diff，增删行数按替换前后的文本数出来。
        self.assertEqual([{"path": "server/main.go", "kind": "modify", "added": 4, "removed": 2}], items[2]["changes"])
        self.assertEqual([{"path": "doc/new.md", "kind": "add", "added": 2, "removed": 0}], items[3]["changes"])
        # 读文件和检索是具名工具，语义写在 action/target 上，面板才不会显示成「已调用 Read」。
        self.assertEqual(("read", "client/web/src/app/globals.css"), (items[4]["action"], items[4]["target"]))
        self.assertEqual(("search", "client/web/src", "delivery-session"), (items[5]["action"], items[5]["target"], items[5]["pattern"]))
        self.assertEqual(("playwright", "browser_navigate"), (items[6]["server"], items[6]["tool"]))
        self.assertEqual("final_answer", items[7]["phase"])
        # 面板投影里 action/target 要一路带到浏览器端，检索条目的 text 是检索式。
        projected = bridge.serialize_turns(turns)[0]["items"]
        self.assertEqual(("read", "client/web/src/app/globals.css"), (projected[4]["action"], projected[4]["target"]))
        self.assertEqual(("search", "delivery-session"), (projected[5]["action"], projected[5]["text"]))
        self.assertEqual("completed", turns[0]["status"])

    def test_claude_turn_fails_when_the_result_reports_an_error(self):
        """登录过期时 Claude 照常收尾，把错误当正文吐出来：这一轮必须记成失败。"""
        events = [
            {"type": "system", "subtype": "init", "session_id": "s-1"},
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "Failed to authenticate: OAuth session expired"}]},
            },
            {
                "type": "result", "subtype": "success", "is_error": True,
                "result": "Failed to authenticate: OAuth session expired",
            },
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
            # 进程本身正常退出，判定只能靠 result 里的 is_error。
            client.process.wait.return_value = 0

            client._consume(turn)

        self.assertEqual("failed", client.turn_status)
        self.assertEqual("failed", turn["status"])
        self.assertEqual("failed", turn["items"][-1]["status"])

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
                            # Codex 实测给的是对象形式的 kind，外加一份 unified diff。
                            {"path": "d.go", "kind": {"type": "update", "move_path": None}, "diff": "@@ -1,2 +1,3 @@\n keep\n-old\n+new\n+extra\n"},
                        ]},
                    ],
                }
            ]
        )

        self.assertEqual(
            [
                {"path": "a.go", "kind": "add", "added": 0, "removed": 0},
                {"path": "b.go", "kind": "delete", "added": 0, "removed": 0},
                {"path": "c.go", "kind": "modify", "added": 0, "removed": 0},
                {"path": "d.go", "kind": "modify", "added": 2, "removed": 1},
            ],
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

    def test_codex_cli_prefers_the_newest_build_on_this_machine(self):
        """PATH 上的旧 CLI 和桌面端捆绑的 MCP 服务对不上：实测 0.134.0 调 node_repl 会被
        拒绝（sandboxCwd 不是 file URI），0.149.0-alpha 同样的调用能跑通。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            desktop = root / "resources" / "codex"
            desktop.parent.mkdir()
            desktop.write_bytes(b"desktop-codex")
            versions = {"/usr/local/bin/codex": "0.134.0", str(desktop): "0.149.0-alpha.4.1"}
            with patch.object(bridge.shutil, "which", return_value="/usr/local/bin/codex"):
                with patch.object(bridge, "host_platform", return_value="macos"):
                    with patch.object(bridge, "codex_desktop_resource_paths", return_value=[desktop]):
                        with patch.object(bridge, "codex_cli_version", side_effect=lambda command: versions.get(command, "")):
                            self.assertEqual(str(desktop), bridge.provision_codex_cli("macos", root / "runtime"))
                            self.assertEqual(str(desktop), bridge.available_codex_cli("macos", root / "runtime"))

    def test_codex_cli_keeps_the_path_build_when_it_is_not_older(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            desktop = root / "resources" / "codex"
            desktop.parent.mkdir()
            desktop.write_bytes(b"desktop-codex")
            # 版本问不出来时保持原来的优先级：PATH 上的 CLI 仍然优先。
            versions = {"/usr/local/bin/codex": "0.150.0", str(desktop): ""}
            with patch.object(bridge.shutil, "which", return_value="/usr/local/bin/codex"):
                with patch.object(bridge, "host_platform", return_value="macos"):
                    with patch.object(bridge, "codex_desktop_resource_paths", return_value=[desktop]):
                        with patch.object(bridge, "codex_cli_version", side_effect=lambda command: versions.get(command, "")):
                            self.assertEqual("/usr/local/bin/codex", bridge.provision_codex_cli("macos", root / "runtime"))

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

    def test_progress_event_forwards_reasoning_summary_delta_but_not_raw_reasoning_delta(self):
        summary_event = bridge.progress_event_of(
            {
                "method": "item/reasoning/summaryTextDelta",
                "params": {"delta": "先检查桥接器的事件映射。"},
            }
        )
        raw_event = bridge.progress_event_of(
            {
                "method": "item/reasoning/textDelta",
                "params": {"delta": "不应显示的原始内容"},
            }
        )

        self.assertEqual(("reasoning", "Codex 推理摘要", "先检查桥接器的事件映射。", "running"), summary_event)
        self.assertIsNone(raw_event)

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
        self.assertEqual("detailed", requests[2][2]["summary"])

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
        self.assertEqual("detailed", requests[1][2]["summary"])

    def test_app_server_sends_images_as_local_image_inputs(self):
        client = bridge.AppServerClient.__new__(bridge.AppServerClient)
        client.workspace = Path("/tmp/delivery-workspace")
        requests = []
        client.send = lambda method, request_id, params: requests.append((method, request_id, params))
        client.wait_response = unittest.mock.MagicMock(side_effect=[{"turn": {"id": "turn-1"}}])

        client.start_turn(
            "thread-1",
            "Review this screenshot",
            [
                {"id": "a" * 16, "name": "attachment.png", "path": "/tmp/attachment.png", "isImage": True},
                {"id": "b" * 16, "name": "spec.pdf", "path": "/tmp/spec.pdf", "isImage": False},
            ],
        )

        parts = requests[0][2]["input"]
        # 用户写的字永远是第一段，附件只在它后面补说明：带附件时把正文吃掉，
        # 执行器收到的就只剩一张图，回过头来问用户「你没写字」。
        self.assertTrue(parts[0]["text"].startswith("Review this screenshot"))
        # 图片走图片输入，非图片只剩这段说明，两种附件都要给出可读的路径。
        self.assertIn("- 图片：attachment.png，路径：/tmp/attachment.png", parts[0]["text"])
        self.assertIn("- 文件：spec.pdf，路径：/tmp/spec.pdf", parts[0]["text"])
        self.assertEqual({"type": "localImage", "path": "/tmp/attachment.png"}, parts[1])
        self.assertEqual(2, len(parts))
        # 聊天记录里回显的还是用户自己那句话。
        self.assertEqual("Review this screenshot", bridge.text_without_attachment_context(parts[0]["text"]))

    def test_serialize_turns_projects_only_browser_safe_conversation_items(self):
        turns = bridge.serialize_turns(
            [{
                "id": "turn-1",
                "status": "completed",
                "items": [
                    {"id": "u1", "type": "userMessage", "content": [{"type": "text", "text": "Implement it"}]},
                    {"id": "a1", "type": "agentMessage", "text": "Implemented and verified."},
                    {"id": "r1", "type": "reasoning", "content": ["raw reasoning"], "summary": ["Checked the interface contract."]},
                    {"id": "c1", "type": "commandExecution", "command": ["go test ./..."], "exitCode": 0},
                    {"id": "f1", "type": "fileChange", "changes": [{"path": "service/item.go", "kind": "modify"}]},
                ],
            }]
        )

        self.assertEqual("Implement it", turns[0]["items"][0]["text"])
        self.assertEqual("agentMessage", turns[0]["items"][1]["type"])
        self.assertEqual("reasoning", turns[0]["items"][2]["type"])
        self.assertEqual("Checked the interface contract.", turns[0]["items"][2]["text"])
        self.assertEqual("go test ./...", turns[0]["items"][3]["text"])
        self.assertEqual("service/item.go", turns[0]["items"][4]["text"])

    def test_thread_item_journal_keeps_the_execution_items_thread_read_drops(self):
        """`thread/read` 是有损的：命令执行和推理摘要读不回来，靠实时流补齐。"""
        with tempfile.TemporaryDirectory() as directory:
            journal = bridge.ThreadItemJournal(Path(directory))
            for message in [
                {"method": "turn/started", "params": {"threadId": "t-1", "turn": {"id": "turn-1", "status": "inProgress"}}},
                {"method": "item/completed", "params": {"threadId": "t-1", "item": {"id": "u1", "type": "userMessage", "content": [{"type": "text", "text": "跑一下测试"}]}}},
                {"method": "item/started", "params": {"threadId": "t-1", "item": {"id": "c1", "type": "commandExecution", "command": "go test ./..."}}},
                {"method": "item/completed", "params": {"threadId": "t-1", "item": {"id": "c1", "type": "commandExecution", "command": "go test ./...", "exitCode": 0, "aggregatedOutput": "ok"}}},
                {"method": "item/completed", "params": {"threadId": "t-1", "item": {"id": "a1", "type": "agentMessage", "text": "测试通过。"}}},
                {"method": "turn/completed", "params": {"threadId": "t-1", "turn": {"id": "turn-1", "status": "completed"}}},
            ]:
                journal.record(message)

            recorded = journal.read("t-1")

        self.assertEqual(["turn-1"], [turn["id"] for turn in recorded])
        self.assertEqual("completed", recorded[0]["status"])
        self.assertEqual(["u1", "c1", "a1"], [item["id"] for item in recorded[0]["items"]])
        # item/started 之后的 item/completed 是覆盖，不是追加。
        self.assertEqual(0, recorded[0]["items"][1]["exitCode"])
        # 命令输出面板不展示，不留在本地记录里。
        self.assertNotIn("aggregatedOutput", recorded[0]["items"][1])

    def test_thread_item_journal_never_stores_raw_reasoning(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = bridge.ThreadItemJournal(Path(directory))
            journal.record({"method": "turn/started", "params": {"threadId": "t-2", "turn": {"id": "turn-1"}}})
            journal.record({
                "method": "item/completed",
                "params": {"threadId": "t-2", "item": {
                    "id": "r1",
                    "type": "reasoning",
                    "summary": ["先确认接口契约。"],
                    "content": ["raw reasoning"],
                    "encryptedContent": "secret",
                }},
            })
            item = journal.read("t-2")[0]["items"][0]

        self.assertEqual(["先确认接口契约。"], item["summary"])
        self.assertNotIn("content", item)
        self.assertNotIn("encryptedContent", item)

    def test_thread_item_journal_rebuilds_reasoning_summaries_from_the_delta_stream(self):
        """推理摘要不在 item 上：实测 item/completed 的 summary 是空的，只有分片流里有正文。"""
        with tempfile.TemporaryDirectory() as directory:
            journal = bridge.ThreadItemJournal(Path(directory))
            for message in [
                {"method": "turn/started", "params": {"threadId": "t-3", "turn": {"id": "turn-1"}}},
                {"method": "item/started", "params": {"threadId": "t-3", "item": {"id": "r1", "type": "reasoning", "summary": []}}},
                {"method": "item/reasoning/summaryPartAdded", "params": {"threadId": "t-3", "itemId": "r1", "summaryIndex": 0}},
                {"method": "item/reasoning/summaryTextDelta", "params": {"threadId": "t-3", "itemId": "r1", "summaryIndex": 0, "delta": "先确认"}},
                {"method": "item/reasoning/summaryTextDelta", "params": {"threadId": "t-3", "itemId": "r1", "summaryIndex": 0, "delta": "接口契约。"}},
                {"method": "item/reasoning/summaryPartAdded", "params": {"threadId": "t-3", "itemId": "r1", "summaryIndex": 1}},
                {"method": "item/reasoning/summaryTextDelta", "params": {"threadId": "t-3", "itemId": "r1", "summaryIndex": 1, "delta": "再跑测试。"}},
                # 终态那条的 summary 是空的，不能把攒好的摘要抹掉。
                {"method": "item/completed", "params": {"threadId": "t-3", "item": {"id": "r1", "type": "reasoning", "summary": [], "status": "completed"}}},
            ]:
                journal.record(message)
            item = journal.read("t-3")[0]["items"][0]

        self.assertEqual(["先确认接口契约。", "再跑测试。"], item["summary"])
        self.assertEqual("先确认接口契约。\n\n再跑测试。", bridge.reasoning_summary_text(item))

    def test_merge_journal_turns_restores_process_items_into_thread_read_history(self):
        thread = {"id": "t-1", "turns": [
            {"id": "turn-1", "status": "completed", "items": [
                {"id": "u1", "type": "userMessage", "content": [{"type": "text", "text": "跑一下测试"}]},
                {"id": "a1", "type": "agentMessage", "text": "测试通过。"},
            ]},
            {"id": "turn-2", "status": "completed", "items": [
                {"id": "a2", "type": "agentMessage", "text": "另一轮。"},
            ]},
        ]}
        merged = bridge.merge_journal_turns(thread, [{"id": "turn-1", "status": "completed", "items": [
            {"id": "u1", "type": "userMessage", "content": [{"type": "text", "text": "跑一下测试"}]},
            {"id": "r1", "type": "reasoning", "summary": ["先确认接口契约。"]},
            {"id": "c1", "type": "commandExecution", "command": "go test ./..."},
            {"id": "a1", "type": "agentMessage", "text": "测试通过。"},
        ]}])

        items = bridge.serialize_turns(merged["turns"])[0]["items"]
        self.assertEqual(["userMessage", "reasoning", "commandExecution", "agentMessage"], [item["type"] for item in items])
        self.assertEqual("先确认接口契约。", items[1]["text"])
        # 桥接没在场的那一轮保持服务端原样。
        self.assertEqual(["a2"], [item["id"] for item in merged["turns"][1]["items"]])

    def test_merge_journal_turns_does_not_duplicate_items_whose_ids_the_server_reassigned(self):
        """实测同一条消息在实时流和 thread/read 里 id 不同，只按 id 去重会整轮重复。"""
        merged = bridge.merge_journal_turns(
            {"id": "t-1", "turns": [{"id": "turn-1", "status": "completed", "items": [
                {"id": "server-u", "type": "userMessage", "content": [{"type": "text", "text": "跑一下测试"}]},
                {"id": "server-a", "type": "agentMessage", "text": "测试通过。"},
            ]}]},
            [{"id": "turn-1", "status": "completed", "items": [
                {"id": "live-u", "type": "userMessage", "content": [{"type": "text", "text": "跑一下测试"}]},
                {"id": "live-c", "type": "commandExecution", "command": "go test ./..."},
                {"id": "live-a", "type": "agentMessage", "text": "测试通过。"},
            ]}],
        )

        self.assertEqual(["live-u", "live-c", "live-a"], [item["id"] for item in merged["turns"][0]["items"]])

    def test_merge_journal_turns_drops_reasoning_summaries_the_live_stream_already_carried(self):
        """thread/read 事后把整轮摘要合成一条还回来，实时流已经有的段落不能再重放一遍。"""
        merged = bridge.merge_journal_turns(
            {"id": "t-1", "turns": [{"id": "turn-1", "status": "completed", "items": [
                {"id": "server-r", "type": "reasoning", "summary": ["先确认接口契约。", "再补一段服务端才有的摘要。"]},
                {"id": "server-a", "type": "agentMessage", "text": "测试通过。"},
            ]}]},
            [{"id": "turn-1", "status": "completed", "items": [
                {"id": "live-r1", "type": "reasoning", "summary": ["先确认接口契约。"]},
                {"id": "live-a", "type": "agentMessage", "text": "测试通过。"},
            ]}],
        )

        items = bridge.serialize_turns(merged["turns"])[0]["items"]
        self.assertEqual(["reasoning", "agentMessage", "reasoning"], [item["type"] for item in items])
        # 重复那段被摘掉，只剩服务端独有的一段。
        self.assertEqual("再补一段服务端才有的摘要。", items[2]["text"])

    def test_merge_journal_turns_drops_a_reasoning_item_whose_summary_is_fully_duplicated(self):
        merged = bridge.merge_journal_turns(
            {"id": "t-1", "turns": [{"id": "turn-1", "status": "completed", "items": [
                {"id": "server-r", "type": "reasoning", "summary": ["先确认接口契约。", "再跑一轮测试。"]},
            ]}]},
            [{"id": "turn-1", "status": "completed", "items": [
                {"id": "live-r1", "type": "reasoning", "summary": ["先确认接口契约。"]},
                {"id": "live-r2", "type": "reasoning", "summary": ["再跑一轮测试。"]},
            ]}],
        )

        self.assertEqual(["live-r1", "live-r2"], [item["id"] for item in merged["turns"][0]["items"]])

    def test_merge_journal_turns_keeps_a_turn_the_server_history_has_not_caught_up_with(self):
        merged = bridge.merge_journal_turns(
            {"id": "t-1", "turns": []},
            [{"id": "turn-9", "status": "completed", "items": [{"id": "a1", "type": "agentMessage", "text": "刚跑完。"}]}],
        )

        self.assertEqual(["turn-9"], [turn["id"] for turn in merged["turns"]])

    def test_chat_archive_writes_a_project_local_visible_conversation_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            relative = bridge.archive_chat_snapshot(
                workspace,
                resource_kind="requirement",
                resource_key="req-api",
                resource_name="实现 / API: 接口",
                conversation_title="需求拆解 · 实现 API 接口",
                thread_id="thread/one",
                provider="codex",
                phase="planning",
                terminal_status="completed",
                turns=[{
                    "id": "turn-1",
                    "status": "completed",
                    "items": [
                        {"type": "userMessage", "content": [{"type": "text", "text": "请实现接口"}]},
                        {"type": "reasoning", "content": ["不应写入归档的原始内容"], "summary": ["先检查现有桥接逻辑。"]},
                        {"type": "commandExecution", "command": "export TOKEN=secret"},
                        {"type": "agentMessage", "text": "接口已实现。"},
                    ],
                }],
            )

            archive = workspace / relative
            content = archive.read_text(encoding="utf-8")
            mode = archive.stat().st_mode & 0o777

        self.assertEqual(
            "chat/requirements/req-api/需求拆解 · 实现 API 接口--thread-one.md",
            relative.as_posix(),
        )
        self.assertIn('threadId: "thread/one"', content)
        self.assertIn("### 用户\n\n请实现接口", content)
        self.assertIn("### 推理摘要\n\n先检查现有桥接逻辑。", content)
        self.assertIn("### 助手\n\n接口已实现。", content)
        self.assertNotIn("不应写入归档的原始内容", content)
        self.assertNotIn("TOKEN=secret", content)
        self.assertIn("<!-- delivery-task-planner-chat-data", content)
        self.assertEqual(0o600, mode)

    def test_terminal_chat_archive_reads_the_full_thread_and_replaces_the_same_file(self):
        class Client:
            def __init__(self):
                self.request_id = 0
                self.turns = [{"id": "turn-1", "items": [{"type": "userMessage", "content": "第一轮"}]}]

            def next_request_id(self):
                self.request_id += 1
                return self.request_id

            def read_thread(self, thread_id, request_id):
                self.last_thread_id = thread_id
                self.last_request_id = request_id
                return {"turns": self.turns}

        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            executor = bridge.ExecutionBridge(workspace)
            client = Client()
            kwargs = {
                "config": self.runtime_config(), "program_id": 1,
                "resource_kind": "task", "resource_key": "task-api", "resource_name": "实现 API",
                "requirement_key": "req-api",
                "conversation_title": "实现 API", "thread_id": "thread-1", "provider": "claude",
                "phase": "development", "terminal_status": "completed",
            }
            with patch.object(executor, "_project_content_sync_settings", return_value={"gitChatSyncEnabled": True}):
                executor._archive_terminal_chat(client, **kwargs)
                client.turns.append({"id": "turn-2", "items": [{"type": "agentMessage", "text": "第二轮完成"}]})
                executor._archive_terminal_chat(client, **kwargs)
            archives = list((workspace / "chat").rglob("*.md"))
            archive_relative = archives[0].relative_to(workspace)
            content = archives[0].read_text(encoding="utf-8")

        self.assertEqual("thread-1", client.last_thread_id)
        self.assertEqual(1, len(archives))
        self.assertEqual("chat/requirements/req-api/task/实现 API--thread-1.md", archive_relative.as_posix())
        self.assertIn('requirementKey: "req-api"', content)
        self.assertIn("第一轮", content)
        self.assertIn("第二轮完成", content)

    def test_workspace_chat_archive_restores_a_thread_when_the_executor_is_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            bridge.archive_chat_snapshot(
                workspace,
                resource_kind="task", resource_key="task-api", resource_name="实现 API",
                requirement_key="req-api",
                conversation_title="实现 API", thread_id="thread-remote", provider="codex",
                phase="development", terminal_status="completed",
                turns=[{"id": "turn-1", "status": "completed", "items": [
                    {"type": "userMessage", "content": "实现接口"},
                    {"type": "agentMessage", "text": "实现完成。", "phase": "final_answer"},
                ]}],
            )

            class Client:
                def next_request_id(self):
                    return 1

                def read_thread(self, _thread_id, request_id=None):
                    raise bridge.BridgeFailure("thread not found")

            executor = bridge.ExecutionBridge(workspace)
            with patch.object(executor, "_project_chat_archive_enabled", return_value=True):
                restored = executor._read_thread_with_workspace_archive(
                    Client(), "thread-remote", "task", "task-api", self.runtime_config(), 1,
                )

        self.assertEqual("workspaceArchive", restored["source"])
        self.assertEqual("实现接口", bridge.serialize_turns(restored["turns"])[0]["items"][0]["text"])
        self.assertEqual("实现完成。", bridge.serialize_turns(restored["turns"])[0]["items"][1]["text"])

    def test_workspace_archive_does_not_override_available_local_executor_history(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            bridge.archive_chat_snapshot(
                workspace,
                resource_kind="requirement", resource_key="req-a", resource_name="旧需求",
                conversation_title="旧会话", thread_id="thread-1", provider="claude",
                phase="planning", terminal_status="completed",
                turns=[{"items": [{"type": "agentMessage", "text": "归档内容"}]}],
            )

            class Client:
                def next_request_id(self):
                    return 1

                def read_thread(self, _thread_id, request_id=None):
                    return {"turns": [{"items": [{"type": "agentMessage", "text": "本机内容"}]}]}

            result = bridge.ExecutionBridge(workspace)._read_thread_with_workspace_archive(
                Client(), "thread-1", "requirement", "req-a", self.runtime_config(), 1,
            )

        self.assertNotIn("source", result)
        self.assertEqual("本机内容", result["turns"][0]["items"][0]["text"])

    def test_project_chat_sync_switch_disables_workspace_chat_archive_and_fallback(self):
        class Client:
            def __init__(self):
                self.read_count = 0

            def next_request_id(self):
                return 1

            def read_thread(self, _thread_id, request_id=None):
                self.read_count += 1
                raise bridge.BridgeFailure("thread not found")

        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            bridge.archive_chat_snapshot(
                workspace,
                resource_kind="task", resource_key="task-api", resource_name="实现 API",
                requirement_key="req-api",
                conversation_title="实现 API", thread_id="thread-remote", provider="codex",
                phase="development", terminal_status="completed",
                turns=[{"items": [{"type": "agentMessage", "text": "本地归档内容"}]}],
            )
            executor = bridge.ExecutionBridge(workspace)
            client = Client()
            with patch.object(bridge.planner, "request_api", return_value={"gitEnabled": True, "gitChatSyncEnabled": False}):
                restored = executor._read_thread_with_workspace_archive(
                    client, "thread-remote", "task", "task-api", self.runtime_config(), 1,
                )

            self.assertEqual(1, client.read_count)
            self.assertEqual([], restored.get("turns", []))

            with patch.object(bridge.planner, "request_api", return_value={"gitEnabled": True, "gitChatSyncEnabled": False}):
                executor._archive_terminal_chat(
                    client,
                    config=self.runtime_config(), program_id=1,
                    resource_kind="task", resource_key="new-task", resource_name="新任务",
                    conversation_title="新任务", thread_id="thread-new", provider="codex",
                    phase="development", terminal_status="completed",
                )
            self.assertFalse((workspace / "chat" / "任务" / "new-task--新任务").exists())

    def test_cloud_sync_categories_only_include_selected_project_files(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / "chat" / "requirements" / "req-api" / "task").mkdir(parents=True)
            (workspace / "chat" / "requirements" / "req-api" / "task" / "聊天.md").write_text("chat", encoding="utf-8")
            (workspace / "doc" / "core" / "task-a").mkdir(parents=True)
            (workspace / "doc" / "core" / "task-a" / "文档.md").write_text("requirement", encoding="utf-8")
            (workspace / "doc" / "core" / "task-a" / "prototype").mkdir()
            (workspace / "doc" / "core" / "task-a" / "prototype" / "index.html").write_text("<main/>", encoding="utf-8")
            entries, skipped = bridge.cloud_sync_workspace_entries(workspace, {"chat", "design"})

        self.assertEqual(0, skipped)
        self.assertEqual(
            [("chat", "chat/requirements/req-api/task/聊天.md"), ("design", "doc/core/task-a/prototype/index.html")],
            [(category, relative) for category, relative, _source, _content_type in entries],
        )

    def test_cloud_chat_sync_does_not_require_git_or_create_a_workspace_archive(self):
        class Client:
            def next_request_id(self):
                return 1

            def read_thread(self, _thread_id, request_id=None):
                return {"turns": [{"items": [{"type": "agentMessage", "text": "已完成"}]}]}

        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            executor = bridge.ExecutionBridge(workspace)
            uploaded = []
            with (
                patch.object(executor, "_project_content_sync_settings", return_value={"gitEnabled": False, "gitChatSyncEnabled": False, "cloudSyncEnabled": True, "cloudSyncScopes": ["chat"]}),
                patch.object(executor, "_upload_cloud_sync_file", side_effect=lambda *_args: uploaded.append(_args[3])),
            ):
                executor._archive_terminal_chat(
                    Client(), config=self.runtime_config(), program_id=1,
                    resource_kind="task", resource_key="task-a", resource_name="实现 API",
                    requirement_key="req-api",
                    conversation_title="实现 API", thread_id="thread-1", provider="codex",
                    phase="development", terminal_status="completed",
                )

            self.assertEqual(1, len(uploaded))
            self.assertEqual("chat/requirements/req-api/task/实现 API--thread-1.md", uploaded[0])
            self.assertFalse((workspace / "chat").exists())

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

    def test_final_markdown_link_with_bare_file_name_becomes_workspace_artifact(self):
        """回复里常常只写文件名，按工作区根目录拼不出路径，得靠全库反查。"""
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            source = workspace / "app" / "src" / "ShenShiAccessibilityService.kt"
            source.parent.mkdir(parents=True)
            source.write_text("class ShenShiAccessibilityService", encoding="utf-8")
            subprocess.run(["git", "init", "-q", str(workspace)], check=True)
            store = bridge.WorkspaceArtifactStore(workspace)

            turns = bridge.serialize_turns(
                [{"items": [{
                    "id": "answer-1",
                    "type": "agentMessage",
                    "phase": "final_answer",
                    "text": "实现见 [ShenShiAccessibilityService.kt](ShenShiAccessibilityService.kt:42)。",
                }]}],
                artifact_resolver=lambda paths: store.register("whatsapp", 1, "a", paths),
            )

            attachment = turns[0]["items"][0]["attachments"][0]
            self.assertEqual("ShenShiAccessibilityService.kt", attachment["name"])
            self.assertEqual("app/src/ShenShiAccessibilityService.kt", attachment["relativePath"])

    def test_bare_file_name_lookup_skips_duplicates_and_external_links(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            for module in ("one", "two"):
                target = workspace / module / "readme.md"
                target.parent.mkdir(parents=True)
                target.write_text(module, encoding="utf-8")
            subprocess.run(["git", "init", "-q", str(workspace)], check=True)
            store = bridge.WorkspaceArtifactStore(workspace)

            # 同名两份：认不出是哪一份就不给预览。带完整目录的写法仍然认得出。
            self.assertEqual([], store.register("whatsapp", 1, "a", ["readme.md"]))
            self.assertEqual(
                ["one/readme.md"],
                [item["relativePath"] for item in store.register("whatsapp", 1, "a", ["one/readme.md"])],
            )
            # 站外地址不能靠文件名蒙到工作区里的同名文件上。
            self.assertEqual([], store.register("whatsapp", 1, "a", ["https://example.com/one/readme.md"]))

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

    def test_requirement_name_keeps_only_the_title_line(self):
        self.assertEqual("交付面板需求自动命名", bridge.requirement_name_of("需求标题：交付面板需求自动命名"))
        self.assertEqual("优化登录超时处理", bridge.requirement_name_of("**优化登录超时处理**\n\n如需调整请告诉我"))
        self.assertEqual("支持需求名称留空", bridge.requirement_name_of("好的，标题如下：\n「支持需求名称留空」"))
        self.assertEqual("支持新建需求不填名称", bridge.requirement_name_of("## 1. 支持新建需求不填名称。"))
        self.assertEqual("", bridge.requirement_name_of("   \n\n"))
        self.assertEqual(bridge.MAX_REQUIREMENT_NAME_CHARS, len(bridge.requirement_name_of("标" * 80)))

    def test_placeholder_requirement_name_takes_the_first_ten_characters(self):
        """起名要跑一轮模型，这几秒里先用首条消息占位，面板上才不会只显示需求编号。"""
        self.assertEqual("手机神识助手识别Wh", bridge.placeholder_requirement_name("手机神识助手识别WhatsApp界面，现在有哪些问题"))
        self.assertEqual(bridge.MAX_REQUIREMENT_PLACEHOLDER_CHARS, len(bridge.placeholder_requirement_name("需" * 80)))
        # Markdown 记号和多余空白不该占掉这十个字。
        self.assertEqual("帮我梳理一下", bridge.placeholder_requirement_name("  ## 帮我梳理一下  "))
        self.assertEqual("", bridge.placeholder_requirement_name("   \n\n"))

    def test_requirement_name_prompt_only_asks_for_a_title(self):
        prompt = bridge.build_requirement_name_prompt("把需求名称做成可以不填", "我建议拆成三条任务……")

        self.assertIn("聊天自动命名", prompt)
        self.assertIn("把需求名称做成可以不填", prompt)
        self.assertIn("我建议拆成三条任务", prompt)
        self.assertIn(f"不超过 {bridge.MAX_REQUIREMENT_NAME_CHARS} 个字", prompt)
        self.assertIn("不要读代码", prompt)

    def test_conversation_title_accepts_task_prefix_and_removes_attachment_context(self):
        self.assertEqual("修复任务会话自动命名", bridge.conversation_title_of("任务标题：修复任务会话自动命名"))
        self.assertEqual(
            "请根据第一条说明命名",
            bridge.text_without_attachment_context(
                "请根据第一条说明命名\n<delivery-task-attachments>附件说明</delivery-task-attachments>\n"
                "<!-- delivery-task-attachments:abcdefghijklmnop -->"
            ),
        )

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

    def test_conversation_metadata_keeps_ai_title_on_follow_up(self):
        initial = bridge.conversation_metadata(None, "thr_first", "turn_first", "running", "默认任务标题")
        updated = bridge.conversation_metadata(
            {"metadata": initial}, "thr_first", "turn_second", "completed", "首条需求自动生成的标题",
        )
        followed_up = bridge.conversation_metadata(
            {"metadata": updated}, "thr_first", "turn_third", "completed",
        )

        entry = next(value for value in followed_up["conversations"] if value["threadId"] == "thr_first")
        self.assertEqual("首条需求自动生成的标题", entry["title"])

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
            {"kind": "file", "key": "doc/requirements/req-a/需求大纲.md", "scope": "requirement-outline"},
            {"kind": "file", "key": "../secret", "scope": "requirement-outline"},
            {"kind": "file", "key": "doc/requirements/req-a/prototype/index.html", "scope": "unknown"},
        ])

        self.assertEqual(
            [
                {"kind": "requirement", "key": "req-a"},
                {"kind": "task", "key": "task.v1"},
                {
                    "kind": "file",
                    "key": "doc/requirements/req-a/需求大纲.md",
                    "scope": "requirement-outline",
                },
            ],
            references,
        )

    def test_conversation_mention_context_accepts_current_requirement_prototype_file(self):
        executor = bridge.ExecutionBridge(Path.cwd())
        prototype = [{
            "path": "doc/requirements/req-a/prototype/index.html",
            "name": "index.html",
        }]
        with patch.object(bridge, "requirement_prototype_files", return_value=("doc/requirements/req-a/prototype", prototype)):
            lines = executor._conversation_mention_context(
                {"api_url": "http://test/api", "key": "k"},
                1,
                [{
                    "kind": "file",
                    "key": "doc/requirements/req-a/prototype/index.html",
                    "scope": "requirement-prototype",
                }],
                {"items": []},
                "req-a",
            )

        rendered = "\n".join(lines)
        self.assertIn("@文件 index.html", rendered)
        self.assertIn("doc/requirements/req-a/prototype/index.html", rendered)

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

    def test_conversation_naming_runs_at_the_start_of_a_new_chat(self):
        # 名称留空的需求，开聊时就把标题定下来：会话目录和需求名称都不用等整轮拆解跑完。
        # 先落首条消息的前十个字占位，AI 起好名再把占位名换掉。
        executor = bridge.ExecutionBridge(Path.cwd())
        session = {"threadId": "thread-1", "catalog": [{"threadId": "thread-1", "title": "需求拆解 · req-a"}]}
        record = {"requirementKey": "req-a", "name": ""}
        calls = []

        def write(*args, **kwargs):
            calls.append((args, kwargs))
            record["name"] = kwargs["body"]["name"]
            return {}

        with (
            patch.object(executor, "_name_conversation", return_value="确认手机端发消息实现"),
            patch.object(executor, "_save_planning_session"),
            patch.object(bridge.planner, "requirement_record", side_effect=lambda *args, **kwargs: dict(record)),
            patch.object(bridge.planner, "request_api", side_effect=write),
        ):
            namer, outcome = executor._start_conversation_naming(
                ("whatsapp", 2, "req-a"), {"_project_id": 2}, 2, "req-a", "codex", "", False,
                "当前手机端是怎么发消息的", session, "thread-1",
            )
            namer.join(10)

        self.assertEqual("确认手机端发消息实现", outcome.get("title"))
        self.assertEqual("确认手机端发消息实现", session["catalog"][0]["title"])
        self.assertEqual(2, len(calls))
        self.assertEqual("/delivery/requirement/name/update", calls[0][0][2])
        # 第一次是占位名，写空名称；第二次拿占位名换成 AI 起的标题。
        self.assertEqual(("当前手机端是怎么发消", ""), (calls[0][1]["body"]["name"], calls[0][1]["body"]["replaceName"]))
        self.assertEqual(("确认手机端发消息实现", "当前手机端是怎么发消"), (calls[1][1]["body"]["name"], calls[1][1]["body"]["replaceName"]))

    def test_conversation_naming_retitles_a_named_requirement_on_its_first_chat(self):
        """编辑进来的需求也一样：一次都没聊过时，首轮的问题要重新定标题。

        这种需求已经有用户填的名字，所以不写占位名 —— AI 标题落库之前，面板上留着原名。
        """
        executor = bridge.ExecutionBridge(Path.cwd())
        session = {"threadId": "thread-1", "catalog": [{"threadId": "thread-1", "title": "需求拆解 · 手填的名字"}]}
        record = {"requirementKey": "req-a", "name": "手填的名字"}
        calls = []

        def write(*args, **kwargs):
            calls.append((args, kwargs))
            record["name"] = kwargs["body"]["name"]
            return {}

        with (
            patch.object(executor, "_name_conversation", return_value="确认手机端发消息实现"),
            patch.object(executor, "_save_planning_session"),
            patch.object(bridge.planner, "requirement_record", side_effect=lambda *args, **kwargs: dict(record)),
            patch.object(bridge.planner, "request_api", side_effect=write),
        ):
            namer, outcome = executor._start_conversation_naming(
                ("whatsapp", 2, "req-a"), {"_project_id": 2}, 2, "req-a", "codex", "", False,
                "当前手机端是怎么发消息的", session, "thread-1", True,
            )
            namer.join(10)

        self.assertEqual("确认手机端发消息实现", outcome.get("title"))
        self.assertEqual("确认手机端发消息实现", session["catalog"][0]["title"])
        # 只写一次：没有占位名这一步，直接拿手填的名字换成 AI 起的标题。
        self.assertEqual(1, len(calls))
        self.assertEqual(("确认手机端发消息实现", "手填的名字"), (calls[0][1]["body"]["name"], calls[0][1]["body"]["replaceName"]))

    def test_conversation_naming_keeps_a_named_requirement_on_later_chats(self):
        """已经聊过的需求再开新会话：只换会话标题，需求名称不动。"""
        executor = bridge.ExecutionBridge(Path.cwd())
        session = {"threadId": "thread-2", "catalog": [{"threadId": "thread-2", "title": "需求拆解 · 手填的名字"}]}
        record = {"requirementKey": "req-a", "name": "手填的名字"}

        with (
            patch.object(executor, "_name_conversation", return_value="补充导出能力"),
            patch.object(executor, "_save_planning_session"),
            patch.object(bridge.planner, "requirement_record", side_effect=lambda *args, **kwargs: dict(record)),
            patch.object(bridge.planner, "request_api") as request_api,
        ):
            namer, _ = executor._start_conversation_naming(
                ("whatsapp", 2, "req-a"), {"_project_id": 2}, 2, "req-a", "codex", "", False,
                "再补一个导出", session, "thread-2",
            )
            namer.join(10)

        self.assertEqual("补充导出能力", session["catalog"][0]["title"])
        request_api.assert_not_called()

    def test_conversation_naming_keeps_a_requirement_name_the_user_already_filled_in(self):
        executor = bridge.ExecutionBridge(Path.cwd())
        with (
            patch.object(bridge.planner, "requirement_record", return_value={"requirementKey": "req-a", "name": "用户自己起的名字"}),
            patch.object(bridge.planner, "request_api") as request_api,
        ):
            executor._write_requirement_name({"_project_id": 2}, 2, "req-a", "AI 起的名字")

        request_api.assert_not_called()

    def test_conversation_naming_does_not_replace_a_name_that_is_no_longer_the_placeholder(self):
        """占位名写下去之后用户自己改了名字：AI 的标题这一轮作废，不能盖回去。"""
        executor = bridge.ExecutionBridge(Path.cwd())
        with (
            patch.object(bridge.planner, "requirement_record", return_value={"requirementKey": "req-a", "name": "用户自己起的名字"}),
            patch.object(bridge.planner, "request_api") as request_api,
        ):
            executor._write_requirement_name({"_project_id": 2}, 2, "req-a", "AI 起的名字", "当前手机端是怎么发消")

        request_api.assert_not_called()

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

    def test_stop_reports_a_turn_that_already_finished(self):
        """Codex 说没有可中断的回合时，停止不该报错，只是这一下点晚了。"""
        executor = bridge.ExecutionBridge(Path.cwd())
        identity = bridge.task_identity("whatsapp", 1, "task-a")

        class FinishedClient:
            def interrupt_turn(self, _thread_id, _turn_id, request_id=13):
                raise RuntimeError("no active turn to interrupt")

            def next_request_id(self):
                return 1

        executor.active.add(identity)
        executor.active_runs[identity] = {
            "client": FinishedClient(), "threadId": "thread-a", "turnId": "turn-a",
            "task": {"itemKey": "task-a"}, "config": self.runtime_config(), "provider": "codex",
        }

        result = executor.stop_conversation(
            {"bizLine": "whatsapp", "programId": 1, "itemKey": "task-a"},
            config=self.runtime_config(),
        )

        self.assertTrue(result["accepted"])
        self.assertTrue(result["alreadyFinished"])

    def test_stop_all_cancels_the_rest_of_a_running_batch(self):
        """点了「全部停止」之后，批量队列里还没启动的任务不能再被拉起来。"""
        statuses = {"a": "todo", "b": "todo"}
        dependencies = {"a": [], "b": ["a"]}
        started: list[str] = []
        finalized: dict[str, str] = {}
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
            started.append(item_key)
            statuses[item_key] = "done"
            # 第一条刚跑起来，用户在任务进度里点了全部停止。
            executor.stop_all_executions({"programId": 1}, config=self.runtime_config())
            return {"accepted": True}

        def finalize(_config, _program_id, _batch_id, status, summary, _provider="codex"):
            finalized.update({"status": status, "summary": summary})

        with (
            patch.object(bridge.planner, "project_context", side_effect=project_context),
            patch.object(executor, "_task_detail", side_effect=task_detail),
            patch.object(executor, "execute", side_effect=execute),
            patch.object(executor, "_update_execution_batch_item", return_value=None),
            patch.object(executor, "_finalize_execution_batch", side_effect=finalize),
        ):
            executor._run_batch("batch-stop", self.runtime_config(), 1, ["a", "b"], "")

        self.assertEqual(["a"], started)
        self.assertEqual("blocked", finalized["status"])
        self.assertIn("停止", finalized["summary"])
        # 队列结束后取消标记要清掉，同一个批次号不会永远停在取消态。
        self.assertNotIn("batch-stop", executor.cancelled_queues)

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

    def test_execute_batch_refuses_completed_tasks_without_redo(self):
        context = {"items": [{"itemKey": "a", "status": "done", "dependsOnItemKeys": []}]}
        executor = bridge.ExecutionBridge(Path.cwd())
        with (
            patch.object(bridge.planner, "project_context", return_value=context),
            patch.object(bridge.threading, "Thread"),
        ):
            with self.assertRaises(bridge.BridgeFailure):
                executor.execute_batch({"bizLine": "whatsapp", "programId": 1, "itemKeys": ["a"]}, config=self.runtime_config())

    def test_execute_batch_redo_reruns_completed_tasks(self):
        context = {
            "items": [
                {"itemKey": "a", "status": "done", "dependsOnItemKeys": []},
                {"itemKey": "b", "status": "done", "dependsOnItemKeys": ["a"]},
            ],
        }
        executor = bridge.ExecutionBridge(Path.cwd())
        with (
            patch.object(bridge.planner, "project_context", return_value=context),
            patch.object(bridge.threading, "Thread") as thread,
        ):
            result = executor.execute_batch(
                {"bizLine": "whatsapp", "programId": 1, "itemKeys": ["a", "b"], "redo": True},
                config=self.runtime_config(),
            )

        self.assertTrue(result["accepted"])
        self.assertEqual(["a", "b"], result["itemKeys"])
        # 服务端要知道这是「再做一次」，否则它会挡下已完成任务。
        self.assertTrue(self.execution_batches[-1]["redo"])
        # 重跑用的是新的执行实例，任务状态本身不回滚。
        self.assertTrue(thread.call_args.kwargs["args"][-1])
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
        self.assertIn("`doc/api/a/`，支持多份文档", prompt)
        self.assertIn("`doc/api/a/design/`，支持多份文档", prompt)

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
            1, {"program": {"name": "Universe"}}, "确认并写入",
            requirement={"requirementKey": "req-a"}, write_allowed=True, thread_id="thread-1",
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
        self.assertIn("该目录支持多份文档", prompt)

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

    def test_requirement_testing_history_excludes_review_sessions(self):
        """review 和测试共用一张会话表，两边的目录都只能看见自己那一类。"""
        rows = [
            {"threadId": "testing-thread", "title": "总体测试", "executorType": "codex", "metadata": {"kind": "requirement-testing"}},
            {"threadId": "review-thread", "title": "代码 review", "executorType": "codex", "metadata": {"kind": "requirement-review"}},
            {"threadId": "legacy-thread", "title": "老数据", "executorType": "codex", "metadata": {}},
        ]

        with tempfile.TemporaryDirectory() as directory:
            executor = bridge.ExecutionBridge(Path(directory))
            with patch.object(bridge.planner, "request_api", return_value=rows):
                testing = executor._load_requirement_testing_session(self.runtime_config(), 1, "req-a", "codex")
                review = executor._load_requirement_review_session(self.runtime_config(), 1, "req-a", "codex")

        self.assertEqual(
            ["testing-thread", "legacy-thread"],
            [entry["threadId"] for entry in testing["catalog"]],
        )
        # 不带 threadId 打开时落到最后一条，不能落到 review 那条上。
        self.assertEqual("legacy-thread", testing["threadId"])
        self.assertEqual(["review-thread"], [entry["threadId"] for entry in review["catalog"]])

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
        task_binding = {
            "executorType": "codex", "phase": "development", "externalSessionId": "task-thread",
            "status": "completed", "metadata": {
                "conversations": [{"threadId": "task-thread", "title": "Create API", "status": "completed"}],
            },
        }
        requests = []

        def request_api(_config, method, path, query=None, body=None):
            requests.append((method, path, query, body))
            if path == "/delivery/item" and method == "GET":
                return task
            if path == "/delivery/item/execution-session" and method == "GET":
                return [task_binding, binding]
            if path == "/delivery/program" and method == "GET":
                return {"gitEnabled": False}
            self.fail(f"unexpected request: {method} {path}")

        with (
            patch.object(bridge.planner, "request_api", side_effect=request_api),
            patch.object(bridge, "create_ai_client", return_value=client),
        ):
            result = executor.task_testing_cases_conversation(1, "api-1", config=self.runtime_config())

        self.assertEqual("cases-thread", result["threadId"])
        self.assertEqual("Create API · 测试用例", result["conversations"][0]["title"])
        self.assertEqual("ready", result["testingCasesStatus"])
        # 目录不再按执行器过滤（换工具也要看得见旧会话），但任务执行会话不能混进用例列表。
        query = next(request for request in requests if request[1] == "/delivery/item/execution-session")[2]
        self.assertNotIn("executorType", query)
        self.assertEqual(["cases-thread"], [entry["threadId"] for entry in result["conversations"]])

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
                {"doc/api/a/文档.md", "doc/api/a/接口设计.md", "doc/api/a/截图.png"},
                {entry["path"] for entry in result["files"]},
            )
            # 非文本文件同样属于这个栏目，只是面板不当正文读，而是走附件预览与下载。
            self.assertEqual(
                {"doc/api/a/文档.md": True, "doc/api/a/接口设计.md": True, "doc/api/a/截图.png": False},
                {entry["path"]: entry["previewable"] for entry in result["files"]},
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

    def test_requirement_outline_column_lists_standalone_html_assets(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            column = workspace / "doc/requirements/req-a"
            column.mkdir(parents=True)
            (column / "需求大纲.md").write_text("# 大纲\n", encoding="utf-8")
            (column / "流程图.html").write_text("<!doctype html><title>流程</title>", encoding="utf-8")
            executor = bridge.ExecutionBridge(workspace)
            with patch.object(executor, "_requirement_for_prototype", return_value={"requirementKey": "req-a"}):
                result = executor.document_set(1, "requirement-outline", "req-a", config=self.runtime_config())

            self.assertIn("doc/requirements/req-a/流程图.html", {entry["path"] for entry in result["files"]})

    def test_document_sets_support_multiple_design_and_requirement_testing_documents(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            design = workspace / "doc/api/a/design"
            design.mkdir(parents=True)
            (design / "接口.md").write_text("# 接口\n", encoding="utf-8")
            (design / "流程.html").write_text("<h1>流程</h1>", encoding="utf-8")
            requirement_testing = workspace / "doc/test/req-a"
            requirement_testing.mkdir(parents=True)
            (requirement_testing / "测试用例.md").write_text("# 用例\n", encoding="utf-8")
            (requirement_testing / "补充场景.md").write_text("# 场景\n", encoding="utf-8")
            executor = bridge.ExecutionBridge(workspace)
            with patch.object(executor, "_task_detail", return_value={"requirementDocumentPath": "doc/api/a/文档.md"}):
                design_result = executor.document_set(1, "task-design", "a", config=self.runtime_config())
            with patch.object(executor, "_requirement_for_prototype", return_value={"requirementKey": "req-a"}):
                testing_result = executor.document_set(1, "requirement-testing", "req-a", config=self.runtime_config())

            self.assertEqual(
                {"doc/api/a/design/接口.md", "doc/api/a/design/流程.html"},
                {entry["path"] for entry in design_result["files"]},
            )
            self.assertEqual(
                {"doc/test/req-a/测试用例.md", "doc/test/req-a/补充场景.md"},
                {entry["path"] for entry in testing_result["files"]},
            )

    def test_multipart_bodies_split_into_plain_fields_and_uploaded_files(self):
        boundary = "----test-boundary"
        body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="scope"\r\n\r\n'
            "requirement-outline\r\n"
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="files"; filename="参考资料.pdf"\r\n'
            "Content-Type: application/pdf\r\n\r\n"
            "%PDF-1.4\r\n"
            f"--{boundary}--\r\n"
        ).encode("utf-8")
        handler = object.__new__(bridge.BridgeHandler)
        handler.headers = Message()
        handler.headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        handler.rfile = io.BytesIO(body)

        fields, uploads = handler.read_multipart(len(body))

        self.assertEqual({"scope": "requirement-outline"}, fields)
        self.assertEqual(1, len(uploads))
        self.assertEqual("参考资料.pdf", uploads[0]["name"])
        self.assertEqual("application/pdf", uploads[0]["contentType"])
        self.assertEqual(b"%PDF-1.4", uploads[0]["data"])

    def test_document_set_default_selection_skips_files_that_cannot_be_previewed(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            column = workspace / "doc/requirements/req-a"
            column.mkdir(parents=True)
            (column / "参考资料.pdf").write_bytes(b"%PDF-1.4")
            (column / "补充说明.md").write_text("# 补充\n", encoding="utf-8")
            executor = bridge.ExecutionBridge(workspace)
            with patch.object(executor, "_requirement_for_prototype", return_value={"requirementKey": "req-a"}):
                result = executor.document_set(1, "requirement-outline", "req-a", config=self.runtime_config())

            self.assertEqual("doc/requirements/req-a/补充说明.md", result["primaryPath"])

    def test_uploaded_documents_land_in_the_requirement_directory_without_overwriting(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            column = workspace / "doc/requirements/req-a"
            column.mkdir(parents=True)
            (column / "需求大纲.md").write_text("# 大纲\n", encoding="utf-8")
            (column / "参考资料.pdf").write_bytes(b"%PDF-old")
            executor = bridge.ExecutionBridge(workspace)
            with patch.object(executor, "_requirement_for_prototype", return_value={"requirementKey": "req-a"}):
                result = executor.upload_documents(
                    1,
                    "requirement-outline",
                    "req-a",
                    [
                        {"name": "参考资料.pdf", "contentType": "application/pdf", "data": b"%PDF-new"},
                        {"name": "粘贴的说明.md", "contentType": "text/markdown", "data": "# 说明\n".encode("utf-8")},
                    ],
                    config=self.runtime_config(),
                )

            self.assertEqual(
                ["doc/requirements/req-a/参考资料-2.pdf", "doc/requirements/req-a/粘贴的说明.md"],
                result["uploaded"],
            )
            self.assertEqual("doc/requirements/req-a/参考资料-2.pdf", result["primaryPath"])
            self.assertEqual(b"%PDF-old", (column / "参考资料.pdf").read_bytes())
            self.assertEqual(b"%PDF-new", (column / "参考资料-2.pdf").read_bytes())
            self.assertEqual("# 说明\n", (column / "粘贴的说明.md").read_text(encoding="utf-8"))

    def test_uploaded_document_names_cannot_escape_the_column_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / "doc/requirements/req-a").mkdir(parents=True)
            executor = bridge.ExecutionBridge(workspace)
            with patch.object(executor, "_requirement_for_prototype", return_value={"requirementKey": "req-a"}):
                result = executor.upload_documents(
                    1,
                    "requirement-outline",
                    "req-a",
                    [{"name": "../../逃逸.md", "contentType": "text/markdown", "data": b"x"}],
                    config=self.runtime_config(),
                )

            self.assertEqual(["doc/requirements/req-a/逃逸.md"], result["uploaded"])
            self.assertFalse((workspace / "逃逸.md").exists())

    def test_document_attachment_registers_a_non_text_document_for_download(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            column = workspace / "doc/requirements/req-a"
            column.mkdir(parents=True)
            (column / "参考资料.pdf").write_bytes(b"%PDF-1.4 body")
            executor = bridge.ExecutionBridge(workspace)
            with patch.object(executor, "_requirement_for_prototype", return_value={"requirementKey": "req-a"}):
                attachment = executor.document_attachment(
                    1, "requirement-outline", "req-a", "doc/requirements/req-a/参考资料.pdf",
                    config=self.runtime_config(),
                )

            self.assertEqual("参考资料.pdf", attachment["name"])
            self.assertEqual("application/pdf", attachment["contentType"])
            self.assertTrue(attachment["url"].startswith("/v1/codex/artifacts/"))

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
        client.read_turn.return_value = {
            "items": [{"type": "commandExecution", "command": "go build ./..."}, {"type": "agentMessage", "text": "done"}],
        }
        order = []
        client.close.side_effect = lambda: order.append("close")
        executor.progress.publish = unittest.mock.MagicMock(side_effect=lambda *_args: order.append("publish"))

        with (
            patch.object(executor, "_sync_result", side_effect=lambda *_args: order.append("sync")),
            patch.object(executor, "_archive_terminal_chat") as archive,
        ):
            executor._follow(
                ("whatsapp", 1, "a"), client, {}, 1, "a",
                {"phase": "development"}, {"version": 2}, "turn-1",
            )

        self.assertEqual(["sync", "close", "publish", "close"], order)
        archive.assert_called_once()

    def test_corrupted_turn_is_detected_when_a_working_phase_calls_no_tool(self):
        turn = {"items": [{"type": "agentMessage", "text": "我已经完成了改造。"}]}

        self.assertTrue(bridge.corrupted_turn_reason("completed", turn, "development"))
        self.assertTrue(bridge.corrupted_turn_reason("completed", turn, "testing"))
        # 梳理需求只写文档，没有命令执行是正常的，不能按同一条判定。
        self.assertEqual("", bridge.corrupted_turn_reason("completed", turn, "requirement"))
        # 非正常结束走既有的失败路径，这里不重复判定。
        self.assertEqual("", bridge.corrupted_turn_reason("interrupted", turn, "development"))

    def test_corrupted_turn_accepts_a_round_that_actually_touched_the_workspace(self):
        for item_type in ("commandExecution", "fileChange", "fileEdit", "mcpToolCall", "dynamicToolCall"):
            turn = {"items": [{"type": item_type}, {"type": "agentMessage", "text": "完成"}]}
            self.assertEqual("", bridge.corrupted_turn_reason("completed", turn, "development"), item_type)

    def test_corrupted_turn_catches_a_leaked_tool_schema_even_in_the_grooming_phase(self):
        # 实测形态：调用没发成调用帧，schema 和超时毫秒数被写进正文，开头的 T 还被吃掉了。
        leaked = '先读技能文件。ARGET_TOOL_SCHEMA={"type":"function","description":"Executor tool"}"120000'
        turn = {"items": [{"type": "commandExecution"}, {"type": "agentMessage", "text": leaked}]}

        self.assertTrue(bridge.corrupted_turn_reason("completed", turn, "requirement"))
        self.assertTrue(bridge.corrupted_turn_reason("completed", turn, "development"))
        full = turn["items"][1]["text"].replace("ARGET_TOOL_SCHEMA", "TARGET_TOOL_SCHEMA")
        turn["items"][1]["text"] = full
        self.assertTrue(bridge.corrupted_turn_reason("completed", turn, "development"))

    def test_corrupted_turn_is_retried_once_with_the_same_prompt(self):
        executor = bridge.ExecutionBridge(Path.cwd())
        client = unittest.mock.MagicMock()
        client.thread_id = "thread-1"
        client.next_request_id.return_value = 11
        client.start_turn.return_value = "turn-2"
        client.wait_turn.return_value = "completed"
        client.read_turn.return_value = {
            "items": [{"type": "commandExecution", "command": "pwd"}, {"type": "agentMessage", "text": "已改完"}],
        }
        executor.progress.publish = unittest.mock.MagicMock()

        turn_id, status, turn, reason = executor._retry_corrupted_turn(
            ("whatsapp", 1, "a"), client, "turn-1", "completed",
            {"items": [{"type": "agentMessage", "text": "我没有可用的工具"}]},
            "development", "原始提示词", model="gpt-5.6-terra", reasoning_effort="xhigh",
        )

        self.assertEqual(("turn-2", "completed", ""), (turn_id, status, reason))
        self.assertEqual("commandExecution", turn["items"][0]["type"])
        client.start_turn.assert_called_once()
        self.assertEqual("原始提示词", client.start_turn.call_args.args[1])
        self.assertEqual("xhigh", client.start_turn.call_args.kwargs["reasoning_effort"])

    def test_corrupted_turn_fails_the_round_when_the_retry_is_also_toolless(self):
        executor = bridge.ExecutionBridge(Path.cwd())
        client = unittest.mock.MagicMock()
        client.thread_id = "thread-1"
        client.next_request_id.return_value = 11
        client.start_turn.return_value = "turn-2"
        client.wait_turn.return_value = "completed"
        client.read_turn.return_value = {"items": [{"type": "agentMessage", "text": "本会话没有暴露工具"}]}
        executor.progress.publish = unittest.mock.MagicMock()

        turn_id, status, _turn, reason = executor._retry_corrupted_turn(
            ("whatsapp", 1, "a"), client, "turn-1", "completed",
            {"items": [{"type": "agentMessage", "text": "我没有可用的工具"}]},
            "development", "原始提示词",
        )

        self.assertEqual(("turn-2", "failed"), (turn_id, status))
        self.assertIn("重试后依然无效", reason)

    def test_corrupted_turn_skips_the_retry_when_the_user_already_sent_a_new_round(self):
        executor = bridge.ExecutionBridge(Path.cwd())
        identity = ("whatsapp", 1, "a")
        executor.active_runs[identity] = {"turnId": "turn-9"}
        client = unittest.mock.MagicMock()
        client.thread_id = "thread-1"

        turn_id, status, _turn, reason = executor._retry_corrupted_turn(
            identity, client, "turn-1", "completed",
            {"items": [{"type": "agentMessage", "text": "我没有可用的工具"}]},
            "development", "原始提示词",
        )

        self.assertEqual(("turn-1", "failed"), (turn_id, status))
        self.assertTrue(reason)
        client.start_turn.assert_not_called()

    def test_follow_keeps_an_invalid_round_out_of_the_task_deliverable(self):
        executor = bridge.ExecutionBridge(Path.cwd())
        client = unittest.mock.MagicMock()
        client.thread_id = "thread-1"
        client.wait_turn.return_value = "completed"
        client.read_turn.return_value = {"items": [{"type": "agentMessage", "text": "本会话没有暴露 shell 工具"}]}
        executor.progress.publish = unittest.mock.MagicMock()
        synced = {}

        with (
            patch.object(executor, "_sync_result", side_effect=lambda *args: synced.update(args=args)),
            patch.object(executor, "_archive_terminal_chat"),
        ):
            executor._follow(
                ("whatsapp", 1, "a"), client, {}, 1, "a",
                {"phase": "development"}, {"version": 2}, "turn-1",
            )

        _config, _program, _key, _task, _binding, _turn_id, status, output, _provider, _title, reason = synced["args"]
        self.assertEqual("failed", status)
        self.assertEqual("", output)
        self.assertTrue(reason)

    def test_app_server_stderr_is_kept_for_diagnostics_instead_of_dropped(self):
        client = bridge.AppServerClient.__new__(bridge.AppServerClient)
        client.process = unittest.mock.MagicMock()
        client.process.stderr = iter(["", "mcp startup timed out\n", "model refresh failed\n"])
        client.stderr_lock = threading.Lock()
        client.stderr_lines = collections.deque(maxlen=bridge.APP_SERVER_STDERR_TAIL)

        client._drain_stderr()

        self.assertEqual("mcp startup timed out\nmodel refresh failed", client.stderr_tail())
        self.assertEqual("model refresh failed", client.stderr_tail(limit=1))

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


    @staticmethod
    def panel_token(subject: int = 4) -> str:
        payload = base64.urlsafe_b64encode(
            json.dumps({"sub": subject, "ver": 2}).encode("utf-8")
        ).decode("ascii").rstrip("=")
        return f"header.{payload}.signature"

    @staticmethod
    def heartbeat_handler(token: str, body: dict) -> tuple[list, dict]:
        raw = json.dumps(body).encode("utf-8")
        handler = object.__new__(bridge.BridgeHandler)
        handler.server = SimpleNamespace(allowed_origins={"*"})
        handler.headers = {"Origin": "http://console.test", "token": token, "Content-Length": str(len(raw))}
        handler.rfile = io.BytesIO(raw)
        handler.path = "/v1/session/heartbeat"
        responses: list = []
        handler.json_response = lambda status, payload: responses.append((status, payload))
        return responses, handler

    def test_heartbeat_stores_the_console_credential(self):
        token = self.panel_token()
        responses, handler = self.heartbeat_handler(token, {"userId": "4"})

        with patch.object(bridge.planner, "save_credential", return_value=True) as save_credential:
            handler.do_POST()

        save_credential.assert_called_once_with(token, "4")
        self.assertEqual([(200, {"stored": True, "userId": "4"})], responses)

    def test_heartbeat_rejects_a_credential_that_is_not_a_panel_token(self):
        responses, handler = self.heartbeat_handler("not-a-jwt", {"userId": "4"})

        with patch.object(bridge.planner, "save_credential") as save_credential:
            handler.do_POST()

        save_credential.assert_not_called()
        self.assertEqual(400, responses[0][0])

    def test_heartbeat_rejects_a_user_id_that_does_not_match_the_credential(self):
        responses, handler = self.heartbeat_handler(self.panel_token(4), {"userId": "9"})

        with patch.object(bridge.planner, "save_credential") as save_credential:
            handler.do_POST()

        save_credential.assert_not_called()
        self.assertEqual(400, responses[0][0])

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
        # 仓库里真实存在的分支不能被前置过滤挡掉：# 和中文都是 git 允许的。
        for value in ("feature/issue#duokai", "feature/issue#listening_message_plugin", "feature/中文分支"):
            self.assertTrue(bridge.valid_git_branch_name(value), value)
        for value in (
            "", "-branch", "feature..1", "feature//1", "feature/", "branch;rm -rf /", "branch name", "a" * 256,
            "feat~1", "feat^2", "feat:x", "feat?", "feat*", "feat[1]", "back\\slash", ".hidden", "x.lock", "@", "a@{0}",
        ):
            self.assertFalse(bridge.valid_git_branch_name(value), value)

    def test_push_accepts_a_branch_name_with_a_hash(self):
        self.add_origin()
        subprocess.run(
            ["git", "-C", str(self.workspace), "checkout", "-q", "-b", "feature/issue#duokai"],
            check=True, capture_output=True,
        )
        (self.workspace / "README.md").write_text("changed", encoding="utf-8")
        result = bridge.git_push_branch(self.workspace, "feature/issue#duokai", "feat: 带井号的分支")
        self.assertTrue(result["pushed"])
        self.assertTrue(result["committed"])

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

    def test_create_branch_pulls_the_latest_base_branch_first(self):
        remote = self.add_origin()
        subprocess.run(["git", "-C", str(self.workspace), "push", "-u", "origin", "main"], check=True, capture_output=True)
        worker = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(worker, ignore_errors=True))
        subprocess.run(["git", "clone", "--branch", "main", str(remote), str(worker)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(worker), "config", "user.email", "worker@test"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(worker), "config", "user.name", "worker"], check=True, capture_output=True)
        (worker / "remote.txt").write_text("latest", encoding="utf-8")
        subprocess.run(["git", "-C", str(worker), "add", "."], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(worker), "commit", "-m", "remote latest"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(worker), "push", "origin", "main"], check=True, capture_output=True)

        bridge.git_create_branch(self.workspace, "main", "feature/latest-base")

        self.assertTrue((self.workspace / "remote.txt").is_file())

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

    def test_push_pulls_remote_changes_before_the_user_commit(self):
        remote = self.add_origin()
        bridge.git_create_branch(self.workspace, "main", "feature/issue_req-pull-first")
        bridge.git_push_branch(self.workspace, "feature/issue_req-pull-first", "chore: initial push")
        worker = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(worker, ignore_errors=True))
        subprocess.run(
            ["git", "clone", "--branch", "feature/issue_req-pull-first", str(remote), str(worker)],
            check=True, capture_output=True,
        )
        subprocess.run(["git", "-C", str(worker), "config", "user.email", "worker@test"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(worker), "config", "user.name", "worker"], check=True, capture_output=True)
        (worker / "remote.txt").write_text("remote", encoding="utf-8")
        subprocess.run(["git", "-C", str(worker), "add", "."], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(worker), "commit", "-m", "remote change"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(worker), "push", "origin", "feature/issue_req-pull-first"], check=True, capture_output=True)
        (self.workspace / "README.md").write_text("local", encoding="utf-8")

        result = bridge.git_push_branch(self.workspace, "feature/issue_req-pull-first", "feat: local change")

        self.assertTrue(result["committed"])
        self.assertEqual("rebased", result["synced"])
        self.assertTrue((self.workspace / "remote.txt").is_file())
        log = subprocess.run(
            ["git", "-C", str(self.workspace), "log", "-1", "--format=%s"],
            check=True, capture_output=True, text=True,
        )
        self.assertEqual("feat: local change", log.stdout.strip())

    def test_push_pull_conflict_restores_uncommitted_work_without_a_temporary_commit(self):
        remote = self.add_origin()
        branch = "feature/issue_req-pull-conflict"
        bridge.git_create_branch(self.workspace, "main", branch)
        bridge.git_push_branch(self.workspace, branch, "chore: initial push")
        worker = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(worker, ignore_errors=True))
        subprocess.run(["git", "clone", "--branch", branch, str(remote), str(worker)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(worker), "config", "user.email", "worker@test"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(worker), "config", "user.name", "worker"], check=True, capture_output=True)
        (worker / "README.md").write_text("remote", encoding="utf-8")
        subprocess.run(["git", "-C", str(worker), "commit", "-am", "remote conflict"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(worker), "push", "origin", branch], check=True, capture_output=True)
        (self.workspace / "README.md").write_text("local", encoding="utf-8")

        with self.assertRaises(bridge.BridgeFailure):
            bridge.git_push_branch(self.workspace, branch, "feat: local conflict")

        self.assertEqual("local", (self.workspace / "README.md").read_text(encoding="utf-8"))
        self.assertTrue(bridge.git_worktree_dirty(self.workspace))
        log = subprocess.run(
            ["git", "-C", str(self.workspace), "log", "-1", "--format=%s"],
            check=True, capture_output=True, text=True,
        )
        self.assertNotEqual("delivery-task-planner: sync before commit", log.stdout.strip())

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

    def make_subproject(self, name: str, branch: str = "main") -> Path:
        """在工作目录下造一个独立的 Git 子工程，模拟「一个目录里摆多个仓库」的布局。"""
        child = self.workspace / name
        child.mkdir()
        for args in (
            ["init", f"--initial-branch={branch}"],
            ["config", "user.email", "bridge@test"],
            ["config", "user.name", "bridge"],
        ):
            subprocess.run(["git", "-C", str(child), *args], check=True, capture_output=True)
        (child / "README.md").write_text(name, encoding="utf-8")
        subprocess.run(["git", "-C", str(child), "add", "."], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(child), "commit", "-m", "init"], check=True, capture_output=True)
        # 根仓库不该把子工程的内容也算成自己的改动，否则建分支会被脏工作区拦下。
        ignore = self.workspace / ".gitignore"
        existing = ignore.read_text(encoding="utf-8").splitlines() if ignore.exists() else []
        ignore.write_text("\n".join(sorted({*existing, f"{name}/"})) + "\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.workspace), "add", ".gitignore"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.workspace), "commit", "-m", f"ignore {name}"], check=True, capture_output=True)
        return child

    def test_pending_submodules_are_initialized_with_the_workspace(self):
        # 造一个带子模块的远端仓库，模拟「clone 下来但子模块还是空目录」。
        module = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(module, ignore_errors=True))
        for args in (["init", "--initial-branch=main"], ["config", "user.email", "m@test"], ["config", "user.name", "m"]):
            subprocess.run(["git", "-C", str(module), *args], check=True, capture_output=True)
        (module / "lib.txt").write_text("lib", encoding="utf-8")
        subprocess.run(["git", "-C", str(module), "add", "."], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(module), "commit", "-m", "init"], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(self.workspace), "-c", "protocol.file.allow=always",
             "submodule", "add", str(module), "server"],
            check=True, capture_output=True,
        )
        subprocess.run(["git", "-C", str(self.workspace), "commit", "-m", "add submodule"], check=True, capture_output=True)
        # 反初始化，回到「.gitmodules 有登记但本机没内容」的状态。
        subprocess.run(["git", "-C", str(self.workspace), "submodule", "deinit", "-f", "server"], check=True, capture_output=True)

        self.assertEqual(["server"], bridge.git_pending_submodules(self.workspace))
        self.assertEqual(["server"], bridge.git_workspace_check(str(self.workspace))["pendingSubmodules"])
        result = bridge.git_initialize_submodules(self.workspace)
        self.assertEqual(["server"], result["submodules"])
        self.assertEqual("", result["submoduleError"])
        self.assertTrue((self.workspace / "server" / "lib.txt").is_file())
        self.assertEqual([], bridge.git_pending_submodules(self.workspace))
        # 没有子模块的仓库走这一步应该什么都不做，也不报错。
        with tempfile.TemporaryDirectory() as plain:
            subprocess.run(["git", "init", "-q", plain], check=True, capture_output=True)
            self.assertEqual({"submodules": [], "submoduleError": ""}, bridge.git_initialize_submodules(Path(plain)))

    def test_subprojects_list_first_level_repositories_only(self):
        self.make_subproject("server")
        plain = self.workspace / "docs"
        plain.mkdir()
        (plain / "note.md").write_text("note", encoding="utf-8")
        nested = self.workspace / "apps"
        nested.mkdir()
        (nested / "web").mkdir()
        subprocess.run(["git", "-C", str(nested / "web"), "init", "-q"], check=True, capture_output=True)
        self.assertEqual(
            ["server"],
            [path.name for path in bridge.git_subproject_workspaces(self.workspace)],
        )

    def test_workspace_projects_report_each_project_branch_and_changes(self):
        child = self.make_subproject("server")
        bridge.git_create_branch(self.workspace, "main", "feature/issue_req-1")
        (child / "README.md").write_text("changed", encoding="utf-8")
        catalog = bridge.git_workspace_projects(self.workspace, "feature/issue_req-1")
        root, server = catalog["projects"]
        self.assertEqual("", root["path"])
        self.assertEqual("feature/issue_req-1", root["currentBranch"])
        self.assertTrue(root["hasBranch"])
        self.assertEqual("server", server["path"])
        self.assertEqual("main", server["currentBranch"])
        self.assertFalse(server["hasBranch"])
        self.assertEqual(1, server["changed"])

    def test_branch_creation_covers_the_selected_subprojects(self):
        self.make_subproject("server")
        self.make_subproject("web")
        result = bridge.git_create_branch_targets(
            self.workspace, "main", "feature/issue_req-1", ["server"],
        )
        self.assertTrue(result["created"])
        self.assertEqual("feature/issue_req-1", bridge.git_current_branch(self.workspace / "server"))
        # 没勾的子项目一点都不该动。
        self.assertEqual("main", bridge.git_current_branch(self.workspace / "web"))
        self.assertEqual(["", "server"], [record["path"] for record in result["results"]])
        self.assertTrue(all(not record["error"] for record in result["results"]))

    def test_branch_creation_can_skip_a_root_that_already_has_the_branch(self):
        server = self.make_subproject("server")
        bridge.git_create_branch(self.workspace, "main", "feature/issue_req-1")
        # 根目录留一份没提交的改动：补建子项目分支不该被它挡住，也不该顺手把它带走。
        (self.workspace / "README.md").write_text("wip", encoding="utf-8")
        result = bridge.git_create_branch_targets(
            self.workspace, "main", "feature/issue_req-1", ["server"], skip_root=True,
        )
        self.assertTrue(result["results"][0]["skipped"])
        self.assertEqual("feature/issue_req-1", bridge.git_current_branch(server))
        self.assertEqual("wip", (self.workspace / "README.md").read_text(encoding="utf-8"))

    def test_branch_creation_refuses_to_skip_a_root_without_the_branch(self):
        self.make_subproject("server")
        with self.assertRaises(bridge.BridgeFailure):
            bridge.git_create_branch_targets(
                self.workspace, "main", "feature/issue_req-1", ["server"], skip_root=True,
            )

    def test_branch_creation_falls_back_to_the_subproject_default_branch(self):
        child = self.make_subproject("server", branch="develop")
        result = bridge.git_create_branch_targets(
            self.workspace, "main", "feature/issue_req-1", ["server"],
        )
        self.assertEqual("feature/issue_req-1", bridge.git_current_branch(child))
        self.assertEqual("develop", result["results"][1]["baseBranch"])

    def test_branch_creation_keeps_other_subprojects_when_one_fails(self):
        self.make_subproject("server")
        broken = self.workspace / "broken"
        broken.mkdir()
        subprocess.run(["git", "-C", str(broken), "init", "-q"], check=True, capture_output=True)
        ignore = self.workspace / ".gitignore"
        ignore.write_text(ignore.read_text(encoding="utf-8") + "broken/\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.workspace), "commit", "-am", "ignore broken"], check=True, capture_output=True)
        result = bridge.git_create_branch_targets(
            self.workspace, "main", "feature/issue_req-1", ["broken", "server"],
        )
        records = {record["path"]: record for record in result["results"]}
        # 空仓库连一条提交都没有，建不出分支；但同一轮里的 server 照样要建好。
        self.assertTrue(records["broken"]["error"])
        self.assertEqual("feature/issue_req-1", bridge.git_current_branch(self.workspace / "server"))

    def test_branch_creation_rejects_paths_outside_the_workspace(self):
        result = bridge.git_create_branch_targets(
            self.workspace, "main", "feature/issue_req-1", ["../escape"],
        )
        self.assertTrue(result["results"][1]["error"])

    def test_push_commits_subprojects_on_their_own_branch_when_the_branch_is_missing(self):
        server = self.make_subproject("server")
        detached = self.make_subproject("detached")
        subprocess.run(
            ["git", "-C", str(detached), "checkout", "--detach", "HEAD"], check=True, capture_output=True,
        )
        self.add_origin()
        bridge.git_create_branch(self.workspace, "main", "feature/issue_req-1")
        (server / "README.md").write_text("changed", encoding="utf-8")
        execution = bridge.ExecutionBridge(self.workspace)
        records = {record["path"]: record for record in execution._push_subproject_branches(
            "feature/issue_req-1",
            "feat: 需求改动",
            ["server", "detached"],
            push=False,
        )}
        # 子项目没有需求分支：改动提交到它自己当前的 main 上，而不是被跳过。
        self.assertEqual("main", records["server"]["branch"])
        self.assertTrue(records["server"]["committed"])
        self.assertFalse(records["server"]["error"])
        self.assertFalse(bridge.git_worktree_dirty(server))
        # 游离 HEAD 没有可推送的分支，只能跳过。
        self.assertTrue(records["detached"]["skipped"])

    def test_batch_push_executes_subprojects_before_the_root(self):
        server = self.make_subproject("server")
        self.add_origin()
        branch = "feature/issue_req-push-order"
        bridge.git_create_branch(self.workspace, "main", branch)
        (server / "README.md").write_text("child changed", encoding="utf-8")
        (self.workspace / "README.md").write_text("root changed", encoding="utf-8")
        execution = bridge.ExecutionBridge(self.workspace)
        calls: list[Path] = []
        real_push = bridge.git_push_branch

        def ordered_push(workspace: Path, *args, **kwargs):
            calls.append(workspace.resolve())
            return real_push(workspace, *args, **kwargs)

        with patch.object(bridge, "git_push_branch", side_effect=ordered_push):
            result = execution.push_requirement_branch({
                "programId": 1,
                "branch": branch,
                "message": "feat: ordered push",
                "targets": ["server"],
                "commitOnly": True,
            })

        self.assertEqual([server.resolve(), self.workspace.resolve()], calls)
        # 展示层仍然把主项目放第一行，避免改变前端现有的结果布局。
        self.assertEqual(["", "server"], [record["path"] for record in result["results"]])

    def test_prepare_switches_subprojects_that_have_the_branch(self):
        server = self.make_subproject("server")
        web = self.make_subproject("web")
        bridge.git_create_branch_targets(self.workspace, "main", "feature/issue_req-1", ["server"])
        bridge.git_checkout_branch(self.workspace, "main")
        bridge.git_checkout_branch(server, "main")
        targets = bridge.git_subproject_targets_of(self.workspace, None, "feature/issue_req-1")
        self.assertEqual(["server"], targets)
        result = bridge.git_prepare_branch_targets(
            self.workspace, "feature/issue_req-1", "switch", "", "", "origin", targets,
        )
        self.assertEqual("feature/issue_req-1", bridge.git_current_branch(server))
        self.assertEqual("main", bridge.git_current_branch(web))
        self.assertEqual(["", "server"], [record["path"] for record in result["results"]])

    def test_prepare_switches_a_detached_subproject_onto_the_remote_branch(self):
        """子模块的常态就是游离 HEAD：分支只在远端时也要建出本地分支并切过去。"""
        remote = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(remote, ignore_errors=True))
        subprocess.run(["git", "init", "--bare", "--initial-branch=main", str(remote)], check=True, capture_output=True)

        server = self.make_subproject("server")
        subprocess.run(["git", "-C", str(server), "remote", "add", "origin", str(remote)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(server), "push", "-u", "origin", "main"], check=True, capture_output=True)
        # 需求分支只存在于远端：本地既没有它，HEAD 也不在任何分支上。
        subprocess.run(["git", "-C", str(server), "checkout", "-b", "feature/issue_req-1"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(server), "push", "origin", "feature/issue_req-1"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(server), "checkout", "main"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(server), "branch", "-D", "feature/issue_req-1"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(server), "checkout", "--detach", "HEAD"], check=True, capture_output=True)
        self.assertEqual("", bridge.git_current_branch(server))

        bridge.git_create_branch(self.workspace, "main", "feature/issue_req-1")
        bridge.git_checkout_branch(self.workspace, "main")
        targets = bridge.git_subproject_targets_of(self.workspace, None, "feature/issue_req-1")
        self.assertEqual(["server"], targets)

        result = bridge.git_prepare_branch_targets(
            self.workspace, "feature/issue_req-1", "switch", "", "", "origin", targets,
        )

        self.assertEqual("feature/issue_req-1", bridge.git_current_branch(server))
        child = next(record for record in result["results"] if record["path"] == "server")
        self.assertTrue(child["switched"])
        self.assertEqual("", child["error"])
        # 本地分支必须跟踪远端的那条，之后的推送才有默认上游。
        upstream = subprocess.run(
            ["git", "-C", str(server), "rev-parse", "--abbrev-ref", "feature/issue_req-1@{upstream}"],
            capture_output=True, text=True,
        )
        self.assertEqual("origin/feature/issue_req-1", upstream.stdout.strip())

    def test_prepare_still_refuses_a_detached_root_workspace(self):
        """根目录的游离 HEAD 通常是人工操作的中间态，仍然拦下来交给用户处理。"""
        subprocess.run(["git", "-C", str(self.workspace), "checkout", "--detach", "HEAD"], check=True, capture_output=True)
        with self.assertRaises(bridge.BridgeFailure) as raised:
            bridge.git_prepare_branch(self.workspace, "feature/issue_req-1")
        self.assertIn("游离 HEAD", str(raised.exception))

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
