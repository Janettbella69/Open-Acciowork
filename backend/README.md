# OPHIRA backend

FastAPI control plane for the OPHIRA single main Agent. One Claude Agent SDK session per
run drives the full Claude Code toolset plus OPHIRA Plugin Skills and in-process MCP
servers; every run writes evidence, decisions, and artifacts into the durable file ledger.
(The former four-subagent "opportunity team" is gone — Deep Research mode alone may spawn
three read-only research subagents.)

[Resource observations](../docs/resource-events.md) publish committed artifact versions
and memory proposals into the originating Run's durable stream. Explicit MCP artifact
registration notifies immediately; final workspace discovery and memory distillation
are reconciled before stream completion. Notification failures are recoverable without
repeating business writes. New resource events contain metadata only; download and
memory review retain their existing authorization boundaries.

## Run locally

From the repository root (the venv must live at the repo root; rebuild it with `--clear`
after moving or renaming the repo, because venv shebangs hardcode absolute paths):

```bash
python3.11 -m venv .venv --clear
.venv/bin/pip install -e './backend[dev]'
.venv/bin/uvicorn ophira_backend.main:app --app-dir backend/src --reload
```

Publishing a saved HTML artifact requires trusted-gateway mode and a separate
`OPHIRA_SITE_PUBLIC_ORIGIN=https://pages.example.com`. Route only `/published/`
from that host to the backend; the management API remains on its own host. A
project administrator reviews and approves each exact version and can withdraw
it later. See [site publication setup](../docs/site-publications.md).

Browser L1/L2 support is an explicit optional install because Playwright also
needs a managed Chromium binary:

```bash
.venv/bin/pip install -e './backend[dev,browser]'
.venv/bin/playwright install chromium
```

L1 stores its own persistent profile under `backend/data/browser-profiles/`; it
never reuses a personal browser profile. L2 is available only after the
`chrome_cdp` connector points to a loopback CDP endpoint, is enabled, and passes
its health check. Browser URLs remain subject to the organization domain
allowlist and public-network SSRF checks. Each Chromium request, including a
server-side redirect hop, is paused and revalidated before it is sent; browser
navigation fails closed if that guard cannot be installed. L2 also refuses a
connected context with active Service Workers, bypasses workers for the
OPHIRA-created page, and blocks new registrations without unregistering or
otherwise modifying the user's existing workers or tabs.

Run the repeatable L1 acceptance path against an approved public URL after the
Playwright install. It launches the real managed Chromium binary through the
production browser tool, then verifies the persisted screenshot, Evidence,
Artifact, source receipt, and run linkage. The archived report deliberately omits
arbitrary page text:

```bash
PYTHONPATH=backend/src .venv/bin/python backend/scripts/run_browser_e2e.py \
  --url https://1.1.1.1/cdn-cgi/trace
```

The L2 acceptance harness attaches through the same host connector path. It can
either use a Chrome instance the operator already started with remote debugging,
or launch a dedicated, extension-free Google Chrome profile for a repeatable
test. Dedicated mode never reuses a personal profile; attach mode never closes or
modifies pre-existing tabs/workers. A connected context with any active Service
Worker still fails closed:

```bash
PYTHONPATH=backend/src .venv/bin/python backend/scripts/run_browser_l2_e2e.py \
  --launch-chrome --cdp-endpoint http://127.0.0.1:9337 \
  --url https://1.1.1.1/cdn-cgi/trace
```

The archived 2026-09-03 L1 and L2 reports both passed and live at
`evals/reports/browser-l1-20260903T093547Z.json` and
`evals/reports/browser-l2-20260903T093647Z.json`. They prove the real-browser smoke
path and durable capture linkage; redirect/Service-Worker attack cases remain covered
by deterministic tests, and L3 OS-level computer use is not implemented.

Register and verify the local Chrome bridge through the connector API (Chrome
must already be listening on the chosen loopback port):

```bash
curl -X PUT http://127.0.0.1:8000/api/connectors/chrome_cdp \
  -H 'content-type: application/json' \
  -d '{"type":"chrome_cdp","display_name":"Local Chrome","scope":"browser:attach","endpoint":"http://127.0.0.1:9222"}'
curl -X POST http://127.0.0.1:8000/api/connectors/chrome_cdp/health-check
```

