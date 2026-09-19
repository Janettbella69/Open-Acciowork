"""Replay and runtime-finalization integration through the real ASGI routes."""

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient
from test_project_stream import assert_end, commit_evidence, frames, setup
from test_resource_events import call

from ophira_backend.constants import EventType, MemoryStatus, RunStatus
from ophira_backend.ledger_tools import build_ledger_tools


@pytest.mark.asyncio
async def test_terminal_replay_recovers_resource_tail_and_resumes_exact_cursor(tmp_path):
    app, project = setup(tmp_path)
    ledger = app.state.ledger
    run = ledger.create_run(project.id, "Create versions and propose a memory")
    ledger.update_run(run.id, status=RunStatus.COMPLETED)
    terminal = await app.state.events.publish(run.id, EventType.RUN_COMPLETED)
    path = ledger.workspace_for_project(project.id) / "report.md"
    path.write_text("Version one")
    first = ledger.register_artifact(project.id, path.name, run_id=run.id)
    path.write_text("Version two")
    second = ledger.register_artifact(project.id, path.name, run_id=run.id)
    tools = build_ledger_tools(ledger, project.id, run_id=run.id, memory=app.state.memory)
    proposed = await call(tools, "propose_memory", text="Private preference", kind="preference")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        protocol = (await client.get("/api/meta/protocol")).json()
        for kind in (EventType.ARTIFACT_UPDATED, EventType.MEMORY_PROPOSED):
            assert kind in protocol["event_types"]
        response = await client.get(f"/api/runs/{run.id}/events")
        assert response.status_code == 200
        items = frames(response)
        assert [item["data"]["data"]["resource_id"] for item in items[1:-1]] == [
            first.id, second.id, proposed["id"],
        ]
        assert_end(items, run, 4)
        for sequence in range(1, 5):
            replay = frames(await client.get(f"/api/runs/{run.id}/events", headers={
                "Last-Event-ID": f"{run.id}:{sequence}",
            }))
            assert [item["id"] for item in replay[:-1]] == [
                f"{run.id}:{index}" for index in range(sequence + 1, 5)
            ]
            assert_end(replay, run, 4)
        assert "Private preference" not in response.text
        for artifact in (first, second):
            downloaded = await client.get(artifact.download_url)
            assert downloaded.status_code == 200
            assert downloaded.content == ledger.capture_artifact(project.id, artifact.id)[1]
    assert ledger.list_events(run.id)[0] == terminal
    assert app.state.memory.get(proposed["id"]).status == MemoryStatus.PROPOSED


@pytest.mark.asyncio
async def test_run_cleanup_projects_distillation_and_artifacts_before_stream_completes(tmp_path):
    app, project = setup(tmp_path)

    async def execute(run):
        (app.state.ledger.workspace_for_project(project.id) / "report.md").write_text("Saved work")
        await commit_evidence(app, run)

    app.state.runtime.run = execute
    run = await app.state.runs.create_and_start(project.id, "Keep user preference")
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://localhost",
        ) as client:
            response = await asyncio.wait_for(client.get(f"/api/runs/{run.id}/events"), 5)
        assert response.status_code == 200
        items = frames(response)
        types = [item["event"] for item in items]
        assert types.count(EventType.ARTIFACT_REGISTERED) == 1
        assert types.count(EventType.MEMORY_PROPOSED) == 1
        assert types.index(EventType.RUN_COMPLETED) < types.index(EventType.MEMORY_PROPOSED)
        assert_end(items, run, app.state.ledger.get_run_summary(run.id).last_sequence)
        assert all(item.status == MemoryStatus.PROPOSED for item in app.state.memory.list())
    finally:
        await app.state.runs.stop()
