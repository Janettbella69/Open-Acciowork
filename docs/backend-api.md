# Backend API and event contract

The backend owns opportunity state, Claude Agent SDK orchestration, permissions, and the observable run stream. The frontend owns presentation and must not infer project truth from rendered chat text.

## HTTP flow

1. `POST /api/projects` creates the shared opportunity ledger.
2. `POST /api/projects/{project_id}/runs` starts an Opportunity Lead run and returns `202` with a run record.
3. `GET /api/runs/{run_id}/events` opens a replayable Server-Sent Events stream.
4. `POST /api/runs/{run_id}/permissions/{request_id}` resolves a pending tool request with `allow_once`, `allow_run`, or `deny`.
5. `POST /api/runs/{run_id}/cancel` interrupts the SDK client and cancels the run.
6. `GET /api/projects` lists durable projects ordered by recent updates.
7. `GET /api/projects/{project_id}` and `GET /api/runs/{run_id}` return durable snapshots.
8. `POST /api/agents` creates an agent from a template (`general`, `research`, `sourcing`, `marketing`, `docs`, `opportunity`) and generates its Agent Core files. `POST /api/agents/{agent_id}/update` bumps `revision`; running sessions keep their snapshot. Pass `agent_id` in the run creation body to run as that agent (omit it for the legacy opportunity team), and `resume_session_id` to continue a prior SDK session.
9. `POST /api/memory` records a user-entered (immediately approved) memory; `GET /api/memory?scope=&scope_id=&status=` lists; `POST /api/memory/{id}/decision` takes `approve`/`reject`/`disable`; `POST /api/memory/{id}/rollback` restores a prior version. Agent-proposed memories (via the `propose_memory` MCP tool) stay `proposed` until a user decision; only `approved` memories are injected into prompts or returned by `search_memory`.
10. `POST /api/schedules` (exactly one of `cron` or `run_once_at`, plus `timezone` and `missed_run_policy`) creates an automation; `POST /api/schedules/{id}/pause|resume|run` manage it. Missed fires while the app was down reconcile at startup: `catch_up` fires once (coalesced), `skip` advances. A schedule never overlaps its own still-active run. Scheduled runs use the same permission broker; unapproved high-risk calls time out into denial.

Write tools (`Write`, `Edit`) and `Bash` are available to single-agent templates but are never pre-approved: each call pauses the run at `permission.requested`. Write paths must resolve inside the run workspace — symlink escapes are denied without asking. `Bash` requires per-call approval and cannot be reused via `allow_run`.

`GET /api/meta/protocol` is the single machine-readable source for event names, run statuses, dimensions, stages, permission decisions, agent templates, memory scopes/statuses, and missed-run policies. Frontend code should fetch or generate types from it instead of maintaining a second enum list.

## SSE replay

Each event has a monotonically increasing ID such as `run_abcd:17`. Reconnect with the standard `Last-Event-ID` header; the server replays every later JSONL event before following live output. A terminal stream closes only after its final durable event has been sent.

```json
{
  "id": "run_abcd:17",
  "sequence": 17,
  "type": "tool.completed",
  "run_id": "run_abcd",
  "project_id": "project_abcd",
  "trace_id": "trace_abcd",
  "timestamp": "2026-08-05T09:30:00Z",
  "actor": {
    "type": "subagent",
    "name": "demand-agent",
    "agent_id": "agent_abcd"
  },
  "parent_id": null,
  "tool_use_id": "toolu_abcd",
  "data": {
    "tool_name": "WebSearch",
    "tool_input": {"query": "portable blender market"},
    "tool_response": "..."
  }
}
```

## Rendering semantics

- `assistant.text.delta` is append-only visible answer text; `assistant.message.completed` is the authoritative completed block.
- `assistant.reasoning_summary.*` is emitted only from SDK output configured as a user-visible summary. It is not private chain-of-thought and must be labeled “推理摘要” in the UI.
- `tool.planned` carries the complete SDK tool-use block. `tool.input.delta` is optional partial JSON for live typing. `tool.started`, `tool.completed`, and `tool.failed` come from SDK lifecycle hooks.
- `subagent.*` identifies the delegated Agent lifecycle; `task.*` reports SDK task progress.
- `permission.requested` pauses the run. Render its redacted input and submit a decision to the permission endpoint.
- `run.result` includes SDK duration, turns, usage, cost, permission denials, structured output, and final result when available.

All event payloads pass through recursive secret redaction and bounded serialization before being appended. The backend intentionally does not expose hidden model chain-of-thought.

## Durable diagnostics

The configured data directory uses three levels:

- `_manifest.jsonl`: append-only run/status index.
- `runs/{run_id}/summary.json`: status, last sequence, and event counts.
- `runs/{run_id}/events.jsonl`: detailed replay stream.

The same `trace_id` is present in run records and every event so client, API, Agent, subagent, and tool activity can be correlated.