High-consequence commerce writes use `read -> stage -> review -> apply`. The Agent
first calls the host-side `read_commercial_target` MCP tool. Its server-configured
connector reads the current resource and issues a short-lived receipt bound to the
project, run, initialized SDK session, connector, resource, version, and exact
sanitized state. `stage_commercial_action` accepts only that receipt plus the proposed
changes/reason; it never trusts model-supplied target, version, or observed-state
fields. Receipts are single-use for staging. There is deliberately no public HTTP
stage endpoint, and the direct service staging method is legacy local-only.

The first executable adapter supports reviewed `send_message` actions over a strict
versioned HTTPS JSON contract. It reloads and compares the external version,
before-state, recipient/content, connector configuration, current policy, project
revision, approval, and stable idempotency key immediately before commit. The action
is recorded as applied only after the connector confirms success and the server
receipt is durable; retryable transport/429/5xx failures reuse the same key, while
4xx responses terminate. Other operations and connector types remain honestly
`not_executed` until a matching adapter is installed. Commercial connector health
checks validate configuration and credential presence; the bounded adapter request
is the first service-level network check. This path is verified with controlled local
transports only; an authorized sandbox/test account has not yet proved a real external
message send.

The immutable preview records the policy version visible at staging, and an approval
records the policy version it evaluated. Apply enforces the current policy first and
requires a new action/review when an otherwise-allowed policy version changed. Cancel
is idempotent only before an external attempt: staged, approved, and queued
`not_executed` actions can be withdrawn with actor/reason audit; attempting,
retryable/ambiguous, and successful executions cannot be hidden by cancellation.

Agent-created [Skill proposals](../docs/skill-proposals.md) reuse native file tools
and explicit `register_artifact` submission of
`skill-proposals/<name>/manifest.json`; ordinary artifacts and workspace scanning
do not submit candidates. The host freezes and scans the package outside the
installed registry. The Skills page provides a separate proposal queue with frozen
text previews, original downloads, source provenance, scan results and review history.
Only a current project owner/admin (or the fixed local owner in local-only mode)
can publish or reject through `/api/skill-proposals/{id}/review`. Publication binds
the candidate hash, review revision and current selection generation, then commits
an immutable version and receipt with enablement off. Interrupted review recovery
uses the exact receipt; absent a receipt, a new human decision is required.
Existing Run snapshots retain their pinned Skills, and enablement remains separate.
This feature is verified with offline native MCP/HTTP, UI and process tests; it does
not establish live model proposal quality or production isolation.

The [private Skill repository](../docs/private-skill-repository.md) also accepts human
ZIP imports without inventing Agent provenance. `POST /api/skills/install` now requires
a `request_id` query parameter and returns a frozen pending import; administrators
publish through `/api/skills/imports/{id}/review` after inspecting files and scans.
Publication remains disabled. The repository UI supports separately reviewed selection
changes, exact historical ZIP export, member read-only access and durable import/review
retries. Deployment scope and short-lived signed identity are rechecked before writes.
Cold backup validates import history and shared Registry publication receipts; reconcile
unfinished human reviews in the original application before taking a snapshot.

The [application cold backup CLI](../docs/application-backup.md),
`python -m ophira_backend.application_backup create|restore`, copies the complete
data directory and application Plugin together. Application construction and the
entire ASGI lifespan hold a shared process lease; backup requires an exclusive lease
and terminal Runs. Stop all old workers before upgrading to this protocol and keep
ASGI lifespan enabled. Restoration validates frozen proposals against their project,
Run, Artifact and publication receipts, then reconciles only interrupted proposal
records on a private copy before publishing to a new directory. It never starts SDK
sessions, schedules, notifications or commercial writes. Data-directory credentials
are included under private local permissions; `.env`/external keys, backup encryption,
cross-host session resume and production disaster recovery remain separate concerns.

