"""Native tool commits, durable repair, and authorization for PRD §4.12."""

import asyncio
import json

import pytest
from httpx import ASGITransport, AsyncClient
from mcp.types import CallToolRequest, CallToolRequestParams
from test_auth_api import principal, request_as, trusted_settings
from test_project_stream import OfflineRuntime, assert_end, frames
from test_project_stream import commit_evidence as commit_user_evidence

from ophira_backend.config import Settings
from ophira_backend.constants import (
    LEDGER_MCP_SERVER_NAME,
    EventType,
    MemoryKind,
    MemoryScope,
    MemorySource,
    PrincipalRole,
    RunStatus,
)
from ophira_backend.events import EventPublisher
from ophira_backend.ledger_tools import build_ledger_tools
from ophira_backend.main import create_app
from ophira_backend.memory import FileMemoryProvider
from ophira_backend.models import ProjectCreate
from ophira_backend.project_events import ProjectEventProjector
from ophira_backend.storage import FileLedger


def setup(tmp_path, identity=None):
    ledger = FileLedger(tmp_path)
    project = ledger.create_project(
        ProjectCreate(name="Records", objective="Observe durable changes"), principal=identity,
    )
    run = ledger.create_run(
        project.id, "Create files and propose memory", agent_id="agent_test", principal=identity,
    )
    memory = FileMemoryProvider(tmp_path / "memory")
    events = EventPublisher(ledger)
    projector = ProjectEventProjector(ledger, events, memory=memory)
    return ledger, project, run, memory, events, projector


def propose(memory, run, **overrides):
    args = {
        "scope": MemoryScope.PROJECT, "scope_id": run.project_id, "source_run_id": run.id,
        "text": "api_key=private-proposal-prose", "kind": MemoryKind.FACT,
        "source": MemorySource.AGENT_PROPOSAL, "principal": run,
    }
    return memory.propose(**(args | overrides))


def register(ledger, run, text):
    (ledger.workspace_for_project(run.project_id) / "report.md").write_text(text)
    return ledger.register_artifact(run.project_id, "report.md", run_id=run.id)


@pytest.mark.asyncio
async def test_versions_reversion_duplicate_run_and_repeated_cleanup(tmp_path):
    ledger, project, run, memory, events, projector = setup(tmp_path)
    first = register(ledger, run, "first")
    second = register(ledger, run, "second")
    third = register(ledger, run, "first")
    assert [first.version, second.version, third.version] == [1, 2, 3]
    assert len({first.id, second.id, third.id}) == 3
    assert first.checksum == third.checksum
    revision = ledger.get_project(project.id).revision
    later = ledger.create_run(project.id, "Try to register the same bytes again")
    assert register(ledger, later, "first").id == third.id
    assert ledger.get_project(project.id).revision == revision
    assert await projector.artifact_registered(project.id, third.id)
    assert ledger.list_events(later.id) == []
    for _ in range(3):
        assert await projector.reconcile_run(run.id)
    registered = [e for e in ledger.list_events(run.id) if e.type == EventType.ARTIFACT_REGISTERED]
    updated = [e for e in ledger.list_events(run.id) if e.type == EventType.ARTIFACT_UPDATED]
    assert {e.data["resource_id"] for e in registered} == {first.id, second.id, third.id}
    assert {e.data["resource_id"] for e in updated} == {second.id, third.id}
    assert all("path" not in e.data and "status" not in e.data for e in updated)


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", [MemoryScope.AGENT, MemoryScope.PROJECT])
@pytest.mark.parametrize("authenticated", [False, True])
async def test_native_memory_tool_binds_source_and_does_not_approve_or_leak_prose(
    tmp_path, scope, authenticated,
):
    owner = principal("alice", PrincipalRole.OWNER) if authenticated else None
    ledger, project, run, memory, events, projector = setup(tmp_path, owner)
    tools = build_ledger_tools(
        ledger, project.id, run_id=run.id, memory=memory, project_events=projector,
        agent_id=run.agent_id if scope == MemoryScope.AGENT else None,
    )
    handler = next(t.handler for t in tools if t.name == "propose_memory")
    response = await handler({"text": "api_key=private-tool-prose", "kind": "preference"})
    item = json.loads(response["content"][0]["text"])
    assert item["source_run_id"] == run.id
    assert item["scope"] == scope
    assert item["status"] == "proposed"
    if owner is not None:
        assert item["tenant_id"] == owner.tenant_id
        assert item["principal_id"] == owner.principal_id
        assert item["principal_role"] == owner.role
        assert item["principal_session_id"] == owner.session_id
    assert memory.search() == []
    assert await projector.reconcile_run(run.id)
    saved = ledger.list_events(run.id)
    assert len(saved) == 1
    assert saved[0].type == EventType.MEMORY_PROPOSED
    assert saved[0].data["memory_id"] == item["id"]
    assert "private-tool-prose" not in saved[0].model_dump_json()
    assert "text" not in saved[0].data and "status" not in saved[0].data
    assert memory.get(item["id"]).status == "proposed"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["artifact", "memory"])
