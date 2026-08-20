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
