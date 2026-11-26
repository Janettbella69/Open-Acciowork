# TradeMind architecture

This is the only architecture document in the repository. If code and this page disagree,
fix one of them in the same change.

## The shape: one computer, one loop, one window

- **One computer.** Every project has a workspace folder. The Agent reads and writes files
  there, writes Python to calculate and build tables, and runs commands; Bash runs inside the
  Claude Agent SDK's sandbox.
- **One loop.** The Claude Agent SDK runs every turn with the `claude_code` tool and prompt
  presets. There is no state machine, no intent routing and no turn limit; the model decides
  which tool to use next and when to stop. The only fuse is a per-turn cost budget.
- **One window.** A project is one continuing conversation (the SDK session is resumed on
  every turn). What should outlast it the Agent writes to its memory folder as Markdown, with
  the SDK's own auto memory.

The principles come from the course textbook the project follows (《Agent Harness 教材》):
"给它一台电脑，给足背景，给足工具，给足自由，把约束写进经验而不是代码，最后一道留给人" (p.3),
and "代码合法的位置有四个" (p.20).

## Where host code is allowed (textbook p.20) and where it is

| Place | What lives there |
| --- | --- |
| Before the Agent starts | the workspace snapshot, the system prompt, the options; a PDF upload gets a Markdown copy of its text (`store.py`, `agent.py`) |
| Infrastructure | the model gateway that adds the key (`gateway.py`); the cost fuse; the same-origin check (`main.py`) |
| Hook, side effects only | `PreToolUse`: a write outside the project is refused; a delete is sent to the person (`agent.py`, `destructive.py`) |
| Before the person decides | `can_use_tool`: approvals and `AskUserQuestion` wait for the person's answer (`agent.py`) |

Nothing else. No code decides what the Agent does next, rewrites what a tool returned, or
speaks to the person on the Agent's behalf. A change that needs code anywhere else is a
design question first.

## Files

```
backend/src/ophira_backend/
  main.py         HTTP: projects, turns, event stream, approvals, files, feedback
  agent.py        one turn: options, the hook, the person's decisions, the event log
  gateway.py      model gateway on 127.0.0.1:8000/_model
  store.py        everything is a file (layout below)
  settings.py     environment configuration (OPHIRA_*)
  destructive.py  recognises deletes in Bash and tool calls
backend/ophira-capabilities/   the local plugin with the Skills (cross-border know-how)
frontend/src/                  landing page and the workspace
deploy/shared-box/             the production server kit and runbook
```

Data, one folder per project under `OPHIRA_DATA_DIR/projects/<project_id>/`:

```
project.json      name, created_at, session_id (the SDK session to resume)
workspace/        the Agent's cwd; uploads land in workspace/uploads/
claude/           CLAUDE_CONFIG_DIR: the SDK's own transcripts and auto memory
snapshots.git/    git history of workspace/, taken before and after every turn
runs/<id>.json    a turn: prompt, status, timestamps, error
runs/<id>.jsonl   the turn's log, one event per line, in arrival order
feedback.jsonl    the person's "this is wrong" reports
```

There is no database and no derived index.

## A turn

1. `POST /api/projects/{id}/runs` with the prompt. One turn at a time per project (409
   otherwise).
2. The workspace is snapshotted, then a `ClaudeSDKClient` is opened with the options in
   `agent.py` and the session resumed.
3. Every message the SDK sends is written to the log as it arrives and pushed to readers.
4. When the Agent wants to delete something, use a tool that needs approval, or asks a
   question, the turn waits for `POST /api/runs/{run}/permissions/{request}`.
5. The turn ends (completed, failed or cancelled), the workspace is snapshotted again, and
   the log gets `run.finished`. A turn cut off by a restart is closed with that reason when
   the backend starts again.

### Event log contract

Each line is `{seq, at, source, type, data}`.

- `source: "sdk"` — `type` is the SDK message class (`AssistantMessage`, `UserMessage`,
  `SystemMessage`, `ResultMessage`, ...) and `data` its fields, as the SDK sent them; every
  nested block carries its class name in `type` (`TextBlock`, `ToolUseBlock`,
  `ToolResultBlock`, `ThinkingBlock`).
- `source: "host"` — one of `run.started`, `permission.requested`
  (`request_id, tool_name, tool_input, tool_use_id`), `permission.resolved`
  (`request_id, allow, message?, answers?`), `run.finished` (`status, error`).

`GET /api/runs/{id}/events` streams it as server-sent events; `Last-Event-ID` (or
`?after=`) resumes after a seen event, and the stream closes after `run.finished`.

### HTTP API

| Method and path | Purpose |
| --- | --- |
| `GET /health` | liveness and the configured model name |
| `GET, POST /api/projects` | list, create |
| `GET /api/projects/{id}` | project, its turns, the active turn |
| `POST /api/projects/{id}/runs` | start a turn |
| `GET /api/runs/{id}/events` | the turn's events (SSE) |
| `POST /api/runs/{id}/cancel` | stop the turn |
| `POST /api/runs/{id}/permissions/{request}` | the person's answer: `allow`, `message`, `answers`, `response` |
| `GET, POST /api/projects/{id}/files` | list the workspace, upload into `uploads/` |
| `GET /api/projects/{id}/files/{path}` | download a workspace file |
| `POST /api/projects/{id}/feedback` | record what is wrong |

## Permissions

- Reading, searching the web, fetching pages, delegating to a subagent and loading Skills
  are allowed outright.
- Writes inside the workspace and the memory folder are allowed without a click; the
  snapshot before the turn is their backup. Writes anywhere else are refused by the hook.
- Bash runs inside the SDK sandbox (`failIfUnavailable`, no unsandboxed escape); a delete in
  Bash or a tool call goes to the person.
- Everything else that the SDK routes to `can_use_tool`, and every `AskUserQuestion`, waits
  for the person.

## The model and its key

The CLI's `ANTHROPIC_BASE_URL` is the gateway, its `ANTHROPIC_AUTH_TOKEN` a per-process
token. The gateway checks the token, replaces it with the real key, and streams the response
back. Providers and what has been measured on them: [`model-providers.md`](model-providers.md).

## Not here, and why

- No user system, organisations or sign-in: there are no users yet (textbook p.66, "先拿到用户").
  The site's password lives in the deployment's proxy.
- No platform connectors, mail, chat channels or browser automation: none has a real seller
  account behind it yet. The pre-rebuild code is at the tag `archive/pre-rebuild-2026-11-26`.
- No custom memory system: the SDK's auto memory is files the Agent writes itself
  (textbook p.93, "写markdown，SDK自己会存").
- No calculation or formatting tools: the Agent writes Python in its workspace
  (textbook p.94, "把数据分析封成MCP → 写Python").
- No evaluation platform inside the product: the first real turns come first
  (textbook p.96, "先做评测平台再做产品 → 先有产品").

## Verifying a change

- `backend`: `PYTHONPATH=src pytest` and `ruff check src tests`. `OPHIRA_E2E_CLI=1` adds
  `tests/test_e2e_cli.py`, which drives the real bundled Claude Code CLI through two turns
  against a scripted local model (`tests/fake_model.py`): tools, the hook, approvals,
  memory, resume. It costs nothing.
- `frontend`: `npm run lint`, `npm run test:run`, `npm run build`.
- In a browser: run both, open `/app`, and go through a turn.
- On the server: `deploy/shared-box/sandbox_acceptance.py` runs the same scripted turn.
