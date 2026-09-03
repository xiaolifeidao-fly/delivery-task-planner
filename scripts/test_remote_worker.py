#!/usr/bin/env python3

import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from delivery_bridge import remote_worker
from delivery_bridge.errors import BridgeFailure
from delivery_bridge.execution import ExecutionBridge


class FakeCommandAPI:
    def __init__(self, claimed, spaces=None, failing_biz_lines=()):
        self.claimed = claimed
        self.spaces = spaces if spaces is not None else []
        self.failing_biz_lines = set(failing_biz_lines)
        self.calls = []

    def request(self, _config, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        if path == "/spaces":
            return self.spaces
        if path == "/workers/register" and kwargs.get("biz_line") in self.failing_biz_lines:
            raise BridgeFailure("远程命令接口请求失败：记录不存在")
        if path == "/workers/commands/claim":
            result, self.claimed = self.claimed, None
            return result
        if path.endswith("/activity"):
            return {"cancelRequested": False}
        return {}


class LocalBridge:
    def __init__(self, workspace):
        self.workspace = workspace

    def sync_cloud_workspace(self, program_id, config):
        return {"enabled": True, "programId": program_id, "files": ["doc/module/a/文档.md"]}


class RootBridge:
    def __init__(self, local):
        self.local = local

    def for_workspace(self, _workspace):
        return self.local


class RemoteWorkerTest(unittest.TestCase):
    def test_command_api_url_falls_back_to_the_baked_in_address(self):
        """漏配不能再让 Worker 静默禁用：那会让「插件没开」和「插件开着但没登记」
        在面板上长成同一句话，而日志里只有一行「未启用」。"""
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(remote_worker.DEFAULT_COMMAND_API_URL, remote_worker.command_api_url())
        with patch.dict(os.environ, {remote_worker.COMMAND_API_URL_ENV: "https://app-api.example.test"}, clear=True):
            self.assertEqual("https://app-api.example.test/api", remote_worker.command_api_url())
            self.assertEqual("https://other.example.test/api", remote_worker.command_api_url("https://other.example.test"))
        # 显式关闭仍然要能退回纯回环桥接，本地开发不该被强行连上远端。
        with patch.dict(os.environ, {remote_worker.COMMAND_API_URL_ENV: "off"}, clear=True):
            self.assertEqual("", remote_worker.command_api_url())
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual("", remote_worker.command_api_url("OFF"))

    def test_workspace_mapping_is_local_and_prunes_missing_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            store = remote_worker.WorkspaceMappingStore(root / "mappings.json")
            store.record(16, workspace, "whatsapp")

            mapping = store.get(16)
            self.assertEqual(16, mapping["programId"])
            self.assertEqual(str(workspace.resolve()), mapping["workspace"])
            self.assertEqual("whatsapp", mapping["bizLine"])
            self.assertEqual(0o600, (root / "mappings.json").stat().st_mode & 0o777)

            workspace.rmdir()
            self.assertEqual([], store.snapshot())

    def test_bridge_records_the_authoritative_program_mapping_after_local_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = remote_worker.WorkspaceMappingStore(root / "mappings.json")
            bridge = ExecutionBridge(root)
            bridge.workspace_mappings = store
            bridge.program_biz_lines[16] = "whatsapp"

            self.assertTrue(bridge.remember_remote_workspace(16, root, {}))
            self.assertEqual(str(root.resolve()), store.get(16)["workspace"])

    def test_worker_registers_only_program_ids_and_completes_a_whitelisted_command(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            store = remote_worker.WorkspaceMappingStore(root / "mappings.json")
            store.record(16, workspace, "whatsapp")
            claimed = {
                "command": {
                    "commandId": "cmd-1",
                    "bizLine": "whatsapp",
                    "programId": 16,
                    "commandType": "documents.cloud-sync",
                    "input": {},
                },
                "leaseToken": "lease-1",
            }
            api = FakeCommandAPI(claimed)
            worker = remote_worker.RemoteCommandWorker(
                RootBridge(LocalBridge(workspace)), mappings=store,
                api_url="https://app.example.test", worker_id="worker-test",
            )

            with (
                patch.object(remote_worker, "CommandAPI", return_value=api),
                patch.object(remote_worker.planner, "load_config", return_value={"key": "token", "key_header": "token", "user_id": "user"}),
            ):
                self.assertTrue(worker.run_once(wait_seconds=1))

            register = next(kwargs["body"] for method, path, kwargs in api.calls if method == "POST" and path == "/workers/register")
            self.assertEqual([16], register["programIds"])
            self.assertNotIn("workspace", register)
            self.assertIn("documents.cloud-sync", register["capabilities"])
            self.assertTrue(any(path == "/workers/heartbeat" for _method, path, _kwargs in api.calls))
            complete = next(kwargs["body"] for _method, path, kwargs in api.calls if path.endswith("/complete"))
            self.assertEqual("succeeded", complete["state"])
            self.assertEqual(["doc/module/a/文档.md"], complete["result"]["files"])
            self.assertNotIn(str(workspace), str(complete))

    def test_worker_registers_every_writable_space_before_any_workspace_is_bound(self):
        """插件起来就该在服务端留下一行，哪怕本机还没绑过任何项目。

        早先没有映射就一次注册都不发，客户端只能说「未登记执行电脑」——和插件根本
        没开完全同一句话，用户没有任何办法把两者分开。
        """
        with tempfile.TemporaryDirectory() as directory:
            store = remote_worker.WorkspaceMappingStore(Path(directory) / "mappings.json")
            api = FakeCommandAPI(None, spaces=[
                {"code": "yinni", "name": "印尼业务线", "canWrite": True},
                {"code": "onlyread", "name": "只读空间", "canWrite": False},
            ])
            worker = remote_worker.RemoteCommandWorker(
                object(), mappings=store, api_url="https://app.example.test", worker_id="worker-test",
            )

            with (
                patch.object(remote_worker, "CommandAPI", return_value=api),
                patch.object(remote_worker.planner, "load_config", return_value={"key": "token", "key_header": "token", "user_id": "user"}),
            ):
                self.assertFalse(worker.run_once(wait_seconds=1))

            registered = [(kwargs["biz_line"], kwargs["body"]["programIds"]) for _method, path, kwargs in api.calls if path == "/workers/register"]
            # 只读空间不注册：那里本来也不允许这台机器写任何东西。
            self.assertEqual([("yinni", [])], registered)
            self.assertEqual(["yinni"], [kwargs["biz_line"] for _method, path, kwargs in api.calls if path == "/workers/heartbeat"])

    def test_one_failing_business_line_keeps_the_others_registered_and_beating(self):
        """一条业务线注册失败不能连坐：映射里一个被删掉的项目曾经足以让整台机器
        在所有业务线上一起显示离线，而日志里只有一行「记录不存在」。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            store = remote_worker.WorkspaceMappingStore(root / "mappings.json")
            store.record(16, workspace, "whatsapp")
            api = FakeCommandAPI(None, spaces=[
                {"code": "yinni", "canWrite": True},
                {"code": "whatsapp", "canWrite": True},
            ], failing_biz_lines={"whatsapp"})
            worker = remote_worker.RemoteCommandWorker(
                object(), mappings=store, api_url="https://app.example.test", worker_id="worker-test",
            )

            with (
                patch.object(remote_worker, "CommandAPI", return_value=api),
                patch.object(remote_worker.planner, "load_config", return_value={"key": "token", "key_header": "token", "user_id": "user"}),
            ):
                self.assertFalse(worker.run_once(wait_seconds=1))

            self.assertEqual(
                ["whatsapp", "yinni"],
                sorted(kwargs["biz_line"] for _method, path, kwargs in api.calls if path == "/workers/register"),
            )
            self.assertEqual(["yinni"], [kwargs["biz_line"] for _method, path, kwargs in api.calls if path == "/workers/heartbeat"])
            # 失败过就不记住这次身份，下一轮十几秒后重试，而不是等满 60 秒。
            self.assertIsNone(worker._registered_identity)

    def test_worker_rejects_unknown_commands_without_dynamic_dispatch(self):
        worker = remote_worker.RemoteCommandWorker(object(), api_url="https://app.example.test", worker_id="worker-test")
        with self.assertRaisesRegex(BridgeFailure, "不支持"):
            worker._dispatch(
                {"programId": 16, "commandType": "shell.exec", "input": {"command": "whoami"}},
                {"workspace": "/not-used", "bizLine": "whatsapp"}, {}, MagicMock(),
            )

    def test_results_strip_absolute_local_paths_before_they_reach_app_api(self):
        workspace = Path("/Users/alice/project")
        value = remote_worker._safe_value(
            {"workspace": str(workspace), "output": f"failed in {workspace}/secret.txt", "paths": ["doc/file.md"]},
            workspace,
        )
        self.assertEqual("project", value["workspaceName"])
        self.assertNotIn("/Users/alice", value["output"])
        self.assertEqual(["doc/file.md"], value["paths"])

    def test_cancellation_only_uses_existing_task_stop_controls(self):
        bridge = MagicMock()
        remote_worker.RemoteCommandWorker._best_effort_cancel(
            {"programId": 16, "commandType": "task.execute", "input": {"itemKey": "task-a"}},
            bridge,
            {"_project_id": 16},
        )
        bridge.stop_conversation.assert_called_once_with({"programId": 16, "itemKey": "task-a"}, config={"_project_id": 16})

    def test_task_command_waits_for_existing_local_turn_before_completion(self):
        bridge = MagicMock()
        bridge.workspace = Path("/workspace")
        bridge.lock = threading.Lock()
        bridge.active = {("", 16, "task-a")}
        worker = remote_worker.RemoteCommandWorker(object(), api_url="https://app.example.test", worker_id="worker-test")
        reporter = MagicMock()

        def clear_active(*_args):
            with bridge.lock:
                bridge.active.clear()

        with patch.object(worker, "_relay_progress", side_effect=clear_active) as relay:
            worker._wait_for_task(bridge, {"_project_id": 16, "_biz_line": "whatsapp"}, 16, "task-a", reporter)

        # 业务线必须一起带下去：少了它，等待回合期间的实时正文一条都发不出去。
        relay.assert_called_once_with(reporter, {"workspace": "/workspace", "bizLine": "whatsapp"})

    def test_task_conversation_downloads_server_attachment_before_local_dispatch(self):
        bridge = MagicMock()
        bridge.upload_conversation_attachments.return_value = {
            "attachments": [{"id": "local-attachment-id"}],
        }
        bridge.send_conversation.return_value = {"accepted": True, "threadId": "thread-1"}
        reporter = MagicMock()
        reporter.biz_line = "whatsapp"
        reporter.config = {"key": "token", "key_header": "token"}
        reporter.api.download_attachment.return_value = {
            "name": "brief.txt", "contentType": "text/plain", "data": b"attachment body",
        }
        worker = remote_worker.RemoteCommandWorker(object(), api_url="https://app.example.test", worker_id="worker-test")

        result = worker._dispatch_task(
            "task.conversation",
            {"programId": 16, "itemKey": "task-a", "message": "continue", "attachmentIds": ["attachment-0123456789abcdef0123456789abcdef"]},
            bridge,
            {"_project_id": 16, "_biz_line": "whatsapp"},
            16,
            reporter,
        )

        reporter.api.download_attachment.assert_called_once_with(
            reporter.config, "whatsapp", 16, "attachment-0123456789abcdef0123456789abcdef",
        )
        bridge.upload_conversation_attachments.assert_called_once()
        payload = bridge.send_conversation.call_args.args[0]
        self.assertEqual(["local-attachment-id"], payload["attachmentIds"])
        self.assertTrue(result["accepted"])
        self.assertEqual("thread-1", result["threadId"])

    def test_business_conversation_runs_in_the_business_workspace_not_the_project_one(self):
        business = MagicMock()
        business.send_business_conversation.return_value = {
            "accepted": True, "threadId": "thread-b", "turnId": "turn-b", "active": True,
        }
        business.business_conversation.side_effect = [
            {"threadId": "thread-b", "active": True, "turns": [{"id": "turn-b", "status": "running", "items": []}]},
            {"threadId": "thread-b", "active": False, "turns": [
                {"id": "turn-old", "status": "completed", "items": [{"type": "agentMessage", "text": "旧的一轮"}]},
                {"id": "turn-b", "status": "completed", "items": [{"type": "agentMessage", "text": "已整理好文档"}]},
            ]},
        ]
        root = MagicMock()
        root.for_business_workspace.return_value = business
        reporter = MagicMock()
        reporter.cancelled.is_set.return_value = False
        worker = remote_worker.RemoteCommandWorker(root, api_url="https://app.example.test", worker_id="worker-test")

        result = worker._dispatch_business(
            {
                "programId": 16, "itemKey": "business-requirement-42", "message": "想做直播",
                "workspace": "alice/业务空间/业务项目", "businessIntake": True, "provider": "codex",
            },
            16,
            reporter,
        )

        root.for_business_workspace.assert_called_once_with("alice/业务空间/业务项目")
        root.for_workspace.assert_not_called()
        payload = business.send_business_conversation.call_args.args[0]
        self.assertNotIn("workspace", payload)
        # 线程标识必须在第一次快照之前就回传，服务端的 Start 正阻塞等它。
        first_report = reporter.report.call_args_list[0]
        self.assertEqual("thread-b", first_report.args[2]["threadId"])
        self.assertEqual("thread-b", result["threadId"])
        # 只回传当前这一轮：历史轮次会把 64 KB 的活动上限撑爆。
        self.assertEqual(["turn-b"], [turn["id"] for turn in result["conversation"]["turns"]])
        self.assertFalse(result["conversation"]["active"])

    def test_business_conversation_materialises_server_attachments_locally(self):
        business = MagicMock()
        business.save_business_attachments.return_value = {"attachments": [{"id": "local-business-id"}]}
        business.send_business_conversation.return_value = {
            "accepted": True, "threadId": "thread-b", "turnId": "turn-b", "active": True,
        }
        business.business_conversation.return_value = {"threadId": "thread-b", "active": False, "turns": []}
        root = MagicMock()
        root.for_business_workspace.return_value = business
        reporter = MagicMock()
        reporter.cancelled.is_set.return_value = False
        reporter.biz_line = "whatsapp"
        reporter.config = {"key": "token", "key_header": "token"}
        reporter.api.download_attachment.return_value = {
            "name": "投放.png", "contentType": "image/png", "data": b"binary",
        }
        worker = remote_worker.RemoteCommandWorker(root, api_url="https://app.example.test", worker_id="worker-test")

        worker._dispatch_business(
            {
                "programId": 16, "itemKey": "business-requirement-42", "message": "想做直播",
                "workspace": "alice/业务空间/业务项目",
                "attachmentIds": ["attachment-0123456789abcdef0123456789abcdef"],
            },
            16,
            reporter,
        )

        reporter.api.download_attachment.assert_called_once_with(
            reporter.config, "whatsapp", 16, "attachment-0123456789abcdef0123456789abcdef",
        )
        payload = business.send_business_conversation.call_args.args[0]
        self.assertEqual(["local-business-id"], payload["attachmentIds"])

    def test_business_progress_relay_does_not_overwrite_the_conversation_snapshot(self):
        worker = remote_worker.RemoteCommandWorker(MagicMock(), api_url="https://app.example.test", worker_id="worker-test")
        reporter = MagicMock()
        reporter.command = {"commandType": "business.conversation", "programId": 16, "input": {}}

        worker._relay_progress(reporter, {"workspace": Path("/workspace"), "bizLine": "whatsapp"})

        reporter.report.assert_not_called()

    def test_task_session_reads_the_local_session_only_after_server_lease(self):
        bridge = MagicMock()
        bridge.conversation.return_value = {"threadId": "thread-1", "turns": [{"text": "done"}]}
        worker = remote_worker.RemoteCommandWorker(object(), api_url="https://app.example.test", worker_id="worker-test")

        result = worker._dispatch_task(
            "task.session",
            {"programId": 16, "itemKey": "task-a"},
            bridge,
            {"_project_id": 16, "_biz_line": "whatsapp"},
            16,
            MagicMock(),
        )

        bridge.conversation.assert_called_once_with(16, "task-a", "", config={"_project_id": 16, "_biz_line": "whatsapp"}, provider="codex")
        self.assertEqual("thread-1", result["threadId"])

    def test_running_turn_relays_conversation_text_as_activity_increments(self):
        """回合跑着时正文顺着活动流走，手机端因此不必再单独发快照命令。"""
        local = MagicMock()
        local.conversation.return_value = {
            "threadId": "thread-1", "activeTurnId": "turn-1", "active": True, "executorType": "codex",
            "turns": [
                {"id": "turn-0", "status": "completed", "items": [{"id": "old", "text": "上一轮的回复"}]},
                {"id": "turn-1", "status": "running", "items": [{"id": "a", "text": "正在改文件"}]},
            ],
        }
        root = MagicMock()
        root.for_workspace.return_value = local
        worker = remote_worker.RemoteCommandWorker(root, api_url="https://app.example.test", worker_id="worker-test")
        reporter = remote_worker.CommandReporter(
            MagicMock(), {"key": "k"},
            {"commandId": "cmd-1", "bizLine": "whatsapp", "commandType": "task.conversation", "programId": 16,
             "input": {"itemKey": "task-a"}},
            "worker-test", "lease-1",
        )
        mapping = {"workspace": Path("/workspace"), "bizLine": "whatsapp"}

        self.assertTrue(worker._relay_progress(reporter, mapping) is None)
        first = reporter.api.request.call_args.kwargs["body"]["data"]["live"]
        # 只带正在跑的那一轮，历史回合不重复占活动体。
        self.assertEqual(["a"], [item["id"] for item in first["items"]])
        self.assertEqual("turn-1", first["turnId"])

        # 同一拍之内不再重复上报：节奏由 LIVE_SNAPSHOT_SECONDS 决定。
        reporter.api.request.reset_mock()
        worker._relay_progress(reporter, mapping)
        reporter.api.request.assert_not_called()

        # 下一拍只回传新长出来的那条，已经发过的不再重发。
        reporter.live_published_at = 0.0
        local.conversation.return_value["turns"][1]["items"].append({"id": "b", "text": "改完了"})
        worker._relay_progress(reporter, mapping)
        second = reporter.api.request.call_args.kwargs["body"]["data"]["live"]
        self.assertEqual(["b"], [item["id"] for item in second["items"]])

        # 回合刚起步、本机还没有任何正文时退回通用进度行，回合本身不受影响。
        reporter.live_published_at = 0.0
        reporter._last_report_at = 0.0
        local.conversation.return_value = {"turns": []}
        local.progress.snapshot.return_value = []
        reporter.api.request.reset_mock()
        worker._relay_progress(reporter, mapping)
        self.assertNotIn("live", reporter.api.request.call_args.kwargs["body"]["data"])

    def test_live_turn_items_stay_inside_the_activity_size_limit(self):
        oversized = [{"id": f"item-{index}", "text": "改" * 4000} for index in range(20)]
        bounded = remote_worker._bounded_live_items(oversized)
        self.assertLess(len(bounded), len(oversized))
        self.assertEqual("item-19", bounded[-1]["id"])
        self.assertLessEqual(
            len(json.dumps(bounded, ensure_ascii=False).encode("utf-8")), remote_worker.MAX_LIVE_SNAPSHOT_BYTES,
        )

    def test_requirement_channel_command_waits_for_the_local_turn_and_returns_the_snapshot(self):
        """评审这类辅助会话和拆解一个规矩：命令跑完了，才算这一轮结束。"""
        bridge = MagicMock()
        bridge.workspace = Path("/workspace")
        bridge.lock = threading.Lock()
        bridge.send_requirement_review.return_value = {"accepted": True, "threadId": "thread-9"}
        bridge.requirement_review.return_value = {"threadId": "thread-9", "turns": [{"id": "t1"}]}
        bridge._requirement_review_identity.return_value = ("review", 16, "req-a")
        bridge.active = {("review", 16, "req-a")}
        worker = remote_worker.RemoteCommandWorker(object(), api_url="https://app.example.test", worker_id="worker-test")

        def clear_active(*_args):
            with bridge.lock:
                bridge.active.clear()

        with patch.object(worker, "_relay_progress", side_effect=clear_active):
            result = worker._dispatch_task(
                "requirement.review",
                {"programId": 16, "requirementKey": "req-a", "message": "评审一下"},
                bridge,
                {"_project_id": 16, "_biz_line": "whatsapp"},
                16,
                MagicMock(),
            )

        self.assertEqual("req-a", bridge.send_requirement_review.call_args.args[0]["requirementKey"])
        # 回合结束后把最终快照带上，手机端不必为了拿正文再单独读一次。
        self.assertEqual("thread-9", result["session"]["threadId"])
        bridge.requirement_review.assert_called_once_with(
            16, "req-a", "thread-9", "codex", config={"_project_id": 16, "_biz_line": "whatsapp"},
        )

    def test_task_channel_commands_cover_send_stop_and_read(self):
        bridge = MagicMock()
        bridge.workspace = Path("/workspace")
        bridge.lock = threading.Lock()
        bridge.active = set()
        bridge.task_fine_tuning_conversation.return_value = {"threadId": "thread-1", "turns": []}
        worker = remote_worker.RemoteCommandWorker(object(), api_url="https://app.example.test", worker_id="worker-test")
        config = {"_project_id": 16, "_biz_line": "whatsapp"}

        worker._dispatch_task("task.fine-tuning-session", {"programId": 16, "itemKey": "task-a"}, bridge, config, 16, MagicMock())
        bridge.task_fine_tuning_conversation.assert_called_once_with(16, "task-a", "", "codex", config=config)

        worker._dispatch_task("task.testing-stop", {"programId": 16, "itemKey": "task-a"}, bridge, config, 16, MagicMock())
        bridge.stop_task_testing_cases.assert_called_once()

        # 只读的两条会话读取要走 Worker 的只读通道，否则长任务占着执行通道时读不到。
        self.assertIn("task.fine-tuning-session", remote_worker.READ_ONLY_COMMAND_CAPABILITIES)
        self.assertIn("task.testing-session", remote_worker.READ_ONLY_COMMAND_CAPABILITIES)
        # 停止和只读不该被当成「有正文可流式回传」的回合。
        self.assertTrue(remote_worker.live_snapshot_command("requirement.review"))
        self.assertFalse(remote_worker.live_snapshot_command("requirement.review-stop"))
        self.assertFalse(remote_worker.live_snapshot_command("task.fine-tuning-session"))

    def test_every_channel_command_is_registered_as_a_capability(self):
        """能力表漏一条，命令就会一直排在队列里没人领 —— 这种缺失只有清单能挡住。"""
        for command_type in remote_worker.CHANNEL_COMMANDS:
            self.assertIn(command_type, remote_worker.COMMAND_CAPABILITIES, command_type)
            channel, scope, action = remote_worker.channel_of(command_type)
            self.assertIn(action, {"send", "generate", "stop", "read"}, command_type)
            self.assertIn(scope, {"requirement", "task"}, command_type)
            self.assertIn("read", channel, command_type)

    def test_planning_command_is_whitelisted_and_returns_the_planning_snapshot(self):
        bridge = MagicMock()
        bridge.workspace = Path("/workspace")
        bridge.send_planning.return_value = {"threadId": "thread-1", "accepted": True}
        bridge.planning.return_value = {"threadId": "thread-1", "result": {"items": []}}
        worker = remote_worker.RemoteCommandWorker(object(), api_url="https://app.example.test", worker_id="worker-test")

        with patch.object(worker, "_wait_for_planning") as wait, patch.object(
            remote_worker.planner, "request_api", return_value={"requirementKey": "req-a", "name": "需求 A", "detail": "正文"}
        ) as request_api:
            result = worker._dispatch_task(
                "task.planning",
                {"programId": 16, "requirementKey": "req-a", "message": "拆解"},
                bridge,
                {"_project_id": 16, "_biz_line": "whatsapp"},
                16,
                MagicMock(),
            )

        self.assertIn("task.planning", remote_worker.COMMAND_CAPABILITIES)
        request_api.assert_called_once()
        # 移动端只传需求键，需求正文由 Worker 从任务面板补齐后再进入拆解会话。
        self.assertEqual("需求 A", bridge.send_planning.call_args.args[0]["requirementName"])
        wait.assert_called_once_with(bridge, 16, "req-a", unittest.mock.ANY)
        self.assertEqual("thread-1", result["planning"]["threadId"])

    def test_planning_session_command_reads_the_local_snapshot_without_sending(self):
        bridge = MagicMock()
        bridge.planning.return_value = {"threadId": "thread-1", "turns": []}
        worker = remote_worker.RemoteCommandWorker(object(), api_url="https://app.example.test", worker_id="worker-test")

        result = worker._dispatch_task(
            "task.planning-session",
            {"programId": 16, "requirementKey": "req-a", "threadId": "thread-1"},
            bridge,
            {"_project_id": 16, "_biz_line": "whatsapp"},
            16,
            MagicMock(),
        )

        bridge.send_planning.assert_not_called()
        bridge.planning.assert_called_once_with(
            16, selected_thread_id="thread-1", config={"_project_id": 16, "_biz_line": "whatsapp"},
            requirement_key="req-a", provider="codex",
        )
        self.assertEqual("thread-1", result["threadId"])
        self.assertLessEqual(remote_worker.READ_ONLY_COMMAND_CAPABILITIES, remote_worker.COMMAND_CAPABILITIES)

    def test_requirement_commands_reach_the_session_dispatch_not_the_git_one(self):
        """需求级命令必须从 _dispatch 就走到会话分发。

        白名单里放行、_dispatch_task 里也接住了，中间那步却按 `task.` 前缀分流 ——
        手机上「消耗」因此一直收到「Worker 不支持远程命令类型」。这条盯的是那一步。
        """
        bridge = MagicMock()
        bridge.requirement_usage.return_value = {"requirementKey": "req-a", "tasks": []}
        root = MagicMock()
        root.for_workspace.return_value = bridge
        worker = remote_worker.RemoteCommandWorker(root, api_url="https://app.example.test", worker_id="worker-test")

        result = worker._dispatch(
            {"programId": 16, "commandType": "requirement.usage", "input": {"requirementKey": "req-a"}},
            {"workspace": "/workspace", "bizLine": "whatsapp"}, {}, MagicMock(),
        )

        bridge.requirement_usage.assert_called_once()
        self.assertEqual("req-a", result["requirementKey"])

    def test_requirement_session_reads_only_the_block_the_command_names(self):
        """消耗面板点进某一块，读的就是那一块自己的会话正文。"""
        bridge = MagicMock()
        bridge.requirement_review.return_value = {"threadId": "thread-1", "turns": []}
        worker = remote_worker.RemoteCommandWorker(object(), api_url="https://app.example.test", worker_id="worker-test")

        result = worker._dispatch_task(
            "requirement.session",
            {"programId": 16, "requirementKey": "req-a", "group": "review", "threadId": "thread-1"},
            bridge,
            {"_project_id": 16, "_biz_line": "whatsapp"},
            16,
            MagicMock(),
        )

        bridge.requirement_review.assert_called_once_with(
            16, "req-a", "thread-1", "codex", config={"_project_id": 16, "_biz_line": "whatsapp"},
        )
        bridge.requirement_testing.assert_not_called()
        self.assertEqual("thread-1", result["threadId"])
        self.assertIn("requirement.session", remote_worker.READ_ONLY_COMMAND_CAPABILITIES)

    def test_requirement_session_refuses_blocks_outside_the_reader_table(self):
        """分块名只能在常量表里查，查不到就拒 —— 命令拿不到表以外的任何桥接方法。"""
        bridge = MagicMock()
        worker = remote_worker.RemoteCommandWorker(object(), api_url="https://app.example.test", worker_id="worker-test")

        with self.assertRaisesRegex(BridgeFailure, "不支持的需求会话分块"):
            worker._dispatch_task(
                "requirement.session",
                {"programId": 16, "requirementKey": "req-a", "group": "sync_cloud_workspace"},
                bridge,
                {"_project_id": 16, "_biz_line": "whatsapp"},
                16,
                MagicMock(),
            )


if __name__ == "__main__":
    unittest.main()
