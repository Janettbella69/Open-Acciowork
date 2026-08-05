# OPHIRA backend

FastAPI service for the OPHIRA product-opportunity team. `Opportunity Lead` delegates focused work to four Claude Agent SDK subagents while every agent reads and updates one durable project ledger.

## Run locally

From the repository root, reuse the local Python 3.11 environment:

```bash
.venv/bin/pip install -e './backend[dev]'
cp backend/.env.example backend/.env
.venv/bin/uvicorn ophira_backend.main:app --app-dir backend/src --reload
```

An Anthropic API key or a supported Claude Code login is required for real Agent runs.
Anthropic-compatible gateways are configured with `ANTHROPIC_BASE_URL`, either
`ANTHROPIC_AUTH_TOKEN` or `ANTHROPIC_API_KEY`, and `OPHIRA_CLAUDE_MODEL`. See
[`../docs/model-providers.md`](../docs/model-providers.md) for current DeepSeek, Kimi,
GLM, and Qwen examples. Project and run records default to `backend/data/` and are
ignored by Git.

## Verify

```bash
.venv/bin/ruff check backend/src backend/tests
PYTHONPATH=backend/src .venv/bin/pytest backend/tests
```

See `docs/backend-api.md` for the frontend contract and observable-event policy.
