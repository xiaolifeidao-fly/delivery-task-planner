#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
plugin_root="$(cd "${script_dir}/.." && pwd)"
install_root="${HOME}/plugins/delivery-task-planner"
marketplace_file="${HOME}/.agents/plugins/marketplace.json"

codex_command="$(command -v codex || true)"
if [[ -z "${codex_command}" ]]; then
  runtime_root="${XDG_STATE_HOME:-${HOME}/.local/state}/delivery-task-planner/bin"
  cached_codex="${runtime_root}/codex"
  if [[ -x "${cached_codex}" ]]; then
    codex_command="${cached_codex}"
  else
    desktop_resources=(
      "${HOME}/Applications/Codex.app/Contents/Resources"
      "/Applications/Codex.app/Contents/Resources"
      "${HOME}/Applications/ChatGPT.app/Contents/Resources"
      "/Applications/ChatGPT.app/Contents/Resources"
    )
    for resource_dir in "${desktop_resources[@]}"; do
      if [[ -x "${resource_dir}/codex" ]]; then
        mkdir -p "${runtime_root}"
        cp -f "${resource_dir}/codex" "${cached_codex}"
        chmod +x "${cached_codex}"
        if [[ -f "${resource_dir}/codex-code-mode-host" ]]; then
          cp -f "${resource_dir}/codex-code-mode-host" "${runtime_root}/codex-code-mode-host"
          chmod +x "${runtime_root}/codex-code-mode-host"
        fi
        codex_command="${cached_codex}"
        break
      fi
    done
  fi
fi
if [[ -z "${codex_command}" ]]; then
  echo "codex CLI is required. Install Codex Desktop so its Resources/codex can be copied, or install the standalone Codex CLI." >&2
  exit 1
fi

if [[ ! -f "${marketplace_file}" ]] || ! grep -q '"name"[[:space:]]*:[[:space:]]*"delivery-task-planner"' "${marketplace_file}"; then
  echo "The personal marketplace entry for delivery-task-planner is missing." >&2
  echo "Create it with the Codex plugin-creator standard personal marketplace flow first." >&2
  exit 1
fi

mkdir -p "${install_root}"
rsync -a --delete \
  --exclude '.git/' \
  --exclude '__pycache__/' \
  --exclude '.pytest_cache/' \
  --exclude '*.pyc' \
  "${plugin_root}/" "${install_root}/"

"${codex_command}" plugin add delivery-task-planner@personal
# 不给桥接进程指定工作目录：每个项目的目录由任务面板在项目管理里绑定，随请求下发。
# 曾经这里传的是插件仓库自身的路径，结果那个仓库成了所有未绑定项目的隐形默认值。
"${install_root}/scripts/start_http.sh"
