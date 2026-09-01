#!/usr/bin/env python3
"""Loopback HTTP bridge that starts one persisted Codex thread per delivery task."""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import mimetypes
import os
import platform
import queue
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import ParseResult, parse_qs, quote, unquote, urlparse
from urllib.request import Request, urlopen

import server as planner

from delivery_bridge import clients, payloads
from delivery_bridge import (
    codex_cli,
    documents,
    errors,
    git_ops,
    github_ssh,
    hostinfo,
    prompt_context,
    runtime,
    turn_output,
    workspaces,
)
from delivery_bridge.update_manager import PluginUpdateManager, UpdateFailure
from delivery_bridge.versioning import compare_versions, manifest_version


# 监听地址默认对所有网卡开放：web-api 可以部署在别的机器上，只能走网络调用业务访谈。
# 桥自身没有鉴权，来源必须由部署方的防火墙或安全组收窄到已知调用方。
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8765
PLUGIN_MANIFEST_PATH = Path(__file__).resolve().parent / ".codex-plugin" / "plugin.json"



PLUGIN_GITHUB_REPOSITORY = "https://github.com/xiaolifeidao-fly/delivery-task-planner.git"
PLUGIN_GITHUB_RAW_BASE_URL = "https://raw.githubusercontent.com/xiaolifeidao-fly/delivery-task-planner"
PLUGIN_VERSION_CHECK_CACHE_SECONDS = 60
PLUGIN_UPDATE_RESTART_POLL_SECONDS = 2
# This value intentionally lives in the running Python process. Change it in a
# later release to verify that silent installation restarted the bridge and
# loaded the new code instead of only replacing files on disk.
PLUGIN_RUNTIME_TEST_VALUE = "delivery-task-planner-python-runtime-v6"


PLUGIN_UPDATES: PluginUpdateManager




def plugin_version_from_manifest(path: Path) -> str:
    try:
        return manifest_version(path)
    except ValueError as exc:
        raise BridgeFailure(str(exc)) from exc


def installed_plugin_version() -> str:
    return plugin_version_from_manifest(PLUGIN_MANIFEST_PATH)


def compare_plugin_versions(left: str, right: str) -> int:
    return compare_versions(left, right)


def remote_plugin_default_branch() -> str:
    try:
        return PLUGIN_UPDATES._resolve_remote().get("branch", "main")
    except (UpdateFailure, NameError):
        return "main"


def fetch_remote_plugin_version() -> str:
    try:
        return PLUGIN_UPDATES._resolve_remote(force=True)["version"]
    except UpdateFailure as exc:
        raise BridgeFailure(str(exc)) from exc


def cached_remote_plugin_version() -> str:
    try:
        return PLUGIN_UPDATES._resolve_remote()["version"]
    except UpdateFailure as exc:
        raise BridgeFailure(str(exc)) from exc


def plugin_update_status() -> dict[str, Any]:
    checked_at = int(time.time())
    try:
        local_version = installed_plugin_version()
    except BridgeFailure as exc:
        return {
            "localVersion": "",
            "remoteVersion": "",
            "updateAvailable": False,
            "checkedAt": checked_at,
            "message": str(exc),
        }
    try:
        remote_version = cached_remote_plugin_version()
        update_available = compare_plugin_versions(remote_version, local_version) > 0
    except (BridgeFailure, ValueError) as exc:
        return {
            "localVersion": local_version,
            "remoteVersion": "",
            "updateAvailable": False,
            "checkedAt": checked_at,
            "message": str(exc),
        }
    result = {
        "localVersion": local_version,
        "remoteVersion": remote_version,
        "updateAvailable": update_available,
        "checkedAt": checked_at,
        "message": "",
    }
    try:
        result["installation"] = PLUGIN_UPDATES.get_job()
    except (UpdateFailure, NameError):
        result["installation"] = None
    return result


from delivery_bridge.runtime import (
    TASKBOARD_CLI,
    taskboard_command,
    PLUGIN_ROOT,
    default_runtime_dir,
    RUNTIME_DIR,
)
PLUGIN_UPDATES = PluginUpdateManager(
    Path(__file__).resolve().parent,
    RUNTIME_DIR,
    PLUGIN_GITHUB_REPOSITORY,
    PLUGIN_GITHUB_RAW_BASE_URL,
    PLUGIN_VERSION_CHECK_CACHE_SECONDS,
)
CODEX_GLOBAL_STATE_PATH = Path.home() / ".codex" / ".codex-global-state.json"


from delivery_bridge.errors import (
    BridgeFailure,
)


# Capture the manifest version exactly once for the lifetime of this Python
# process. Package replacement changes the file on disk, but this value only
# changes after the bridge has genuinely restarted and imported the new code.
try:
    PLUGIN_RUNTIME_VERSION = installed_plugin_version()
except BridgeFailure:
    PLUGIN_RUNTIME_VERSION = ""


def content_disposition_of(name: str, inline: bool = False) -> str:
    """Build a browser-safe Content-Disposition header for arbitrary file names."""
    cleaned = re.sub(r"[\r\n\"]", "", str(name)).strip() or "attachment"
    suffix = Path(cleaned).suffix
    safe_suffix = suffix if re.fullmatch(r"\.[A-Za-z0-9]{1,16}", suffix) else ""
    ascii_stem = re.sub(r"[^A-Za-z0-9_-]", "_", Path(cleaned).stem.encode("ascii", "ignore").decode("ascii")).strip("_-")
    fallback = f"{ascii_stem or 'download'}{safe_suffix}"
    disposition = "inline" if inline else "attachment"
    encoded = quote(cleaned, safe="!#$&+-.^_`|~")
    return f"{disposition}; filename=\"{fallback}\"; filename*=UTF-8''{encoded}"


def create_http_server(
    host: str,
    port: int,
    workspace: Path,
    allowed_origins: set[str],
    business_workspace_root: Path | None = None,
) -> ThreadingHTTPServer:
    """Create the bridge listener; host is loopback unless deployment opts out."""
    httpd = ThreadingHTTPServer((host, port), BridgeHandler)
    business_root = (business_workspace_root or DEFAULT_BUSINESS_WORKSPACE_ROOT).expanduser().resolve()
    httpd.bridge = ExecutionBridge(workspace, business_workspace_root=business_root)  # type: ignore[attr-defined]
    httpd.allowed_origins = allowed_origins  # type: ignore[attr-defined]
    httpd.business_workspace_root = business_root  # type: ignore[attr-defined]
    return httpd


def schedule_bridge_restart() -> None:
    """Hand restart ownership to a detached helper so this request can finish."""
    helper = Path(__file__).resolve().parent / "delivery_bridge" / "restart_helper.py"
    restart_log = RUNTIME_DIR / "restart-helper.log"
    restart_log.parent.mkdir(parents=True, exist_ok=True)
    with restart_log.open("a", encoding="utf-8") as output:
        subprocess.Popen(
            [
                sys.executable,
                str(helper),
                "--pid",
                str(os.getpid()),
                "--plugin-root",
                str(Path(__file__).resolve().parent),
                *sys.argv[1:],
            ],
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=output,
            start_new_session=sys.platform != "win32",
            creationflags=(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)) if sys.platform == "win32" else 0,
            close_fds=True,
        )


def complete_plugin_update_in_background(job_id: str, bridge: Any) -> None:
    """Restart after replacement once every bridge-managed run has finished."""
    def monitor() -> None:
        while True:
            try:
                job = PLUGIN_UPDATES.get_job(job_id)
            except UpdateFailure:
                return
            status = str(job.get("status") or "")
            if status in {"completed", "failed", "restarting"}:
                return
            if status == "restart_required":
                active_runs = bridge.active_run_count()
                if active_runs > 0:
                    try:
                        PLUGIN_UPDATES.mark_waiting_for_runs(job_id, active_runs)
                    except UpdateFailure:
                        return
                    time.sleep(PLUGIN_UPDATE_RESTART_POLL_SECONDS)
                    continue
                try:
                    PLUGIN_UPDATES.mark_restarting(job_id)
                except UpdateFailure:
                    return
                schedule_bridge_restart()
                return
            time.sleep(PLUGIN_UPDATE_RESTART_POLL_SECONDS)

    threading.Thread(target=monitor, daemon=True, name=f"plugin-update-{job_id[:8]}").start()


















from delivery_bridge.executor_env import codex_environment
from delivery_bridge.providers import (
    CODEX_MODEL_CATALOG,
    DEFAULT_BIZ_LINE,
    AI_PROVIDERS,
    CODEX_REASONING_EFFORTS,
    CLAUDE_REASONING_EFFORTS,
    ai_provider_of,
    provider_label,
    executor_type_of,
    executor_provider_of,
    executor_purpose_of,
    same_executor_purpose,
    reasoning_effort_of,
    fast_mode_of,
    program_id_of,
)






from delivery_bridge.hostinfo import (
    host_platform,
    host_platform_label,
)
























from delivery_bridge.codex_cli import (
    CODEX_DESKTOP_RESOURCE_COMPANIONS,
    codex_cli_name,
    codex_cli_cache_path,
    codex_desktop_resource_paths,
    WINDOWS_CLI_WRAPPER_SUFFIXES,
    path_codex_cli,
    CODEX_CLI_VERSIONS,
    codex_cli_version,
    newest_codex_cli,
    newer_codex_cli,
    codex_cli_candidates,
    available_codex_cli,
    provision_codex_cli,
)






from delivery_bridge.workspaces import (
    DEFAULT_BUSINESS_WORKSPACE_ROOT,
    placeholder_workspace,
    environment_setup_workspace,
    BUSINESS_WORKSPACE_SCOPE,
    workspace_path_of,
    business_workspace_path_of,
)


