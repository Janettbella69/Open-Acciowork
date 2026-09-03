# OPHIRA backend

FastAPI control plane for the OPHIRA single main Agent. One Claude Agent SDK session per
run drives the full Claude Code toolset plus OPHIRA Plugin Skills and in-process MCP
servers; every run writes evidence, decisions, and artifacts into the durable file ledger.
(The former four-subagent "opportunity team" is gone — Deep Research mode alone may spawn
three read-only research subagents.)

## Run locally

From the repository root (the venv must live at the repo root; rebuild it with `--clear`
after moving or renaming the repo, because venv shebangs hardcode absolute paths):

```bash
python3.11 -m venv .venv --clear
.venv/bin/pip install -e './backend[dev]'
.venv/bin/uvicorn ophira_backend.main:app --app-dir backend/src --reload
```

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

Register and verify the local Chrome bridge through the connector API (Chrome
must already be listening on the chosen loopback port):

```bash
curl -X PUT http://127.0.0.1:8000/api/connectors/chrome_cdp \
  -H 'content-type: application/json' \
  -d '{"type":"chrome_cdp","display_name":"Local Chrome","scope":"browser:attach","endpoint":"http://127.0.0.1:9222"}'
curl -X POST http://127.0.0.1:8000/api/connectors/chrome_cdp/health-check
```

Configure a model endpoint either via `backend/.env` (see `.env.example`; the values
bootstrap an `environment` provider profile) or through the Settings page / Provider API,
which stores profiles in `backend/data/providers/` and keys in the mode-0600 vault at
`backend/data/secrets/`. Coding-subscription keys (Kimi Code, GLM/Qwen Coding Plan) are
licensed for personal coding tools only — never wire them into this backend.

## Verify

```bash
.venv/bin/ruff check backend/src backend/tests
PYTHONPATH=backend/src .venv/bin/pytest backend/tests          # unit + L1 gates (offline)
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
for the architecture red lines enforced by `backend/tests/test_arch_gates.py`.
