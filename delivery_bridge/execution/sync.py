"""聊天归档与云同步。

Git 聊天同步决定 chat/ 是否落到工作目录，云端开关决定把用户选中的内容传给服务端，
两者相互独立。读会话正文时优先用执行器本机的历史，读不到再回落到工作目录的归档。
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path
from typing import Any

import server as planner

from delivery_bridge.chat_archive import (
    CLOUD_SYNC_SCOPES,
    MAX_CLOUD_SYNC_FILE_BYTES,
    archive_chat_snapshot,
    archived_chat_text,
    chat_archive_relative_path,
    cloud_sync_workspace_entries,
    read_workspace_chat_archive,
)
from delivery_bridge.cloud_documents import CloudDocumentIndex
from delivery_bridge.clients.pool import (
    ACTIVE_THREAD_READ_TIMEOUT_SECONDS,
    THREAD_READERS,
    read_thread_or_empty,
)
from delivery_bridge.errors import BridgeFailure
from delivery_bridge.payloads import assert_runtime_project


class SyncMixin:
    def _project_content_sync_settings(self, config: dict[str, Any], program_id: int) -> dict[str, Any]:
        """Read the authoritative project-level switches; failures deliberately fail closed."""
        if program_id <= 0:
            return {}
        try:
            program = planner.request_api(
                config,
                "GET",
                "/delivery/program",
                query={"programId": program_id},
            )
        except planner.ToolFailure as exc:
            print(f"读取项目内容同步配置失败，跳过本地/云端归档：{program_id}: {exc}", file=sys.stderr, flush=True)
            return {}
        return program if isinstance(program, dict) else {}

    def _project_chat_archive_enabled(self, config: dict[str, Any], program_id: int) -> bool:
        """The explicit project Git chat-sync switch controls workspace chat/ archives."""
        return bool(self._project_content_sync_settings(config, program_id).get("gitChatSyncEnabled"))

    @staticmethod
    def _project_cloud_sync_scopes(program: dict[str, Any]) -> set[str]:
        if not bool(program.get("cloudSyncEnabled")):
            return set()
        raw_scopes = program.get("cloudSyncScopes")
        if not isinstance(raw_scopes, list):
            return set()
        return {str(scope).strip() for scope in raw_scopes if str(scope).strip() in CLOUD_SYNC_SCOPES}

    @staticmethod
    def _upload_cloud_sync_file(
        config: dict[str, Any],
        program_id: int,
        category: str,
        relative_path: str,
        content_type: str,
        content: bytes,
        owner_kind: str = "program",
        owner_key: str = "",
        stage: str = "",
    ) -> None:
        if category not in CLOUD_SYNC_SCOPES:
            raise BridgeFailure("云端同步类别无效")
        if len(content) > MAX_CLOUD_SYNC_FILE_BYTES:
            raise BridgeFailure(f"云端同步文件不能超过 8MB：{relative_path}")
        planner.request_api(
            config,
            "POST",
            "/delivery/cloud-sync/file",
            body={
                "programId": program_id,
                "category": category,
                "relativePath": relative_path,
                "contentType": content_type,
                "contentBase64": base64.b64encode(content).decode("ascii"),
                "ownerKind": owner_kind,
                "ownerKey": owner_key,
                "stage": stage,
                "actorName": "delivery-http-bridge",
            },
        )

    def _cloud_document_index(self, config: dict[str, Any], program_id: int) -> CloudDocumentIndex:
        """需求与任务清单是归属判断的唯一依据；读不回来就整批按未归类上传。

        面板上「这份文档属于哪条需求 / 哪条任务」不能靠猜路径里的目录名，
        所以清单请求失败时宁可不归类，也不要造出一条不存在的需求或任务。
        """
        try:
            requirements = planner.request_api(
                config, "GET", "/delivery/requirements", query={"programId": program_id},
            ) or []
        except planner.ToolFailure as exc:
            print(f"读取需求清单失败，本次云端同步不归类：{program_id}: {exc}", file=sys.stderr, flush=True)
            requirements = []
        items: list[Any] = []
        page_index = 1
        while True:
            try:
                page = planner.request_api(
                    config, "GET", "/delivery/items",
                    query={"programId": program_id, "pageIndex": page_index, "pageSize": 200},
                ) or {}
            except planner.ToolFailure as exc:
                print(f"读取任务清单失败，本次云端同步不归类：{program_id}: {exc}", file=sys.stderr, flush=True)
                items = []
                break
            rows = page.get("data") if isinstance(page, dict) else None
            if not isinstance(rows, list) or not rows:
                break
            items.extend(rows)
            if len(items) >= int(page.get("total") or len(items)):
                break
            page_index += 1
        requirement_rows = requirements if isinstance(requirements, list) else []
        return CloudDocumentIndex(requirement_rows, items)

    def _sync_workspace_cloud_files(
        self,
        config: dict[str, Any],
        program_id: int,
        scopes: set[str],
    ) -> dict[str, Any]:
        index = self._cloud_document_index(config, program_id)
        entries, skipped = cloud_sync_workspace_entries(self.workspace, scopes, program_id, index)
        uploaded: list[str] = []
        for entry in entries:
            self._upload_cloud_sync_file(
                config, program_id, entry.category, entry.relative_path, entry.content_type,
                entry.source.read_bytes(), entry.owner_kind, entry.owner_key, entry.stage,
            )
            uploaded.append(entry.relative_path)
        return {
            "enabled": bool(scopes), "scopes": sorted(scopes),
            "uploaded": len(uploaded), "skipped": skipped, "files": uploaded,
        }

    def sync_cloud_workspace(self, program_id: int, config: dict[str, Any]) -> dict[str, Any]:
        """Manually sync the currently selected project workspace without exposing its absolute path."""
        assert_runtime_project(config, program_id)
        program = self._project_content_sync_settings(config, program_id)
        scopes = self._project_cloud_sync_scopes(program)
        if not scopes:
            return {"enabled": False, "scopes": [], "uploaded": 0, "skipped": 0, "files": []}
        return self._sync_workspace_cloud_files(config, program_id, scopes)

    def _archive_terminal_chat(
        self,
        client: Any,
        *,
        config: dict[str, Any],
        program_id: int,
        resource_kind: str,
        resource_key: str,
        resource_name: str,
        requirement_key: str = "",
        conversation_title: str,
        thread_id: str,
        provider: str,
        phase: str,
        terminal_status: str,
    ) -> None:
        """Best-effort workspace archive; failures must not hide the task result."""
        program = self._project_content_sync_settings(config, program_id)
        archive_to_workspace = bool(program.get("gitChatSyncEnabled"))
        cloud_scopes = self._project_cloud_sync_scopes(program)
        if not archive_to_workspace and not cloud_scopes:
            return
        try:
            if thread_id and (archive_to_workspace or "chat" in cloud_scopes):
                thread = client.read_thread(thread_id, request_id=client.next_request_id())
                turns = thread.get("turns") if isinstance(thread, dict) else []
                relative = chat_archive_relative_path(
                    resource_kind,
                    resource_key,
                    conversation_title or resource_name,
                    thread_id,
                    requirement_key=requirement_key,
                )
                if archive_to_workspace:
                    relative = archive_chat_snapshot(
                        self.workspace,
                        resource_kind=resource_kind,
                        resource_key=resource_key,
                        resource_name=resource_name,
                        requirement_key=requirement_key,
                        conversation_title=conversation_title,
                        thread_id=thread_id,
                        provider=provider,
                        phase=phase,
                        terminal_status=terminal_status,
                        turns=turns,
                    )
                    print(f"聊天记录已归档：{relative.as_posix()}", file=sys.stderr, flush=True)
                if "chat" in cloud_scopes:
                    owning_requirement = resource_key if resource_kind == "requirement" else requirement_key
                    self._upload_cloud_sync_file(
                        config,
                        program_id,
                        "chat",
                        relative.as_posix(),
                        "text/markdown; charset=utf-8",
                        archived_chat_text(
                            resource_kind=resource_kind,
                            resource_key=resource_key,
                            resource_name=resource_name,
                            requirement_key=requirement_key,
                            conversation_title=conversation_title,
                            thread_id=thread_id,
                            provider=provider,
                            phase=phase,
                            terminal_status=terminal_status,
                            turns=turns,
                        ).encode("utf-8"),
                        "requirement" if owning_requirement else "program",
                        owning_requirement,
                        "chat",
                    )
            document_scopes = cloud_scopes - {"chat"}
            if document_scopes:
                self._sync_workspace_cloud_files(config, program_id, document_scopes)
        except Exception as exc:
            print(f"归档或云端同步失败：{resource_kind}/{resource_key}/{thread_id}: {exc}", file=sys.stderr, flush=True)

    def _read_thread_with_workspace_archive(
        self,
        client: Any,
        thread_id: str,
        resource_kind: str,
        resource_key: str,
        config: dict[str, Any],
        program_id: int,
        *,
        provider: str = "codex",
        environment: dict[str, str] | None = None,
        workspace: Path | None = None,
    ) -> dict[str, Any]:
        """Prefer the executor's local history, then fall back to this workspace's Chat archive.

        `client` 传空表示这条线程当前没有活跃回合，正文走只读复用池：不再为每次
        轮询拉起一个执行器子进程，同一瞬间的重复读也会被合并掉。
        """
        reader_workspace = workspace or self.workspace
        if client is None:
            thread = THREAD_READERS.read(provider, reader_workspace, environment, thread_id)
        else:
            # 回合正在跑：共用执行器那一路的 client，给一个明显低于面板超时的上限，
            # 读不回来就用上一次的好快照兜底，别让浏览器等到自己 abort。
            thread = read_thread_or_empty(client, thread_id, timeout=ACTIVE_THREAD_READ_TIMEOUT_SECONDS)
            if thread.get("turns"):
                THREAD_READERS.remember(provider, reader_workspace, thread_id, thread)
            else:
                thread = THREAD_READERS.last_good(provider, reader_workspace, thread_id) or thread
        turns = thread.get("turns") if isinstance(thread, dict) else None
        if isinstance(turns, list) and turns:
            return thread
        if not self._project_chat_archive_enabled(config, program_id):
            return thread
        archived = read_workspace_chat_archive(self.workspace, resource_kind, resource_key, thread_id)
        archived_turns = archived.get("turns") if isinstance(archived, dict) else None
        if isinstance(archived_turns, list) and archived_turns:
            print(
                f"本机执行器未返回会话正文，已从项目聊天归档读取：{resource_kind}/{resource_key}/{thread_id}",
                file=sys.stderr,
                flush=True,
            )
            return archived
        return thread
