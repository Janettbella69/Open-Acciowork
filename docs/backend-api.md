# Backend API and event contract

The backend owns opportunity state, Claude Agent SDK orchestration, permissions, and the observable run stream. The frontend owns presentation and must not infer project truth from rendered chat text.

## Identity and deployment boundary

Every `/api/*` request except the independently Feishu-signed callback is authenticated
by middleware. In `trusted_gateway` mode the gateway sends a short-lived HMAC assertion
over the exact method/path and `tenant_id`, `principal_id`, `role`, `session_id`, Unix
timestamp, and single-use nonce. A valid assertion is necessary but not sufficient:
project requests also require a current durable membership whose role exactly matches
the assertion. Unsigned identity-shaped headers and model/tool arguments never create a
principal. In `local_only` mode only direct loopback requests are accepted and the
server deliberately exposes no authenticated principal.

Local API requests must also carry a loopback `Host`. Browser `Origin` (or `Referer`
when Origin is absent) must match the request origin or an explicit
`OPHIRA_CORS_ORIGINS` entry. Missing-source cross-site Fetch requests, duplicate source
headers, opaque origins, and untrusted hosts/origins return `403` before route execution,
including bodyless writes and CORS preflights. A CORS `*` is not an authorization grant.
Direct local CLI clients without browser source headers remain supported. The separately
signed Feishu callback and trusted-gateway authentication retain their own boundaries.

This is a trusted-principal, single-deployment-tenant boundary, not complete
multi-tenancy. `GET /api/auth/status` therefore returns `multi_tenant: false` in both
modes. Global Provider, Agent, Skill, Connector, policy, and automation stores still
need physical tenant partitioning before a multi-tenant production claim is valid.

## HTTP flow

Before schedulers and delivery workers start, the backend reconciles interrupted runs.
A run lease is acquired before publishing its record and held through execution cleanup;
startup recovery skips leases still owned by another backend worker. Ownerless queued,
running, permission-waiting, or paused runs become `failed` with `SDK_RUNTIME_FAILED`;
interrupted cancellation becomes `cancelled`. Existing evidence, artifacts, provider/Skill
snapshots and SDK session IDs are retained, pending permission prompts are expired, and
the terminal event is replayable. Recovery does not automatically rerun the Agent or
reapply commercial actions. The existing retry API can resume the saved SDK session or
start a new branch after user review. Normal shutdown cancels and drains owned run tasks.
When first upgrading from a version without run leases, stop the old backend workers
before starting the new version so untracked live workers cannot be mistaken for orphans.

Automatic schedule occurrences also hold a non-blocking, per-schedule process lease.
Their stable `automation_claim_id` derives from the schedule ID and UTC due time and
is persisted in the Run before execution. If the worker exits before `schedule.fired`
or schedule advancement, another worker completes that bookkeeping for the existing
Run without launching it again, including under the `skip` missed-run policy. Event
audit data includes `scheduled_for` and `automation_claim_id`. Conditional advancement
preserves a concurrent pause or trigger edit. These locks require a local filesystem
with POSIX advisory-lock semantics; stop all older workers before upgrading.

Ledger transactions serialize record updates across API workers. Run summaries include
`event_log_bytes`, a derived JSONL checkpoint: missing/stale summaries rebuild from the
Run and complete event records before allocating another event sequence. Incomplete or
inconsistent event logs fail closed and are not silently truncated. Neither this recovery
nor schedule dispatch asserts exactly-once behavior for an external service.

1. `POST /api/projects` creates the shared opportunity ledger.
2. `POST /api/projects/{project_id}/runs` starts a single main-Agent run and returns `202` with a run record (optional `mode: "deep_research"`, `provider_profile_id`, `resume_session_id`). The run record pins immutable provider and skill snapshots.
3. `GET /api/runs/{run_id}/events` opens a replayable Server-Sent Events stream.
4. `POST /api/runs/{run_id}/permissions/{request_id}` resolves a pending tool request with `allow_once`, `allow_run`, or `deny`.
5. `POST /api/runs/{run_id}/cancel` interrupts the SDK client and cancels the run.
6. `GET /api/projects` lists durable projects ordered by recent updates.
7. `GET /api/projects/{project_id}` and `GET /api/runs/{run_id}` return durable snapshots.
   `GET /api/runs/{run_id}/telemetry` deterministically replays the durable run and
   event log into latency, tool lifecycle, token/cache, cost, provider/model, and
   completion metrics. A metric that was not actually observed is returned as
   `availability: "unavailable"`; it is never guessed or silently replaced with zero.
   `GET|POST /api/projects/{project_id}/access` reads/grants owner-authorized project
   membership and `DELETE /api/projects/{project_id}/access/{principal_id}` revokes it.
   The grant body is `{"principal_id":"...","role":"admin|member|reviewer|viewer"}`;
   ownership itself cannot be reassigned through this endpoint.
