from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from ophira_backend.agent_core import create_agent_core
from ophira_backend.config import Settings
from ophira_backend.constants import AgentTemplate, RunMode
from ophira_backend.events import EventPublisher
from ophira_backend.main import create_app
from ophira_backend.memory import FileMemoryProvider
from ophira_backend.models import AgentCreate, ProjectCreate
from ophira_backend.permissions import PermissionBroker
from ophira_backend.sdk_adapter import ClaudeAgentRuntime
from ophira_backend.storage import FileLedger
from ophira_backend.templates import TEMPLATE_SPECS


class FakeRuntime:
    async def run(self, run) -> None:
        return None

    async def cancel(self, run_id: str) -> None:
        return None


def build_runtime(tmp_path: Path, ledger: FileLedger | None = None):
    ledger = ledger or FileLedger(tmp_path / "data")
    memory = FileMemoryProvider(tmp_path / "memory")
    events = EventPublisher(ledger)
    permissions = PermissionBroker(ledger, events, timeout_seconds=30)
    runtime = ClaudeAgentRuntime(
        Settings(data_dir=tmp_path / "data"),
        ledger,
        events,
        permissions,
        memory,
    )
    return ledger, memory, runtime


def test_template_creation_generates_agent_core_files(tmp_path: Path) -> None:
    ledger, _memory, _runtime = build_runtime(tmp_path)
    agent = ledger.create_agent(AgentCreate(name="小研", template=AgentTemplate.RESEARCH))
    spec = TEMPLATE_SPECS[agent.template]
    create_agent_core(ledger.agent_core_dir(agent.id), spec, agent)

    core_dir = ledger.agent_core_dir(agent.id)
    for filename in ("IDENTITY.md", "AGENTS.md", "SOUL.md", "USER.md", "MEMORY.md", "TOOLS.md"):
        assert (core_dir / filename).exists(), filename
    assert (core_dir / "diary").is_dir()
    assert "小研" in (core_dir / "IDENTITY.md").read_text(encoding="utf-8")

    # Core files are never overwritten once the user may have edited them.
    (core_dir / "USER.md").write_text("custom", encoding="utf-8")
    create_agent_core(core_dir, spec, agent)
    assert (core_dir / "USER.md").read_text(encoding="utf-8") == "custom"


@pytest.mark.asyncio
async def test_agent_api_create_and_update_bumps_revision(tmp_path: Path) -> None:
    app = create_app(Settings(data_dir=tmp_path), runtime=FakeRuntime())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        response = await client.post(
            "/api/agents",
            json={"name": "小采", "template": "sourcing"},
        )
        assert response.status_code == 201
        agent = response.json()
        assert agent["revision"] == 1

        response = await client.post(
            f"/api/agents/{agent['id']}/update",
            json={"name": "小采Pro"},
        )
        assert response.status_code == 200
        assert response.json()["revision"] == 2
        assert response.json()["name"] == "小采Pro"

        response = await client.get("/api/agents")
        assert any(item["id"] == agent["id"] for item in response.json())


def test_single_agent_options_gate_write_tools_and_pass_resume(tmp_path: Path) -> None:
    ledger, _memory, runtime = build_runtime(tmp_path)
    agent = ledger.create_agent(AgentCreate(name="通用", template=AgentTemplate.GENERAL))
    project = ledger.create_project(ProjectCreate(name="Kettle", objective="Validate"))
    run = ledger.create_run(
        project.id,
        "Start",
        agent_id=agent.id,
        resume_session_id="session-abc",
        skill_snapshot=runtime.skills.snapshot(),
    )

    options = runtime._build_options(run)

    assert options.agents is None
    assert options.resume == "session-abc"
    assert options.tools == {"type": "preset", "preset": "claude_code"}
    assert "Write" not in options.allowed_tools
    assert "Edit" not in options.allowed_tools
    assert "Bash" not in options.allowed_tools
    assert options.disallowed_tools == []
    assert options.system_prompt["preset"] == "claude_code"
    assert "安全政策" in options.system_prompt["append"]
    assert len(options.plugins) == 1
    assert "/skills/runtime/" in options.plugins[0]["path"]
    assert options.plugins[0]["path"].endswith("ophira-core")
    assert isinstance(options.skills, list)
    assert len(options.skills) == 14
    assert options.enable_file_checkpointing is True


def test_opportunity_template_is_a_normal_single_agent(tmp_path: Path) -> None:
    ledger, _memory, runtime = build_runtime(tmp_path)
    agent = ledger.create_agent(
        AgentCreate(name="机会团队", template=AgentTemplate.OPPORTUNITY)
    )
    project = ledger.create_project(ProjectCreate(name="Kettle", objective="Validate"))
    run = ledger.create_run(project.id, "Start", agent_id=agent.id)

    options = runtime._build_options(run)

    assert options.agents is None
    assert options.disallowed_tools == []


def test_deep_research_must_be_explicit_and_subagents_are_read_only(tmp_path: Path) -> None:
    ledger, _memory, runtime = build_runtime(tmp_path)
    project = ledger.create_project(ProjectCreate(name="Kettle", objective="Validate"))
    run = ledger.create_run(project.id, "Research", mode=RunMode.DEEP_RESEARCH)

    options = runtime._build_options(run)

    assert options.disallowed_tools == []
    assert options.agents is not None
    assert set(options.agents) == {
        "source-mapper",
        "multilingual-researcher",
        "conflict-verifier",
    }
    for agent in options.agents.values():
        assert {"Write", "Edit", "Bash", "Agent"} <= set(agent.disallowedTools or [])
