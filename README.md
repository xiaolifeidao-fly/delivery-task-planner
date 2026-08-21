# Delivery Task Planner

Task-planning plugin for creating projects, stages, modules, and dependency-aware tasks in the Universe delivery board. Task execution is intentionally not exposed as a Codex plugin action.

## Skills

All six delivery-board skills are maintained here under `skills/`. Four cover the task life cycle; `requirement-prototype` and `delivery-requirement-testing` cover requirement-level work.

| Skill | Scope |
| --- | --- |
| `delivery-task-planner` | Break a requirement into tasks and write them to the board (preview first, write only after the user confirms in the board); keeps the multi-document requirement directory at `doc/requirements/<requirementKey>/` with `需求大纲.md` as the primary document and can pre-generate each task's primary requirement document at `doc/<moduleKey>/<itemKey>/文档.md` |
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

The plugin exposes no MCP server. Every task board operation runs through the `taskboard.py` CLI (`python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/plugins/delivery-task-planner}/taskboard.py <action> [--flag value] [--json ...]`; `taskboard.py actions` prints every action and its parameters). The service address is fixed at `http://47.110.3.214:8691/api` in `server.py` and is not configurable. Credentials never come from the config file: the console posts the signed-in `token` and `userId` to `POST /v1/session/heartbeat` on the local bridge once a minute, and the bridge stores them in `~/.config/delivery-task-planner/credential.json` with mode `0600`. Both the CLI and the bridge read that file, so switching console accounts or refreshing an expired token takes effect within a minute. `taskboard.py store-task-board-credential --key <token>` writes one by hand when the console is unavailable. The credential header is `token`, matching the existing web console.

Start a new Codex task after installing the plugin, invoke `@delivery-task-planner`, and ask to “拆解需求并写入任务面板”. Select a project from `list_task_board_projects` and pass its numeric `programId` as `program_id`; project names and `programCode` are never task association identifiers. New projects use `program_code` only for display and import idempotency, then return their numeric primary key. The workflow accepts an optional stage and module, previews the plan, and writes it in dependency order.

The MCP surface does not expose queue, claim, session-binding, status-transition, or finish tools. Requests to start or continue work must be handled from the delivery board UI.

Installation starts a loopback HTTP bridge at `http://127.0.0.1:8765`; no local certificate is required. The bridge sends `Access-Control-Allow-Origin: *`, so the delivery board may be served from any browser origin. This bridge is private infrastructure for the delivery board's execution buttons and session views; it is not a Codex chat command. Every board request carries the selected numeric project primary key `programId`, the browser-local confirmed `workspace`, `bizLine`, and the current login `token` header. The bridge verifies that token can access the selected project, validates the workspace as an existing absolute directory, creates a temporary project-scoped API context, and starts or resumes Codex in that workspace. It never persists the board token or accepts another project ID for that child process. The project management page can discover Codex Desktop's local projects through `GET /v1/codex/workspaces`; the selected path remains browser-local. Project discovery is the only endpoint that does not require `workspace`; every Codex interaction rejects a missing workspace instead of falling back to the bridge installation or startup directory. The board can call `POST /v1/codex/execute` for one task, `POST /v1/codex/execute-batch` to run selected incomplete tasks by dependency layer (parallel within each layer, then automatically release successors), or `POST /v1/codex/execute-sequence` for selected incomplete tasks in dependency order. Every mode validates task completion, dependencies, local queue conflicts, creates persisted Codex threads, and synchronizes readable output. Runtime state and logs live under `~/.local/state/delivery-task-planner/`.

## Install prompt for Codex or Claude

When the task board reports that the local plugin is unavailable, paste the following into Codex or Claude:

```text
Install the delivery-task-planner plugin from https://github.com/xiaolifeidao-fly/delivery-task-planner. Configure and start its local HTTP bridge at http://127.0.0.1:8765 for the Universe delivery task board. When installation is complete, return to the task board and refresh the page.
```
