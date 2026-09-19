"""Committed artifact versions and memory proposals survive lost notifications."""

import json

import pytest
from mcp.types import CallToolRequest, CallToolRequestParams

from ophira_backend.constants import (
    LEDGER_MCP_SERVER_NAME,
    EventType,
    MemoryKind,
    MemoryScope,
    MemorySource,
    MemoryStatus,
)
from ophira_backend.events import EventPublisher
from ophira_backend.ledger_tools import build_ledger_tools, build_mcp_servers
from ophira_backend.memory import FileMemoryProvider
from ophira_backend.models import ProjectCreate
from ophira_backend.project_events import ProjectEventProjector
from ophira_backend.storage import FileLedger


def setup(root):
    ledger = FileLedger(root)
    project = ledger.create_project(ProjectCreate(name="Resources", objective="Observe saved work"))
    run = ledger.create_run(project.id, "Prepare report and propose a preference")
    memory = FileMemoryProvider(root / "memory")
    events = EventPublisher(ledger)
    projector = ProjectEventProjector(ledger, events, memory)
    return ledger, project, run, memory, events, projector


async def call(tools, name, **arguments):
    result = await next(item.handler for item in tools if item.name == name)(arguments)
    assert result.get("is_error") is False
    return json.loads(result["content"][0]["text"])


@pytest.mark.asyncio
async def test_native_mcp_registers_each_version_once_and_binds_memory_source(tmp_path):
    ledger, project, run, memory, _events, projector = setup(tmp_path)
    server = build_mcp_servers(
        ledger, project.id, run_id=run.id, memory=memory, project_events=projector,
    )[LEDGER_MCP_SERVER_NAME]["instance"]

    async def request(name, arguments):
        result = await server.request_handlers[CallToolRequest](CallToolRequest(
            params=CallToolRequestParams(name=name, arguments=arguments),
        ))
        assert not result.root.isError
        return json.loads(result.root.content[0].text)

    path = ledger.workspace_for_project(project.id) / "report.md"
    path.write_text("Original report: api_key=private-body", encoding="utf-8")
    first = await request("register_artifact", {"path": path.name, "source_ids": []})
    repeated = await request("register_artifact", {"path": path.name, "source_ids": []})
    assert first["id"] == repeated["id"]
    path.write_text("Revised report", encoding="utf-8")
    second = await request("register_artifact", {"path": path.name, "source_ids": []})
    proposed = await request("propose_memory", {"text": "Private preference", "kind": "preference"})
    assert proposed["source_run_id"] == run.id
    assert memory.get(proposed["id"]).status == MemoryStatus.PROPOSED
    assert await projector.reconcile_run(run.id)
    assert await projector.reconcile_run(run.id)
    observations = ledger.list_events(run.id)
    assert [row.type for row in observations] == [
        EventType.ARTIFACT_REGISTERED, EventType.ARTIFACT_UPDATED, EventType.MEMORY_PROPOSED,
    ]
    assert [row.data["resource_id"] for row in observations] == [
        first["id"], second["id"], proposed["id"],
    ]
    assert [row.data["version"] for row in observations[:2]] == [1, 2]
    for row in observations:
        assert row.run_id == run.id and row.project_id == project.id
        assert row.trace_id == run.trace_id and row.actor.type == "system"
        assert set(row.data) <= {
            "schema_version", "resource_id", "artifact_id", "memory_id", "version", "recorded_at",
        }
        assert "private-body" not in row.model_dump_json()
        assert "Private preference" not in row.model_dump_json()
    assert ledger.capture_artifact(project.id, first["id"])[1].startswith(b"Original report")
    assert ledger.capture_artifact(project.id, second["id"])[1] == b"Revised report"


@pytest.mark.asyncio
@pytest.mark.parametrize("after_append", [False, True])
async def test_lost_observation_does_not_fail_tool_and_new_instance_recovers(
    tmp_path, monkeypatch, after_append, caplog,
):
    ledger, project, run, memory, events, projector = setup(tmp_path)
    original = events.publish_once

    async def fail(*args, **kwargs):
        if after_append:
            await original(*args, **kwargs)
        raise OSError("api_key=private-failure")

    monkeypatch.setattr(events, "publish_once", fail)
    tools = build_ledger_tools(
        ledger, project.id, run_id=run.id, memory=memory, project_events=projector,
    )
    (ledger.workspace_for_project(project.id) / "report.md").write_text("Saved")
    saved = await call(tools, "register_artifact", path="report.md", source_ids=[])
    proposed = await call(tools, "propose_memory", text="Saved preference", kind="preference")
    assert not await projector.reconcile_run(run.id)
    assert "private-failure" not in caplog.text
    assert len(ledger.list_events(run.id)) == 2 * int(after_append)
    fresh = FileLedger(tmp_path)
    recovered = ProjectEventProjector(
        fresh, EventPublisher(fresh), FileMemoryProvider(tmp_path / "memory"),
    )
    assert await recovered.reconcile_run(run.id)
    assert await recovered.reconcile_run(run.id)
    assert [row.data["resource_id"] for row in fresh.list_events(run.id)] == [
        saved["id"], proposed["id"],
    ]


