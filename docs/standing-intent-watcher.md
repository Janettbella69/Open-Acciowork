# Standing intent autonomous watcher (FR-26)

This document describes the locally implemented backend contract for autonomous
standing-intent checks. Trusted-gateway principal and project authorization are
implemented, but the deployment is still explicitly single-tenant
(`multi_tenant=false`). This document does not claim a real Provider run, long-running
production quality, or real IM/mobile delivery.

## What the watcher does

`StandingIntentWatcher` scans explicitly authorized and enabled `StandingIntent`
records. For each due record it atomically reserves one check budget and starts one
`RunMode.STANDARD` run through the existing `RunManager`. The run keeps the intent's
`agent_id`, Provider binding, project context, and the normal main-Agent execution
path. The watcher is infrastructure only; it never evaluates the business condition
or calls the trigger itself.

The evaluation prompt makes the following distinction explicit:

- a check being due is not evidence that the condition is true;
- only fresh, sufficiently clear observed evidence may cause a hit;
- false, unknown, ambiguous, or unavailable-source results end with
  `HEARTBEAT_OK` and create no notification;
- the run may use read-only tools plus the trigger tool bound to that exact intent;
- generic notification, file writes, automation changes, external actions, and
  sub-Agent delegation are denied for this unattended run.

A positive result must pass the existing `trigger_standing_intent` MCP tool. That
tool checks the bound intent ID, project, currently active check Run, enabled state,
expiry, remaining trigger count, and cooldown before writing the existing inbox.
A late result from a timed-out or superseded Run returns `stale_check` and cannot
consume trigger budget or notify.

## Authorization boundary

Only these creation paths set `authorization_source` and `authorized_at`:

- `user_api`: an explicit create/resume operation on the user control plane;
- `user_approved_agent`: `create_standing_intent` after the existing tool
  permission flow approves the Agent's proposed commitment. The tool requires a
  real, same-project source Run and copies its main-Agent and Provider bindings;
  a synthetic/missing Run cannot mint this authorization provenance.

Legacy records without both fields are marked `unauthorized`, have no next check,
and are never dispatched. Disabling an intent immediately prevents new checks.
Resuming a legacy record must be an explicit user action and pass
`authorization_source="user_api"`.

In `trusted_gateway` mode, create/pause/resume/check requests use the request-bound HMAC
principal and current durable project membership. A created intent stores that exact
principal binding; every autonomous dispatch revalidates its tenant, principal, role,
project membership, and `run.create` permission before creating a Run. Revoking or
changing membership therefore stops future checks instead of letting an old automation
retain authority. In `local_only` mode only loopback requests are accepted and an
intent carries no authenticated principal binding; identity-shaped client headers are
ignored.

This is a trusted-principal boundary within one configured deployment tenant, not
production multi-tenancy. Global Provider, Agent, Skill, Connector, policy, and
automation stores are not physically tenant-partitioned, so `/api/auth/status` remains
`multi_tenant: false`.

## Persistent state and defaults

Each record persists:

| Control | Default | Purpose |
| --- | ---: | --- |
| `check_interval_minutes` | 1440 | Earliest normal interval between evaluations |
| `check_timeout_seconds` | 1800 | Durable claim lease and orphan timeout |
| `per_check_budget_usd` | 0.50 | Hard SDK budget fuse assigned to one Run |
| `total_budget_usd` | 15.00 | Maximum conservative sum of started Run caps |
| `cooldown_hours` | 24 | Minimum time after a delivered hit |
| `max_triggers` | 3 | Lifetime notification count ceiling |
| `expires_days` | 90 | Authorization lifetime from creation |

The persisted lifecycle includes `next_check_at`, `last_check_at`,
`last_check_run_id`, `check_count`, `consecutive_failures`, last error details,
active claim/Run/owner/lease fields, reserved and committed budgets, and
`watcher_status`.

`budget_reserved_usd` closes the gap between claiming work and persisting a Run.
After a Run is durably attached, its full cap moves to `budget_committed_usd`. The
total budget therefore counts assigned caps conservatively; it is not derived from
the SDK's provider-price estimate and is not represented as an authoritative bill.
The Agent SDK's `max_budget_usd` is a safety fuse, not a guaranteed invoice limit,
and a Provider profile's operator-configured `budget_scale` is applied when mapping
the local cap into SDK accounting units.

