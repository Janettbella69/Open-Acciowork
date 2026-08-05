import asyncio
import dataclasses
from pathlib import Path

import pytest
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

from ophira_backend.config import Settings
from ophira_backend.constants import AgentTemplate, PermissionDecision
from ophira_backend.events import EventPublisher
from ophira_backend.memory import FileMemoryProvider
from ophira_backend.models import AgentCreate, PermissionResolution, ProjectCreate
from ophira_backend.permissions import PermissionBroker
from ophira_backend.sdk_adapter import ClaudeAgentRuntime, _path_within
from ophira_backend.storage import FileLedger


@dataclasses.dataclass
class FakeToolContext:
    agent_id: str | None = None


def build_runtime(tmp_path: Path):
    ledger = FileLedger(tmp_path / "data")
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
    return ledger, permissions, runtime


def test_path_within_workspace(tmp_path: Path) -> None:
    root = (tmp_path / "workspace").resolve()
    root.mkdir(parents=True)
    (root / "inside.md").touch()
    outside = tmp_path / "outside.md"
    outside.touch()
    link = root / "escape.md"
    link.symlink_to(outside)

    assert _path_within(root, "inside.md") is True
    assert _path_within(root, str(root / "inside.md")) is True
    assert _path_within(root, "../outside.md") is False
    assert _path_within(root, str(outside)) is False
    assert _path_within(root, "escape.md") is False  # symlink resolves outside
    assert _path_within(root, None) is False


@pytest.mark.asyncio
async def test_write_outside_workspace_denied_without_asking(tmp_path: Path) -> None:
    ledger, permissions, runtime = build_runtime(tmp_path)
    agent = ledger.create_agent(AgentCreate(name="通用", template=AgentTemplate.GENERAL))
    project = ledger.create_project(ProjectCreate(name="Kettle", objective="Validate"))
    run = ledger.create_run(project.id, "Start", agent_id=agent.id)
    options = runtime._build_options(run)

    result = await options.can_use_tool(
        "Write", {"file_path": "../outside.md"}, FakeToolContext()
    )

    assert isinstance(result, PermissionResultDeny)
    assert permissions._pending == {}


@pytest.mark.asyncio
async def test_write_inside_workspace_is_allowed_without_a_permission_round_trip(
    tmp_path: Path,
) -> None:
    ledger, permissions, runtime = build_runtime(tmp_path)
    agent = ledger.create_agent(AgentCreate(name="通用", template=AgentTemplate.GENERAL))
    project = ledger.create_project(ProjectCreate(name="Kettle", objective="Validate"))
    run = ledger.create_run(project.id, "Start", agent_id=agent.id)
    options = runtime._build_options(run)

    result = await options.can_use_tool(
        "Write", {"file_path": "report.md"}, FakeToolContext()
    )

    assert isinstance(result, PermissionResultAllow)
    assert permissions._pending == {}


@pytest.mark.asyncio
async def test_bash_is_never_reused_for_run_scope(tmp_path: Path) -> None:
    ledger, permissions, _runtime = build_runtime(tmp_path)
    project = ledger.create_project(ProjectCreate(name="Kettle", objective="Validate"))
    run = ledger.create_run(project.id, "Start")

    async def request_and_resolve(tool_name: str) -> None:
        task = asyncio.create_task(
            permissions.request(run.id, tool_name, {}, agent_id=None, context={})
        )
        for _ in range(200):
            if permissions._pending:
                break
            await asyncio.sleep(0.01)
        request_id = next(iter(permissions._pending))
        await permissions.resolve(
            run.id,
            request_id,
            PermissionResolution(decision=PermissionDecision.ALLOW_RUN),
        )
        await task

    await request_and_resolve("Write")
    assert "Write" in permissions._run_allowed_tools[run.id]

    await request_and_resolve("Bash")
    assert "Bash" not in permissions._run_allowed_tools[run.id]
