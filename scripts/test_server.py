#!/usr/bin/env python3

import importlib.util
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


SERVER_PATH = Path(__file__).resolve().parents[1] / "server.py"
SPEC = importlib.util.spec_from_file_location("delivery_task_planner_server", SERVER_PATH)
server = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(server)


class DeliveryTaskPlannerTest(unittest.TestCase):
    def test_mcp_content_length_frame_reads_utf8_and_writes_a_framed_response(self):
        request = {"jsonrpc": "2.0", "id": 7, "method": "ping", "params": {"message": "中文"}}
        payload = json.dumps(request, ensure_ascii=False).encode("utf-8")
        source = io.BytesIO(b"Content-Length: " + str(len(payload)).encode("ascii") + b"\r\n\r\n" + payload)

        message, framed = server.read_mcp_message(source) or ({}, False)
        self.assertTrue(framed)
        self.assertEqual(request, message)

        target = io.BytesIO()
        server.write_mcp_message(target, {"jsonrpc": "2.0", "id": 7, "result": {"text": "中文"}}, framed)
        header, response_payload = target.getvalue().split(b"\r\n\r\n", 1)
        self.assertIn(f"Content-Length: {len(response_payload)}".encode("ascii"), header)
        self.assertEqual("中文", json.loads(response_payload.decode("utf-8"))["result"]["text"])

    def test_mcp_legacy_json_line_stays_line_delimited(self):
        source = io.BytesIO(b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n')

        message, framed = server.read_mcp_message(source) or ({}, True)
        self.assertFalse(framed)
        self.assertEqual("ping", message["method"])

        target = io.BytesIO()
        server.write_mcp_message(target, {"jsonrpc": "2.0", "id": 1, "result": {}}, framed)
        self.assertEqual({"jsonrpc": "2.0", "id": 1, "result": {}}, json.loads(target.getvalue().decode("utf-8")))

    def test_mcp_framed_request_keeps_framing_for_unexpected_errors(self):
        payload = b'{"jsonrpc":"2.0","id":1,"method":"ping"}'
        source = io.BytesIO(b"Content-Length: " + str(len(payload)).encode("ascii") + b"\r\n\r\n" + payload)
        target = io.BytesIO()

        with patch.object(server, "handle", side_effect=RuntimeError("broken")):
            server.serve_stdio(source, target)

        header, response_payload = target.getvalue().split(b"\r\n\r\n", 1)
        self.assertTrue(header.startswith(b"Content-Length: "))
        self.assertEqual("broken", json.loads(response_payload.decode("utf-8"))["error"]["message"])

    def test_api_request_does_not_forward_business_line_header(self):
        response = MagicMock()
        response.read.return_value = b'{"success": true, "data": []}'
        response.__enter__.return_value = response
        config = {
            "api_url": "http://example.test/api",
            "key": "secret",
            "key_header": "token",
        }

        with patch.object(server.urllib.request, "urlopen", return_value=response) as urlopen:
            self.assertEqual([], server.request_api(config, "GET", "/delivery/programs", query={"programId": 1}))

        request = urlopen.call_args.args[0]
        self.assertEqual("http://example.test/api/delivery/programs?programId=1", request.full_url)
        self.assertIsNone(request.get_header("X-biz-line"))

    def test_initialize_does_not_store_business_line_configuration(self):
        with (
            patch.object(server, "request_api", return_value=[]),
            patch.object(server, "save_config") as save_config,
        ):
            result = server.initialize({"api_url": "http://example.test", "key": "secret", "biz_line": "legacy"})

        config = save_config.call_args.args[0]
        self.assertNotIn("biz_line", config)
        self.assertNotIn("bizLine", result)
        self.assertEqual("http://example.test/api", config["bridge_api_url"])

    def test_bridge_api_url_defaults_to_the_fixed_remote_service(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.json"
            with patch.object(server, "CONFIG_PATH", missing), patch.object(server, "LEGACY_CONFIG_PATH", missing):
                self.assertEqual("http://47.110.3.214:8691/api", server.bridge_api_url())

    def test_update_api_url_updates_the_bridge_target_without_requiring_the_key_again(self):
        config = {"api_url": "http://old.test/api", "key": "secret", "key_header": "token", "user_id": "planner"}
        with (
            patch.object(server, "load_config", return_value=config),
            patch.object(server, "request_api", return_value=[{"programId": 4}]) as request_api,
            patch.object(server, "save_config") as save_config,
        ):
            result = server.update_api_url({"api_url": "https://board.example.test"})

        saved = save_config.call_args.args[0]
        self.assertEqual("https://board.example.test/api", saved["api_url"])
        self.assertEqual("https://board.example.test/api", saved["bridge_api_url"])
        self.assertEqual("secret", saved["key"])
        self.assertEqual("https://board.example.test/api", request_api.call_args.args[0]["api_url"])
        self.assertEqual("https://board.example.test/api", result["bridgeApiUrl"])

    def test_preview_mode_blocks_task_board_writes(self):
        with patch.dict(os.environ, {server.RUNTIME_WRITE_MODE_ENV: "preview"}):
            for tool, arguments in (
                (server.create_tasks, {"program_id": 1, "tasks": []}),
                (server.create_stage, {"program_id": 1, "stage_key": "s1", "title": "阶段"}),
                (server.create_module, {"program_id": 1, "module_key": "api", "name": "接口"}),
                (server.update_task, {"program_id": 1, "item_key": "task-a", "title": "更新"}),
                (server.update_task_dependencies, {"program_id": 1, "item_key": "task-a", "depends_on_item_keys": []}),
                (server.delete_task_dependencies, {"program_id": 1, "item_key": "task-a", "predecessor_item_keys": ["task-b"]}),
            ):
                with self.assertRaisesRegex(server.ToolFailure, "预览阶段"):
                    tool(arguments)

    def test_writes_are_allowed_when_no_write_mode_is_declared(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(server.RUNTIME_WRITE_MODE_ENV, None)
            # 只要不是 preview 就放行：普通 MCP 工作流没有这个环境变量。
            self.assertIsNone(server.assert_write_allowed("写入任务"))

    def test_program_id_is_required_when_runtime_scope_is_absent(self):
        with patch.dict(os.environ, {"CODEX_PROJECT_NAME": "universe"}):
            with self.assertRaisesRegex(server.ToolFailure, "数值主键"):
                server.program_value_of({})

    def test_program_code_is_rejected_as_a_program_id(self):
        with patch.dict(os.environ, {"CODEX_PROJECT_NAME": "universe"}):
            with self.assertRaisesRegex(server.ToolFailure, "数值主键"):
                server.program_value_of({"program_id": "phoenix"})

    def test_runtime_project_scope_overrides_displayed_project(self):
        with patch.dict(
            os.environ,
            {server.RUNTIME_PROJECT_ID_ENV: "1", "CODEX_PROJECT_NAME": "universe"},
            clear=False,
        ):
            value, used_current_project = server.program_value_of({})
        self.assertEqual(1, value)
        self.assertFalse(used_current_project)

    def test_runtime_project_scope_rejects_another_project(self):
        with patch.dict(os.environ, {server.RUNTIME_PROJECT_ID_ENV: "1"}, clear=False):
            with self.assertRaisesRegex(server.ToolFailure, "不能切换到其他项目"):
                server.program_value_of({"program_id": 2})

    def test_runtime_config_uses_only_transient_board_credentials(self):
        with patch.dict(
            os.environ,
            {
                server.RUNTIME_API_URL_ENV: "http://board.test",
                server.RUNTIME_TOKEN_ENV: "current-user-token",
                server.RUNTIME_TOKEN_HEADER_ENV: "token",
                server.RUNTIME_USER_ID_ENV: "user-1",
            },
            clear=False,
        ):
            config = server.load_config()
        self.assertEqual("http://board.test/api", config["api_url"])
        self.assertEqual("current-user-token", config["key"])
        self.assertEqual("user-1", config["user_id"])
        self.assertNotIn("_biz_line", config)

    def test_project_context_loads_project_by_numeric_primary_key(self):
        config = {"api_url": "http://example.test/api", "key": "secret"}
        responses = [
            {"programId": 2, "bizLine": "xianglong", "name": "任务宇宙"},
            [],
            [],
            {"data": [], "total": 0},
        ]
        with patch.object(server, "request_api", side_effect=responses) as request:
            context = server.project_context(config, 2)

        self.assertEqual(2, context["program"]["programId"])
        self.assertEqual("xianglong", context["program"]["bizLine"])
        self.assertEqual("/delivery/program", request.call_args_list[0].args[2])
        self.assertEqual({"programId": 2}, request.call_args_list[0].kwargs["query"])
        self.assertNotIn("/delivery/programs", [call.args[2] for call in request.call_args_list])

    def test_selected_stage_and_module_override_every_task(self):
        context = {
            "program": {"programId": 1},
            "stages": [{"stageKey": "s1"}, {"stageKey": "s2"}],
            "modules": [{"moduleKey": "api"}, {"moduleKey": "web"}],
            "items": [],
        }
        tasks = [
            {
                "ref": "build",
                "title": "Build endpoint",
                "benefit_tags": ["接口可复用"],
                "stage_key": "s2",
                "module_key": "web",
                "depends_on": [],
            }
        ]
        refs, order = server.validate_tasks(tasks, context, "s1", "api")
        self.assertEqual(["build"], order)
        self.assertEqual("s1", refs["build"]["stage_key"])
        self.assertEqual("api", refs["build"]["module_key"])

    def test_task_benefit_tags_are_required_and_normalized(self):
        context = {"program": {"programId": 1}, "stages": [], "modules": [], "items": []}
        with self.assertRaisesRegex(server.ToolFailure, "benefit_tags"):
            server.validate_tasks([{"ref": "build", "title": "Build endpoint", "depends_on": []}], context, "", "")

        refs, _ = server.validate_tasks([
            {"ref": "build", "title": "Build endpoint", "benefit_tags": [" 接口复用 ", "接口复用", "自动化"], "depends_on": []},
        ], context, "", "")
        self.assertEqual(["接口复用", "自动化"], refs["build"]["benefit_tags"])

        with self.assertRaisesRegex(server.ToolFailure, "最多 3"):
            server.validate_tasks([
                {"ref": "too-many", "title": "Too many tags", "benefit_tags": ["标签一", "标签二", "标签三", "标签四"], "depends_on": []},
            ], context, "", "")

    def test_cycle_is_rejected(self):
        tasks = [
            {"ref": "a", "depends_on": ["b"]},
            {"ref": "b", "depends_on": ["a"]},
        ]
        with self.assertRaisesRegex(server.ToolFailure, "存在环"):
            server.topological_order(tasks)

    def test_dependency_layers_allow_at_most_three_parallel_tasks(self):
        tasks = [
            {"ref": "a", "depends_on": []},
            {"ref": "b", "depends_on": []},
            {"ref": "c", "depends_on": []},
            {"ref": "d", "depends_on": ["a"]},
            {"ref": "e", "depends_on": ["b"]},
            {"ref": "f", "depends_on": ["c"]},
        ]
        self.assertEqual([["a", "b", "c"], ["d", "e", "f"]], server.dependency_layers(tasks))

    def test_rejects_more_than_three_parallel_tasks(self):
        context = {"program": {"programId": 1}, "stages": [], "modules": [], "items": []}
        tasks = [
            {"ref": ref, "title": ref, "benefit_tags": ["交付可用"], "depends_on": []}
            for ref in ("a", "b", "c", "d")
        ]
        with self.assertRaisesRegex(server.ToolFailure, "最多只能并行 3"):
            server.validate_tasks(tasks, context, "", "")

    def test_rejects_cross_layer_and_existing_task_dependencies(self):
        context = {
            "program": {"programId": 1},
            "stages": [],
            "modules": [],
            "items": [{"itemKey": "existing-item"}],
        }
        cross_layer = [
            {"ref": "a", "title": "A", "benefit_tags": ["交付可用"], "depends_on": []},
            {"ref": "b", "title": "B", "benefit_tags": ["交付可用"], "depends_on": ["a"]},
            {"ref": "c", "title": "C", "benefit_tags": ["交付可用"], "depends_on": ["a", "b"]},
        ]
        with self.assertRaisesRegex(server.ToolFailure, "跨越了执行层"):
            server.validate_tasks(cross_layer, context, "", "")
        external_dependency = [
            {"ref": "a", "title": "A", "benefit_tags": ["交付可用"], "depends_on": ["existing-item"]},
        ]
        with self.assertRaisesRegex(server.ToolFailure, "只能引用同批任务"):
            server.validate_tasks(external_dependency, context, "", "")

    def test_execution_queue_obeys_stage_and_dependencies(self):
        context = {
            "stages": [
                {"stageKey": "s1", "seq": 1, "title": "设计"},
                {"stageKey": "s2", "seq": 2, "title": "开发"},
            ],
            "modules": [{"moduleKey": "api"}],
            "items": [
                {"itemKey": "a", "stageKey": "s1", "moduleKey": "api", "status": "done", "sortOrder": 1},
                {"itemKey": "b", "stageKey": "s1", "moduleKey": "api", "status": "todo", "sortOrder": 2, "dependsOnItemKeys": ["a"]},
                {"itemKey": "c", "stageKey": "s1", "moduleKey": "api", "status": "todo", "sortOrder": 3, "dependsOnItemKeys": ["b"]},
                {"itemKey": "d", "stageKey": "s2", "moduleKey": "api", "status": "todo", "sortOrder": 1},
            ],
        }
        queue = server.execution_queue_from_context(context)
        self.assertEqual("s1", queue["currentStage"]["stageKey"])
        self.assertEqual(["b"], [item["itemKey"] for item in queue["readyTasks"]])
        self.assertEqual(["c"], [item["itemKey"] for item in queue["waitingTasks"]])

    def test_blocked_task_keeps_stage_current(self):
        context = {
            "stages": [{"stageKey": "s1", "seq": 1}, {"stageKey": "s2", "seq": 2}],
            "modules": [],
            "items": [
                {"itemKey": "a", "stageKey": "s1", "status": "blocked"},
                {"itemKey": "b", "stageKey": "s2", "status": "todo"},
            ],
        }
        queue = server.execution_queue_from_context(context)
        self.assertEqual("s1", queue["currentStage"]["stageKey"])
        self.assertEqual(["a"], [item["itemKey"] for item in queue["blockedTasks"]])

    def test_actor_can_resume_owned_running_task(self):
        context = {
            "stages": [{"stageKey": "s1", "seq": 1}],
            "modules": [],
            "items": [
                {"itemKey": "a", "stageKey": "s1", "status": "doing", "ownerName": "Codex"},
            ],
        }
        queue = server.execution_queue_from_context(context, actor_name="Codex")
        self.assertEqual(["a"], [item["itemKey"] for item in queue["resumableTasks"]])

    def test_module_filter_selects_that_modules_current_stage(self):
        context = {
            "stages": [{"stageKey": "s1", "seq": 1}, {"stageKey": "s2", "seq": 2}],
            "modules": [{"moduleKey": "api"}, {"moduleKey": "web"}],
            "items": [
                {"itemKey": "web-a", "stageKey": "s1", "moduleKey": "web", "status": "todo"},
                {"itemKey": "api-b", "stageKey": "s2", "moduleKey": "api", "status": "todo"},
            ],
        }
        queue = server.execution_queue_from_context(context, selected_module="api")
        self.assertEqual("s2", queue["currentStage"]["stageKey"])
        self.assertEqual(["api-b"], [item["itemKey"] for item in queue["readyTasks"]])

    def test_patch_execution_item_sends_version_and_transition(self):
        with patch.object(server, "request_api", return_value={"itemKey": "a", "version": 4}) as request:
            server.patch_execution_item(
                {"api_url": "http://example.test/api", "key": "secret"},
                1,
                {"itemKey": "a", "version": 3},
                "doing",
                "Codex",
                "start",
                1,
            )
        body = request.call_args.kwargs["body"]
        self.assertEqual(3, body["version"])
        self.assertEqual("doing", body["status"])
        self.assertEqual("Codex", body["ownerName"])

    def test_claim_returns_new_version_from_patch(self):
        context = {
            "program": {"programId": 1},
            "stages": [{"stageKey": "s1", "seq": 1}],
            "modules": [],
            "items": [
                {
                    "itemKey": "a",
                    "stageKey": "s1",
                    "status": "todo",
                    "version": 3,
                    "progress": 0,
                    "dependsOnItemKeys": [],
                }
            ],
        }
        with (
            patch.object(server, "load_config", return_value={"api_url": "http://example.test/api", "key": "secret"}),
            patch.object(server, "project_context", return_value=context),
            patch.object(server, "patch_execution_item", return_value={"itemKey": "a", "status": "doing", "version": 4}),
        ):
            result = server.claim_next_task({"program_id": 1})
        self.assertEqual("claimed", result["action"])
        self.assertEqual(4, result["task"]["version"])

    def test_finish_requires_running_task_owned_by_actor(self):
        context = {
            "program": {"programId": 1},
            "stages": [],
            "modules": [],
            "items": [
                {"itemKey": "a", "status": "doing", "version": 4, "ownerName": "Other", "progress": 20}
            ],
        }
        with (
            patch.object(server, "load_config", return_value={"api_url": "http://example.test/api", "key": "secret"}),
            patch.object(server, "project_context", return_value=context),
        ):
            with self.assertRaisesRegex(server.ToolFailure, "不能结束"):
                server.finish_execution_task(
                    {
                        "program_id": 1,
                        "item_key": "a",
                        "version": 4,
                        "outcome": "done",
                        "comment": "verified",
                        "actor_name": "Codex",
                    }
                )

    def test_config_is_private_and_key_is_masked(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            with patch.object(server, "CONFIG_PATH", path):
                server.save_config(
                    {
                        "api_url": "http://localhost:8691/api",
                        "key": "secret-key",
                        "key_header": "token",
                    }
                )
                self.assertEqual(0o600, os.stat(path).st_mode & 0o777)
                self.assertEqual("***-key", server.configuration()["key"])

    def test_created_tasks_report_pending_session_bindings(self):
        context = {"program": {"programId": 1}, "stages": [], "modules": [], "items": []}
        with (
            patch.object(server, "load_config", return_value={"api_url": "http://example.test/api", "key": "secret"}),
            patch.object(server, "project_context", return_value=context),
            patch.object(server, "request_api", return_value={"itemKey": "task-a"}),
        ):
            result = server.create_tasks({"program_id": 1, "tasks": [{"ref": "a", "title": "A", "benefit_tags": ["自动化收益"], "depends_on": []}]})
        self.assertEqual([{"programId": 1, "itemKey": "task-a"}], result["sessionBindingsPending"])
        self.assertEqual("doc/module/task-a/文档.md", result["created"][0]["requirementDocumentPath"])

    def test_created_tasks_write_readable_requirement_document(self):
        context = {
            "program": {"programId": 1},
            "stages": [{"stageKey": "s1", "title": "开发"}],
            "modules": [{"moduleKey": "api", "name": "接口"}],
            "items": [],
        }
        with (
            patch.object(server, "load_config", return_value={"api_url": "http://example.test/api", "key": "secret"}),
            patch.object(server, "project_context", return_value=context),
            patch.object(server, "request_api", return_value={"itemKey": "task-a"}) as request,
        ):
            result = server.create_tasks({
                "program_id": 1,
                "tasks": [{
                    "ref": "a", "title": "Build endpoint", "description": "Create the endpoint.", "benefit_tags": ["接口复用"],
                    "stage_key": "s1", "module_key": "api", "acceptance_criteria": ["Tests pass"], "depends_on": [],
                }],
            })
        body = request.call_args.kwargs["body"]
        self.assertIn("# Build endpoint", body["requirementDocument"])
        self.assertIn("## 验收标准", body["requirementDocument"])
        self.assertIn("- Tests pass", body["requirementDocument"])
        self.assertEqual("doc/api/task-a/文档.md", result["created"][0]["requirementDocumentPath"])

    def test_created_tasks_inherit_the_requirement_primary_owner(self):
        context = {"program": {"programId": 1}, "stages": [], "modules": [], "items": []}
        created_bodies = []

        def request(_config, method, path, **kwargs):
            if method == "GET" and path == "/delivery/requirement":
                return {
                    "requirementKey": "req-a",
                    "owners": [
                        {"id": "owner-1", "name": "需求负责人"},
                        {"id": "owner-2", "name": "第二负责人"},
                    ],
                }
            if method == "POST" and path == "/delivery/item/create":
                created_bodies.append(kwargs["body"])
                return {"itemKey": kwargs["body"]["itemKey"]}
            self.fail(f"unexpected request: {method} {path}")

        with (
            patch.object(server, "load_config", return_value={"api_url": "http://example.test/api", "key": "secret"}),
            patch.object(server, "project_context", return_value=context),
            patch.object(server, "request_api", side_effect=request),
        ):
            result = server.create_tasks({
                "program_id": 1,
                "requirement_key": "req-a",
                "tasks": [{"ref": "a", "title": "A", "benefit_tags": ["自动化收益"], "depends_on": []}],
            })

        self.assertEqual("owner-1", created_bodies[0]["ownerId"])
        self.assertEqual("需求负责人", created_bodies[0]["ownerName"])
        self.assertIn("- 负责人：需求负责人", created_bodies[0]["requirementDocument"])
        self.assertEqual("owner-1", result["created"][0]["ownerId"])

    def test_create_tasks_appends_the_enabled_prototype_task_last(self):
        context = {
            "program": {"programId": 1},
            "stages": [{"stageKey": "s1", "title": "开发"}],
            "modules": [{"moduleKey": "web", "name": "控制台"}],
            "items": [],
        }
        created_bodies = []

        def request(config, method, path, **kwargs):
            if method == "GET" and path == "/delivery/requirement":
                return {"requirementKey": "req-a", "generatePrototype": True}
            if method == "POST" and path == "/delivery/item/create":
                created_bodies.append(kwargs["body"])
                return {"itemKey": kwargs["body"]["itemKey"]}
            self.fail(f"unexpected request: {method} {path}")

        with (
            patch.object(server, "load_config", return_value={"api_url": "http://example.test/api", "key": "secret"}),
            patch.object(server, "project_context", return_value=context),
            patch.object(server, "request_api", side_effect=request),
        ):
            result = server.create_tasks({
                "program_id": 1,
                "requirement_key": "req-a",
                "generate_prototype": True,
                "tasks": [
                    {"ref": "api", "title": "实现接口", "benefit_tags": ["能力可用"], "stage_key": "s1", "module_key": "web", "depends_on": []},
                    {"ref": "page", "title": "实现页面", "benefit_tags": ["用户可见"], "stage_key": "s1", "module_key": "web", "depends_on": ["api"]},
                ],
            })

        self.assertEqual(3, result["createdCount"])
        self.assertEqual(["能力可用"], created_bodies[0]["benefitTags"])
        self.assertTrue(created_bodies[-1]["prototypeTask"])
        self.assertEqual("生成需求原型图", created_bodies[-1]["title"])
        self.assertEqual([created_bodies[1]["itemKey"]], created_bodies[-1]["dependsOnItemKeys"])

    def test_create_tasks_refuses_multiple_tasks_when_the_requirement_disables_splitting(self):
        context = {"program": {"programId": 1}, "stages": [], "modules": [], "items": []}

        def request(_config, method, path, **kwargs):
            if method == "GET" and path == "/delivery/requirement":
                return {"requirementKey": "req-a", "splitTasks": False}
            self.fail(f"unexpected request: {method} {path}")

        with (
            patch.object(server, "load_config", return_value={"api_url": "http://example.test/api", "key": "secret"}),
            patch.object(server, "project_context", return_value=context),
            patch.object(server, "request_api", side_effect=request),
        ):
            with self.assertRaisesRegex(server.ToolFailure, "只能创建一条"):
                server.create_tasks({
                    "program_id": 1,
                    "requirement_key": "req-a",
                    "tasks": [
                        {"ref": "a", "title": "A", "benefit_tags": ["收益"], "depends_on": []},
                        {"ref": "b", "title": "B", "benefit_tags": ["收益"], "depends_on": ["a"]},
                    ],
                })

    def test_bind_execution_session_uses_generic_fields(self):
        with (
            patch.object(server, "load_config", return_value={"api_url": "http://example.test/api", "key": "secret"}),
            patch.object(server, "request_api", return_value={"version": 1}) as request,
        ):
            server.bind_execution_session({
                "program_id": 1,
                "item_key": "task-a",
                "executor_type": "claude",
                "external_session_id": "session-1",
                "external_host_id": "host-1",
                "status": "running",
                "metadata": {"workspace": "universe"},
            })
        self.assertEqual("/delivery/item/execution-session/bind", request.call_args.args[2])
        body = request.call_args.kwargs["body"]
        self.assertEqual("claude", body["executorType"])
        self.assertEqual("session-1", body["externalSessionId"])
        self.assertNotIn("codexSessionId", body)

    def test_update_execution_session_status_sends_version(self):
        with (
            patch.object(server, "load_config", return_value={"api_url": "http://example.test/api", "key": "secret"}),
            patch.object(server, "request_api", return_value={"version": 3}) as request,
        ):
            server.update_execution_session_status({
                "program_id": 1, "item_key": "task-a", "executor_type": "codex",
                "version": 2, "status": "completed",
            })
        body = request.call_args.kwargs["body"]
        self.assertEqual(2, body["version"])
        self.assertEqual("completed", body["status"])

    def test_update_task_reads_current_version_and_patches_planning_fields(self):
        current = {"itemKey": "task-a", "version": 7, "dependsOnItemKeys": []}
        updated = {"itemKey": "task-a", "version": 8, "title": "更新后的标题"}
        with (
            patch.object(server, "load_config", return_value={"api_url": "http://example.test/api", "key": "secret"}),
            patch.object(server, "request_api", side_effect=[current, updated]) as request,
        ):
            result = server.update_task({
                "program_id": 1,
                "item_key": "task-a",
                "title": "更新后的标题",
                "description": "调整任务范围",
                "benefit_tags": ["可维护性", "可维护性", "交付清晰"],
                "comment": "根据评审意见调整",
            })

        self.assertEqual(7, result["previousVersion"])
        self.assertEqual(8, result["version"])
        self.assertEqual("/delivery/item", request.call_args_list[0].args[2])
        self.assertEqual({"programId": 1, "itemKey": "task-a"}, request.call_args_list[0].kwargs["query"])
        self.assertEqual("/delivery/item/patch", request.call_args_list[1].args[2])
        body = request.call_args_list[1].kwargs["body"]
        self.assertEqual(7, body["version"])
        self.assertEqual(["可维护性", "交付清晰"], body["benefitTags"])
        self.assertEqual("根据评审意见调整", body["comment"])

    def test_update_task_requirement_synchronizes_primary_owner(self):
        current = {"itemKey": "task-a", "version": 2, "dependsOnItemKeys": []}
        requirement = {"requirementKey": "req-b", "owners": [{"id": "owner-b", "name": "新负责人"}]}
        with (
            patch.object(server, "load_config", return_value={"api_url": "http://example.test/api", "key": "secret"}),
            patch.object(server, "request_api", side_effect=[current, requirement, {"itemKey": "task-a", "version": 3}]) as request,
        ):
            server.update_task({"program_id": 1, "item_key": "task-a", "requirement_key": "req-b"})

        body = request.call_args_list[-1].kwargs["body"]
        self.assertEqual("req-b", body["requirementKey"])
        self.assertEqual("owner-b", body["ownerId"])
        self.assertEqual("新负责人", body["ownerName"])

    def test_update_task_dependencies_replaces_all_direct_dependencies(self):
        current = {"itemKey": "task-c", "version": 4, "dependsOnItemKeys": ["task-a"]}
        with (
            patch.object(server, "load_config", return_value={"api_url": "http://example.test/api", "key": "secret"}),
            patch.object(server, "request_api", side_effect=[current, {"itemKey": "task-c", "version": 5}]) as request,
        ):
            server.update_task_dependencies({
                "program_id": 1,
                "item_key": "task-c",
                "depends_on_item_keys": ["task-a", "task-b", "task-a"],
                "dependency_source_sides": {"task-a": "right", "task-b": "bottom"},
                "dependency_target_sides": {"task-a": "left", "task-b": "top"},
            })

        body = request.call_args_list[-1].kwargs["body"]
        self.assertEqual(["task-a", "task-b"], body["dependsOnItemKeys"])
        self.assertEqual({"task-a": "right", "task-b": "bottom"}, body["dependencySourceSides"])
        self.assertEqual({"task-a": "left", "task-b": "top"}, body["dependencyTargetSides"])

    def test_delete_task_dependencies_preserves_remaining_dependency_anchors(self):
        current = {
            "itemKey": "task-c",
            "version": 4,
            "dependsOnItemKeys": ["task-a", "task-b"],
            "dependencySourceSides": {"task-a": "right", "task-b": "bottom"},
            "dependencyTargetSides": {"task-a": "left", "task-b": "top"},
        }
        with (
            patch.object(server, "load_config", return_value={"api_url": "http://example.test/api", "key": "secret"}),
            patch.object(server, "request_api", side_effect=[current, {"itemKey": "task-c", "version": 5}]) as request,
        ):
            result = server.delete_task_dependencies({
                "program_id": 1,
                "item_key": "task-c",
                "predecessor_item_keys": ["task-b"],
            })

        body = request.call_args_list[-1].kwargs["body"]
        self.assertEqual(["task-a"], body["dependsOnItemKeys"])
        self.assertEqual({"task-a": "right"}, body["dependencySourceSides"])
        self.assertEqual({"task-a": "left"}, body["dependencyTargetSides"])
        self.assertEqual(["task-b"], result["removedPredecessorItemKeys"])

    def test_delete_task_dependencies_rejects_nonexistent_edge(self):
        current = {"itemKey": "task-c", "version": 4, "dependsOnItemKeys": ["task-a"]}
        with (
            patch.object(server, "load_config", return_value={"api_url": "http://example.test/api", "key": "secret"}),
            patch.object(server, "request_api", return_value=current),
        ):
            with self.assertRaisesRegex(server.ToolFailure, "不是当前任务的依赖"):
                server.delete_task_dependencies({
                    "program_id": 1,
                    "item_key": "task-c",
                    "predecessor_item_keys": ["task-b"],
                })

    def test_only_planning_and_structure_tools_are_registered(self):
        names = {tool["name"] for tool in server.TOOLS}
        self.assertTrue({
            "create_task_board_project",
            "create_task_board_stage",
            "create_task_board_module",
            "create_task_board_tasks",
            "update_task_board_task",
            "update_task_board_task_dependencies",
            "delete_task_board_task_dependencies",
        }.issubset(names))
        self.assertTrue({
            "bind_task_execution_session",
            "get_task_execution_sessions",
            "update_task_execution_session_status",
            "get_task_execution_queue",
            "claim_next_task",
            "finish_execution_task",
        }.isdisjoint(names))

    def test_create_project_refuses_existing_code(self):
        with (
            patch.object(server, "load_config", return_value={"api_url": "http://example.test/api", "key": "secret"}),
            patch.object(server, "request_api", return_value=[{"programId": 1, "programCode": "existing"}]),
        ):
            with self.assertRaisesRegex(server.ToolFailure, "已存在"):
                server.create_project({"program_code": "existing", "name": "Existing"})

    def test_create_project_returns_numeric_primary_key(self):
        with (
            patch.object(server, "load_config", return_value={"api_url": "http://example.test/api", "key": "secret"}),
            patch.object(
                server,
                "request_api",
                side_effect=[[], None, [{"programId": 7, "programCode": "universe", "name": "Universe"}]],
            ) as request,
        ):
            result = server.create_project({"program_code": "universe", "name": "Universe"})

        self.assertEqual(7, result["programId"])
        self.assertEqual("universe", result["programCode"])
        self.assertEqual(0, request.call_args_list[1].kwargs["body"]["programId"])

    def test_create_stage_appends_sequence(self):
        context = {
            "program": {"programId": 1},
            "stages": [{"stageKey": "s1", "seq": 3}],
            "modules": [],
            "items": [],
        }
        with (
            patch.object(server, "load_config", return_value={"api_url": "http://example.test/api", "key": "secret"}),
            patch.object(server, "project_context", return_value=context),
            patch.object(server, "request_api", return_value=None) as request,
        ):
            result = server.create_stage({"program_id": 1, "stage_key": "s2", "tag": "开发", "title": "完成开发"})
        self.assertEqual(4, result["seq"])
        self.assertEqual(4, request.call_args.kwargs["body"]["seq"])

    def test_create_module_appends_sequence(self):
        context = {
            "program": {"programId": 1},
            "stages": [],
            "modules": [{"moduleKey": "api", "seq": 2}],
            "items": [],
        }
        with (
            patch.object(server, "load_config", return_value={"api_url": "http://example.test/api", "key": "secret"}),
            patch.object(server, "project_context", return_value=context),
            patch.object(server, "request_api", return_value=None) as request,
        ):
            result = server.create_module({"program_id": 1, "module_key": "web", "name": "Web", "weight": 20})
        self.assertEqual(3, result["seq"])
        self.assertEqual(20, request.call_args.kwargs["body"]["weight"])


if __name__ == "__main__":
    unittest.main()
