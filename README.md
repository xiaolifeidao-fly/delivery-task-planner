# Delivery Task Planner

Task-planning plugin for creating projects, stages, modules, and dependency-aware tasks in the Universe delivery board. Task execution is intentionally not exposed as a Codex plugin action.

## Skills

All six delivery-board skills are maintained here under `skills/`. Four cover the task life cycle; `requirement-prototype` and `delivery-requirement-testing` cover requirement-level work.

| Skill | Scope |
| --- | --- |
| `delivery-task-planner` | Break a requirement into tasks and write them to the board (preview first, write only after the user confirms in the board); keeps per-chat process summaries under the installed plugin's `.temp/requirements/` tree, updates `doc/requirements/<requirementKey>/需求大纲.md` only on confirmation, and can pre-generate each task's primary requirement document at `doc/<moduleKey>/<itemKey>/文档.md` |
| `delivery-requirement-grooming` | A task's `requirement` phase — produce `doc/<module>/<itemKey>/文档.md` |
| `delivery-action-execution` | A task's `development` phase — implement against that document; the final reply is stored as the task's action output |
| `delivery-testing-report` | A task's `testing` phase — verify against the acceptance criteria; the final reply is stored as the task's test report (8MB limit) |
| `requirement-prototype` | Generate or update a requirement's polished, modular HTML prototype under `doc/requirements/<requirementKey>/prototype/`; no separate prototype window is required |
| `delivery-requirement-testing` | A requirement-level overall test — verify linked tasks together and archive its multi-document plan, evidence, and report under `doc/test/<requirementKey>/` |

## Source and installation

This directory is the only maintained source of the plugin. Do not edit the deployed copy under `~/plugins/delivery-task-planner` or the Codex cache.

Install or refresh it through the standard personal marketplace flow:

```bash
./scripts/install_personal.sh
```

The script publishes this source to `~/plugins/delivery-task-planner` and runs `codex plugin add delivery-task-planner@personal`. The personal marketplace entry must already exist; create it with the Codex `plugin-creator` standard flow on a new machine. If the standalone Codex CLI is absent, the installer copies the bundled CLI from Codex Desktop (or ChatGPT on macOS) into the task-board runtime directory; the bridge uses the same fallback when it starts work.

On Windows, run `powershell -ExecutionPolicy Bypass -File .\scripts\install_personal.ps1` from this directory. It installs the plugin for the current user and registers the local HTTP bridge as the current user's logon task.

Every task board operation runs through the `taskboard.py` CLI (`python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/plugins/delivery-task-planner}/taskboard.py <action> [--flag value] [--json ...]`; `taskboard.py actions` prints every action and its parameters). The service address is fixed at `http://47.110.3.214:8691/api` in `server.py` and is not configurable. Credentials never come from the config file: the console posts the signed-in `token` and `userId` to `POST /v1/session/heartbeat` on the local bridge once a minute, and the bridge stores them in `~/.config/delivery-task-planner/credential.json` with mode `0600`. Both the CLI and the bridge read that file, so switching console accounts or refreshing an expired token takes effect within a minute. `taskboard.py store-task-board-credential --key <token>` writes one by hand when the console is unavailable. The credential header is `token`, matching the existing web console.

Start a new Codex task after installing the plugin, invoke `@delivery-task-planner`, and ask to “拆解需求并写入任务面板”. Select a project from `list_task_board_projects` and pass its numeric `programId` as `program_id`; project names and `programCode` are never task association identifiers. New projects use `program_code` only for display and import idempotency, then return their numeric primary key. The workflow accepts an optional stage and module, previews the plan, and writes it in dependency order.

The CLI does not expose queue, claim, session-binding, status-transition, or finish actions. Requests to start or continue work must be handled from the delivery board UI.

Installation starts an HTTP bridge on `0.0.0.0:8765`; no local certificate is required. The bridge sends `Access-Control-Allow-Origin: *`, so the delivery board may be served from any browser origin. This bridge is private infrastructure for the delivery board's execution buttons and session views; it is not a Codex chat command. Every board request carries the selected numeric project primary key `programId`, the browser-local confirmed `workspace`, `bizLine`, and the current login `token` header. The bridge verifies that token can access the selected project, validates the workspace as an existing absolute directory, creates a temporary project-scoped API context, and starts or resumes Codex in that workspace. It never persists the board token or accepts another project ID for that child process. The project management page can discover Codex Desktop's local projects through `GET /v1/codex/workspaces`; the selected path remains browser-local. Project discovery is the only endpoint that does not require `workspace`; every Codex interaction rejects a missing workspace instead of falling back to the bridge installation or startup directory. The board can call `POST /v1/codex/execute` for one task, `POST /v1/codex/execute-batch` to run selected incomplete tasks by dependency layer (parallel within each layer, then automatically release successors), or `POST /v1/codex/execute-sequence` for selected incomplete tasks in dependency order. Every mode validates task completion, dependencies, local queue conflicts, creates persisted Codex threads, and synchronizes readable output. Runtime state and logs live under `~/.local/state/delivery-task-planner/`.

## Remote business interview mode

The same conversation endpoint also supports the server-to-server business interview flow when the request has `businessIntake: true`:

```bash
python3 http_bridge.py --port 8765
```

By default, the business workspace root is `~/.local/share/delivery-task-planner/business-workspaces`. Set `BUSINESS_KODES_WORKSPACE_ROOT` only when it needs to use a dedicated volume, for example `BUSINESS_KODES_WORKSPACE_ROOT=/srv/universe/business-workspaces python3 http_bridge.py --port 8765`.