@pytest.mark.parametrize("after_append", [False, True])
async def test_failed_projection_retries_from_truth_in_new_provider(
    tmp_path, monkeypatch, caplog, kind, after_append,
):
    ledger, project, run, memory, events, projector = setup(tmp_path)
    item = register(ledger, run, "draft") if kind == "artifact" else propose(memory, run)
    original = events.publish_once

    async def fail(*args, **kwargs):
        if after_append:
            await original(*args, **kwargs)
        raise OSError("api_key=private-failure")

    monkeypatch.setattr(events, "publish_once", fail)
    assert await projector.reconcile_run(run.id) is False
    assert len(ledger.list_events(run.id)) == int(after_append)
    assert "private-failure" not in caplog.text
    other_ledger = FileLedger(tmp_path)
    recovered = ProjectEventProjector(
        other_ledger, EventPublisher(other_ledger),
        memory=FileMemoryProvider(tmp_path / "memory"),
    )
    assert await recovered.reconcile_run(run.id)
    assert await recovered.reconcile_run(run.id)
    assert [e.data["resource_id"] for e in ledger.list_events(run.id)] == [item.id]


@pytest.mark.asyncio
async def test_memory_list_failure_does_not_hide_artifacts_or_claim_complete(tmp_path, monkeypatch):
    ledger, project, run, memory, events, projector = setup(tmp_path)
    item = register(ledger, run, "draft")
    def fail(**kwargs):
        raise OSError("private-list-error")
    monkeypatch.setattr(memory, "list", fail)
    assert await projector.reconcile_run(run.id) is False
    assert [e.data["resource_id"] for e in ledger.list_events(run.id)] == [item.id]


@pytest.mark.asyncio
async def test_memory_scope_missing_foreign_source_and_deletion_are_not_projected(tmp_path):
    ledger, project, run, memory, events, projector = setup(tmp_path)
    other = ledger.create_project(ProjectCreate(name="Other", objective="Keep scope"))
    foreign = ledger.create_run(other.id, "Foreign source")
    items = [
        propose(memory, run, scope_id=other.id),
        propose(memory, run, scope=MemoryScope.USER, scope_id="default"),
        propose(memory, run, scope=MemoryScope.AGENT, scope_id="other_agent"),
        propose(memory, run, source_run_id=foreign.id),
        propose(memory, run, source_run_id="run_missing"),
        propose(memory, run, source_run_id=None),
    ]
    deleted = propose(memory, run)
    memory.delete(deleted.id, deleted_by="owner", reason="Remove before notification")
    for item in [*items, deleted]:
        assert await projector.memory_proposed(project.id, item.id)
    assert await projector.reconcile_run(run.id)
    assert ledger.list_events(run.id) == []
    assert ledger.list_events(foreign.id) == []


@pytest.mark.asyncio
async def test_correction_before_recovery_retains_original_proposal_observation(tmp_path):
    ledger, project, run, memory, events, projector = setup(tmp_path)
    item = propose(memory, run)
    corrected = memory.correct(item.id, text="User corrected the proposal")
    assert corrected.source == MemorySource.USER
    assert await projector.reconcile_run(run.id)
    event = ledger.list_events(run.id)[0]
    assert event.type == EventType.MEMORY_PROPOSED
    assert event.data["recorded_at"] == item.created_at.isoformat()
    assert "corrected" not in event.model_dump_json()
    assert memory.get(item.id).status == "approved"