8. `POST /api/agents` creates an agent from a template (`general`, `research`, `sourcing`, `marketing`, `docs`, `opportunity`) and generates its Agent Core files. `POST /api/agents/{agent_id}/update` bumps `revision`; running sessions keep their snapshot. Pass `agent_id` in the run creation body to run as that agent (omit it for the default single Agent), and `resume_session_id` to continue a prior SDK session.
9. `POST /api/memory` records a user-entered (immediately approved) memory and accepts
   either absolute `expires_at` or relative `ttl_seconds`; `GET /api/memory?scope=&scope_id=&status=`
   lists authorized records. `POST /api/memory/{id}/decision` takes
   `approve`/`reject`/`disable`, `POST /api/memory/{id}/rollback` restores a prior
   version, and `PATCH /api/memory/{id}/expiry` sets/renews/clears expiry. Agent-proposed
   memories (via the `propose_memory` MCP tool) stay `proposed` until a user decision;
   only approved, non-expired memories are injected into prompts or returned by
   `search_memory`. `POST /api/memory/retrieve` performs deterministic request-signal
   prefetch across explicit authorized scopes and returns matching/ranking/candidate
   metadata. `GET /api/memory/export` downloads authorized active records and minimal
   tombstones with visible provenance and sensitivity but no principal-session binding.
   `DELETE /api/memory/{id}`
   hard-deletes content/current history after persisting a content-free tombstone and
   removes the exact `MEMORY.md` marker. Approved agent-scope memories are mirrored as
   marker lines in the agent's `MEMORY.md`; user freehand edits are preserved. Every
   finished run also appends a dated work-journal entry to the agent's (or project's)
   `diary/YYYY-MM-DD.md`. See [`memory-governance.md`](memory-governance.md) for exact
   lifetime, retrieval, export, deletion, and infrastructure-backup boundaries.
10. `POST /api/schedules` (exactly one of `cron` or `run_once_at`, plus `timezone` and `missed_run_policy`) creates an automation; `POST /api/schedules/{id}/pause|resume|run` manage it. Missed fires while the app was down reconcile at startup: `catch_up` fires once (coalesced), `skip` advances. Automatic due fires skip while their recorded previous Run remains active. The manual `/run` test operation is separate and does not change fire bookkeeping. Scheduled runs use the same permission broker; unapproved high-risk calls time out into denial.
11. `GET /api/providers` lists Anthropic-compatible Provider profiles (never the key; `secret_configured` only). `PUT /api/providers/{id}` upserts a profile — the payload replaces the whole profile, so clients must round-trip tier mappings and capabilities; include `secret` only when rotating the key. `POST /api/providers/{id}/preflight` runs the real-SDK preflight (skill discovery + MCP health + 8-class error diagnosis, bounded by a timeout) and persists `last_verified_at` / `last_error_code`.
12. `GET /api/skills` lists versioned Plugin skills (namespace, content hash, enabled); `POST /api/skills/{id}/enable?enabled=` toggles discovery for future runs. `GET /api/skills/{id}/versions` lists immutable package history, and `POST /api/skills/{id}/rollback` selects one reviewed tenant version by exact `content_hash` (or by an unambiguous, exact declared `version` string). Rollback never deletes history, reruns package review, and preserves the current enablement state.
13. `POST /api/projects/{project_id}/files?filename=&mime=` uploads raw bytes into the project workspace `uploads/` and registers an Artifact; `GET /api/projects/{project_id}/artifacts` lists artifacts and `.../artifacts/{artifact_id}/download` streams one.
14. `GET /api/projects/{project_id}/evidence?q=&kind=&tag=&status=&limit=` queries the evidence ledger. `POST /api/projects/{project_id}/evidence/{evidence_id}/status` (`{"status": "active|stale|removed"}`) implements FR-10 invalidation: stale/removed evidence is appended to linked decisions' `invalidated_by_evidence_ids` and active decisions drop to `needs_review`; restoring reverses it once no invalidating evidence remains. The response lists `affected_decision_ids`. The same propagation is reachable in-run via the permission-gated `update_evidence_status` MCP tool.
15. `POST /api/skills/{skill_id}/test` runs the deterministic package test (frontmatter shape, referenced files, secret and dangerous-command scan, ineffective `allowed-tools` warning) and returns per-check results; it never calls a model.
16. `GET /api/connectors` lists configured connector records (credential locations
    only, never raw secrets); `PUT /api/connectors/{id}` configures one and
    `POST /api/connectors/{id}/revoke|health-check` revokes it or re-evaluates
    configuration/credential presence. Administrator-selected credential environment
    references are restricted to the server-owned connector namespace and are blanked
    in Agent workers. An `ok` health value is not a live third-party handshake; the
    bounded target read/apply request performs the first service-level network check.