Project/action reads and human review/apply/cancel are available through HTTP. In
`trusted_gateway` mode, every normal control-plane API request requires a short-lived,
request-bound HMAC
assertion carrying tenant, principal, role, session, timestamp, and a single-use
nonce. Project membership and role permissions are then revalidated from the durable
server ledger. In `local_only` mode the control plane accepts only direct loopback
requests and uses one fixed local audit actor without claiming an authenticated
principal; identity-shaped client headers are ignored. The Feishu callback is the
exception to gateway headers because its request body has its own Feishu signature
boundary.
`GET /api/auth/status` always reports `multi_tenant: false`: trusted mode establishes
a trusted principal inside one configured deployment tenant, but global Provider,
Agent, Skill, Connector, policy, and automation stores are not physically tenant
partitioned yet. It must not be advertised as production multi-tenancy. Project owners
manage durable non-owner membership through
`GET|POST /api/projects/{project_id}/access` and
`DELETE /api/projects/{project_id}/access/{principal_id}`. The asserted role must match
the current stored membership on every project request, so stale/elevated claims fail
closed.
Review/apply/cancel never appear on the Agent or MCP surface. Connector credential
environment references are restricted to the server-owned `OPHIRA_CONNECTOR_*`
namespace (plus two built-in legacy names), and every configured name is explicitly
blanked in Agent worker environments.

Commercial HTTPS endpoints reject loopback, link-local, private, reserved, and other
non-public IP literals. Before every credential-bearing attempt, the adapter resolves
the hostname and requires every returned address to be globally routable. It then sends
that attempt to one validated numeric address (rather than allowing the transport to
resolve again), while retaining the configured hostname in the HTTP `Host` header and
TLS SNI/certificate verification. Environment proxies, connection reuse, and redirects
are disabled. This closes the adapter's DNS-validation/connection rebinding window; a
deployment that replaces the adapter's client or transport must preserve the same
address pinning and TLS-hostname guarantees.

Successful main-Agent web reads and browser captures also create an immutable
server-issued source receipt under `backend/data/receipts/`. New `web`/`tool`
Evidence must cite the receipt ID returned by that same run and initialized SDK
session; canonical source, retrieval time, payload hash, and object version are
hydrated and checked by the server. Because built-in SDK tool renderers can retain
host-added fields only in transcript metadata, the `PostToolUse` hook also mirrors only
the receipt IDs minted by that exact hook invocation through the model-visible
`additionalContext` channel. Receipt claims embedded in external payloads are stripped,
and delegated scratch reads receive neither receipts nor trusted receipt context.
Aggregate native-search responses retain an honest `ophira-search://` canonical source;
the Green Path verifier accepts it only when the Evidence points to an integrity-checked
Web receipt issued by the same Run.
Receipts can be listed and replayed read-only at
`GET /api/projects/{project_id}/source-receipts` and
`GET /api/projects/{project_id}/source-receipts/{receipt_id}`. Authenticated receipts
also bind the durable run's tenant, principal, role, and principal session; local-only
receipts explicitly retain project/run/SDK-session scope without claiming a tenant
identity. Both read APIs pass through the same control-plane project authorization.

Memory records support explicit absolute expiry or a bounded TTL. Expired records remain
owner-visible for governance but are excluded from prompt recall, MCP search, and the
deterministic request-signal retrieval API. Authorized JSON export exposes source/origin,
run/anchor provenance, confidence, sensitivity, status, and expiry without exporting the
principal-session binding. Hard delete persists a minimal content-free tombstone first,
then scrubs the live snapshot, version history, and exact agent `MEMORY.md` marker; startup
recovery completes an interrupted scrub. Deployment backups and physical-media erasure
remain operator responsibilities. The full contract and endpoint shapes are documented in
[`docs/memory-governance.md`](../docs/memory-governance.md).

Owners/admins can also [delete one conversation](../docs/conversation-deletion.md) while keeping
the project. Review resolves the full resume chain and distinguishes removed chat/session copies
from retained files, evidence, memories and other project records. A durable operation prevents
resuming or recreating deleted Runs; interrupted cleanup is retried before worker startup.
Content-free provenance keeps surviving project exports, recovery checkpoints and Skill backups
verifiable. The EN/ZH workspace exposes review, explicit confirmation and reloadable progress.

### Feishu reply channel (FR-24)

The Feishu route accepts a business message only after timestamp/signature checks,
optional payload decryption, replay protection, and the group-mention gate. For an
accepted message it captures the verified `message_id` and chat/thread scope in a
separate delivery obligation before the Run task starts. A terminal Run then replies
to that original message with the final visible result (or a safe failure summary).
The generic `OPHIRA_CHANNEL_WEBHOOK_URL` notification hook is not used as a Feishu
reply transport.

