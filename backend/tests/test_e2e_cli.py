"""The real Claude Code CLI, end to end, against a scripted local model. Costs nothing.

    OPHIRA_E2E_CLI=1 PYTHONPATH=src pytest tests/test_e2e_cli.py -s

Two turns go through the HTTP API exactly as the browser drives them, and a person
answers each approval. Checked against the textbook audit (2026-11-26):
SDK memory on and written as a file (rows 22/49), Bash on in the sandbox (row 21),
the business background on the default path (row 43), deletes go to the person and
writes outside the project are refused (rows 50-55), the key stays outside the
Agent (gateway), and the next turn resumes the same session.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest
from fake_model import (
    OUTSIDE,
    FakeModel,
    all_text,
    create_fake_model_app,
    drive,
    free_port,
    person,
    serve,
    tool_results,
    user_texts,
)
from pydantic import SecretStr

from ophira_backend.main import create_app
from ophira_backend.settings import Settings

pytestmark = pytest.mark.skipif(os.environ.get("OPHIRA_E2E_CLI") != "1",
                                reason="starts the real CLI; set OPHIRA_E2E_CLI=1")


def test_two_turns_through_the_real_cli(tmp_path):
    OUTSIDE.unlink(missing_ok=True)
    model = FakeModel()
    model_port, backend_port = free_port(), free_port()
    serve(create_fake_model_app(model), model_port)
    settings = Settings(data_dir=tmp_path / "data", model="fake-model",
                        model_base_url=f"http://127.0.0.1:{model_port}",
                        model_auth_token=SecretStr("sk-test-real"),
                        self_url=f"http://127.0.0.1:{backend_port}", max_budget_usd=5)
    app = create_app(settings)
    serve(app, backend_port)
    http = httpx.Client(base_url=f"http://127.0.0.1:{backend_port}", timeout=30)

    project = http.post("/api/projects", json={"name": "宠物用品"}).json()
    events = drive(http, project["id"], "第一轮：我想卖宠物用品到美国", person)
    store = app.state.store
    workspace = store.workspace(project["id"])

    finished = events[-1]
    assert finished["data"] == {"status": "completed", "error": None}, finished
    asked = [e["data"]["tool_name"] for e in events if e["type"] == "permission.requested"]
    assert asked == ["Bash", "AskUserQuestion"]  # the delete and the question, nothing else

    main = model.main_requests()
    last = main[-1]
    results = {r["tool_use_id"]: r for r in tool_results(last)}
    text = {k: json.dumps(v, ensure_ascii=False) for k, v in results.items()}
    assert (workspace / "plan.md").read_text() == "# 选品计划\n"         # edit, no click
    assert results["toolu_01"].get("is_error") is not True               # Bash ran, sandboxed
    assert "plan.md" in text["toolu_01"]
    assert "office ok" in text["toolu_02"]                              # Office libs on PATH
    memories = list((store.config_dir(project["id"]) / "projects").glob("*/memory/MEMORY.md"))
    assert memories and "宠物用品到美国" in memories[0].read_text()      # SDK memory, a file
    assert not OUTSIDE.exists() and "只能写本项目工作区" in text["toolu_04"]
    assert (workspace / "plan.md").exists() and "先别删" in text["toolu_05"]
    assert "美国" in text["toolu_06"]

    first_system = json.dumps(main[0].get("system"), ensure_ascii=False)
    assert "# 业务背景" in first_system and "出海新手" in first_system
    assert "# Memory" in first_system  # the SDK's own memory instructions are on
    offered = all_text(main[0]) + json.dumps(main[0].get("tools"), ensure_ascii=False)
    skills = Path(settings.capabilities_dir, "skills")
    assert all(skill.name in offered for skill in skills.iterdir())  # every plugin Skill
    assert not any("auto-memory" in t and "宠物用品到美国" in t for t in user_texts(main[0]))
    assert all(r["_auth"] == "Bearer sk-test-real" for r in model.requests)

    events = drive(http, project["id"], "第二轮：还记得我想做什么吗", person)
    assert events[-1]["data"]["status"] == "completed"
    second = model.main_requests()[-1]
    replies = [e for e in events if e["type"] == "AssistantMessage"]
    # What the Agent wrote to MEMORY.md in turn one is in front of it in turn two.
    assert replies and "记忆在" in json.dumps(replies[-1]["data"], ensure_ascii=False)
    assert "第一轮" in json.dumps(second["messages"], ensure_ascii=False)  # resumed session
