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


PLANNER_SKILL_DIR = PLUGIN_ROOT / "skills" / "delivery-task-planner"


def planner_skill_text(name: str) -> str:
    """读技能正文。

    提示词和技能正文不再重复同一条规则：随轮次变的（模式、权限、路径、开关）在提示词里，
    不随轮次变的行为规范在技能里。所以这类用例要盯两头——提示词指对了路，正文也还在。
    """
    return (PLANNER_SKILL_DIR / name).read_text(encoding="utf-8")


class HttpBridgeTest(unittest.TestCase):
    def stub_claude_help(self, flags: set[str] | None = None) -> None:
        """固定住 CLI 能力探测：用例不该去问本机真装了哪个版本的 claude。"""
        from delivery_bridge.clients import claude as claude_client

        patcher = patch.object(claude_client, "CLAUDE_HELP_FLAGS", flags if flags is not None else set())
        patcher.start()
        self.addCleanup(patcher.stop)

    def setUp(self) -> None:
        # THREAD_READERS 是模块级全局池，会把上一条用例建的只读执行器留给下一条。
        bridge.THREAD_READERS.shutdown()
        self.addCleanup(bridge.THREAD_READERS.shutdown)

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

    def test_main_binds_every_interface_without_a_host_argument(self):
        server = unittest.mock.MagicMock()
        argv = ["http_bridge.py", "--port", "8765"]

        with (
            patch.object(bridge.sys, "argv", argv),
            patch.object(bridge, "placeholder_workspace", return_value=Path("/workspace")),
            patch.object(bridge, "create_http_server", return_value=server) as create,
            patch.object(bridge.threading, "Thread"),
            patch.object(bridge.THREAD_READERS, "shutdown"),
        ):
            bridge.main()

        self.assertEqual("0.0.0.0", create.call_args.args[0])
        self.assertEqual(8765, create.call_args.args[1])
        server.serve_forever.assert_called_once_with()

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

    def test_business_attachments_round_trip_inside_the_business_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "business-root"
            workspace = bridge.business_workspace_path_of("alice/业务空间/客户运营平台", root)
            execution = bridge.ExecutionBridge(workspace, business_workspace_root=root)

            saved = execution.save_business_attachments(
                7, "business-requirement-12", [{"name": "背景.png", "data": b"png-bytes", "contentType": "image/png"}],
            )
            attachment = saved["attachments"][0]
            self.assertEqual("背景.png", attachment["name"])
            self.assertTrue(attachment["isImage"])

            manifest, path = execution.business_attachment(7, "business-requirement-12", attachment["id"])
            self.assertEqual(b"png-bytes", path.read_bytes())
            self.assertEqual("背景.png", manifest["name"])
            # 附件必须落在这条业务诉求自己的工作目录里，不能漏到受控根目录之外。
            self.assertTrue(str(path).startswith(str(workspace)))
            with self.assertRaises(bridge.BridgeFailure):
                execution.business_attachment(8, "business-requirement-12", attachment["id"])

            # 会话请求按 id 取回附件时要拿到可读的绝对路径，Codex 才能把图片当输入读进去。
            resolved = execution.attachments.resolve(7, "business-requirement-12", [attachment["id"]])
            self.assertEqual([str(path)], [item["path"] for item in resolved])
            with self.assertRaises(bridge.BridgeFailure):
                execution.attachments.resolve(7, "business-requirement-13", [attachment["id"]])

    def test_business_conversation_payload_keeps_attachment_ids(self):
        payload = {
            "programId": 7, "itemKey": "business-requirement-12", "message": "看看这张图",
            "businessIntake": True, "provider": "codex", "attachmentIds": ["  first  ", "", "second"],
        }
        self.assertEqual(
            ["first", "second"],
            bridge.validate_business_conversation_payload(payload)[6],
        )
        payload["attachmentIds"] = ["id"] * (bridge.MAX_CONVERSATION_ATTACHMENTS + 1)
        with self.assertRaises(bridge.BridgeFailure):
            bridge.validate_business_conversation_payload(payload)

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

    def test_plugin_update_install_does_not_require_a_user_token(self):
        payload = json.dumps({"expectedVersion": "1.2.3"}).encode("utf-8")
        handler = object.__new__(bridge.BridgeHandler)
        handler.server = SimpleNamespace(allowed_origins={"*"}, bridge=unittest.mock.MagicMock())
        handler.server.bridge.active_run_count.return_value = 0
        handler.headers = Message()
        handler.headers["Content-Type"] = "application/json"
        handler.headers["Content-Length"] = str(len(payload))
        handler.rfile = io.BytesIO(payload)
        handler.path = "/v1/plugin/update/install"
        responses = []
        handler.json_response = lambda status, value: responses.append((status, value))

        job = {"jobId": "job-1", "status": "resolving"}
        with (
            patch.object(bridge.PLUGIN_UPDATES, "start", return_value=job) as start,
            patch.object(bridge, "complete_plugin_update_in_background") as complete,
        ):
            handler.do_POST()

        start.assert_called_once_with("1.2.3")
        handler.server.bridge.active_run_count.assert_called_once_with()
        complete.assert_called_once_with("job-1", handler.server.bridge)
        self.assertEqual([(202, {"jobId": "job-1", "status": "resolving", "activeRuns": 0})], responses)

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
        with tempfile.TemporaryDirectory() as temporary, patch.object(bridge.runtime, "PLUGIN_ROOT", Path(temporary)):
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

    def test_planning_temp_summary_appends_rounds_without_losing_the_baseline(self):
        """续聊的回复只是增量：整篇覆盖会把首轮那份完整预览冲掉。"""
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "temp.md"
            bridge.write_planning_temp_summary(path, "审核", "req-a", "thread-1", "首轮", "完整预览")
            bridge.write_planning_temp_summary(
                path, "审核", "req-a", "thread-1", "再改一条", "只改第 3 条", incremental=True,
            )
            content = path.read_text(encoding="utf-8")
            baseline, rounds = bridge.planning_temp_sections(path)

        self.assertEqual("完整预览", baseline)
        self.assertEqual(1, len(rounds))
        self.assertIn("只改第 3 条", rounds[0])
        self.assertIn("完整预览", content)
        self.assertIn("再改一条", content)

    def test_planning_temp_summary_keeps_only_the_latest_rounds(self):
        """更早的增量早被后面的轮次覆盖了，留着只会把恢复上下文撑大。"""
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "temp.md"
            bridge.write_planning_temp_summary(path, "审核", "req-a", "thread-1", "首轮", "完整预览")
            for index in range(bridge.MAX_PLANNING_TEMP_ROUNDS + 3):
                bridge.write_planning_temp_summary(
                    path, "审核", "req-a", "thread-1", f"第 {index} 次", f"增量 {index}", incremental=True,
                )
            baseline, rounds = bridge.planning_temp_sections(path)
            content = path.read_text(encoding="utf-8")

        self.assertEqual("完整预览", baseline)
        self.assertEqual(bridge.MAX_PLANNING_TEMP_ROUNDS, len(rounds))
        self.assertNotIn("增量 0", content)
        self.assertIn(f"增量 {bridge.MAX_PLANNING_TEMP_ROUNDS + 2}", content)

    def test_planning_temp_summary_restarts_the_baseline_for_an_unreadable_draft(self):
        """旧格式或被删过的摘要认不出基线：这一轮只能自己当基线，不能接着往后追加。"""
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "temp.md"
            path.write_text("# 老格式的摘要\n", encoding="utf-8")
            bridge.write_planning_temp_summary(
                path, "审核", "req-a", "thread-1", "续聊", "本轮增量", incremental=True,
            )
            baseline, rounds = bridge.planning_temp_sections(path)

        self.assertEqual("本轮增量", baseline)
        self.assertEqual([], rounds)

    def test_planning_temp_summary_is_deleted_only_inside_the_managed_directory(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(bridge.runtime, "PLUGIN_ROOT", Path(temporary)):
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

    def test_planning_first_round_talks_the_requirement_through_before_planning(self):
        """需求刚起头时用户还在补背景：这一轮的提示词里不该有任何拆解契约。"""
        discussion = bridge.build_planning_prompt(
            1, {"program": {}}, "我想做一个审核台",
            requirement={"requirementKey": "req-a", "generatePrototype": True, "preGenerateTaskDocuments": True},
            mode=bridge.PLANNING_MODE_DISCUSSION,
        )

        self.assertIn("和用户一起把这条需求聊清楚", discussion)
        self.assertIn(bridge.BREAKDOWN_INVITE_QUESTION, discussion)
        self.assertIn("禁止执行 create-task-board-tasks", discussion)
        # 拆解契约、任务说明格式和拆解设置都不出现，免得执行器把「先出方案」当成本轮目标。
        self.assertNotIn("序号 / 任务标题 / 收益标签", discussion)
        self.assertNotIn("每条任务说明至少交代六件事", discussion)
        self.assertNotIn("确认并写入」按钮", discussion)
        self.assertNotIn("拆解成多条任务:", discussion)
        self.assertNotIn("拆解后生成原型图:", discussion)
        self.assertNotIn("预生成任务需求文档", discussion)

    def test_planning_breakdown_round_carries_the_full_contract(self):
        breakdown = bridge.build_planning_prompt(
            1, {"program": {}}, "可以拆解了", requirement={"requirementKey": "req-a"},
            mode=bridge.PLANNING_MODE_BREAKDOWN,
        )

        self.assertIn("序号 / 任务标题 / 收益标签", breakdown)
        # 六段式任务说明的正文只在技能里写一份，提示词负责把执行器指过去。
        self.assertIn("references/任务拆解与写入.md", breakdown)
        self.assertIn("每条描述至少交代六件事", planner_skill_text("references/任务拆解与写入.md"))
        # 关键词误判也要兜住：用户其实只是在补需求时，这一轮不能硬凑一份任务表。
        self.assertIn("不要硬凑一份任务表出来", breakdown)

    def test_planning_round_mode_enters_breakdown_only_when_it_is_asked_for(self):
        mode = bridge.planning_round_mode
        self.assertEqual(bridge.PLANNING_MODE_DISCUSSION, mode("这个审核台还要支持批量驳回"))
        self.assertEqual(bridge.PLANNING_MODE_BREAKDOWN, mode("帮我拆解一下"))
        self.assertEqual(bridge.PLANNING_MODE_BREAKDOWN, mode("生成任务吧"))
        # 确认写入必然是拆解轮；进过拆解态之后不再退回沟通态。
        self.assertEqual(bridge.PLANNING_MODE_BREAKDOWN, mode("好", confirm_write=True))
        self.assertEqual(
            bridge.PLANNING_MODE_BREAKDOWN,
            mode("再补一条", previous_mode=bridge.PLANNING_MODE_BREAKDOWN),
        )
        # 模型问过「要不要开始拆」之后，一句「好的」才算接住引导。
        self.assertEqual(bridge.PLANNING_MODE_DISCUSSION, mode("好的"))
        self.assertEqual(bridge.PLANNING_MODE_BREAKDOWN, mode("好的", invited=True))
        self.assertEqual(bridge.PLANNING_MODE_DISCUSSION, mode("可以先不管权限这块", invited=True))
        self.assertTrue(bridge.planning_invite_offered(f"...\n{bridge.BREAKDOWN_INVITE_QUESTION}"))
        self.assertFalse(bridge.planning_invite_offered("需求我理解了，还有两个问题"))

    def test_planning_follow_up_prompt_keeps_talking_while_in_discussion_mode(self):
        follow_up = bridge.build_planning_follow_up_prompt(
            1, {"program": {}}, "再补充一点背景", requirement={"requirementKey": "req-a"},
            thread_id="thread-1", mode=bridge.PLANNING_MODE_DISCUSSION,
        )

        self.assertIn("本轮仍然只聊需求", follow_up)
        self.assertIn(bridge.BREAKDOWN_INVITE_QUESTION, follow_up)
        self.assertNotIn("输出增量，不要重印整份预览", follow_up)
        self.assertNotIn("确认无误后点「确认并写入」", follow_up)
        self.assertNotIn("拆解成多条任务:", follow_up)

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

    def test_planning_follow_up_prompt_asks_for_an_increment_instead_of_a_reprint(self):
        """整份预览重印一次，就是本轮的输出加上后面每一轮的重读，成本按轮次翻倍。"""
        follow_up = bridge.build_planning_follow_up_prompt(
            1, {"program": {}}, "第 3 条改一下", requirement={"requirementKey": "req-a"}, thread_id="thread-1",
        )

        self.assertIn("输出增量，不要重印整份预览", follow_up)
        self.assertIn("其余 N 条不变", follow_up)
        self.assertIn("开始拆解那一轮的全量预览 + 后续各轮增量", follow_up)
        self.assertIn("确认无误后点「确认并写入」", follow_up)
        self.assertNotIn("输出格式沿用首轮", follow_up)

    def test_planning_follow_up_prompt_only_lists_the_tasks_it_has_not_sent_yet(self):
        context = {
            "items": [
                {"itemKey": "task-a", "title": "首轮就有的任务", "requirementKey": "req-a"},
                {"itemKey": "task-b", "title": "这一轮才建的任务", "requirementKey": "req-a"},
            ],
        }
        requirement = {"requirementKey": "req-a"}

        first = bridge.build_planning_follow_up_prompt(1, context, "继续", requirement=requirement)
        incremental = bridge.build_planning_follow_up_prompt(
            1, context, "继续", requirement=requirement, known_item_keys=["task-a"],
        )
        nothing_new = bridge.build_planning_follow_up_prompt(
            1, context, "继续", requirement=requirement, known_item_keys=["task-a", "task-b"],
        )

        self.assertIn("首轮就有的任务", first)
        self.assertNotIn("首轮就有的任务", incremental)
        self.assertIn("这一轮才建的任务", incremental)
        self.assertIn("这里只补新增的部分", incremental)
        self.assertIn("与此前轮次给出的那 2 条相同", nothing_new)
        self.assertNotIn("这一轮才建的任务", nothing_new)

    def test_requirement_item_keys_follow_the_prompt_order(self):
        context = {
            "items": [
                {"itemKey": "task-a", "requirementKey": "req-a"},
                {"itemKey": "task-z", "requirementKey": "req-z"},
                {"itemKey": "task-b", "requirementKey": "req-a"},
            ],
        }

        self.assertEqual(["task-a", "task-b"], bridge.requirement_item_keys(context, "req-a"))
        self.assertEqual([], bridge.requirement_item_keys(context, ""))

    def test_planning_prompt_bounds_the_workspace_survey(self):
        """勘察是首轮最容易失控的一段：不封边界就会把整个模块通读一遍。"""
        preview = bridge.build_planning_prompt(1, {"program": {}}, "拆一下", requirement={"requirementKey": "req-a"})

        # 勘察纪律的正文在 SKILL.md 第 3 节（每轮都会加载），提示词只负责点名它。
        self.assertIn("SKILL.md 第 3 节「勘察工作区现状」", preview)
        skill = planner_skill_text("SKILL.md")
        self.assertIn("勘察点到为止", skill)
        self.assertIn("不要通读整个模块", skill)
        self.assertIn("只贴关键几行并给出路径和行号", skill)

    def test_planning_confirm_prompt_merges_the_incremental_preview(self):
        confirmed = bridge.build_planning_prompt(
            1, {"program": {}}, "确认并写入", requirement={"requirementKey": "req-a"},
            write_allowed=True, thread_id="thread-1",
        )

        self.assertIn("开始拆解那一轮给完整方案，之后每轮只给增量", confirmed)
        self.assertIn("不要凭最后一轮的增量反推整份方案", confirmed)

    def test_planning_follow_up_prompt_resends_the_detail_only_after_it_changes(self):
        requirement = {"requirementKey": "req-a", "detail": "改过的正文"}
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            with patch.object(bridge.runtime, "PLUGIN_ROOT", workspace):
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
        self.assertNotIn("前置任务的文档目录", follow_up)

    def test_requirement_fine_tuning_prompt_loads_workspace_skills_without_reopening_delivery_flow(self):
        requirement = {"requirementKey": "req-a", "name": "需求一", "detail": "已完成能力"}
        context = {
            "items": [
                {"itemKey": "task-a", "title": "接口", "requirementKey": "req-a", "phase": "development", "status": "done"},
                {"itemKey": "task-b", "title": "无关任务", "requirementKey": "req-b"},
            ],
        }

        prompt = bridge.build_requirement_fine_tuning_prompt(
            1, requirement, context, "补充一个筛选条件", Path("/tmp/workspace"),
        )

        self.assertIn(".codex/skills/", prompt)
        self.assertIn("task-a: 接口", prompt)
        self.assertNotIn("task-b: 无关任务", prompt)
        self.assertIn("不得创建或拆解任务", prompt)
        self.assertIn("不得领取任务", prompt)
        self.assertIn("补充一个筛选条件", prompt)

    def test_task_fine_tuning_prompt_and_stop_payload_preserve_task_lifecycle(self):
        task = {
            "itemKey": "task-a", "title": "接口", "requirementKey": "req-a",
            "description": "已交付", "phase": "development", "status": "done",
        }
        requirement = {"requirementKey": "req-a", "name": "需求一", "detail": "需求上下文"}

        prompt = bridge.build_task_fine_tuning_prompt(
            1, task, {"items": [task]}, requirement, "调整返回字段", Path("/tmp/workspace"),
        )
        parsed = bridge.validate_fine_tuning_payload({"programId": 1, "itemKey": "task-a"}, "task", message_required=False)

        self.assertEqual("task-a", parsed[1])
        self.assertIn(".codex/skills/", prompt)
        self.assertIn("需求上下文", prompt)
        self.assertIn("不得领取任务、推进任务或需求阶段", prompt)
        self.assertIn("调整返回字段", prompt)

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
                patch.object(bridge.factory, "create_ai_client", return_value=client),
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
            patch.object(bridge.factory, "create_ai_client", return_value=client),
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
            patch.object(bridge.factory, "create_ai_client", return_value=client),
        ):
            conversation = executor.planning(2, "thread-remote", config=self.runtime_config() | {"_project_id": 2}, requirement_key="req-a")

        self.assertEqual("thread-remote", conversation["threadId"])
        self.assertEqual([], conversation["turns"])
        self.assertEqual(["thread-remote"], [entry["threadId"] for entry in conversation["conversations"]])
        # 只读执行器现在归 THREAD_READERS 复用池所有：读完不再立刻关，
        # 但池子收摊时必须把进程关掉，不能泄漏。
        client.close.assert_not_called()
        bridge.THREAD_READERS.shutdown()
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
            patch.object(bridge.factory, "create_ai_client", return_value=client) as create_client,
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

        def make_client(provider, workspace, listener=None, environment=None, **options):
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
            patch.object(bridge.factory, "create_ai_client", side_effect=make_client),
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

    def test_planning_talks_first_and_only_plans_once_the_user_asks(self):
        """需求聊天是引导式的：首轮只沟通，用户开口要拆才发拆解契约，也才允许确认写入。"""
        executor = bridge.ExecutionBridge(Path.cwd())
        client = unittest.mock.MagicMock()
        client.start_task.return_value = ("thread-1", "turn-1")
        client.start_turn.return_value = "turn-2"
        context = {"program": {"programId": 2, "name": "项目 2"}, "items": [], "stages": [], "modules": []}
        config = {"api_url": "http://test/api", "key": "k", "_project_id": 2}
        payload = {"programId": 2, "message": "我想做一个审核台", "newConversation": True, "requirementKey": "req-a"}
        planning_sessions = []

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

        def release():
            with executor.lock:
                executor.active_runs.clear()
                executor.active.clear()

        with (
            patch.object(bridge.planner, "project_context", return_value=context),
            patch.object(bridge.planner, "request_api", side_effect=request_api),
            patch.object(bridge.factory, "create_ai_client", return_value=client),
            patch.object(bridge.threading, "Thread"),
        ):
            executor.send_planning(payload, config)
            release()
            discussion_prompt = client.start_task.call_args[0][1]
            first_round_mode = planning_sessions[0]["metadata"]["planningMode"]
            # 还在沟通阶段就点确认：没有可写的方案，直接拦下。
            with self.assertRaisesRegex(bridge.BridgeFailure, "请先梳理需求并生成拆解预览"):
                executor.send_planning(
                    {**payload, "message": "确认", "newConversation": False, "confirmWrite": True}, config,
                )
            executor.send_planning({**payload, "message": "帮我拆解一下", "newConversation": False}, config)
            release()

        breakdown_prompt = client.start_turn.call_args[0][1]
        self.assertIn("和用户一起把这条需求聊清楚", discussion_prompt)
        self.assertNotIn("序号 / 任务标题 / 收益标签", discussion_prompt)
        self.assertEqual(bridge.PLANNING_MODE_DISCUSSION, first_round_mode)
        self.assertEqual(bridge.PLANNING_MODE_BREAKDOWN, planning_sessions[0]["metadata"]["planningMode"])
        # 从沟通转进拆解的那一轮要给全量契约：此前一轮都没发过，增量续聊无从增起。
        self.assertIn("序号 / 任务标题 / 收益标签", breakdown_prompt)
        self.assertNotIn("输出增量，不要重印整份预览", breakdown_prompt)

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

        def make_client(provider, workspace, listener=None, environment=None, **options):
            providers.append(provider)
            return client

        with (
            patch.object(bridge.planner, "request_api", side_effect=request_api),
            patch.object(bridge.factory, "create_ai_client", side_effect=make_client),
        ):
            result = executor.planning(
                2, "", config={"api_url": "http://test/api", "key": "k", "_project_id": 2},
                requirement_key="req-a", provider="claude",
            )

        # 原型会话与拆解会话同表不同用途，不能混进拆解列表。
        self.assertEqual(["codex-thread"], [entry["threadId"] for entry in result["conversations"]])
        self.assertEqual("codex", result["conversations"][0]["executorType"])
        self.assertEqual("codex-thread", result["threadId"])
        self.assertEqual({"codex"}, set(providers))

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

        def make_client(provider, workspace, listener=None, environment=None, **options):
            providers.append(provider)
            return client

        with (
            patch.object(bridge.planner, "project_context", return_value=context),
            patch.object(bridge.planner, "request_api", side_effect=request_api),
            patch.object(bridge.factory, "create_ai_client", side_effect=make_client),
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
        self.assertEqual({"codex"}, set(providers))
        self.assertEqual("codex", binds[-1]["executorType"])

    def test_planning_follow_up_sends_only_the_tasks_the_thread_has_not_seen(self):
        """已建任务清单是「不要重复建任务」的依据，但它逐轮重发就是每轮几十行的固定开销。"""
        executor = bridge.ExecutionBridge(Path.cwd())
        client = unittest.mock.MagicMock()
        client.start_turn.return_value = "turn-2"
        context = {
            "program": {"programId": 2, "name": "项目 2"},
            "items": [
                {"itemKey": "task-a", "title": "首轮就有的任务", "requirementKey": "req-a"},
                {"itemKey": "task-b", "title": "这一轮才建的任务", "requirementKey": "req-a"},
            ],
            "stages": [],
            "modules": [],
        }
        binds = []
        rows = [{
            "threadId": "codex-thread", "title": "需求拆解 · A", "status": "completed",
            "executorType": "codex", "updatedAt": "2026-08-01T00:00:00Z",
            "metadata": {"sentItemKeys": ["task-a"]},
        }]

        def request_api(_config, method, path, query=None, body=None):
            if method == "GET" and path == "/delivery/requirement/planning-sessions":
                return list(rows)
            if method == "POST" and path == "/delivery/requirement/planning-session/bind":
                binds.append(body)
                return None
            self.fail(f"unexpected request: {method} {path}")

        with (
            patch.object(bridge.planner, "project_context", return_value=context),
            patch.object(bridge.planner, "request_api", side_effect=request_api),
            patch.object(bridge.factory, "create_ai_client", return_value=client),
            patch.object(bridge.threading, "Thread"),
        ):
            executor.send_planning(
                {
                    "programId": 2, "message": "继续", "requirementKey": "req-a",
                    "threadId": "codex-thread", "provider": "codex",
                },
                {"api_url": "http://test/api", "key": "k", "_project_id": 2},
            )

        prompt = client.start_turn.call_args.args[1]
        self.assertNotIn("首轮就有的任务", prompt)
        self.assertIn("这一轮才建的任务", prompt)
        # 本轮列过的键并进会话记录，下一轮从这里继续算增量。
        self.assertEqual(["task-a", "task-b"], binds[-1]["metadata"]["sentItemKeys"])

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

        def make_client(provider, workspace, listener=None, environment=None, **options):
            providers.append(provider)
            return client

        with (
            patch.object(bridge.planner, "request_api", side_effect=request_api),
            patch.object(bridge.factory, "create_ai_client", side_effect=make_client),
        ):
            result = executor.conversation(1, "api-1", config=self.runtime_config(), provider="claude")

        self.assertEqual("codex-thread", result["threadId"])
        self.assertEqual("codex", result["conversations"][0]["executorType"])
        self.assertEqual({"codex"}, set(providers))

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
        self.stub_claude_help()
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

    def _claude_command(self, lightweight: bool = False, flags: set[str] | None = None) -> list[str]:
        self.stub_claude_help(flags)
        client = bridge.ClaudeCLIClient(Path.cwd(), lightweight=lightweight)
        with patch.object(bridge.shutil, "which", return_value="/bin/claude"), patch.object(
            bridge.subprocess, "Popen", return_value=unittest.mock.MagicMock(),
        ) as popen, patch.object(bridge.threading, "Thread"):
            client.start_task("Task", "Prompt")
        return popen.call_args.args[0]

    def test_delivery_sessions_only_load_the_tools_a_delivery_round_uses(self):
        command = self._claude_command()

        index = command.index("--tools")
        self.assertEqual(
            ["Bash", "Read", "Edit", "Write", "Skill"],
            command[index + 1:index + 6],
        )
        # 变长参数后面必须还跟着别的开关，不然模型或会话号会被它吞掉。
        self.assertTrue(command[index + 6].startswith("--"))

    def test_naming_rounds_load_no_tools_at_all(self):
        command = self._claude_command(lightweight=True)

        self.assertEqual("", command[command.index("--tools") + 1])

    def test_an_older_cli_without_the_tools_flag_keeps_the_full_tool_set(self):
        command = self._claude_command(flags={"--strict-mcp-config"})

        self.assertNotIn("--tools", command)

    def test_the_bridge_process_owns_its_pid_file(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(bridge.runtime, "RUNTIME_DIR", Path(directory)):
                bridge.pidfile.write_pid(4321)
                self.assertEqual(4321, bridge.pidfile.read_pid())
                self.assertEqual(
                    "4321", (Path(directory) / "http-bridge.pid").read_text(encoding="utf-8").strip(),
                )

                bridge.pidfile.clear_pid(4321)
                self.assertEqual(0, bridge.pidfile.read_pid())
                self.assertFalse((Path(directory) / "http-bridge.pid").exists())

    def test_clearing_never_removes_another_process_record(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(bridge.runtime, "RUNTIME_DIR", Path(directory)):
                # 重启时新旧进程会短暂并存：退出的那个不能把接班进程的记录抹掉。
                bridge.pidfile.write_pid(777)

                bridge.pidfile.clear_pid(4321)

                self.assertEqual(777, bridge.pidfile.read_pid())

    def test_an_unreadable_pid_file_reads_as_no_bridge(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(bridge.runtime, "RUNTIME_DIR", Path(directory)):
                (Path(directory) / "http-bridge.pid").write_text("不是数字", encoding="utf-8")

                self.assertEqual(0, bridge.pidfile.read_pid())

    def test_tracking_registers_the_real_pid_and_a_signal_cleanup(self):
        signals: list[int] = []
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(bridge.runtime, "RUNTIME_DIR", Path(directory)),
                patch.object(bridge.pidfile.atexit, "register") as register,
                patch.object(
                    bridge.pidfile.signal, "signal", side_effect=lambda number, _handler: signals.append(number),
                ),
            ):
                bridge.pidfile.track_current_process()

            self.assertEqual(os.getpid(), bridge.pidfile.read_pid())
        register.assert_called_once_with(bridge.pidfile.clear_pid)
        self.assertIn(bridge.pidfile.signal.SIGTERM, signals)

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
                with patch.object(bridge.hostinfo, "host_platform", return_value="windows"):
                    with patch.object(bridge.runtime, "RUNTIME_DIR", Path(directory)):
                        self.assertTrue(executor.health("codex")["ready"])

    def test_codex_desktop_resource_is_copied_when_the_cli_is_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "resources" / "codex.exe"
            source.parent.mkdir()
            source.write_bytes(b"desktop-codex")
            runtime = root / "runtime"
            with patch.object(bridge.shutil, "which", return_value=None):
                with patch.object(bridge.codex_cli, "codex_desktop_resource_paths", return_value=[source]):
                    copied = bridge.provision_codex_cli("windows", runtime)

            self.assertEqual(runtime / "bin" / "codex.exe", Path(copied))
            self.assertEqual(b"desktop-codex", Path(copied).read_bytes())

    def test_claude_sessions_load_no_mcp_server_at_all(self):
        """执行会话只用 CLI 自带的工具：本机全局配置的 MCP 与交付任务无关，白占系统提示。"""
        from delivery_bridge.clients import claude as claude_client
        self.stub_claude_help()

        with tempfile.TemporaryDirectory() as workspace:
            store = bridge.ClaudeTranscriptStore(Path(workspace) / "transcripts")
            client = bridge.ClaudeCLIClient(Path(workspace), transcripts=store)
            with patch.object(claude_client.shutil, "which", return_value="/usr/local/bin/claude"):
                with patch.object(claude_client.subprocess, "Popen") as popen:
                    with patch.object(claude_client.threading, "Thread"):
                        client._start("执行任务")

        command = popen.call_args.args[0]
        self.assertIn("--strict-mcp-config", command)
        self.assertNotIn("--mcp-config", command)

    def test_claude_sessions_keep_the_system_prompt_stable_for_the_prompt_cache(self):
        """cwd、git 状态这些每轮都在变的段落留在系统提示里，等于每轮都要按新 token 重算。"""
        from delivery_bridge.clients import claude as claude_client
        self.stub_claude_help()

        with tempfile.TemporaryDirectory() as workspace:
            store = bridge.ClaudeTranscriptStore(Path(workspace) / "transcripts")
            client = bridge.ClaudeCLIClient(Path(workspace), transcripts=store)
            with patch.object(claude_client.shutil, "which", return_value="/usr/local/bin/claude"):
                with patch.object(claude_client.subprocess, "Popen") as popen:
                    with patch.object(claude_client.threading, "Thread"):
                        client._start("执行任务")

        self.assertIn("--exclude-dynamic-system-prompt-sections", popen.call_args.args[0])

    def test_claude_saving_flags_are_filtered_by_the_local_cli(self):
        """老版本 CLI 不认识这些新开关，直接传过去它会当场退出，整条会话就废了。"""
        from delivery_bridge.clients import claude as claude_client

        self.stub_claude_help({"--strict-mcp-config", "--model"})
        self.assertEqual(
            ["--strict-mcp-config"],
            claude_client.supported_claude_flags(["--strict-mcp-config", "--safe-mode"]),
        )

    def test_claude_flags_survive_a_failed_capability_probe(self):
        """问不到 help 时保持既有行为，不为一次探测失败退回高开销模式。"""
        from delivery_bridge.clients import claude as claude_client

        self.stub_claude_help(set())
        self.assertEqual(
            ["--strict-mcp-config", "--safe-mode"],
            claude_client.supported_claude_flags(["--strict-mcp-config", "--safe-mode"]),
        )

    def test_claude_lightweight_sessions_drop_the_project_context(self):
        """起标题这类内务回合不读项目：默认系统提示、CLAUDE.md 和技能清单都不该为它加载。"""
        from delivery_bridge.clients import claude as claude_client
        self.stub_claude_help()

        with tempfile.TemporaryDirectory() as workspace:
            store = bridge.ClaudeTranscriptStore(Path(workspace) / "transcripts")
            client = bridge.ClaudeCLIClient(Path(workspace), transcripts=store, lightweight=True)
            with patch.object(claude_client.shutil, "which", return_value="/usr/local/bin/claude"):
                with patch.object(claude_client.subprocess, "Popen") as popen:
                    with patch.object(claude_client.threading, "Thread"):
                        client._start("起个标题")

        command = popen.call_args.args[0]
        self.assertIn("--safe-mode", command)
        self.assertIn("--disable-slash-commands", command)
        self.assertIn("--no-session-persistence", command)
        self.assertEqual(
            claude_client.LIGHTWEIGHT_SYSTEM_PROMPT,
            command[command.index("--system-prompt") + 1],
        )

    def test_codex_lightweight_threads_are_ephemeral_and_read_only(self):
        client = bridge.AppServerClient.__new__(bridge.AppServerClient)
        client.workspace = Path("/tmp/delivery-lightweight")
        client.lightweight = True
        requests = []
        client.send = lambda method, request_id, params: requests.append((method, request_id, params))
        client.wait_response = unittest.mock.MagicMock(
            side_effect=[{"thread": {"id": "thread-1"}}, {}, {"turn": {"id": "turn-1"}}]
        )

        client.start_task("聊天自动命名", "起个标题")

        self.assertTrue(requests[0][2]["ephemeral"])
        self.assertEqual("read-only", requests[0][2]["sandbox"])
        self.assertEqual({"type": "readOnly"}, requests[2][2]["sandboxPolicy"])

    def test_lightweight_naming_picks_the_cheapest_tier_per_executor(self):
        """起一行标题不跟主会话的模型和推理档位；Codex 没有更便宜的档位就沿用原模型。"""
        self.assertEqual("haiku", bridge.lightweight_model("claude", "opus"))
        self.assertEqual("gpt-5.6-terra", bridge.lightweight_model("codex", "gpt-5.6-terra"))
        self.assertEqual("low", bridge.lightweight_reasoning_effort("claude"))
        self.assertEqual("minimal", bridge.lightweight_reasoning_effort("codex"))

    def test_lightweight_workspace_is_an_empty_directory_outside_the_project(self):
        workspace = bridge.factory.lightweight_workspace()

        self.assertTrue(workspace.is_dir())
        self.assertEqual([], list(workspace.iterdir()))

    def test_codex_app_server_turns_off_every_configured_mcp_server(self):
        with tempfile.TemporaryDirectory() as home:
            (Path(home) / "config.toml").write_text(
                '[mcp_servers.pencil]\ncommand = "pencil"\n\n'
                '[mcp_servers.pencil.env]\nA = "1"\n\n'
                '[mcp_servers."node-repl"]\ncommand = "node_repl"\n\n'
                '[projects."/tmp/x"]\ntrust_level = "trusted"\n',
                encoding="utf-8",
            )

            names = bridge.codex_cli.codex_mcp_server_names(Path(home))
            overrides = bridge.codex_cli.codex_mcp_disable_overrides(Path(home))

        # 同一个服务的 [x] 和 [x.env] 两节只算一次，projects 之类的其他表不能混进来。
        self.assertEqual(["pencil", "node-repl"], names)
        self.assertEqual(
            ["-c", "mcp_servers.pencil.enabled=false", "-c", "mcp_servers.node-repl.enabled=false"],
            overrides,
        )

    def test_codex_mcp_overrides_are_empty_without_a_config_file(self):
        with tempfile.TemporaryDirectory() as home:
            self.assertEqual([], bridge.codex_cli.codex_mcp_disable_overrides(Path(home)))

    def test_requirement_document_catalog_lists_only_dependency_directories(self):
        """前置任务给目录，非前置任务一条都不给：列出来模型就会挨个打开。"""
        task = {"itemKey": "task-c", "requirementKey": "req-a", "moduleKey": "svc", "dependsOnItemKeys": ["task-a"]}
        items = [
            {"itemKey": "task-a", "title": "上游接口", "requirementKey": "req-a", "moduleKey": "svc", "status": "done"},
            {"itemKey": "task-b", "title": "同需求但不是前置", "requirementKey": "req-a", "moduleKey": "svc"},
            {"itemKey": "task-d", "title": "别的需求", "requirementKey": "req-b", "moduleKey": "svc"},
        ]
        with tempfile.TemporaryDirectory() as workspace:
            for item_key in ("task-a", "task-b", "task-d"):
                document = Path(workspace) / "doc" / "svc" / item_key / "文档.md"
                document.parent.mkdir(parents=True, exist_ok=True)
                document.write_text("需求", encoding="utf-8")

            catalog = bridge.requirement_document_catalog(items, task, Path(workspace))

        self.assertEqual(["- task-a: 上游接口（已完成） → `doc/svc/task-a/`"], catalog)
        rendered = "\n".join(bridge.sibling_document_lines(catalog))
        self.assertIn("前置任务的文档目录（只给目录，默认不要打开）", rendered)
        self.assertNotIn("文档.md", rendered)

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
                with patch.object(bridge.hostinfo, "host_platform", return_value="macos"):
                    with patch.object(bridge.codex_cli, "codex_desktop_resource_paths", return_value=[desktop]):
                        with patch.object(bridge.codex_cli, "codex_cli_version", side_effect=lambda command: versions.get(command, "")):
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
                with patch.object(bridge.hostinfo, "host_platform", return_value="macos"):
                    with patch.object(bridge.codex_cli, "codex_desktop_resource_paths", return_value=[desktop]):
                        with patch.object(bridge.codex_cli, "codex_cli_version", side_effect=lambda command: versions.get(command, "")):
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
        # 不要推理摘要：面板不展示推理，摘要本身也是要付钱的输出 token。
        self.assertEqual("none", requests[2][2]["summary"])

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
        self.assertEqual("none", requests[1][2]["summary"])

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

    def test_claude_consume_stores_the_turn_usage_on_the_turn(self):
        events = [
            {"type": "system", "subtype": "init", "session_id": "s-1"},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "做完了"}]}},
            {
                "type": "result", "subtype": "success", "is_error": False, "result": "做完了",
                "total_cost_usd": 0.21,
                "usage": {"input_tokens": 100, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 900, "output_tokens": 50},
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
            client.process.wait.return_value = 0

            client._consume(turn)

        self.assertEqual(1000, turn["usage"]["inputTokens"])
        self.assertEqual(900, turn["usage"]["cachedInputTokens"])
        self.assertEqual(50, turn["usage"]["outputTokens"])
        self.assertEqual(0.21, turn["usage"]["costUsd"])

    def test_claude_consume_tracks_the_context_window_on_the_turn(self):
        """上下文跟着最近一次模型请求走；窗口大小以 result 里 modelUsage 报的为准。"""
        events = [
            {"type": "system", "subtype": "init", "session_id": "s-1"},
            {"type": "assistant", "message": {
                "model": "claude-opus-5",
                "content": [{"type": "text", "text": "在看"}],
                "usage": {"input_tokens": 10, "cache_creation_input_tokens": 20000, "cache_read_input_tokens": 0, "output_tokens": 90},
            }},
            {"type": "assistant", "message": {
                "model": "claude-opus-5",
                "content": [{"type": "text", "text": "做完了"}],
                "usage": {"input_tokens": 30, "cache_creation_input_tokens": 5000, "cache_read_input_tokens": 20000, "output_tokens": 120},
            }},
            {
                "type": "result", "subtype": "success", "is_error": False, "result": "做完了",
                "usage": {"input_tokens": 40, "cache_creation_input_tokens": 25000, "cache_read_input_tokens": 20000, "output_tokens": 210},
                "modelUsage": {"claude-opus-5": {"contextWindow": 200000}},
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
            client.process.wait.return_value = 0

            client._consume(turn)

        # 后一次请求盖前一次：占用是此刻提示词的长度，不是两次相加。
        self.assertEqual(25150, turn["context"]["usedTokens"])
        self.assertEqual(200000, turn["context"]["windowTokens"])
        self.assertEqual("claude-opus-5", turn["context"]["model"])

    def test_merged_journal_turns_keep_the_recorded_usage(self):
        """`thread/read` 的回合里没有用量字段，合并时不能把记录里的那份丢掉。"""
        usage = {"inputTokens": 10, "cachedInputTokens": 0, "outputTokens": 5, "reasoningOutputTokens": 0, "totalTokens": 15, "costUsd": None}
        thread = {"turns": [{"id": "turn-1", "status": "completed", "items": [{"id": "a1", "type": "agentMessage", "text": "做完了"}]}]}

        merged = bridge.merge_journal_turns(thread, [{"id": "turn-1", "status": "completed", "items": [], "usage": usage}])

        self.assertEqual(usage, merged["turns"][0]["usage"])

    def test_every_conversation_payload_carries_the_current_context_window(self):
        """每个对话窗口都要能回答「上下文用了多少、还剩多少、总共多少」。"""
        payload = bridge.with_usage({
            "turns": [
                {"id": "t1", "context": {"usedTokens": 30000, "windowTokens": 200000, "provider": "claude", "model": "claude-opus-5"}},
                # 起标题这类回合没有读数，不能把它当成「上下文清空了」。
                {"id": "t2"},
                {"id": "t3", "context": {"usedTokens": 120000, "windowTokens": 200000, "provider": "claude", "model": "claude-opus-5"}},
            ],
        })

        self.assertEqual(120000, payload["context"]["usedTokens"])
        self.assertEqual(80000, payload["context"]["remainingTokens"])
        self.assertEqual(200000, payload["context"]["windowTokens"])
        self.assertEqual(60.0, payload["context"]["usedPercent"])
        # 一轮都没跑过的会话给一份零值，面板据此按当前选中的模型自己补「总共多少」。
        self.assertEqual(0, bridge.with_usage({"turns": []})["context"]["windowTokens"])

    def test_codex_context_reading_is_the_last_request_not_the_thread_total(self):
        """上下文是此刻提示词的长度：只有最近一次请求算数，线程累计不是它。"""
        params = {
            "tokenUsage": {
                "last": {"inputTokens": 90000, "cachedInputTokens": 80000, "outputTokens": 1000, "totalTokens": 91000},
                "total": {"inputTokens": 500000, "outputTokens": 9000, "totalTokens": 509000},
                "modelContextWindow": 272000,
            },
        }

        context = bridge.codex_turn_context(params, "gpt-5.6-terra")

        self.assertEqual(91000, context["usedTokens"])
        self.assertEqual(272000, context["windowTokens"])
        self.assertEqual("gpt-5.6-terra", context["model"])
        # 执行器没报窗口时按模型兜底，面板不会显示成「总共 0」。
        self.assertEqual(
            bridge.CODEX_DEFAULT_CONTEXT_WINDOW,
            bridge.codex_turn_context({"tokenUsage": {"last": {"totalTokens": 100}}})["windowTokens"],
        )

    def test_claude_context_reading_follows_the_last_model_request(self):
        """Claude 的一次请求 = 一条 assistant 事件：输入三段加输出才是占住的上下文。"""
        event = {
            "message": {
                "model": "claude-opus-5",
                "usage": {
                    "input_tokens": 12,
                    "cache_creation_input_tokens": 6000,
                    "cache_read_input_tokens": 40000,
                    "output_tokens": 300,
                },
            },
        }

        context = bridge.claude_message_context(event)

        self.assertEqual(46312, context["usedTokens"])
        # 回合跑到一半时窗口只能查表；result 事件到了再用执行器报的真值覆盖。
        self.assertEqual(bridge.CLAUDE_DEFAULT_CONTEXT_WINDOW, context["windowTokens"])
        self.assertEqual(
            200000,
            bridge.claude_context_window({"modelUsage": {"claude-opus-5": {"contextWindow": 200000}}}, "claude-opus-5"),
        )
        # 一轮里用过小模型时按主模型取，取不到就退回最大的那个。
        self.assertEqual(
            1000000,
            bridge.claude_context_window({"modelUsage": {
                "claude-haiku-4-5": {"contextWindow": 200000},
                "claude-sonnet-5": {"contextWindow": 1000000},
            }}),
        )

    def test_merged_journal_turns_keep_the_recorded_context(self):
        """上下文读数和执行过程一样只在实时流里出现，合并时不能丢。"""
        context = {"usedTokens": 51000, "windowTokens": 272000, "provider": "codex", "model": "gpt-5.6-terra"}
        thread = {"turns": [{"id": "turn-1", "status": "completed", "items": []}]}

        merged = bridge.merge_journal_turns(thread, [{"id": "turn-1", "status": "completed", "items": [], "context": context}])

        self.assertEqual(context, merged["turns"][0]["context"])

    def test_requirement_sessions_ask_for_reasoning_summaries_and_task_runs_do_not(self):
        """需求对话读的是模型怎么想的，任务执行读的是命令和结论——摘要按会话类型开关。"""
        created = []

        class RecordingClient:
            def __init__(self, workspace, event_callback=None, environment=None, lightweight=False, show_reasoning=False):
                created.append(show_reasoning)

        with patch.object(bridge.factory.codex, "AppServerClient", RecordingClient):
            bridge.factory.create_ai_client("codex", Path("/tmp/x"), show_reasoning=True)
            bridge.factory.create_ai_client("codex", Path("/tmp/x"))

        self.assertEqual([True, False], created)

        client = bridge.AppServerClient.__new__(bridge.AppServerClient)
        client.workspace = Path("/tmp/delivery-workspace")
        client.reasoning_summary = bridge.TURN_REASONING_SUMMARY_SHOWN
        requests = []
        client.send = lambda method, request_id, params: requests.append((method, request_id, params))
        client.wait_response = unittest.mock.MagicMock(side_effect=[{"thread": {"id": "t-1"}}, {}, {"turn": {"id": "turn-1"}}])

        client.start_task("需求拆解", "把这条需求拆成任务")

        self.assertEqual("detailed", requests[2][2]["summary"])
        # 没走 __init__ 的实例按类属性兜底，也就是任务执行那一档：不展示推理。
        self.assertEqual("none", bridge.AppServerClient.__new__(bridge.AppServerClient).reasoning_summary)

    def test_every_conversation_payload_carries_a_usage_total(self):
        """需求侧和任务侧的会话返回结构各不相同，但都得带上这条会话的合计。"""
        payload = bridge.with_usage({
            "programId": 1,
            "turns": [
                {"id": "t1", "usage": {"inputTokens": 100, "cachedInputTokens": 0, "outputTokens": 20, "reasoningOutputTokens": 0, "totalTokens": 120, "costUsd": None}},
                {"id": "t2", "usage": {"inputTokens": 300, "cachedInputTokens": 200, "outputTokens": 30, "reasoningOutputTokens": 0, "totalTokens": 330, "costUsd": None}},
            ],
        })

        self.assertEqual(450, payload["usage"]["totalTokens"])
        self.assertEqual(200, payload["usage"]["cachedInputTokens"])
        # 没有回合的会话给一份零值，面板不必判断字段在不在。
        self.assertEqual(0, bridge.with_usage({"turns": []})["usage"]["totalTokens"])

    def test_requirement_usage_splits_conversations_and_tasks_by_executor(self):
        """需求总账 = 需求侧会话 + 每条任务；线程归哪个执行器，按正文落在哪个缓存里认。"""
        def usage(total):
            return {"inputTokens": total, "cachedInputTokens": 0, "outputTokens": 0, "reasoningOutputTokens": 0, "totalTokens": total, "costUsd": None}

        stored = {
            # 需求拆解走 Codex，任务 a 的执行会话走 Claude，任务 a 还有一条早期的 Codex 会话。
            "codex": {"thread-planning": usage(1000), "thread-a-old": usage(300), "thread-review": usage(70)},
            "claude": {"thread-a": usage(500), "thread-testing": usage(90)},
        }

        def request_api(_config, method, path, query=None, body=None):
            if path == "/delivery/requirement/planning-sessions":
                return [{"threadId": "thread-planning"}]
            if path == "/delivery/requirement/testing-sessions":
                return [
                    {"threadId": "thread-review", "metadata": {"kind": "requirement-review"}},
                    {"threadId": "thread-testing"},
                ]
            if path == "/delivery/item/execution-session":
                if query["itemKey"] == "task-a":
                    return [{"externalSessionId": "thread-a", "metadata": {"conversations": [{"threadId": "thread-a-old"}]}}]
                return []
            self.fail(f"unexpected request: {method} {path}")

        context = {
            "program": {"programId": 1},
            "items": [
                {"itemKey": "task-a", "title": "接口", "requirementKey": "req-a", "phase": "development", "status": "done"},
                {"itemKey": "task-b", "title": "还没跑过", "requirementKey": "req-a", "phase": "requirement", "status": "todo"},
                {"itemKey": "task-x", "title": "别的需求", "requirementKey": "req-b"},
            ],
        }

        def thread_usage(thread_id):
            for provider, threads in stored.items():
                if thread_id in threads:
                    return provider, threads[thread_id]
            return "", bridge.empty_usage()

        with (
            patch.object(bridge.planner, "request_api", side_effect=request_api),
            patch.object(bridge.planner, "project_context", return_value=context),
            patch.object(bridge.usage_index, "thread_usage", side_effect=thread_usage),
        ):
            executor = bridge.ExecutionBridge(Path.cwd())
            result = executor.requirement_usage(1, "req-a", config={"_project_id": 1})

        # 需求会话现在含两张表：拆解在拆解会话表，review 和需求测试在测试会话表。
        self.assertEqual(1070, result["conversations"]["codex"]["totalTokens"])
        self.assertEqual(90, result["conversations"]["claude"]["totalTokens"])
        tasks = {task["itemKey"]: task for task in result["tasks"]}
        # 别的需求的任务不算进来。
        self.assertEqual(["task-a", "task-b"], sorted(tasks))
        self.assertEqual(300, tasks["task-a"]["usage"]["codex"]["totalTokens"])
        self.assertEqual(500, tasks["task-a"]["usage"]["claude"]["totalTokens"])
        self.assertEqual(800, tasks["task-a"]["usage"]["total"]["totalTokens"])
        # 没跑过的任务给零值，面板照样能画两行。
        self.assertEqual(0, tasks["task-b"]["usage"]["total"]["totalTokens"])
        self.assertEqual(1370, result["usage"]["codex"]["totalTokens"])
        self.assertEqual(590, result["usage"]["claude"]["totalTokens"])
        self.assertEqual(1960, result["usage"]["total"]["totalTokens"])

    def test_requirement_usage_splits_requirement_sessions_by_block(self):
        """需求分析、拆解、原型、review、需求测试、微调各算各的；没跑过的块也留一行零值。"""
        def usage(total):
            return {"inputTokens": total, "cachedInputTokens": 0, "outputTokens": 0, "reasoningOutputTokens": 0, "totalTokens": total, "costUsd": None}

        stored = {
            "thread-planning": usage(1000),
            "thread-prototype": usage(200),
            "thread-analysis": usage(300),
            "thread-review": usage(70),
            "thread-testing": usage(90),
            "thread-fine-tuning": usage(40),
            "thread-legacy-testing": usage(5),
        }

        def request_api(_config, method, path, query=None, body=None):
            if path == "/delivery/requirement/planning-sessions":
                return [
                    {"threadId": "thread-planning", "executorType": "codex"},
                    # 原型会话跟拆解共用一张表，只有执行器类型的用途后缀能认出来。
                    {"threadId": "thread-prototype", "executorType": "claude-prototype"},
                ]
            if path == "/delivery/requirement/testing-sessions":
                return [
                    {"threadId": "thread-analysis", "metadata": {"kind": "requirement-analysis"}},
                    {"threadId": "thread-review", "metadata": {"kind": "requirement-review"}},
                    {"threadId": "thread-testing", "metadata": {"kind": "requirement-testing"}},
                    {"threadId": "thread-fine-tuning", "metadata": {"kind": "requirement-fine-tuning"}},
                    # 老数据没写 kind，按需求测试算。
                    {"threadId": "thread-legacy-testing"},
                ]
            if path == "/delivery/item/execution-session":
                return []
            self.fail(f"unexpected request: {method} {path}")

        context = {"program": {"programId": 1}, "items": []}

        with (
            patch.object(bridge.planner, "request_api", side_effect=request_api),
            patch.object(bridge.planner, "project_context", return_value=context),
            patch.object(bridge.usage_index, "thread_usage", side_effect=lambda thread_id: ("codex", stored[thread_id]) if thread_id in stored else ("", bridge.empty_usage())),
        ):
            executor = bridge.ExecutionBridge(Path.cwd())
            result = executor.requirement_usage(1, "req-a", config={"_project_id": 1})

        groups = {group["key"]: group for group in result["conversationGroups"]}
        self.assertEqual(
            ["analysis", "planning", "prototype", "review", "testing", "fineTuning"],
            [group["key"] for group in result["conversationGroups"]],
        )
        self.assertEqual(300, groups["analysis"]["usage"]["total"]["totalTokens"])
        self.assertEqual(1000, groups["planning"]["usage"]["total"]["totalTokens"])
        self.assertEqual(200, groups["prototype"]["usage"]["total"]["totalTokens"])
        self.assertEqual(70, groups["review"]["usage"]["total"]["totalTokens"])
        self.assertEqual(95, groups["testing"]["usage"]["total"]["totalTokens"])
        self.assertEqual(2, groups["testing"]["threads"])
        self.assertEqual(40, groups["fineTuning"]["usage"]["total"]["totalTokens"])
        # 各块之和就是需求会话合计，别让面板上下两个数对不上。
        self.assertEqual(1705, result["conversations"]["total"]["totalTokens"])

    def test_thread_usage_reads_whichever_executor_cache_holds_the_thread(self):
        """会话表里的 executorType 可能过时；正文在哪个缓存里，就算哪个执行器的。"""
        usage = {"inputTokens": 40, "cachedInputTokens": 0, "outputTokens": 10, "reasoningOutputTokens": 0, "totalTokens": 50, "costUsd": None}
        with tempfile.TemporaryDirectory() as directory:
            journal = bridge.ThreadItemJournal(Path(directory) / "codex")
            transcripts = bridge.ClaudeTranscriptStore(Path(directory) / "claude")
            journal._write("thread-codex", [{"id": "t1", "items": [], "usage": usage}])
            transcripts.write("thread-claude", [{"id": "t1", "items": [], "usage": usage}])

            with (
                patch.object(bridge.usage_index, "THREAD_ITEMS", journal),
                patch.object(bridge.usage_index, "CLAUDE_TRANSCRIPTS", transcripts),
            ):
                self.assertEqual("codex", bridge.usage_index.thread_usage("thread-codex")[0])
                self.assertEqual("claude", bridge.usage_index.thread_usage("thread-claude")[0])
                self.assertEqual(("", bridge.empty_usage()), bridge.usage_index.thread_usage("thread-missing"))

    def test_thread_item_journal_records_the_turn_token_usage(self):
        """回合用量只能拿线程累计去减：`last` 是最近一次模型请求，一个回合里请求好多次。"""
        with tempfile.TemporaryDirectory() as directory:
            journal = bridge.ThreadItemJournal(Path(directory))
            journal.record({"method": "turn/started", "params": {"threadId": "t-u", "turn": {"id": "turn-1"}}})
            # 本轮开始前线程已经累计了 1000/100；两次通知分别是本轮第一次和第二次请求。
            for total_input, total_output, last_input, last_output in ((1600, 140, 600, 40), (2300, 190, 700, 50)):
                journal.record({
                    "method": "thread/tokenUsage/updated",
                    "params": {
                        "threadId": "t-u", "turnId": "turn-1",
                        "tokenUsage": {
                            "total": {
                                "inputTokens": total_input, "cachedInputTokens": 0, "outputTokens": total_output,
                                "reasoningOutputTokens": 0, "totalTokens": total_input + total_output,
                            },
                            "last": {
                                "inputTokens": last_input, "cachedInputTokens": 0, "outputTokens": last_output,
                                "reasoningOutputTokens": 0, "totalTokens": last_input + last_output,
                            },
                        },
                    },
                })
            usage = journal.read("t-u")[0]["usage"]

        # 1000/100 是本轮之前的累计，两次请求之后本轮自己烧了 1300 输入 / 90 输出。
        self.assertEqual(1300, usage["inputTokens"])
        self.assertEqual(90, usage["outputTokens"])
        self.assertEqual(1390, usage["totalTokens"])

    def test_thread_item_journal_records_the_context_window(self):
        """同一条用量通知还带上下文占用：它按最近一次请求算，和累计用量各记各的。"""
        with tempfile.TemporaryDirectory() as directory:
            journal = bridge.ThreadItemJournal(Path(directory))
            journal.note_model("t-c", "gpt-5.6-terra")
            journal.record({"method": "turn/started", "params": {"threadId": "t-c", "turn": {"id": "turn-1"}}})
            journal.record({
                "method": "thread/tokenUsage/updated",
                "params": {
                    "threadId": "t-c", "turnId": "turn-1",
                    "tokenUsage": {
                        "total": {"inputTokens": 120000, "outputTokens": 4000, "totalTokens": 124000},
                        "last": {"inputTokens": 60000, "cachedInputTokens": 55000, "outputTokens": 800, "totalTokens": 60800},
                        "modelContextWindow": 272000,
                    },
                },
            })
            context = journal.read("t-c")[0]["context"]

        self.assertEqual(60800, context["usedTokens"])
        self.assertEqual(272000, context["windowTokens"])
        self.assertEqual("gpt-5.6-terra", context["model"])

    def test_claude_result_event_carries_the_turn_token_usage(self):
        event = {
            "type": "result", "subtype": "success", "result": "做完了", "total_cost_usd": 0.42,
            "usage": {
                "input_tokens": 300, "cache_creation_input_tokens": 1200, "cache_read_input_tokens": 9000,
                "output_tokens": 800, "output_tokens_details": {"thinking_tokens": 120},
            },
        }

        usage = bridge.claude_turn_usage(event)

        # Claude 把新输入、写缓存、读缓存分三段报，面板要的是「这轮送进去多少」。
        self.assertEqual(10500, usage["inputTokens"])
        self.assertEqual(9000, usage["cachedInputTokens"])
        self.assertEqual(800, usage["outputTokens"])
        self.assertEqual(120, usage["reasoningOutputTokens"])
        self.assertEqual(11300, usage["totalTokens"])
        self.assertEqual(0.42, usage["costUsd"])

    def test_serialized_turns_carry_the_context_reading_through_to_the_panel(self):
        """会话正文要重新投影一遍才发给面板；上下文读数不能在这一步被过滤掉。"""
        turns = bridge.serialize_turns([
            {
                "id": "turn-1", "status": "completed",
                "items": [{"id": "a1", "type": "agentMessage", "text": "做完了"}],
                "context": {"usedTokens": 88000, "windowTokens": 272000, "provider": "codex", "model": "gpt-5.6-terra"},
            },
            {"id": "turn-2", "status": "completed", "items": [{"id": "a2", "type": "agentMessage", "text": "再来一轮"}]},
        ])

        self.assertEqual(88000, turns[0]["context"]["usedTokens"])
        # 没测到读数的回合不给字段，会话级读数据此往前找上一条。
        self.assertNotIn("context", turns[1])
        self.assertEqual(88000, bridge.with_usage({"turns": turns})["context"]["usedTokens"])

    def test_serialized_turns_carry_usage_only_when_the_executor_reported_it(self):
        turns = bridge.serialize_turns([
            {
                "id": "turn-1", "status": "completed",
                "items": [{"id": "a1", "type": "agentMessage", "text": "做完了"}],
                "usage": {"inputTokens": 10, "cachedInputTokens": 0, "outputTokens": 5, "reasoningOutputTokens": 0, "totalTokens": 15, "costUsd": None},
            },
            {"id": "turn-2", "status": "completed", "items": [{"id": "a2", "type": "agentMessage", "text": "再来一轮"}]},
        ])

        self.assertEqual(15, turns[0]["usage"]["totalTokens"])
        # 老会话读不出用量，就别给字段，面板据此不显示这一行。
        self.assertNotIn("usage", turns[1])
        total = bridge.turns_usage_total(turns)
        self.assertEqual(15, total["totalTokens"])

    def test_turn_usage_totals_add_up_across_turns_and_keep_cost_optional(self):
        total = bridge.merge_usage([
            {"inputTokens": 100, "cachedInputTokens": 40, "outputTokens": 20, "reasoningOutputTokens": 5, "totalTokens": 120, "costUsd": 0.1},
            {"inputTokens": 200, "cachedInputTokens": 60, "outputTokens": 30, "reasoningOutputTokens": 0, "totalTokens": 230, "costUsd": None},
        ])

        self.assertEqual(300, total["inputTokens"])
        self.assertEqual(100, total["cachedInputTokens"])
        self.assertEqual(350, total["totalTokens"])
        # Codex 不报钱：能算的那部分照样给出来，不因为缺一半就整个丢掉。
        self.assertEqual(0.1, total["costUsd"])
        self.assertIsNone(bridge.merge_usage([])["costUsd"])

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

            def read_thread(self, thread_id, request_id, timeout=20):
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

                def read_thread(self, _thread_id, request_id=None, timeout=20):
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

                def read_thread(self, _thread_id, request_id=None, timeout=20):
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

            def read_thread(self, _thread_id, request_id=None, timeout=20):
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
            index = bridge.CloudDocumentIndex(
                [{"requirementKey": "req-api"}],
                [{"itemKey": "task-a", "requirementKey": "req-api", "moduleKey": "core"}],
            )
            entries, skipped = bridge.cloud_sync_workspace_entries(workspace, {"chat", "prototype"}, None, index)

        self.assertEqual(0, skipped)
        self.assertEqual(
            [("chat", "chat/requirements/req-api/task/聊天.md"), ("prototype", "doc/core/task-a/prototype/index.html")],
            [(entry.category, entry.relative_path) for entry in entries],
        )
        # 归属和阶段跟着文件一起上云，面板才能把需求文档和任务文档分开展示。
        self.assertEqual(
            [("requirement", "req-api", "chat"), ("task", "task-a", "prototype")],
            [(entry.owner_kind, entry.owner_key, entry.stage) for entry in entries],
        )

    def test_cloud_sync_splits_requirement_documents_by_stage(self):
        """一条需求的文档要按阶段分开：拆解、原型、评审、测试、微调各归各的。"""
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            for relative, content in [
                ("doc/requirements/req-api/需求大纲.md", "outline"),
                ("doc/requirements/req-api/prototype/index.html", "<main/>"),
                ("doc/review/req-api/review报告.md", "review"),
                ("doc/test/req-api/测试用例.md", "cases"),
                ("doc/fine-tuning/req-api/微调记录.md", "tuning"),
            ]:
                path = workspace / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
            index = bridge.CloudDocumentIndex([{"requirementKey": "req-api"}], [])
            entries, skipped = bridge.cloud_sync_workspace_entries(
                workspace, {"requirement", "prototype", "test"}, None, index,
            )

        self.assertEqual(0, skipped)
        self.assertEqual(
            {
                "doc/requirements/req-api/需求大纲.md": ("requirement", "req-api", "outline"),
                "doc/requirements/req-api/prototype/index.html": ("requirement", "req-api", "prototype"),
                "doc/review/req-api/review报告.md": ("requirement", "req-api", "review"),
                "doc/test/req-api/测试用例.md": ("requirement", "req-api", "testing"),
                "doc/fine-tuning/req-api/微调记录.md": ("requirement", "req-api", "fine-tuning"),
            },
            {entry.relative_path: (entry.owner_kind, entry.owner_key, entry.stage) for entry in entries},
        )

    def test_cloud_sync_does_not_invent_an_owner_the_board_never_reported(self):
        """面板没给过的键不能凭空变成一条需求或任务的分组。"""
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            path = workspace / "doc" / "unknown-module" / "unknown-task" / "文档.md"
            path.parent.mkdir(parents=True)
            path.write_text("orphan", encoding="utf-8")
            entries, _skipped = bridge.cloud_sync_workspace_entries(
                workspace, {"requirement"}, None, bridge.CloudDocumentIndex([], []),
            )

        self.assertEqual([("program", "", "requirement")], [(e.owner_kind, e.owner_key, e.stage) for e in entries])

    def test_cloud_sync_includes_test_execution_and_owning_attachments_only(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / "doc" / "test" / "task-a").mkdir(parents=True)
            (workspace / "doc" / "test" / "task-a" / "用例.md").write_text("test", encoding="utf-8")
            (workspace / "server").mkdir()
            (workspace / "server" / "result.txt").write_text("execution", encoding="utf-8")
            attachment_root = workspace / ".codex" / "delivery-task-attachments"
            artifact_root = workspace / ".codex" / "delivery-task-artifacts"
            attachment_root.mkdir(parents=True)
            artifact_root.mkdir(parents=True)
            (attachment_root / "att-16.png").write_bytes(b"image")
            (attachment_root / "att-16.json").write_text(json.dumps({
                "id": "att-16", "programId": 16, "itemKey": "task-a", "name": "结果图.png", "fileName": "att-16.png",
            }), encoding="utf-8")
            (attachment_root / "att-other.txt").write_text("other", encoding="utf-8")
            (attachment_root / "att-other.json").write_text(json.dumps({
                "id": "att-other", "programId": 17, "itemKey": "task-b", "name": "其他.txt", "fileName": "att-other.txt",
            }), encoding="utf-8")
            (artifact_root / "artifact-16.json").write_text(json.dumps({
                "id": "artifact-16", "programId": 16, "itemKey": "task-a", "relativePath": "server/result.txt",
            }), encoding="utf-8")

            index = bridge.CloudDocumentIndex([], [{"itemKey": "task-a", "moduleKey": "core"}])
            entries, skipped = bridge.cloud_sync_workspace_entries(
                workspace, {"test", "execution", "attachment"}, 16, index,
            )

        self.assertEqual(0, skipped)
        self.assertEqual(
            [
                ("attachment", "attachments/task-a/att-16-结果图.png"),
                ("execution", "execution/task-a/server/result.txt"),
                ("test", "doc/test/task-a/用例.md"),
            ],
            [(entry.category, entry.relative_path) for entry in entries],
        )
        self.assertEqual(
            [("task", "task-a", "attachment"), ("task", "task-a", "execution"), ("task", "task-a", "testing")],
            [(entry.owner_kind, entry.owner_key, entry.stage) for entry in entries],
        )

    def test_cloud_chat_sync_does_not_require_git_or_create_a_workspace_archive(self):
        class Client:
            def next_request_id(self):
                return 1

            def read_thread(self, _thread_id, request_id=None, timeout=20):
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
        with patch.object(bridge.execution.turns, "requirement_prototype_files", return_value=("doc/requirements/req-a/prototype", prototype)):
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

    def test_conversation_mention_context_truncates_long_details_and_gives_the_path(self):
        """@ 三四个需求就是几万字上下文，其中真正被用到的往往只有一两段。"""
        executor = bridge.ExecutionBridge(Path.cwd())
        requirement = {"requirementKey": "req-a", "name": "关联需求", "detail": "需" * 9000}
        task = {
            "itemKey": "task-a", "title": "关联任务", "description": "说" * 9000,
            "requirementKey": "", "moduleKey": "web",
        }
        with (
            patch.object(bridge.planner, "requirement_record", return_value=requirement),
            patch.object(executor, "_task_detail", return_value=task),
        ):
            lines = executor._conversation_mention_context(
                {"api_url": "http://test/api", "key": "k"},
                1,
                [{"kind": "requirement", "key": "req-a"}, {"kind": "task", "key": "task-a"}],
                {"items": []},
            )

        rendered = "\n".join(lines)
        self.assertIn("需求大纲: doc/requirements/req-a/需求大纲.md", rendered)
        self.assertIn("全文见 `doc/requirements/req-a/需求大纲.md`", rendered)
        self.assertIn("全文见 `doc/web/task-a/文档.md`", rendered)
        self.assertLess(rendered.count("需"), 3000)
        self.assertLess(rendered.count("说"), 3000)

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
            patch.object(bridge.clients.codex, "AppServerClient", return_value=reader),
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
            patch.object(executor, "_cancel_execution_batches", return_value=["batch-stop"]) as cancel,
        ):
            executor._run_batch("batch-stop", self.runtime_config(), 1, ["a", "b"], "")

        self.assertEqual(["a"], started)
        self.assertEqual("blocked", finalized["status"])
        self.assertIn("停止", finalized["summary"])
        # 停止必须同时让服务端收尾批次，否则任务会被锁在 running 批次里再也启动不了。
        self.assertEqual(1, cancel.call_count)
        # 队列结束后取消标记要清掉，同一个批次号不会永远停在取消态。
        self.assertNotIn("batch-stop", executor.cancelled_queues)

    def test_stop_all_closes_server_batches_even_without_local_queue(self):
        """桥接重启或断网收尾失败后本地什么都不剩，全部停止仍要让服务端关掉僵尸批次。"""
        executor = bridge.ExecutionBridge(Path.cwd())

        with patch.object(executor, "_cancel_execution_batches", return_value=["batch-zombie"]) as cancel:
            result = executor.stop_all_executions({"programId": 1}, config=self.runtime_config())

        self.assertEqual([], result["queueIds"])
        self.assertEqual([], result["itemKeys"])
        self.assertEqual(["batch-zombie"], result["cancelledBatchIds"])
        self.assertEqual(1, cancel.call_count)

    def test_failed_batch_finalize_is_retried_until_it_lands(self):
        """收尾请求丢了就等于任务被永久锁死，所以必须落盘、等网络恢复后补上。"""
        with tempfile.TemporaryDirectory() as directory:
            executor = bridge.ExecutionBridge(Path.cwd())
            executor.pending_batch_finalizes = bridge.PendingBatchFinalizeStore(Path(directory) / "pending-finalize.json")
            attempts: list[dict] = []

            def request_with_retry(_config, path, body):
                attempts.append({"path": path, "body": body})
                if len(attempts) == 1:
                    raise bridge.BridgeFailure("网络不可用")
                return {}

            # setUp 把整个类的收尾方法打了桩，这里要的是真实实现。
            real_finalize = bridge.execution.core.CoreMixin._finalize_execution_batch

            with patch.object(executor, "_request_with_retry", side_effect=request_with_retry):
                with self.assertRaises(bridge.BridgeFailure):
                    real_finalize(executor, self.runtime_config(), 1, "batch-lost", "blocked", "断网")
                # 请求失败，收尾留在盘上等补偿。
                self.assertEqual(["batch-lost"], [entry["batchId"] for entry in executor.pending_batch_finalizes.snapshot()])

                with patch.object(executor, "_execution_batch_status", return_value="running"):
                    executor.reconcile_pending(self.runtime_config(), 1)

            self.assertEqual([], executor.pending_batch_finalizes.snapshot())
            self.assertEqual(
                ["/delivery/execution-batch/finalize", "/delivery/execution-batch/finalize"],
                [attempt["path"] for attempt in attempts],
            )
            self.assertEqual("blocked", attempts[1]["body"]["status"])

    def test_pending_finalize_is_dropped_once_the_batch_is_already_closed(self):
        """批次已经被「全部停止」收尾过了，补偿就该消失，而不是每轮都去撞一次。"""
        with tempfile.TemporaryDirectory() as directory:
            executor = bridge.ExecutionBridge(Path.cwd())
            executor.pending_batch_finalizes = bridge.PendingBatchFinalizeStore(Path(directory) / "pending-finalize.json")
            executor.pending_batch_finalizes.add({"programId": 1, "batchId": "batch-closed", "status": "blocked", "summary": ""})

            with (
                patch.object(executor, "_execution_batch_status", return_value="blocked"),
                patch.object(executor, "_request_with_retry") as request,
            ):
                executor.reconcile_pending(self.runtime_config(), 1)

            request.assert_not_called()
            self.assertEqual([], executor.pending_batch_finalizes.snapshot())

    def test_reconcile_heartbeats_running_queues(self):
        """心跳是服务端判断执行端还活着的唯一依据，队列在跑就必须持续续上。"""
        executor = bridge.ExecutionBridge(Path.cwd())
        executor._remember_config(1, self.runtime_config())
        executor._register_queue("batch-alive", 1)

        with patch.object(executor, "_heartbeat_execution_batches", return_value=["batch-alive"]) as heartbeat:
            executor.reconcile()

        heartbeat.assert_called_once_with(self.runtime_config(), 1, ["batch-alive"])
        self.assertNotIn("batch-alive", executor.cancelled_queues)

    def test_heartbeat_stops_a_queue_the_server_no_longer_runs(self):
        """别处点了全部停止，本地队列要跟着收摊，不能继续往下拉任务。"""
        executor = bridge.ExecutionBridge(Path.cwd())
        executor._remember_config(1, self.runtime_config())
        executor._register_queue("batch-dropped", 1)

        with patch.object(executor, "_heartbeat_execution_batches", return_value=[]):
            executor.reconcile()

        self.assertIn("batch-dropped", executor.cancelled_queues)

    def test_reconcile_without_a_known_identity_sends_nothing(self):
        """没有浏览器给的身份就不许后台扫描，凭证从来不落盘。"""
        executor = bridge.ExecutionBridge(Path.cwd())
        executor._register_queue("batch-orphan", 1)

        with patch.object(executor, "_heartbeat_execution_batches") as heartbeat:
            executor.reconcile()

        heartbeat.assert_not_called()

    def test_stop_all_surfaces_a_failed_batch_cancel(self):
        """服务端没收到收尾就不能报成功，否则用户以为停干净了，再做一次照样被拦。"""
        executor = bridge.ExecutionBridge(Path.cwd())

        with patch.object(executor, "_cancel_execution_batches", side_effect=bridge.BridgeFailure("接口不可用")):
            with self.assertRaises(bridge.BridgeFailure):
                executor.stop_all_executions({"programId": 1}, config=self.runtime_config())

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

    def test_run_sequence_hands_upstream_code_facts_to_the_next_task(self):
        statuses = {"a": "todo", "b": "todo"}
        outputs = {
            "a": "## 进度说明\n\n改完了。\n\n## 代码事实交接\n\n- server/x.go:41 ListItems(ctx, q)\n",
            "b": "",
        }
        received: list[tuple[str, str]] = []
        executor = bridge.ExecutionBridge(Path.cwd())

        def task_detail(_config, _program_id, item_key):
            return {
                "itemKey": item_key, "title": item_key, "version": 1,
                "status": statuses[item_key], "actionOutput": outputs[item_key],
            }

        def execute(payload, config=None):
            item_key = payload["task"]["itemKey"]
            received.append((item_key, str(payload.get("upstreamCodeFacts") or "")))
            statuses[item_key] = "done"
            return {"accepted": True}

        with (
            patch.object(executor, "_task_detail", side_effect=task_detail),
            patch.object(executor, "execute", side_effect=execute),
        ):
            executor._run_sequence("sequence-1", self.runtime_config(), 1, ["a", "b"], "", "codex")

        self.assertEqual("", received[0][1])
        self.assertIn("【a】", received[1][1])
        self.assertIn("server/x.go:41", received[1][1])

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

    def test_task_prompt_prefers_the_requirement_outline_when_the_app_layer_sees_it(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            outline = workspace / "doc" / "requirements" / "req-a" / "需求大纲.md"
            outline.parent.mkdir(parents=True)
            outline.write_text("# 需求", encoding="utf-8")
            document = workspace / "doc" / "svc" / "a" / "文档.md"
            document.parent.mkdir(parents=True)
            document.write_text("# 任务", encoding="utf-8")

            prompt = bridge.build_task_prompt(
                {
                    "programId": 1,
                    "task": {
                        "itemKey": "a", "title": "Build API", "moduleKey": "svc",
                        "requirementKey": "req-a", "phase": "development",
                        "description": "把接口补齐",
                    },
                },
                workspace,
            )

        self.assertIn("需求级文档: `doc/requirements/req-a/需求大纲.md`（应用层已核对存在）", prompt)
        self.assertIn("任务需求文档: `doc/svc/a/文档.md`（应用层已核对存在）", prompt)
        self.assertIn("以需求级文档为准", prompt)

    def test_task_prompt_falls_back_to_the_task_description_when_the_document_is_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            prompt = bridge.build_task_prompt(
                {
                    "programId": 1,
                    "task": {
                        "itemKey": "a", "title": "Build API", "moduleKey": "svc",
                        "requirementKey": "req-a", "phase": "development",
                        "description": "补齐 /delivery/item 的批量接口",
                    },
                },
                Path(directory),
            )

        self.assertIn("本任务没有任务级需求文档", prompt)
        self.assertIn("任务说明（本任务级需求的唯一来源", prompt)
        self.assertIn("补齐 /delivery/item 的批量接口", prompt)
        self.assertNotIn("跨回合累积的文档", prompt)

    def test_development_prompt_requires_writing_the_whole_design_process(self):
        prompt = bridge.build_task_prompt(
            {
                "programId": 1,
                "task": {"itemKey": "a", "title": "Build API", "moduleKey": "svc", "phase": "development"},
            }
        )

        self.assertIn("doc/svc/a/design/", prompt)
        self.assertIn("完整的设计与思考过程", prompt)
        self.assertNotIn(
            "完整的设计与思考过程",
            bridge.build_task_prompt(
                {"programId": 1, "task": {"itemKey": "a", "title": "Build API", "moduleKey": "svc"}}
            ),
        )

    def test_planning_prompt_demands_self_contained_task_descriptions(self):
        prompt = bridge.build_planning_prompt(
            1, {"program": {"name": "Universe"}}, "确认并写入",
            requirement={"requirementKey": "req-a"}, write_allowed=True, thread_id="thread-1",
        )

        self.assertIn("动作执行阶段的兜底需求输入", prompt)
        self.assertIn("references/任务拆解与写入.md", prompt)
        self.assertIn("300–1500 字", planner_skill_text("references/任务拆解与写入.md"))

    def test_task_prompt_asks_batch_items_to_hand_over_code_facts(self):
        prompt = bridge.build_task_prompt(
            {
                "programId": 1,
                "task": {"itemKey": "a", "title": "Build API", "moduleKey": "svc", "phase": "development"},
                "batchMode": True,
            }
        )

        self.assertIn("## 代码事实交接", prompt)
        self.assertNotIn(
            "## 代码事实交接",
            bridge.build_task_prompt(
                {
                    "programId": 1,
                    "task": {"itemKey": "a", "title": "Build API", "moduleKey": "svc", "phase": "development"},
                }
            ),
        )

    def test_task_prompt_carries_upstream_code_facts_when_the_queue_collected_them(self):
        prompt = bridge.build_task_prompt(
            {
                "programId": 1,
                "task": {"itemKey": "b", "title": "Build UI", "moduleKey": "svc", "phase": "development"},
                "upstreamCodeFacts": "【a】\n- server/x.go:41 ListItems(ctx, q) ([]Item, error)",
            }
        )

        self.assertIn("同队列上游任务已确认的代码事实", prompt)
        self.assertIn("server/x.go:41", prompt)
        self.assertNotIn(
            "同队列上游任务已确认的代码事实",
            bridge.build_task_prompt(
                {"programId": 1, "task": {"itemKey": "b", "title": "Build UI", "moduleKey": "svc"}}
            ),
        )

    def test_code_facts_are_read_from_the_handoff_section_only(self):
        output = (
            "## 进度说明\n\n"
            "改了什么：见下。\n\n"
            "## 代码事实交接\n\n"
            "- server/x.go:41 ListItems(ctx, q) ([]Item, error)\n"
            "- 表 zt_task_item 主键 id\n\n"
            "## 遗留与风险\n\n"
            "- 无\n"
        )

        facts = bridge.code_facts_from_output(output)

        self.assertIn("ListItems(ctx, q)", facts)
        self.assertIn("zt_task_item", facts)
        self.assertNotIn("遗留与风险", facts)
        self.assertNotIn("改了什么", facts)
        self.assertEqual("", bridge.code_facts_from_output("## 进度说明\n\n没有交接节。"))

    def test_code_facts_are_capped_so_a_pasted_file_cannot_ride_the_whole_queue(self):
        body = "\n".join(f"- 第 {index} 条事实" for index in range(400))

        facts = bridge.code_facts_from_output(f"## 代码事实交接\n\n{body}", limit=200)

        self.assertLessEqual(len(facts), 200)
        self.assertTrue(facts.startswith("- 第 0 条事实"))
        self.assertNotIn("第 399 条事实", facts)

    def test_task_prompt_forbids_writing_only_the_appended_requirement(self):
        prompt = bridge.build_task_prompt(
            {"programId": 1, "task": {"itemKey": "a", "title": "Build API", "moduleKey": "svc"}}
        )

        self.assertIn("doc/svc/a/文档.md` 是跨回合累积的文档", prompt)
        self.assertIn("按章节定点编辑", prompt)
        self.assertIn("不要把整篇重写一遍", prompt)

    def test_conversation_prompt_carries_the_document_revision_rule(self):
        prompt = bridge.build_conversation_prompt(1, {"itemKey": "a", "moduleKey": "svc", "title": "Build API"}, "再加一条需求")

        self.assertIn("跨回合累积的文档", prompt)
        self.assertIn("更不要用只含本轮增量的内容覆盖整份文件", prompt)

    def test_follow_up_context_repeats_the_phase_document_and_merge_rule(self):
        lines = bridge.follow_up_context_lines({"itemKey": "a", "moduleKey": "svc", "phase": "development"})
        context = "\n".join(lines)

        self.assertIn("doc/svc/a/文档.md", context)
        self.assertIn(bridge.PHASE_SKILLS["development"], context)
        self.assertIn("按章节定点编辑", context)

    def test_follow_up_context_omits_the_document_path_when_the_board_did_not_give_one(self):
        context = "\n".join(bridge.follow_up_context_lines({"itemKey": "a"}))

        self.assertIn("追加回合", context)
        self.assertNotIn("doc/module/a/文档.md", context)

    def test_planning_prompt_requires_merging_the_existing_outline_before_writing_it_back(self):
        prompt = bridge.build_planning_prompt(
            1, {"program": {"name": "Universe"}}, "确认并写入",
            requirement={"requirementKey": "req-a"}, write_allowed=True, thread_id="thread-1",
        )

        self.assertIn("按章节定点编辑", prompt)
        self.assertIn("禁止只把本轮追加的那段需求写进文件覆盖全篇", prompt)

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

    def test_requirement_analysis_prompt_clarifies_before_it_writes(self):
        """澄清回合一个字都不许落盘；确认生成那一轮才允许写分析文档。"""
        requirement = {"requirementKey": "req-a", "name": "结算改造", "detail": "支持分期"}

        clarify = bridge.build_requirement_analysis_prompt(1, requirement, "想做分期结算", Path("/tmp/workspace"))
        generate = bridge.build_requirement_analysis_prompt(
            1, requirement, "信息补齐了", Path("/tmp/workspace"), generate_document=True,
        )

        self.assertIn("不要写任何文档、原型或业务代码", clarify)
        self.assertIn("delivery-requirement-analysis", clarify)
        # 技能不一定挂在工作目录根上，每个子工程的根目录都可能各带一份。
        self.assertIn("子工程", clarify)
        self.assertNotIn("需求分析文档已生成", clarify)
        self.assertIn("doc/analysis/req-a/需求分析.md", generate)
        self.assertIn("需求分析文档已生成", generate)

    def test_requirement_analysis_prompt_only_draws_a_prototype_on_request(self):
        requirement = {"requirementKey": "req-a", "name": "结算改造", "detail": "支持分期"}

        without = bridge.build_requirement_analysis_prompt(1, requirement, "先聊聊", Path("/tmp/workspace"))
        with_prototype = bridge.build_requirement_analysis_prompt(
            1, requirement, "顺便画一版", Path("/tmp/workspace"), generate_prototype=True,
        )

        self.assertIn("本轮不需要画原型", without)
        self.assertIn("doc/requirements/req-a/prototype", with_prototype)

    def test_requirement_analysis_history_is_isolated_from_testing_and_review(self):
        """需求分析和测试、review 共用一张会话表，三边的目录都只看得见自己那一类。"""
        rows = [
            {"threadId": "testing-thread", "title": "总体测试", "executorType": "codex", "metadata": {"kind": "requirement-testing"}},
            {"threadId": "review-thread", "title": "代码 review", "executorType": "codex", "metadata": {"kind": "requirement-review"}},
            {"threadId": "analysis-thread", "title": "需求分析", "executorType": "codex", "metadata": {"kind": "requirement-analysis"}},
        ]

        with tempfile.TemporaryDirectory() as directory:
            executor = bridge.ExecutionBridge(Path(directory))
            with patch.object(bridge.planner, "request_api", return_value=rows):
                analysis = executor._load_requirement_analysis_session(self.runtime_config(), 1, "req-a", "codex")
                testing = executor._load_requirement_testing_session(self.runtime_config(), 1, "req-a", "codex")
                review = executor._load_requirement_review_session(self.runtime_config(), 1, "req-a", "codex")

        self.assertEqual(["analysis-thread"], [entry["threadId"] for entry in analysis["catalog"]])
        self.assertEqual(["testing-thread"], [entry["threadId"] for entry in testing["catalog"]])
        self.assertEqual(["review-thread"], [entry["threadId"] for entry in review["catalog"]])

    def test_requirement_analysis_column_lists_every_analysis_document(self):
        """分析目录支持多份文档，主文档没落盘时也要能选到别的那份。"""
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            column = workspace / "doc/analysis/req-a"
            column.mkdir(parents=True)
            (column / "需求分析.md").write_text("# 分析\n", encoding="utf-8")
            (column / "接口清单.md").write_text("# 接口\n", encoding="utf-8")
            executor = bridge.ExecutionBridge(workspace)
            with patch.object(executor, "_requirement_for_prototype", return_value={"requirementKey": "req-a"}):
                result = executor.document_set(1, "requirement-analysis", "req-a", config=self.runtime_config())

            self.assertEqual("doc/analysis/req-a/需求分析.md", result["primaryPath"])
            self.assertEqual(
                {"doc/analysis/req-a/需求分析.md", "doc/analysis/req-a/接口清单.md"},
                {entry["path"] for entry in result["files"]},
            )

    def test_requirement_analysis_documents_can_be_mentioned_from_the_planning_chat(self):
        """拆解聊天 @ 的分析文档必须真的在这条需求的分析目录里，别的路径一律拒掉。"""
        references = bridge.conversation_references_of([
            {"kind": "file", "key": "doc/analysis/req-a/需求分析.md", "scope": "requirement-analysis"},
            {"kind": "file", "key": "../etc/passwd", "scope": "requirement-analysis"},
        ])

        self.assertEqual(
            [{"kind": "file", "key": "doc/analysis/req-a/需求分析.md", "scope": "requirement-analysis"}],
            references,
        )

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
                patch.object(bridge.factory, "create_ai_client", return_value=client),
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
                patch.object(bridge.factory, "create_ai_client", return_value=client),
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
                patch.object(bridge.factory, "create_ai_client", return_value=client),
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
            patch.object(bridge.factory, "create_ai_client", return_value=client),
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
            patch.object(bridge.factory, "create_ai_client", return_value=client),
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

    def test_task_testing_completion_persists_report_at_relative_workspace_path(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            executor = bridge.ExecutionBridge(workspace)
            task = {
                "itemKey": "api-1", "title": "Create API", "version": 2,
                "phase": "testing", "status": "doing", "progress": 75, "testingReport": "",
            }
            requests = []

            def request_with_retry(_config, path, body):
                requests.append((path, body))
                return {"version": 3}

            with (
                patch.object(executor, "_task_detail", return_value=task),
                patch.object(executor, "_request_with_retry", side_effect=request_with_retry),
            ):
                executor._sync_result(
                    self.runtime_config(), 1, "api-1", task,
                    {"version": 1, "externalSessionId": "testing-thread", "metadata": {}},
                    "testing-turn", "completed", "验收判定：通过\n\n测试完成。",
                )

            report_path = workspace / "doc/test/api-1/测试报告.md"
            self.assertTrue(report_path.is_file())
            self.assertIn("验收判定：通过", report_path.read_text(encoding="utf-8"))
            patch_request = next(request for request in requests if request[0] == "/delivery/item/patch")
            self.assertEqual("testingReport", next(key for key in patch_request[1] if key == "testingReport"))
            self.assertEqual(report_path.read_text(encoding="utf-8"), patch_request[1]["testingReport"])

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
            patch.object(bridge.clients.codex, "AppServerClient", return_value=fake_client),
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
        client.messages = bridge.clients.codex.queue.Queue()
        client.messages.put({"method": "unrelated/notification"})
        client.read_turn_status = unittest.mock.MagicMock(side_effect=["inProgress", "interrupted"])

        status = client.wait_turn("turn-1", poll_interval=0)

        self.assertEqual("interrupted", status)
        self.assertEqual(2, client.read_turn_status.call_count)

    def test_wait_turn_rides_out_a_freshly_created_thread_that_reads_empty(self):
        """会话刚建好时 rollout 还没落盘，thread/read 会直接报错；这不该让整轮等待作废。"""
        client = bridge.AppServerClient.__new__(bridge.AppServerClient)
        client.thread_id = "thread-1"
        client.process = unittest.mock.MagicMock()
        client.process.poll.return_value = None
        client.messages = bridge.clients.codex.queue.Queue()
        client.read_turn_status = unittest.mock.MagicMock(side_effect=[
            bridge.BridgeFailure("failed to read thread: rollout at ... is empty"),
            bridge.BridgeFailure("failed to read thread: rollout at ... is empty"),
            "completed",
        ])

        self.assertEqual("completed", client.wait_turn("turn-1", poll_interval=0))
        self.assertEqual(3, client.read_turn_status.call_count)

    def test_wait_turn_gives_up_when_the_thread_never_becomes_readable(self):
        """一直读不出来就不是瞬时问题了：过了宽限期要把错抛出去，不能空转。"""
        client = bridge.AppServerClient.__new__(bridge.AppServerClient)
        client.thread_id = "thread-1"
        client.process = unittest.mock.MagicMock()
        client.process.poll.return_value = None
        client.messages = bridge.clients.codex.queue.Queue()
        client.read_turn_status = unittest.mock.MagicMock(side_effect=bridge.BridgeFailure("broken"))

        with patch.object(bridge.clients.codex, "THREAD_READ_GRACE_SECONDS", 0):
            with self.assertRaises(bridge.BridgeFailure):
                client.wait_turn("turn-1", poll_interval=0)

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
            patch.object(bridge.clients.codex, "AppServerClient") as app_server,
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

    def make_submodule(self, name: str) -> Path:
        """把一个独立仓库登记成根仓库的子模组，模拟真实项目里的 submodule 布局。"""
        module = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(module, ignore_errors=True))
        for args in (
            ["init", "--initial-branch=main"],
            ["config", "user.email", "bridge@test"],
            ["config", "user.name", "bridge"],
        ):
            subprocess.run(["git", "-C", str(module), *args], check=True, capture_output=True)
        (module / "lib.txt").write_text(name, encoding="utf-8")
        subprocess.run(["git", "-C", str(module), "add", "."], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(module), "commit", "-m", "init"], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(self.workspace), "-c", "protocol.file.allow=always",
             "submodule", "add", str(module), name],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(self.workspace), "commit", "-m", f"add submodule {name}"],
            check=True, capture_output=True,
        )
        return self.workspace / name

    def test_branch_creation_never_clones_an_uninitialized_submodule(self):
        """没检出过的子模组只是个空目录：切分支不该顺手把它整个 clone 下来。"""
        self.make_submodule("galactus")
        subprocess.run(
            ["git", "-C", str(self.workspace), "submodule", "deinit", "-f", "galactus"],
            check=True, capture_output=True,
        )
        self.assertEqual(["galactus"], bridge.git_pending_submodules(self.workspace))

        calls: list[list[str]] = []
        real_run_git = bridge.git_ops.run_git

        def record(workspace, args, **kwargs):
            calls.append(list(args))
            return real_run_git(workspace, args, **kwargs)

        # git_create_branch_targets 和 run_git 同在 delivery_bridge.git_ops 里，
        # 打桩必须打在它真正解析名字的那个模块上，打在 http_bridge 的再导出名上是无效的。
        with patch.object(bridge.git_ops, "run_git", side_effect=record):
            bridge.git_create_branch_targets(self.workspace, "main", "feature/issue_req-1", [])

        self.assertNotEqual([], calls)
        self.assertEqual([], [args for args in calls if args[:2] == ["submodule", "update"]])
        self.assertEqual(["galactus"], bridge.git_pending_submodules(self.workspace))
        self.assertEqual("feature/issue_req-1", bridge.git_current_branch(self.workspace))

    def test_branch_creation_survives_a_submodule_that_cannot_be_updated(self):
        """基准分支记着一个本机取不到的子模组 commit 时，需求分支照样要建出来。"""
        self.make_submodule("galactus")
        # 先留一条指针正常的分支，等下切回来把工作区恢复干净。
        subprocess.run(["git", "-C", str(self.workspace), "branch", "work"], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(self.workspace), "update-index", "--cacheinfo",
             f"160000,{'d' * 40},galactus"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(self.workspace), "commit", "-m", "broken submodule pointer"],
            check=True, capture_output=True,
        )
        subprocess.run(["git", "-C", str(self.workspace), "checkout", "work"], check=True, capture_output=True)
        self.assertFalse(bridge.git_worktree_dirty(self.workspace))

        result = bridge.git_create_branch_targets(self.workspace, "main", "feature/issue_req-1", [])

        self.assertTrue(result["created"])
        self.assertEqual("feature/issue_req-1", bridge.git_current_branch(self.workspace))
        broken = next(record for record in result["results"] if record["path"] == "galactus")
        self.assertIn("galactus", broken["error"])
        # 根目录这条记录必须是干净的：坏掉的子模组只影响它自己那一行。
        self.assertEqual("", result["results"][0]["error"])

    def test_branch_creation_syncs_unselected_submodules_to_the_base_branch(self):
        """没勾的子模组不建自己的分支，但要跟着根仓库的指针走，否则工作区一直是脏的。"""
        module = self.make_submodule("galactus")
        first = subprocess.run(
            ["git", "-C", str(module), "rev-parse", "HEAD"], check=True, capture_output=True, text=True,
        ).stdout.strip()
        subprocess.run(["git", "-C", str(self.workspace), "branch", "legacy"], check=True, capture_output=True)
        (module / "lib.txt").write_text("next", encoding="utf-8")
        subprocess.run(["git", "-C", str(module), "commit", "-am", "next"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.workspace), "commit", "-am", "bump galactus"], check=True, capture_output=True)
        self.assertFalse(bridge.git_worktree_dirty(self.workspace))

        result = bridge.git_create_branch_targets(self.workspace, "legacy", "feature/issue_req-1", [])

        head = subprocess.run(
            ["git", "-C", str(module), "rev-parse", "HEAD"], check=True, capture_output=True, text=True,
        ).stdout.strip()
        self.assertEqual(first, head)
        self.assertFalse(bridge.git_worktree_dirty(self.workspace))
        self.assertTrue(all(not record["error"] for record in result["results"]))

    def test_branch_creation_leaves_a_selected_submodule_on_its_own_branch(self):
        """勾中的子模组要停在自己的需求分支上，不能被同步成游离 HEAD。"""
        module = self.make_submodule("galactus")

        result = bridge.git_create_branch_targets(self.workspace, "main", "feature/issue_req-1", ["galactus"])

        self.assertEqual("feature/issue_req-1", bridge.git_current_branch(module))
        self.assertEqual(["", "galactus"], [record["path"] for record in result["results"]])
        self.assertTrue(all(not record["error"] for record in result["results"]))

    def give_origin(self, child: Path) -> Path:
        """给子工程配一个真实的 origin，并把它当前的主干推上去。"""
        remote = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(remote, ignore_errors=True))
        subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(child), "remote", "add", "origin", str(remote)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(child), "push", "origin", "HEAD"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(child), "fetch", "origin"], check=True, capture_output=True)
        return remote

    def strand_on_previous_requirement(self, child: Path) -> None:
        """把子工程留在上一条需求分支上，并带一个只属于那条分支的提交。"""
        subprocess.run(
            ["git", "-C", str(child), "checkout", "-b", "feature/issue_req-0"], check=True, capture_output=True,
        )
        (child / "leak.txt").write_text("上一条需求的提交", encoding="utf-8")
        subprocess.run(["git", "-C", str(child), "add", "."], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(child), "commit", "-m", "previous requirement"], check=True, capture_output=True)

    def test_branch_reference_lookup_accepts_a_remote_prefixed_name(self):
        """origin/main 已经带了远端前缀，不能再拼一次查成 origin/origin/main。"""
        child = self.make_subproject("galactus")
        self.give_origin(child)

        self.assertTrue(bridge.git_branch_reference_exists(child, "origin/main"))
        self.assertTrue(bridge.git_branch_reference_exists(child, "main"))
        self.assertFalse(bridge.git_branch_reference_exists(child, "origin/nope"))

    def test_subproject_cuts_from_a_remote_prefixed_base_branch(self):
        """基准分支选 origin/main 时，子项目要从 origin/main 切，而不是从它当前停着的需求分支切。"""
        child = self.make_subproject("galactus")
        self.give_origin(child)
        self.strand_on_previous_requirement(child)
        self.add_origin()
        subprocess.run(
            ["git", "-C", str(self.workspace), "push", "origin", "main"], check=True, capture_output=True,
        )

        result = bridge.git_create_branch_targets(
            self.workspace, "origin/main", "feature/issue_req-1", ["galactus"],
        )

        record = next(item for item in result["results"] if item["path"] == "galactus")
        self.assertEqual("", record["error"])
        self.assertEqual("origin/main", record["baseBranch"])
        self.assertEqual("feature/issue_req-1", bridge.git_current_branch(child))
        # 上一条需求分支上的提交绝不能被带进新分支。
        self.assertFalse((child / "leak.txt").exists())

    def test_subproject_base_falls_back_to_the_trunk_instead_of_the_current_branch(self):
        """子项目没有同名基准分支时退回主干；停在哪条需求分支上都不作数。"""
        child = self.make_subproject("galactus")
        self.strand_on_previous_requirement(child)
        subprocess.run(["git", "-C", str(self.workspace), "branch", "legacy"], check=True, capture_output=True)

        result = bridge.git_create_branch_targets(self.workspace, "legacy", "feature/issue_req-1", ["galactus"])

        record = next(item for item in result["results"] if item["path"] == "galactus")
        self.assertEqual("", record["error"])
        self.assertEqual("main", record["baseBranch"])
        self.assertEqual("feature/issue_req-1", bridge.git_current_branch(child))
        self.assertFalse((child / "leak.txt").exists())

    def test_subproject_base_falls_back_to_master_when_there_is_no_main(self):
        """主干叫 master 的子项目要退回 origin/master，不能因为没有 main 就用当前分支。"""
        child = self.make_subproject("kakrolot", branch="master")
        self.give_origin(child)
        self.strand_on_previous_requirement(child)
        subprocess.run(["git", "-C", str(self.workspace), "branch", "legacy"], check=True, capture_output=True)

        result = bridge.git_create_branch_targets(self.workspace, "legacy", "feature/issue_req-1", ["kakrolot"])

        record = next(item for item in result["results"] if item["path"] == "kakrolot")
        self.assertEqual("", record["error"])
        self.assertIn(record["baseBranch"], ("origin/master", "master"))
        self.assertFalse((child / "leak.txt").exists())

    def test_subproject_without_any_trunk_reports_an_error_instead_of_guessing(self):
        """连 main / master 都找不到时宁可报错，也不要拿当前分支凑合。"""
        child = self.make_subproject("surfer", branch="feature/issue_req-0")

        result = bridge.git_create_branch_targets(self.workspace, "main", "feature/issue_req-1", ["surfer"])

        record = next(item for item in result["results"] if item["path"] == "surfer")
        self.assertIn("master", record["error"])
        self.assertFalse(record["created"])
        # 子项目要原地不动，不能被切到半路上。
        self.assertEqual("feature/issue_req-0", bridge.git_current_branch(child))

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
        real_push = bridge.execution.git.git_push_branch

        def ordered_push(workspace: Path, *args, **kwargs):
            calls.append(workspace.resolve())
            return real_push(workspace, *args, **kwargs)

        with patch.object(bridge.execution.git, "git_push_branch", side_effect=ordered_push):
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
