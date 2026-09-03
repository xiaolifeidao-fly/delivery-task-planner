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
# 与 delivery_bridge/restart_helper.py 的 SYSTEMD_USER_UNIT 必须一致：面板触发的
# 重启靠这个名字判断该不该自己拉进程。
systemd_unit="delivery-task-planner-bridge.service"

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
  # nohup 时代留下的进程也要收掉，否则新起的那个会撞在同一个端口上起不来。
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
  # 优先交给 systemd --user 托管，理由和 macOS 上用 LaunchAgent 一样：nohup 起来的
  # 进程注销或关机就没了，下次开机 Worker 一次心跳都不发，面板上只剩「未登记执行
  # 电脑」——而用户以为插件是装好的。
  if command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
    unit_dir="${HOME}/.config/systemd/user"
    mkdir -p "${unit_dir}"
    exec_start="$(python3 -c 'import shlex, sys; print(" ".join(shlex.quote(value) for value in sys.argv[1:]))' \
      "$(command -v python3)" "${plugin_root}/http_bridge.py" "${bridge_args[@]}")"
    cat >"${unit_dir}/${systemd_unit}" <<UNIT
[Unit]
Description=Delivery task planner local bridge
After=default.target

[Service]
Type=simple
WorkingDirectory=${plugin_root}
ExecStart=${exec_start}
Restart=always
RestartSec=2
StandardOutput=append:${log_file}
StandardError=append:${log_file}

[Install]
WantedBy=default.target
UNIT
    systemctl --user daemon-reload
    # 关机、注销之后也要自己起来。没权限开 linger 不算失败，只是少了开机自启，
    # 用户下次登录时 systemd 仍会把它拉起来。
    loginctl enable-linger "$(id -un)" >/dev/null 2>&1 || true
    systemctl --user enable "${systemd_unit}" >/dev/null 2>&1 || true
    systemctl --user restart "${systemd_unit}"
  else
    echo "systemd --user 不可用，改用 nohup 启动：本次会话结束或关机后需要重新执行本脚本。" >&2
    # 桥接进程启动后会自己把真实 pid 写进这个文件（macOS 的 LaunchAgent 路径同理），
    # 这里再记一次只是为了「起进程」和「文件可读」之间不留空窗，两边写的是同一个值。
    nohup python3 "${plugin_root}/http_bridge.py" "${bridge_args[@]}" \
      >"${log_file}" 2>&1 &
    echo $! >"${pid_file}"
  fi
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
