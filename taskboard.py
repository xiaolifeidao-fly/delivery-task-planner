#!/usr/bin/env python3
"""任务面板命令行入口。

技能通过它调用任务面板接口，不再需要 MCP：服务端地址写死在 server.py 里，
凭证由控制台心跳写进 ~/.config/delivery-task-planner/credential.json。

    taskboard.py actions                     列出全部动作和参数
    taskboard.py get-task-board-context --program-id 4
    taskboard.py create-task-board-tasks --json @/tmp/tasks.json

成功时把结果 JSON 打到 stdout；失败时把原因打到 stderr 并以 1 退出。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

SERVER_PATH = Path(__file__).resolve().parent / "server.py"
SPEC = importlib.util.spec_from_file_location("delivery_task_planner_server", SERVER_PATH)
planner = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(planner)


def command_of(action: str) -> str:
    return action.replace("_", "-")


def action_of(command: str) -> str:
    return command.replace("-", "_")


def load_json_argument(raw: str) -> dict[str, Any]:
    """--json 支持直接给 JSON，也支持 @文件 —— 任务数组很长，命令行放不下。"""
    raw = raw.strip()
    if raw.startswith("@"):
        raw = Path(raw[1:]).expanduser().read_text(encoding="utf-8")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("--json 必须是 JSON 对象")
    return value


def scalar_flags(schema: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """只给标量参数生成扁平选项；数组和对象一律走 --json。"""
    properties = schema.get("properties") or {}
    return {
        name: definition
        for name, definition in properties.items()
        if definition.get("type") in {"string", "integer", "number", "boolean"}
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="taskboard.py", description="Universe 交付任务面板命令行")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("actions", help="列出全部动作及其参数")

    for action in planner.ACTIONS:
        schema = action.get("inputSchema") or {}
        subparser = subparsers.add_parser(
            command_of(action["name"]),
            help=action.get("title") or action["name"],
            description=action.get("description") or "",
        )
        subparser.add_argument("--json", dest="json_payload", help="完整参数 JSON，或 @文件路径")
        for name, definition in scalar_flags(schema).items():
            option = f"--{command_of(name)}"
            if definition.get("type") == "boolean":
                subparser.add_argument(option, dest=name, choices=["true", "false"], help=definition.get("description"))
            elif definition.get("type") == "integer":
                subparser.add_argument(option, dest=name, type=int, help=definition.get("description"))
            else:
                subparser.add_argument(option, dest=name, help=definition.get("description"))
    return parser


def arguments_of(action: str, namespace: argparse.Namespace) -> dict[str, Any]:
    payload = load_json_argument(namespace.json_payload) if namespace.json_payload else {}
    schema = next(item.get("inputSchema") or {} for item in planner.ACTIONS if item["name"] == action)
    for name, definition in scalar_flags(schema).items():
        value = getattr(namespace, name, None)
        if value is None:
            continue
        # 扁平选项覆盖 --json 里的同名字段：命令行写在后面，意图更明确。
        payload[name] = value == "true" if definition.get("type") == "boolean" else value
    return payload


def describe_actions() -> list[dict[str, Any]]:
    return [
        {
            "command": command_of(action["name"]),
            "title": action.get("title") or "",
            "description": action.get("description") or "",
            "parameters": (action.get("inputSchema") or {}).get("properties") or {},
            "required": (action.get("inputSchema") or {}).get("required") or [],
        }
        for action in planner.ACTIONS
    ]


def main(argv: list[str] | None = None) -> int:
    namespace = build_parser().parse_args(argv)
    if namespace.command == "actions":
        print(json.dumps(describe_actions(), ensure_ascii=False, indent=2))
        return 0
    action = action_of(namespace.command)
    try:
        arguments = arguments_of(action, namespace)
        value = planner.run_action(action, arguments)
    except planner.ToolFailure as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"参数无效：{exc}", file=sys.stderr)
        return 1
    print(json.dumps(value, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
