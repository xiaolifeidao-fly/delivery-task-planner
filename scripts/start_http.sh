#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
plugin_root="$(cd "${script_dir}/.." && pwd)"
# 进程级工作目录只是可选的兜底：真正用哪个目录由面板请求里的 workspace 按项目决定。
# 这里绝不拿插件安装目录的上级当默认值——那只是插件恰好被放在了某个仓库里，不代表它属于那个项目。
workspace="${DELIVERY_CODEX_WORKSPACE:-}"
command_api_url="${DELIVERY_COMMAND_API_URL:-}"
runtime_dir="${HOME}/.local/state/delivery-task-planner"
workspace_file="${runtime_dir}/workspace"
log_file="${runtime_dir}/http-bridge.log"

mkdir -p "${runtime_dir}"
if [[ -z "${workspace}" ]] && [[ -f "${workspace_file}" ]]; then
  workspace="$(cat "${workspace_file}")"
fi
if [[ -n "${workspace}" ]]; then
  printf '%s\n' "${workspace}" >"${workspace_file}"
fi

if [[ "$(uname -s)" == "Darwin" ]]; then
  launch_args=(
    --plugin-root "${plugin_root}"
    --workspace "${workspace}"
    --allow-origin "*"
  )
  if [[ -n "${command_api_url}" ]]; then
    launch_args+=(--command-api-url "${command_api_url}")
  fi
  python3 "${script_dir}/install_http_service.py" "${launch_args[@]}"
else
  pid_file="${runtime_dir}/http-bridge.pid"
  if [[ -f "${pid_file}" ]] && kill -0 "$(cat "${pid_file}")" 2>/dev/null; then
    kill "$(cat "${pid_file}")"
  fi
  bridge_args=(
    --workspace "${workspace}"
    --allow-origin "*"
  )
  if [[ -n "${command_api_url}" ]]; then
    bridge_args+=(--command-api-url "${command_api_url}")
  fi
  nohup python3 "${plugin_root}/http_bridge.py" "${bridge_args[@]}" \
    >"${log_file}" 2>&1 &
  echo $! >"${pid_file}"
fi

for _ in {1..30}; do
  if curl --silent --fail http://127.0.0.1:8765/healthz >/dev/null; then
    echo "Codex HTTP bridge is running at http://127.0.0.1:8765"
    exit 0
  fi
  sleep 0.2
done

echo "Codex HTTP bridge failed to start. See ${log_file}" >&2
exit 1
