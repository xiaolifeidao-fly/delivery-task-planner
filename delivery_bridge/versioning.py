"""Version parsing shared by the bridge and the self-update runtime."""

from __future__ import annotations

import json
import re
from pathlib import Path


VERSION_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)


def manifest_version(path: Path) -> str:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取插件版本信息：{exc}") from exc
    version = str(manifest.get("version") or "").strip() if isinstance(manifest, dict) else ""
    if not version:
        raise ValueError("插件版本信息为空")
    return version


def semver_parts(value: str) -> tuple[tuple[int, int, int], tuple[str, ...] | None]:
    """Parse SemVer while deliberately ignoring build metadata."""
    normalized = str(value or "").strip().lstrip("v").split("+", 1)[0]
    match = VERSION_RE.fullmatch(normalized)
    if not match:
        raise ValueError(f"无效的插件版本号：{value}")
    release = tuple(int(match.group(index)) for index in range(1, 4))
    pre_release = tuple(match.group(4).split(".")) if match.group(4) else None
    return release, pre_release


def compare_versions(left: str, right: str) -> int:
    """Return a positive value when ``left`` is newer than ``right``."""
    left_release, left_pre_release = semver_parts(left)
    right_release, right_pre_release = semver_parts(right)
    if left_release != right_release:
        return 1 if left_release > right_release else -1
    if left_pre_release is None and right_pre_release is None:
        return 0
    if left_pre_release is None:
        return 1
    if right_pre_release is None:
        return -1
    for left_identifier, right_identifier in zip(left_pre_release, right_pre_release):
        if left_identifier == right_identifier:
            continue
        left_numeric = left_identifier.isdigit()
        right_numeric = right_identifier.isdigit()
        if left_numeric and right_numeric:
            return 1 if int(left_identifier) > int(right_identifier) else -1
        if left_numeric != right_numeric:
            return -1 if left_numeric else 1
        return 1 if left_identifier > right_identifier else -1
    if len(left_pre_release) == len(right_pre_release):
        return 0
    return 1 if len(left_pre_release) > len(right_pre_release) else -1
