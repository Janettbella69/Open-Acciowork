#!/usr/bin/env python3
"""Key-free check that the Agent can work on this host.

Drives one turn through the real backend and the pinned Claude Agent SDK CLI. Only
the model endpoint is synthetic: the scripted local model from backend/tests asks for
a file write, Bash inside the sandbox, Python with the Office libraries, a memory
write, a write outside the project, a delete and a question. A stand-in person
refuses the delete and answers the question. No model account, user data or paid
request is involved, so this proves the host side (CLI start-up, sandbox
prerequisites, the hook, the gateway) and says nothing about a real model endpoint.

Usage (from the repository root, with the backend's interpreter):
    python deploy/shared-box/sandbox_acceptance.py
Exit status 0 means every check passed; the JSON report says which ones ran.
"""

from __future__ import annotations

import json
import sys
import tempfile
from importlib.metadata import version
from pathlib import Path


def repo_root() -> Path:
    for parent in [Path.cwd(), *Path(__file__).resolve().parents]:
        if (parent / "backend" / "src" / "ophira_backend").is_dir():
            return parent
    raise SystemExit("run this from the repository (backend/src/ophira_backend not found)")


ROOT = repo_root()
sys.path[:0] = [str(ROOT / "backend" / "src"), str(ROOT / "backend" / "tests")]

import httpx  # noqa: E402
from fake_model import (  # noqa: E402
    OUTSIDE,
    FakeModel,
    create_fake_model_app,
    drive,
    free_port,
    person,
    serve,
    tool_results,
)
from pydantic import SecretStr  # noqa: E402

from ophira_backend.main import create_app  # noqa: E402
from ophira_backend.settings import Settings  # noqa: E402


def main() -> int:
    OUTSIDE.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(prefix="trademind-acceptance-") as tmp:
        model = FakeModel()
        model_port, backend_port = free_port(), free_port()
        serve(create_fake_model_app(model), model_port)
        settings = Settings(data_dir=Path(tmp) / "data", model="fake-model",
                            model_base_url=f"http://127.0.0.1:{model_port}",
                            model_auth_token=SecretStr("sk-acceptance"),
                            self_url=f"http://127.0.0.1:{backend_port}", max_budget_usd=1)
        app = create_app(settings)
        serve(app, backend_port)
        http = httpx.Client(base_url=f"http://127.0.0.1:{backend_port}", timeout=30)
        project = http.post("/api/projects", json={"name": "acceptance"}).json()
        events = drive(http, project["id"], "第一轮：验收", person)
        main_requests = model.main_requests()
        results = ({r["tool_use_id"]: json.dumps(r, ensure_ascii=False)
                    for r in tool_results(main_requests[-1])} if main_requests else {})
        workspace = app.state.store.workspace(project["id"])
        memory_root = app.state.store.config_dir(project["id"]) / "projects"
        bash = results.get("toolu_01", '"is_error": true')
        checks = {
            "turn_completed": events[-1]["data"].get("status") == "completed",
            "edit_without_click": (workspace / "plan.md").exists(),
            "bash_in_sandbox": '"is_error": true' not in bash,
            "office_libraries": "office ok" in results.get("toolu_02", ""),
            "memory_written": any(memory_root.glob("*/memory/MEMORY.md")),
            "outside_write_refused": not OUTSIDE.exists(),
            "delete_went_to_person": "先别删" in results.get("toolu_05", ""),
            "key_stayed_outside": all(r["_auth"] == "Bearer sk-acceptance" for r in model.requests),
        }
        report = {
            "passed": all(checks.values()),
            "checks": checks,
            "claude_agent_sdk": version("claude-agent-sdk"),
            "error": events[-1]["data"].get("error"),
        }
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