@pytest.mark.asyncio
async def test_legacy_artifact_notifications_are_preserved_without_duplicate_projection(tmp_path):
    ledger, project, run, memory, events, projector = setup(tmp_path)
    artifact = register(ledger, run, "original")
    for _ in range(2):
        await events.publish(
            run.id, EventType.ARTIFACT_REGISTERED, artifact.model_dump(mode="json"),
        )
    original = ledger.list_events(run.id)
    assert await projector.reconcile_run(run.id)
    assert ledger.list_events(run.id) == original
    next_version = register(ledger, run, "edit")
    assert await projector.reconcile_run(run.id)
    assert [e.type for e in ledger.list_events(run.id)] == [
        EventType.ARTIFACT_REGISTERED, EventType.ARTIFACT_REGISTERED,
        EventType.ARTIFACT_REGISTERED, EventType.ARTIFACT_UPDATED,
    ]
    assert ledger.list_events(run.id)[-1].data["artifact_id"] == next_version.id


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["project_id", "run_id", "checksum", "version"])
async def test_inconsistent_legacy_notice_does_not_acknowledge_completion(tmp_path, field):
    ledger, project, run, memory, events, projector = setup(tmp_path)
    artifact = register(ledger, run, "original")
    payload = artifact.model_dump(mode="json")
    payload[field] = 999 if field == "version" else "mismatch"
    await events.publish(run.id, EventType.ARTIFACT_REGISTERED, payload)
    before = ledger.list_events(run.id)
    assert await projector.reconcile_run(run.id) is False
    assert ledger.list_events(run.id) == before


@pytest.mark.asyncio
async def test_real_native_mcp_options_wire_both_new_observations(tmp_path):
    app = create_app(Settings(_env_file=None, data_dir=tmp_path))
    ledger = app.state.ledger
    project = ledger.create_project(ProjectCreate(name="MCP", objective="Commit tools"))
    run = ledger.create_run(project.id, "Local native tools")
    (ledger.workspace_for_project(project.id) / "report.md").write_text("first")
    server = app.state.runtime._build_options(run).mcp_servers[LEDGER_MCP_SERVER_NAME]["instance"]
    for name, args in (
        ("register_artifact", {"path": "report.md", "source_ids": []}),
        ("propose_memory", {"text": "Owner prefers EUR", "kind": "preference"}),
    ):
        result = await server.request_handlers[CallToolRequest](CallToolRequest(
            params=CallToolRequestParams(name=name, arguments=args),
        ))
        assert not result.root.isError
    assert [e.type for e in ledger.list_events(run.id)] == [
        EventType.ARTIFACT_REGISTERED, EventType.MEMORY_PROPOSED,
    ]


@pytest.mark.asyncio
async def test_terminal_sse_repairs_artifacts_and_memory_before_watermark(tmp_path):
    app = create_app(Settings(_env_file=None, data_dir=tmp_path), runtime=OfflineRuntime())
    ledger = app.state.ledger
    project = ledger.create_project(ProjectCreate(name="Tail", objective="Repair notices"))
    run = ledger.create_run(project.id, "Historical task")
    ledger.update_run(run.id, status=RunStatus.COMPLETED)
    await app.state.events.publish(run.id, EventType.RUN_COMPLETED)
    register(ledger, run, "first")
    register(ledger, run, "second")
    item = propose(app.state.memory, run)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        response = await client.get(f"/api/runs/{run.id}/events")
        assert response.status_code == 200
        items = frames(response)
        assert [f["event"] for f in items[:-1]] == [
            EventType.RUN_COMPLETED, EventType.ARTIFACT_REGISTERED,
            EventType.ARTIFACT_REGISTERED, EventType.ARTIFACT_UPDATED, EventType.MEMORY_PROPOSED,
        ]
        assert_end(items, run, 5)
        replay = frames(await client.get(
            f"/api/runs/{run.id}/events", headers={"Last-Event-ID": f"{run.id}:4"},
        ))
        assert replay[0]["data"]["data"]["memory_id"] == item.id
        assert_end(replay, run, 5)
        current = await client.get(f"/api/memory?source_run_id={run.id}")
        assert [m["id"] for m in current.json()] == [item.id]
        assert current.headers["cache-control"] == "no-store"
    assert len(ledger.list_events(run.id)) == 5