def codex_local_projects() -> list[dict[str, Any]]:
    if not CODEX_GLOBAL_STATE_PATH.exists():
        return []
    try:
        state = json.loads(CODEX_GLOBAL_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BridgeFailure(f"无法读取 Codex 本地项目：{exc}") from exc
    projects = state.get("local-projects") if isinstance(state, dict) else None
    if not isinstance(projects, dict):
        return []
    result: list[dict[str, Any]] = []
    for project_id, value in projects.items():
        if not isinstance(value, dict):
            continue
        roots = []
        for raw_root in value.get("rootPaths") or []:
            try:
                root = workspace_path_of(raw_root)
            except BridgeFailure:
                continue
            roots.append(str(root))
        name = str(value.get("name") or "").strip()
        if name and roots:
            result.append({"id": str(value.get("id") or project_id), "name": name, "rootPaths": roots})
    return sorted(result, key=lambda item: (item["name"].casefold(), item["id"]))










from delivery_bridge.stores import (
    ENVIRONMENT_SETUP_SESSIONS_PATH,
    ENVIRONMENT_SETUP_SESSIONS,
    PENDING_SESSION_SYNCS_PATH,
    GIT_ENVIRONMENT_SESSIONS_PATH,
    MAX_GIT_ENVIRONMENT_CONVERSATIONS,
    ProgressStore,
    PendingBatchFinalizeStore,
    PendingSessionSyncStore,
    GitEnvironmentSessionStore,
)




from delivery_bridge.reasoning import (
    reasoning_summary_text,
)


from delivery_bridge.progress_events import (
    generated_image_from_event,
    progress_event_of,
)






from delivery_bridge.prompt_context import (
    BRIDGE_CONTEXT_RE,
    BRIDGE_CONTEXT_TAG,
    wrap_bridge_context,
    with_mention_context,
    workspace_instruction,
)
















from delivery_bridge.prompts.common import (
    PLANNING_SKILL,
    PHASE_SKILLS,
    REQUIREMENT_SCOPE_RULE,
    document_path_of,
    document_revision_rule,
    follow_up_context_lines,
    prototype_directory_of,
    readable_document,
    requirement_document_catalog,
    sibling_document_lines,
    git_branch_lines,
    requirement_document_rule_lines,
)










from delivery_bridge.prompts.task import (
    build_task_prompt,
    build_task_testing_cases_prompt,
    fine_tuning_skill_instruction,
    build_requirement_fine_tuning_prompt,
    build_task_fine_tuning_prompt,
)




































from delivery_bridge.prompts.conversation import (
    CONVERSATION_TITLE_TIMEOUT_SECONDS,
    REQUIREMENT_NAME_TIMEOUT_SECONDS,
    MAX_REQUIREMENT_NAME_CHARS,
    MAX_CONVERSATION_TITLE_CHARS,
    MAX_REQUIREMENT_PLACEHOLDER_CHARS,
    build_conversation_prompt,
    build_conversation_title_prompt,
    conversation_title_of,
    build_requirement_name_prompt,
    requirement_name_of,
    placeholder_requirement_name,
)


from delivery_bridge.prompts.planning import (
    planning_temp_segment,
    planning_temp_document_path,
    write_planning_temp_summary,
    delete_planning_temp_summary,
    planning_temp_rule_lines,
    requirement_outline_rule_lines,
    build_planning_prompt,
    planning_detail_digest,
    build_planning_follow_up_prompt,
)



















from delivery_bridge.github_ssh import (
    GITHUB_SSH_HOST,
    GITHUB_SSH_KEY_NAME,
    GITHUB_SSH_CONFIG_START,
    GITHUB_SSH_CONFIG_END,
    GITHUB_SSH_CONFIG_BLOCK_RE,
    SSH_PUBLIC_KEY_RE,
    github_ssh_paths,
    github_identity_files,
    public_key_from_file,
    github_ssh_key_status,
    write_github_ssh_config,
    ensure_github_ssh_key,
)


# ---------------------------------------------------------------------------
# 需求分支：面板只记录关联结果，真正的 Git 命令全部在本机工作目录里执行。
# 命令参数一律固定，不拼接用户输入到 shell；分支名先做白名单校验再交给 Git。
# ---------------------------------------------------------------------------

















































































































# ---------------------------------------------------------------------------
# 时间计划的分支合并。三个方向共用同一套「target ← sources」机制：
#   - 回合基线：target = 计划分支，sources = [基线分支]
#   - 合并需求：target = 计划分支，sources = [各需求分支]
#   - 回推基线：target = 基线分支，sources = [计划分支]
# 每个方向都先出一份预览（哪些工程参与、各改了多少文件），由用户勾选后再真正合并。
# ---------------------------------------------------------------------------
































from delivery_bridge.git_ops import (
    GIT_BRANCH_FORBIDDEN_RE,
    GIT_REMOTE_PREFIX,
    GIT_REMOTE_NAME_RE,
    GIT_REPOSITORY_URL_RE,
    valid_git_branch_name,
    valid_git_remote_name,
    run_git,
    git_output,
    git_workspace_probe,
    require_git_workspace,
    git_current_branch,
    git_default_branch,
    git_fetch_all,
    git_branch_catalog,
    normalized_git_remote_url,
    git_remote_url,
    git_worktree_summary,
    MAX_GIT_CHANGE_FILES,
    MAX_GIT_CHANGE_FILE_BYTES,
    run_git_bytes,
    git_has_head,
    git_change_kind_of,
    git_numstat_totals,
    git_untracked_line_count,
    git_change_files,
    git_change_text,
    git_change_detail,
    git_local_branch_for_reference,
    git_checkout_reference,
    git_workspace_status,
    git_prepare_branch,
    git_branch_exists,
    git_worktree_dirty,
    git_submodule_workspaces,
    git_dirty_submodule_workspaces,
    git_submodule_label,
    git_sync_unselected_submodules,
    GIT_SUBPROJECT_SKIP_DIRS,
    git_subproject_workspaces,
    git_subproject_workspace_of,
    git_branch_reference_exists,
    git_project_snapshot,
    git_workspace_projects,
    git_subproject_targets_of,
    git_checkout_branch,
    git_default_remote,
    GIT_PUSH_REPAIR_TIMEOUT_SECONDS,
    git_branch_synced,
    build_git_push_repair_prompt,
    MAX_GIT_COMMIT_MESSAGE_BYTES,
    git_commit_message_of,
    git_rebase_onto_remote,
    git_push_branch,
    git_remote_ref_merged,
    git_pull_branch,
    git_sync_base_branch,
    git_create_branch,
    git_effective_base_branch,
    git_create_branch_targets,
    git_prepare_branch_targets,
    GIT_MERGE_REPAIR_TIMEOUT_SECONDS,
    git_merge_resolved_ref,
    git_merge_changed_files,
    git_merge_ahead_commits,
    git_merge_project_preview,
    git_merge_preview,
    build_git_merge_repair_prompt,
    git_merge_conflict_files,
    git_merge_in_progress,
    git_merge_one,
    git_repository_url_of,
    git_initializable_workspace_of,
    git_workspace_check,
    git_adopt_remote_branch,
    git_pending_submodules,
    git_initialize_submodules,
    git_initialize_workspace,
)







from delivery_bridge.environments import (
    MAX_ENVIRONMENT_SETUP_ITEMS,
    GLOBAL_ENVIRONMENT_SETUP_PROGRAM_ID,
    ENVIRONMENT_PRESETS,
    GIT_PRESET,
    environment_selection_of,
    validate_environment_setup_payload,
    environment_command_for,
    PROBE_VERSION_RE,
    version_at_least,
    environment_probe_status,
    environment_probe_statuses,
)


from delivery_bridge.prompts.environment import (
    build_environment_setup_prompt,
)

























































from delivery_bridge.documents import (
    MAX_REQUIREMENT_DOCUMENT_BYTES,
    MAX_DOCUMENT_UPLOAD_FILES,
    MAX_DOCUMENT_UPLOAD_FILE_BYTES,
    MAX_DOCUMENT_UPLOAD_BYTES,
    TESTING_CASES_FILE_NAME,
    REQUIREMENT_OUTLINE_FILE_NAME,
    MAX_REQUIREMENT_OUTLINE_BYTES,
    MAX_EDITABLE_OUTLINE_BYTES,
    DOCUMENT_SET_SUFFIXES,
    MAX_DOCUMENT_SET_FILES,
    MAX_DOCUMENT_SET_FILE_BYTES,
    TESTING_ASSET_ROOT,
    HTML_SUFFIXES,
    HTML_ASSET_SUFFIXES,
    MAX_HTML_ASSET_FILES,
    MAX_HTML_ASSET_TOTAL_BYTES,
    HTML_ASSET_REFERENCE_RE,
    requirement_prototype_directory_of,
    requirement_outline_path_of,
    requirement_document_directory_of,
    legacy_task_outline_path_of,
    outline_file_in_workspace,
    outline_document,
    write_outline_document,
    requirement_outline_document,
    testing_asset_directory_of,
    document_set_entries,
    document_in_set,
    document_upload_name,
    available_document_name,
    html_asset_payloads,
    document_payload,
)














from delivery_bridge.item_keys import (
    PLANNING_ITEM_KEY,
    ENVIRONMENT_SETUP_ITEM_KEY,
    REQUIREMENT_TESTING_ITEM_KEY,
    REQUIREMENT_REVIEW_ITEM_KEY,
    REQUIREMENT_FINE_TUNING_ITEM_KEY,
    REQUIREMENT_REVIEW_SESSION_KIND,
    REQUIREMENT_FINE_TUNING_SESSION_KIND,
    MAX_REQUIREMENT_PROTOTYPE_FILES,
    MAX_REQUIREMENT_PROTOTYPE_FILE_BYTES,
    MAX_REQUIREMENT_PROTOTYPE_TOTAL_BYTES,
    document_attachment_item_key,
    requirement_prototype_item_key,
    requirement_prototype_executor_type,
    task_testing_cases_executor_type,
    task_fine_tuning_executor_type,
    requirement_prototype_files,
)




from delivery_bridge.prompts.requirement import (
    REVIEW_EXCLUDED_DIRECTORIES,
    REVIEW_GUIDELINES,
    build_requirement_testing_prompt,
    review_scope_of,
    review_scope_lines,
    requirement_review_report_relative_path,
    build_requirement_review_prompt,
    prototype_session_detail_digest,
    build_requirement_prototype_prompt,
)








from delivery_bridge.attachments_text import (
    ATTACHMENT_MARKER_RE,
    ATTACHMENT_CONTEXT_RE,
    attachment_marker,
    message_with_attachments,
    attachment_ids_from_text,
    text_without_attachment_context,
)












from delivery_bridge.timeutil import (
    utc_now,
)












from delivery_bridge.sessions import (
    MAX_ENVIRONMENT_SETUP_CONVERSATIONS,
    MAX_PLANNING_CONVERSATIONS,
    MAX_CONVERSATIONS_PER_TASK,
    next_conversation_version,
    conversation_title,
    conversation_catalog,
    turn_already_finished,
    conversation_metadata,
    merged_conversation_catalog,
)


















from delivery_bridge.payloads import (
    MAX_CONVERSATION_ATTACHMENTS,
    MAX_CONVERSATION_REFERENCES,
    RUNTIME_CONFIG_KEY,
    validate_planning_payload,
    session_kind_of,
    validate_requirement_review_payload,
    validate_requirement_testing_payload,
    validate_task_testing_cases_payload,
    validate_fine_tuning_payload,
    planning_requirement_of,
    planning_requirement_references_of,
    planning_requirement_item_references_of,
    conversation_references_of,
    validate_requirement_prototype_payload,
    validate_execute_payload,
    validate_conversation_payload,
    business_item_key_of,
    business_intake_of,
    validate_business_conversation_payload,
    runtime_config_from_payload,
    assert_runtime_project,
    biz_line_of,
    scoped_config,
    config_biz_line,
    request_scoped_config,
    task_identity,
    validate_task_identity,
)


















from delivery_bridge.clients.journal import (
    CODEX_THREAD_ITEMS_DIR,
    MAX_THREAD_JOURNAL_TURNS,
    MAX_THREAD_JOURNAL_ITEMS,
    REASONING_SUMMARY_METHODS,
    JOURNAL_METHODS,
    journal_item,
    ThreadItemJournal,
    THREAD_ITEMS,
    journal_item_signature,
    reasoning_summary_parts,
    normalized_reasoning_part,
    deduped_reasoning_item,
    merge_journal_turns,
)


from delivery_bridge.clients.codex import (
    APP_SERVER_STDERR_TAIL,
    TURN_REASONING_SUMMARY,
    THREAD_READ_GRACE_SECONDS,
    AppServerClient,
)












from delivery_bridge.clients.claude import (
    MAX_CLAUDE_TRANSCRIPT_TURNS,
    CLAUDE_TRANSCRIPTS_DIR,
    CLAUDE_TRANSCRIPTS,
    CLAUDE_FILE_TOOLS,
    CLAUDE_COMMAND_TOOLS,
    CLAUDE_READ_TOOLS,
    CLAUDE_SEARCH_TOOLS,
    ClaudeTranscriptStore,
    text_line_count,
    claude_edit_line_counts,
    claude_tool_item,
    ClaudeCLIClient,
)


# 造执行器客户端一律走 factory 模块属性：只读会话复用池那一路也走同一个名字，
# 测试打桩改这一处就对所有调用方生效。
from delivery_bridge.clients import factory
from delivery_bridge.clients.factory import create_ai_client








from delivery_bridge.clients.pool import (
    THREAD_SNAPSHOT_TTL_SECONDS,
    THREAD_READER_IDLE_SECONDS,
    ACTIVE_THREAD_READ_TIMEOUT_SECONDS,
    THREAD_LAST_GOOD_LIMIT,
    ThreadReaderPool,
    THREAD_READERS,
    read_thread_or_empty,
)




from delivery_bridge.artifacts import (
    MAX_CONVERSATION_UPLOAD_BYTES,
    MAX_CONVERSATION_ATTACHMENT_BYTES,
    MAX_WORKSPACE_ARTIFACT_BYTES,
    WORKSPACE_FILE_INDEX_TTL_SECONDS,
    MAX_WORKSPACE_FILE_INDEX_ENTRIES,
    ATTACHMENT_DIRECTORY_NAME,
    ARTIFACT_DIRECTORY_NAME,
    IMAGE_SUFFIXES,
    MARKDOWN_ARTIFACT_RE,
    EXCLUDED_ARTIFACT_PARTS,
    EXCLUDED_ARTIFACT_NAMES,
    image_format,
    ConversationAttachmentStore,
    WorkspaceArtifactStore,
)


from delivery_bridge import execution
from delivery_bridge.execution import ExecutionBridge
from delivery_bridge.remote_worker import RemoteCommandWorker





























from delivery_bridge.turn_output import (
    SESSION_STATUS,
    TERMINAL_TURN_STATUSES,
    EXECUTION_OUTPUT_LIMIT,
    TOOL_CALL_ITEM_TYPES,
    LEAKED_TOOL_CALL_MARKER,
    WORKING_PHASES,
    turn_agent_text,
    turn_tool_call_count,
    corrupted_turn_reason,
    execution_output,
    merged_execution_output,
    final_agent_text_from_output,
    testing_verdict_from_output,
    BATCH_OUTCOME_RE,
    BATCH_TURN_STATUS_RE,
    BATCH_HARD_PROBLEM_RE,
    batch_task_outcome,
    text_from_user_item,
    FILE_CHANGE_KINDS,
    FILE_CHANGE_ALIASES,
    diff_line_counts,
    file_changes_of,
)


















from delivery_bridge.chat_archive import (
    CHAT_ARCHIVE_DIRECTORY_NAME,
    CHAT_ARCHIVE_REQUIREMENTS_DIRECTORY_NAME,
    CHAT_ARCHIVE_TASK_DIRECTORY_NAME,
    LEGACY_CHAT_ARCHIVE_DIRECTORY_NAME,
    CHAT_ARCHIVE_MAX_NAME_BYTES,
    CHAT_ARCHIVE_MAX_THREAD_ID_BYTES,
    CHAT_ARCHIVE_MAX_FILES_TO_SCAN,
    CHAT_ARCHIVE_MAX_FILE_BYTES,
    CLOUD_SYNC_SCOPES,
    MAX_CLOUD_SYNC_FILE_BYTES,
    MAX_CLOUD_SYNC_FILES_PER_RUN,
    chat_archive_component,
    chat_archive_relative_path,
    visible_chat_archive_turns,
    archived_chat_text,
    archive_chat_snapshot,
    chat_archive_metadata,
    archived_chat_turns,
    read_workspace_chat_archive,
    cloud_sync_workspace_entries,
)




from delivery_bridge.turn_view import (
    serialize_turns,
    ensure_terminal_result,
)


class BridgeHandler(BaseHTTPRequestHandler):
    server_version = "DeliveryAIAppServer/0.2"

    @property
    def bridge(self) -> ExecutionBridge:
        return self.server.bridge  # type: ignore[attr-defined]

    @property
    def allowed_origins(self) -> set[str]:
        return self.server.allowed_origins  # type: ignore[attr-defined]

    def allows_all_origins(self) -> bool:
        return "*" in self.allowed_origins

    def allowed_origin(self) -> str:
        # The board may be served from any origin, and direct browser navigations do
        # not include Origin, so use the standard opaque-origin value instead of
        # rejecting those requests.
        return self.headers.get("Origin", "").strip() or "null"

    def cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, token")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        # 面板可能部署在公网origin，浏览器把到 127.0.0.1 的请求当作私有网络访问，
        # preflight 会带 Access-Control-Request-Private-Network，缺了这个应答头就直接报跨域。
        if self.headers.get("Access-Control-Request-Private-Network", "").strip().lower() == "true":
            self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Access-Control-Max-Age", "600")

    def json_response(self, status: int, value: dict[str, Any]) -> None:
        raw = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def attachment_response(self, manifest: dict[str, Any], path: Path) -> None:
        content_type = str(manifest.get("contentType") or "application/octet-stream")
        self.send_response(200)
        self.cors()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(path.stat().st_size))
        self.send_header(
            "Content-Disposition",
            content_disposition_of(str(manifest.get("name") or "attachment"), bool(manifest.get("isImage"))),
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        with path.open("rb") as source:
            shutil.copyfileobj(source, self.wfile)

    def do_OPTIONS(self) -> None:
        if not self.allowed_origin():
            self.json_response(403, {"error": "origin not allowed"})
            return
        self.send_response(204)
        self.cors()
        self.end_headers()

    # 路径 -> 处理方法。加接口就在这里加一行，不必再往 if 链里插。
    GET_ROUTES = {
        "/healthz": "_get_healthz",
        "/v1/ai/health": "_get_health",
        "/v1/ai/models": "_get_health",
        "/v1/codex/business-attachment": "_get_business_attachment",
        "/v1/codex/conversation": "_get_conversation",
        "/v1/codex/document-file": "_get_document_set",
        "/v1/codex/document-set": "_get_document_set",
        "/v1/codex/environment-setup": "_get_environment_setup",
        "/v1/codex/git/branches": "_get_git_branches",
        "/v1/codex/git/change": "_get_git_changes",
        "/v1/codex/git/changes": "_get_git_changes",
        "/v1/codex/git/merge-preview": "_get_git_merge_preview",
        "/v1/codex/git/projects": "_get_git_projects",
        "/v1/codex/git/status": "_get_git_branches",
        "/v1/codex/git/workspace-check": "_get_git_workspace_check",
        "/v1/codex/health": "_get_health",
        "/v1/codex/models": "_get_health",
        "/v1/codex/planning": "_get_planning",
        "/v1/codex/prototype-directory": "_get_prototype_directory",
        "/v1/codex/requirement-document": "_get_requirement_document",
        "/v1/codex/requirement-fine-tuning": "_get_requirement_fine_tuning",
        "/v1/codex/requirement-outline": "_get_requirement_outline",
        "/v1/codex/requirement-prototype": "_get_requirement_prototype",
        "/v1/codex/requirement-prototype/conversation": "_get_requirement_prototype_conversation",
        "/v1/codex/requirement-review": "_get_requirement_review",
        "/v1/codex/requirement-testing": "_get_requirement_testing",
        "/v1/codex/task-fine-tuning": "_get_task_fine_tuning",
        "/v1/codex/task-testing-cases": "_get_task_testing_cases",
        "/v1/codex/workspace/validate": "_get_workspaces",
        "/v1/codex/workspaces": "_get_workspaces",
        "/v1/plugin/info": "_get_plugin_info",
        "/v1/plugin/runtime-test": "_get_plugin_runtime_test",
        "/v1/plugin/update": "_get_plugin_update",
    }

    # 「收下 payload 就交给执行桥」的接口：路径 -> (HTTP 状态, 用哪个桥, 方法, config 是否按关键字传)。
    # workspace 表示请求里指定工作目录的那个桥，process 表示进程级的桥
    # （预设环境装的是本机全局环境，不挂在任何业务仓库上）。
    POST_ROUTES = {
        "/v1/codex/conversation": (202, "workspace", "send_conversation", True),
        "/v1/codex/environment-setup": (202, "process", "send_environment_setup", False),
        "/v1/codex/environment-setup/stop": (202, "process", "stop_environment_setup", False),
        "/v1/codex/execute": (202, "workspace", "execute", True),
        "/v1/codex/execute-batch": (202, "workspace", "execute_batch", True),
        "/v1/codex/execute-sequence": (202, "workspace", "execute_sequence", True),
        "/v1/codex/git/merge": (200, "workspace", "merge_time_plan_branches", False),
        "/v1/codex/git/push": (200, "workspace", "push_requirement_branch", False),
        "/v1/codex/planning": (202, "workspace", "send_planning", False),
        "/v1/codex/planning/stop": (202, "workspace", "stop_planning", False),
        "/v1/codex/requirement-fine-tuning": (202, "workspace", "send_requirement_fine_tuning", False),
        "/v1/codex/requirement-fine-tuning/stop": (202, "workspace", "stop_requirement_fine_tuning", False),
        "/v1/codex/requirement-prototype/conversation": (202, "workspace", "send_requirement_prototype_message", False),
        "/v1/codex/requirement-prototype/generate": (202, "workspace", "generate_requirement_prototype", False),
        "/v1/codex/requirement-review": (202, "workspace", "send_requirement_review", False),
        "/v1/codex/requirement-review/stop": (202, "workspace", "stop_requirement_review", False),
        "/v1/codex/requirement-testing": (202, "workspace", "send_requirement_testing", False),
        "/v1/codex/requirement-testing/stop": (202, "workspace", "stop_requirement_testing", False),
        "/v1/codex/stop": (202, "workspace", "stop_conversation", True),
        "/v1/codex/stop-all": (202, "workspace", "stop_all_executions", True),
        "/v1/codex/task-fine-tuning": (202, "workspace", "send_task_fine_tuning", False),
        "/v1/codex/task-fine-tuning/stop": (202, "workspace", "stop_task_fine_tuning", False),
        "/v1/codex/task-testing-cases": (202, "workspace", "generate_task_testing_cases", False),
        "/v1/codex/task-testing-cases/stop": (202, "workspace", "stop_task_testing_cases", False),
    }

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        route = self.GET_ROUTES.get(parsed.path)
        if route is not None:
            getattr(self, route)(parsed)
            return
        # 这两条带路径参数，匹配不出常量表，只能单独判。
        if self._get_attachment(parsed) or self._get_artifact(parsed):
            return
        self.json_response(404, {"error": "not found"})

    def _get_attachment(self, parsed: ParseResult) -> bool:
        attachment_match = re.fullmatch(r"/v1/codex/attachments/([A-Za-z0-9_-]{16,80})", parsed.path)
        if not attachment_match:
            return False
        if not self.allowed_origin():
            self.json_response(403, {"error": "origin not allowed"})
            return True
        try:
            query = parse_qs(parsed.query)
            selected_bridge = self.bridge.for_workspace((query.get("workspace") or [""])[0])
            manifest, attachment_path = selected_bridge.attachments.download(attachment_match.group(1))
            program_id = program_id_of((query.get("programId") or [""])[0])
            if program_id != program_id_of(manifest.get("programId")):
                raise BridgeFailure("附件项目上下文不一致")
            config = self.bridge.request_config(
                {"programId": program_id},
                self.allowed_origin() or "",
                self.headers.get("token", "").strip(),
            )
            assert_runtime_project(config, program_id_of(manifest.get("programId")))
            self.attachment_response(manifest, attachment_path)
        except BridgeFailure as exc:
            self.json_response(404, {"error": str(exc)})
        return True

    def _get_artifact(self, parsed: ParseResult) -> bool:
        artifact_match = re.fullmatch(r"/v1/codex/artifacts/([a-f0-9]{40})", parsed.path)
        if not artifact_match:
            return False
        if not self.allowed_origin():
            self.json_response(403, {"error": "origin not allowed"})
            return True
        try:
            query = parse_qs(parsed.query)
            selected_bridge = self.bridge.for_workspace((query.get("workspace") or [""])[0])
            manifest, artifact_path = selected_bridge.artifacts.download(artifact_match.group(1))
            program_id = program_id_of((query.get("programId") or [""])[0])
            if program_id != program_id_of(manifest.get("programId")):
                raise BridgeFailure("产物项目上下文不一致")
            config = self.bridge.request_config(
                {"programId": program_id},
                self.allowed_origin() or "",
                self.headers.get("token", "").strip(),
            )
            assert_runtime_project(config, program_id_of(manifest.get("programId")))
            self.attachment_response(manifest, artifact_path)
        except BridgeFailure as exc:
            self.json_response(404, {"error": str(exc)})
        return True

    def _get_healthz(self, parsed: ParseResult) -> None:
        self.json_response(200, self.bridge.health())
        return

    def _get_plugin_info(self, parsed: ParseResult) -> None:
        self.json_response(200, {
            "installed": bool(PLUGIN_RUNTIME_VERSION),
            "version": PLUGIN_RUNTIME_VERSION,
        })
        return

    def _get_plugin_runtime_test(self, parsed: ParseResult) -> None:
        self.json_response(200, {"value": PLUGIN_RUNTIME_TEST_VALUE})
        return

    def _get_plugin_update(self, parsed: ParseResult) -> None:
        force = str((parse_qs(parsed.query).get("force") or [""])[0]).lower() in {"1", "true", "yes"}
        status = PLUGIN_UPDATES.status(force=force)
        if isinstance(status.get("installation"), dict):
            status["installation"]["activeRuns"] = self.bridge.active_run_count()
        self.json_response(200, status)
        return

    def _get_workspaces(self, parsed: ParseResult) -> None:
        if not self.allowed_origin():
            self.json_response(403, {"error": "origin not allowed"})
            return
        query = parse_qs(parsed.query)
        program_id = program_id_of((query.get("programId") or [""])[0])
        try:
            config = self.bridge.request_config(
                {"programId": program_id},
                self.allowed_origin() or "",
                self.headers.get("token", "").strip(),
            )
            if parsed.path.endswith("/validate"):
                selected_bridge = self.bridge.for_workspace((query.get("workspace") or [""])[0])
                self.bridge.remember_remote_workspace(program_id, selected_bridge.workspace, config)
                self.json_response(200, {
                    "valid": True,
                    "workspace": str(selected_bridge.workspace),
                    "name": selected_bridge.workspace.name,
                })
                return
            self.json_response(200, {
                "projects": codex_local_projects(),
            })
        except (BridgeFailure, planner.ToolFailure, ValueError) as exc:
            self.json_response(400, {"error": str(exc)})
        except Exception as exc:
            self.json_response(500, {"error": f"读取 Codex 工作目录失败：{exc}"})
        return

    def _get_git_workspace_check(self, parsed: ParseResult) -> None:
        if not self.allowed_origin():
            self.json_response(403, {"error": "origin not allowed"})
            return
        query = parse_qs(parsed.query)
        program_id = program_id_of((query.get("programId") or [""])[0])
        try:
            self.bridge.request_config(
                {"programId": program_id},
                self.allowed_origin() or "",
                self.headers.get("token", "").strip(),
            )
            self.json_response(200, git_workspace_check((query.get("workspace") or [""])[0]))
        except (BridgeFailure, planner.ToolFailure, ValueError) as exc:
            self.json_response(400, {"error": str(exc)})
        except Exception as exc:
            self.json_response(500, {"error": f"检查工作目录 Git 状态失败：{exc}"})
        return

    def _get_git_changes(self, parsed: ParseResult) -> None:
        if not self.allowed_origin():
            self.json_response(403, {"error": "origin not allowed"})
            return
        query = parse_qs(parsed.query)
        program_id = program_id_of((query.get("programId") or [""])[0])
        try:
            self.bridge.request_config(
                {"programId": program_id},
                self.allowed_origin() or "",
                self.headers.get("token", "").strip(),
            )
            selected_bridge = self.bridge.for_workspace((query.get("workspace") or [""])[0])
            if parsed.path.endswith("/changes"):
                self.json_response(200, git_change_files(selected_bridge.workspace))
            else:
                self.json_response(200, git_change_detail(
                    selected_bridge.workspace,
                    str((query.get("path") or [""])[0]),
                ))
        except (BridgeFailure, planner.ToolFailure, ValueError) as exc:
            self.json_response(400, {"error": str(exc)})
        except Exception as exc:
            self.json_response(500, {"error": f"读取 Git 变更失败：{exc}"})
        return

    def _get_git_projects(self, parsed: ParseResult) -> None:
        if not self.allowed_origin():
            self.json_response(403, {"error": "origin not allowed"})
            return
        query = parse_qs(parsed.query)
        program_id = program_id_of((query.get("programId") or [""])[0])
        try:
            self.bridge.request_config(
                {"programId": program_id},
                self.allowed_origin() or "",
                self.headers.get("token", "").strip(),
            )
            selected_bridge = self.bridge.for_workspace((query.get("workspace") or [""])[0])
            self.json_response(200, git_workspace_projects(
                selected_bridge.workspace,
                str((query.get("branch") or [""])[0]).strip(),
                str((query.get("remoteName") or ["origin"])[0]),
            ))
        except (BridgeFailure, planner.ToolFailure, ValueError) as exc:
            self.json_response(400, {"error": str(exc)})
        except Exception as exc:
            self.json_response(500, {"error": f"读取子项目 Git 状态失败：{exc}"})
        return

    def _get_git_merge_preview(self, parsed: ParseResult) -> None:
        if not self.allowed_origin():
            self.json_response(403, {"error": "origin not allowed"})
            return
        query = parse_qs(parsed.query)
        program_id = program_id_of((query.get("programId") or [""])[0])
        try:
            self.bridge.request_config(
                {"programId": program_id},
                self.allowed_origin() or "",
                self.headers.get("token", "").strip(),
            )
            selected_bridge = self.bridge.for_workspace((query.get("workspace") or [""])[0])
            # sources 可能是多条需求分支，用重复参数传，不拿逗号拼串 —— 分支名本身允许带逗号。
            self.json_response(200, git_merge_preview(
                selected_bridge.workspace,
                str((query.get("target") or [""])[0]).strip(),
                [str(value or "").strip() for value in (query.get("sources") or [])],
                str((query.get("remoteName") or ["origin"])[0]),
            ))
        except (BridgeFailure, planner.ToolFailure, ValueError) as exc:
            self.json_response(400, {"error": str(exc)})
        except Exception as exc:
            self.json_response(500, {"error": f"读取分支合并预览失败：{exc}"})
        return

    def _get_git_branches(self, parsed: ParseResult) -> None:
        if not self.allowed_origin():
            self.json_response(403, {"error": "origin not allowed"})
            return
        query = parse_qs(parsed.query)
        program_id = program_id_of((query.get("programId") or [""])[0])
        try:
            self.bridge.request_config(
                {"programId": program_id},
                self.allowed_origin() or "",
                self.headers.get("token", "").strip(),
            )
            selected_bridge = self.bridge.for_workspace((query.get("workspace") or [""])[0])
            if parsed.path.endswith("/status"):
                self.json_response(200, git_workspace_status(
                    selected_bridge.workspace,
                    str((query.get("expectedRemoteUrl") or [""])[0]),
                    str((query.get("remoteName") or ["origin"])[0]),
                ))
            else:
                self.json_response(200, git_branch_catalog(selected_bridge.workspace))
        except (BridgeFailure, planner.ToolFailure, ValueError) as exc:
            self.json_response(400, {"error": str(exc)})
        except Exception as exc:
            self.json_response(500, {"error": f"读取 Git 分支失败：{exc}"})
        return

    def _get_health(self, parsed: ParseResult) -> None:
        if not self.allowed_origin():
            self.json_response(403, {"error": "origin not allowed"})
            return
        query = parse_qs(parsed.query)
        provider = ai_provider_of((query.get("provider") or ["codex"])[0])
        program_id_value = (query.get("programId") or [""])[0]
        if parsed.path.endswith("/health") and not str(program_id_value).strip():
            self.json_response(200, self.bridge.health(provider))
            return
        try:
            program_id = program_id_of(program_id_value)
        except BridgeFailure as exc:
            self.json_response(400, {"error": str(exc)})
            return
        try:
            config = self.bridge.request_config(
                {"programId": program_id},
                self.allowed_origin() or "",
                self.headers.get("token", "").strip(),
            )
            selected_bridge = self.bridge.for_workspace((query.get("workspace") or [""])[0])
            if parsed.path.endswith("/health"):
                self.json_response(200, selected_bridge.health(provider))
                return
            self.json_response(200, selected_bridge.models(config, provider))
        except (BridgeFailure, planner.ToolFailure, ValueError) as exc:
            self.json_response(400, {"error": str(exc)})
        except Exception as exc:
            action = f"读取 {provider_label(provider)} 模型" if parsed.path.endswith("/models") else f"检查 {provider_label(provider)} 环境"
            self.json_response(500, {"error": f"{action}失败：{exc}"})
        return

    def _get_requirement_outline(self, parsed: ParseResult) -> None:
        if not self.allowed_origin():
            self.json_response(403, {"error": "origin not allowed"})
            return
        query = parse_qs(parsed.query)
        try:
            program_id = program_id_of((query.get("programId") or [""])[0])
            requirement_key = str((query.get("requirementKey") or [""])[0]).strip()
            if not requirement_key:
                raise BridgeFailure("缺少需求标识")
            selected_bridge = self.bridge.for_workspace((query.get("workspace") or [""])[0])
            config = self.bridge.request_config(
                {"programId": program_id}, self.allowed_origin() or "", self.headers.get("token", "").strip(),
            )
            self.json_response(200, selected_bridge.requirement_outline(program_id, requirement_key, config=config))
        except (BridgeFailure, planner.ToolFailure, ValueError) as exc:
            self.json_response(400, {"error": str(exc)})
        except Exception as exc:
            self.json_response(500, {"error": f"读取需求大纲失败：{exc}"})
        return

    def _get_requirement_prototype(self, parsed: ParseResult) -> None:
        if not self.allowed_origin():
            self.json_response(403, {"error": "origin not allowed"})
            return
        query = parse_qs(parsed.query)
        try:
            program_id = program_id_of((query.get("programId") or [""])[0])
            requirement_key = str((query.get("requirementKey") or [""])[0]).strip()
            if not requirement_key:
                raise BridgeFailure("缺少需求标识")
            selected_bridge = self.bridge.for_workspace((query.get("workspace") or [""])[0])
            config = self.bridge.request_config(
                {"programId": program_id}, self.allowed_origin() or "", self.headers.get("token", "").strip(),
            )
            self.json_response(200, selected_bridge.requirement_prototype(program_id, requirement_key, config=config))
        except (BridgeFailure, planner.ToolFailure, ValueError) as exc:
            self.json_response(400, {"error": str(exc)})
        except Exception as exc:
            self.json_response(500, {"error": f"读取需求 HTML 原型失败：{exc}"})
        return

    def _get_requirement_prototype_conversation(self, parsed: ParseResult) -> None:
        if not self.allowed_origin():
            self.json_response(403, {"error": "origin not allowed"})
            return
        query = parse_qs(parsed.query)
        try:
            program_id = program_id_of((query.get("programId") or [""])[0])
            requirement_key = str((query.get("requirementKey") or [""])[0]).strip()
            thread_id = str((query.get("threadId") or [""])[0]).strip()
            provider = ai_provider_of((query.get("provider") or ["codex"])[0])
            if not requirement_key:
                raise BridgeFailure("缺少需求标识")
            selected_bridge = self.bridge.for_workspace((query.get("workspace") or [""])[0])
            config = self.bridge.request_config(
                {"programId": program_id}, self.allowed_origin() or "", self.headers.get("token", "").strip(),
            )
            self.json_response(200, selected_bridge.requirement_prototype_conversation(
                program_id, requirement_key, thread_id, provider, config=config,
            ))
        except (BridgeFailure, planner.ToolFailure, ValueError) as exc:
            self.json_response(400, {"error": str(exc)})
        except Exception as exc:
            self.json_response(500, {"error": f"读取原型编辑会话失败：{exc}"})
        return

    def _get_requirement_testing(self, parsed: ParseResult) -> None:
        if not self.allowed_origin():
            self.json_response(403, {"error": "origin not allowed"})
            return
        query = parse_qs(parsed.query)
        try:
            program_id = program_id_of((query.get("programId") or [""])[0])
            requirement_key = str((query.get("requirementKey") or [""])[0]).strip()
            thread_id = str((query.get("threadId") or [""])[0]).strip()
            provider = ai_provider_of((query.get("provider") or ["codex"])[0])
            if not requirement_key:
                raise BridgeFailure("缺少需求标识")
            selected_bridge = self.bridge.for_workspace((query.get("workspace") or [""])[0])
            config = self.bridge.request_config(
                {"programId": program_id}, self.allowed_origin() or "", self.headers.get("token", "").strip(),
            )
            self.json_response(200, selected_bridge.requirement_testing(
                program_id, requirement_key, thread_id, provider, config=config,
            ))
        except (BridgeFailure, planner.ToolFailure, ValueError) as exc:
            self.json_response(400, {"error": str(exc)})
        except Exception as exc:
            self.json_response(500, {"error": f"读取需求总体测试会话失败：{exc}"})
        return

    def _get_requirement_review(self, parsed: ParseResult) -> None:
        if not self.allowed_origin():
            self.json_response(403, {"error": "origin not allowed"})
            return
        query = parse_qs(parsed.query)
        try:
            program_id = program_id_of((query.get("programId") or [""])[0])
            requirement_key = str((query.get("requirementKey") or [""])[0]).strip()
            thread_id = str((query.get("threadId") or [""])[0]).strip()
            provider = ai_provider_of((query.get("provider") or ["codex"])[0])
            if not requirement_key:
                raise BridgeFailure("缺少需求标识")
            selected_bridge = self.bridge.for_workspace((query.get("workspace") or [""])[0])
            config = self.bridge.request_config(
                {"programId": program_id}, self.allowed_origin() or "", self.headers.get("token", "").strip(),
            )
            self.json_response(200, selected_bridge.requirement_review(
                program_id, requirement_key, thread_id, provider, config=config,
            ))
        except (BridgeFailure, planner.ToolFailure, ValueError) as exc:
            self.json_response(400, {"error": str(exc)})
        except Exception as exc:
            self.json_response(500, {"error": f"读取代码 review 会话失败：{exc}"})
        return

    def _get_requirement_fine_tuning(self, parsed: ParseResult) -> None:
        if not self.allowed_origin():
            self.json_response(403, {"error": "origin not allowed"})
            return
        query = parse_qs(parsed.query)
        try:
            program_id = program_id_of((query.get("programId") or [""])[0])
            requirement_key = str((query.get("requirementKey") or [""])[0]).strip()
            thread_id = str((query.get("threadId") or [""])[0]).strip()
            provider = ai_provider_of((query.get("provider") or ["codex"])[0])
            if not requirement_key:
                raise BridgeFailure("缺少需求标识")
            selected_bridge = self.bridge.for_workspace((query.get("workspace") or [""])[0])
            config = self.bridge.request_config(
                {"programId": program_id}, self.allowed_origin() or "", self.headers.get("token", "").strip(),
            )
            self.json_response(200, selected_bridge.requirement_fine_tuning(
                program_id, requirement_key, thread_id, provider, config=config,
            ))
        except (BridgeFailure, planner.ToolFailure, ValueError) as exc:
            self.json_response(400, {"error": str(exc)})
        except Exception as exc:
            self.json_response(500, {"error": f"读取需求微调会话失败：{exc}"})
        return

    def _get_task_testing_cases(self, parsed: ParseResult) -> None:
        if not self.allowed_origin():
            self.json_response(403, {"error": "origin not allowed"})
            return
        query = parse_qs(parsed.query)
        try:
            program_id = program_id_of((query.get("programId") or [""])[0])
            item_key = str((query.get("itemKey") or [""])[0]).strip()
            thread_id = str((query.get("threadId") or [""])[0]).strip()
            provider = ai_provider_of((query.get("provider") or ["codex"])[0])
            if not item_key:
                raise BridgeFailure("缺少任务标识")
            selected_bridge = self.bridge.for_workspace((query.get("workspace") or [""])[0])
            config = self.bridge.request_config(
                {"programId": program_id}, self.allowed_origin() or "", self.headers.get("token", "").strip(),
            )
            self.json_response(200, selected_bridge.task_testing_cases_conversation(
                program_id, item_key, thread_id, provider, config=config,
            ))
        except (BridgeFailure, planner.ToolFailure, ValueError) as exc:
            self.json_response(400, {"error": str(exc)})
        except Exception as exc:
            self.json_response(500, {"error": f"读取任务测试用例会话失败：{exc}"})
        return

    def _get_task_fine_tuning(self, parsed: ParseResult) -> None:
        if not self.allowed_origin():
            self.json_response(403, {"error": "origin not allowed"})
            return
        query = parse_qs(parsed.query)
        try:
            program_id = program_id_of((query.get("programId") or [""])[0])
            item_key = str((query.get("itemKey") or [""])[0]).strip()
            thread_id = str((query.get("threadId") or [""])[0]).strip()
            provider = ai_provider_of((query.get("provider") or ["codex"])[0])
            if not item_key:
                raise BridgeFailure("缺少任务标识")
            selected_bridge = self.bridge.for_workspace((query.get("workspace") or [""])[0])
            config = self.bridge.request_config(
                {"programId": program_id}, self.allowed_origin() or "", self.headers.get("token", "").strip(),
            )
            self.json_response(200, selected_bridge.task_fine_tuning_conversation(
                program_id, item_key, thread_id, provider, config=config,
            ))
        except (BridgeFailure, planner.ToolFailure, ValueError) as exc:
            self.json_response(400, {"error": str(exc)})
        except Exception as exc:
            self.json_response(500, {"error": f"读取任务微调会话失败：{exc}"})
        return

    def _get_document_set(self, parsed: ParseResult) -> None:
        if not self.allowed_origin():
            self.json_response(403, {"error": "origin not allowed"})
            return
        query = parse_qs(parsed.query)
        try:
            program_id = program_id_of((query.get("programId") or [""])[0])
            scope = str((query.get("scope") or [""])[0]).strip()
            key = str((query.get("key") or [""])[0]).strip()
            if not program_id or not scope or not key:
                raise BridgeFailure("programId、scope 和 key 都是必填项")
            selected_bridge = self.bridge.for_workspace((query.get("workspace") or [""])[0])
            config = self.bridge.request_config(
                {"programId": program_id},
                self.allowed_origin() or "",
                self.headers.get("token", "").strip(),
            )
            if parsed.path == "/v1/codex/document-set":
                self.json_response(200, selected_bridge.document_set(program_id, scope, key, config=config))
            else:
                self.json_response(200, selected_bridge.document_file(
                    program_id, scope, key, str((query.get("path") or [""])[0]), config=config,
                ))
        except (BridgeFailure, planner.ToolFailure, ValueError) as exc:
            self.json_response(400, {"error": str(exc)})
        except Exception as exc:
            self.json_response(500, {"error": f"读取文档失败：{exc}"})
        return

    def _get_requirement_document(self, parsed: ParseResult) -> None:
        if not self.allowed_origin():
            self.json_response(403, {"error": "origin not allowed"})
            return
        query = parse_qs(parsed.query)
        program_id = program_id_of((query.get("programId") or [""])[0])
        item_key = str((query.get("itemKey") or [""])[0]).strip()
        if not program_id or not item_key:
            self.json_response(400, {"error": "programId and itemKey are required"})
            return
        try:
            selected_bridge = self.bridge.for_workspace((query.get("workspace") or [""])[0])
            config = self.bridge.request_config(
                {"programId": program_id},
                self.allowed_origin() or "",
                self.headers.get("token", "").strip(),
            )
            self.json_response(200, selected_bridge.requirement_document(program_id, item_key, config=config))
        except (BridgeFailure, planner.ToolFailure, ValueError) as exc:
            self.json_response(400, {"error": str(exc)})
        except Exception as exc:
            self.json_response(500, {"error": f"读取需求文档失败：{exc}"})
        return

    def _get_prototype_directory(self, parsed: ParseResult) -> None:
        if not self.allowed_origin():
            self.json_response(403, {"error": "origin not allowed"})
            return
        query = parse_qs(parsed.query)
        program_id = program_id_of((query.get("programId") or [""])[0])
        item_key = str((query.get("itemKey") or [""])[0]).strip()
        if not program_id or not item_key:
            self.json_response(400, {"error": "programId and itemKey are required"})
            return
        try:
            selected_bridge = self.bridge.for_workspace((query.get("workspace") or [""])[0])
            config = self.bridge.request_config(
                {"programId": program_id},
                self.allowed_origin() or "",
                self.headers.get("token", "").strip(),
            )
            self.json_response(200, selected_bridge.prototype_directory(program_id, item_key, config=config))
        except (BridgeFailure, planner.ToolFailure, ValueError) as exc:
            self.json_response(400, {"error": str(exc)})
        except Exception as exc:
            self.json_response(500, {"error": f"读取原型图目录失败：{exc}"})
        return

    def _get_business_attachment(self, parsed: ParseResult) -> None:
        query = parse_qs(parsed.query)
        try:
            selected_bridge = self.bridge.for_business_workspace((query.get("workspace") or [""])[0])
            manifest, path = selected_bridge.business_attachment(
                (query.get("programId") or [""])[0],
                (query.get("itemKey") or [""])[0],
                str((query.get("attachmentId") or [""])[0]).strip(),
            )
            self.attachment_response(manifest, path)
        except (BridgeFailure, ValueError) as exc:
            self.json_response(400, {"error": str(exc)})
        except OSError as exc:
            self.json_response(500, {"error": f"读取业务访谈附件失败：{exc}"})
        return

    def _get_conversation(self, parsed: ParseResult) -> None:
        if not self.allowed_origin():
            self.json_response(403, {"error": "origin not allowed"})
            return
        query = parse_qs(parsed.query)
        if business_intake_of((query.get("businessIntake") or [""])[0]):
            try:
                program_id = program_id_of((query.get("programId") or [""])[0])
                item_key = business_item_key_of((query.get("itemKey") or [""])[0])
                thread_id = str((query.get("threadId") or [""])[0]).strip()
                provider = ai_provider_of((query.get("provider") or ["codex"])[0])
                selected_bridge = self.bridge.for_business_workspace((query.get("workspace") or [""])[0])
                self.json_response(200, selected_bridge.business_conversation(program_id, item_key, thread_id, provider))
            except (BridgeFailure, ValueError) as exc:
                self.json_response(400, {"error": str(exc)})
            except Exception as exc:
                self.json_response(500, {"error": f"读取业务访谈会话失败：{exc}"})
            return
        program_id = program_id_of((query.get("programId") or [""])[0])
        item_key = str((query.get("itemKey") or [""])[0]).strip()
        thread_id = str((query.get("threadId") or [""])[0]).strip()
        provider = ai_provider_of((query.get("provider") or ["codex"])[0])
        if not program_id or not item_key:
            self.json_response(400, {"error": "programId and itemKey are required"})
            return
        try:
            selected_bridge = self.bridge.for_workspace((query.get("workspace") or [""])[0])
            config = self.bridge.request_config(
                {"programId": program_id},
                self.allowed_origin() or "",
                self.headers.get("token", "").strip(),
            )
            self.json_response(200, selected_bridge.conversation(program_id, item_key, thread_id, config=config, provider=provider))
        except (BridgeFailure, planner.ToolFailure, ValueError) as exc:
            self.json_response(400, {"error": str(exc)})
        except Exception as exc:
            self.json_response(500, {"error": f"读取 Codex 会话失败：{exc}"})
        return

    def _get_environment_setup(self, parsed: ParseResult) -> None:
        if not self.allowed_origin():
            self.json_response(403, {"error": "origin not allowed"})
            return
        query = parse_qs(parsed.query)
        try:
            thread_id = str((query.get("threadId") or [""])[0]).strip()
            provider = ai_provider_of((query.get("provider") or ["codex"])[0])
            use_git = str((query.get("useGit") or [""])[0]).strip().lower() == "true"
            environments_raw = str((query.get("environments") or ["[]"])[0])
            try:
                environments = environment_selection_of(json.loads(environments_raw))
            except (json.JSONDecodeError, TypeError):
                raise BridgeFailure("预设环境参数无效")
            config = self.bridge.global_environment_config({}, self.headers.get("token", "").strip())
            self.json_response(200, self.bridge.environment_setup(
                GLOBAL_ENVIRONMENT_SETUP_PROGRAM_ID,
                thread_id,
                config=config,
                provider=provider,
                use_git=use_git,
                environments=environments,
            ))
        except (BridgeFailure, planner.ToolFailure, ValueError) as exc:
            self.json_response(400, {"error": str(exc)})
        except Exception as exc:
            self.json_response(500, {"error": f"读取预设环境会话失败：{exc}"})
        return

    def _get_planning(self, parsed: ParseResult) -> None:
        if not self.allowed_origin():
            self.json_response(403, {"error": "origin not allowed"})
            return
        query = parse_qs(parsed.query)
        program_id = program_id_of((query.get("programId") or [""])[0])
        thread_id = str((query.get("threadId") or [""])[0]).strip()
        requirement_key = str((query.get("requirementKey") or [""])[0]).strip()
        provider = ai_provider_of((query.get("provider") or ["codex"])[0])
        if not program_id:
            self.json_response(400, {"error": "programId is required"})
            return
        try:
            selected_bridge = self.bridge.for_workspace((query.get("workspace") or [""])[0])
            config = self.bridge.request_config(
                {"programId": program_id},
                self.allowed_origin() or "",
                self.headers.get("token", "").strip(),
            )
            self.json_response(200, selected_bridge.planning(program_id, thread_id, config=config, requirement_key=requirement_key, provider=provider))
        except (BridgeFailure, planner.ToolFailure, ValueError) as exc:
            self.json_response(400, {"error": str(exc)})
        except Exception as exc:
            self.json_response(500, {"error": f"读取拆解会话失败：{exc}"})
        return

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/v1/session/heartbeat":
            if not self.allowed_origin():
                self.json_response(403, {"error": "origin not allowed"})
                return
            self.handle_heartbeat()
            return
        if path in {"/v1/plugin/update/install", "/v1/plugin/update/restart"}:
            if not self.allowed_origin():
                self.json_response(403, {"error": "origin not allowed"})
                return
            try:
                if self.headers.get_content_type() != "application/json":
                    raise BridgeFailure("application/json required")
                length = int(self.headers.get("Content-Length") or 0)
                if length < 0 or length > 8 * 1024:
                    raise BridgeFailure("请求体大小无效")
                payload = json.loads(self.rfile.read(length)) if length else {}
                if not isinstance(payload, dict):
                    raise BridgeFailure("请求体必须是 JSON 对象")
                if path.endswith("/install"):
                    job = PLUGIN_UPDATES.start(str(payload.get("expectedVersion") or "").strip())
                    job["activeRuns"] = self.bridge.active_run_count()
                    complete_plugin_update_in_background(str(job.get("jobId") or ""), self.bridge)
                    self.json_response(202, job)
                    return
                active_runs = self.bridge.active_run_count()
                if active_runs:
                    self.json_response(409, {"error": f"当前还有 {active_runs} 个执行会话运行中，请等待完成后再重启"})
                    return
                job = PLUGIN_UPDATES.mark_restarting(str(payload.get("jobId") or "").strip())
                job["activeRuns"] = 0
                self.json_response(202, job)
                schedule_bridge_restart()
                return
            except (BridgeFailure, UpdateFailure, json.JSONDecodeError, ValueError) as exc:
                self.json_response(400, {"error": str(exc)})
            except Exception as exc:
                self.json_response(500, {"error": f"更新插件失败：{exc}"})
            return
        if path not in {
            "/v1/codex/execute",
            "/v1/codex/task-testing-cases",
            "/v1/codex/task-testing-cases/stop",
            "/v1/codex/task-fine-tuning",
            "/v1/codex/task-fine-tuning/stop",
            "/v1/codex/execute-batch",
            "/v1/codex/execute-sequence",
            "/v1/codex/conversation",
            "/v1/codex/planning",
            "/v1/codex/planning/stop",
            "/v1/codex/environment-setup",
            "/v1/codex/environment-setup/stop",
            "/v1/codex/requirement-prototype/generate",
            "/v1/codex/requirement-prototype/conversation",
            "/v1/codex/requirement-testing",
            "/v1/codex/requirement-testing/stop",
            "/v1/codex/requirement-review",
            "/v1/codex/requirement-review/stop",
            "/v1/codex/requirement-fine-tuning",
            "/v1/codex/requirement-fine-tuning/stop",
            "/v1/codex/attachments",
            "/v1/codex/business-attachments",
            "/v1/codex/prototype-directory/open",
            "/v1/codex/workspace-file/reveal",
            "/v1/codex/git/branch",
            "/v1/codex/git/init",
            "/v1/codex/git/submodules",
            "/v1/codex/git/prepare",
            "/v1/codex/git/push",
            "/v1/codex/git/merge",
            "/v1/codex/cloud-sync",
            "/v1/codex/requirement-document",
            "/v1/codex/requirement-outline",
            "/v1/codex/document-file",
            "/v1/codex/document-upload",
            "/v1/codex/document-attachment",
            "/v1/codex/stop",
            "/v1/codex/stop-all",
        }:
            self.json_response(404, {"error": "not found"})
            return
        if not self.allowed_origin():
            self.json_response(403, {"error": "origin not allowed"})
            return
        try:
            if path == "/v1/codex/attachments":
                self.handle_attachment_upload()
                return
            if path == "/v1/codex/business-attachments":
                self.handle_business_attachment_upload()
                return
            if path == "/v1/codex/document-upload":
                self.handle_document_upload()
                return
            if self.headers.get_content_type() != "application/json":
                self.json_response(415, {"error": "application/json required"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            # 需求文档与需求大纲编辑提交的是整篇 Markdown，比控制类请求大一个量级，单独放宽。
            limit = (
                MAX_REQUIREMENT_DOCUMENT_BYTES + 4 * 1024
                if path == "/v1/codex/requirement-document"
                else MAX_EDITABLE_OUTLINE_BYTES + 4 * 1024
                if path == "/v1/codex/requirement-outline"
                else MAX_DOCUMENT_SET_FILE_BYTES + 4 * 1024
                if path == "/v1/codex/document-file"
                else 64 * 1024
            )
            if length <= 0 or length > limit:
                raise BridgeFailure("请求体大小无效")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise BridgeFailure("请求体必须是 JSON 对象")
            if path == "/v1/codex/conversation" and business_intake_of(payload.get("businessIntake")):
                selected_bridge = self.bridge.for_business_workspace(payload.get("workspace"))
                self.json_response(202, selected_bridge.send_business_conversation(payload))
                return
            if path == "/v1/codex/git/submodules":
                # 目录早就是仓库、只是子模块没拉下来：单独补这一步，不碰主仓库的分支和改动。
                config = self.bridge.request_config(
                    payload,
                    self.allowed_origin() or "",
                    self.headers.get("token", "").strip(),
                )
                selected_bridge = self.bridge.for_workspace(payload.get("workspace"))
                self.bridge.remember_remote_workspace(program_id_of(payload.get("programId")), selected_bridge.workspace, config)
                submodule_workspace = selected_bridge.workspace
                require_git_workspace(submodule_workspace)
                self.json_response(200, {
                    "workspace": str(submodule_workspace),
                    **git_initialize_submodules(submodule_workspace),
                })
                return
            if path == "/v1/codex/git/init":
                # 初始化时目录还不是仓库、甚至可能还没建出来，不能先走 for_workspace 的存在性校验。
                config = self.bridge.request_config(
                    payload,
                    self.allowed_origin() or "",
                    self.headers.get("token", "").strip(),
                )
                initialized_workspace = git_initializable_workspace_of(payload.get("workspace"))
                self.bridge.remember_remote_workspace(program_id_of(payload.get("programId")), initialized_workspace, config)
                self.json_response(200, git_initialize_workspace(
                    initialized_workspace,
                    str(payload.get("repositoryUrl") or ""),
                    str(payload.get("remoteName") or "origin").strip() or "origin",
                    str(payload.get("baseBranch") or "").strip(),
                ))
                return
            if path in {"/v1/codex/environment-setup", "/v1/codex/environment-setup/stop"}:
                config = self.bridge.global_environment_config(payload, self.headers.get("token", "").strip())
                selected_bridge = self.bridge
            else:
                config = self.bridge.request_config(
                    payload,
                    self.allowed_origin() or "",
                    self.headers.get("token", "").strip(),
                )
                selected_bridge = self.bridge.for_workspace(payload.get("workspace"))
                self.bridge.remember_remote_workspace(program_id_of(payload.get("programId")), selected_bridge.workspace, config)
            route = self.POST_ROUTES.get(path)
            if route is not None:
                status, target, method, config_as_keyword = route
                target_bridge = selected_bridge if target == "workspace" else self.bridge
                call = getattr(target_bridge, method)
                self.json_response(
                    status,
                    call(payload, config=config) if config_as_keyword else call(payload, config),
                )
            elif path == "/v1/codex/requirement-document":
                item_key = str(payload.get("itemKey") or "").strip()
                if not item_key:
                    raise BridgeFailure("缺少任务标识")
                content = payload.get("content")
                if not isinstance(content, str):
                    raise BridgeFailure("需求文档正文必须是字符串")
                self.json_response(200, selected_bridge.save_requirement_document(
                    program_id_of(payload.get("programId")), item_key, content, config=config,
                ))
            elif path == "/v1/codex/requirement-outline":
                requirement_key = str(payload.get("requirementKey") or "").strip()
                if not requirement_key:
                    raise BridgeFailure("缺少需求标识")
                markdown = payload.get("markdown")
                if not isinstance(markdown, str):
                    raise BridgeFailure("需求大纲正文必须是字符串")
                self.json_response(200, selected_bridge.save_requirement_outline(
                    program_id_of(payload.get("programId")), requirement_key, markdown, config=config,
                ))
            elif path == "/v1/codex/document-file":
                self.json_response(200, selected_bridge.save_document_file(
                    program_id_of(payload.get("programId")),
                    str(payload.get("scope") or "").strip(),
                    str(payload.get("key") or "").strip(),
                    str(payload.get("path") or "").strip(),
                    payload.get("content"),
                    config=config,
                ))
            elif path == "/v1/codex/document-attachment":
                self.json_response(200, selected_bridge.document_attachment(
                    program_id_of(payload.get("programId")),
                    str(payload.get("scope") or "").strip(),
                    str(payload.get("key") or "").strip(),
                    str(payload.get("path") or "").strip(),
                    config=config,
                ))
            elif path == "/v1/codex/cloud-sync":
                self.json_response(200, selected_bridge.sync_cloud_workspace(
                    program_id_of(payload.get("programId")), config,
                ))
            elif path == "/v1/codex/git/branch":
                self.json_response(200, git_create_branch_targets(
                    selected_bridge.workspace,
                    str(payload.get("baseBranch") or "").strip(),
                    str(payload.get("branch") or "").strip(),
                    # 建分支不做「没传就全建」：勾选哪些子项目由创建表单说了算。
                    git_subproject_targets_of(selected_bridge.workspace, payload.get("targets") or []),
                    bool(payload.get("skipRoot")),
                ))
            elif path == "/v1/codex/git/prepare":
                self.json_response(200, selected_bridge.prepare_requirement_git_branch(payload))
            elif path == "/v1/codex/workspace-file/reveal":
                self.json_response(202, selected_bridge.reveal_workspace_file(str(payload.get("path") or "")))
            elif path == "/v1/codex/prototype-directory/open":
                item_key = str(payload.get("itemKey") or "").strip()
                if not item_key:
                    raise BridgeFailure("缺少任务标识")
                self.json_response(202, selected_bridge.open_prototype_directory(program_id_of(payload.get("programId")), item_key, config=config))
            else:
                raise BridgeFailure(f"未处理的接口路径：{path}")
        except (BridgeFailure, planner.ToolFailure, json.JSONDecodeError, ValueError) as exc:
            self.json_response(400, {"error": str(exc)})
        except Exception as exc:
            self.json_response(500, {"error": f"启动 AI 工具失败：{exc}"})

    def handle_heartbeat(self) -> None:
        """控制台每分钟送一次当前账号的 token 和 user_id，插件存下来当作凭证来源。

        配置文件只维护接口地址；普通命令行会话没有面板注入的环境变量，全靠这里
        存下来的凭证。心跳不打任何面板接口，凭证真伪由后续真实请求判定。
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length < 0 or length > 8 * 1024:
                raise BridgeFailure("请求体大小无效")
            payload = json.loads(self.rfile.read(length)) if length else {}
            if not isinstance(payload, dict):
                raise BridgeFailure("请求体必须是 JSON 对象")
            token = self.headers.get("token", "").strip() or str(payload.get("token") or "").strip()
            if not token:
                raise BridgeFailure("当前用户凭证为空")
            user_id = str(payload.get("userId") or "").strip()
            subject = planner.token_subject(token)
            if not subject:
                raise BridgeFailure("凭证不是任务面板登录凭证")
            if user_id and user_id != subject:
                raise BridgeFailure("凭证与用户标识不一致")
            planner.save_credential(token, user_id or subject)
            self.json_response(200, {"stored": True, "userId": user_id or subject})
        except (BridgeFailure, planner.ToolFailure, json.JSONDecodeError, ValueError) as exc:
            self.json_response(400, {"error": str(exc)})
        except Exception as exc:
            self.json_response(500, {"error": f"保存任务面板凭证失败：{exc}"})

    def handle_document_upload(self) -> None:
        """把面板选的本地文件（或粘贴正文生成的文件）写进某个栏目的目录。"""
        content_length = int(self.headers.get("Content-Length") or 0)
        if content_length <= 0 or content_length > MAX_DOCUMENT_UPLOAD_BYTES:
            raise BridgeFailure("文档请求体大小无效")
        if self.headers.get_content_type() != "multipart/form-data":
            raise BridgeFailure("文档必须使用 multipart/form-data 上传")
        fields, uploads = self.read_multipart(content_length)
        program_id = program_id_of(fields.get("programId"))
        selected_bridge = self.bridge.for_workspace(fields.get("workspace"))
        config = self.bridge.request_config(
            {"programId": program_id},
            self.allowed_origin() or "",
            self.headers.get("token", "").strip(),
        )
        self.bridge.remember_remote_workspace(program_id, selected_bridge.workspace, config)
        self.json_response(
            201,
            selected_bridge.upload_documents(
                program_id,
                fields.get("scope", ""),
                fields.get("key", ""),
                uploads,
                config_biz_line(config),
                config,
            ),
        )

    def read_multipart(self, content_length: int) -> tuple[dict[str, str], list[dict[str, Any]]]:
        """Split one multipart body into plain fields and uploaded files."""
        content_type = self.headers.get("Content-Type", "")
        raw = self.rfile.read(content_length)
        message = BytesParser(policy=policy.default).parsebytes(
            f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("ascii") + raw
        )
        if not message.is_multipart():
            raise BridgeFailure("请求体不是有效的 multipart/form-data")
        fields: dict[str, str] = {}
        uploads: list[dict[str, Any]] = []
        for part in message.iter_parts():
            name = str(part.get_param("name", header="content-disposition") or "")
            filename = str(part.get_filename() or "")
            data = part.get_payload(decode=True) or b""
            if not filename:
                fields[name] = data.decode(part.get_content_charset() or "utf-8", errors="replace").strip()
                continue
            uploads.append({"name": filename, "contentType": part.get_content_type(), "data": data})
        return fields, uploads

    def handle_business_attachment_upload(self) -> None:
        """业务方在访谈里贴的图片和文档：不走任务面板凭证，只认业务工作目录。"""
        content_length = int(self.headers.get("Content-Length") or 0)
        if content_length <= 0 or content_length > MAX_CONVERSATION_UPLOAD_BYTES:
            raise BridgeFailure("附件请求体大小无效")
        if self.headers.get_content_type() != "multipart/form-data":
            raise BridgeFailure("附件必须使用 multipart/form-data 上传")
        fields, uploads = self.read_multipart(content_length)
        selected_bridge = self.bridge.for_business_workspace(fields.get("workspace"))
        self.json_response(
            201,
            selected_bridge.save_business_attachments(
                fields.get("programId"),
                fields.get("itemKey", ""),
                uploads,
            ),
        )

    def handle_attachment_upload(self) -> None:
        content_length = int(self.headers.get("Content-Length") or 0)
        if content_length <= 0 or content_length > MAX_CONVERSATION_UPLOAD_BYTES:
            raise BridgeFailure("附件请求体大小无效")
        if self.headers.get_content_type() != "multipart/form-data":
            raise BridgeFailure("附件必须使用 multipart/form-data 上传")
        fields, uploads = self.read_multipart(content_length)
        program_id = program_id_of(fields.get("programId"))
        selected_bridge = self.bridge.for_workspace(fields.get("workspace"))
        config = self.bridge.request_config(
            {"programId": program_id},
            self.allowed_origin() or "",
            self.headers.get("token", "").strip(),
        )
        self.bridge.remember_remote_workspace(program_id, selected_bridge.workspace, config)
        self.json_response(
            201,
            selected_bridge.upload_conversation_attachments(
                config_biz_line(config),
                program_id,
                fields.get("itemKey", ""),
                uploads,
                config,
            ),
        )

    def log_message(self, format: str, *args: Any) -> None:
        return


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--command-api-url",
        default=os.environ.get("DELIVERY_COMMAND_API_URL", ""),
        help="app-api base URL for the optional remote command Worker (for example https://api.example.com)",
    )
    # 进程级工作目录是可选的：真正干活的目录由每个请求带的 workspace 决定（见 for_workspace）。
    # 不给就落到一个空的中性占位目录，绝不拿安装目录或启动目录冒充某个项目的仓库。
    parser.add_argument("--workspace", default="")
    parser.add_argument("--allow-origin", action="append", default=[])
    parser.add_argument(
        "--business-workspace-root",
        default=os.environ.get("BUSINESS_KODES_WORKSPACE_ROOT", str(DEFAULT_BUSINESS_WORKSPACE_ROOT)),
        help="远端业务访谈的受控工作目录根路径；默认 ~/.local/share/delivery-task-planner/business-workspaces",
    )
    args = parser.parse_args()
    if args.workspace:
        workspace = Path(args.workspace).resolve()
        if not workspace.is_dir():
            raise SystemExit(f"workspace does not exist: {workspace}")
    else:
        workspace = placeholder_workspace()
    business_workspace_root = Path(args.business_workspace_root).expanduser().resolve()
    origins = set(args.allow_origin or ["*"])
    httpd = create_http_server(
        args.host,
        args.port,
        workspace,
        origins,
        business_workspace_root=business_workspace_root,
    )
    threading.Thread(target=httpd.bridge.reconcile_forever, daemon=True).start()  # type: ignore[attr-defined]
    remote_worker = RemoteCommandWorker(httpd.bridge, api_url=args.command_api_url)  # type: ignore[attr-defined]
    httpd.remote_worker = remote_worker  # type: ignore[attr-defined]
    threading.Thread(target=remote_worker.run_forever, daemon=True, name="delivery-remote-worker").start()
    try:
        httpd.serve_forever()
    finally:
        remote_worker.stop()
        # 只读执行器是常驻子进程，退出时收干净，别留孤儿。
        THREAD_READERS.shutdown()


if __name__ == "__main__":
    main()
