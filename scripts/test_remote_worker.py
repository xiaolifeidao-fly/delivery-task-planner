#!/usr/bin/env python3

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
    def __init__(self, claimed):
        self.claimed = claimed
        self.calls = []

    def request(self, _config, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
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
            worker._wait_for_task(bridge, {"_project_id": 16}, 16, "task-a", reporter)

        relay.assert_called_once_with(reporter, {"workspace": "/workspace"})

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


if __name__ == "__main__":
    unittest.main()