@pytest.mark.asyncio
async def test_run_memory_query_enforces_project_and_private_identity_scope(tmp_path):
    app = create_app(trusted_settings(tmp_path), runtime=OfflineRuntime())
    ledger, memory = app.state.ledger, app.state.memory
    alice = principal("alice", PrincipalRole.OWNER)
    project = ledger.create_project(
        ProjectCreate(name="Private", objective="Keep access"), principal=alice,
    )
    app.state.project_access.bind_project(project.id, alice)
    run = ledger.create_run(project.id, "Alice run", principal=alice, agent_id="agent_test")
    shared = propose(memory, run)
    private = propose(memory, run, scope=MemoryScope.AGENT, scope_id=run.agent_id)
    later = ledger.create_run(project.id, "Other run", principal=alice)
    propose(memory, later)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://gateway.test",
    ) as client:
        path = f"/api/memory?source_run_id={run.id}"
        authorized = await request_as(client, "GET", path, "alice", PrincipalRole.OWNER)
        assert authorized.status_code == 200
        assert {i["id"] for i in authorized.json()} == {shared.id, private.id}
        denied = await request_as(client, "GET", path, "bob", PrincipalRole.OWNER)
        assert denied.status_code == 403
        assert "private-proposal-prose" not in denied.text
        missing = await request_as(
            client, "GET", "/api/memory?source_run_id=missing", "alice", PrincipalRole.OWNER,
        )
        assert missing.status_code == 404
        granted = await request_as(
            client, "POST", f"/api/projects/{project.id}/access", "alice", PrincipalRole.OWNER,
            json={"principal_id": "bob", "role": "member"},
        )
        assert granted.status_code == 200
        shared_view = await request_as(client, "GET", path, "bob", PrincipalRole.MEMBER)
        assert shared_view.status_code == 200
        assert [i["id"] for i in shared_view.json()] == [shared.id]
        revoked = await request_as(
            client, "DELETE", f"/api/projects/{project.id}/access/bob",
            "alice", PrincipalRole.OWNER,
        )
        assert revoked.status_code == 200
        denied = await request_as(client, "GET", path, "bob", PrincipalRole.MEMBER)
        assert denied.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("authenticated", [False, True])
async def test_finished_runtime_distillation_is_observed_in_the_terminal_tail(
    tmp_path, authenticated,
):
    class RecordUserFact(OfflineRuntime):
        async def run(self, run):
            await commit_user_evidence(app, run)

    settings = (
        trusted_settings(tmp_path) if authenticated else Settings(_env_file=None, data_dir=tmp_path)
    )
    app = create_app(settings, runtime=RecordUserFact())
    owner = principal("alice", PrincipalRole.OWNER) if authenticated else None
    project = app.state.ledger.create_project(
        ProjectCreate(name="Distill", objective="Keep proposals"), principal=owner,
    )
    if owner is not None:
        app.state.project_access.bind_project(project.id, owner)
    run = await app.state.runs.create_and_start(project.id, "Record owner inputs", principal=owner)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        path = f"/api/runs/{run.id}/events"
        request = (
            request_as(client, "GET", path, "alice", PrincipalRole.OWNER)
            if authenticated else client.get(path)
        )
        response = await asyncio.wait_for(request, 5)
        assert response.status_code == 200
        items = frames(response)
        types = [f["event"] for f in items]
        assert types.index(EventType.RUN_COMPLETED) < types.index(EventType.MEMORY_PROPOSED)
        observations = [f["data"]["data"] for f in items if f["event"] == EventType.MEMORY_PROPOSED]
        assert len(observations) == 1
        memory = app.state.memory.get(observations[0]["memory_id"])
        assert memory.source == MemorySource.DISTILLATION and memory.status == "proposed"
        assert memory.source_run_id == run.id
        if owner is not None:
            assert memory.principal_role == owner.role
            assert memory.principal_session_id == owner.session_id
        assert_end(items, run, app.state.ledger.get_run_summary(run.id).last_sequence)
    await app.state.runs.stop()