Configure a Feishu custom app with bot capability and message-send permission, add
the bot to the target chat, then set the inbound and outbound values documented in
`.env.example`: `OPHIRA_FEISHU_ENCRYPT_KEY`, `OPHIRA_FEISHU_BOT_OPEN_ID`,
`OPHIRA_FEISHU_APP_ID`, and `OPHIRA_FEISHU_APP_SECRET`. The outbound adapter obtains
an app `tenant_access_token` and calls the official reply-message API. Its base URL is
pinned to `https://open.feishu.cn` or `https://open.larksuite.com`; redirects are not
followed. App credentials and tenant tokens stay in the control plane and are
explicitly shadowed out of Agent worker environments.

Delivery state is stored under `backend/data/channel-deliveries/` as atomic,
mode-0600 snapshots with `pending -> attempting -> delivered|failed` history. The
worker recovers interrupted attempts at startup, reuses a deterministic Feishu UUID,
retries network errors, HTTP 429, and HTTP 5xx with bounded backoff, and treats HTTP
4xx as terminal. It stops an ambiguous crash replay after Feishu's one-hour UUID
deduplication window instead of risking a duplicate message. A new business message
is rejected before Run creation when outbound credentials or organization channel
policy are unavailable; a credential or policy failure discovered after a durable
obligation exists is recorded as an explicit terminal failure, never as sent.

The repository verifies this adapter offline with `httpx.MockTransport`; it has not
performed or claimed a real-tenant Feishu E2E. Production acceptance still requires
a dedicated test app/chat that proves inbound verification, Run creation, reply
acknowledgement, permission behavior, retry telemetry, and credential rotation.

In `trusted_gateway` mode, Feishu challenge verification remains reachable through its
own Feishu signature boundary. Business dispatch resolves an administrator-created,
durable mapping from the exact signed-body tenant/chat/user tuple to a tenant,
principal, role, principal session, and project; membership and `run.create` are
revalidated for every event. Administrators manage mappings through
`GET|POST /api/channels/feishu/bindings` and
`GET|DELETE /api/channels/feishu/bindings/{binding_id}`. Missing, revoked, ambiguous,
cross-tenant, or role-drifted mappings return `503`; the handler never accepts a
query-selected or first-project fallback in trusted mode. This local identity closure
does not change the `multi_tenant: false` deployment truth or substitute for a
real-tenant Feishu E2E.

Configure a model endpoint either via `backend/.env` (see `.env.example`; the values
bootstrap an `environment` provider profile) or through the Settings page / Provider API,
which stores profiles in `backend/data/providers/` and keys in the mode-0600 vault at
`backend/data/secrets/`. Coding-subscription keys (Kimi Code, GLM/Qwen Coding Plan) are
licensed for personal coding tools only — never wire them into this backend.

## Verify

```bash
.venv/bin/ruff check backend/src backend/tests
PYTHONPATH=backend/src .venv/bin/pytest backend/tests          # unit + L1 gates (offline)
PYTHONPATH=evals/tools python3 evals/tools/run_snapshot_evals.py  # 61 offline cases
```

Live verification (real endpoint, real money, env-gated):

```bash
# Green Path E2E gate (PRD §6.2): 3 consecutive full-chain rounds
PYTHONPATH=backend/src .venv/bin/python backend/scripts/run_green_path.py --provider deepseek --rounds 3

# Provider contract matrix (PRD §4.9)
OPHIRA_CONTRACT_PROVIDER=deepseek PYTHONPATH=backend/src .venv/bin/pytest backend/tests/contract -v

# Eval L2 benchmark tasks
PYTHONPATH=backend/src .venv/bin/python backend/scripts/run_l2.py --dry-run
```

Reports land in `evals/reports/`. See `docs/backend-api.md` for the frontend contract and
observable-event policy, and the PRD (`docs/OPHIRA-PRD-and-Technical-Architecture-v2.md`)
for the architecture red lines enforced by `backend/tests/test_arch_gates.py`. The
FR-26 autonomous-check safety, budget, and recovery contract is documented in
`docs/standing-intent-watcher.md`.
