#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
plugin_root="$(cd "${script_dir}/.." && pwd)"
install_root="${HOME}/plugins/delivery-task-planner"
marketplace_file="${HOME}/.agents/plugins/marketplace.json"

if ! command -v codex >/dev/null 2>&1; then
  echo "codex CLI is required." >&2
  exit 1
fi

if [[ ! -f "${marketplace_file}" ]] || ! grep -q '"name"[[:space:]]*:[[:space:]]*"delivery-task-planner"' "${marketplace_file}"; then
  echo "The personal marketplace entry for delivery-task-planner is missing." >&2
  echo "Create it with the Codex plugin-creator standard personal marketplace flow first." >&2
  exit 1
fi

mkdir -p "${install_root}"
rsync -a --delete \
  --exclude '__pycache__/' \
  --exclude '*.pyc' \
  "${plugin_root}/" "${install_root}/"

codex plugin add delivery-task-planner@personal
# 不给桥接进程指定工作目录：每个项目的目录由任务面板在项目管理里绑定，随请求下发。
# 曾经这里传的是插件仓库自身的路径，结果那个仓库成了所有未绑定项目的隐形默认值。
"${install_root}/scripts/start_http.sh"
