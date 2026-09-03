# Backend API and event contract

The backend owns opportunity state, Claude Agent SDK orchestration, permissions, and the observable run stream. The frontend owns presentation and must not infer project truth from rendered chat text.

## HTTP flow

1. `POST /api/projects` creates the shared opportunity ledger.
2. `POST /api/projects/{project_id}/runs` starts a single main-Agent run and returns `202` with a run record (optional `mode: "deep_research"`, `provider_profile_id`, `resume_session_id`). The run record pins immutable provider and skill snapshots.
3. `GET /api/runs/{run_id}/events` opens a replayable Server-Sent Events stream.
4. `POST /api/runs/{run_id}/permissions/{request_id}` resolves a pending tool request with `allow_once`, `allow_run`, or `deny`.
5. `POST /api/runs/{run_id}/cancel` interrupts the SDK client and cancels the run.
6. `GET /api/projects` lists durable projects ordered by recent updates.
7. `GET /api/projects/{project_id}` and `GET /api/runs/{run_id}` return durable snapshots.
8. `POST /api/agents` creates an agent from a template (`general`, `research`, `sourcing`, `marketing`, `docs`, `opportunity`) and generates its Agent Core files. `POST /api/agents/{agent_id}/update` bumps `revision`; running sessions keep their snapshot. Pass `agent_id` in the run creation body to run as that agent (omit it for the default single Agent), and `resume_session_id` to continue a prior SDK session.
9. `POST /api/memory` records a user-entered (immediately approved) memory; `GET /api/memory?scope=&scope_id=&status=` lists; `POST /api/memory/{id}/decision` takes `approve`/`reject`/`disable`; `POST /api/memory/{id}/rollback` restores a prior version. Agent-proposed memories (via the `propose_memory` MCP tool) stay `proposed` until a user decision; only `approved` memories are injected into prompts or returned by `search_memory`. Approved agent-scope memories are mirrored as marker lines in the agent's `MEMORY.md` (removed again on disable/reject/rollback); user freehand edits in that file are preserved. Every finished run also appends a dated work-journal entry to the agent's (or project's) `diary/YYYY-MM-DD.md`.
10. `POST /api/schedules` (exactly one of `cron` or `run_once_at`, plus `timezone` and `missed_run_policy`) creates an automation; `POST /api/schedules/{id}/pause|resume|run` manage it. Missed fires while the app was down reconcile at startup: `catch_up` fires once (coalesced), `skip` advances. A schedule never overlaps its own still-active run. Scheduled runs use the same permission broker; unapproved high-risk calls time out into denial.
11. `GET /api/providers` lists Anthropic-compatible Provider profiles (never the key; `secret_configured` only). `PUT /api/providers/{id}` upserts a profile — the payload replaces the whole profile, so clients must round-trip tier mappings and capabilities; include `secret` only when rotating the key. `POST /api/providers/{id}/preflight` runs the real-SDK preflight (skill discovery + MCP health + 8-class error diagnosis, bounded by a timeout) and persists `last_verified_at` / `last_error_code`.
12. `GET /api/skills` lists versioned Plugin skills (namespace, content hash, enabled); `POST /api/skills/{id}/enable?enabled=` toggles discovery for future runs. `GET /api/skills/{id}/versions` lists immutable package history, and `POST /api/skills/{id}/rollback` selects one reviewed tenant version by exact `content_hash` (or by an unambiguous, exact declared `version` string). Rollback never deletes history, reruns package review, and preserves the current enablement state.
13. `POST /api/projects/{project_id}/files?filename=&mime=` uploads raw bytes into the project workspace `uploads/` and registers an Artifact; `GET /api/projects/{project_id}/artifacts` lists artifacts and `.../artifacts/{artifact_id}/download` streams one.
14. `GET /api/projects/{project_id}/evidence?q=&kind=&tag=&status=&limit=` queries the evidence ledger. `POST /api/projects/{project_id}/evidence/{evidence_id}/status` (`{"status": "active|stale|removed"}`) implements FR-10 invalidation: stale/removed evidence is appended to linked decisions' `invalidated_by_evidence_ids` and active decisions drop to `needs_review`; restoring reverses it once no invalidating evidence remains. The response lists `affected_decision_ids`. The same propagation is reachable in-run via the permission-gated `update_evidence_status` MCP tool.
15. `POST /api/skills/{skill_id}/test` runs the deterministic package test (frontmatter shape, referenced files, secret and dangerous-command scan, ineffective `allowed-tools` warning) and returns per-check results; it never calls a model.
16. `GET /api/connectors` lists configured connector records (credential locations only, never raw secrets); `POST /api/connectors/{id}/health-check` re-evaluates credential presence. Real integrations arrive with FR-21.
17. `PATCH /api/schedules/{id}` applies partial updates (prompt, cron/run_once_at, timezone, missed_run_policy, provider, enabled); exactly one trigger must remain and `next_fire_at` is recomputed when the trigger or timezone changes.
18. High-consequence commerce mutations use a mandatory staged-action gate. Staging exists only as the main Agent's in-process `stage_commercial_action` MCP tool; there is deliberately no public HTTP stage endpoint. The tool accepts only the structured proposal while project, run, and initialized SDK session are injected by the server rather than accepted from model input, and the server creates the opaque action ID. `POST .../{action_id}/review` records an authenticated user's approve/reject decision, reason, time, and approval expiry. `POST .../{action_id}/apply` re-checks the current project revision and current organization operation/connector/field/numeric limits, then creates one idempotent `execution_requested` record. Project transactions are serialized with both an in-process lock and a cross-process file lock; snapshot replacements use unique temporary files. Because no commerce execution adapter is installed, the external status is always `not_executed`; it must never be presented as an external success. `GET` on the collection or individual action is read-only; `expires_at` is authoritative, while an attempted review/apply persists the `expired` transition and audit history. Review/apply require both `X-Ophira-Authenticated-User` and `X-Ophira-Approval-Secret`; the latter must match the server-side `OPHIRA_COMMERCIAL_APPROVAL_SECRET`, which is shadowed out of Agent worker environments. A production ingress must strip both client-supplied headers, authenticate a human, and inject the asserted identity plus gateway secret. If the secret is unconfigured, review/apply return `503`; missing or incorrect gateway authentication returns `401`. Direct local access is not an end-user authentication system or a production human-approval boundary. Review and apply are intentionally absent from every Agent/MCP tool surface.

`Write` and `Edit` are available to attended single-Agent runs but are not pre-approved: each call crosses the permission broker, and paths must resolve inside the run workspace (symlink escapes are denied without asking). Sandboxed `Bash` follows the SDK-native `autoAllowBashIfSandboxed` path; the runtime fails closed if the sandbox is unavailable, permanently denies `dangerouslyDisableSandbox`, asks before destructive deletion, and denies Bash entirely for scheduled runs.

`GET /api/meta/protocol` is the machine-readable runtime source for event names, run/status/mode values, permission and schedule values, Agent templates, memory values, commercial action operations/status/review decisions, external execution status, and typed-presentation schema/component values. The frontend consumes `event_types` when subscribing to SSE; static TypeScript DTOs must remain OpenAPI-aligned until generation is automated.

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
- `ui.presentation` is emitted only after the presentation MCP server joins model-selected ledger IDs to current server facts and validates the full versioned component payload. Clients must still require the matching project ID/schema/origin and render text without raw HTML; artifact links must match the server-owned download route.
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
