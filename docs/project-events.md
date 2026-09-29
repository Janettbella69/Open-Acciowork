# Committed project observations and Run stream completion

Manual project changes and schedule/Provider lifecycle now also have independent
[scoped event streams](scoped-events.md). The Run-bound contract below is unchanged;
manual writes are not attributed to an unrelated historical Run.

PRD §4.12 now includes `evidence.added` and `decision.recorded` for records with
verifiable `source_run_id` provenance. The project ledger remains authoritative;
these events tell a client when to read its authorized project again.

## Business record first

`ProjectEventProjector` reads an already committed Evidence or Decision, finds its
original Run, and verifies that Run belongs to the same project. Notifications use
the original Run's envelope, trace, provider and SDK-session metadata. A duplicate
tool request in a later Run repairs the original observation; it does not reassign
the business record or create a second observation in the later Run.

The payload is deliberately small:

```json
{
  "schema_version": 1,
  "resource_id": "evidence_example",
  "evidence_id": "evidence_example",
  "recorded_at": "2026-09-06T00:00:00+00:00"
}
```

Decision notifications use `decision_id` and the decision's `created_at` value.
Neither payload carries record prose, project snapshots, or a `status` field.
The envelope actor is the host's `project-ledger` system actor. Sequence numbers
describe observation order, including later recovery, rather than inventing the
time at which an earlier business decision occurred.

The native ledger MCP handlers notify after each successful commit, including
partial batches. The browser handler reconciles in its `finally` block so a saved
Evidence can still be observed when later Artifact registration fails. All of
these handlers use the existing native SDK MCP server; no tool or input schema
was added. A notification failure does not change a successful business write
into a failed tool response.

## Durable repair and deduplication

The deduplication key is `(run_id, event_type, resource_id)`. `append_event_once`
holds the existing cross-process Run ledger lock across log validation, lookup
and append. There is no separately committed deduplication index. If the JSONL
append succeeds but updating the summary fails, a new worker repairs the summary
from that same log and returns the existing event on retry.

Mismatched event identity, invalid keys, inconsistent sequence or Run scope,
duplicate historical observations, and malformed or incomplete event logs are
rejected. The rejected log is left intact. This builds on the existing file
ledger's atomic writes and replay checks; it is not a disk-repair procedure.

Missing observations are reconciled after runtime execution and cleanup, during
startup recovery (including terminal Runs), before an authorized SSE subscription,
and before declaring that subscription complete. Recovery repeats observation
only; it never repeats a model call or business operation. Missing/unattributable
historical source Runs are skipped without fabricating provenance. Read or
publication failures return an incomplete result and log the exception class
without copying sensitive record contents or exception text.

## A terminal status does not end the stream

Artifact discovery and other host cleanup can run after a Run's terminal status
has been saved. `/api/runs/{run_id}/events` therefore waits until the actual
execution lease is released, including a lease held by another worker. It then
reconciles project observations and drains any newly appended rows. Polling at
most one second apart also discovers writes from another process, whose local
condition variable cannot notify the subscribing worker.

Only then does the endpoint send the control frame advertised as
`stream_end_event` by `/api/meta/protocol`:

```text
event: stream.completed
data: {"run_id":"run_example","project_id":"project_example","last_sequence":42}

```

This frame has no `id` and is not an `EventEnvelope`, `EventType`, persisted event,
or new Run status. Its watermark equals the last durable sequence consumed by
that subscription. A terminal Run with no events can end at sequence zero.
`Last-Event-ID` accepts the existing numeric cursor or the preferred `run_id:N`;
invalid, negative, cross-Run, and future cursors return HTTP 400.

Initial reconciliation failure returns HTTP 503. Failure during final
reconciliation ends the connection without a completion frame, so a client can
reconnect and repair. Project authorization is checked initially and again
before every outgoing record, heartbeat and completion frame; revocation stops
delivery, as does expiry of the connection's authenticated assertion. A network
disconnect alone cannot prove completion.

## Workspace consumption

The workspace verifies Run/project/envelope identity and contiguous sequence,
ignores duplicate replay, and accepts Run status only from Run lifecycle events
with a status declared by protocol metadata. Historical lifecycle replay cannot
temporarily turn a terminal Run back into a running one.

Evidence and Decision observations trigger an authorized `GET project`. Reads
are coalesced into one request at a time, with a fresh trailing read when another
observation arrives. Selection generations and abort signals prevent an old
response from replacing a different project or a newer selection of the same
project. Event payloads never become fabricated Evidence/Decision cards.

The client keeps the connection through terminal status, including reconnecting
to receive a missing tail. It closes only for the matching final watermark and
refreshes the saved Run and project before leaving history recovery. Read errors
remain visible instead of implying that the displayed ledger is fresh.

## Verification and remaining scope

Offline coverage includes actual native MCP requests, partial business commits,
HTTP replay and finalization, revocation, corrupt logs, failed summary writes,
and spawned-process races/crashes. Frontend tests cover terminal tails, replay,
status isolation, project selection races, failed reads, and the actual workspace
rendering saved evidence and decisions after their observations arrive.

Frozen verification on 2026-09-06:

- Backend: `OPHIRA_CONTRACT_PROVIDER= PYTHONPATH=backend/src .venv/bin/python -m pytest backend/tests -q`
  — 1291 passed, 8 live-only skipped.
- Frontend: `npm run test:run` — 225 passed; `npm run lint` and `npm run build` passed.
- Ruff and `git diff --check` passed. The existing Vite chunk-size warning remains
  (main JavaScript 622.69 kB).

The Green Path verifier validates the final control frame and checks a fresh
post-subscription ledger snapshot. Replay also has an overall deadline using the
runner's `timeout_s`, so continuous heartbeats cannot keep acceptance waiting
indefinitely. Failed replay is archived as FAIL with the final durable event log
and does not start the subsequent paid session-resume check. These new verifier
checks were exercised offline; the earlier real-provider reports predate them.

These tests do not call a Provider or prove real-browser EventSource behavior,
production load, remote-filesystem lock semantics, or cross-host recovery.
The completion frame confirms this stream's durable tail, not an external side
effect or successful business outcome. Old frontend and backend versions should
not be mixed; upgrade workers and clients together.

This delivery covers additions attributed to Runs. Record edits and unbound
manual/imported records still need their own project-level event contract.
`artifact.updated`, `memory.proposed`, and the remaining schedule/preflight
sub-events are tracked separately in the full PRD inventory.