The Go service sends only a logical workspace name in the form `{username}/业务空间/{projectName}`. The bridge validates it, resolves it beneath that root, and creates the directory on first use. It never accepts an arbitrary absolute path for a business interview. This mode does not call delivery-item APIs and does not require an additional business token; protect the listener with network or reverse-proxy access controls when exposing it to the Go service.

`--host` defaults to `0.0.0.0` so the Go service can reach the bridge from another machine. Pass `--host 127.0.0.1` to keep it loopback-only. The bridge has no authentication of its own, so an internet-facing deployment must be paired with a firewall or security-group rule that limits the source addresses to the known callers.

## Runtime layout and updates

The bridge entry point remains `http_bridge.py`, but reusable runtime concerns live under `delivery_bridge/`:

Modules are imported back into `http_bridge.py` by name so existing callers keep resolving them there. When a test needs to patch one of these functions, it must patch the module that owns it (for example `delivery_bridge.git_ops.run_git`) — patching the re-exported name on `http_bridge` has no effect on callers that already live inside the package.

- `errors.py` owns `BridgeFailure`, the one business-failure exception every lower layer raises so no module has to import the entry point back.
- `workspaces.py` resolves the project working directory and the server-supplied `{username}/业务空间/{projectName}` business workspace, enforcing the containment check in one place.
- `prompt_context.py` owns the `delivery-bridge-context` wrapper that separates board-assembled prompt context from the words the user actually typed.
- `git_ops.py` owns every local Git command behind requirement branches — branch catalog, change detail, push/rebase, merge preview, submodules and subprojects. Arguments are always fixed and branch names are validated before reaching Git.
- `turn_output.py` turns an executor turn's item stream into what the board renders: whether the turn really touched the workspace, whether it counts as finished, the testing verdict, and per-file change counts.
- `errors.py` owns `BridgeFailure`, the one business-failure exception every lower layer raises so no module has to import the entry point back.
- `runtime.py` resolves the runtime directory (logs, caches, queued syncs), overridable through `DELIVERY_TASK_PLANNER_RUNTIME_DIR`.
- `hostinfo.py` owns the macOS / Windows / Linux decision that several modules branch on.
- `codex_cli.py` enumerates every codex build on the machine (PATH, local cache, Codex Desktop resources), compares versions, and copies the newest one out when needed.
- `providers.py` normalizes executor identity: which AI, which purpose, reasoning effort, fast mode.
- `workspaces.py` resolves the project working directory and the server-supplied `{username}/业务空间/{projectName}` business workspace, enforcing the containment check in one place.
- `github_ssh.py` inspects and configures the plugin's own GitHub SSH key, rewriting only the block between its own markers in `~/.ssh/config`.
- `environments.py` holds the preset-environment catalog with its version floors, probe commands, and per-platform install commands.
- `prompt_context.py` owns the `delivery-bridge-context` wrapper that separates board-assembled prompt context from the words the user actually typed.
- `documents.py` owns the on-disk shape of the outline, task, design, and test-case document sets, including HTML companion assets.
- `git_ops.py` owns every local Git command behind requirement branches — branch catalog, change detail, push/rebase, merge preview, submodules and subprojects. Arguments are always fixed and branch names are validated before reaching Git.
- `turn_output.py` turns an executor turn's item stream into what the board renders: whether the turn really touched the workspace, whether it counts as finished, the testing verdict, and per-file change counts.
- `versioning.py` owns SemVer comparison shared by update checks and installation.
- `update_manager.py` resolves an immutable Git commit, downloads and validates the release archive, backs up the active package, refreshes Codex and Claude Code caches, and persists bounded installation logs.
- `restart_helper.py` preserves the bridge command-line arguments and restarts the bridge after the HTTP response is delivered. On macOS it hands control back to the per-user LaunchAgent. On Windows the logon task runs `windows_supervisor.py`, which relaunches a stopped bridge worker in under a second instead of waiting for Task Scheduler's one-minute failure interval; an update also migrates an older direct-bridge task to the supervisor action and waits for `/healthz` before declaring the restart usable. Restart diagnostics are written to the platform runtime directory, and a stale `restarting` job becomes retryable after 45 seconds instead of polling forever.

The console checks `GET /v1/plugin/update` once a minute and silently starts `POST /v1/plugin/update/install` whenever the repository contains a newer SemVer release. Installation no longer requires an update dialog or operator confirmation. The install endpoint accepts only the expected version; executable files are always downloaded by the loopback bridge from the fixed repository and never uploaded by a browser origin. The archive is size-limited, path-checked, pinned to one Git commit, and must contain matching Codex and Claude plugin manifests plus the bridge entry points and skills.

An installation refreshes the Codex personal-marketplace source and invokes `codex plugin add`. Existing Claude Code installations are copied into a new versioned cache directory and `installed_plugins.json` is replaced atomically, leaving the previous cache available to already running Claude sessions. Every successful update waits for bridge-managed Codex and Claude runs to finish, then invokes the detached restart helper even for manifest-only releases; it continues if the browser tab closes. `GET /v1/plugin/info` exposes the manifest version captured in memory when the Python bridge starts, so the reported version changes only after a genuine restart. `GET /v1/plugin/runtime-test` remains available as a low-level diagnostic endpoint. Backups and update state live under `~/.local/state/delivery-task-planner/`.

## Install prompt for Codex or Claude

When the task board reports that the local plugin is unavailable, paste the following into Codex or Claude:

```text
Install the delivery-task-planner plugin from https://github.com/xiaolifeidao-fly/delivery-task-planner. Configure and start its local HTTP bridge at http://127.0.0.1:8765 for the Universe delivery task board. When installation is complete, return to the task board and refresh the page.
```