17. `PATCH /api/schedules/{id}` applies partial updates (prompt, cron/run_once_at, timezone, missed_run_policy, provider, enabled); exactly one trigger must remain and `next_fire_at` is recomputed when the trigger or timezone changes.
18. High-consequence commerce mutations use `read -> stage -> review -> apply`. The
    main Agent first calls `read_commercial_target`, whose host-owned connector reads
    the current resource and issues a short-lived receipt bound to project, run,
    initialized SDK session, connector configuration, resource, version, and exact
    sanitized state. `stage_commercial_action` accepts only that receipt plus changes
    and reason; target, operation, expected version, and observed state are hydrated by
    the server. A receipt can stage at most one action, and there is deliberately no
    public HTTP stage endpoint. `POST .../{action_id}/review` accepts only `decision`,
    human `reason`, and optional `valid_for_seconds`; the reviewer comes only from
    request authentication. `POST .../{action_id}/apply` accepts only
    `{"reason":"..."}`; source run/session, target, and receipt lineage are hydrated
    from the stored action. Apply revalidates
    source scope, distinct participants, project revision, current policy, external
    version/before-state, recipient/content, connector configuration, and the durable
    idempotency key before a matching adapter may commit. The first installed adapter
    executes only `send_message` through a strict HTTPS JSON connector; unsupported
    operations/connectors remain `not_executed`. External success is recorded only
    with a connector-confirmed result and server-issued receipt. Retryable
    timeout/429/5xx outcomes reuse the same key, while 4xx outcomes terminate.
    `POST .../{action_id}/cancel` idempotently cancels staged/approved actions and
    queued `not_executed` requests, with actor/reason/audit; it rejects attempting,
    retryable/ambiguous, or successful execution. Project transactions use process
    and file locks plus durable atomic snapshots. Review, apply, and cancel are absent
    from Agent/MCP tools. In `trusted_gateway` mode they are authorized only from a
    short-lived request-bound HMAC principal assertion and durable project membership;
    in direct `local_only` mode identity-shaped headers are ignored and a fixed
    loopback audit actor is used without claiming an authenticated principal. The
    `send_message` adapter and its failure/recovery invariants are covered by controlled
    local transports; no authorized sandbox/test-account E2E has proved a real external
    send.
19. `POST /api/intents` creates an explicitly authorized standing intent and can bind
    an existing main `agent_id` and Provider profile. The response persists check
    interval/timeout, per-check and total dollar caps, next/last check state,
    cooldown/trigger limits, expiry, authorization provenance, claim/Run linkage,
    failures, and watcher status. `POST /api/intents/{id}/check` requests an immediate
    bounded evaluation and returns `outcome`, the current intent, and an optional Run;
    it cannot bypass enabled, authorization, cooldown, expiry, active-claim, Provider
    readiness, or budget gates. Pause prevents new checks and cancels a reconciled
    active check; resume is an explicit `user_api` reauthorization. Autonomous checks
    use the same standard main-Agent path but only read-only tools plus the exact
    bound trigger. A completed false/unknown check remains silent. See
    [`standing-intent-watcher.md`](standing-intent-watcher.md) for recovery and budget
    semantics.

`Write` and `Edit` are available to attended single-Agent runs but are not pre-approved: each call crosses the permission broker, and paths must resolve inside the run workspace (symlink escapes are denied without asking). Sandboxed `Bash` follows the SDK-native `autoAllowBashIfSandboxed` path; the runtime fails closed if the sandbox is unavailable, permanently denies `dangerouslyDisableSandbox`, asks before destructive deletion, and denies Bash entirely for scheduled runs.

`GET /api/meta/protocol` is the machine-readable runtime source for event names, run/status/mode values, permission and schedule values, Agent templates, memory values, commercial action operations/status/review decisions, external execution status, and typed-presentation schema/component values. The frontend consumes `event_types` when subscribing to SSE; static TypeScript DTOs must remain OpenAPI-aligned until generation is automated.

## Feishu Run/reply closure

