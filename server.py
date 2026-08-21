#!/usr/bin/env python3
"""MCP tools for planning and writing Universe delivery-board tasks."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any


CONFIG_PATH = Path(
    os.environ.get(
        "DELIVERY_TASK_PLANNER_CONFIG",
        str(Path.home() / ".config" / "delivery-task-planner" / "config.json"),
    )
)
LEGACY_CONFIG_PATH = Path.home() / ".config" / "codex" / "delivery-task-planner.json"
# 配置文件只维护后端接口地址；token 与 user_id 由控制台心跳接口送进来，单独落在这里。
CREDENTIAL_PATH = Path(
    os.environ.get(
        "DELIVERY_TASK_PLANNER_CREDENTIAL",
        str(CONFIG_PATH.parent / "credential.json"),
    )
)
VALID_KINDS = {"gap", "capability", "asset"}
MAX_BENEFIT_TAGS = 3
MAX_BENEFIT_TAG_LENGTH = 32
MAX_PARALLEL_TASKS = 3
ACTIVE_STATUSES = {"todo", "doing", "blocked"}
RUNTIME_PROJECT_ID_ENV = "DELIVERY_TASK_BOARD_PROJECT_ID"
RUNTIME_TOKEN_ENV = "DELIVERY_TASK_BOARD_TOKEN"
RUNTIME_TOKEN_HEADER_ENV = "DELIVERY_TASK_BOARD_TOKEN_HEADER"
RUNTIME_USER_ID_ENV = "DELIVERY_TASK_BOARD_USER_ID"
RUNTIME_API_URL_ENV = "DELIVERY_TASK_BOARD_API_URL"
RUNTIME_WRITE_MODE_ENV = "DELIVERY_TASK_BOARD_WRITE_MODE"
# 服务端地址是固定的：插件不从浏览器 Origin、也不从配置文件推断面板地址。
TASK_BOARD_API_URL = "http://47.110.3.214:8691/api"


class ToolFailure(Exception):
    pass


def assert_write_allowed(action: str) -> None:
    """需求梳理的预览轮次只给只读工具；写入要等用户在任务面板确认。

    环境变量缺省表示允许写入，普通 MCP 用法不受影响；只有面板发起的
    梳理会话会把它设成 preview。
    """
    if os.environ.get(RUNTIME_WRITE_MODE_ENV, "").strip().lower() != "preview":
        return
    raise ToolFailure(
        f"当前是需求梳理的预览阶段，不能{action}。"
        "请先给出拆解预览供用户评审，等用户在任务面板点击「确认并写入」后再调用写入工具。"
    )


def require_program_id(value: Any, label: str = "项目标识") -> int:
    if isinstance(value, bool):
        raise ToolFailure(f"{label}必须是项目表的数值主键。")
    try:
        program_id = int(str(value).strip())
    except (TypeError, ValueError):
        raise ToolFailure(f"{label}必须是项目表的数值主键。") from None
    if program_id <= 0:
        raise ToolFailure(f"{label}必须是项目表的正整数主键。")
    return program_id


def program_value_of(arguments: dict[str, Any]) -> tuple[int, bool]:
    raw_runtime_program_id = os.environ.get(RUNTIME_PROJECT_ID_ENV, "").strip()
    explicit_value = arguments.get("program_id")
    runtime_program_id = require_program_id(raw_runtime_program_id) if raw_runtime_program_id else 0
    if runtime_program_id:
        if explicit_value not in (None, "") and require_program_id(explicit_value) != runtime_program_id:
            raise ToolFailure(
                f"program_id 必须传当前项目主键 {runtime_program_id}，不能切换到其他项目。"
            )
        return runtime_program_id, False
    if explicit_value not in (None, ""):
        return require_program_id(explicit_value), False
    raise ToolFailure("请提供项目表的数值主键 program_id。")


def load_config() -> dict[str, Any]:
    runtime_token = os.environ.get(RUNTIME_TOKEN_ENV, "").strip()
    if runtime_token:
        config = {
            "api_url": TASK_BOARD_API_URL,
            "key": runtime_token,
            "key_header": os.environ.get(RUNTIME_TOKEN_HEADER_ENV, "token").strip() or "token",
            "user_id": os.environ.get(RUNTIME_USER_ID_ENV, "task-executor").strip() or "task-executor",
            # 面板入口注入的凭证，跟着控制台当前登录账号走。
            "_source": "runtime",
        }
        return config
    settings = stored_settings()
    credential = load_credential()
    # 老版本把 token 写在配置文件里，升级后第一次心跳到达前仍然可用。
    key = credential.get("key") or str(settings.get("key") or "").strip()
    if not key:
        raise ToolFailure(
            "尚未收到任务面板凭证。请在控制台登录并保持任务面板打开，"
            "控制台会通过心跳接口把 token 和 user_id 送给插件。"
        )
    user_id = credential.get("user_id") or str(settings.get("user_id") or "").strip()
    return {
        "api_url": TASK_BOARD_API_URL,
        "key": key,
        "key_header": str(settings.get("key_header") or "token").strip() or "token",
        "user_id": user_id or token_subject(key) or "task-executor",
        "_source": "heartbeat" if credential.get("key") else "config",
    }


def stored_settings() -> dict[str, Any]:
    """读取配置文件里的接口地址等设置；读不出来就当没配置过。"""
    config_path = CONFIG_PATH if CONFIG_PATH.exists() else LEGACY_CONFIG_PATH
    if not config_path.exists():
        return {}
    try:
        settings = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ToolFailure(f"无法读取插件配置：{exc}") from exc
    return settings if isinstance(settings, dict) else {}


def load_credential() -> dict[str, Any]:
    """读取心跳接口存下来的当前控制台账号凭证。

    永不抛错：凭证文件缺失或损坏时退回空字典，由调用方决定怎么提示。
    """
    try:
        if not CREDENTIAL_PATH.exists():
            return {}
        credential = json.loads(CREDENTIAL_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(credential, dict):
        return {}
    key = str(credential.get("key") or "").strip()
    if not key:
        return {}
    return {
        "key": key,
        "user_id": str(credential.get("user_id") or "").strip() or token_subject(key),
        "updated_at": int(credential.get("updated_at") or 0),
    }


def save_credential(token: str, user_id: str = "") -> bool:
    """把心跳送来的凭证落盘，凭证内容有变化时返回 True。

    永不抛错：心跳失败不该影响控制台，也不该打断正在服务的请求。
    """
    token = str(token or "").strip()
    if not token:
        return False
    # user_id 只是标签，真正认账号的是 token 本身；能从凭证里读出来就以它为准。
    user_id = token_subject(token) or str(user_id or "").strip() or "task-executor"
    current = load_credential()
    changed = current.get("key") != token or current.get("user_id") != user_id
    try:
        write_json_file(
            CREDENTIAL_PATH,
            {"key": token, "user_id": user_id, "updated_at": int(time.time())},
        )
    except OSError:
        return False
    return changed


def bridge_api_url() -> str:
    """固定的服务端地址：普通 MCP 会话和本地桥接共用，不可配置。"""
    return TASK_BOARD_API_URL


def normalize_api_url(value: str) -> str:
    value = value.strip().rstrip("/")
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ToolFailure("接口地址必须是有效的 http 或 https URL。")
    if parsed.query or parsed.fragment:
        raise ToolFailure("接口地址不能包含 query 或 fragment。")
    return value if value.endswith("/api") else value + "/api"


def save_config(config: dict[str, Any]) -> None:
    # 下划线开头的是运行期标记（凭证来源、桥接项目），不属于持久化配置。
    # key/user_id 也不再写进配置文件：它们由控制台心跳接口维护在凭证文件里。
    config = {
        key: value
        for key, value in config.items()
        if not key.startswith("_") and key not in {"key", "user_id"}
    }
    write_json_file(CONFIG_PATH, config)


def write_json_file(path: Path, payload: dict[str, Any]) -> None:
    """同目录临时文件 + 原子替换，避免半截内容被别的会话读到。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".delivery-task-planner-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def token_subject(token: str) -> str:
    """Read the user id out of a panel JWT without verifying it.

    The value is only a label: authorization is decided by the server from the
    token itself. Any malformed token simply yields an empty label.
    """
    try:
        payload = token.split(".")[1]
        raw = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        subject = json.loads(raw).get("sub")
    except (IndexError, ValueError, TypeError, json.JSONDecodeError):
        return ""
    return str(subject) if subject not in (None, "") else ""


