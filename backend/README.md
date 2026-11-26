# TradeMind backend

One Claude Agent SDK loop per turn, a workspace per project, and a person who decides on
deletes, approvals and questions. Design and API: [`../docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md).

```bash
# from the repository root
python3.11 -m venv .venv && .venv/bin/pip install -e './backend[dev]'
cp backend/.env.example backend/.env          # the model endpoint and key

cd backend
../.venv/bin/uvicorn ophira_backend.main:app --app-dir src --port 8000
PYTHONPATH=src ../.venv/bin/pytest            # unit and API tests
OPHIRA_E2E_CLI=1 PYTHONPATH=src ../.venv/bin/pytest tests/test_e2e_cli.py   # real CLI, no cost
../.venv/bin/ruff check src tests
```

`ophira-capabilities/` is the local plugin the SDK loads: the Skills, one folder each.