@pytest.mark.asyncio
async def test_artifacts_keep_original_run_skip_human_unattributed_and_foreign_sources(tmp_path):
    ledger, project, first, _memory, events, projector = setup(tmp_path)
    second = ledger.create_run(project.id, "Another task")
    other = ledger.create_project(ProjectCreate(name="Other", objective="Keep separate"))
    foreign = ledger.create_run(other.id, "Foreign task")
    workspace = ledger.workspace_for_project(project.id)
    for name, run_id in (("original", first.id), ("manual", None), ("foreign", foreign.id)):
        (workspace / f"{name}.md").write_text(name)
        ledger.register_artifact(project.id, f"{name}.md", run_id=run_id)
    original = ledger.get_project(project.id).artifacts[0]
    # A historical full-Artifact event already emitted by the old finalizer is
    # also a durable observation; reconnecting must not duplicate it.
    await events.publish(first.id, EventType.ARTIFACT_REGISTERED, original.model_dump(mode="json"))
    tools = build_ledger_tools(ledger, project.id, run_id=second.id, project_events=projector)
    repeated = await call(tools, "register_artifact", path="original.md", source_ids=[])
    assert repeated["run_id"] == first.id
    for run in (first, second, foreign):
        assert await projector.reconcile_run(run.id)
    assert len(ledger.list_events(first.id)) == 1
    assert ledger.list_events(second.id) == ledger.list_events(foreign.id) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_id", [None, "agent_resource_test"])
async def test_memory_replay_checks_scope_origin_and_identity_without_changing_approval(
    tmp_path, agent_id,
):
    ledger, project, run, memory, _events, projector = setup(tmp_path)
    run = ledger.update_run(run.id, agent_id=agent_id)
    scope = MemoryScope.AGENT if agent_id else MemoryScope.PROJECT
    scope_id = agent_id or project.id

    def propose(**changes):
        return memory.propose(**{
            "scope": scope, "scope_id": scope_id, "text": "Private preference",
            "kind": MemoryKind.PREFERENCE, "source": MemorySource.AGENT_PROPOSAL,
            "source_run_id": run.id, "principal": run, **changes,
        })

    valid = propose()
    # Approval may precede notification recovery. This remains a historical
    # proposal, not an assertion that it is still pending.
    memory.decide(valid.id, "approve")
    deleted = propose()
    memory.delete(deleted.id, deleted_by="owner", reason="Forget")
    propose(source=MemorySource.USER)
    propose(source_run_id=None)
    propose(scope_id="different_scope")
    from test_auth_api import principal

    from ophira_backend.constants import PrincipalRole
    propose(principal=principal("different-owner", PrincipalRole.OWNER))
    before = memory.list(scope=scope, scope_id=scope_id)
    assert await projector.reconcile_run(run.id)
    assert [row.data["resource_id"] for row in ledger.list_events(run.id)] == [valid.id]
    assert memory.list(scope=scope, scope_id=scope_id) == before
    assert memory.get(valid.id).status == MemoryStatus.APPROVED


@pytest.mark.asyncio
async def test_missing_or_failed_business_write_produces_no_observation(tmp_path, monkeypatch):
    ledger, project, run, memory, _events, projector = setup(tmp_path)
    tools = build_ledger_tools(
        ledger, project.id, run_id=run.id, memory=memory, project_events=projector,
    )
    with pytest.raises((OSError, ValueError)):
        await call(tools, "register_artifact", path="missing.md", source_ids=[])

    def fail(*_args, **_kwargs):
        raise OSError("Memory write failed")

    monkeypatch.setattr(memory, "_write_items", fail)
    with pytest.raises(OSError):
        await call(tools, "propose_memory", text="No commit", kind="fact")
    assert await projector.artifact_saved(project.id, "missing")
    assert await projector.memory_proposed(run.id, "missing")
    assert ledger.list_events(run.id) == []
