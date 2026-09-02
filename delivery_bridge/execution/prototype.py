"""需求原型：大纲、原型页生成与追改、目录与文件的本机打开。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import server as planner

from delivery_bridge.artifacts import IMAGE_SUFFIXES
from delivery_bridge.clients import factory
from delivery_bridge.clients.codex import AppServerClient
from delivery_bridge.documents import (
    requirement_outline_document,
    requirement_outline_path_of,
    requirement_prototype_directory_of,
    write_outline_document,
)
from delivery_bridge.errors import BridgeFailure
from delivery_bridge.executor_env import codex_environment
from delivery_bridge.item_keys import (
    requirement_prototype_executor_type,
    requirement_prototype_files,
    requirement_prototype_item_key,
)
from delivery_bridge.payloads import (
    biz_line_of,
    config_biz_line,
    request_scoped_config,
    task_identity,
    validate_requirement_prototype_payload,
)
from delivery_bridge.prompts.common import prototype_directory_of
from delivery_bridge.prompts.planning import planning_detail_digest
from delivery_bridge.prompts.requirement import (
    build_requirement_prototype_prompt,
    prototype_session_detail_digest,
)
from delivery_bridge.providers import (
    DEFAULT_BIZ_LINE,
    ai_provider_of,
    executor_provider_of,
    fast_mode_of,
    reasoning_effort_of,
    same_executor_purpose,
)
from delivery_bridge.token_usage import with_usage
from delivery_bridge.turn_view import serialize_turns


class PrototypeMixin:
    @staticmethod
    def _requirement_prototype_identity(program_id: int, requirement_key: str) -> tuple[str, int, str]:
        return task_identity("", program_id, requirement_prototype_item_key(requirement_key))

    def _requirement_for_prototype(self, config: dict[str, Any], program_id: int, requirement_key: str) -> dict[str, Any]:
        requirement = planner.request_api(
            config,
            "GET",
            "/delivery/requirement",
            query={"programId": program_id, "requirementKey": requirement_key},
        )
        if not isinstance(requirement, dict) or str(requirement.get("requirementKey") or "") != requirement_key:
            raise BridgeFailure("需求不存在或无法读取")
        return requirement

    def _prototype_session_rows(
        self, config: dict[str, Any], program_id: int, requirement_key: str, provider: str,
    ) -> list[dict[str, Any]]:
        # 只比用途后缀，不比工具：换成另一个工具之后，之前那批原型会话也要留在列表里。
        rows = planner.request_api(
            config,
            "GET",
            "/delivery/requirement/planning-sessions",
            query={"programId": program_id, "requirementKey": requirement_key},
        )
        return [
            row for row in (rows or [])
            if isinstance(row, dict) and str(row.get("threadId") or "")
            and same_executor_purpose(row, requirement_prototype_executor_type(provider))
        ]

    def _save_prototype_session(
        self,
        config: dict[str, Any],
        program_id: int,
        requirement_key: str,
        provider: str,
        thread_id: str,
        turn_id: str,
        title: str,
        status: str,
        detail_digest: str = "",
    ) -> None:
        planner.request_api(
            config,
            "POST",
            "/delivery/requirement/planning-session/bind",
            body={
                "programId": program_id,
                "requirementKey": requirement_key,
                "executorType": requirement_prototype_executor_type(provider),
                "threadId": thread_id,
                "title": title[:120],
                "status": status,
                "metadata": {
                    "turnId": turn_id,
                    "kind": "requirement-prototype",
                    "detailDigest": detail_digest,
                    "workspace": self.workspace.name,
                },
                "actorName": f"{provider}-http-bridge",
            },
        )

    def requirement_outline(
        self,
        program_id: int,
        requirement_key: str,
        biz_line: str = DEFAULT_BIZ_LINE,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Read the breakdown outline the planning session keeps for one requirement."""
        config = request_scoped_config(config, biz_line, program_id)
        # 走一遍需求校验：面板不能靠猜一个需求键就读到工作区里的任意文档。
        self._requirement_for_prototype(config, program_id, requirement_key)
        document = requirement_outline_document(self.workspace, requirement_key)
        identity = self._planning_identity(program_id, requirement_key)
        with self.lock:
            active = self.active_runs.get(identity)
        return {"programId": program_id, "requirementKey": requirement_key, **document, "active": active is not None}

    def save_requirement_outline(
        self,
        program_id: int,
        requirement_key: str,
        markdown: str,
        biz_line: str = DEFAULT_BIZ_LINE,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Overwrite one requirement's breakdown outline from the task board editor."""
        config = request_scoped_config(config, biz_line, program_id)
        # 与读取同一条校验：需求键必须真的属于当前项目，才允许落盘。
        self._requirement_for_prototype(config, program_id, requirement_key)
        text = markdown if markdown.endswith("\n") or not markdown.strip() else markdown + "\n"
        document = write_outline_document(self.workspace, requirement_outline_path_of(requirement_key), text)
        identity = self._planning_identity(program_id, requirement_key)
        with self.lock:
            active = self.active_runs.get(identity)
        return {"programId": program_id, "requirementKey": requirement_key, **document, "active": active is not None}

    def requirement_prototype(
        self,
        program_id: int,
        requirement_key: str,
        biz_line: str = DEFAULT_BIZ_LINE,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        config = request_scoped_config(config, biz_line, program_id)
        requirement_prototype_directory_of(requirement_key)
        self._requirement_for_prototype(config, program_id, requirement_key)
        metadata = planner.request_api(
            config,
            "GET",
            "/delivery/requirement/prototype",
            query={"programId": program_id, "requirementKey": requirement_key},
        )
        metadata = metadata if isinstance(metadata, dict) else {}
        path, files = requirement_prototype_files(self.workspace, requirement_key)
        identity = self._requirement_prototype_identity(program_id, requirement_key)
        with self.lock:
            active = self.active_runs.get(identity)
        return {
            "requirementKey": requirement_key,
            "path": path,
            "exists": bool(files),
            "files": files,
            "generatedAt": str(metadata.get("generatedAt") or ""),
            "active": bool(active is not None and active.get("prototype")),
        }

    def _start_requirement_prototype(
        self,
        config: dict[str, Any],
        program_id: int,
        requirement_key: str,
        requirement: dict[str, Any],
        provider: str,
        model: str,
        reasoning_effort: str,
        fast_mode: bool,
        message: str = "",
        editing: bool = False,
        thread_id: str = "",
        previous_detail_digest: str = "",
    ) -> dict[str, Any]:
        identity = self._requirement_prototype_identity(program_id, requirement_key)
        detail_digest = planning_detail_digest(requirement)
        title = f"需求原型 · {str(requirement.get('name') or requirement_key).strip()}"[:120]
        client = factory.create_ai_client(
            provider,
            self.workspace,
            lambda event: self._publish_app_server_event(identity, event),
            codex_environment(config, program_id),
        )
        try:
            prompt = build_requirement_prototype_prompt(
                program_id, requirement, message, self.workspace, editing=editing,
                follow_up=bool(thread_id),
                include_detail=detail_digest != previous_detail_digest,
            )
            if thread_id:
                client.resume_thread(thread_id)
                turn_id = client.start_turn(
                    thread_id,
                    prompt,
                    request_id=client.next_request_id(),
                    model=model,
                    reasoning_effort=reasoning_effort,
                    fast_mode=fast_mode,
                )
            else:
                thread_id, turn_id = client.start_task(
                    title,
                    prompt,
                    model=model,
                    reasoning_effort=reasoning_effort,
                    fast_mode=fast_mode,
                )
            self._save_prototype_session(
                config, program_id, requirement_key, provider, thread_id, turn_id, title, "running", detail_digest,
            )
        except Exception:
            client.close()
            raise
        with self.lock:
            self.active.add(identity)
            self.active_runs[identity] = {
                "client": client,
                "threadId": thread_id,
                "turnId": turn_id,
                "prototype": True,
                "provider": provider,
                "config": config,
                "programId": program_id,
                "title": title,
            }
        self.progress.publish(identity, "status", "正在生成需求 HTML 原型" if not editing else "正在修改需求 HTML 原型", title, "running")
        threading.Thread(
            target=self._follow_requirement_prototype,
            args=(identity, client, config, program_id, requirement_key, provider, thread_id, turn_id, title, detail_digest),
            daemon=True,
        ).start()
        return {
            "accepted": True,
            "programId": program_id,
            "requirementKey": requirement_key,
            "threadId": thread_id,
            "turnId": turn_id,
            "active": True,
        }

    def generate_requirement_prototype(self, raw: Any, config: dict[str, Any] | None = None) -> dict[str, Any]:
        program_id, requirement_key, _message, _thread_id, provider, model = validate_requirement_prototype_payload(raw)
        config = request_scoped_config(config, biz_line_of(raw), program_id)
        requirement = self._requirement_for_prototype(config, program_id, requirement_key)
        if not bool(requirement.get("generatePrototype")):
            raise BridgeFailure("当前需求未启用 HTML 原型生成")
        identity = self._requirement_prototype_identity(program_id, requirement_key)
        with self.lock:
            if identity in self.active:
                raise BridgeFailure("该需求已有正在运行的原型会话，请稍后再试")
        return self._start_requirement_prototype(
            config,
            program_id,
            requirement_key,
            requirement,
            provider,
            model,
            reasoning_effort_of(raw, provider),
            fast_mode_of(raw, provider),
        )

    def requirement_prototype_conversation(
        self,
        program_id: int,
        requirement_key: str,
        thread_id: str = "",
        provider: str = "codex",
        biz_line: str = DEFAULT_BIZ_LINE,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        provider = ai_provider_of(provider)
        config = request_scoped_config(config, biz_line, program_id)
        requirement_prototype_directory_of(requirement_key)
        self._requirement_for_prototype(config, program_id, requirement_key)
        rows = self._prototype_session_rows(config, program_id, requirement_key, provider)
        known_thread_ids = {str(row.get("threadId") or "") for row in rows}
        if thread_id and thread_id not in known_thread_ids:
            raise BridgeFailure("所选原型编辑会话不存在")
        selected_thread_id = thread_id or str((rows[-1] if rows else {}).get("threadId") or "")
        identity = self._requirement_prototype_identity(program_id, requirement_key)
        with self.lock:
            active = self.active_runs.get(identity)
        if not selected_thread_id:
            return {"programId": program_id, "requirementKey": requirement_key, "threadId": "", "executorType": provider, "turns": [], "active": False, "activeTurnId": ""}
        # 线程正文在它自己那个执行器的缓存里：读跟着线程走，不跟当前选中的工具走。
        provider = executor_provider_of(
            next((row for row in rows if str(row.get("threadId") or "") == selected_thread_id), {}), provider,
        )
        live_client = active["client"] if active is not None and active.get("threadId") == selected_thread_id else None
        thread = self._read_thread_with_workspace_archive(
            live_client, selected_thread_id, "requirement", requirement_key, config, program_id,
            provider=provider,
            environment=codex_environment(config, program_id),
        )
        item_key = requirement_prototype_item_key(requirement_key)
        return with_usage({
            "programId": program_id,
            "requirementKey": requirement_key,
            "threadId": selected_thread_id,
            "executorType": provider,
            "turns": serialize_turns(
                thread.get("turns") or [],
                artifact_resolver=lambda paths: self.artifacts.register(config_biz_line(config), program_id, item_key, paths),
            ),
            "active": bool(active is not None and active.get("threadId") == selected_thread_id and active.get("prototype")),
            "activeTurnId": str((active or {}).get("turnId") or ""),
        })

    def send_requirement_prototype_message(self, raw: Any, config: dict[str, Any] | None = None) -> dict[str, Any]:
        program_id, requirement_key, message, requested_thread_id, provider, model = validate_requirement_prototype_payload(raw, message_required=True)
        config = request_scoped_config(config, biz_line_of(raw), program_id)
        requirement = self._requirement_for_prototype(config, program_id, requirement_key)
        identity = self._requirement_prototype_identity(program_id, requirement_key)
        with self.lock:
            active = self.active_runs.get(identity)
        if active is not None:
            if requested_thread_id and requested_thread_id != active.get("threadId"):
                raise BridgeFailure("该需求已有正在运行的原型会话，请稍后再试")
            active["client"].steer_turn(
                str(active["threadId"]), str(active["turnId"]), message, request_id=active["client"].next_request_id(),
            )
            return {"accepted": True, "programId": program_id, "requirementKey": requirement_key, "threadId": active["threadId"], "turnId": active["turnId"], "active": True}
        rows = self._prototype_session_rows(config, program_id, requirement_key, provider)
        known_thread_ids = {str(row.get("threadId") or "") for row in rows}
        if requested_thread_id and requested_thread_id not in known_thread_ids:
            raise BridgeFailure("所选原型编辑会话不存在")
        selected_thread_id = requested_thread_id or str((rows[-1] if rows else {}).get("threadId") or "")
        if selected_thread_id:
            # 续已有原型会话只能用这条线程自己的执行器。
            provider = executor_provider_of(
                next((row for row in rows if str(row.get("threadId") or "") == selected_thread_id), {}), provider,
            )
        return self._start_requirement_prototype(
            config,
            program_id,
            requirement_key,
            requirement,
            provider,
            model,
            reasoning_effort_of(raw, provider),
            fast_mode_of(raw, provider),
            message=message,
            editing=True,
            thread_id=selected_thread_id,
            previous_detail_digest=prototype_session_detail_digest(rows, selected_thread_id),
        )

    def _follow_requirement_prototype(
        self,
        identity: tuple[str, int, str],
        client: AppServerClient,
        config: dict[str, Any],
        program_id: int,
        requirement_key: str,
        provider: str,
        thread_id: str,
        turn_id: str,
        title: str,
        detail_digest: str = "",
    ) -> None:
        status = "failed"
        try:
            status = client.wait_turn(turn_id)
            self._archive_terminal_chat(
                client,
                config=config,
                program_id=program_id,
                resource_kind="requirement",
                resource_key=requirement_key,
                resource_name=title,
                conversation_title=title,
                thread_id=thread_id,
                provider=provider,
                phase="prototype",
                terminal_status=status,
            )
            if status == "completed":
                path, files = requirement_prototype_files(self.workspace, requirement_key)
                if not files:
                    raise BridgeFailure("未生成 HTML 原型文件")
                planner.request_api(
                    config,
                    "POST",
                    "/delivery/requirement/prototype/save",
                    body={"programId": program_id, "requirementKey": requirement_key, "path": path, "actorName": f"{provider}-http-bridge"},
                )
            self._save_prototype_session(config, program_id, requirement_key, provider, thread_id, turn_id, title, status, detail_digest)
            self.progress.publish(
                identity,
                "status",
                "需求 HTML 原型已更新" if status == "completed" else "需求 HTML 原型未完成",
                title,
                status,
            )
        except Exception as exc:
            status = "failed"
            try:
                self._save_prototype_session(config, program_id, requirement_key, provider, thread_id, turn_id, title, status, detail_digest)
            except Exception:
                pass
            self.progress.publish(identity, "error", "同步需求 HTML 原型失败", str(exc), status)
            print(f"同步需求 HTML 原型失败：{program_id}/{requirement_key}: {exc}", file=sys.stderr, flush=True)
        finally:
            client.close()
            with self.lock:
                current = self.active_runs.get(identity)
                if current is not None and current.get("client") is client:
                    self.active.discard(identity)
                    self._release_active_run(identity)

    def prototype_directory(
        self,
        program_id: int,
        item_key: str,
        biz_line: str = DEFAULT_BIZ_LINE,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return the absolute, task-scoped prototype directory when it has images."""
        config = request_scoped_config(config, biz_line, program_id)
        task = self._task_detail(config, program_id, item_key)
        if not bool(task.get("prototypeTask")):
            raise BridgeFailure("当前任务不是原型图生成任务")
        relative = Path(prototype_directory_of(task))
        if relative.is_absolute() or ".." in relative.parts:
            raise BridgeFailure("原型图目录无效")
        directory = (self.workspace / relative).resolve()
        try:
            directory.relative_to(self.workspace)
        except ValueError as exc:
            raise BridgeFailure("原型图目录超出当前项目") from exc
        image_count = 0
        if directory.is_dir():
            image_count = sum(
                1 for path in directory.rglob("*")
                if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES
            )
        return {"path": str(directory), "exists": image_count > 0, "imageCount": image_count}

    def open_prototype_directory(
        self,
        program_id: int,
        item_key: str,
        biz_line: str = DEFAULT_BIZ_LINE,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        directory = self.prototype_directory(program_id, item_key, biz_line=biz_line, config=config)
        if not directory["exists"]:
            raise BridgeFailure("原型图尚未生成，暂时不能打开目录")
        opener = shutil.which("open")
        if not opener:
            raise BridgeFailure("当前系统不支持打开本机原型图目录")
        try:
            subprocess.Popen([opener, directory["path"]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as exc:
            raise BridgeFailure(f"打开原型图目录失败：{exc}") from exc
        return directory

    def reveal_workspace_file(self, raw_path: str) -> dict[str, Any]:
        """在本机文件管理器里定位工作区中的一个文件。

        路径一律按工作区相对路径解析，解析结果必须仍落在工作区内：面板传来的字符串
        不能成为读取工作区之外任意路径的入口。桥接跑在用户自己的机器上，所以这里只是
        唤起文件管理器，不读文件内容。
        """
        candidate = Path(str(raw_path or "").strip())
        if not candidate.parts:
            raise BridgeFailure("缺少文件路径")
        resolved = candidate.resolve() if candidate.is_absolute() else (self.workspace / candidate).resolve()
        try:
            relative = resolved.relative_to(self.workspace)
        except ValueError as exc:
            raise BridgeFailure("文件路径超出当前项目") from exc
        if not resolved.exists():
            raise BridgeFailure("文件不存在或已被移动")
        directory = resolved if resolved.is_dir() else resolved.parent
        if sys.platform == "darwin":
            opener = shutil.which("open")
            # -R 是「显示并选中」，只打开目录会让用户在一堆文件里自己找。
            command = [opener, "-R", str(resolved)] if opener else []
        elif sys.platform == "win32":
            explorer = shutil.which("explorer")
            command = [explorer, f"/select,{resolved}"] if explorer else []
        else:
            # Linux 没有统一的「选中某个文件」协议，退一步打开所在目录。
            opener = shutil.which("xdg-open")
            command = [opener, str(directory)] if opener else []
        if not command:
            raise BridgeFailure("当前系统不支持打开本机文件目录")
        try:
            subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as exc:
            raise BridgeFailure(f"打开文件所在目录失败：{exc}") from exc
        return {
            "path": str(resolved),
            "directory": str(directory),
            "relativePath": relative.as_posix(),
        }
