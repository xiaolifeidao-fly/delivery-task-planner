# Delivery Task Planner

Task-planning plugin for creating projects, stages, modules, and dependency-aware tasks in the Universe delivery board. Task execution is intentionally not exposed as a Codex plugin action.

## Skills

All six delivery-board skills are maintained here under `skills/`. Four cover the task life cycle; `requirement-prototype` and `delivery-requirement-testing` cover requirement-level work.

| Skill | Scope |
| --- | --- |
| `delivery-task-planner` | Break a requirement into tasks and write them to the board (preview first, write only after the user confirms in the board); keeps the requirement outline at `doc/requirements/<requirementKey>/需求大纲.md` and can pre-generate each task's sole requirement document at `doc/<moduleKey>/<itemKey>/文档.md` |
| `delivery-requirement-grooming` | A task's `requirement` phase — produce `doc/<module>/<itemKey>/文档.md` |
| `delivery-action-execution` | A task's `development` phase — implement against that document; the final reply is stored as the task's action output |
| `delivery-testing-report` | A task's `testing` phase — verify against the acceptance criteria; the final reply is stored as the task's test report (8MB limit) |
| `requirement-prototype` | Generate or update a requirement's polished, modular HTML prototype under `doc/requirements/<requirementKey>/prototype/`; no separate prototype window is required |
| `delivery-requirement-testing` | A requirement-level overall test — verify linked tasks together and archive its plan, evidence, and report under `doc/test/<requirementKey>/` |

## Source and installation

This directory is the only maintained source of the plugin. Do not edit the deployed copy under `~/plugins/delivery-task-planner` or the Codex cache.

Install or refresh it through the standard personal marketplace flow:

```bash
./scripts/install_personal.sh
```

The script publishes this source to `~/plugins/delivery-task-planner` and runs `codex plugin add delivery-task-planner@personal`. The personal marketplace entry must already exist; create it with the Codex `plugin-creator` standard flow on a new machine.

On Windows, run `powershell -ExecutionPolicy Bypass -File .\scripts\install_personal.ps1` from this directory. It installs the plugin for the current user, creates the local HTTPS CA and bridge certificate under `%LOCALAPPDATA%\delivery-task-planner\tls\`, imports the CA into `Cert:\CurrentUser\Root`, and registers the bridge as the current user's logon task. The bridge certificate includes `localhost`, `127.0.0.1`, and `::1`.

The normal MCP planning workflow stores its independently configured connection in `~/.config/delivery-task-planner/config.json` with mode `0600` and continues to read the previous Codex-specific path for migration. The default credential header is `token`, matching the existing web console. An API URL may be supplied either as the service root or with the `/api` suffix. The local HTTPS bridge defaults to `http://47.110.3.214:8691/api`; use the `set_task_board_api_url` MCP tool to change the shared planning and bridge target without re-entering the saved credential.

Start a new Codex task after installing the plugin, invoke `@delivery-task-planner`, and ask to “拆解需求并写入任务面板”. Select a project from `list_task_board_projects` and pass its numeric `programId` as `program_id`; project names and `programCode` are never task association identifiers. New projects use `program_code` only for display and import idempotency, then return their numeric primary key. The workflow accepts an optional stage and module, previews the plan, and writes it in dependency order.

The MCP surface does not expose queue, claim, session-binding, status-transition, or finish tools. Requests to start or continue work must be handled from the delivery board UI.

Installation starts a loopback HTTPS bridge at `https://127.0.0.1:8765`. On macOS, its first start creates a private local CA and bridge certificate under `~/.local/state/delivery-task-planner/tls/`, then trusts that CA in the current user's login keychain. The certificate includes `localhost`, `127.0.0.1`, and `::1`; private keys are never stored in the plugin directory. Linux also generates the certificate, but its CA must be imported into the browser trust store before browser requests can use it. The bridge sends `Access-Control-Allow-Origin: *`, so the delivery board may be served from any browser origin. This bridge is private infrastructure for the delivery board's execution buttons and session views; it is not a Codex chat command. Every board request carries the selected numeric project primary key `programId`, the browser-local confirmed `workspace`, `bizLine`, and the current login `token` header. The bridge verifies that token can access the selected project, validates the workspace as an existing absolute directory, creates a temporary project-scoped API context, and starts or resumes Codex in that workspace. It never persists the board token or accepts another project ID for that child process. The project management page can discover Codex Desktop's local projects through `GET /v1/codex/workspaces`; the selected path remains browser-local. Project discovery is the only endpoint that does not require `workspace`; every Codex interaction rejects a missing workspace instead of falling back to the bridge installation or startup directory. The board can call `POST /v1/codex/execute` for one task, `POST /v1/codex/execute-batch` to run selected incomplete tasks by dependency layer (parallel within each layer, then automatically release successors), or `POST /v1/codex/execute-sequence` for selected incomplete tasks in dependency order. Every mode validates task completion, dependencies, local queue conflicts, creates persisted Codex threads, and synchronizes readable output. Runtime state and logs live under `~/.local/state/delivery-task-planner/`.