def remember_browser_identity(token: str, user_id: str = "") -> None:
    """Persist the console's current credential as the fallback identity.

    Sessions started from the task panel get the logged-in user's token through
    the runtime environment. Plain MCP sessions have no such environment, so they
    read the credential file that the console keeps fresh — through the
    heartbeat endpoint every minute, and through every bridge request on top of
    that. Without the refresh the fallback kept writing as the *previous*
    account after a console switch, which the panel rejects as a plain
    permission error and sends the investigation the wrong way.

    Never raises: a failure here must not break the request being served.
    """
    save_credential(token, user_id)


# 服务端对失效凭证的两种说法：token 解不开，或者用户的 token 版本已经作废。
AUTH_FAILURE_HINTS = ("not login", "登录凭证已失效")


def is_auth_failure(message: str) -> bool:
    lowered = str(message).lower()
    return any(hint.lower() in lowered for hint in AUTH_FAILURE_HINTS)


def refresh_credential(config: dict[str, Any]) -> bool:
    """把桥接保持最新的那份凭证换进 config，成功换到新值返回 True。

    面板入口启动的会话在**启动那一刻**把 token 冻进环境变量，之后一直用它。
    token 过期、或者中途在控制台换了账号，这个长期会话就会一直拿着废凭证请求，
    服务端只回一句 not login —— 用户明明是登录状态，看着像面板坏了。
    凭证文件那份由控制台心跳每分钟刷新，所以失效时回头读它就能自愈。
    """
    credential = load_credential()
    key = credential.get("key") or ""
    if not key:
        # 心跳还没送过凭证时，退回老版本写在配置文件里的那份。
        try:
            key = str(stored_settings().get("key") or "").strip()
        except ToolFailure:
            return False
    if not key or key == config.get("key"):
        return False
    # 原地替换：同一个 config 会在一次工具调用里被反复使用，换掉之后后续请求直接复用。
    config["key"] = key
    subject = credential.get("user_id") or token_subject(key)
    if subject:
        config["user_id"] = subject
    return True


def request_api(
    config: dict[str, Any],
    method: str,
    path: str,
    *,
    query: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
    allow_credential_refresh: bool = True,
) -> Any:
    query_values = {key: value for key, value in (query or {}).items() if value not in (None, "")}
    url = config["api_url"] + path
    if query_values:
        url += "?" + urllib.parse.urlencode(query_values)
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        config.get("key_header", "token"): config["key"],
        "X-User-ID": config.get("user_id", "task-executor"),
    }
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=payload, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise ToolFailure(f"接口返回 HTTP {exc.code}：{detail[:500]}") from exc
    except urllib.error.URLError as exc:
        raise ToolFailure(f"无法连接任务面板接口：{exc.reason}") from exc
    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ToolFailure("任务面板接口返回了非 JSON 内容。") from exc
    if not isinstance(envelope, dict) or not envelope.get("success"):
        if isinstance(envelope, dict):
            message = envelope.get("error") or envelope.get("message") or "未知错误"
        else:
            message = "响应格式错误"
        if allow_credential_refresh and is_auth_failure(message) and refresh_credential(config):
            return request_api(
                config, method, path, query=query, body=body, allow_credential_refresh=False
            )
        if is_auth_failure(message):
            raise ToolFailure(
                "任务面板登录凭证已失效。请在控制台重新登录，再从任务面板入口重新发起本次操作。"
            )
        raise ToolFailure(f"任务面板接口请求失败：{message}")
    return envelope.get("data")


def project_context(config: dict[str, Any], program_value: int) -> dict[str, Any]:
    program_id = require_program_id(program_value)
    program = request_api(
        config,
        "GET",
        "/delivery/program",
        query={"programId": program_id},
    )
    if not isinstance(program, dict):
        raise ToolFailure("项目详情接口返回格式错误。")
    if require_program_id(program.get("programId")) != program_id:
        raise ToolFailure("任务面板项目上下文校验失败。")
    program["programId"] = program_id
    stages = request_api(config, "GET", "/delivery/stages", query={"programId": program_id}) or []
    modules = request_api(config, "GET", "/delivery/modules", query={"programId": program_id}) or []
    items: list[dict[str, Any]] = []
    item_total = 0
    page_index = 1
    while True:
        item_page = request_api(
            config,
            "GET",
            "/delivery/items",
            query={"programId": program_id, "pageIndex": page_index, "pageSize": 200},
        ) or {}
        if not isinstance(item_page, dict):
            raise ToolFailure("任务列表接口返回格式错误。")
        page_items = item_page.get("data") or []
        if not isinstance(page_items, list):
            raise ToolFailure("任务列表接口 data 格式错误。")
        items.extend(page_items)
        item_total = int(item_page.get("total") or len(items))
        if not page_items or len(items) >= item_total:
            break
        page_index += 1
    return {
        "program": program,
        "stages": stages,
        "modules": modules,
        "items": items,
        "itemTotal": item_total,
    }


def require_option(value: str, options: list[dict[str, Any]], key: str, label: str) -> None:
    if value and value not in {str(item.get(key, "")) for item in options}:
        raise ToolFailure(f"{label}“{value}”不属于所选项目。")


def requirement_record(config: dict[str, Any], program_id: int, requirement_key: str) -> dict[str, Any]:
    """Read one requirement from the task board; empty dict when the batch has no requirement."""
    if not requirement_key:
        return {}
    requirement = request_api(
        config,
        "GET",
        "/delivery/requirement",
        query={"programId": program_id, "requirementKey": requirement_key},
    )
    if not isinstance(requirement, dict):
        raise ToolFailure("需求详情接口返回格式错误。")
    return requirement


