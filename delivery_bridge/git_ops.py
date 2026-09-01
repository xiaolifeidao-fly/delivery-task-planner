"""需求分支的本机 Git 操作。

面板只记录关联结果，真正的 Git 命令全部在本机工作目录里执行。命令参数一律固定，
不拼接用户输入到 shell；分支名先做白名单校验再交给 Git。

这里只放对 Git 仓库本身的读写（分支、变更、合并、子模组、子工程），
不涉及任务面板的会话状态——那部分在 ExecutionBridge 里。
"""

from __future__ import annotations

import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from .errors import BridgeFailure
from .prompt_context import workspace_instruction, wrap_bridge_context
from .workspaces import workspace_path_of

# ---------------------------------------------------------------------------
# 需求分支：面板只记录关联结果，真正的 Git 命令全部在本机工作目录里执行。
# 命令参数一律固定，不拼接用户输入到 shell；分支名先做白名单校验再交给 Git。
# ---------------------------------------------------------------------------

# git check-ref-format 明确禁止的字符，外加空白和 ASCII 控制字符。
# 别再自己另立一套更窄的白名单：仓库里真实存在 feature/issue#duokai 这种合法分支，
# 白名单挡掉它们之后，提交推送这些工程会直接失败在「分支名不合法」上。
GIT_BRANCH_FORBIDDEN_RE = re.compile(r"[\x00-\x20\x7f~^:?*\[\\]")
GIT_REMOTE_PREFIX = "remotes/"
GIT_REMOTE_NAME_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")
# 关联远端仓库时只接受这几种常见写法，挡掉以 - 开头会被 git 当成选项的输入。
GIT_REPOSITORY_URL_RE = re.compile(r"(?:[A-Za-z0-9._-]+@[A-Za-z0-9._-]+:[A-Za-z0-9._~/-]+|(?:ssh|git|https|http)://[A-Za-z0-9._~@:/-]+)")


def valid_git_branch_name(value: str) -> bool:
    """挡掉明显非法的分支名。最终仍由 git check-ref-format 判定，这里只做前置过滤。

    规则对齐 git 自己的 check-ref-format：只挡它禁止的字符和形态，不额外收窄。
    额外挡掉以 - 开头的名字——命令参数是按列表传的，不过 git 会把它当成选项。
    """
    name = str(value or "").strip()
    if not name or len(name) > 255 or name == "@":
        return False
    if GIT_BRANCH_FORBIDDEN_RE.search(name):
        return False
    if name.startswith(("-", "/", ".")) or name.endswith(("/", ".", ".lock")):
        return False
    return ".." not in name and "//" not in name and "@{" not in name


def valid_git_remote_name(value: str) -> bool:
    return bool(GIT_REMOTE_NAME_RE.fullmatch(str(value or "").strip()))


