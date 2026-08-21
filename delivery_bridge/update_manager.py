"""Versioned, observable updater for the local bridge and both AI clients.

The browser may request an update, but it never uploads executable files. This
module resolves one immutable Git commit from the configured repository,
downloads that archive itself, validates the package and then refreshes the
bridge source plus the Codex and Claude installation caches.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote
from urllib.request import Request, urlopen

from .versioning import compare_versions, manifest_version, semver_parts


MAX_ARCHIVE_BYTES = 80 * 1024 * 1024
MAX_EXTRACTED_BYTES = 180 * 1024 * 1024
MAX_ARCHIVE_FILES = 4000
UPDATE_TERMINAL_STATES = {"completed", "failed", "restart_required"}
RESTART_STALE_SECONDS = 45
PACKAGE_EXCLUDES = {".git", "__pycache__", ".pytest_cache", ".DS_Store"}


class UpdateFailure(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def atomic_json_write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def directory_digest(root: Path, suffix: str = ".py") -> str:
    digest = hashlib.sha256()
    if not root.exists():
        return ""
    for path in sorted(root.rglob(f"*{suffix}")):
        if not path.is_file() or any(part in PACKAGE_EXCLUDES for part in path.parts):
            continue
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def copy_package(source: Path, destination: Path) -> None:
    """Copy a validated package without development caches or repository data."""
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(
        source,
        destination,
        ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc", ".pytest_cache", ".DS_Store"),
    )


def replace_package(source: Path, destination: Path) -> None:
    """Replace package-owned entries while keeping an enclosing checkout intact."""
    destination.mkdir(parents=True, exist_ok=True)
    source_names = {path.name for path in source.iterdir() if path.name not in PACKAGE_EXCLUDES}
    for current in destination.iterdir():
        if current.name in PACKAGE_EXCLUDES or current.name == ".git":
            continue
        if current.name not in source_names:
            if current.is_dir() and not current.is_symlink():
                shutil.rmtree(current)
            else:
                current.unlink(missing_ok=True)
    for item in source.iterdir():
        if item.name in PACKAGE_EXCLUDES:
            continue
        target = destination / item.name
        temporary = destination / f".{item.name}.update-{uuid.uuid4().hex[:8]}"
        if item.is_dir():
            copy_package(item, temporary)
        else:
            shutil.copy2(item, temporary)
        if target.exists() or target.is_symlink():
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink(missing_ok=True)
        os.replace(temporary, target)


class PluginUpdateManager:
    def __init__(
        self,
        plugin_root: Path,
        runtime_dir: Path,
        repository: str,
        raw_base_url: str,
        cache_seconds: int = 60,
        home_dir: Path | None = None,
    ) -> None:
        self.plugin_root = plugin_root.resolve()
        self.runtime_dir = runtime_dir.resolve()
        self.repository = repository
        self.raw_base_url = raw_base_url.rstrip("/")
        self.cache_seconds = cache_seconds
        self.home_dir = (home_dir or Path.home()).resolve()
        self.state_path = self.runtime_dir / "plugin-update.json"
        self.download_dir = self.runtime_dir / "updates"
        self.backup_dir = self.runtime_dir / "plugin-backups"
        self.lock = threading.RLock()
        self.remote_cache: tuple[float, dict[str, str]] | None = None
        self.job = self._load_job()

    def _load_job(self) -> dict[str, Any] | None:
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(value, dict):
            return None
        if value.get("status") == "restarting":
            value.update({
                "status": "completed",
                "progress": 100,
                "restartRequired": False,
                "finishedAt": utc_now(),
                "message": "桥接服务已使用新版本重新启动。",
            })
            logs = value.get("logs") if isinstance(value.get("logs"), list) else []
            logs.append({"at": utc_now(), "level": "success", "message": "桥接服务重启完成。"})
            value["logs"] = logs[-400:]
            atomic_json_write(self.state_path, value)
        elif value.get("status") in {"resolving", "downloading", "validating", "installing"}:
            value.update({
                "status": "failed",
                "finishedAt": utc_now(),
                "message": "更新过程因桥接服务退出而中断，请重新执行更新。",
            })
            atomic_json_write(self.state_path, value)
        return value

    def installed_version(self) -> str:
        try:
            return manifest_version(self.plugin_root / ".codex-plugin" / "plugin.json")
        except ValueError as exc:
            raise UpdateFailure(str(exc)) from exc

    def _resolve_remote(self, force: bool = False) -> dict[str, str]:
        now = time.monotonic()
        with self.lock:
            if not force and self.remote_cache and now - self.remote_cache[0] < self.cache_seconds:
                return dict(self.remote_cache[1])
        branch = "main"
        commit = ""
        try:
            completed = subprocess.run(
                ["git", "ls-remote", "--symref", self.repository, "HEAD"],
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
            branch_match = re.search(r"^ref:\s+refs/heads/(.+?)\s+HEAD$", completed.stdout, re.MULTILINE)
            commit_match = re.search(r"^([0-9a-f]{40})\s+HEAD$", completed.stdout, re.MULTILINE)
            if branch_match:
                branch = branch_match.group(1)
            if commit_match:
                commit = commit_match.group(1)
        except (OSError, subprocess.SubprocessError):
            pass
        revision = commit or branch
        manifest_url = f"{self.raw_base_url}/{quote(revision, safe='')}/.codex-plugin/plugin.json"
        request = Request(manifest_url, headers={"User-Agent": "delivery-task-planner-updater"})
        try:
            with urlopen(request, timeout=10) as response:
                manifest = json.loads(response.read().decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UpdateFailure(f"无法读取 Git 仓库中的插件版本：{exc}") from exc
        version = str(manifest.get("version") or "").strip() if isinstance(manifest, dict) else ""
        if not version:
            raise UpdateFailure("Git 仓库中的插件版本信息为空")
        result = {"branch": branch, "commit": commit, "revision": revision, "version": version}
        with self.lock:
            self.remote_cache = (now, result)
        return dict(result)

    def status(self, force: bool = False) -> dict[str, Any]:
        checked_at = int(time.time())
        try:
            local_version = self.installed_version()
        except UpdateFailure as exc:
            return self._status_payload("", {}, False, checked_at, str(exc))
        try:
            remote = self._resolve_remote(force=force)
            update_available = compare_versions(remote["version"], local_version) > 0
        except (UpdateFailure, ValueError) as exc:
            return self._status_payload(local_version, {}, False, checked_at, str(exc))
        return self._status_payload(local_version, remote, update_available, checked_at, "")

    def _status_payload(
        self,
        local_version: str,
        remote: dict[str, str],
        update_available: bool,
        checked_at: int,
        message: str,
    ) -> dict[str, Any]:
        with self.lock:
            job = dict(self.job) if self.job else None
            if job and isinstance(job.get("logs"), list):
                job["logs"] = list(job["logs"])
        return {
            "localVersion": local_version,
            "remoteVersion": remote.get("version", ""),
            "remoteCommit": remote.get("commit", ""),
            "updateAvailable": update_available,
            "checkedAt": checked_at,
            "message": message,
            "installation": job,
        }

    def _save_job(self) -> None:
        if self.job:
            atomic_json_write(self.state_path, self.job)

    def _patch_job(self, **values: Any) -> None:
        with self.lock:
            if not self.job:
                return
            self.job.update(values)
            self._save_job()

    def _log(self, message: str, level: str = "info") -> None:
        with self.lock:
            if not self.job:
                return
            logs = self.job.setdefault("logs", [])
            logs.append({"at": utc_now(), "level": level, "message": message})
            self.job["logs"] = logs[-400:]
            self._save_job()

    def start(self, expected_version: str = "") -> dict[str, Any]:
        with self.lock:
            if self.job and self.job.get("status") not in UPDATE_TERMINAL_STATES:
                raise UpdateFailure("已有插件更新正在执行")
            job_id = uuid.uuid4().hex
            self.job = {
                "jobId": job_id,
                "status": "resolving",
                "progress": 2,
                "localVersion": self.installed_version(),
                "targetVersion": expected_version,
                "commit": "",
                "startedAt": utc_now(),
                "finishedAt": "",
                "message": "正在解析远程发布版本。",
                "restartRequired": False,
                "components": [],
                "logs": [],
            }
            self._save_job()
        threading.Thread(target=self._install, args=(job_id, expected_version), daemon=True).start()
        return self.get_job(job_id)

    def get_job(self, job_id: str = "") -> dict[str, Any]:
        with self.lock:
            if not self.job or (job_id and self.job.get("jobId") != job_id):
                raise UpdateFailure("未找到插件更新记录")
            self._recover_stale_restart()
            return json.loads(json.dumps(self.job, ensure_ascii=False))

    def _recover_stale_restart(self) -> None:
        if not self.job or self.job.get("status") != "restarting":
            return
        requested_at = str(self.job.get("restartRequestedAt") or "")
        if not requested_at:
            logs = self.job.get("logs") if isinstance(self.job.get("logs"), list) else []
            requested_at = str((logs[-1] if logs else {}).get("at") or self.job.get("startedAt") or "")
        try:
            requested = datetime.fromisoformat(requested_at.replace("Z", "+00:00"))
        except ValueError:
            return
        if (datetime.now(timezone.utc) - requested).total_seconds() < RESTART_STALE_SECONDS:
            return
        self.job.update({
            "status": "restart_required",
            "progress": 96,
            "message": "上次自动重启未完成，正在重新尝试。",
            "restartRequired": True,
            "restartRequestedAt": "",
        })
        logs = self.job.setdefault("logs", [])
        logs.append({"at": utc_now(), "level": "warning", "message": "自动重启等待超时，已恢复为可重试状态。"})
        self.job["logs"] = logs[-400:]
        self._save_job()

    def mark_restarting(self, job_id: str) -> dict[str, Any]:
        with self.lock:
            if not self.job or self.job.get("jobId") != job_id:
                raise UpdateFailure("未找到插件更新记录")
            if self.job.get("status") != "restart_required":
                raise UpdateFailure("当前更新不需要重启桥接服务")
            self.job.update({
                "status": "restarting",
                "progress": 98,
                "message": "正在重启桥接服务。",
                "restartRequestedAt": utc_now(),
            })
            self._log("安装结果已落盘，开始重启桥接服务。")
            return self.get_job(job_id)

    def _install(self, job_id: str, expected_version: str) -> None:
        backup: Path | None = None
        try:
            remote = self._resolve_remote(force=True)
            if not remote["commit"]:
                raise UpdateFailure("无法锁定 GitHub 提交，请确认本机 Git 可用后重试")
            if expected_version and compare_versions(remote["version"], expected_version) != 0:
                raise UpdateFailure(f"远程版本已经变化：期望 {expected_version}，当前 {remote['version']}")
            if compare_versions(remote["version"], self.installed_version()) <= 0:
                raise UpdateFailure("远程版本不高于当前安装版本")
            self._patch_job(targetVersion=remote["version"], commit=remote["commit"])
            self._log(f"已锁定发布版本 {remote['version']}（{remote['revision'][:12]}）。")

            self._patch_job(status="downloading", progress=10, message="正在下载固定提交的发布包。")
            archive = self._download_archive(job_id, remote["revision"])
            self._log(f"发布包下载完成，SHA-256 {self._sha256(archive)[:16]}…")

            self._patch_job(status="validating", progress=28, message="正在校验发布包结构和版本。")
            staged = self._extract_archive(job_id, archive)
            self._validate_package(staged, remote["version"])
            self._log("发布包结构、插件名称和双端版本校验通过。", "success")

            old_python_digest = directory_digest(self.plugin_root)
            new_python_digest = directory_digest(staged)
            backup = self.backup_dir / f"{int(time.time())}-{self.installed_version().replace('/', '_')}"
            backup.parent.mkdir(parents=True, exist_ok=True)
            copy_package(self.plugin_root, backup)
            self._log(f"当前版本已备份到 {backup}。")

            self._patch_job(status="installing", progress=42, message="正在替换桥接运行文件。")
            replace_package(staged, self.plugin_root)
            components = ["bridge", "skills"]
            self._log(f"桥接源码和 Skills 已更新到 {remote['version']}。", "success")

            self._patch_job(progress=58, message="正在刷新 Codex 插件安装缓存。")
            codex_result = self._install_codex(staged)
            if codex_result:
                components.append("codex")

            self._patch_job(progress=76, message="正在刷新 Claude Code 插件缓存。")
            claude_result = self._install_claude_cache(staged, remote["commit"])
            if claude_result:
                components.append("claude")

            restart_required = old_python_digest != new_python_digest
            final_status = "restart_required" if restart_required else "completed"
            final_message = (
                "插件与双端缓存已更新，请重启桥接服务完成 Python 代码切换。"
                if restart_required
                else "插件与双端缓存更新完成，新建会话即可使用。"
            )
            self._patch_job(
                status=final_status,
                progress=96 if restart_required else 100,
                message=final_message,
                restartRequired=restart_required,
                components=components,
                finishedAt="" if restart_required else utc_now(),
            )
            self._log(final_message, "success")
            with self.lock:
                self.remote_cache = None
        except Exception as exc:
            if backup and backup.exists():
                try:
                    replace_package(backup, self.plugin_root)
                    self._log("安装失败，桥接运行文件已回滚到更新前版本。", "warning")
                except Exception as rollback_exc:
                    self._log(f"自动回滚失败：{rollback_exc}", "error")
            self._patch_job(
                status="failed",
                finishedAt=utc_now(),
                message=str(exc),
                restartRequired=False,
            )
            self._log(f"更新失败：{exc}", "error")

    def _download_archive(self, job_id: str, revision: str) -> Path:
        target_dir = self.download_dir / job_id
        if target_dir.exists():
            shutil.rmtree(target_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        archive = target_dir / "release.zip"
        repository_web = self.repository.removesuffix(".git")
        request = Request(
            f"{repository_web}/archive/{quote(revision, safe='')}.zip",
            headers={"User-Agent": "delivery-task-planner-updater"},
        )
        total = 0
        try:
            with urlopen(request, timeout=30) as response, archive.open("wb") as output:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_ARCHIVE_BYTES:
                        raise UpdateFailure("发布包超过允许的大小")
                    output.write(chunk)
        except OSError as exc:
            raise UpdateFailure(f"下载发布包失败：{exc}") from exc
        return archive

    def _extract_archive(self, job_id: str, archive: Path) -> Path:
        extract_root = self.download_dir / job_id / "extracted"
        extract_root.mkdir(parents=True, exist_ok=True)
        total = 0
        try:
            with zipfile.ZipFile(archive) as bundle:
                entries = bundle.infolist()
                if len(entries) > MAX_ARCHIVE_FILES:
                    raise UpdateFailure("发布包文件数量超过限制")
                for entry in entries:
                    path = PurePosixPath(entry.filename)
                    if path.is_absolute() or ".." in path.parts:
                        raise UpdateFailure("发布包包含不安全路径")
                    mode = entry.external_attr >> 16
                    if mode & 0o170000 == 0o120000:
                        raise UpdateFailure("发布包不允许包含符号链接")
                    total += entry.file_size
                    if total > MAX_EXTRACTED_BYTES:
                        raise UpdateFailure("发布包解压后超过允许的大小")
                bundle.extractall(extract_root)
        except (OSError, zipfile.BadZipFile) as exc:
            raise UpdateFailure(f"发布包解压失败：{exc}") from exc
        candidates = [path for path in extract_root.iterdir() if path.is_dir()]
        if len(candidates) != 1:
            raise UpdateFailure("发布包根目录结构无效")
        return candidates[0]

    @staticmethod
    def _validate_package(root: Path, expected_version: str) -> None:
        required = [
            root / ".codex-plugin" / "plugin.json",
            root / ".claude-plugin" / "plugin.json",
            root / "skills",
            root / "http_bridge.py",
            root / "server.py",
            root / "taskboard.py",
            root / "delivery_bridge" / "update_manager.py",
        ]
        if any(not path.exists() for path in required):
            raise UpdateFailure("发布包缺少插件清单、Skills 或桥接核心代码")
        for manifest_path in required[:2]:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if str(manifest.get("name") or "") != "delivery-task-planner":
                raise UpdateFailure(f"插件名称校验失败：{manifest_path}")
            version = str(manifest.get("version") or "")
            if semver_parts(version)[0] != semver_parts(expected_version)[0]:
                raise UpdateFailure(f"Codex 与 Claude 插件发布版本不一致：{version} / {expected_version}")

    def _run(self, command: list[str], label: str, timeout: int = 120) -> None:
        self._log(f"执行 {label}：{' '.join(command[:3])}")
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError as exc:
            raise UpdateFailure(f"无法执行 {label}：{exc}") from exc
        deadline = time.monotonic() + timeout
        assert process.stdout is not None
        output: queue.Queue[str] = queue.Queue()

        def read_output() -> None:
            assert process.stdout is not None
            for item in process.stdout:
                output.put(item)

        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        while process.poll() is None:
            try:
                line = output.get(timeout=0.2)
                if line.strip():
                    self._log(f"[{label}] {line.rstrip()}")
            except queue.Empty:
                pass
            if time.monotonic() > deadline:
                process.kill()
                raise UpdateFailure(f"{label}执行超时")
        reader.join(timeout=1)
        while not output.empty():
            line = output.get_nowait()
            if line.strip():
                self._log(f"[{label}] {line.rstrip()}")
        if process.returncode != 0:
            raise UpdateFailure(f"{label}失败，退出码 {process.returncode}")
        self._log(f"{label}完成。", "success")

    def _codex_command(self) -> str:
        command = shutil.which("codex")
        if command:
            return command
        cached = self.home_dir / ".local" / "state" / "delivery-task-planner" / "bin" / "codex"
        if cached.is_file() and os.access(cached, os.X_OK):
            return str(cached)
        for app in ("Codex.app", "ChatGPT.app"):
            candidate = Path("/Applications") / app / "Contents" / "Resources" / "codex"
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
        return ""

    def _install_codex(self, staged: Path) -> bool:
        command = self._codex_command()
        marketplace = self.home_dir / ".agents" / "plugins" / "marketplace.json"
        if not command or not marketplace.exists():
            self._log("未找到 Codex CLI 或个人插件市场，已跳过 Codex 缓存刷新。", "warning")
            return False
        try:
            catalog = json.loads(marketplace.read_text(encoding="utf-8"))
            entry = next(item for item in catalog.get("plugins", []) if item.get("name") == "delivery-task-planner")
            source = entry.get("source")
            source_path = source.get("path") if isinstance(source, dict) else source
            if not isinstance(source_path, str) or not source_path.startswith("./"):
                raise ValueError("个人插件市场中的 source.path 无效")
            marketplace_root = marketplace.parents[2]
            codex_source = (marketplace_root / source_path[2:]).resolve()
        except (OSError, ValueError, StopIteration, json.JSONDecodeError) as exc:
            raise UpdateFailure(f"无法解析 Codex 插件安装位置：{exc}") from exc
        if codex_source != self.plugin_root:
            replace_package(staged, codex_source)
            self._log(f"Codex 插件源已同步到 {codex_source}。")
        marketplace_name = str(catalog.get("name") or "").strip()
        if not marketplace_name:
            raise UpdateFailure("Codex 个人插件市场名称为空")
        self._run([command, "plugin", "add", f"delivery-task-planner@{marketplace_name}"], "Codex 插件安装")
        return True

    def _install_claude_cache(self, staged: Path, commit: str) -> bool:
        metadata_path = self.home_dir / ".claude" / "plugins" / "installed_plugins.json"
        if not metadata_path.exists():
            self._log("未发现 Claude Code 插件安装记录，已跳过 Claude 缓存刷新。", "warning")
            return False
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise UpdateFailure(f"无法读取 Claude 插件安装记录：{exc}") from exc
        plugins = metadata.get("plugins") if isinstance(metadata, dict) else None
        if not isinstance(plugins, dict):
            raise UpdateFailure("Claude 插件安装记录格式无效")
        matches = [(key, value) for key, value in plugins.items() if key.split("@", 1)[0] == "delivery-task-planner"]
        if not matches:
            self._log("Claude Code 尚未安装 delivery-task-planner，已跳过缓存刷新。", "warning")
            return False
        claude_version = manifest_version(staged / ".claude-plugin" / "plugin.json")
        changed = False
        for plugin_key, installations in matches:
            if not isinstance(installations, list):
                continue
            marketplace_name = plugin_key.split("@", 1)[1] if "@" in plugin_key else "local"
            target = self.home_dir / ".claude" / "plugins" / "cache" / marketplace_name / "delivery-task-planner" / claude_version
            temporary = target.with_name(f".{target.name}.update-{uuid.uuid4().hex[:8]}")
            temporary.parent.mkdir(parents=True, exist_ok=True)
            copy_package(staged, temporary)
            if target.exists():
                shutil.rmtree(target)
            os.replace(temporary, target)
            for installation in installations:
                if not isinstance(installation, dict):
                    continue
                installation.update({
                    "installPath": str(target),
                    "version": claude_version,
                    "lastUpdated": utc_now(),
                })
                if commit:
                    installation["gitCommitSha"] = commit
                changed = True
            self._log(f"Claude Code 缓存已安装到 {target}。", "success")
        if changed:
            backup = metadata_path.with_suffix(f".json.backup-{int(time.time())}")
            shutil.copy2(metadata_path, backup)
            atomic_json_write(metadata_path, metadata)
            self._log("Claude Code 安装索引已原子更新；新窗口将加载新版本。", "success")
        return changed

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