`POST /api/channels/feishu` is an inbound Feishu Event Subscription endpoint, not a
generic webhook-to-webhook relay. Challenge handling is allowed after the normal
signature/decryption boundary. A business message additionally passes replay and
mention checks, captures its `message_id`, `chat_id`, `chat_type`, and thread/root
scope, and creates a delivery obligation bound to the new Run before execution is
scheduled. Raw request signatures, app credentials, and tenant tokens are never
included in that obligation, Run events, or the Agent environment.

The callback is exempt from trusted-gateway headers because Feishu authenticates it
with its own body-bound signature. In `trusted_gateway` mode URL-verification challenge
handling still works. A business event extracts the exact signed-body
`tenant_key + chat_id + sender.open_id` tuple and resolves an administrator-created,
durable binding to the deployment tenant, principal, role, principal session, and
project. Current project membership, role, and `run.create` permission are revalidated
on every dispatch. Missing, revoked, ambiguous, cross-tenant, or role-drifted bindings
return `503`; query-selected and first-project fallback are never used in trusted mode.

Trusted-gateway owners/admins manage those records through:

- `GET|POST /api/channels/feishu/bindings`;
- `GET|DELETE /api/channels/feishu/bindings/{binding_id}`.

Binding persistence and callback/reply behavior are locally implemented and tested.
They do not physically partition the global stores, change `multi_tenant: false`, or
prove delivery in a real Feishu/Lark tenant.

For a completed Run, the authoritative reply body is the last durable `run.result`
or `assistant.message.completed` visible text. If neither contains text, the reply
says that no visible result was available and points to the Run ID. A failed Run gets
a bounded, redacted failure summary. Channel delivery failure never changes the
already-terminal Run status.

Outbound Feishu replies use these official application calls:

- `POST /open-apis/auth/v3/tenant_access_token/internal` with server-held app
  credentials;
- `POST /open-apis/im/v1/messages/{message_id}/reply` with a bearer tenant token,
  text content, captured thread mode, and a deterministic `uuid` no longer than 50
  characters.

Only the official Feishu/Lark HTTPS origins are accepted. The durable delivery ledger
is independent of the event stream and records every `pending`, `attempting`,
`delivered`, and `failed` transition, acknowledgement message ID, bounded attempt
count, safe error code, and next retry time. Network failures, HTTP 429, and HTTP 5xx
are retryable; HTTP 4xx and malformed acknowledgements fail terminally. On startup,
interrupted attempts are retried with the same UUID while its one-hour deduplication
window remains valid. Once that window expires, the worker fails closed with
`IDEMPOTENCY_WINDOW_EXPIRED` rather than risk a second reply.

Missing app credentials produce `CHANNEL_NOT_CONFIGURED`; organization policy denial
produces `CHANNEL_POLICY_DISABLED`. Neither state may be presented as delivery. The
legacy `OPHIRA_CHANNEL_WEBHOOK_URL` remains only a generic notification hook and must
not be substituted for the Feishu reply API. WeChat reply delivery is not implemented.
All repository tests for this path are offline `httpx.MockTransport` tests, not proof
of real Feishu tenant delivery.

## Browser acceptance boundary

The production L1 browser tool and L2 loopback Chrome-CDP connector each passed a
real-browser smoke acceptance on 2026-09-03. The archived reports verify browser-level
selection plus persisted screenshot, Evidence, Artifact, source receipt, and Run
linkage. They do not prove Provider behavior or L3 OS-level computer use. Redirect,
private-network, Service-Worker, cleanup, and connector-confusion edge cases remain
deterministic security-test evidence rather than a claim that every case was exercised
against an external browser environment.

## SSE replay

Each durable event has a monotonically increasing ID such as `run_abcd:17`.
Reconnect with the standard `Last-Event-ID` header; the server replays every later
JSONL event before following live output. A `run.completed`, `run.failed`, or
`run.cancelled` event does not end this replay: observations of committed project
records can follow it, including records recovered during subscription.

After the terminal Run has finished execution, reconciliation has succeeded, and
all durable events have been sent, the server sends one transport control frame:

```text
event: stream.completed
data: {"run_id":"run_abcd","project_id":"project_abcd","last_sequence":17}

```

This frame has no `id` field, is not a JSONL event, and consumes no sequence number.
Clients validate its Run, project, and `last_sequence` against the last durable
sequence received, or the requested checkpoint when the replay is empty. Keep the
durable event ID as the reconnect cursor. Missing, duplicate, or mismatched control
frames are incomplete/invalid replay; EOF alone does not confirm completion. Eval
consumers compare replay against a fresh ledger snapshot after this frame, since
subscription may have added legitimate recovered observations.

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
