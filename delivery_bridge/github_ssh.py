"""本机 GitHub SSH 密钥的探测与配置。

推分支要走 SSH，但用户机器上大多没有配好 key。这里只做三件事：看现状、
按需生成一把插件专用的 ed25519 密钥、把 ~/.ssh/config 里属于本插件的那一段
用成对标记圈起来改写——绝不碰标记之外的任何配置。
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .errors import BridgeFailure


GITHUB_SSH_HOST = "github.com"


GITHUB_SSH_KEY_NAME = "id_ed25519_github_delivery_task_planner"


GITHUB_SSH_CONFIG_START = "# >>> delivery-task-planner GitHub SSH key >>>"


GITHUB_SSH_CONFIG_END = "# <<< delivery-task-planner GitHub SSH key <<<"


GITHUB_SSH_CONFIG_BLOCK_RE = re.compile(
    rf"(?ms)^{re.escape(GITHUB_SSH_CONFIG_START)}\n.*?^{re.escape(GITHUB_SSH_CONFIG_END)}\n?",
)


SSH_PUBLIC_KEY_RE = re.compile(
    r"^(?:ssh-(?:ed25519|rsa|dss)|ecdsa-sha2-nistp(?:256|384|521)|sk-(?:ssh-ed25519|ecdsa-sha2-nistp256)@openssh\\.com)\s+[A-Za-z0-9+/=]+(?:\s+.*)?$",
)


def github_ssh_paths(home: Path | None = None) -> tuple[Path, Path]:
    root = (home or Path.home()).expanduser()
    ssh_directory = root / ".ssh"
    return ssh_directory, ssh_directory / "config"


def github_identity_files(config_path: Path, home: Path) -> list[Path]:
    """Read only `Host github.com` identity entries from the user's SSH config.

    The UI only claims that a GitHub key is ready when its corresponding public
    key is present. We intentionally do not inspect private-key contents.
    """
    try:
        lines = config_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    identities: list[Path] = []
    host_matches = False
    for line in lines:
        try:
            parts = shlex.split(line, comments=True, posix=True)
        except ValueError:
            continue
        if len(parts) < 2:
            continue
        option = parts[0].lower()
        if option == "host":
            host_matches = any(item.casefold() == GITHUB_SSH_HOST for item in parts[1:])
            continue
        if option != "identityfile" or not host_matches:
            continue
        raw_path = parts[1].replace("%d", str(home)).replace("%h", GITHUB_SSH_HOST)
        if raw_path == "~":
            candidate = home
        elif raw_path.startswith("~/"):
            candidate = home / raw_path[2:]
        else:
            candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            candidate = home / ".ssh" / candidate
        resolved = candidate.resolve(strict=False)
        if resolved not in identities:
            identities.append(resolved)
    return identities


def public_key_from_file(path: Path) -> str:
    try:
        if path.stat().st_size > 16 * 1024:
            return ""
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return ""
    for line in lines:
        candidate = line.strip()
        if candidate and not candidate.startswith("#"):
            return candidate if SSH_PUBLIC_KEY_RE.fullmatch(candidate) else ""
    return ""


def github_ssh_key_status(home: Path | None = None) -> dict[str, Any]:
    """Return only public, display-safe GitHub SSH state for the environment UI."""
    root = (home or Path.home()).expanduser()
    _, config_path = github_ssh_paths(root)
    result = {
        "githubSshConfigured": False,
        "githubSshPublicKey": "",
        "githubSshError": "",
    }
    for identity_path in github_identity_files(config_path, root):
        public_key = public_key_from_file(identity_path.with_name(f"{identity_path.name}.pub"))
        if public_key:
            result.update({"githubSshConfigured": True, "githubSshPublicKey": public_key})
            return result
    return result


def write_github_ssh_config(config_path: Path, home: Path, identity_path: Path) -> None:
    if config_path.exists() and config_path.is_symlink():
        raise BridgeFailure("SSH 配置文件是符号链接，未自动修改；请先手动配置 GitHub 密钥")
    try:
        existing = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    except (OSError, UnicodeDecodeError) as exc:
        raise BridgeFailure(f"无法读取 SSH 配置文件：{exc}") from exc
    relative_identity = identity_path.relative_to(home)
    managed_block = "\n".join((
        GITHUB_SSH_CONFIG_START,
        f"Host {GITHUB_SSH_HOST}",
        f"  HostName {GITHUB_SSH_HOST}",
        "  User git",
        f"  IdentityFile ~/{relative_identity}",
        "  IdentitiesOnly yes",
        GITHUB_SSH_CONFIG_END,
        "",
    ))
    content = GITHUB_SSH_CONFIG_BLOCK_RE.sub("", existing).lstrip()
    temporary = config_path.with_name(f".{config_path.name}.delivery-task-planner.tmp")
    try:
        temporary.write_text(managed_block + content, encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, config_path)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise BridgeFailure(f"无法写入 SSH 配置文件：{exc}") from exc


def ensure_github_ssh_key(home: Path | None = None) -> dict[str, Any]:
    """Create a managed GitHub key only when no configured public key is usable."""
    root = (home or Path.home()).expanduser()
    current = github_ssh_key_status(root)
    if current["githubSshConfigured"]:
        return current
    ssh_directory, config_path = github_ssh_paths(root)
    try:
        ssh_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(ssh_directory, 0o700)
    except OSError as exc:
        current["githubSshError"] = f"无法创建 SSH 目录：{exc}"
        return current
    private_key = ssh_directory / GITHUB_SSH_KEY_NAME
    public_key = private_key.with_name(f"{private_key.name}.pub")
    ssh_keygen = shutil.which("ssh-keygen")
    if not ssh_keygen:
        current["githubSshError"] = "未找到 ssh-keygen；请先完成 Git 安装后重新预设"
        return current
    try:
        if not private_key.exists():
            generated = subprocess.run(
                [ssh_keygen, "-q", "-t", "ed25519", "-f", str(private_key), "-N", "", "-C", "delivery-task-planner-github"],
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=10,
            )
            if generated.returncode != 0:
                current["githubSshError"] = f"GitHub SSH 密钥生成失败：{(generated.stdout or '').strip() or 'ssh-keygen 退出异常'}"
                return current
        elif not public_key_from_file(public_key):
            recovered = subprocess.run(
                [ssh_keygen, "-y", "-f", str(private_key)],
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=10,
            )
            recovered_key = (recovered.stdout or "").strip()
            if recovered.returncode != 0 or not SSH_PUBLIC_KEY_RE.fullmatch(recovered_key):
                current["githubSshError"] = "已有 GitHub 密钥无法恢复公钥，未覆盖原有文件"
                return current
            public_key.write_text(f"{recovered_key}\n", encoding="utf-8")
        os.chmod(private_key, 0o600)
        os.chmod(public_key, 0o644)
        write_github_ssh_config(config_path, root, private_key)
    except (BridgeFailure, OSError, subprocess.SubprocessError) as exc:
        current["githubSshError"] = str(exc)
        return current
    configured = github_ssh_key_status(root)
    if not configured["githubSshConfigured"]:
        configured["githubSshError"] = "GitHub SSH 密钥已生成，但未能完成配置校验"
    return configured
