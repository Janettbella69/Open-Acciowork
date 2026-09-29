# Events outside a Run

Project revisions, schedule outcomes and Provider preflights have durable streams
independent of the existing Run event sequence. No synthetic Run is created.
`GET /api/meta/protocol` advertises their event names.

| Scope | Live SSE | Bounded history |
| --- | --- | --- |
| Project | `/api/projects/{id}/events` | `/api/projects/{id}/events/history` |
| Provider | `/api/providers/{id}/events` | `/api/providers/{id}/events/history` |

History accepts `after` (default 0), `limit` (1–200), or `tail=true` for the most
recent page. It returns `stream_id`, `events`, and the stream's `last_sequence`.
Start SSE with `?after=last_sequence`; native reconnection sends `Last-Event-ID`,
which takes precedence. A cursor is the exact opaque `stream_id:sequence` from
the same scope. Foreign, malformed and future cursors are rejected. Streams
stay open after an individual Run or check finishes.

Every record carries a monotonic sequence, timestamp, trace, actor, scope and
resource ID. `run_id`, `project_id`, `sdk_session_id` and `provider_profile_id`
are nullable when that identity does not exist. A Run binding is verified from
the real ledger and supplies its original trace and Provider/session metadata.
All data is redacted before fsynced JSONL append; event bodies contain identifiers,
revision numbers, timestamps and classified outcomes, not project or credential
prose. Cross-worker locks serialize sequence allocation and `(type, resource_id)`
deduplication. A corrupt or incomplete log fails closed without truncating history.

Project read permission is checked before history access and before every live
record or heartbeat. Deletion and gateway-assertion expiry stop existing streams.
Provider history requires local access or a tenant administrator; the authenticated
tenant determines its stream, never a request field. Provider configuration remains
deployment-scoped, as before; this does not add multitenant storage isolation.

## What drives each event

- `project.changed`: a committed `project.revision`. This covers uploads, manual
  artifact edits, evidence/decision status and project changes even without a Run.
  History access and live polling repair missed observations. Multiple revisions
  during downtime can coalesce into one notification of the current revision;
  the stream is an invalidation feed, not an edit-by-edit audit. The workspace
  rereads the authorized project after initial history and each notification.
- `schedule.fired/completed/failed/cancelled`: real schedule-bound Run records,
  observed at dispatch, cleanup and replay. Cancellation has its own event.
- `schedule.skipped`: a durable decision for the scheduled occurrence, classified
  as `missed_run_policy` or `active_run`, before advancing its next fire time.
  Recovery of that occurrence completes advancement without starting an Agent.
  Pre-start authorization, policy and Provider failures are classified without
  copying exception text. A private pending receipt is committed before pausing;
  recovery pauses the matching configuration/occurrence and repairs the event,
  while an explicit resume's new fire time remains intact. Failed observation
  never turns an already-created
  manual Run into an unsuccessful start response.
- `provider.preflight.started/passed/failed`: one check ID under a nonblocking
  cross-process Provider lease. Replay only repairs unfinished checks after that
  lease is free. A lost terminal observation is marked `interrupted` (unconfirmed
  outcome), not retried or presented as a certified Provider failure. Configuration
  and credentials are captured atomically; an old check cannot certify a newer
  configuration revision. Cosmetic renaming preserves existing readiness.

The schedule page displays the latest ten schedule observations from a selected
project's last 200 events. Provider settings show persistent preflight status.
Neither feed is merged into conversational output or used as business authority.

## Storage and verification boundaries

Project streams live inside `projects/{id}/observations/` and follow full project
deletion. Content-free schedule provenance remains with other retained project
records when deleting only a conversation. Provider streams live under
`control-events/providers/{local-or-tenant-hash}/{id}/`; revoking a credential
retains these credential-free observations. Application cold backups include them.

Limits are 128 KiB per event, 64 MiB or 100,000 events per stream, and 200 events
per page. Growth at the limit is explicitly rejected. Validation scans the full
bounded log; batch reconciliation skips already-observed schedule outcomes and
reads existing Run metadata. Large-history
performance, operator retention/archival and production proxy/load testing remain
open. There is no silent pruning or sequence reset.

Offline tests cover process-safe appends, torn-log rejection, link defenses,
authorization and deletion, initial/reconnect cursors, committed-revision recovery,
preflight cancellation/configuration changes, dispatch crashes and UI selection
races. Browser discovery returned no available browser in this environment; these
checks do not constitute a visual browser acceptance or real Provider certification.

The independent HTTP runner starts real Uvicorn with temporary data and connects
through socket HTTP/SSE, including live upload notifications, Last-Event-ID replay,
preflight success/failure and schedule success/failure/skip. Its Runtime is an
explicit offline fixture. Run it with:

```sh
PYTHONPATH=backend/src .venv/bin/python backend/scripts/run_scoped_events_e2e.py \
  --report /tmp/ophira-scoped-events.json
```

The [retained report](../evals/reports/scoped-events-20260929.json) has eight passing
checks. Two independent concurrent repetitions also passed after the HTTP exercise
exposed and regression tests fixed a conflict between history recovery's brief
lease and starting a preflight. History and SSE responses explicitly send
`Cache-Control: no-store`.
