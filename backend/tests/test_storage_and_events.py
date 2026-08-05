import asyncio
from pathlib import Path

import pytest

from ophira_backend.constants import EventType, RunStatus
from ophira_backend.events import EventPublisher, redact
from ophira_backend.models import EventActor, ProjectCreate
from ophira_backend.storage import FileLedger


def test_file_ledger_keeps_manifest_summary_and_detail(tmp_path: Path) -> None:
    ledger = FileLedger(tmp_path)
    project = ledger.create_project(ProjectCreate(name="Travel kettle", objective="Validate EU"))
    run = ledger.create_run(project.id, "Validate this opportunity")

    assert ledger.get_project(project.id).revision == 1
    assert ledger.get_run(run.id).status == RunStatus.QUEUED
    assert (tmp_path / "_manifest.jsonl").exists()
    assert (tmp_path / "runs" / run.id / "summary.json").exists()


def test_file_ledger_lists_project_runs_newest_first(tmp_path: Path) -> None:
    ledger = FileLedger(tmp_path)
    first_project = ledger.create_project(ProjectCreate(name="Kettle", objective="Validate EU"))
    second_project = ledger.create_project(ProjectCreate(name="Lamp", objective="Validate US"))
    first_run = ledger.create_run(first_project.id, "First pass")
    second_run = ledger.create_run(first_project.id, "Second pass")
    ledger.create_run(second_project.id, "Separate project")

    assert [run.id for run in ledger.list_runs(first_project.id)] == [second_run.id, first_run.id]


@pytest.mark.asyncio
async def test_event_sequence_is_replayable_and_redacted(tmp_path: Path) -> None:
    ledger = FileLedger(tmp_path)
    project = ledger.create_project(ProjectCreate(name="Kettle", objective="Validate"))
    run = ledger.create_run(project.id, "Start")
    publisher = EventPublisher(ledger)

    first = await publisher.publish(
        run.id,
        EventType.TOOL_STARTED,
        {
            "tool_name": "WebFetch",
            "authorization": "Bearer private",
            "nested": {"api-key": "private", "safe": "visible"},
        },
        actor=EventActor(type="agent", name="demand-agent"),
    )
    second = await publisher.publish(run.id, EventType.TOOL_COMPLETED, {"result": "ok"})

    replay = ledger.list_events(run.id, after_sequence=first.sequence)
    assert [event.id for event in replay] == [second.id]
    assert first.data["authorization"] == "[REDACTED]"
    assert first.data["nested"]["api-key"] == "[REDACTED]"
    assert first.data["nested"]["safe"] == "visible"
    summary = ledger.get_run_summary(run.id)
    assert summary.last_sequence == 2
    assert summary.event_counts[EventType.TOOL_STARTED.value] == 1


def test_redact_bounds_unknown_values_and_secrets() -> None:
    assert redact({"password": "x", "value": object()})["password"] == "[REDACTED]"
    assert "private-value" not in redact("Bearer private-value")
    assert "sk-ant-sensitive" not in redact("key=sk-ant-sensitive")


@pytest.mark.asyncio
async def test_terminal_stream_replays_then_closes(tmp_path: Path) -> None:
    ledger = FileLedger(tmp_path)
    project = ledger.create_project(ProjectCreate(name="Kettle", objective="Validate"))
    run = ledger.create_run(project.id, "Start")
    publisher = EventPublisher(ledger)
    await publisher.publish(run.id, EventType.RUN_STARTED, {})
    ledger.update_run(run.id, status=RunStatus.COMPLETED)
    await publisher.publish(run.id, EventType.RUN_COMPLETED, {})

    received = []
    async for event in publisher.stream(run.id, heartbeat_seconds=0.01):
        if event is not None:
            received.append(event.type)
    assert received == [EventType.RUN_STARTED.value, EventType.RUN_COMPLETED.value]


@pytest.mark.asyncio
async def test_stream_waits_for_new_event(tmp_path: Path) -> None:
    ledger = FileLedger(tmp_path)
    project = ledger.create_project(ProjectCreate(name="Kettle", objective="Validate"))
    run = ledger.create_run(project.id, "Start")
    publisher = EventPublisher(ledger)
    stream = publisher.stream(run.id, heartbeat_seconds=1)
    pending = asyncio.create_task(anext(stream))
    await asyncio.sleep(0)
    await publisher.publish(run.id, EventType.RUN_STARTED, {})
    event = await asyncio.wait_for(pending, timeout=1)
    assert event is not None
    assert event.type == EventType.RUN_STARTED.value
    await stream.aclose()
