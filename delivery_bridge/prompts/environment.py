"""预设环境安装的提示词。

装的是本机全局环境，cwd 用运行时目录下的空目录，不落进业务仓库。
探测与安装命令由 environments 给出，这里只负责把它们组织成一段话。
"""

from __future__ import annotations

from typing import Any

from .. import hostinfo
from ..environments import GIT_PRESET, environment_command_for
from ..prompt_context import wrap_bridge_context
from ..workspaces import environment_setup_workspace, workspace_path_of

def build_environment_setup_prompt(
    use_git: bool, environments: list[dict[str, Any]], message: str, first_turn: bool, host: str = "",
) -> str:
    """项目偏好「预设环境」的提示词：先检测，只补装缺的，装完把版本核一遍。

    macOS 和 Windows 的命令名、包管理器、权限模型都不一样，所以清单按本机系统生成，
    只把该系统那一套命令写进去，不给执行器留自由发挥的余地。
    """
    host = host or hostinfo.host_platform()
    label = hostinfo.host_platform_label(host)
    privilege = "管理员" if host == "windows" else "sudo"
    if not first_turn:
        return wrap_bridge_context(
            [
                f"这是「预设环境」会话的续聊，本机是 {label}，继续按既定顺序把本机全局环境补齐。",
                "已经装好并且版本达标的环境不要重装、不要升级、不要改用户已有的版本管理器配置。",
                "每装完一项都要确认它的可执行文件目录已经持久化写进本机 PATH 环境变量（新开终端仍然生效），"
                "Git、Node.js、Python 尤其要逐个核对；缺了就补写，补完新开终端复检。",
                f"需要 {privilege} 权限的命令，如果当前拿不到权限，就把命令原样交给用户执行，然后等用户回话。",
                "本上下文标记闭合之后的内容，是用户本轮说的话。",
            ],
            message,
        )
    checklist = []
    if use_git:
        checklist.append(
            f"- Git：先执行 `{environment_command_for(GIT_PRESET, 'probe', host)}` 检测；未安装才装"
            f"（{environment_command_for(GIT_PRESET, 'install', host)}）。"
            "装好后顺带确认 `git config --global user.name` 与 `git config --global user.email` 是否已配置，"
            "缺了就问用户要，不要自己编。"
            "随后检查 `~/.ssh/config` 中 `Host github.com` 的 `IdentityFile`，且对应 `.pub` 文件必须是有效 SSH 公钥。"
            "已有有效 GitHub 密钥时不要重建、不要覆盖；没有有效配置时，生成新的 ed25519 密钥对"
            " `~/.ssh/id_ed25519_github_delivery_task_planner`，并在配置文件最前面写入带"
            " `delivery-task-planner GitHub SSH key` 标记的 `Host github.com` 配置块。"
            "绝不读取、展示或输出私钥；最后只输出公钥，并明确提示用户将它添加到 GitHub 账户的 SSH keys。"
        )
    for entry in environments:
        probe = environment_command_for(entry, "probe", host)
        install = environment_command_for(entry, "install", host)
        probe_text = f"`{probe}`" if probe else f"该环境在 {label} 上对应的版本命令"
        requirement = f"版本要求 {entry['requirement']}" if entry.get("requirement") else "版本由用户在偏好设置里自定义，按字面理解"
        install_text = f"（{label} 上装：{install}）" if install else f"（自定义项，按 {label} 的常规装法安装）"
        checklist.append(
            f"- {entry['label']}：先执行 {probe_text} 检测，{requirement}。"
            f"低于要求或没装才安装/升级到满足要求的版本{install_text}。"
        )
    if host == "windows":
        platform_rules = [
            "7. 命令用 PowerShell 执行；包管理器优先 winget，没有 winget 再退到 scoop / choco 或官网安装包，并把选择理由说清楚。",
            "8. 装完要开新的 PowerShell 会话或先刷新 PATH（`$env:Path = [System.Environment]::GetEnvironmentVariable('Path','Machine') "
            "+ ';' + [System.Environment]::GetEnvironmentVariable('Path','User')`）再复检，"
            "否则复检读到的是旧 PATH，会把装好的环境误判成没装上。",
            "9. Windows 的 PATH 必须写进持久化的环境变量，不能只用 `set` 或 `$env:Path=`（那只对当前会话有效）："
            "用 `[System.Environment]::SetEnvironmentVariable('Path', $新值, 'User')` 追加（拿得到管理员权限时才用 'Machine'），"
            "$新值 由先读出的原值加分号拼上新目录得到，重复的目录不要再加。"
            "常见目录：Git 是 `C:\\Program Files\\Git\\cmd`，Node.js 是 `C:\\Program Files\\nodejs`（npm 全局包在 `%APPDATA%\\npm`），"
            "Python 是安装目录本身和它下面的 `Scripts`（用 winget 装的一般在 `%LOCALAPPDATA%\\Programs\\Python\\PythonXXX`）；"
            "实际目录以 `where.exe git`、`where.exe node`、`py -3 -c \"import sys;print(sys.prefix)\"` 的真实输出为准，不要照抄路径。",
            "10. Windows 上 Python 的命令是 `py -3` 或 `python`，没有 `python3`；"
            "如果 `python` 打开的是微软商店占位程序，说明真实 Python 目录没进 PATH 或被商店别名挡住，"
            "要在「应用执行别名」里关掉 python.exe / python3.exe 的别名并把真实目录加进 PATH。"
            "winget 触发 UAC 弹窗时当前会话无法确认，直接把命令交给用户以管理员身份运行。",
        ]
    elif host == "macos":
        platform_rules = [
            "7. 包管理器用 Homebrew；没有 brew 就先把官方安装命令交给用户，或退到官网安装包，并把选择理由说清楚。",
            "8. Apple Silicon 的 brew 前缀是 /opt/homebrew、Intel 是 /usr/local；装完 `which` 找不到命令时，"
            "先确认对应的 bin 目录在 PATH 里再判定失败。"
            "需要补 PATH 时写进用户默认 shell 的配置文件（zsh 是 `~/.zshrc`，bash 是 `~/.bash_profile`），"
            "以 `export PATH=\"<新目录>:$PATH\"` 追加，写之前先确认文件里还没有同一行，然后 `source` 或新开终端复检。",
            "9. 不要用 sudo 跑 brew。",
        ]
    else:
        platform_rules = [
            "7. 按发行版选包管理器（Debian/Ubuntu 用 apt，RHEL/CentOS 用 yum/dnf）；"
            "官方源版本低于要求时改用官网安装包或版本管理器，并把选择理由说清楚。",
            "8. 需要补 PATH 时写进用户默认 shell 的配置文件（`~/.bashrc` 或 `~/.zshrc`），"
            "以 `export PATH=\"<新目录>:$PATH\"` 追加，写之前先确认没有重复行，然后新开终端复检。",
        ]
    return wrap_bridge_context(
        [
            "这是交付任务面板「项目管理 → 偏好设置 → 高级设置 → 预设环境」发起的一次本机环境预设。",
            "它装的是本机全局环境，不属于任何项目：不要读取、修改或提交任何业务仓库的代码，"
            "也不要执行任务面板的任务拆解、执行或测试命令。",
            f"本机系统是 {label}，下面的命令已经按 {label} 给好了，照着执行，不要换成别的系统那一套。",
            f"本轮 cwd 是一个专用空目录：{environment_setup_workspace()}；只在需要落临时文件时用它。",
            "只做下面这份清单，逐项先检测再动手，并把执行过的命令和真实输出讲清楚：",
            *checklist,
            "硬约束：",
            "1. 只装缺的。检测到已安装且版本满足要求的，直接跳过并说明当前版本，"
            "绝不重装、降级或顶掉用户已有的版本管理器（nvm / nvm-windows / pyenv / asdf / conda 等）配置。",
            "2. 全局安装，不要建项目级虚拟环境。",
            f"3. 需要 {privilege} 权限而当前拿不到时不要硬闯，把命令原样交给用户执行，然后等用户回话。",
            "4. 装完再跑一次检测命令核对版本，用一个表格列出每项环境的「安装前状态 / 处理动作 / 安装后版本」。",
            "5. 每一项装完都必须把它的可执行文件目录持久化写进本机 PATH 环境变量，"
            "Git、Node.js、Python 三项无论是不是本轮新装的，都要逐个确认 PATH 里有；"
            "npm 全局 bin、Python 的 Scripts/bin 这类附带目录同样要在 PATH 里。"
            "只在当前会话里临时设置不算数，必须新开一个终端复检命令仍然可用。"
            "追加 PATH 前先读出原值，只做追加和去重，绝不整体覆盖用户已有的 PATH。",
            "6. 清单以外的环境一律不装。",
            *platform_rules,
            "最终回复末尾单独给出「下一步」，写清还需要用户自己动手的事项；全部就绪就明说无需额外操作。",
            "本上下文标记闭合之后的内容，是用户本轮补充的说明。",
        ],
        message,
    )