`watcher_status` can be `idle`, `checking`, `backoff`, `paused`, `expired`,
`trigger_exhausted`, `budget_exhausted`, or `unauthorized`.

## Concurrency, restart, and failure behavior

Intent read/modify/write operations use an in-process lock plus a cross-process
`flock`. The scanner also holds one advisory leader lock, so multiple API workers
may construct the service while only one performs periodic scans. `flock` requires
the supported Unix-like deployment environment. Followers keep retrying the lock
and take over automatically after the leader stops or its process exits.

Each evaluation has an opaque durable `automation_claim_id`, stored on both the
intent and Run. This provides recovery for every crash boundary:

1. a claim without a Run remains reserved until its lease expires;
2. a Run persisted before `active_check_run_id` was written is recovered by its
   claim ID and is never launched again;
3. a missing/stale Run pointer falls back to the claim-ID lookup;
4. a terminal recovered Run releases the claim exactly once;
5. an orphan beyond its timeout is cancelled, recorded as a failure, and moved to
   backoff rather than duplicated.

Provider registry presence, a selected profile, enabled/production-eligible state,
credential presence, successful preflight, and organization policy are checked
before an evaluation Run is created. A dispatch or terminal Run failure is written
to the intent, surfaced in the inbox with intent/Run linkage where available, and
retried with exponential backoff. Backoff is never shorter than the configured
check interval and is capped at seven days. A completed false/unknown check remains
silent. Generic Run retry is rejected for watcher-bound Runs because it would bypass
the intent's reservation and total-budget gate.

## FastAPI wiring

`main.py` constructs the service from the existing stores and Run manager:

```python
from .intent_watcher import StandingIntentWatcher

intent_watcher = StandingIntentWatcher(ledger, runs, intents, notifications)
application.state.intent_watcher = intent_watcher
```

It starts after `scheduler.start()` and stops before `scheduler.stop()`; the
existing channel-delivery service remains the outer lifecycle:

```python
await scheduler.start()
await intent_watcher.start()
try:
    yield
finally:
    await intent_watcher.stop()
    await scheduler.stop()
```

The explicit check-now endpoint returns the dispatch record while still honoring
authorization, enabled, expiry, cooldown, active-claim, Provider-readiness, and both
budget gates:

```python
@application.post(
    "/api/intents/{intent_id}/check",
    response_model=StandingIntentCheckResponse,
    status_code=202,
)
async def check_intent_now(intent_id: str) -> StandingIntentCheckResponse:
    try:
        result = await intent_watcher.run_now(intent_id)
    except IntentNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Intent not found") from exc
    return StandingIntentCheckResponse(
        outcome=result.outcome,
        intent=result.intent,
        run=result.run,
    )
```

The resume route must re-establish explicit authorization:

```python
intents.set_enabled(intent_id, True, authorization_source="user_api")
```

When `agent_id` or `provider_profile_id` is supplied at creation, the HTTP handler
should resolve it through `ledger.get_agent` or `providers.get` before storing the
intent. Runtime readiness is still rechecked for every dispatch because credentials,
preflight state, or policy can change after creation.

## Offline verification

No test contacts a Provider or spends money. The focused suite covers silent false
checks, the controlled positive trigger, explicit authorization, disabled records,
cooldown/expiry/trigger and dollar budgets, Provider readiness, monitor-only SDK
tools, per-Run cap scaling, concurrent scanners, leader handoff, crash recovery,
orphan timeout, visible failure/backoff, stale Run rejection, and retry bypass.

```bash
.venv/bin/ruff check \
  backend/src/ophira_backend/automation.py \
  backend/src/ophira_backend/automation_tools.py \
  backend/src/ophira_backend/intent_watcher.py \
  backend/tests/test_intent_watcher.py

PYTHONPATH=backend/src .venv/bin/pytest \
  backend/tests/test_intent_watcher.py \
  backend/tests/test_automation.py \
  backend/tests/test_automation_tools.py \
  backend/tests/test_scheduler.py \
  backend/tests/test_sdk_adapter.py -q
```
