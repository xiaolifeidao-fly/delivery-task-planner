"""HTTP 请求体的校验与解析。

面板和上游服务传过来的都是自由 JSON，这一层把它收敛成后面能直接用的取值：
必填的缺了就抛 BridgeFailure，可选的落到默认值，数量和长度一律截到上限，
键名一律过白名单——绝不把原样字符串带进路径拼接或分支判断。

返回的是位置元组而不是对象，这是既有约定；改动顺序会波及全部调用方。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .documents import DOCUMENT_SET_SUFFIXES, requirement_prototype_directory_of
from .errors import BridgeFailure
from .providers import (
    ai_provider_of,
    executor_purpose_of,
    fast_mode_of,
    program_id_of,
    reasoning_effort_of,
)
from .prompts.requirement import review_scope_of

MAX_CONVERSATION_ATTACHMENTS = 5
MAX_CONVERSATION_REFERENCES = 16
RUNTIME_CONFIG_KEY = "_deliveryRuntimeConfig"

def validate_planning_payload(value: Any) -> tuple[int, str, str, bool, str, str, str, str, str, bool, dict[str, Any], list[str], list[dict[str, str]], bool]:
    if not isinstance(value, dict):
        raise BridgeFailure("请求体必须是 JSON 对象")
    program_id = program_id_of(value.get("programId"))
    message = str(value.get("message") or "").strip()
    thread_id = str(value.get("threadId") or "").strip()
    selected_stage = str(value.get("stageKey") or "").strip()
    selected_module = str(value.get("moduleKey") or "").strip()
    selected_kind = str(value.get("kind") or "").strip()
    model = str(value.get("model") or "").strip()
    provider = ai_provider_of(value)
    reasoning_effort = reasoning_effort_of(value, provider)
    fast_mode = fast_mode_of(value, provider)
    requirement = planning_requirement_of(value)
    attachment_ids = value.get("attachmentIds") or []
    if not isinstance(attachment_ids, list) or len(attachment_ids) > MAX_CONVERSATION_ATTACHMENTS:
        raise BridgeFailure("附件数量无效")
    attachment_ids = [str(attachment_id).strip() for attachment_id in attachment_ids if str(attachment_id).strip()]
    chat_references = conversation_references_of(value.get("chatReferences"))
    # 只带附件不写字也是一次有效的追问，图片本身就是需求说明。
    if not message and not attachment_ids:
        raise BridgeFailure("请输入要拆解的需求")
    if len(message) > 32 * 1024:
        raise BridgeFailure("需求内容不能超过 32KB")
    if len(thread_id) > 255 or len(model) > 128:
        raise BridgeFailure("会话或模型标识无效")
    if selected_kind and selected_kind not in {"gap", "capability", "asset"}:
        raise BridgeFailure("任务类型无效")
    return (
        program_id,
        message,
        thread_id,
        bool(value.get("newConversation")),
        selected_stage,
        selected_module,
        selected_kind,
        model,
        reasoning_effort,
        fast_mode,
        requirement,
        attachment_ids,
        chat_references,
        # 只有面板上的「确认并写入」会带上这个标记，其余轮次一律是只读的预览。
        bool(value.get("confirmWrite")),
    )

def session_kind_of(row: Any) -> str:
    """会话表里这一行属于哪类会话；老数据没写 kind，一律按需求测试处理。"""
    metadata = row.get("metadata") if isinstance(row, dict) else None
    if not isinstance(metadata, dict):
        return "requirement-testing"
    return str(metadata.get("kind") or "requirement-testing").strip() or "requirement-testing"

def validate_requirement_review_payload(value: Any) -> tuple[int, str, str, str, bool, str, str, bool, list[dict[str, Any]], list[dict[str, str]], bool]:
    if not isinstance(value, dict):
        raise BridgeFailure("请求体必须是 JSON 对象")
    program_id = program_id_of(value.get("programId"))
    requirement_key = str(value.get("requirementKey") or "").strip()
    message = str(value.get("message") or "").strip()
    thread_id = str(value.get("threadId") or "").strip()
    model = str(value.get("model") or "").strip()
    provider = ai_provider_of(value)
    reasoning_effort = reasoning_effort_of(value, provider)
    fast_mode = fast_mode_of(value, provider)
    scope = review_scope_of(value.get("scope"))
    if not program_id or not requirement_key or len(requirement_key) > 64:
        raise BridgeFailure("缺少或无效的项目、需求标识")
    if not message:
        raise BridgeFailure("请输入本轮 review 的重点或规则")
    if len(message) > 32 * 1024:
        raise BridgeFailure("review 要求不能超过 32KB")
    if len(thread_id) > 255 or len(model) > 128:
        raise BridgeFailure("会话或模型标识无效")
    return (
        program_id, requirement_key, message, thread_id, bool(value.get("newConversation")), model,
        reasoning_effort, fast_mode, scope, conversation_references_of(value.get("chatReferences")),
        bool(value.get("generateReport")),
    )

def validate_requirement_testing_payload(value: Any) -> tuple[int, str, str, str, bool, str, str, bool, list[str], list[dict[str, str]], bool]:
    if not isinstance(value, dict):
        raise BridgeFailure("请求体必须是 JSON 对象")
    program_id = program_id_of(value.get("programId"))
    requirement_key = str(value.get("requirementKey") or "").strip()
    message = str(value.get("message") or "").strip()
    thread_id = str(value.get("threadId") or "").strip()
    model = str(value.get("model") or "").strip()
    provider = ai_provider_of(value)
    reasoning_effort = reasoning_effort_of(value, provider)
    fast_mode = fast_mode_of(value, provider)
    attachment_ids = value.get("attachmentIds") or []
    if not program_id or not requirement_key or len(requirement_key) > 64:
        raise BridgeFailure("缺少或无效的项目、需求标识")
    if not isinstance(attachment_ids, list) or len(attachment_ids) > MAX_CONVERSATION_ATTACHMENTS:
        raise BridgeFailure("附件数量无效")
    attachment_ids = [str(attachment_id).strip() for attachment_id in attachment_ids if str(attachment_id).strip()]
    if not message and not attachment_ids:
        raise BridgeFailure("请输入测试要求或上传测试资料")
    if len(message) > 32 * 1024:
        raise BridgeFailure("测试要求不能超过 32KB")
    if len(thread_id) > 255 or len(model) > 128:
        raise BridgeFailure("会话或模型标识无效")
    return (
        program_id, requirement_key, message, thread_id, bool(value.get("newConversation")), model,
        reasoning_effort, fast_mode, attachment_ids, conversation_references_of(value.get("chatReferences")),
        bool(value.get("testCaseOnly")),
    )

def validate_task_testing_cases_payload(value: Any) -> tuple[int, str, str, str, bool, str, str, bool]:
    if not isinstance(value, dict):
        raise BridgeFailure("请求体必须是 JSON 对象")
    program_id = program_id_of(value.get("programId"))
    item_key = str(value.get("itemKey") or "").strip()
    message = str(value.get("message") or "").strip()
    thread_id = str(value.get("threadId") or "").strip()
    model = str(value.get("model") or "").strip()
    provider = ai_provider_of(value)
    if not item_key or len(item_key) > 64:
        raise BridgeFailure("缺少或无效的项目、任务标识")
    if len(message) > 32 * 1024:
        raise BridgeFailure("测试要求不能超过 32KB")
    if len(thread_id) > 255 or len(model) > 128:
        raise BridgeFailure("会话或模型标识无效")
    return (
        program_id, item_key, message, thread_id, bool(value.get("newConversation")), model,
        reasoning_effort_of(value, provider), fast_mode_of(value, provider),
    )

def validate_fine_tuning_payload(
    value: Any, scope: str, message_required: bool = True,
) -> tuple[int, str, str, str, bool, str, str, str, bool]:
    """Validate the common free-form refinement request without introducing task actions."""
    if not isinstance(value, dict):
        raise BridgeFailure("请求体必须是 JSON 对象")
    program_id = program_id_of(value.get("programId"))
    key_name = "requirementKey" if scope == "requirement" else "itemKey"
    subject_name = "需求" if scope == "requirement" else "任务"
    key = str(value.get(key_name) or "").strip()
    message = str(value.get("message") or "").strip()
    thread_id = str(value.get("threadId") or "").strip()
    model = str(value.get("model") or "").strip()
    provider = ai_provider_of(value)
    if not program_id or not key or len(key) > 64:
        raise BridgeFailure(f"缺少或无效的项目、{subject_name}标识")
    if message_required and not message:
        raise BridgeFailure("请输入微调要求")
    if len(message) > 32 * 1024:
        raise BridgeFailure("微调要求不能超过 32KB")
    if len(thread_id) > 255 or len(model) > 128:
        raise BridgeFailure("会话或模型标识无效")
    return (
        program_id, key, message, thread_id, bool(value.get("newConversation")), model,
        provider, reasoning_effort_of(value, provider), fast_mode_of(value, provider),
    )

def planning_requirement_of(value: dict[str, Any]) -> dict[str, Any]:
    """Normalize the requirement a planning turn belongs to.

    拆解会话按需求分组：同一个项目下不同需求各自一条会话线，
    requirementKey 为空时退回到项目级会话（需求层落地之前的老用法）。
    """
    requirement_key = str(value.get("requirementKey") or "").strip()
    if len(requirement_key) > 64:
        raise BridgeFailure("需求标识无效")
    detail = str(value.get("requirementDetail") or "")
    if len(detail) > 32 * 1024:
        raise BridgeFailure("需求详情不能超过 32KB")
    # 简易模式直接把任务放进动作执行，专业模式由用户选起始阶段，默认梳理需求。
    start_phase = str(value.get("requirementStartPhase") or "").strip() or "requirement"
    if start_phase not in {"requirement", "development", "testing"}:
        raise BridgeFailure("起始阶段无效")
    return {
        "requirementKey": requirement_key,
        "name": str(value.get("requirementName") or "").strip()[:255],
        "detail": detail,
        "owners": str(value.get("requirementOwners") or "").strip()[:512],
        "assistants": str(value.get("requirementAssistants") or "").strip()[:512],
        "startPhase": start_phase,
        # 老客户端不带这个字段时按拆解处理，保持既有行为。
        "splitTasks": bool(value.get("requirementSplitTasks", True)),
        # 兼容已安装的旧面板字段；新面板使用 requirementPreGenerateTaskDocuments。
        "preGenerateTaskDocuments": bool(
            value.get("requirementPreGenerateTaskDocuments", value.get("requirementGenerateTaskOutline", False))
        ),
        "generatePrototype": bool(value.get("requirementGeneratePrototype")),
        "references": planning_requirement_references_of(value.get("requirementReferences")),
        "itemReferences": planning_requirement_item_references_of(value.get("requirementItemReferences")),
    }

def planning_requirement_references_of(value: Any) -> list[dict[str, str]]:
    """Normalize the earlier requirements a new requirement @-mentions.

    面板只传被 @ 的需求键和名字：正文按需从各自的大纲产物地址读取，
    不把历史大纲整段塞进提示词。
    """
    if not isinstance(value, list):
        return []
    references: list[dict[str, str]] = []
    seen: set[str] = set()
    for entry in value[:20]:
        if not isinstance(entry, dict):
            continue
        requirement_key = str(entry.get("requirementKey") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", requirement_key) or requirement_key in seen:
            continue
        seen.add(requirement_key)
        references.append({
            "requirementKey": requirement_key,
            "name": str(entry.get("name") or "").strip()[:255] or requirement_key,
        })
    return references

def planning_requirement_item_references_of(value: Any) -> list[dict[str, str]]:
    """Normalize the existing tasks a requirement detail @-mentions.

    Titles are deliberately not accepted from the browser. The planning prompt resolves
    each task key against the current project catalog before exposing its document path.
    """
    if not isinstance(value, list):
        return []
    references: list[dict[str, str]] = []
    seen: set[str] = set()
    for entry in value[:20]:
        if not isinstance(entry, dict):
            continue
        item_key = str(entry.get("itemKey") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", item_key) or item_key in seen:
            continue
        seen.add(item_key)
        references.append({"itemKey": item_key})
    return references

def conversation_references_of(value: Any) -> list[dict[str, str]]:
    """Normalize objects selected from a task or requirement chat @ menu.

    The browser can only nominate keys. The bridge later fetches every record again,
    so labels or arbitrary text supplied by a browser never become task context.
    """
    if not isinstance(value, list):
        return []
    references: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for entry in value[:MAX_CONVERSATION_REFERENCES]:
        if not isinstance(entry, dict):
            continue
        kind = str(entry.get("kind") or "").strip()
        key = str(entry.get("key") or "").strip()
        if kind == "file":
            scope = str(entry.get("scope") or "").strip()
            path = Path(key)
            if (
                scope not in {"requirement-outline", "requirement-testing", "requirement-prototype", "requirement-review"}
                or not key
                or len(key) > 512
                or "\x00" in key
                or "\\" in key
                or path.is_absolute()
                or ".." in path.parts
                or (kind, key) in seen
            ):
                continue
            seen.add((kind, key))
            references.append({"kind": kind, "key": key, "scope": scope})
            continue
        pattern = r"[A-Za-z0-9_-]{1,64}" if kind == "requirement" else r"[A-Za-z0-9._-]{1,64}"
        if kind not in {"requirement", "task"} or not re.fullmatch(pattern, key) or (kind, key) in seen:
            continue
        seen.add((kind, key))
        references.append({"kind": kind, "key": key})
    return references

def validate_requirement_prototype_payload(value: Any, message_required: bool = False) -> tuple[int, str, str, str, str, bool]:
    if not isinstance(value, dict):
        raise BridgeFailure("请求体必须是 JSON 对象")
    program_id = program_id_of(value.get("programId"))
    requirement_key = str(value.get("requirementKey") or "").strip()
    requirement_prototype_directory_of(requirement_key)
    message = str(value.get("message") or "").strip()
    if message_required and not message:
        raise BridgeFailure("请输入原型修改要求")
    if len(message) > 32 * 1024:
        raise BridgeFailure("原型修改要求不能超过 32KB")
    thread_id = str(value.get("threadId") or "").strip()
    if len(thread_id) > 255:
        raise BridgeFailure("会话标识无效")
    provider = ai_provider_of(value)
    model = str(value.get("model") or "").strip()
    if len(model) > 128:
        raise BridgeFailure("模型标识不能超过 128 个字符")
    return program_id, requirement_key, message, thread_id, provider, model

def validate_execute_payload(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BridgeFailure("请求体必须是 JSON 对象")
    program_id = program_id_of(value.get("programId"))
    task = value.get("task")
    if not isinstance(task, dict):
        raise BridgeFailure("缺少项目或任务")
    required = ("itemKey", "title", "version")
    if any(not task.get(key) for key in required):
        raise BridgeFailure("任务缺少 itemKey、title 或 version")
    status = str(task.get("status") or "")
    # 「再做一次」不回滚状态，只是给已完成的任务再开一轮执行实例。
    redo = bool(value.get("redo"))
    if status == "done" and not redo:
        raise BridgeFailure("已完成任务不能再次执行")
    normalized = dict(value)
    normalized["redo"] = redo
    normalized.pop("bizLine", None)
    normalized["programId"] = program_id
    normalized["task"] = dict(task)
    model = str(value.get("model") or "").strip()
    if len(model) > 128:
        raise BridgeFailure("模型标识不能超过 128 个字符")
    normalized["model"] = model
    normalized["provider"] = ai_provider_of(value)
    normalized["reasoningEffort"] = reasoning_effort_of(value, normalized["provider"])
    normalized["fastMode"] = fast_mode_of(value, normalized["provider"])
    follow_up = str(value.get("followUp") or "").strip()
    if len(follow_up) > 32 * 1024:
        raise BridgeFailure("追加要求不能超过 32KB")
    normalized["followUp"] = follow_up
    normalized["conversationReferences"] = conversation_references_of(value.get("conversationReferences"))
    execution_constraints = str(value.get("executionConstraints") or "").strip()
    if len(execution_constraints) > 32 * 1024:
        raise BridgeFailure("任务约束条件说明不能超过 32KB")
    normalized["executionConstraints"] = execution_constraints
    attachments = value.get("followUpAttachments") or []
    if not isinstance(attachments, list) or len(attachments) > MAX_CONVERSATION_ATTACHMENTS:
        raise BridgeFailure("附件数量无效")
    normalized["followUpAttachments"] = attachments
    return normalized

def validate_conversation_payload(value: Any) -> tuple[int, str, str, str, bool, list[str], str, str, bool, list[dict[str, str]]]:
    if not isinstance(value, dict):
        raise BridgeFailure("请求体必须是 JSON 对象")
    program_id = program_id_of(value.get("programId"))
    item_key = str(value.get("itemKey") or "").strip()
    message = str(value.get("message") or "").strip()
    if not item_key:
        raise BridgeFailure("缺少项目或任务标识")
    if len(message) > 32 * 1024:
        raise BridgeFailure("消息不能超过 32KB")
    thread_id = str(value.get("threadId") or "").strip()
    if len(thread_id) > 255:
        raise BridgeFailure("会话标识无效")
    raw_attachment_ids = value.get("attachmentIds") or []
    if not isinstance(raw_attachment_ids, list) or len(raw_attachment_ids) > MAX_CONVERSATION_ATTACHMENTS:
        raise BridgeFailure("附件数量无效")
    attachment_ids = [str(attachment_id or "").strip() for attachment_id in raw_attachment_ids]
    if any(not re.fullmatch(r"[A-Za-z0-9_-]{16,80}", attachment_id) for attachment_id in attachment_ids):
        raise BridgeFailure("附件标识无效")
    if len(set(attachment_ids)) != len(attachment_ids):
        raise BridgeFailure("附件不能重复")
    if not message and not attachment_ids:
        raise BridgeFailure("请输入要发送的内容或添加附件")
    model = str(value.get("model") or "").strip()
    if len(model) > 128:
        raise BridgeFailure("模型标识不能超过 128 个字符")
    provider = ai_provider_of(value)
    reasoning_effort = reasoning_effort_of(value, provider)
    fast_mode = fast_mode_of(value, provider)
    references = conversation_references_of(value.get("references"))
    return (
        program_id,
        item_key,
        message,
        thread_id,
        bool(value.get("newConversation")),
        attachment_ids,
        model,
        reasoning_effort,
        fast_mode,
        references,
    )

def business_item_key_of(value: Any) -> str:
    item_key = str(value or "").strip()
    if not re.fullmatch(r"business-requirement-[1-9][0-9]*", item_key):
        raise BridgeFailure("业务诉求标识无效")
    return item_key

def business_intake_of(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes"}
    return False

def validate_business_conversation_payload(value: Any) -> tuple[int, str, str, str, str, str, list[str]]:
    if not isinstance(value, dict) or not business_intake_of(value.get("businessIntake")):
        raise BridgeFailure("业务访谈请求标识无效")
    program_id = program_id_of(value.get("programId"))
    item_key = business_item_key_of(value.get("itemKey"))
    message = str(value.get("message") or "").strip()
    if not message:
        raise BridgeFailure("请输入业务诉求")
    if len(message) > 32 * 1024:
        raise BridgeFailure("业务诉求不能超过 32KB")
    thread_id = str(value.get("threadId") or "").strip()
    if len(thread_id) > 255:
        raise BridgeFailure("会话标识无效")
    provider = ai_provider_of(value)
    if provider != "codex":
        raise BridgeFailure("业务访谈仅支持 Codex")
    model = str(value.get("model") or "").strip()
    if len(model) > 128:
        raise BridgeFailure("模型标识不能超过 128 个字符")
    attachment_ids = value.get("attachmentIds") or []
    if not isinstance(attachment_ids, list) or len(attachment_ids) > MAX_CONVERSATION_ATTACHMENTS:
        raise BridgeFailure(f"一条消息最多携带 {MAX_CONVERSATION_ATTACHMENTS} 个附件")
    attachment_ids = [str(item).strip() for item in attachment_ids if str(item).strip()]
    return program_id, item_key, message, thread_id, model, reasoning_effort_of(value, provider), attachment_ids

def runtime_config_from_payload(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BridgeFailure("请求参数无效")
    runtime_config = value.get(RUNTIME_CONFIG_KEY)
    if not isinstance(runtime_config, dict):
        raise BridgeFailure("任务面板身份上下文缺失")
    return runtime_config

def assert_runtime_project(config: dict[str, Any], program_id: int) -> None:
    runtime_value = config.get("_project_id")
    runtime_program_id = program_id_of(runtime_value) if runtime_value not in (None, "") else 0
    if program_id and runtime_program_id and runtime_program_id != program_id:
        raise BridgeFailure("当前请求项目与任务面板入口项目不一致")

def biz_line_of(value: Any) -> str:
    # Accepted for backwards-compatible clients only. Project-scoped work never
    # uses this value to resolve or authorize a project.
    return str(value.get("bizLine") or "") if isinstance(value, dict) else ""

def scoped_config(config: dict[str, Any], biz_line: str = "") -> dict[str, Any]:
    return config

def config_biz_line(config: dict[str, Any]) -> str:
    return ""

def request_scoped_config(config: dict[str, Any] | None, biz_line: str, program_id: int) -> dict[str, Any]:
    if config is None:
        raise BridgeFailure("任务面板身份上下文缺失")
    assert_runtime_project(config, program_id)
    return config

def task_identity(biz_line: str, program_id: int, item_key: str) -> tuple[str, int, str]:
    return "", program_id, item_key

def validate_task_identity(value: Any) -> tuple[str, int, str]:
    if not isinstance(value, dict):
        raise BridgeFailure("请求参数无效")
    program_id = program_id_of(value.get("programId"))
    item_key = str(value.get("itemKey") or "").strip()
    if not item_key:
        raise BridgeFailure("缺少项目或任务标识")
    return "", program_id, item_key
