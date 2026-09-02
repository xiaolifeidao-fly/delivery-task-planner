"""需求分支：建分支、提交推送、按时间计划合并，以及失败后交给执行器修。
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from typing import Any

import server as planner

from delivery_bridge.clients import factory
from delivery_bridge.errors import BridgeFailure
from delivery_bridge.executor_env import codex_environment
from delivery_bridge.git_ops import (
    GIT_MERGE_REPAIR_TIMEOUT_SECONDS,
    GIT_PUSH_REPAIR_TIMEOUT_SECONDS,
    build_git_merge_repair_prompt,
    build_git_push_repair_prompt,
    git_branch_exists,
    git_branch_synced,
    git_checkout_reference,
    git_commit_message_of,
    git_current_branch,
    git_default_remote,
    git_merge_in_progress,
    git_merge_one,
    git_merge_resolved_ref,
    git_prepare_branch_targets,
    git_pull_branch,
    git_push_branch,
    git_subproject_targets_of,
    git_subproject_workspace_of,
    git_worktree_dirty,
    require_git_workspace,
    run_git,
    valid_git_branch_name,
)
from delivery_bridge.payloads import request_scoped_config
from delivery_bridge.providers import (
    ai_provider_of,
    fast_mode_of,
    program_id_of,
    provider_label,
    reasoning_effort_of,
)
from delivery_bridge.turn_output import execution_output, final_agent_text_from_output


class GitMixin:
    def _ensure_requirement_git_branch(self, config: dict[str, Any], program_id: int, task: dict[str, Any]) -> str:
        """任务所属需求关联了 Git 分支时，验证工作目录已由用户确认切换。

        自动切分支会在多人和脏工作区场景中吞掉重要上下文，因此这里不再产生副作用；
        用户必须先在需求列表的 Git 检查里确认提交、暂存或直接切换。
        """
        requirement_key = str(task.get("requirementKey") or "").strip()
        if not requirement_key:
            return ""
        try:
            requirement = planner.request_api(
                config,
                "GET",
                "/delivery/requirement",
                query={"programId": program_id, "requirementKey": requirement_key},
            )
        except planner.ToolFailure as exc:
            raise BridgeFailure(f"读取需求 Git 设置失败：{exc}") from exc
        if not isinstance(requirement, dict) or not requirement.get("gitEnabled"):
            return ""
        branch = str(requirement.get("gitBranch") or "").strip()
        if not branch:
            return ""
        if not valid_git_branch_name(branch):
            raise BridgeFailure(f"需求关联的分支名不合法：{branch}")
        require_git_workspace(self.workspace)
        if not git_branch_exists(self.workspace, branch):
            raise BridgeFailure(f"本机不存在需求分支 {branch}，请先在需求窗口创建分支")
        current = git_current_branch(self.workspace)
        if current == branch:
            return branch
        raise BridgeFailure(
            f"当前项目位于分支 {current or 'HEAD'}，与需求分支 {branch} 不一致；请先在需求列表执行 Git 检查并确认切换"
        )

    def prepare_requirement_git_branch(self, raw: Any) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise BridgeFailure("请求体必须是 JSON 对象")
        branch = str(raw.get("branch") or "").strip()
        if not branch:
            raise BridgeFailure("缺少需求分支")
        with self.lock:
            busy = sorted(key for _, _, key in self.active)
        if busy:
            raise BridgeFailure(f"本机仍有任务在执行（{', '.join(busy)}），不能切换项目分支")
        remote = str(raw.get("remoteName") or "origin").strip() or "origin"
        # 没传 targets 的调用方（需求列表的分支检查）也要带上子项目，否则只有根目录跟着切。
        targets = git_subproject_targets_of(self.workspace, raw.get("targets"), branch, remote)
        return git_prepare_branch_targets(
            self.workspace,
            branch,
            str(raw.get("strategy") or "switch").strip(),
            str(raw.get("commitMessage") or ""),
            str(raw.get("expectedRemoteUrl") or ""),
            remote,
            targets,
        )

    def push_requirement_branch(self, raw: Any, config: dict[str, Any] | None = None) -> dict[str, Any]:
        """需求窗口的「推送到 Git」：先推子项目，最后提交并推送主项目。"""
        if not isinstance(raw, dict):
            raise BridgeFailure("请求体必须是 JSON 对象")
        program_id = program_id_of(raw.get("programId"))
        if not program_id:
            raise BridgeFailure("缺少项目标识")
        branch = str(raw.get("branch") or "").strip()
        message = str(raw.get("message") or "")
        provider = ai_provider_of(raw)
        commit_only = bool(raw.get("commitOnly"))
        # 子项目的改动也是这条需求的产物：不带上它们，推完远端仍然缺一半代码。
        targets = git_subproject_targets_of(self.workspace, raw.get("targets"), branch)
        # 子项目必须先落提交：submodule 的新 commit 会表现为主项目里的 gitlink 改动，
        # 主项目最后提交才能把这个指针一并带上。单个子项目失败仍继续其它项目和主项目。
        child_records = self._push_subproject_branches(branch, message, targets, push=not commit_only)
        if commit_only:
            # 仅提交是本机动作，失败原因基本是工作区自身的问题，不值得再起一轮 AI 去修。
            result = git_push_branch(self.workspace, branch, message, push=False)
            result["repaired"] = False
            result["results"] = self._push_branch_results(result, branch, child_records)
            return result
        try:
            result = git_push_branch(self.workspace, branch, message)
            result["repaired"] = False
            result["results"] = self._push_branch_results(result, branch, child_records)
            return result
        except BridgeFailure as exc:
            failure = str(exc)
        config = request_scoped_config(config, "", program_id)
        summary, status = self._repair_git_push(
            config,
            program_id,
            branch,
            git_commit_message_of(message, branch),
            failure,
            provider,
            str(raw.get("model") or "").strip(),
            reasoning_effort_of(raw, provider),
            fast_mode_of(raw, provider),
        )
        remote = git_default_remote(self.workspace)
        # 以仓库的真实状态判定成功与否，不采信 AI 的结论。
        if not git_branch_synced(self.workspace, branch, remote):
            raise BridgeFailure(f"推送失败，{provider_label(provider)} 也没能解决：{failure}\n\n处理说明：{summary or '无'}")
        repaired = {
            "pushed": True,
            "branch": branch,
            "remote": remote,
            "committed": True,
            "commitMessage": "",
            "upToDate": False,
            "synced": "repaired",
            "repaired": True,
            "repairStatus": status,
            "repairSummary": summary,
            "output": failure,
        }
        # 子项目已经在主项目之前处理完成；AI 只修主项目，不能再把子项目重复推一轮。
        repaired["results"] = self._push_branch_results(repaired, branch, child_records)
        return repaired

    def _push_subproject_branches(
        self,
        branch: str,
        message: str,
        targets: list[str],
        push: bool = True,
    ) -> list[dict[str, Any]]:
        """按选择顺序先提交、推送各子项目，并返回各自结果。

        子项目失败只记录原因，不打断其它子项目：一个工程推不动不该让别的也停在本机。
        """
        records: list[dict[str, Any]] = []
        for relative in targets:
            record: dict[str, Any] = {
                "path": relative,
                "name": relative,
                "branch": branch,
                "pushed": False,
                "committed": False,
                "upToDate": False,
                "skipped": False,
                "error": "",
            }
            try:
                child = git_subproject_workspace_of(self.workspace, relative)
                if child == self.workspace.resolve():
                    continue
                # 子项目本机没有这条需求分支时，提交推送它自己当前所处的分支：多工程工作目录里
                # 每个工程有自己的分支节奏，不该因为分支名对不上就把这一轮的改动留在本机。
                child_branch = branch if git_branch_exists(child, branch) else git_current_branch(child)
                record["branch"] = child_branch
                # 游离 HEAD 没有可推送的分支，跳过并如实标出来，不替用户猜该推到哪。
                if not child_branch:
                    record["skipped"] = True
                else:
                    child_result = git_push_branch(child, child_branch, message, push=push)
                    record["pushed"] = bool(child_result.get("pushed"))
                    record["committed"] = bool(child_result.get("committed"))
                    record["upToDate"] = bool(child_result.get("upToDate"))
            except BridgeFailure as exc:
                record["error"] = str(exc)
            records.append(record)
        return records

    def _push_branch_results(
        self,
        root: dict[str, Any],
        branch: str,
        children: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """结果展示仍保持主项目在第一行，但真实执行顺序是 children → root。"""
        return [{
            "path": "",
            "name": self.workspace.name,
            "branch": str(root.get("branch") or branch),
            "pushed": bool(root.get("pushed")),
            "committed": bool(root.get("committed")),
            "upToDate": bool(root.get("upToDate")),
            "skipped": False,
            "error": "",
        }, *children]

    def merge_time_plan_branches(self, raw: Any, config: dict[str, Any] | None = None) -> dict[str, Any]:
        """分支合并：把若干来源分支合进目标分支，冲突交给 AI 解，最后推送。

        时间计划的三个方向（回合基线 / 合并需求分支 / 回推基线）和需求窗口的「合并到分支」
        都走这里，只是 target 和 sources 不同。
        执行顺序是「先子项目、最后根工作目录」：子模组的新提交在根仓库里表现为 gitlink，
        根仓库最后推才能把指针一并带上。单个工程失败只记在结果里，不回滚已经合好的工程 ——
        把已完成的部分撤掉比留着更难收拾。
        """
        if not isinstance(raw, dict):
            raise BridgeFailure("请求体必须是 JSON 对象")
        program_id = program_id_of(raw.get("programId"))
        if not program_id:
            raise BridgeFailure("缺少项目标识")
        target = str(raw.get("target") or "").strip()
        if not target:
            raise BridgeFailure("缺少目标分支")
        sources = [str(value or "").strip() for value in (raw.get("sources") or []) if str(value or "").strip()]
        if not sources:
            raise BridgeFailure("缺少要合并的来源分支")
        remote = str(raw.get("remoteName") or "origin").strip() or "origin"
        push = raw.get("push") is not False
        provider = ai_provider_of(raw)
        model = str(raw.get("model") or "").strip()
        reasoning_effort = reasoning_effort_of(raw, provider)
        fast_mode = fast_mode_of(raw, provider)
        # 合并会切分支、改工作区文件，本机还有任务在跑时不能动。
        with self.lock:
            busy = sorted(key for _, _, key in self.active)
        if busy:
            raise BridgeFailure(f"本机仍有任务在执行（{', '.join(busy)}），不能合并分支")
        # 勾选哪些子项目由合并弹窗说了算，不做「没传就全合」的猜测。
        targets = git_subproject_targets_of(self.workspace, raw.get("targets") or [])
        skip_root = bool(raw.get("skipRoot"))
        config = request_scoped_config(config, "", program_id)

        records: list[dict[str, Any]] = []
        for relative in targets:
            child = git_subproject_workspace_of(self.workspace, relative)
            if child == self.workspace.resolve():
                continue
            records.append(self._merge_one_project(
                child, relative, target, sources, remote, push,
                config, program_id, provider, model, reasoning_effort, fast_mode,
            ))
        if not skip_root:
            records.insert(0, self._merge_one_project(
                self.workspace, "", target, sources, remote, push,
                config, program_id, provider, model, reasoning_effort, fast_mode,
            ))
        return {
            "target": target,
            "sources": sources,
            "remote": remote,
            "pushed": push and all(record["pushed"] or record["skipped"] for record in records),
            # 只要有一个工程没成，面板就要把它标出来，不能因为整体 200 就当作全合上了。
            "failed": [record["name"] for record in records if record["error"]],
            "results": records,
        }

    def _merge_one_project(
        self,
        workspace: Path,
        relative: str,
        target: str,
        sources: list[str],
        remote: str,
        push: bool,
        config: dict[str, Any],
        program_id: int,
        provider: str,
        model: str,
        reasoning_effort: str,
        fast_mode: bool,
    ) -> dict[str, Any]:
        """一个工程里的完整合并：切目标分支 → 逐条合来源 → 冲突交 AI → 推送。"""
        record: dict[str, Any] = {
            "path": relative,
            "name": relative or workspace.name,
            "branch": target,
            "merged": [],
            "resolutions": [],
            "pushed": False,
            "skipped": False,
            "error": "",
        }
        try:
            require_git_workspace(workspace)
            if git_merge_in_progress(workspace):
                raise BridgeFailure("上一次合并还没收尾（仓库仍处于 merge 状态），请先在本机处理完再重试")
            if git_worktree_dirty(workspace):
                raise BridgeFailure("工作目录有未提交改动，无法合并，请先提交或暂存")
            target_ref = git_merge_resolved_ref(workspace, target, remote)
            if not target_ref:
                # 这个工程没有目标分支：多工程工作目录里不是每个工程都参与这条计划。
                record["skipped"] = True
                return record
            # 切到目标分支并拉到远端最新，再往上合，避免合到过时的基础上。
            local, _ = git_checkout_reference(workspace, target, remote)
            record["branch"] = local
            git_pull_branch(workspace, local, remote)
            merged_any = False
            for source in sources:
                source_ref = git_merge_resolved_ref(workspace, source, remote)
                if not source_ref:
                    # 这个工程里没有这条来源分支：不是每个工程都参与每条需求，不算失败。
                    record["merged"].append({
                        "branch": source, "merged": False, "upToDate": False,
                        "conflict": False, "missing": True, "conflictFiles": [], "output": "",
                    })
                    continue
                outcome = git_merge_one(workspace, local, source_ref, source)
                outcome["missing"] = False
                if outcome["conflict"]:
                    summary, status = self._resolve_git_merge_conflict(
                        workspace, config, program_id, local, source, remote,
                        outcome["output"], outcome["conflictFiles"],
                        provider, model, reasoning_effort, fast_mode,
                    )
                    record["resolutions"].append({
                        "project": record["name"],
                        "branch": source,
                        "files": outcome["conflictFiles"],
                        "status": status,
                        "summary": summary,
                    })
                    # 以仓库的真实状态判定，不采信 AI 的自述：合并没收尾就是没解决。
                    if git_merge_in_progress(workspace):
                        run_git(workspace, ["merge", "--abort"], timeout=120)
                        raise BridgeFailure(
                            f"合并 {source} 到 {local} 的冲突，{provider_label(provider)} 也没能解决，"
                            f"已回滚这次合并。冲突文件：{', '.join(outcome['conflictFiles']) or '未知'}。"
                            f"处理说明：{summary or '无'}"
                        )
                    outcome["conflict"] = False
                    outcome["resolved"] = True
                    outcome["merged"] = True
                if outcome["merged"]:
                    merged_any = True
                record["merged"].append(outcome)
            if not push:
                return record
            if not merged_any and git_branch_synced(workspace, local, remote):
                # 没有新提交，也没有落后远端：这个工程本来就是最新的，不必再推一次。
                record["pushed"] = True
                return record
            completed = run_git(workspace, ["push", "--set-upstream", remote, f"{local}:{local}"], timeout=300)
            if completed.returncode != 0:
                raise BridgeFailure(
                    f"推送分支 {local} 失败：{(completed.stdout or '').strip() or 'git 退出异常'}"
                )
            record["pushed"] = True
        except BridgeFailure as exc:
            record["error"] = str(exc)
        return record

    def _resolve_git_merge_conflict(
        self,
        workspace: Path,
        config: dict[str, Any],
        program_id: int,
        target: str,
        source: str,
        remote: str,
        failure: str,
        conflicts: list[str],
        provider: str,
        model: str,
        reasoning_effort: str,
        fast_mode: bool,
    ) -> tuple[str, str]:
        """起一轮 AI 会话专门解这一次合并冲突，返回它对「解决了什么」的说明。

        超时就掐掉进程，不让 HTTP 请求无限期挂着；调用方随后用仓库状态复核结果。
        """
        client = factory.create_ai_client(provider, workspace, None, codex_environment(config, program_id))
        try:
            thread_id, turn_id = client.start_task(
                f"解决 {source} 合并到 {target} 的冲突",
                build_git_merge_repair_prompt(workspace, target, source, remote, failure, conflicts),
                None,
                model,
                reasoning_effort=reasoning_effort,
                fast_mode=fast_mode,
            )
            outcome: dict[str, str] = {}

            def wait() -> None:
                try:
                    outcome["status"] = client.wait_turn(turn_id)
                except Exception as exc:
                    print(f"解合并冲突等待失败：{exc}", file=sys.stderr, flush=True)

            waiter = threading.Thread(target=wait, daemon=True)
            waiter.start()
            waiter.join(GIT_MERGE_REPAIR_TIMEOUT_SECONDS)
            if waiter.is_alive():
                return "", "timeout"
            status = outcome.get("status") or "failed"
            turn = client.read_turn(thread_id, turn_id, client.next_request_id())
            return final_agent_text_from_output(execution_output(status, turn)), status
        finally:
            client.close()

    def _repair_git_push(
        self,
        config: dict[str, Any],
        program_id: int,
        branch: str,
        commit_message: str,
        failure: str,
        provider: str,
        model: str,
        reasoning_effort: str,
        fast_mode: bool,
    ) -> tuple[str, str]:
        """起一轮 AI 会话专门修推送。超时就掐掉进程，不让 HTTP 请求无限期挂着。"""
        remote = git_default_remote(self.workspace)
        client = factory.create_ai_client(provider, self.workspace, None, codex_environment(config, program_id))
        try:
            thread_id, turn_id = client.start_task(
                f"推送需求分支 {branch}",
                build_git_push_repair_prompt(self.workspace, branch, remote, failure, commit_message),
                None,
                model,
                reasoning_effort=reasoning_effort,
                fast_mode=fast_mode,
            )
            outcome: dict[str, str] = {}

            def wait() -> None:
                try:
                    outcome["status"] = client.wait_turn(turn_id)
                except Exception as exc:
                    print(f"修推送等待失败：{exc}", file=sys.stderr, flush=True)

            waiter = threading.Thread(target=wait, daemon=True)
            waiter.start()
            waiter.join(GIT_PUSH_REPAIR_TIMEOUT_SECONDS)
            if waiter.is_alive():
                return "", "timeout"
            status = outcome.get("status") or "failed"
            turn = client.read_turn(thread_id, turn_id, client.next_request_id())
            return final_agent_text_from_output(execution_output(status, turn)), status
        finally:
            client.close()