def primary_requirement_owner(
    config: dict[str, Any],
    program_id: int,
    requirement_key: str,
    requirement: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """Return the requirement's first primary owner for task-level ownership.

    需求允许多个主负责人，而任务目前只有一个 ownerId / ownerName。拆解时统一
    采用需求主负责人列表的第一位，避免每条任务由模型自行猜测或漏填负责人。
    """
    if not requirement_key:
        return "", ""
    if requirement is None:
        requirement = requirement_record(config, program_id, requirement_key)
    owners = requirement.get("owners") or []
    if not isinstance(owners, list):
        raise ToolFailure("需求负责人字段格式错误，无法继承任务负责人。")
    for owner in owners:
        if not isinstance(owner, dict):
            continue
        owner_id = str(owner.get("id") or "").strip()
        if not owner_id:
            continue
        owner_name = str(owner.get("name") or owner_id).strip()
        if len(owner_id) > 64 or len(owner_name) > 64:
            raise ToolFailure("需求首位主负责人超过任务负责人字段长度，无法继承。")
        return owner_id, owner_name
    return "", ""


def safe_key_part(value: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return value[:24]


def make_item_key(program_id: int, task: dict[str, Any]) -> str:
    prefix = safe_key_part(str(task.get("module_key") or task.get("ref") or "task")) or "task"
    seed = "|".join((str(program_id), str(task.get("ref", "")), str(task.get("title", "")), uuid.uuid4().hex))
    suffix = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:10]
    return f"{prefix}-{suffix}"[:64]


def dependency_layers(tasks: list[dict[str, Any]]) -> list[list[str]]:
    """Return execution layers while enforcing the planner's compact DAG rules."""
    refs = {task["ref"] for task in tasks}
    incoming = {task["ref"]: {dep for dep in task.get("depends_on", []) if dep in refs} for task in tasks}
    ready = sorted(ref for ref, dependencies in incoming.items() if not dependencies)
    layers: list[list[str]] = []
    layer_by_ref: dict[str, int] = {}
    while ready:
        layer = ready
        if len(layer) > MAX_PARALLEL_TASKS:
            raise ToolFailure(
                f"第 {len(layers) + 1} 层有 {len(layer)} 个可并行任务，"
                f"同一执行层最多只能并行 {MAX_PARALLEL_TASKS} 个任务。"
            )
        layer_index = len(layers)
        layers.append(layer)
        for ref in layer:
            layer_by_ref[ref] = layer_index
        next_ready: list[str] = []
        for candidate in sorted(incoming):
            incoming[candidate].difference_update(layer)
            if not incoming[candidate] and candidate not in layer_by_ref:
                next_ready.append(candidate)
        ready = next_ready
    if len(layer_by_ref) != len(tasks):
        cyclic = sorted(ref for ref, dependencies in incoming.items() if dependencies)
        raise ToolFailure("任务依赖存在环：" + ", ".join(cyclic))
    for task in tasks:
        ref = task["ref"]
        for dependency in task.get("depends_on", []):
            if dependency in refs and layer_by_ref[dependency] != layer_by_ref[ref] - 1:
                raise ToolFailure(
                    f"任务 {ref} 对 {dependency} 的依赖跨越了执行层。"
                    "依赖只能指向紧邻的上一层任务，请删除可由传递关系推导出的重复依赖。"
                )
    return layers


def topological_order(tasks: list[dict[str, Any]]) -> list[str]:
    return [ref for layer in dependency_layers(tasks) for ref in layer]


def store_credential(arguments: dict[str, Any]) -> dict[str, Any]:
    """手工写入一次凭证的兜底入口。

    正常情况下凭证由控制台心跳送达；只有控制台没跑或桥接没装时才需要它。
    服务端地址是写死的，这里不接受也不保存任何地址。
    """
    key = str(arguments.get("key", "")).strip()
    if not key:
        raise ToolFailure("凭证不能为空。")
    user_id = str(arguments.get("user_id") or "").strip() or token_subject(key) or "task-executor"
    config = {
        "api_url": TASK_BOARD_API_URL,
        "key": key,
        "key_header": "token",
        "user_id": user_id,
    }
    verify = bool(arguments.get("verify_connection", True))
    programs = request_api(config, "GET", "/delivery/programs") or [] if verify else []
    save_credential(key, user_id)
    return {
        "stored": True,
        "apiUrl": TASK_BOARD_API_URL,
        "userId": user_id,
        "verified": verify,
        "projectCount": len(programs),
    }


def configuration() -> dict[str, Any]:
    config_path = CONFIG_PATH if CONFIG_PATH.exists() else LEGACY_CONFIG_PATH
    if not config_path.exists():
        return {"configured": False, "configPath": str(CONFIG_PATH)}
    credential = load_credential()
    try:
        config = load_config()
    except ToolFailure as exc:
        # 地址配好了但心跳还没送来凭证：这是等待状态，不是坏配置。
        return {
            "configured": False,
            "apiUrl": str(stored_settings().get("api_url") or ""),
            "bridgeApiUrl": bridge_api_url(),
            "configPath": str(config_path),
            "credentialPath": str(CREDENTIAL_PATH),
            "credentialSource": "none",
            "error": str(exc),
        }
    # userId 是自填标签，认不出凭证背后到底是谁。真实账号一律问服务端 ——
    # 「用错账号」和「没有权限」在面板那边报出来是同一句话，这里必须能一眼分辨。
    try:
        account = request_api(config, "GET", "/auth/me") or {}
    except ToolFailure as exc:
        account = {"error": str(exc)}
    return {
        "configured": True,
        "apiUrl": config["api_url"],
        "bridgeApiUrl": bridge_api_url(),
        "keyHeader": config.get("key_header", "token"),
        "userId": config.get("user_id", "task-executor"),
        "key": "***" + config["key"][-4:] if len(config["key"]) >= 4 else "***",
        "configPath": str(config_path),
        "credentialPath": str(CREDENTIAL_PATH),
        "credentialSource": config.get("_source", "config"),
        "credentialUpdatedAt": credential.get("updated_at") or 0,
        "account": {
            "id": account.get("id"),
            "username": account.get("username"),
            "displayName": account.get("displayName"),
            "error": account.get("error"),
        },
    }


def list_projects() -> dict[str, Any]:
    config = load_config()
    programs = request_api(config, "GET", "/delivery/programs") or []
    return {"projects": programs, "count": len(programs)}


def require_business_key(value: Any, label: str) -> str:
    key = str(value or "").strip()
    if not key:
        raise ToolFailure(f"{label}不能为空。")
    if len(key) > 64 or not re.fullmatch(r"[A-Za-z0-9._/-]+", key):
        raise ToolFailure(f"{label}只能包含字母、数字、点、下划线、斜杠和连字符，且不能超过 64 字符。")
    return key


def create_project(arguments: dict[str, Any]) -> dict[str, Any]:
    assert_write_allowed("创建项目")
    if os.environ.get(RUNTIME_PROJECT_ID_ENV, "").strip():
        raise ToolFailure("当前项目级会话不能创建或切换项目。")
    config = load_config()
    program_code = require_business_key(arguments.get("program_code"), "项目编码")
    name = str(arguments.get("name") or "").strip()
    if not name:
        raise ToolFailure("项目名称不能为空。")
    programs = request_api(config, "GET", "/delivery/programs") or []
    if any(str(item.get("programCode") or "") == program_code for item in programs):
        raise ToolFailure(f"项目编码已存在：{program_code}")
    request_api(
        config,
        "POST",
        "/delivery/program/save",
        body={
            "programId": 0,
            "programCode": program_code,
            "name": name,
            "summary": str(arguments.get("summary") or "").strip(),
            "status": str(arguments.get("status") or "active").strip(),
            "actorName": str(arguments.get("actor_name") or "task-planner").strip(),
        },
    )
    created = next((item for item in request_api(config, "GET", "/delivery/programs") or [] if str(item.get("programCode") or "") == program_code), None)
    if not isinstance(created, dict) or int(created.get("programId") or 0) <= 0:
        raise ToolFailure("项目已创建，但未能读取项目主键。")
    return {"created": True, "programId": int(created["programId"]), "programCode": program_code, "name": name}


def create_stage(arguments: dict[str, Any]) -> dict[str, Any]:
    assert_write_allowed("创建里程碑")
    program_value, used_current_project = program_value_of(arguments)
    config = load_config()
    context = project_context(config, program_value)
    stage_key = require_business_key(arguments.get("stage_key"), "阶段标识")
    if any(str(item.get("stageKey") or "") == stage_key for item in context["stages"]):
        raise ToolFailure(f"阶段标识已存在：{stage_key}")
    tag = str(arguments.get("tag") or "").strip()
    title = str(arguments.get("title") or "").strip()
    if not tag or not title:
        raise ToolFailure("阶段名称和阶段目标不能为空。")
    seq = int(arguments.get("seq") or 0)
    if seq <= 0:
        seq = max((int(item.get("seq") or 0) for item in context["stages"]), default=0) + 1
    program_id = context["program"]["programId"]
    request_api(
        config,
        "POST",
        "/delivery/stage/save",
        body={
            "programId": program_id,
            "stageKey": stage_key,
            "seq": seq,
            "tag": tag,
            "timeWindow": str(arguments.get("time_window") or "").strip(),
            "maturityLevel": str(arguments.get("maturity_level") or "").strip(),
            "title": title,
        },
    )
    return {
        "created": True,
        "programId": program_id,
        "projectSource": "current-executor-project" if used_current_project else "explicit",
        "stageKey": stage_key,
        "seq": seq,
        "tag": tag,
        "title": title,
    }


def create_module(arguments: dict[str, Any]) -> dict[str, Any]:
    assert_write_allowed("创建模块")
    program_value, used_current_project = program_value_of(arguments)
    config = load_config()
    context = project_context(config, program_value)
    module_key = require_business_key(arguments.get("module_key"), "模块标识")
    if any(str(item.get("moduleKey") or "") == module_key for item in context["modules"]):
        raise ToolFailure(f"模块标识已存在：{module_key}")
    name = str(arguments.get("name") or "").strip()
    if not name:
        raise ToolFailure("模块名称不能为空。")
    seq = int(arguments.get("seq") or 0)
    if seq <= 0:
        seq = max((int(item.get("seq") or 0) for item in context["modules"]), default=0) + 1
    weight = int(arguments.get("weight") or 0)
    if weight < 0:
        raise ToolFailure("模块权重不能为负数。")
    program_id = context["program"]["programId"]
    request_api(
        config,
        "POST",
        "/delivery/module/save",
        body={
            "programId": program_id,
            "moduleKey": module_key,
            "seq": seq,
            "name": name,
            "weight": weight,
            "kind": str(arguments.get("kind") or "").strip(),
        },
    )
    return {
        "created": True,
        "programId": program_id,
        "projectSource": "current-executor-project" if used_current_project else "explicit",
        "moduleKey": module_key,
        "seq": seq,
        "name": name,
        "weight": weight,
    }


def get_context(arguments: dict[str, Any]) -> dict[str, Any]:
    program_value, used_current_project = program_value_of(arguments)
    config = load_config()
    try:
        context = project_context(config, program_value)
    except ToolFailure as exc:
        if used_current_project:
            raise ToolFailure(f"当前执行器项目“{program_value}”在任务面板中不存在：{exc}") from exc
        raise
    stage_key = str(arguments.get("stage_key") or "").strip()
    module_key = str(arguments.get("module_key") or "").strip()
    require_option(stage_key, context["stages"], "stageKey", "阶段")
    require_option(module_key, context["modules"], "moduleKey", "模块")
    context["selection"] = {
        "programId": context["program"]["programId"],
        "projectSource": "current-executor-project" if used_current_project else "explicit",
        "requestedProject": program_value,
        "stageKey": stage_key,
        "moduleKey": module_key,
    }
    return context


def validate_tasks(
    tasks: list[dict[str, Any]],
    context: dict[str, Any],
    selected_stage: str,
    selected_module: str,
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    if not tasks:
        raise ToolFailure("至少需要一个待创建任务。")
    if len(tasks) > 50:
        raise ToolFailure("单次最多创建 50 个任务。")
    refs: dict[str, dict[str, Any]] = {}
    existing_keys = {str(item.get("itemKey")) for item in context["items"]}
    stage_keys = {str(item.get("stageKey")) for item in context["stages"]}
    module_keys = {str(item.get("moduleKey")) for item in context["modules"]}
    generated_keys: set[str] = set()
    for index, original in enumerate(tasks, start=1):
        task = dict(original)
        ref = str(task.get("ref", "")).strip()
        title = str(task.get("title", "")).strip()
        if not ref or len(ref) > 32:
            raise ToolFailure(f"第 {index} 个任务的 ref 必填且不能超过 32 字符。")
        if ref in refs:
            raise ToolFailure(f"任务 ref 重复：{ref}")
        if not title:
            raise ToolFailure(f"任务 {ref} 的标题不能为空。")
        benefit_tags = task.get("benefit_tags")
        if not isinstance(benefit_tags, list):
            raise ToolFailure(f"任务 {ref} 必须提供 benefit_tags 数组。")
        normalized_benefit_tags: list[str] = []
        seen_benefit_tags: set[str] = set()
        for value in benefit_tags:
            tag = str(value).strip()
            if not tag or tag in seen_benefit_tags:
                continue
            if len(tag) > MAX_BENEFIT_TAG_LENGTH:
                raise ToolFailure(f"任务 {ref} 的收益标签不能超过 {MAX_BENEFIT_TAG_LENGTH} 个字符。")
            seen_benefit_tags.add(tag)
            normalized_benefit_tags.append(tag)
        if not normalized_benefit_tags:
            raise ToolFailure(f"任务 {ref} 至少需要一个收益标签。")
        if len(normalized_benefit_tags) > MAX_BENEFIT_TAGS:
            raise ToolFailure(f"任务 {ref} 的收益标签最多 {MAX_BENEFIT_TAGS} 个。")
        task["benefit_tags"] = normalized_benefit_tags
        kind = str(task.get("kind") or "capability")
        if kind not in VALID_KINDS:
            raise ToolFailure(f"任务 {ref} 的 kind 无效：{kind}")
        task["title"] = title
        task["kind"] = kind
        task["stage_key"] = selected_stage or str(task.get("stage_key") or "").strip()
        task["module_key"] = selected_module or str(task.get("module_key") or "").strip()
        if stage_keys and not task["stage_key"]:
            raise ToolFailure(f"任务 {ref} 缺少阶段归属。")
        if module_keys and not task["module_key"]:
            raise ToolFailure(f"任务 {ref} 缺少模块归属。")
        if task["stage_key"] and task["stage_key"] not in stage_keys:
            raise ToolFailure(f"任务 {ref} 的阶段不属于所选项目：{task['stage_key']}")
        if task["module_key"] and task["module_key"] not in module_keys:
            raise ToolFailure(f"任务 {ref} 的模块不属于所选项目：{task['module_key']}")
        item_key = str(task.get("item_key") or "").strip()
        if item_key and (len(item_key) > 64 or not re.fullmatch(r"[A-Za-z0-9._/-]+", item_key)):
            raise ToolFailure(f"任务 {ref} 的 item_key 格式无效。")
        task["item_key"] = item_key or make_item_key(context["program"]["programId"], task)
        if task["item_key"] in existing_keys or task["item_key"] in generated_keys:
            raise ToolFailure(f"任务键已存在或重复：{task['item_key']}")
        generated_keys.add(task["item_key"])
        task["depends_on"] = [str(value).strip() for value in task.get("depends_on", []) if str(value).strip()]
        refs[ref] = task
    for ref, task in refs.items():
        for dependency in task["depends_on"]:
            if dependency == ref:
                raise ToolFailure(f"任务 {ref} 不能依赖自身。")
            if dependency not in refs:
                if dependency in existing_keys:
                    raise ToolFailure(
                        f"任务 {ref} 不能依赖已存在任务 {dependency}；"
                        "本次需求拆解的依赖只能引用同批任务。"
                    )
                raise ToolFailure(f"任务 {ref} 引用了不存在的依赖：{dependency}")
    return refs, topological_order(list(refs.values()))


def append_prototype_task(
    config: dict[str, Any],
    context: dict[str, Any],
    requirement_key: str,
    refs: dict[str, dict[str, Any]],
    order: list[str],
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Append the opted-in prototype task after every task in this write batch.

    The requirement-level switch is authoritative.  The planner only asks for the
    append through ``generate_prototype``; it cannot add an arbitrary task merely
    by choosing the flag.  A dedicated marker then lets the board and local bridge
    identify the task without relying on display copy.
    """
    if not requirement_key:
        raise ToolFailure("生成原型图任务必须提供 requirement_key。")
    requirement = request_api(
        config,
        "GET",
        "/delivery/requirement",
        query={"programId": context["program"]["programId"], "requirementKey": requirement_key},
    )
    if not isinstance(requirement, dict) or not requirement.get("generatePrototype"):
        raise ToolFailure("该需求未启用专业模式的原型图生成，不能追加原型图任务。")
    if any(
        str(item.get("requirementKey") or "") == requirement_key and bool(item.get("prototypeTask"))
        for item in context["items"]
    ):
        # 需求允许反复补充任务；原型图任务只应保留一条，不能因二次确认而重复创建。
        return refs, order

    previous_layer = dependency_layers(list(refs.values()))[-1]
    final_task = refs[previous_layer[-1]]
    ref = "prototype-image"
    suffix = 2
    while ref in refs:
        ref = f"prototype-image-{suffix}"
        suffix += 1
    prototype_task = {
        "ref": ref,
        "title": "生成需求原型图",
        "description": "基于本需求及全部前置任务的已交付内容，生成可供评审的原型图。图片必须保存到本任务文档目录下的 prototype/ 文件夹。",
        "stage_key": final_task["stage_key"],
        "module_key": final_task["module_key"],
        "kind": "capability",
        "benefit_tags": ["评审可视化"],
        "prototype_task": True,
        # 只连上一执行层，批处理的层级屏障已保证更早的任务已经完成。
        "depends_on": previous_layer,
        "acceptance_criteria": [
            "生成至少一张 PNG、JPG、WEBP 或 GIF 格式的原型图。",
            "原型图保存在本任务需求文档同级的 prototype/ 目录。",
            "任务详情可通过“打开原型图目录”查看生成的图片。",
        ],
    }
    prototype_task["item_key"] = make_item_key(context["program"]["programId"], prototype_task)
    refs[ref] = prototype_task
    return refs, [*order, ref]


def create_tasks(arguments: dict[str, Any]) -> dict[str, Any]:
    assert_write_allowed("写入任务")
    program_value, used_current_project = program_value_of(arguments)
    config = load_config()
    try:
        context = project_context(config, program_value)
    except ToolFailure as exc:
        if used_current_project:
            raise ToolFailure(f"当前执行器项目“{program_value}”在任务面板中不存在：{exc}") from exc
        raise
    selected_stage = str(arguments.get("stage_key") or "").strip()
    selected_module = str(arguments.get("module_key") or "").strip()
    # 需求键由会话提示词下发，工具只负责原样透传：任务必须挂回发起这次拆解的那条需求。
    requirement_key = str(arguments.get("requirement_key") or "").strip()
    if len(requirement_key) > 64:
        raise ToolFailure("requirement_key 不能超过 64 个字符。")
    # 简易模式让任务直接从动作执行起步，跳过梳理需求那一轮。
    phase = str(arguments.get("phase") or "requirement").strip() or "requirement"
    if phase not in {"requirement", "development", "testing"}:
        raise ToolFailure(f"任务起始阶段无效：{phase}")
    require_option(selected_stage, context["stages"], "stageKey", "阶段")
    require_option(selected_module, context["modules"], "moduleKey", "模块")
    raw_tasks = arguments.get("tasks")
    if not isinstance(raw_tasks, list):
        raise ToolFailure("tasks 必须是数组。")
    refs, order = validate_tasks(raw_tasks, context, selected_stage, selected_module)
    requirement = requirement_record(config, context["program"]["programId"], requirement_key)
    # 需求关掉拆解开关时，一条需求只能落一条任务；原型任务由工具自己追加，不算模型拆出来的。
    if requirement and not bool(requirement.get("splitTasks", True)) and len(refs) > 1:
        raise ToolFailure("该需求已关闭「拆解成多条任务」，本次只能创建一条覆盖整条需求的任务。")
    owner_id, owner_name = primary_requirement_owner(
        config,
        context["program"]["programId"],
        requirement_key,
        requirement,
    )
    if bool(arguments.get("generate_prototype")):
        refs, order = append_prototype_task(config, context, requirement_key, refs, order)
    created: list[dict[str, Any]] = []
    ref_to_key = {ref: task["item_key"] for ref, task in refs.items()}
    program_id = context["program"]["programId"]
    stage_names = {str(stage.get("stageKey") or ""): str(stage.get("title") or stage.get("tag") or "") for stage in context["stages"]}
    module_names = {str(module.get("moduleKey") or ""): str(module.get("name") or "") for module in context["modules"]}
    for sort_order, ref in enumerate(order, start=1):
        task = refs[ref]
        dependencies = [ref_to_key.get(value, value) for value in task["depends_on"]]
        body = {
            "programId": program_id,
            "itemKey": task["item_key"],
            "stageKey": task["stage_key"],
            "moduleKey": task["module_key"],
            "requirementKey": requirement_key,
            "phase": phase,
            "kind": task["kind"],
            "prototypeTask": bool(task.get("prototype_task")),
            "title": task["title"],
            "description": str(task.get("description") or ""),
            "benefitTags": task["benefit_tags"],
            "requirementDocument": requirement_document(
                task,
                program_id,
                stage_names.get(task["stage_key"], task["stage_key"]),
                module_names.get(task["module_key"], task["module_key"]),
                owner_name,
                dependencies,
            ),
            "status": "todo",
            "progress": 0,
            "ownerId": owner_id,
            "ownerName": owner_name,
            "note": str(task.get("note") or ""),
            "sortOrder": int(task.get("sort_order") or sort_order),
            "dependsOnItemKeys": dependencies,
            "actorName": str(arguments.get("actor_name") or "task-executor"),
        }
        try:
            result = request_api(config, "POST", "/delivery/item/create", body=body)
        except ToolFailure as exc:
            pending = [candidate for candidate in order if candidate not in {item["ref"] for item in created}]
            raise ToolFailure(
                f"批量写入在任务 {ref} 处停止：{exc}；"
                f"已创建 {json.dumps(created, ensure_ascii=False)}；未创建 {pending}"
            ) from exc
        created_item_key = result.get("itemKey", task["item_key"]) if isinstance(result, dict) else task["item_key"]
        created.append(
            {
                "ref": ref,
                "itemKey": created_item_key,
                "title": task["title"],
                "benefitTags": task["benefit_tags"],
                "ownerId": owner_id,
                "ownerName": owner_name,
                "stageKey": task["stage_key"],
                "moduleKey": task["module_key"],
                "requirementKey": requirement_key,
                "prototypeTask": bool(task.get("prototype_task")),
                "dependsOnItemKeys": dependencies,
                "requirementDocumentPath": f"doc/{str(task['module_key']).strip() or 'module'}/{created_item_key}/文档.md",
            }
        )
    return {
        "programId": program_id,
        "projectSource": "current-executor-project" if used_current_project else "explicit",
        "requestedProject": program_value,
        "createdCount": len(created),
        "created": created,
        "sessionBindingsPending": [
            {"programId": program_id, "itemKey": item["itemKey"]} for item in created
        ],
    }


def task_item_key(value: Any, label: str = "任务键") -> str:
    item_key = str(value or "").strip()
    if not item_key:
        raise ToolFailure(f"{label}不能为空。")
    if len(item_key) > 64 or not re.fullmatch(r"[A-Za-z0-9._/-]+", item_key):
        raise ToolFailure(f"{label}格式无效。")
    return item_key


def current_task(config: dict[str, Any], program_id: int, item_key: str) -> dict[str, Any]:
    item = request_api(
        config,
        "GET",
        "/delivery/item",
        query={"programId": program_id, "itemKey": item_key},
    )
    if not isinstance(item, dict):
        raise ToolFailure("任务详情接口返回格式错误。")
    if str(item.get("itemKey") or "").strip() != item_key:
        raise ToolFailure("任务详情接口返回了不匹配的任务键。")
    version = item.get("version")
    if isinstance(version, bool):
        raise ToolFailure("任务详情缺少有效版本号，请刷新后重试。")
    try:
        if int(version) <= 0:
            raise ValueError
    except (TypeError, ValueError):
        raise ToolFailure("任务详情缺少有效版本号，请刷新后重试。") from None
    return item


def normalized_benefit_tags(values: Any) -> list[str]:
    if not isinstance(values, list):
        raise ToolFailure("benefit_tags 必须是数组。")
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        tag = str(value).strip()
        if not tag or tag in seen:
            continue
        if len(tag) > MAX_BENEFIT_TAG_LENGTH:
            raise ToolFailure(f"收益标签不能超过 {MAX_BENEFIT_TAG_LENGTH} 个字符。")
        seen.add(tag)
        result.append(tag)
    if not result:
        raise ToolFailure("至少需要一个收益标签。")
    if len(result) > MAX_BENEFIT_TAGS:
        raise ToolFailure(f"收益标签最多 {MAX_BENEFIT_TAGS} 个。")
    return result


def normalized_dependency_keys(values: Any, label: str) -> list[str]:
    if not isinstance(values, list):
        raise ToolFailure(f"{label}必须是数组。")
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        item_key = task_item_key(value, label)
        if item_key not in seen:
            seen.add(item_key)
            result.append(item_key)
    return result


def normalized_dependency_sides(
    values: Any,
    dependencies: list[str],
    label: str,
) -> dict[str, str]:
    if not isinstance(values, dict):
        raise ToolFailure(f"{label}必须是以前置任务键为 key 的对象。")
    allowed = {"", "top", "right", "bottom", "left"}
    result: dict[str, str] = {}
    for raw_item_key, raw_side in values.items():
        item_key = task_item_key(raw_item_key, f"{label}中的任务键")
        if item_key not in dependencies:
            raise ToolFailure(f"{label}包含的任务 {item_key} 不在当前依赖列表中。")
        side = str(raw_side or "").strip().lower()
        if side not in allowed:
            raise ToolFailure(f"{label}的连线方向只能是 top、right、bottom、left 或空字符串。")
        result[item_key] = side
    return result


def task_dependency_sides(current: dict[str, Any], field: str) -> dict[str, Any]:
    values = current.get(field) or {}
    if not isinstance(values, dict):
        raise ToolFailure(f"任务详情中的 {field} 格式错误。")
    return values


def task_patch_body(
    program_id: int,
    item_key: str,
    current: dict[str, Any],
    actor_name: str,
) -> dict[str, Any]:
    return {
        "programId": program_id,
        "itemKey": item_key,
        "version": int(current["version"]),
        "actorName": actor_name,
    }


def patch_task(config: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    updated = request_api(config, "POST", "/delivery/item/patch", body=body)
    if not isinstance(updated, dict):
        raise ToolFailure("任务更新接口返回格式错误。")
    return {
        "programId": body["programId"],
        "itemKey": body["itemKey"],
        "previousVersion": body["version"],
        "version": updated.get("version"),
        "task": updated,
    }


def update_task(arguments: dict[str, Any]) -> dict[str, Any]:
    assert_write_allowed("更新任务")
    program_id, _ = program_value_of(arguments)
    item_key = task_item_key(arguments.get("item_key"))
    config = load_config()
    current = current_task(config, program_id, item_key)
    actor_name = str(arguments.get("actor_name") or "task-planner").strip() or "task-planner"
    body = task_patch_body(program_id, item_key, current, actor_name)
    updated_fields = 0

    text_fields = {
        "title": "title",
        "description": "description",
        "due_date": "dueDate",
        "note": "note",
    }
    for argument_name, api_name in text_fields.items():
        if argument_name not in arguments:
            continue
        value = arguments[argument_name]
        if not isinstance(value, str):
            raise ToolFailure(f"{argument_name}必须是字符串。")
        body[api_name] = value
        updated_fields += 1

    if "kind" in arguments:
        kind = str(arguments["kind"] or "").strip()
        if kind not in VALID_KINDS:
            raise ToolFailure("kind 只能是 gap、capability 或 asset。")
        body["kind"] = kind
        updated_fields += 1
    if "benefit_tags" in arguments:
        body["benefitTags"] = normalized_benefit_tags(arguments["benefit_tags"])
        updated_fields += 1
    if "sort_order" in arguments:
        value = arguments["sort_order"]
        if isinstance(value, bool):
            raise ToolFailure("sort_order 必须是整数。")
        try:
            body["sortOrder"] = int(value)
        except (TypeError, ValueError):
            raise ToolFailure("sort_order 必须是整数。") from None
        updated_fields += 1
    if "stage_key" in arguments:
        stage_key = str(arguments["stage_key"] or "").strip()
        require_option(
            stage_key,
            request_api(config, "GET", "/delivery/stages", query={"programId": program_id}) or [],
            "stageKey",
            "阶段",
        )
        body["stageKey"] = stage_key
        updated_fields += 1
    if "module_key" in arguments:
        module_key = str(arguments["module_key"] or "").strip()
        require_option(
            module_key,
            request_api(config, "GET", "/delivery/modules", query={"programId": program_id}) or [],
            "moduleKey",
            "模块",
        )
        body["moduleKey"] = module_key
        updated_fields += 1
    if "requirement_key" in arguments:
        requirement_key = str(arguments["requirement_key"] or "").strip()
        if len(requirement_key) > 64:
            raise ToolFailure("requirement_key 不能超过 64 个字符。")
        owner_id, owner_name = primary_requirement_owner(config, program_id, requirement_key)
        body["requirementKey"] = requirement_key
        body["ownerId"] = owner_id
        body["ownerName"] = owner_name
        updated_fields += 1

    comment = str(arguments.get("comment") or "").strip()
    if comment:
        body["comment"] = comment
    if updated_fields == 0 and not comment:
        raise ToolFailure("至少提供一个需要更新的任务字段或 comment。")
    return patch_task(config, body)


def update_task_dependencies(arguments: dict[str, Any]) -> dict[str, Any]:
    assert_write_allowed("更新任务依赖")
    program_id, _ = program_value_of(arguments)
    item_key = task_item_key(arguments.get("item_key"))
    dependencies = normalized_dependency_keys(arguments.get("depends_on_item_keys"), "depends_on_item_keys")
    config = load_config()
    current = current_task(config, program_id, item_key)
    actor_name = str(arguments.get("actor_name") or "task-planner").strip() or "task-planner"
    body = task_patch_body(program_id, item_key, current, actor_name)
    body["dependsOnItemKeys"] = dependencies
    if "dependency_source_sides" in arguments:
        body["dependencySourceSides"] = normalized_dependency_sides(
            arguments["dependency_source_sides"], dependencies, "dependency_source_sides"
        )
    if "dependency_target_sides" in arguments:
        body["dependencyTargetSides"] = normalized_dependency_sides(
            arguments["dependency_target_sides"], dependencies, "dependency_target_sides"
        )
    comment = str(arguments.get("comment") or "").strip()
    if comment:
        body["comment"] = comment
    return patch_task(config, body)


def delete_task_dependencies(arguments: dict[str, Any]) -> dict[str, Any]:
    assert_write_allowed("删除任务依赖")
    program_id, _ = program_value_of(arguments)
    item_key = task_item_key(arguments.get("item_key"))
    removed = normalized_dependency_keys(arguments.get("predecessor_item_keys"), "predecessor_item_keys")
    if not removed:
        raise ToolFailure("至少提供一条要删除的前置任务依赖。")
    config = load_config()
    current = current_task(config, program_id, item_key)
    current_dependencies = normalized_dependency_keys(current.get("dependsOnItemKeys") or [], "当前任务依赖")
    missing = sorted(set(removed) - set(current_dependencies))
    if missing:
        raise ToolFailure("以下前置任务不是当前任务的依赖：" + "、".join(missing))
    removed_set = set(removed)
    remaining = [dependency for dependency in current_dependencies if dependency not in removed_set]
    actor_name = str(arguments.get("actor_name") or "task-planner").strip() or "task-planner"
    body = task_patch_body(program_id, item_key, current, actor_name)
    body["dependsOnItemKeys"] = remaining
    source_sides = task_dependency_sides(current, "dependencySourceSides")
    target_sides = task_dependency_sides(current, "dependencyTargetSides")
    body["dependencySourceSides"] = {
        dependency: str(source_sides.get(dependency) or "")
        for dependency in remaining
    }
    body["dependencyTargetSides"] = {
        dependency: str(target_sides.get(dependency) or "")
        for dependency in remaining
    }
    comment = str(arguments.get("comment") or "").strip()
    if comment:
        body["comment"] = comment
    result = patch_task(config, body)
    result["removedPredecessorItemKeys"] = removed
    return result


def requirement_document(
    task: dict[str, Any],
    program_id: int,
    stage_name: str,
    module_name: str,
    owner_name: str,
    dependencies: list[str],
) -> str:
    """Build a readable default when a planner provides structured task fields only."""
    supplied = str(task.get("requirement_document") or "").strip()
    if supplied:
        return supplied

    description = str(task.get("description") or "").strip() or "根据任务标题完成可验证的交付。"
    note = str(task.get("note") or "").strip()
    criteria = [str(value).strip() for value in task.get("acceptance_criteria") or [] if str(value).strip()]
    lines = [
        f"# {task['title']}",
        "",
        "## 任务信息",
        f"- 项目：{program_id}",
        f"- 任务键：{task['item_key']}",
        f"- 阶段：{stage_name or '未指定'}",
        f"- 模块：{module_name or '未指定'}",
        f"- 类型：{task['kind']}",
        f"- 负责人：{owner_name or '未指定'}",
        f"- 预期收益：{'、'.join(task['benefit_tags'])}",
        "",
        "## 需求说明",
        description,
        "",
        "## 前置依赖",
    ]
    lines.extend(f"- {item_key}" for item_key in dependencies) if dependencies else lines.append("- 无")
    if criteria:
        lines.extend(["", "## 验收标准"])
        lines.extend(f"- {criterion}" for criterion in criteria)
    if note:
        lines.extend(["", "## 补充说明", note])
    return "\n".join(lines)


def bind_execution_session(arguments: dict[str, Any]) -> dict[str, Any]:
    config = load_config()
    body = {
        "programId": require_program_id(arguments.get("program_id")),
        "itemKey": str(arguments.get("item_key") or "").strip(),
        "executorType": str(arguments.get("executor_type") or "").strip(),
        "externalSessionId": str(arguments.get("external_session_id") or "").strip(),
        "externalHostId": str(arguments.get("external_host_id") or "").strip(),
        "status": str(arguments.get("status") or "pending").strip(),
        "metadata": arguments.get("metadata") or {},
        "actorName": str(arguments.get("actor_name") or "task-executor").strip(),
    }
    if not body["programId"] or not body["itemKey"]:
        raise ToolFailure("绑定执行会话必须提供 program_id 和 item_key。")
    result = request_api(config, "POST", "/delivery/item/execution-session/bind", body=body)
    if not isinstance(result, dict):
        raise ToolFailure("执行会话绑定接口返回格式错误。")
    return result


def get_execution_sessions(arguments: dict[str, Any]) -> dict[str, Any]:
    config = load_config()
    program_id = require_program_id(arguments.get("program_id"))
    item_key = str(arguments.get("item_key") or "").strip()
    if not program_id or not item_key:
        raise ToolFailure("查询执行会话必须提供 program_id 和 item_key。")
    sessions = request_api(
        config,
        "GET",
        "/delivery/item/execution-session",
        query={
            "programId": program_id,
            "itemKey": item_key,
            "executorType": str(arguments.get("executor_type") or "").strip(),
        },
    ) or []
    if not isinstance(sessions, list):
        raise ToolFailure("执行会话查询接口返回格式错误。")
    return {"programId": program_id, "itemKey": item_key, "sessions": sessions, "count": len(sessions)}


def update_execution_session_status(arguments: dict[str, Any]) -> dict[str, Any]:
    config = load_config()
    body = {
        "programId": require_program_id(arguments.get("program_id")),
        "itemKey": str(arguments.get("item_key") or "").strip(),
        "executorType": str(arguments.get("executor_type") or "").strip(),
        "version": int(arguments.get("version") or 0),
        "status": str(arguments.get("status") or "").strip(),
        "actorName": str(arguments.get("actor_name") or "task-executor").strip(),
    }
    if "metadata" in arguments:
        body["metadata"] = arguments["metadata"]
    result = request_api(config, "POST", "/delivery/item/execution-session/status", body=body)
    if not isinstance(result, dict):
        raise ToolFailure("执行会话状态接口返回格式错误。")
    return result


def execution_queue_from_context(
    context: dict[str, Any],
    selected_stage: str = "",
    selected_module: str = "",
    actor_name: str = "task-executor",
) -> dict[str, Any]:
    require_option(selected_stage, context["stages"], "stageKey", "阶段")
    require_option(selected_module, context["modules"], "moduleKey", "模块")
    all_items = context["items"]
    by_key = {str(item.get("itemKey")): item for item in all_items}

    if selected_stage:
        current_stage = next(item for item in context["stages"] if item.get("stageKey") == selected_stage)
    elif context["stages"]:
        current_stage = None
        for stage in sorted(context["stages"], key=lambda item: (int(item.get("seq") or 0), str(item.get("stageKey") or ""))):
            stage_key = str(stage.get("stageKey") or "")
            if any(
                item.get("stageKey") == stage_key
                and item.get("status") in ACTIVE_STATUSES
                and (not selected_module or item.get("moduleKey") == selected_module)
                for item in all_items
            ):
                current_stage = stage
                break
    else:
        current_stage = {"stageKey": "", "title": "未分阶段", "seq": 0}

    if current_stage is None:
        return {
            "currentStage": None,
            "readyTasks": [],
            "runningTasks": [],
            "resumableTasks": [],
            "waitingTasks": [],
            "blockedTasks": [],
            "done": True,
            "message": "所有阶段任务均已完成或不做。",
        }

    stage_key = str(current_stage.get("stageKey") or "")
    scoped = [
        item
        for item in all_items
        if str(item.get("stageKey") or "") == stage_key
        and (not selected_module or item.get("moduleKey") == selected_module)
    ]
    ready: list[dict[str, Any]] = []
    waiting: list[dict[str, Any]] = []
    running: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    for item in scoped:
        status = item.get("status")
        if status == "doing":
            running.append(item)
            continue
        if status == "blocked":
            blocked.append(item)
            continue
        if status != "todo":
            continue
        dependencies = [str(value) for value in item.get("dependsOnItemKeys") or []]
        incomplete = [
            {
                "itemKey": key,
                "title": by_key.get(key, {}).get("title", "未知任务"),
                "status": by_key.get(key, {}).get("status", "missing"),
            }
            for key in dependencies
            if by_key.get(key, {}).get("status") != "done"
        ]
        enriched = dict(item)
        enriched["incompleteDependencies"] = incomplete
        if incomplete:
            waiting.append(enriched)
        else:
            ready.append(enriched)

    order_key = lambda item: (int(item.get("sortOrder") or 0), str(item.get("itemKey") or ""))
    ready.sort(key=order_key)
    waiting.sort(key=order_key)
    running.sort(key=order_key)
    blocked.sort(key=order_key)
    resumable = [item for item in running if str(item.get("ownerName") or "") == actor_name]
    return {
        "currentStage": current_stage,
        "moduleKey": selected_module,
        "readyTasks": ready,
        "runningTasks": running,
        "resumableTasks": resumable,
        "waitingTasks": waiting,
        "blockedTasks": blocked,
        "done": False,
    }


def get_execution_queue(arguments: dict[str, Any]) -> dict[str, Any]:
    program_value, used_current_project = program_value_of(arguments)
    config = load_config()
    context = project_context(config, program_value)
    queue = execution_queue_from_context(
        context,
        str(arguments.get("stage_key") or "").strip(),
        str(arguments.get("module_key") or "").strip(),
        str(arguments.get("actor_name") or "task-executor").strip(),
    )
    queue.update({
        "programId": context["program"]["programId"],
        "projectSource": "current-executor-project" if used_current_project else "explicit",
        "requestedProject": program_value,
    })
    return queue


def patch_execution_item(
    config: dict[str, Any],
    program_id: int,
    item: dict[str, Any],
    status: str,
    actor_name: str,
    comment: str,
    progress: int | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "programId": program_id,
        "itemKey": item["itemKey"],
        "version": int(item["version"]),
        "status": status,
        "actorName": actor_name,
        "comment": comment,
    }
    if progress is not None:
        body["progress"] = progress
    if status == "doing":
        body["ownerName"] = actor_name
    result = request_api(config, "POST", "/delivery/item/patch", body=body)
    if not isinstance(result, dict):
        raise ToolFailure("任务状态接口返回格式错误。")
    return result


def claim_next_task(arguments: dict[str, Any]) -> dict[str, Any]:
    program_value, used_current_project = program_value_of(arguments)
    config = load_config()
    context = project_context(config, program_value)
    actor_name = str(arguments.get("actor_name") or "task-executor").strip()
    queue = execution_queue_from_context(
        context,
        str(arguments.get("stage_key") or "").strip(),
        str(arguments.get("module_key") or "").strip(),
        actor_name,
    )
    program_id = context["program"]["programId"]
    if queue.get("resumableTasks"):
        return {
            "action": "resume",
            "programId": program_id,
            "projectSource": "current-executor-project" if used_current_project else "explicit",
            "currentStage": queue["currentStage"],
            "task": queue["resumableTasks"][0],
        }
    if queue.get("runningTasks") and not arguments.get("allow_parallel", False):
        return {
            "action": "busy",
            "programId": program_id,
            "currentStage": queue["currentStage"],
            "runningTasks": queue["runningTasks"],
            "message": "当前阶段已有其他执行中的任务；默认不并行领取。",
        }
    if not queue.get("readyTasks"):
        return {
            "action": "none",
            "programId": program_id,
            "currentStage": queue.get("currentStage"),
            "waitingTasks": queue.get("waitingTasks", []),
            "blockedTasks": queue.get("blockedTasks", []),
            "message": queue.get("message", "当前阶段没有依赖已满足的未开始任务。"),
        }
    task = queue["readyTasks"][0]
    updated = patch_execution_item(
        config,
        program_id,
        task,
        "doing",
        actor_name,
        str(arguments.get("comment") or "执行引擎已领取任务并开始执行。"),
        progress=min(99, max(1, int(task.get("progress") or 0))),
    )
    return {
        "action": "claimed",
        "programId": program_id,
        "projectSource": "current-executor-project" if used_current_project else "explicit",
        "currentStage": queue["currentStage"],
        "task": updated,
    }


def finish_execution_task(arguments: dict[str, Any]) -> dict[str, Any]:
    outcome = str(arguments.get("outcome") or "").strip()
    if outcome not in {"done", "blocked", "todo"}:
        raise ToolFailure("outcome 只能是 done、blocked 或 todo。")
    comment = str(arguments.get("comment") or "").strip()
    if not comment:
        raise ToolFailure("结束任务时必须提供结果或阻塞说明。")
    program_value, used_current_project = program_value_of(arguments)
    config = load_config()
    context = project_context(config, program_value)
    item_key = str(arguments.get("item_key") or "").strip()
    version = int(arguments.get("version") or 0)
    item = next((value for value in context["items"] if value.get("itemKey") == item_key), None)
    if item is None:
        raise ToolFailure(f"任务不存在：{item_key}")
    if version <= 0 or int(item.get("version") or 0) != version:
        raise ToolFailure("任务版本已变化，请重新读取执行队列后再更新。")
    if item.get("status") != "doing":
        raise ToolFailure(f"只能结束进行中的任务，当前状态为 {item.get('status')}。")
    actor_name = str(arguments.get("actor_name") or "task-executor").strip()
    if str(item.get("ownerName") or "") != actor_name:
        raise ToolFailure(f"任务由 {item.get('ownerName') or '其他执行器'} 执行，{actor_name} 不能结束该任务。")
    progress = 100 if outcome == "done" else (0 if outcome == "todo" else int(item.get("progress") or 0))
    updated = patch_execution_item(config, context["program"]["programId"], item, outcome, actor_name, comment, progress)
    return {
        "action": "finished",
        "programId": context["program"]["programId"],
        "projectSource": "current-executor-project" if used_current_project else "explicit",
        "outcome": outcome,
        "task": updated,
    }


ACTIONS = [
    {
        "name": "store_task_board_credential",
        "title": "手工写入任务面板凭证",
        "description": "兜底入口：控制台心跳没跑时手工存一次 token；服务端地址写死在插件里，不接受地址参数。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "控制台登录 token"},
                "user_id": {"type": "string", "description": "可选；缺省时从 token 里读"},
                "verify_connection": {"type": "boolean", "default": True},
            },
            "required": ["key"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_task_board_configuration",
        "title": "检查任务面板配置",
        "description": "检查插件是否已初始化，只返回脱敏后的凭证信息。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "list_task_board_projects",
        "title": "列出任务面板项目",
        "description": "读取用户可以选择的交付项目。执行任务拆解前必须先选项目。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "create_task_board_project",
        "title": "创建任务面板项目",
        "description": "创建新的交付项目；program_code 是展示和导入幂等编码，创建后返回数值主键 programId。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "program_code": {"type": "string", "description": "唯一项目业务编码，不作为任务关联标识"},
                "name": {"type": "string", "description": "项目名称"},
                "summary": {"type": "string"},
                "status": {"type": "string", "default": "active"},
                "actor_name": {"type": "string", "default": "task-planner"},
            },
            "required": ["program_code", "name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "create_task_board_stage",
        "title": "创建任务面板阶段",
        "description": "在指定项目中创建里程碑；program_id 必须为项目表数值主键，会话已绑定项目时可省略。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "program_id": {"type": "integer", "minimum": 1, "description": "项目表数值主键；会话已绑定项目时可省略"},
                "stage_key": {"type": "string", "description": "唯一阶段业务标识"},
                "seq": {"type": "integer", "minimum": 1, "description": "可选；省略时追加到最后"},
                "tag": {"type": "string", "description": "阶段名称"},
                "title": {"type": "string", "description": "阶段目标"},
                "time_window": {"type": "string"},
                "maturity_level": {"type": "string"},
            },
            "required": ["stage_key", "tag", "title"],
            "additionalProperties": False,
        },
    },
    {
        "name": "create_task_board_module",
        "title": "创建任务面板模块",
        "description": "在指定项目中创建模块；program_id 必须为项目表数值主键，会话已绑定项目时可省略。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "program_id": {"type": "integer", "minimum": 1, "description": "项目表数值主键；会话已绑定项目时可省略"},
                "module_key": {"type": "string", "description": "唯一模块业务标识"},
                "seq": {"type": "integer", "minimum": 1, "description": "可选；省略时追加到最后"},
                "name": {"type": "string", "description": "模块名称"},
                "weight": {"type": "integer", "minimum": 0, "default": 0},
                "kind": {"type": "string"},
            },
            "required": ["module_key", "name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_task_board_context",
        "title": "读取项目任务上下文",
        "description": "校验项目及可选里程碑、模块，并返回里程碑、模块和已有任务；program_id 必须为项目表数值主键，会话已绑定项目时可省略。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "program_id": {"type": "integer", "minimum": 1, "description": "项目表数值主键；会话已绑定项目时可省略"},
                "stage_key": {"type": "string", "description": "可选阶段键"},
                "module_key": {"type": "string", "description": "可选模块键"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "create_task_board_tasks",
        "title": "批量创建任务面板任务",
        "description": "校验分类和紧凑分层依赖图（每层最多并行 3 项、只连上一层），强制应用用户选择的里程碑/模块，并按拓扑顺序写入；program_id 必须为项目表数值主键，会话已绑定项目时可省略。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "program_id": {"type": "integer", "minimum": 1, "description": "项目表数值主键；会话已绑定项目时可省略"},
                "stage_key": {"type": "string", "description": "用户选定时传入并覆盖所有任务"},
                "module_key": {"type": "string", "description": "用户选定时传入并覆盖所有任务"},
                "requirement_key": {"type": "string", "description": "本次拆解所属需求；由会话提示词给出，必须原样传回"},
                "phase": {"type": "string", "enum": ["requirement", "development", "testing"], "description": "任务起始阶段；由会话提示词给出，必须原样传回，默认 requirement"},
                "generate_prototype": {"type": "boolean", "description": "需求已启用原型图时传 true；工具会自动追加一条位于末尾、只依赖上一执行层任务的原型图生成任务"},
                "actor_name": {"type": "string", "default": "task-executor"},
                "tasks": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 50,
                    "items": {
                        "type": "object",
                        "properties": {
                            "ref": {"type": "string", "description": "本批任务内唯一短引用"},
                            "item_key": {"type": "string", "description": "可选固定任务键"},
                            "title": {"type": "string"},
                            "description": {"type": "string"},
                            "benefit_tags": {"type": "array", "minItems": 1, "maxItems": 3, "items": {"type": "string", "minLength": 1, "maxLength": 32}, "description": "任务完成后带来的 1-3 个简短收益或作用标签"},
                            "requirement_document": {"type": "string", "description": "可选的完整任务需求文档（Markdown/纯文本）；不传时按任务字段自动生成"},
                            "stage_key": {"type": "string"},
                            "module_key": {"type": "string"},
                            "kind": {"type": "string", "enum": ["gap", "capability", "asset"]},
                            "note": {"type": "string"},
                            "acceptance_criteria": {"type": "array", "items": {"type": "string"}, "description": "可选的可验证验收标准"},
                            "sort_order": {"type": "integer"},
                            "depends_on": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "仅限本批任务 ref，且只能引用紧邻上一执行层的任务",
                            },
                        },
                        "required": ["ref", "title", "benefit_tags", "depends_on"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["tasks"],
            "additionalProperties": False,
        },
    },
    {
        "name": "update_task_board_task",
        "title": "更新任务面板任务",
        "description": "更新一条已存在任务的规划字段。工具会先读取当前版本后再写入；任务所属需求变化时自动同步该需求的首位负责人。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "program_id": {"type": "integer", "minimum": 1, "description": "项目表数值主键；会话已绑定项目时可省略"},
                "item_key": {"type": "string", "description": "要更新的任务键"},
                "stage_key": {"type": "string", "description": "阶段键；传空字符串可清空"},
                "module_key": {"type": "string", "description": "模块键；传空字符串可清空"},
                "requirement_key": {"type": "string", "description": "所属需求键；传空字符串可解除关联"},
                "kind": {"type": "string", "enum": ["gap", "capability", "asset"]},
                "title": {"type": "string"},
                "description": {"type": "string"},
                "benefit_tags": {"type": "array", "minItems": 1, "maxItems": 3, "items": {"type": "string", "minLength": 1, "maxLength": 32}},
                "due_date": {"type": "string", "description": "YYYY-MM-DD；传空字符串可清空"},
                "note": {"type": "string"},
                "sort_order": {"type": "integer"},
                "comment": {"type": "string", "description": "可选变更说明，会记录到任务时间线"},
                "actor_name": {"type": "string", "default": "task-planner"},
            },
            "required": ["item_key"],
            "additionalProperties": False,
        },
    },
    {
        "name": "update_task_board_task_dependencies",
        "title": "更新任务面板任务依赖",
        "description": "替换一条任务的全部直接前置依赖。服务端会验证前置任务归属和整张依赖图无环；传空数组可清空全部依赖。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "program_id": {"type": "integer", "minimum": 1, "description": "项目表数值主键；会话已绑定项目时可省略"},
                "item_key": {"type": "string", "description": "后置任务键，即需要设置前置依赖的任务"},
                "depends_on_item_keys": {"type": "array", "items": {"type": "string"}, "description": "完整的直接前置任务键列表"},
                "dependency_source_sides": {"type": "object", "additionalProperties": {"type": "string", "enum": ["", "top", "right", "bottom", "left"]}, "description": "可选：以前置任务键为 key 的出线边"},
                "dependency_target_sides": {"type": "object", "additionalProperties": {"type": "string", "enum": ["", "top", "right", "bottom", "left"]}, "description": "可选：以前置任务键为 key 的入线边"},
                "comment": {"type": "string", "description": "可选变更说明，会记录到任务时间线"},
                "actor_name": {"type": "string", "default": "task-planner"},
            },
            "required": ["item_key", "depends_on_item_keys"],
            "additionalProperties": False,
        },
    },
    {
        "name": "delete_task_board_task_dependencies",
        "title": "删除任务面板任务依赖",
        "description": "从一条任务中删除指定的直接前置依赖，并保留其余依赖及连线锚点。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "program_id": {"type": "integer", "minimum": 1, "description": "项目表数值主键；会话已绑定项目时可省略"},
                "item_key": {"type": "string", "description": "后置任务键，即需要删除其前置依赖的任务"},
                "predecessor_item_keys": {"type": "array", "minItems": 1, "items": {"type": "string"}, "description": "要删除的直接前置任务键"},
                "comment": {"type": "string", "description": "可选变更说明，会记录到任务时间线"},
                "actor_name": {"type": "string", "default": "task-planner"},
            },
            "required": ["item_key", "predecessor_item_keys"],
            "additionalProperties": False,
        },
    },
]


def run_action(name: str, arguments: dict[str, Any]) -> Any:
    if name == "store_task_board_credential":
        return store_credential(arguments)
    if name == "get_task_board_configuration":
        return configuration()
    if name == "list_task_board_projects":
        return list_projects()
    if name == "create_task_board_project":
        return create_project(arguments)
    if name == "create_task_board_stage":
        return create_stage(arguments)
    if name == "create_task_board_module":
        return create_module(arguments)
    if name == "get_task_board_context":
        return get_context(arguments)
    if name == "create_task_board_tasks":
        return create_tasks(arguments)
    if name == "update_task_board_task":
        return update_task(arguments)
    if name == "update_task_board_task_dependencies":
        return update_task_dependencies(arguments)
    if name == "delete_task_board_task_dependencies":
        return delete_task_dependencies(arguments)
    raise ToolFailure(f"未知动作：{name}")
