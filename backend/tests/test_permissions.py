import asyncio
from pathlib import Path

import pytest

from ophira_backend.constants import EventType, PermissionDecision, RunStatus
from ophira_backend.events import EventPublisher
from ophira_backend.models import PermissionResolution, ProjectCreate
from ophira_backend.permissions import PermissionBroker
from ophira_backend.storage import FileLedger


@pytest.mark.asyncio
async def test_permission_round_trip_and_run_scope_memory(tmp_path: Path) -> None:
    ledger = FileLedger(tmp_path)
    project = ledger.create_project(ProjectCreate(name="Kettle", objective="Validate"))
    run = ledger.create_run(project.id, "Start")
    ledger.update_run(run.id, status=RunStatus.RUNNING)
    events = EventPublisher(ledger)
    broker = PermissionBroker(ledger, events, timeout_seconds=30)

    pending = asyncio.create_task(
        broker.request(
            run.id,
            "ExternalWrite",
            {"path": "report.json"},
            agent_id="agent-1",
            context={"title": "Write report"},
        )
    )
    for _ in range(20):
        await asyncio.sleep(0)
        requested = [
            event
            for event in ledger.list_events(run.id)
            if event.type == EventType.PERMISSION_REQUESTED.value
        ]
        if requested:
            break
    assert requested
    request_id = requested[-1].data["request_id"]
    await broker.resolve(
        run.id,
        request_id,
        PermissionResolution(decision=PermissionDecision.ALLOW_RUN),
    )
    resolution = await pending
    assert resolution.decision == PermissionDecision.ALLOW_RUN
    assert ledger.get_run(run.id).status == RunStatus.RUNNING

    remembered = await broker.request(
        run.id,
        "ExternalWrite",
        {"path": "second.json"},
        agent_id="agent-1",
        context={},
    )
    assert remembered.decision == PermissionDecision.ALLOW_RUN
