# Committed project observations and Run stream completion

PRD §4.12 includes `evidence.added`, `decision.recorded`, `artifact.updated`, and
`memory.proposed` for records with verifiable Run provenance. `artifact.registered`
also uses recoverable, deduplicated publication. The project ledger remains authoritative;
these events tell a client when to read its authorized project again.

## Business record first

`ProjectEventProjector` reads an already committed Evidence, Decision, Artifact, or
MemoryItem, finds its
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

Evidence, Decision, and Artifact observations trigger an authorized `GET project`. Reads
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
The remaining schedule/preflight sub-events are tracked separately in the full PRD
inventory.


## Artifact versions and memory proposals (2026-09-22)

Every registered Artifact version now gets one recoverable `artifact.registered`
observation, retaining its existing serialized Artifact fields plus `schema_version`,
`resource_id`, `artifact_id`, and `recorded_at`. Versions greater than one additionally
get `artifact.updated` with only those identifiers, the version, and creation time.
Both events refer to the Run that actually registered that version. Retrying an
unchanged file in another Run repairs the original observation without taking ownership.
The native registration and media MCP handlers publish immediately after commit;
browser capture, terminal workspace discovery, startup and SSE repair share this path.
A failed Skill submission following successful Artifact registration does not erase
the saved Artifact or its observation.

Artifact version allocation and duplicate detection use the existing cross-process
project lock and immutable snapshots described in [Artifact history](artifact-versions.md).
Only the latest version can be an unchanged duplicate: A → B → A produces v1, v2, v3.
This event delivery preserves the existing binary history and reviewed
[workspace recovery](workspace-recovery.md); it does not replay a filesystem edit.
Native file edits are observed when explicitly registered or discovered during
terminal cleanup. Manual edits without a source Run still need a project-level
event contract and are not assigned an invented Run.

Existing legacy `artifact.registered` rows had no `resource_id` and could repeat.
Recovery validates their Artifact id, project, Run, checksum and version against
the committed record, treats matching rows as an existing observation, and leaves
the original log intact. Inconsistent legacy identity fails reconciliation. This
exception does not allow duplicate modern resource observations.

The existing native `propose_memory` tool now records its server-bound `source_run_id`.
Its schema is unchanged; the model cannot select that source Run. Both these proposals
and end-of-run distillation can emit `memory.proposed`, carrying only the memory id,
resource id, schema version and original creation time. Projection checks the source
Run's project/Agent scope and principal binding. It never copies proposal text,
approves memory, injects memory into an Agent, or mirrors it into `MEMORY.md`.
Approved/corrected proposals retain their original observation identity. Deleted
records are not restored; an already published event retains only its identifier.
Historical tool proposals with no source Run are not assigned guessed provenance.

The workspace has a lazily opened **Run memories** panel. Its API query,
`GET /api/memory?source_run_id=...`, checks the source project's memory-read permission
and applies existing per-item authorization (including private Agent memories).
Events request a fresh authorized read; they do not supply displayed proposal text
or current status. Refreshes are aborted on Run changes and cannot overwrite newer
selections or decisions. New proposals arriving during review are read after the
mutation settles. An unsaved correction survives refresh, failures remain visible,
and the user can refresh again. Existing approval/correction endpoints still enforce
memory-write permission independently.

Backend offline acceptance exercises native MCP dispatch, post-commit notification
failure, exact SSE tails, old-log migration, private-memory authorization, a process
exiting immediately after commits, and competing recovery/registration processes.
Frontend acceptance covers observed updates, task selection races, review/refresh
races, retained edits, failure recovery and Chinese copy. These tests do not prove
real Provider proposal quality, a real browser session, or production durability.

Frozen local verification: backend **1563 passed / 8 live-only skipped**; frontend
**281 passed**, lint and build passed; eval tools **59 passed**, Snapshot **61/61**,
Ruff and diff checks passed. The authenticated-memory cases also fixed an existing
identity mapping defect: persisted Runs provide `principal_role/principal_session_id`,
whereas request principals provide `role/session_id`. Both direct native proposals
and end-of-run distillation now retain the correct identity. Browser discovery
returned no available browser, so visual acceptance is still open.