def run_git(workspace: Path, args: list[str], timeout: int = 20) -> subprocess.CompletedProcess:
    """在项目工作目录里执行一条只带固定参数的 Git 命令。"""
    try:
        return subprocess.run(
            ["git", "-C", str(workspace), *args],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise BridgeFailure("本机未安装 Git，请先在环境预设中完成安装") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise BridgeFailure(f"执行 Git 命令失败：{exc}") from exc


def git_output(workspace: Path, args: list[str], failure: str, timeout: int = 20) -> str:
    completed = run_git(workspace, args, timeout=timeout)
    if completed.returncode != 0:
        raise BridgeFailure(f"{failure}：{(completed.stdout or '').strip() or 'git 退出异常'}")
    return (completed.stdout or "").strip()


def git_workspace_probe(workspace: Path) -> tuple[bool, str]:
    """判断目录是否落在某个 Git 工作树里，同时把 git 原文带回去用于报错。"""
    completed = run_git(workspace, ["rev-parse", "--is-inside-work-tree"])
    output = (completed.stdout or "").strip()
    # run_git 把 stderr 并进了 stdout，git 的 warning/hint 会混在结果前面，判定只认最后一行。
    verdict = output.splitlines()[-1].strip() if output else ""
    return (completed.returncode == 0 and verdict == "true"), (output or "git 退出异常")


def require_git_workspace(workspace: Path) -> None:
    inside, detail = git_workspace_probe(workspace)
    if not inside:
        # 带上 git 原文，否则「不是仓库」和「仓库归属存疑」「HOME 不可读」在前端长得一模一样。
        raise BridgeFailure(f"项目工作目录不是 Git 仓库：{workspace}（git: {detail}）")


def git_current_branch(workspace: Path) -> str:
    """游离 HEAD 时返回空串，调用方据此提示用户先切回分支。"""
    completed = run_git(workspace, ["symbolic-ref", "--quiet", "--short", "HEAD"])
    return (completed.stdout or "").strip() if completed.returncode == 0 else ""


def git_default_branch(workspace: Path, branches: list[str]) -> str:
    """基准分支的默认值：优先当前分支，其次远端 HEAD，最后常见主干名。"""
    current = git_current_branch(workspace)
    if current:
        return current
    head = run_git(workspace, ["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"])
    if head.returncode == 0:
        candidate = (head.stdout or "").strip()
        if candidate in branches:
            return candidate
    for candidate in ("main", "master", "develop"):
        if candidate in branches:
            return candidate
    return branches[0] if branches else ""


def git_fetch_all(workspace: Path, remote: str = "origin") -> str:
    """把远端分支引用同步到本机。返回失败说明，空串表示成功或没有远端。

    别人新建的需求分支只有 fetch 过才看得见；离线时不该把整个分支列表也一起废掉。
    """
    if remote not in git_output(workspace, ["remote"], "读取 Git 远端失败").split():
        return ""
    completed = run_git(workspace, ["fetch", "--prune", remote], timeout=180)
    if completed.returncode != 0:
        return (completed.stdout or "").strip() or "git 退出异常"
    return ""


def git_branch_catalog(workspace: Path) -> dict[str, Any]:
    """本地分支加远端分支，去重后按名称排序；origin/HEAD 这类符号引用不列出。"""
    require_git_workspace(workspace)
    # 列分支前先同步远端引用：别人刚推的分支要能直接在建分支表单里选到。
    fetch_error = git_fetch_all(workspace)
    listed = git_output(
        workspace,
        ["for-each-ref", "--format=%(refname:short)", "refs/heads", "refs/remotes"],
        "读取 Git 分支失败",
    )
    # refs/remotes/origin/HEAD 的简写就是 origin，光判 /HEAD 结尾漏得掉，会混进分支下拉里。
    remote_names = set(git_output(workspace, ["remote"], "读取 Git 远端失败").split())
    branches: list[str] = []
    for line in listed.splitlines():
        name = line.strip()
        if not name or name.endswith("/HEAD") or name in remote_names:
            continue
        if name not in branches:
            branches.append(name)
    branches.sort()
    return {
        "branches": branches,
        "defaultBranch": git_default_branch(workspace, branches),
        # 面板要标注项目此刻所处的分支，游离 HEAD 时为空串。
        "currentBranch": git_current_branch(workspace),
        # 拉取远端失败时照样给本地分支，但要让面板能说清列表可能不是最新的。
        "fetchError": fetch_error,
    }


def normalized_git_remote_url(value: str) -> str:
    """用于显示层面的远端比较，忽略协议和 .git 尾缀的等价形式。"""
    text = str(value or "").strip().rstrip("/")
    if text.endswith(".git"):
        text = text[:-4]
    if text.startswith("git@") and ":" in text:
        host, path = text[4:].split(":", 1)
        text = f"{host}/{path}"
    for prefix in ("ssh://git@", "https://", "http://"):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    return text.lower().rstrip("/")


def git_remote_url(workspace: Path, remote: str) -> str:
    if not valid_git_remote_name(remote):
        raise BridgeFailure("Git 远端名称不合法")
    completed = run_git(workspace, ["remote", "get-url", remote])
    if completed.returncode != 0:
        return ""
    return (completed.stdout or "").strip()


def git_worktree_summary(workspace: Path) -> dict[str, int | bool]:
    """把 porcelain 状态压成面板需要的数量，绝不返回文件路径。"""
    # porcelain 的前两位就是暂存区 / 工作区状态，不能复用 git_output：它会 trim
    # 整段输出，恰好会吞掉第一行的前导空格，把 " M" 误读成 "M "。
    completed = run_git(workspace, ["status", "--porcelain=v1"])
    if completed.returncode != 0:
        raise BridgeFailure(f"读取 Git 工作区状态失败：{(completed.stdout or '').strip() or 'git 退出异常'}")
    output = (completed.stdout or "").rstrip()
    changed = 0
    staged = 0
    unstaged = 0
    untracked = 0
    for line in output.splitlines():
        changed += 1
        if line.startswith("??"):
            untracked += 1
            continue
        state = line[:2]
        if state[:1] not in {" ", "?"}:
            staged += 1
        if len(state) > 1 and state[1:2] not in {" ", "?"}:
            unstaged += 1
    return {
        "dirty": bool(output),
        "changed": changed,
        "staged": staged,
        "unstaged": unstaged,
        "untracked": untracked,
    }


# 变更明细面板的两条硬上限：文件太多只列前面一批，单个文件太大就不回正文。
MAX_GIT_CHANGE_FILES = 300
MAX_GIT_CHANGE_FILE_BYTES = 512 * 1024


def run_git_bytes(workspace: Path, args: list[str], timeout: int = 20) -> subprocess.CompletedProcess:
    """要原样拿文件内容时用这个：stderr 单独收，也不做文本解码。"""
    try:
        return subprocess.run(
            ["git", "-C", str(workspace), *args],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise BridgeFailure("本机未安装 Git，请先在环境预设中完成安装") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise BridgeFailure(f"执行 Git 命令失败：{exc}") from exc


def git_has_head(workspace: Path) -> bool:
    return run_git(workspace, ["rev-parse", "--verify", "--quiet", "HEAD"]).returncode == 0


def git_change_kind_of(state: str) -> str:
    """把 porcelain 的两位状态压成面板要的四种：add / modify / delete / rename。"""
    letters = {state[:1], state[1:2]} - {" "}
    if "D" in letters:
        return "delete"
    if "R" in letters:
        return "rename"
    if "A" in letters or "?" in letters:
        return "add"
    return "modify"


def git_numstat_totals(workspace: Path) -> dict[str, tuple[int, int]]:
    """已跟踪文件相对 HEAD 的增删行数；二进制文件 git 给的是 -，按 0 计。"""
    if not git_has_head(workspace):
        return {}
    completed = run_git(workspace, ["diff", "--numstat", "HEAD"], timeout=60)
    if completed.returncode != 0:
        return {}
    totals: dict[str, tuple[int, int]] = {}
    for line in (completed.stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        added = int(parts[0]) if parts[0].isdigit() else 0
        removed = int(parts[1]) if parts[1].isdigit() else 0
        # 重命名在 numstat 里是 "old => new" 这种写法，取最后一段当作现在的路径。
        path = parts[2].split(" => ")[-1].strip("{}")
        totals[path] = (added, removed)
    return totals


def git_untracked_line_count(workspace: Path, path: str) -> int:
    target = workspace / path
    try:
        if not target.is_file() or target.stat().st_size > MAX_GIT_CHANGE_FILE_BYTES:
            return 0
        raw = target.read_bytes()
    except OSError:
        return 0
    if b"\0" in raw:
        return 0
    return len(raw.splitlines())


def git_change_files(workspace: Path) -> dict[str, Any]:
    """列出工作区相对 HEAD 的文件级改动，给「变更」面板点开看明细用。"""
    require_git_workspace(workspace)
    # -uall 让未跟踪目录展开成一条条文件，否则新增的整个目录只会收到一条以 / 结尾的条目，
    # 面板既算不出行数，也读不到正文，点开只剩「没有可对比的内容」。
    completed = run_git(workspace, ["status", "--porcelain=v1", "-z", "-uall"], timeout=60)
    if completed.returncode != 0:
        raise BridgeFailure(f"读取 Git 变更清单失败：{(completed.stdout or '').strip() or 'git 退出异常'}")
    entries = [chunk for chunk in (completed.stdout or "").split("\0") if chunk]
    totals = git_numstat_totals(workspace)
    files: list[dict[str, Any]] = []
    skip_next = False
    for index, entry in enumerate(entries):
        if skip_next:
            # 重命名条目后面紧跟一条旧路径，-z 下没有引号可解析，只能靠位置跳过。
            skip_next = False
            continue
        if len(entry) < 4:
            continue
        state = entry[:2]
        path = entry[3:]
        kind = git_change_kind_of(state)
        if kind == "rename":
            skip_next = index + 1 < len(entries)
        untracked = state == "??"
        added, removed = totals.get(path, (0, 0))
        if untracked:
            added = git_untracked_line_count(workspace, path)
            removed = 0
        files.append({
            "path": path,
            "kind": kind,
            "added": added,
            "removed": removed,
            "staged": state[:1] not in {" ", "?"},
            "untracked": untracked,
        })
    files.sort(key=lambda item: item["path"])
    truncated = len(files) > MAX_GIT_CHANGE_FILES
    return {
        "workspace": str(workspace),
        "branch": git_current_branch(workspace),
        "files": files[:MAX_GIT_CHANGE_FILES],
        "total": len(files),
        "truncated": truncated,
    }


def git_change_text(raw: bytes) -> tuple[str, bool]:
    """返回可展示的正文和「是不是二进制」；二进制一律不回正文。"""
    if b"\0" in raw:
        return "", True
    try:
        return raw.decode("utf-8"), False
    except UnicodeDecodeError:
        return "", True


def git_change_detail(workspace: Path, path: str) -> dict[str, Any]:
    """一个文件改动前后的两份正文，交给前端的 diff 组件对比。

    只接受当前确实有改动的路径：这样既不用自己防目录穿越，也不会变成任意文件读取口子。
    """
    target = str(path or "").strip()
    if not target:
        raise BridgeFailure("缺少文件路径")
    listing = git_change_files(workspace)
    entry = next((item for item in listing["files"] if item["path"] == target), None)
    if entry is None:
        raise BridgeFailure(f"当前工作区没有这个文件的改动：{target}")
    old_raw = b""
    if not entry["untracked"] and entry["kind"] != "add" and git_has_head(workspace):
        completed = run_git_bytes(workspace, ["show", f"HEAD:{target}"], timeout=60)
        if completed.returncode == 0:
            old_raw = completed.stdout or b""
    new_raw = b""
    working = workspace / target
    try:
        if working.is_file():
            new_raw = working.read_bytes()
    except OSError as exc:
        raise BridgeFailure(f"读取文件失败：{exc}") from exc
    if len(old_raw) > MAX_GIT_CHANGE_FILE_BYTES or len(new_raw) > MAX_GIT_CHANGE_FILE_BYTES:
        return {**entry, "oldText": "", "newText": "", "binary": False, "truncated": True}
    old_text, old_binary = git_change_text(old_raw)
    new_text, new_binary = git_change_text(new_raw)
    return {
        **entry,
        "oldText": old_text,
        "newText": new_text,
        "binary": old_binary or new_binary,
        "truncated": False,
    }


def git_local_branch_for_reference(workspace: Path, reference: str, remote: str) -> tuple[str, str]:
    """解析本地或远端分支引用，返回应使用的本地名和可选远端引用。"""
    value = str(reference or "").strip()
    if not valid_git_branch_name(value):
        raise BridgeFailure("需求分支名不合法")
    if git_branch_exists(workspace, value):
        return value, ""
    remote_prefix = f"{remote}/"
    if value.startswith(remote_prefix):
        local = value[len(remote_prefix):]
        remote_ref = value
    else:
        local = value
        remote_ref = f"{remote}/{value}"
    if not valid_git_branch_name(local):
        raise BridgeFailure("远端需求分支名不合法")
    exists = run_git(workspace, ["rev-parse", "--verify", "--quiet", f"refs/remotes/{remote_ref}"])
    if exists.returncode != 0:
        # 分支可能是别人刚推上来的，本机还没 fetch 过；先拉一次远端引用再判断。
        fetched = run_git(workspace, ["fetch", remote, local], timeout=180)
        exists = run_git(workspace, ["rev-parse", "--verify", "--quiet", f"refs/remotes/{remote_ref}"])
        if exists.returncode != 0:
            detail = (fetched.stdout or "").strip()
            raise BridgeFailure(
                f"本机和远端都不存在需求分支 {value}" + (f"。git 输出：{detail}" if fetched.returncode != 0 and detail else "")
            )
    return local, remote_ref


def git_checkout_reference(
    workspace: Path, reference: str, remote: str, keep_submodules: list[str] | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """切到本地分支；只有远端存在时创建受跟踪的本地分支。返回分支名和子模组同步结果。"""
    local, remote_ref = git_local_branch_for_reference(workspace, reference, remote)
    if git_current_branch(workspace) == local:
        return local, []
    if git_worktree_dirty(workspace):
        raise BridgeFailure(f"工作目录有未提交改动，无法切换到分支 {local}，请先提交或暂存")
    args = ["checkout", local] if not remote_ref else ["checkout", "-b", local, "--track", remote_ref]
    completed = run_git(workspace, args)
    if completed.returncode != 0:
        raise BridgeFailure(f"切换分支 {local} 失败：{(completed.stdout or '').strip() or 'git 退出异常'}")
    return local, git_sync_unselected_submodules(workspace, keep_submodules or [])


def git_workspace_status(workspace: Path, expected_remote_url: str = "", remote: str = "origin") -> dict[str, Any]:
    """读取项目当前 Git 状态。此函数只做本机读取，不 fetch、不切换、不写入。"""
    require_git_workspace(workspace)
    if not valid_git_remote_name(remote):
        raise BridgeFailure("Git 远端名称不合法")
    actual_remote_url = git_remote_url(workspace, remote)
    expected = str(expected_remote_url or "").strip()
    remote_matches = not expected or (
        bool(actual_remote_url) and normalized_git_remote_url(actual_remote_url) == normalized_git_remote_url(expected)
    )
    summary = git_worktree_summary(workspace)
    current = git_current_branch(workspace)
    # 远端地址可能包含嵌入式凭据；浏览器只需要知道是否一致，不能回传具体地址。
    return {
        "workspace": str(workspace),
        "isGitRepository": True,
        "remoteName": remote,
        "remoteMatches": remote_matches,
        "currentBranch": current,
        "detached": not bool(current),
        "checkedAt": int(time.time()),
        **summary,
    }


def git_prepare_branch(
    workspace: Path,
    reference: str,
    strategy: str = "switch",
    commit_message: str = "",
    expected_remote_url: str = "",
    remote: str = "origin",
    allow_detached: bool = False,
    keep_submodules: list[str] | None = None,
) -> dict[str, Any]:
    """用户确认后才处理未提交改动并切分支；绝不丢弃改动或自动应用 stash。

    allow_detached 只给子项目用。`git submodule update` 本来就是把子模块检出到父仓库
    记录的那个 commit，游离 HEAD 是子模块的常态而不是异常，一律拒绝会让子项目永远停在
    「分支不一致」上，切不过去。根工作目录仍然拒绝：那里的游离 HEAD 通常是人工操作的中间态。
    """
    if strategy not in {"switch", "commit", "stash"}:
        raise BridgeFailure("未知的 Git 分支处理方式")
    status = git_workspace_status(workspace, expected_remote_url, remote)
    if not status["remoteMatches"]:
        raise BridgeFailure("本机 Git 远端与项目配置不一致，请先确认项目仓库地址或工作目录")
    if status["detached"] and not allow_detached:
        raise BridgeFailure("当前工作目录处于游离 HEAD，不能切换需求分支")
    if status["detached"] and status["dirty"] and strategy == "commit":
        # 游离 HEAD 上提交会生成一个没有分支指向的提交，切走之后就只能靠 reflog 找回来。
        raise BridgeFailure("当前项目处于游离 HEAD，改动不能就地提交，请改选暂存后切换")
    local, _ = git_local_branch_for_reference(workspace, reference, remote)
    if status["currentBranch"] == local:
        # 已经在需求分支上：直接拉一次最新。工作区脏也照拉，git 的快进本身就会拒绝覆盖
        # 未提交改动，拉得动说明改动和远端不冲突，原样留在工作区里。
        pulled_only = git_pull_branch(workspace, local, remote)
        return {
            "branch": local,
            "previousBranch": status["currentBranch"],
            "pulled": pulled_only,
            "committed": False,
            "stashed": False,
            "submodules": [],
            "status": git_workspace_status(workspace, expected_remote_url, remote) if pulled_only else status,
        }
    # 先把目标分支拉到远端最新，再决定怎么处理当前改动：拉不动就不该先提交一轮。
    pulled = git_pull_branch(workspace, local, remote)
    committed = False
    stashed = False
    if status["dirty"]:
        dirty_submodules = git_dirty_submodule_workspaces(workspace)
        if strategy == "commit":
            message = git_commit_message_of(commit_message, str(status["currentBranch"]))
            for submodule in dirty_submodules:
                submodule_label = git_submodule_label(workspace, submodule)
                if not git_current_branch(submodule):
                    raise BridgeFailure(f"子模块 {submodule_label} 处于游离 HEAD，不能自动提交，请改选暂存后切换")
                if run_git(submodule, ["add", "--all"]).returncode != 0:
                    raise BridgeFailure(f"暂存子模块 {submodule_label} 改动失败")
                completed = run_git(submodule, ["commit", "-m", f"{message} ({submodule_label})"], timeout=120)
                if completed.returncode != 0:
                    raise BridgeFailure(
                        f"提交子模块 {submodule_label} 改动失败：{(completed.stdout or '').strip() or 'git 退出异常'}"
                    )
            if run_git(workspace, ["add", "--all"]).returncode != 0:
                raise BridgeFailure("暂存当前工作区改动失败")
            completed = run_git(workspace, ["commit", "-m", message], timeout=120)
            if completed.returncode != 0:
                raise BridgeFailure(f"提交当前分支改动失败：{(completed.stdout or '').strip() or 'git 退出异常'}")
            committed = True
        elif strategy == "stash":
            label = f"delivery-task-planner: {status['currentBranch']} -> {local}"
            for submodule in dirty_submodules:
                submodule_label = git_submodule_label(workspace, submodule)
                completed = run_git(
                    submodule,
                    ["stash", "push", "--include-untracked", "-m", f"{label} ({submodule_label})"],
                    timeout=120,
                )
                if completed.returncode != 0:
                    raise BridgeFailure(
                        f"暂存子模块 {submodule_label} 改动失败：{(completed.stdout or '').strip() or 'git 退出异常'}"
                    )
            completed = run_git(workspace, ["stash", "push", "--include-untracked", "-m", label], timeout=120)
            if completed.returncode != 0:
                raise BridgeFailure(f"暂存当前分支改动失败：{(completed.stdout or '').strip() or 'git 退出异常'}")
            stashed = True
        else:
            raise BridgeFailure("工作目录有未提交改动，请选择先提交或暂存后再切换")
        if git_worktree_dirty(workspace):
            remaining = git_worktree_summary(workspace)
            raise BridgeFailure(
                f"处理改动后工作目录仍有 {remaining['changed']} 个待提交文件，可能有其它进程正在写入；请停止写入后重试"
            )
    branch, submodules = git_checkout_reference(workspace, reference, remote, keep_submodules)
    return {
        "branch": branch,
        "pulled": pulled,
        "committed": committed,
        "stashed": stashed,
        "submodules": submodules,
        "previousBranch": status["currentBranch"],
        "status": git_workspace_status(workspace, expected_remote_url, remote),
    }


def git_branch_exists(workspace: Path, branch: str) -> bool:
    return run_git(workspace, ["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"]).returncode == 0


def git_worktree_dirty(workspace: Path) -> bool:
    return bool(git_output(workspace, ["status", "--porcelain"], "读取 Git 工作区状态失败"))


def git_submodule_workspaces(workspace: Path) -> list[Path]:
    """返回已初始化的子模块，按最内层到最外层排列，便于先处理嵌套工作区。"""
    root = workspace.resolve()
    seen: set[Path] = set()
    result: list[Path] = []

    def collect(parent: Path) -> None:
        completed = run_git(parent, ["config", "--file", ".gitmodules", "--null", "--get-regexp", r"^submodule\..*\.path$"])
        if completed.returncode == 1:
            return
        if completed.returncode != 0:
            raise BridgeFailure(f"读取子模块配置失败：{(completed.stdout or '').strip() or 'git 退出异常'}")
        for record in (completed.stdout or "").split("\0"):
            if not record:
                continue
            _, separator, raw_path = record.partition("\n")
            child = (parent / raw_path.strip()).resolve()
            if not separator or not raw_path.strip():
                raise BridgeFailure("子模块路径配置无效")
            try:
                child.relative_to(root)
            except ValueError as exc:
                raise BridgeFailure("子模块路径超出项目工作目录") from exc
            # 没初始化的子模组只是个空目录，rev-parse 会一路走到父仓库照样说「在工作树里」。
            # 只有自带 .git（子模组是一个 gitfile）才算真的检出过，能当成独立工作区来读写。
            if child in seen or not (child / ".git").exists():
                continue
            seen.add(child)
            collect(child)
            result.append(child)

    collect(root)
    return result


def git_dirty_submodule_workspaces(workspace: Path) -> list[Path]:
    return [submodule for submodule in git_submodule_workspaces(workspace) if git_worktree_dirty(submodule)]


def git_submodule_label(workspace: Path, submodule: Path) -> str:
    return submodule.resolve().relative_to(workspace.resolve()).as_posix()


def git_sync_unselected_submodules(workspace: Path, targets: list[str]) -> list[dict[str, Any]]:
    """切完分支后，把没被勾选的子模组同步到父仓库这条分支记录的 commit。

    切分支本身不再带 --recurse-submodules：那是整仓行为，一个对象缺失或有本地改动的子模组
    就能让整条需求分支都建不出来，而界面上勾没勾它完全不起作用。改成切完再逐个同步之后，
    同步不动的子模组只留一条结果记录，不连累根工作目录和同一轮里的其它工程。

    勾选中的子模组不在这里同步：它们下一步要建（或切到）自己的需求分支，
    先 submodule update 会把它们检出成游离 HEAD，把那一步的活儿白做一遍。
    """
    selected = {str(name or "").strip().strip("/") for name in targets}
    records: list[dict[str, Any]] = []
    try:
        # git_submodule_workspaces 是最内层在前，这里反过来：先更新外层，嵌套的那层才对得上新指针。
        submodules = list(reversed(git_submodule_workspaces(workspace)))
    except BridgeFailure as exc:
        # 分支已经切好了，读不动子模组配置不该反过来变成整个动作失败。
        return [{
            "path": ".gitmodules",
            "name": ".gitmodules",
            "branch": "",
            "baseBranch": "",
            "created": False,
            "switched": False,
            "skipped": True,
            "error": str(exc),
        }]
    for submodule in submodules:
        label = git_submodule_label(workspace, submodule)
        if label in selected:
            continue
        # 不带 --init：这里只把已经检出的子模组对齐到新指针，绝不顺手克隆一个新的。
        # 建分支是个前台动作，超时按分钟算，不能让一次慢 fetch 把整个弹窗挂住。
        completed = run_git(
            submodule.parent, ["submodule", "update", "--", submodule.name], timeout=120,
        )
        if completed.returncode != 0:
            records.append({
                "path": label,
                "name": label,
                "branch": "",
                "baseBranch": "",
                "created": False,
                "switched": False,
                "skipped": True,
                "error": f"同步子模组 {label} 失败：{(completed.stdout or '').strip() or 'git 退出异常'}",
            })
    return records


# 扫子项目时不进这些目录：依赖和产物目录里也可能躺着 .git，但它们不是这个项目的工程。
GIT_SUBPROJECT_SKIP_DIRS = {
    "node_modules", "vendor", "dist", "build", "out", "target",
    "__pycache__", "venv", ".venv", "tmp", "temp",
}


def git_subproject_workspaces(workspace: Path) -> list[Path]:
    """工作目录下一级子目录里自带 .git 的独立工程，按目录名排序。

    只看一级：再往下扫要为每个候选目录多跑一轮 IO，而实际的多工程布局都是
    「工作目录/工程名」这一层。已注册成 submodule 的目录同样自带 .git，照样列出来。
    """
    root = workspace.resolve()
    try:
        children = sorted(root.iterdir(), key=lambda item: item.name.casefold())
    except OSError as exc:
        raise BridgeFailure(f"读取项目工作目录失败：{exc}") from exc
    result: list[Path] = []
    for child in children:
        if child.name.startswith(".") or child.name in GIT_SUBPROJECT_SKIP_DIRS:
            continue
        if child.is_symlink() or not child.is_dir():
            continue
        if not (child / ".git").exists():
            continue
        result.append(child.resolve())
    return result


def git_subproject_workspace_of(workspace: Path, relative: str) -> Path:
    """把前端传回来的子项目相对路径还原成目录；只认扫描列出来的那一级。"""
    value = str(relative or "").strip().strip("/")
    root = workspace.resolve()
    if not value or value == ".":
        return root
    candidate = (root / value).resolve()
    if candidate == root:
        return root
    if candidate not in git_subproject_workspaces(root):
        raise BridgeFailure(f"子项目不存在或不是 Git 仓库：{value}")
    return candidate


def git_branch_reference_exists(workspace: Path, branch: str, remote: str = "origin") -> bool:
    """本机或已同步的远端引用里有没有这条分支；只读本地引用，不联网 fetch。"""
    if not valid_git_branch_name(branch):
        return False
    if git_branch_exists(workspace, branch):
        return True
    # 分支名本身可能已经带了远端前缀（下拉里选的是 origin/main）；再拼一次会查成 origin/origin/main，
    # 结果把明明存在的基准分支判成不存在。
    reference = branch if branch.startswith(f"{remote}/") else f"{remote}/{branch}"
    return run_git(workspace, ["rev-parse", "--verify", "--quiet", f"refs/remotes/{reference}"]).returncode == 0


def git_project_snapshot(workspace: Path, relative: str, branch: str = "", remote: str = "origin") -> dict[str, Any]:
    """单个工程的 Git 快照。读不动的工程只把原因带回去，不连累同级的其它工程。"""
    record: dict[str, Any] = {
        "path": relative,
        "name": relative or workspace.name,
        "workspace": str(workspace),
        "isGitRepository": False,
        "hasBranch": False,
        "error": "",
        "remoteName": remote,
        "remoteMatches": True,
        "currentBranch": "",
        "detached": False,
        "dirty": False,
        "changed": 0,
        "staged": 0,
        "unstaged": 0,
        "untracked": 0,
        "checkedAt": int(time.time()),
    }
    try:
        record.update(git_workspace_status(workspace, "", remote))
        if branch:
            record["hasBranch"] = git_branch_reference_exists(workspace, branch, remote)
    except BridgeFailure as exc:
        record["error"] = str(exc)
    # git_workspace_status 会覆盖 workspace，但不认识 path / name，这里补回来。
    record["path"] = relative
    record["name"] = relative or workspace.name
    return record


def git_workspace_projects(workspace: Path, branch: str = "", remote: str = "origin") -> dict[str, Any]:
    """根工作目录加一级子项目的 Git 快照，给需求窗口的 Git 面板分工程展示。"""
    projects = [git_project_snapshot(workspace, "", branch, remote)]
    for child in git_subproject_workspaces(workspace):
        projects.append(git_project_snapshot(child, child.name, branch, remote))
    return {"workspace": str(workspace), "projects": projects}


def git_subproject_targets_of(workspace: Path, raw: Any, branch: str = "", remote: str = "origin") -> list[str]:
    """请求里带了 targets 就按它来，没带就自动选出所有已有这条分支的子项目。

    没带 targets 的调用方（需求列表的分支检查、旧版本控制台）也该把子项目一起带上，
    否则切完分支只有根目录跟着走，子工程还停在别的需求上。
    """
    if raw is None:
        if not branch:
            return []
        return [
            child.name for child in git_subproject_workspaces(workspace)
            if git_branch_reference_exists(child, branch, remote)
        ]
    if not isinstance(raw, list):
        raise BridgeFailure("子项目列表格式不正确")
    targets: list[str] = []
    for value in raw:
        name = str(value or "").strip().strip("/")
        if name and name != "." and name not in targets:
            targets.append(name)
    return targets


def git_checkout_branch(
    workspace: Path, branch: str, keep_submodules: list[str] | None = None,
) -> list[dict[str, Any]]:
    """切换到已存在的本地分支；工作区有未提交改动时不强行切，交回给用户处理。"""
    if git_current_branch(workspace) == branch:
        return []
    if git_worktree_dirty(workspace):
        raise BridgeFailure(f"工作目录有未提交改动，无法切换到分支 {branch}，请先提交或暂存")
    completed = run_git(workspace, ["checkout", branch])
    if completed.returncode != 0:
        raise BridgeFailure(f"切换分支 {branch} 失败：{(completed.stdout or '').strip() or 'git 退出异常'}")
    return git_sync_unselected_submodules(workspace, keep_submodules or [])


def git_default_remote(workspace: Path) -> str:
    """只认 origin：需求分支是给评审用的，推到哪个远端不该由面板猜。"""
    remotes = git_output(workspace, ["remote"], "读取 Git 远端失败").split()
    if "origin" in remotes:
        return "origin"
    raise BridgeFailure("当前仓库没有配置 origin 远端，无法推送")


GIT_PUSH_REPAIR_TIMEOUT_SECONDS = 15 * 60


def git_branch_synced(workspace: Path, branch: str, remote: str = "origin") -> bool:
    """本地分支是否已经全部推到远端。AI 兜底之后用它判定，而不是信 AI 的自述。"""
    run_git(workspace, ["fetch", remote, branch], timeout=180)
    ahead = run_git(workspace, ["rev-list", "--count", f"{remote}/{branch}..{branch}"])
    return ahead.returncode == 0 and (ahead.stdout or "").strip() == "0"


def build_git_push_repair_prompt(workspace: Path, branch: str, remote: str, failure: str, commit_message: str) -> str:
    """推送失败时交给 AI 的修复提示词。只授权它解决推送本身，不允许改业务实现。"""
    return wrap_bridge_context(
        [
            "这是交付任务面板的「推送需求分支」回合：面板已经尝试提交并推送，但失败了，请你在本机把它修好并真正推送成功。",
            workspace_instruction(workspace),
            f"需求分支: {branch}",
            f"远端: {remote}",
            f"面板使用的提交说明: {commit_message}",
            "",
            "面板执行失败的原始输出:",
            failure,
            "",
            "处理要求:",
            "- 面板已经把改动提交成提交点，并自动试过一次 rebase；失败后已经 --abort，仓库现在是干净状态，不在变基中。",
            "- 只解决提交与推送本身：拉取远端、rebase 或 merge、解决冲突、补提交、重新 push。",
            "- 解决冲突时保留双方的真实意图，不要为了让命令通过而删掉别人的改动。",
            "- 不要修改与本次冲突无关的业务实现，不要改动其他分支。",
            f"- 禁止 force push、禁止 push 到 {branch} 以外的分支、禁止改写已经推到远端的历史。",
            "- 处理不了（例如需要凭据、需要人工决策的冲突）就停下来说明原因，不要绕开。",
            "- 最后必须实际执行一次 push，并在回复里贴出 push 命令的真实输出。",
        ],
        f"推送需求分支 {branch} 失败，请解决后重新推送。",
    )


MAX_GIT_COMMIT_MESSAGE_BYTES = 4 * 1024


def git_commit_message_of(value: str, branch: str) -> str:
    """提交说明来自用户输入，只做长度和控制字符限制；命令参数是数组，不存在注入。"""
    message = str(value or "").strip() or f"chore: {branch}"
    if len(message.encode("utf-8")) > MAX_GIT_COMMIT_MESSAGE_BYTES:
        raise BridgeFailure("提交说明过长")
    if "\x00" in message:
        raise BridgeFailure("提交说明不能包含控制字符")
    return message


def git_rebase_onto_remote(workspace: Path, branch: str, remote: str = "origin") -> str:
    """推送前把远端最新并进本地分支。返回 ""/"pulled"/"rebased"。

    这里刻意不用 stash：改动在上一步已经提交成一个提交点，没有需要暂存的东西；
    冲突留在 rebase 里比留在 stash pop 里可控得多，失败也会 --abort 回到干净状态。
    """
    if remote not in git_output(workspace, ["remote"], "读取 Git 远端失败").split():
        return ""
    fetched = run_git(workspace, ["fetch", remote, branch], timeout=180)
    remote_ref = f"{remote}/{branch}"
    if run_git(workspace, ["rev-parse", "--verify", "--quiet", f"refs/remotes/{remote_ref}"]).returncode != 0:
        # 远端还没有这条分支，这次就是首推。
        return ""
    if fetched.returncode != 0:
        raise BridgeFailure(f"拉取分支 {branch} 失败：{(fetched.stdout or '').strip() or 'git 退出异常'}")
    if git_remote_ref_merged(workspace, remote_ref, branch):
        return ""
    if run_git(workspace, ["merge-base", "--is-ancestor", branch, remote_ref]).returncode == 0:
        # 本地只是落后：快进即可，不要平白造一个合并提交。
        completed = run_git(workspace, ["merge", "--ff-only", remote_ref], timeout=120)
        if completed.returncode != 0:
            raise BridgeFailure(f"快进到 {remote_ref} 失败：{(completed.stdout or '').strip() or 'git 退出异常'}")
        return "pulled"
    completed = run_git(workspace, ["rebase", remote_ref], timeout=300)
    if completed.returncode != 0:
        detail = (completed.stdout or "").strip() or "git 退出异常"
        # 冲突不留在半路：先回到干净状态，再把原始输出抛上去交给 AI 兜底那一轮处理。
        run_git(workspace, ["rebase", "--abort"], timeout=120)
        raise BridgeFailure(f"本地分支 {branch} 与 {remote_ref} 有冲突，自动 rebase 没能完成：{detail}")
    return "rebased"


def git_push_branch(workspace: Path, branch: str, message: str = "", push: bool = True) -> dict[str, Any]:
    """先同步远端最新，再把工作区改动提交到需求分支并推到 origin。

    只做普通推送，不带 --force：远端已经跑在前面时报错给用户，不在这里替他决定怎么合。
    push=False 时只提交不推送，给「仅提交」用：本地留一个提交点，什么时候推由用户决定。
    """
    if not valid_git_branch_name(branch):
        raise BridgeFailure("需求分支名不合法")
    require_git_workspace(workspace)
    if not git_branch_exists(workspace, branch):
        raise BridgeFailure(f"本机不存在需求分支 {branch}，请先创建分支")
    # 仅提交在有 origin 时也要先同步最新；纯本地仓库仍可正常落一个提交点。
    remote = git_default_remote(workspace) if push else (
        "origin" if "origin" in git_output(workspace, ["remote"], "读取 Git 远端失败").split() else ""
    )
    commit_message = git_commit_message_of(message, branch)
    current = git_current_branch(workspace)
    dirty = git_worktree_dirty(workspace)
    if current != branch:
        # 改动是在别的分支上做的，提交到需求分支多半是误操作，让用户自己先归位。
        if dirty:
            raise BridgeFailure(
                f"工作目录当前在分支 {current or 'HEAD'} 上且有未提交改动，请先处理后再推送需求分支 {branch}"
            )
        git_checkout_branch(workspace, branch)
    committed = False
    synced = ""
    if dirty:
        # 工作区有改动时不能直接 pull/rebase。先落一个仅供同步使用的临时提交，
        # 同步完成（或失败并 abort）后立即拆回工作区改动，再用用户填写的说明正式提交。
        # 这样远端冲突发生在正式提交之前，也不会把用户改动藏进 stash。
        add = run_git(workspace, ["add", "--all"])
        if add.returncode != 0:
            raise BridgeFailure(f"暂存改动失败：{(add.stdout or '').strip() or 'git 退出异常'}")
        temporary = run_git(workspace, ["commit", "-m", "delivery-task-planner: sync before commit"], timeout=120)
        if temporary.returncode != 0:
            raise BridgeFailure(f"准备拉取前的临时提交失败：{(temporary.stdout or '').strip() or 'git 退出异常'}")
        try:
            if remote:
                synced = git_rebase_onto_remote(workspace, branch, remote)
        finally:
            restored = run_git(workspace, ["reset", "--mixed", "HEAD^"], timeout=120)
            if restored.returncode != 0:
                raise BridgeFailure(
                    f"拉取后恢复待提交改动失败：{(restored.stdout or '').strip() or 'git 退出异常'}"
                )
        commit = run_git(workspace, ["add", "--all"])
        if commit.returncode != 0:
            raise BridgeFailure(f"暂存改动失败：{(commit.stdout or '').strip() or 'git 退出异常'}")
        commit = run_git(workspace, ["commit", "-m", commit_message], timeout=120)
        if commit.returncode != 0:
            raise BridgeFailure(f"提交改动失败：{(commit.stdout or '').strip() or 'git 退出异常'}")
        committed = True
    elif remote:
        # 没有工作区改动也要在“提交/推送”动作开始时同步一次，避免基于旧分支判断已是最新。
        synced = git_rebase_onto_remote(workspace, branch, remote)
    if not push:
        return {
            "pushed": False,
            "branch": branch,
            "remote": remote,
            "committed": committed,
            "commitMessage": commit_message if committed else "",
            "upToDate": False,
            "synced": synced,
            "output": "",
        }
    # 首次同步与 push 之间远端仍可能变化；推送前再确认一次，关闭这段竞态窗口。
    synced = git_rebase_onto_remote(workspace, branch, remote) or synced
    completed = run_git(workspace, ["push", "--set-upstream", remote, f"{branch}:{branch}"], timeout=180)
    output = (completed.stdout or "").strip()
    if completed.returncode != 0:
        raise BridgeFailure(f"推送分支 {branch} 失败：{output or 'git 退出异常'}")
    return {
        "pushed": True,
        "branch": branch,
        "remote": remote,
        "committed": committed,
        "commitMessage": commit_message if committed else "",
        "upToDate": "Everything up-to-date" in output,
        # 推送前是否并过远端最新："pulled" 是快进，"rebased" 是把本地提交挪到远端之后。
        "synced": synced,
        "output": output[-2000:],
    }


def git_remote_ref_merged(workspace: Path, remote_ref: str, local_branch: str) -> bool:
    """远端引用是否已经在本地分支里；领先远端的本地分支不该被当成「拉取失败」。"""
    return run_git(workspace, ["merge-base", "--is-ancestor", remote_ref, local_branch]).returncode == 0


def git_pull_branch(workspace: Path, local: str, remote: str = "origin") -> bool:
    """切到需求分支前先把它拉到远端最新；拉不动就报错，不带着旧代码切过去。

    返回是否真的更新过本地分支。远端还没有这条分支时直接跳过：刚建的需求分支很正常。
    """
    remotes = git_output(workspace, ["remote"], "读取 Git 远端失败").split()
    if remote not in remotes:
        return False
    fetched = run_git(workspace, ["fetch", remote, local], timeout=180)
    remote_ref = f"{remote}/{local}"
    if run_git(workspace, ["rev-parse", "--verify", "--quiet", f"refs/remotes/{remote_ref}"]).returncode != 0:
        return False
    if fetched.returncode != 0:
        raise BridgeFailure(f"拉取需求分支 {local} 失败：{(fetched.stdout or '').strip() or 'git 退出异常'}")
    if not git_branch_exists(workspace, local):
        # 本机还没有这条分支，切换时会基于刚拉到的远端引用创建，已经是最新的。
        return False
    if git_remote_ref_merged(workspace, remote_ref, local):
        return False
    if git_current_branch(workspace) == local:
        completed = run_git(workspace, ["merge", "--ff-only", remote_ref], timeout=120)
    else:
        completed = run_git(workspace, ["fetch", remote, f"{local}:{local}"], timeout=180)
    if completed.returncode != 0:
        raise BridgeFailure(
            f"需求分支 {local} 无法快进到 {remote_ref}：本机的 {local} 上有还没推送的提交，或者已经和远端分叉。"
            f"请先把 {local} 上的改动推送或自行合并，再重新切换。"
            f"git 输出：{(completed.stdout or '').strip() or 'git 退出异常'}"
        )
    return True


def git_sync_base_branch(workspace: Path, base_branch: str, remote: str = "origin") -> str:
    """建需求分支前把基准分支拉到远端最新；否则新分支会切在过时的代码上。

    返回真正用来切分支的引用：能同步就是本地基准分支，只存在于远端时返回 remote/xxx。
    只做快进，分叉了就报错交回给用户，不在这里 merge 或 rebase。
    """
    remotes = git_output(workspace, ["remote"], "读取 Git 远端失败").split()
    remote_prefix = f"{remote}/"
    # 基准分支可能是用户直接选的 origin/xxx；拉取时要用远端那一侧的名字。
    remote_side = base_branch[len(remote_prefix):] if base_branch.startswith(remote_prefix) else base_branch
    local_exists = git_branch_exists(workspace, base_branch)
    if remote not in remotes:
        # 没有 origin 的纯本地仓库没有「最新」可拉，按本地基准分支继续。
        if not local_exists:
            raise BridgeFailure(f"基准分支不存在：{base_branch}")
        return base_branch
    fetched = run_git(workspace, ["fetch", remote, remote_side], timeout=180)
    remote_ref = f"{remote}/{remote_side}"
    remote_exists = run_git(workspace, ["rev-parse", "--verify", "--quiet", f"refs/remotes/{remote_ref}"]).returncode == 0
    if not remote_exists:
        # 远端没有这条分支：只可能是仅存在于本机的基准分支。
        if local_exists:
            return base_branch
        raise BridgeFailure(f"基准分支在本机和 {remote} 都不存在：{base_branch}")
    if fetched.returncode != 0:
        raise BridgeFailure(f"拉取基准分支 {base_branch} 失败：{(fetched.stdout or '').strip() or 'git 退出异常'}")
    if not local_exists:
        # 本机还没有这条基准分支，直接从刚拉到的远端引用切出需求分支。
        return remote_ref
    if git_remote_ref_merged(workspace, remote_ref, base_branch):
        # 本地已经包含远端全部提交（可能还领先），没有可拉的东西。
        return base_branch
    if git_current_branch(workspace) == base_branch:
        completed = run_git(workspace, ["merge", "--ff-only", remote_ref], timeout=120)
    else:
        # 不在基准分支上时用 fetch 的引用更新做快进，避免为了拉一次而来回切分支。
        completed = run_git(workspace, ["fetch", remote, f"{remote_side}:{base_branch}"], timeout=180)
    if completed.returncode != 0:
        raise BridgeFailure(
            f"基准分支 {base_branch} 无法快进到 {remote_ref}，可能已经分叉或有未推送的提交，"
            f"请先自行处理：{(completed.stdout or '').strip() or 'git 退出异常'}"
        )
    return base_branch


def git_create_branch(
    workspace: Path, base_branch: str, branch: str, keep_submodules: list[str] | None = None,
) -> dict[str, Any]:
    """从基准分支创建并切换到需求分支；分支已存在时只切换，不覆盖已有提交。"""
    if not valid_git_branch_name(base_branch):
        raise BridgeFailure("基准分支名不合法")
    if not valid_git_branch_name(branch):
        raise BridgeFailure("需求分支名不合法")
    require_git_workspace(workspace)
    remote = "origin"
    has_remote = remote in git_output(workspace, ["remote"], "读取 Git 远端失败").split()
    # 「已有分支」下拉给的是 origin/xxx；照原样建会多出一条名叫 origin/xxx 的本地分支。
    local = branch[len(f"{remote}/"):] if has_remote and branch.startswith(f"{remote}/") else branch
    if not local or run_git(workspace, ["check-ref-format", "--branch", local]).returncode != 0:
        raise BridgeFailure(f"需求分支名不符合 Git 规范：{branch}")
    if git_branch_exists(workspace, local):
        # 本机已有这条分支：切过去并拉到远端最新，不覆盖已有提交。
        submodules = git_checkout_branch(workspace, local, keep_submodules)
        git_pull_branch(workspace, local)
        return {"created": False, "baseBranch": base_branch, "branch": local, "submodules": submodules}
    if git_worktree_dirty(workspace):
        raise BridgeFailure("工作目录有未提交改动，无法创建需求分支，请先提交或暂存")
    if has_remote:
        run_git(workspace, ["fetch", remote, local], timeout=180)
        if run_git(workspace, ["rev-parse", "--verify", "--quiet", f"refs/remotes/{remote}/{local}"]).returncode == 0:
            # 别人已经推过同名分支：必须关联它，从基准分支另起一条会和远端分叉。
            checkout = run_git(workspace, ["checkout", "-b", local, "--track", f"{remote}/{local}"])
            if checkout.returncode != 0:
                raise BridgeFailure(f"关联远端需求分支 {local} 失败：{(checkout.stdout or '').strip() or 'git 退出异常'}")
            return {
                "created": False, "baseBranch": base_branch, "branch": local,
                "submodules": git_sync_unselected_submodules(workspace, keep_submodules or []),
            }
    # 先把基准分支拉到最新，再从它切出去；基准分支是否存在也在这一步确认。
    base_reference = git_sync_base_branch(workspace, base_branch)
    # 从远端引用切出来时不要跟踪基准分支：需求分支后面要推到它自己的远端分支。
    from_remote_ref = run_git(
        workspace, ["rev-parse", "--verify", "--quiet", f"refs/remotes/{base_reference}"],
    ).returncode == 0
    track = ["--no-track"] if from_remote_ref else []
    completed = run_git(workspace, ["checkout", *track, "-b", local, base_reference])
    if completed.returncode != 0:
        raise BridgeFailure(f"创建需求分支失败：{(completed.stdout or '').strip() or 'git 退出异常'}")
    return {
        "created": True, "baseBranch": base_branch, "branch": local,
        "submodules": git_sync_unselected_submodules(workspace, keep_submodules or []),
    }


def git_effective_base_branch(workspace: Path, base_branch: str, remote: str = "origin") -> str:
    """子项目不一定有同名基准分支：没有就退回它自己的主干，而不是直接建不出来。

    回落只认主干，绝不认「当前分支」：子项目常常还停在上一条需求分支上，从那里切出去
    会把上一条需求的提交整个带进新需求分支，而且看不出来。宁可报错也不要切错基准。
    """
    if git_branch_reference_exists(workspace, base_branch, remote):
        return base_branch
    candidates: list[str] = []
    # origin/HEAD 是远端自己声明的主干，比猜名字准；没同步过这条符号引用时才轮到下面的常见名。
    head = run_git(workspace, ["symbolic-ref", "--quiet", "--short", f"refs/remotes/{remote}/HEAD"])
    if head.returncode == 0:
        candidates.append((head.stdout or "").strip())
    # 只认主干名，按常见程度排；develop 排在最后，主干真叫它的仓库才会用到。
    candidates.extend([
        f"{remote}/main", f"{remote}/master", f"{remote}/develop", "main", "master", "develop",
    ])
    for candidate in candidates:
        if candidate and git_branch_reference_exists(workspace, candidate, remote):
            return candidate
    raise BridgeFailure(
        f"子项目里没有基准分支 {base_branch}，也找不到 {remote}/main、{remote}/master 这样的主干可以退回"
    )


def git_create_branch_targets(
    workspace: Path,
    base_branch: str,
    branch: str,
    targets: list[str],
    skip_root: bool = False,
) -> dict[str, Any]:
    """在根工作目录和选中的子项目里创建同一条需求分支。

    根目录失败照旧直接报错（需求还没落库，不该留下半条关联）；子项目失败只记在结果里，
    不回滚已经建好的分支——把已完成的部分撤掉比留着更难收拾。

    skip_root 用于「给已有需求补建子项目分支」：根目录早就在这条分支上，这一轮不该顺手
    切它、拉它，工作区里没提交的改动更不该被牵连。
    """
    if skip_root:
        require_git_workspace(workspace)
        remote = "origin"
        has_remote = remote in git_output(workspace, ["remote"], "读取 Git 远端失败").split()
        local = branch[len(f"{remote}/"):] if has_remote and branch.startswith(f"{remote}/") else branch
        if not git_branch_exists(workspace, local):
            raise BridgeFailure(f"根工作目录还没有需求分支 {local}，请先创建需求分支")
        result: dict[str, Any] = {"created": False, "baseBranch": base_branch, "branch": local}
    else:
        result = git_create_branch(workspace, base_branch, branch, targets)
        local = str(result["branch"])
    records: list[dict[str, Any]] = [{
        "path": "",
        "name": workspace.name,
        "branch": local,
        "baseBranch": str(result["baseBranch"]),
        "created": bool(result["created"]),
        "skipped": skip_root,
        "error": "",
    }]
    # 勾中的子模组在下面单独建自己的需求分支，这里只带回没勾的那些的同步结果。
    records.extend(result.pop("submodules", []))
    for relative in targets:
        record: dict[str, Any] = {
            "path": relative,
            "name": relative,
            "branch": local,
            "baseBranch": "",
            "created": False,
            "error": "",
        }
        try:
            child = git_subproject_workspace_of(workspace, relative)
            if child == workspace.resolve():
                continue
            base = git_effective_base_branch(child, base_branch)
            child_result = git_create_branch(child, base, local)
            record["baseBranch"] = str(child_result["baseBranch"])
            record["created"] = bool(child_result["created"])
        except BridgeFailure as exc:
            record["error"] = str(exc)
        records.append(record)
    result["results"] = records
    return result


def git_prepare_branch_targets(
    workspace: Path,
    reference: str,
    strategy: str,
    commit_message: str,
    expected_remote_url: str,
    remote: str,
    targets: list[str],
) -> dict[str, Any]:
    """根工作目录切完需求分支后，把选中的子项目也切到同一条分支上。

    子项目里没有这条分支时跳过：不是每个工程都参与这条需求，缺分支不算失败。
    """
    result = git_prepare_branch(
        workspace, reference, strategy, commit_message, expected_remote_url, remote, keep_submodules=targets,
    )
    branch = str(result["branch"])
    records: list[dict[str, Any]] = [{
        "path": "",
        "name": workspace.name,
        "branch": branch,
        "switched": True,
        "skipped": False,
        "error": "",
    }]
    records.extend(result.pop("submodules", []))
    for relative in targets:
        record: dict[str, Any] = {
            "path": relative,
            "name": relative,
            "branch": branch,
            "switched": False,
            "skipped": False,
            "error": "",
        }
        try:
            child = git_subproject_workspace_of(workspace, relative)
            if child == workspace.resolve():
                continue
            if not git_branch_reference_exists(child, branch, remote):
                record["skipped"] = True
            else:
                # 子模块常态就是游离 HEAD，这里放行，让它从远端的需求分支建出本地分支。
                child_result = git_prepare_branch(
                    child, branch, strategy, commit_message, "", remote, allow_detached=True,
                )
                record["branch"] = str(child_result["branch"])
                record["switched"] = True
        except BridgeFailure as exc:
            record["error"] = str(exc)
        records.append(record)
    result["results"] = records
    return result


# ---------------------------------------------------------------------------
# 时间计划的分支合并。三个方向共用同一套「target ← sources」机制：
#   - 回合基线：target = 计划分支，sources = [基线分支]
#   - 合并需求：target = 计划分支，sources = [各需求分支]
#   - 回推基线：target = 基线分支，sources = [计划分支]
# 每个方向都先出一份预览（哪些工程参与、各改了多少文件），由用户勾选后再真正合并。
# ---------------------------------------------------------------------------

# 解冲突可能要读不少代码，按一轮完整会话的量级给，和修推送同一个数量级。
GIT_MERGE_REPAIR_TIMEOUT_SECONDS = 20 * 60


def git_merge_resolved_ref(workspace: Path, branch: str, remote: str = "origin") -> str:
    """把分支名解析成本机此刻可用的引用，优先远端最新。

    合并要合的是「远端上那一版」，不是本机可能落后好几天的同名本地分支；
    远端没有这条分支时（纯本地分支、还没推过的计划分支）才退回本地引用。
    """
    value = str(branch or "").strip()
    if not valid_git_branch_name(value):
        raise BridgeFailure(f"分支名不合法：{branch}")
    remote_prefix = f"{remote}/"
    remote_side = value[len(remote_prefix):] if value.startswith(remote_prefix) else value
    remote_ref = f"{remote}/{remote_side}"
    if run_git(workspace, ["rev-parse", "--verify", "--quiet", f"refs/remotes/{remote_ref}"]).returncode == 0:
        return remote_ref
    if git_branch_exists(workspace, value):
        return value
    return ""


def git_merge_changed_files(workspace: Path, target_ref: str, source_ref: str) -> list[str]:
    """source 相对合并基准改了哪些文件。

    用三点 diff：只算源分支自己带来的改动，不把目标分支上别人的提交也算进来，
    否则计划分支越往后走，每条需求显示的文件数都会虚高。
    """
    completed = run_git(workspace, ["diff", "--name-only", f"{target_ref}...{source_ref}"], timeout=120)
    if completed.returncode != 0:
        return []
    return [line.strip() for line in (completed.stdout or "").splitlines() if line.strip()]


def git_merge_ahead_commits(workspace: Path, target_ref: str, source_ref: str) -> int:
    """source 上还没进 target 的提交数；为 0 表示这一条已经合过了。"""
    completed = run_git(workspace, ["rev-list", "--count", f"{target_ref}..{source_ref}"])
    if completed.returncode != 0:
        return 0
    return int((completed.stdout or "0").strip() or 0)


def git_merge_project_preview(
    workspace: Path, relative: str, target: str, sources: list[str], remote: str = "origin",
) -> dict[str, Any]:
    """单个工程的合并预览。读不动的工程只带回原因，不连累同级的其它工程。"""
    record: dict[str, Any] = {
        "path": relative,
        "name": relative or workspace.name,
        "workspace": str(workspace),
        "hasTarget": False,
        "targetRef": "",
        "dirty": False,
        "currentBranch": "",
        "changedFiles": 0,
        "sources": [],
        "error": "",
    }
    try:
        require_git_workspace(workspace)
        # 预览必须基于远端最新：拿本机过时的引用算出来的文件数会误导勾选。
        git_fetch_all(workspace, remote)
        record["currentBranch"] = git_current_branch(workspace)
        record["dirty"] = git_worktree_dirty(workspace)
        target_ref = git_merge_resolved_ref(workspace, target, remote)
        record["hasTarget"] = bool(target_ref)
        record["targetRef"] = target_ref
        changed_paths: set[str] = set()
        for source in sources:
            entry: dict[str, Any] = {
                "branch": source,
                "exists": False,
                "sourceRef": "",
                "changedFiles": 0,
                "commits": 0,
            }
            source_ref = git_merge_resolved_ref(workspace, source, remote)
            entry["exists"] = bool(source_ref)
            entry["sourceRef"] = source_ref
            if source_ref and target_ref:
                files = git_merge_changed_files(workspace, target_ref, source_ref)
                entry["changedFiles"] = len(files)
                entry["commits"] = git_merge_ahead_commits(workspace, target_ref, source_ref)
                changed_paths.update(files)
            record["sources"].append(entry)
        # 工程层面按去重后的文件数报：两条需求改同一个文件，勾选面板上不该显示成两个。
        record["changedFiles"] = len(changed_paths)
    except BridgeFailure as exc:
        record["error"] = str(exc)
    return record


def git_merge_preview(
    workspace: Path, target: str, sources: list[str], remote: str = "origin",
) -> dict[str, Any]:
    """根工作目录加一级子项目的合并预览，供合并弹窗按工程勾选。"""
    if not str(target or "").strip():
        raise BridgeFailure("缺少目标分支")
    branches = [str(value or "").strip() for value in sources if str(value or "").strip()]
    if not branches:
        raise BridgeFailure("缺少要合并的来源分支")
    projects = [git_merge_project_preview(workspace, "", target, branches, remote)]
    for child in git_subproject_workspaces(workspace):
        projects.append(git_merge_project_preview(child, child.name, target, branches, remote))
    return {"workspace": str(workspace), "target": target, "sources": branches, "projects": projects}


def build_git_merge_repair_prompt(
    workspace: Path, target: str, source: str, remote: str, failure: str, conflicts: list[str],
) -> str:
    """合并冲突时交给 AI 的提示词。只授权它解决这一次合并的冲突，不允许顺手改别的实现。"""
    return wrap_bridge_context(
        [
            "这是交付任务面板的「时间计划分支合并」回合：面板执行 git merge 时遇到冲突，"
            "仓库现在停在冲突状态，请你在本机把冲突解决掉并完成这次合并提交。",
            workspace_instruction(workspace),
            f"目标分支（当前所在分支）: {target}",
            f"来源分支: {source}",
            f"远端: {remote}",
            "",
            "冲突文件:",
            *([f"- {path}" for path in conflicts] or ["- （git 没有列出具体文件，请自行用 git status 确认）"]),
            "",
            "git merge 的原始输出:",
            failure,
            "",
            "处理要求:",
            f"- 仓库正处于 merge 冲突中，目标分支是 {target}，不要 --abort，也不要切到别的分支。",
            "- 逐个文件解决冲突：两边的真实意图都要保留，不要为了让命令通过就整块采用一侧或删掉别人的改动。",
            "- 冲突涉及业务逻辑时，先读双方改动所在的上下文再决定合并结果，必要时读相关实现文件。",
            "- 不要修改与本次冲突无关的文件，不要顺手重构。",
            "- 解决完执行 git add 与 git commit 完成这次合并提交；不要 push，推送由面板统一负责。",
            "- 解决不了（需要人工决策的业务取舍）就停下来说明卡在哪个文件、两边分别想做什么，不要瞎猜。",
            "- 回复里逐条列出：改了哪些文件、每个冲突各自采用了什么结论。面板会把这段说明原样展示给用户。",
        ],
        f"合并 {source} 到 {target} 时发生冲突，请解决冲突并完成合并提交。",
    )


def git_merge_conflict_files(workspace: Path) -> list[str]:
    """当前处于冲突状态的文件清单。"""
    completed = run_git(workspace, ["diff", "--name-only", "--diff-filter=U"])
    if completed.returncode != 0:
        return []
    return [line.strip() for line in (completed.stdout or "").splitlines() if line.strip()]


def git_merge_in_progress(workspace: Path) -> bool:
    """仓库是不是还停在一次没收尾的合并里。

    用 rev-parse 问 git 而不是看 .git/MERGE_HEAD 这个路径：子模组的 .git 是一个指向别处的
    文件，按路径拼永远判不出来，会把停在冲突里的子项目当成干净仓库继续往下走。
    """
    if run_git(workspace, ["rev-parse", "--verify", "--quiet", "MERGE_HEAD"]).returncode == 0:
        return True
    return bool(git_merge_conflict_files(workspace))


def git_merge_one(
    workspace: Path, target: str, source_ref: str, source_label: str,
) -> dict[str, Any]:
    """把一条来源分支合进当前已经切好的目标分支。

    返回 conflict=True 时仓库仍停在冲突状态，交给调用方决定是否起 AI 来解；
    这里不 --abort，abort 掉就没法把冲突现场交给 AI 了。
    """
    record: dict[str, Any] = {
        "branch": source_label,
        "merged": False,
        "upToDate": False,
        "conflict": False,
        "conflictFiles": [],
        "output": "",
    }
    if git_merge_ahead_commits(workspace, target, source_ref) == 0:
        record["upToDate"] = True
        return record
    message = f"Merge branch '{source_label}' into {target}"
    completed = run_git(workspace, ["merge", "--no-ff", "-m", message, source_ref], timeout=300)
    output = (completed.stdout or "").strip()
    record["output"] = output[-2000:]
    if completed.returncode == 0:
        record["merged"] = True
        return record
    conflicts = git_merge_conflict_files(workspace)
    if not conflicts and not git_merge_in_progress(workspace):
        raise BridgeFailure(f"合并 {source_label} 到 {target} 失败：{output or 'git 退出异常'}")
    record["conflict"] = True
    record["conflictFiles"] = conflicts
    return record


def git_repository_url_of(value: Any) -> str:
    """关联远端只接受完整的仓库地址；带空白、换行或以 - 开头的输入直接拒绝。"""
    url = str(value or "").strip()
    if not url:
        raise BridgeFailure("请先填写 Git 仓库地址")
    if len(url) > 512 or any(char.isspace() for char in url) or url.startswith("-"):
        raise BridgeFailure(f"Git 仓库地址不合法：{url}")
    if not GIT_REPOSITORY_URL_RE.fullmatch(url):
        raise BridgeFailure(f"Git 仓库地址不合法：{url}")
    return url


def git_initializable_workspace_of(value: Any) -> Path:
    """关联前目录可以还不存在：父目录必须已存在，缺的那一层由这里补上。"""
    raw = str(value or "").strip()
    if not raw:
        raise BridgeFailure("未提供 Codex 工作目录，请先在项目管理中确认当前项目的工作目录")
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        raise BridgeFailure("Codex 工作目录必须是绝对路径")
    if candidate.exists():
        return workspace_path_of(candidate)
    parent = candidate.parent
    if not parent.is_dir():
        raise BridgeFailure(f"上级目录不存在：{parent}")
    try:
        candidate.mkdir()
    except OSError as exc:
        raise BridgeFailure(f"创建项目工作目录失败：{exc}") from exc
    return workspace_path_of(candidate)


def git_workspace_check(value: Any) -> dict[str, Any]:
    """给「项目偏好设置」判断这个目录要不要初始化 Git，本身不写任何东西。"""
    raw = str(value or "").strip()
    if not raw:
        raise BridgeFailure("未提供 Codex 工作目录，请先在项目管理中确认当前项目的工作目录")
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        raise BridgeFailure("Codex 工作目录必须是绝对路径")
    resolved = candidate.resolve()
    if not resolved.is_dir():
        return {
            "workspace": str(resolved),
            "exists": False,
            "isGitRepository": False,
            "repositoryRoot": "",
            "remoteName": "origin",
            "remoteConfigured": False,
            "empty": False,
            "pendingSubmodules": [],
        }
    inside, _ = git_workspace_probe(resolved)
    if not inside:
        return {
            "workspace": str(resolved),
            "exists": True,
            "isGitRepository": False,
            "repositoryRoot": "",
            "remoteName": "origin",
            "remoteConfigured": False,
            "empty": not any(resolved.iterdir()),
            "pendingSubmodules": [],
        }
    root = run_git(resolved, ["rev-parse", "--show-toplevel"])
    return {
        "workspace": str(resolved),
        "exists": True,
        "isGitRepository": True,
        "repositoryRoot": (root.stdout or "").strip().splitlines()[-1].strip() if root.returncode == 0 else "",
        "remoteName": "origin",
        # 远端地址可能带内嵌凭据，只回传是否已配置。
        "remoteConfigured": bool(git_remote_url(resolved, "origin")),
        "empty": False,
        # 目录早就是仓库、但子模块还是空的，也要能在偏好设置里补一次初始化。
        "pendingSubmodules": git_pending_submodules(resolved),
    }


def git_adopt_remote_branch(workspace: Path, branch: str, remote: str) -> None:
    """目录里已有文件、检出会被拒时的退路。

    索引对齐远端提交，本地已有的同名文件原样留成未提交改动；
    本地缺的那些文件再从索引检出来，这样远端内容仍然完整落到磁盘上，且不覆盖任何本地文件。
    """
    git_output(workspace, ["branch", "--force", branch, f"{remote}/{branch}"], "创建本地分支失败")
    git_output(workspace, ["symbolic-ref", "HEAD", f"refs/heads/{branch}"], "切换本地分支失败")
    git_output(workspace, ["reset", "--mixed"], "对齐远端提交失败", timeout=120)
    run_git(workspace, ["branch", "--set-upstream-to", f"{remote}/{branch}", branch])
    missing = [
        line for line in git_output(workspace, ["ls-files", "-z", "--deleted"], "读取缺失文件失败", timeout=120).split("\0")
        if line
    ]
    # 一次全塞进命令行可能超出系统参数上限，按批检出。
    for start in range(0, len(missing), 200):
        git_output(workspace, ["checkout", "--", *missing[start:start + 200]], "检出远端文件失败", timeout=300)


def git_pending_submodules(workspace: Path) -> list[str]:
    """.gitmodules 里登记了、但本机还没检出内容的子模块路径。

    git submodule status 用行首的 - 标记「还没初始化」，这里只认这个标记，
    不去猜目录空不空——子模块目录本来就可能有被忽略的构建产物。
    """
    if not (workspace / ".gitmodules").is_file():
        return []
    completed = run_git(workspace, ["submodule", "status", "--recursive"], timeout=120)
    if completed.returncode != 0:
        return []
    pending: list[str] = []
    for line in (completed.stdout or "").splitlines():
        if not line.startswith("-"):
            continue
        parts = line[1:].strip().split()
        if len(parts) >= 2:
            pending.append(parts[1])
    return pending


def git_initialize_submodules(workspace: Path) -> dict[str, Any]:
    """把 .gitmodules 里还没初始化的子模块一并拉下来。

    子模块失败不该把主仓库的初始化算作失败：主仓库已经可用了，这里只把原因带回去。
    """
    pending = git_pending_submodules(workspace)
    if not pending:
        return {"submodules": [], "submoduleError": ""}
    completed = run_git(workspace, ["submodule", "update", "--init", "--recursive"], timeout=1800)
    if completed.returncode != 0:
        return {
            "submodules": pending,
            "submoduleError": (completed.stdout or "").strip() or "git 退出异常",
        }
    remaining = git_pending_submodules(workspace)
    return {
        "submodules": [path for path in pending if path not in remaining],
        "submoduleError": (
            f"以下子模块仍未初始化：{'、'.join(remaining)}" if remaining else ""
        ),
    }


def git_initialize_workspace(
    workspace: Path,
    repository_url: str,
    remote: str = "origin",
    base_branch: str = "",
) -> dict[str, Any]:
    """把还不是 Git 仓库的项目目录关联到远端：init + remote + fetch + 检出默认分支。

    目录里已有文件时不覆盖：改成把索引对齐到远端提交，本地文件留作未提交改动，
    由用户自己决定提交还是丢弃。中途失败会把这一步刚建出来的 .git 删掉，方便改地址重试。
    """
    url = git_repository_url_of(repository_url)
    if not valid_git_remote_name(remote):
        raise BridgeFailure("Git 远端名称不合法")
    if base_branch and not valid_git_branch_name(base_branch):
        raise BridgeFailure("基准分支名不合法")
    inside, _ = git_workspace_probe(workspace)
    if inside:
        raise BridgeFailure(f"项目工作目录已经是 Git 仓库：{workspace}")
    git_directory = workspace / ".git"
    created_git_directory = False
    try:
        git_output(workspace, ["init"], "初始化 Git 仓库失败")
        created_git_directory = git_directory.exists()
        git_output(workspace, ["remote", "add", remote, url], "关联 Git 远端失败")
        # 首次关联要把整个仓库拉下来，网络耗时远超普通 Git 命令。
        git_output(workspace, ["fetch", "--prune", remote], "拉取远端仓库失败", timeout=900)
        run_git(workspace, ["remote", "set-head", remote, "-a"], timeout=60)
        branch = base_branch.strip()
        if branch and run_git(workspace, ["rev-parse", "--verify", "--quiet", f"{remote}/{branch}^{{commit}}"]).returncode != 0:
            raise BridgeFailure(f"远端仓库没有基准分支：{branch}")
        if not branch:
            head = run_git(workspace, ["symbolic-ref", "--quiet", "--short", f"refs/remotes/{remote}/HEAD"])
            candidate = (head.stdout or "").strip() if head.returncode == 0 else ""
            prefix = f"{remote}/"
            branch = candidate[len(prefix):] if candidate.startswith(prefix) else ""
        if not branch:
            for candidate in ("main", "master", "develop"):
                if run_git(workspace, ["rev-parse", "--verify", "--quiet", f"{remote}/{candidate}^{{commit}}"]).returncode == 0:
                    branch = candidate
                    break
        if not branch:
            raise BridgeFailure("远端仓库没有可检出的分支，请确认仓库地址是否正确")
        adopted = run_git(workspace, ["checkout", "-b", branch, "--track", f"{remote}/{branch}"], timeout=300).returncode != 0
        if adopted:
            git_adopt_remote_branch(workspace, branch, remote)
    except BaseException:
        # 只删这一步自己建出来的 .git，工作目录里原有的文件一个都不动。
        if created_git_directory and git_directory.is_dir():
            shutil.rmtree(git_directory, ignore_errors=True)
        raise
    # 主仓库已经能用了，子模块是附带的一步：失败只回传原因，不回滚 .git。
    submodules = git_initialize_submodules(workspace)
    return {
        "workspace": str(workspace),
        "initialized": True,
        "branch": branch,
        "remoteName": remote,
        "adopted": adopted,
        "status": git_workspace_status(workspace, url, remote),
        **submodules,
    }
