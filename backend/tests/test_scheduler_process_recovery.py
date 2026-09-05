"""Real process death/contending writers, with no model or external-service calls."""

import asyncio
import multiprocessing
from datetime import timedelta
from pathlib import Path

import pytest

from ophira_backend.constants import EventType, MissedRunPolicy, RunStatus
from ophira_backend.events import EventPublisher
from ophira_backend.models import ProjectCreate, Schedule, utc_now
from ophira_backend.permissions import PermissionBroker
from ophira_backend.runs import RunManager
from ophira_backend.scheduler import ScheduleService
from ophira_backend.storage import FileLedger


class WaitingRuntime:
    async def run(self, run):
        await asyncio.Event().wait()

    async def cancel(self, run_id):
        return None


def build(root: Path):
    ledger = FileLedger(root)
    events = EventPublisher(ledger)
    runs = RunManager(ledger, events, PermissionBroker(ledger, events, 30), WaitingRuntime())
    return ledger, runs, ScheduleService(ledger, runs, events)


def dispatch_until_killed(root, ready, after_audit):
    async def execute():
        _, _, service = build(root)
        publish = service.events.publish

        async def interrupted(run_id, event_type, *args, **kwargs):
            if event_type == EventType.SCHEDULE_FIRED:
                if after_audit:
                    await publish(run_id, event_type, *args, **kwargs)
                ready.set()
                await asyncio.Event().wait()
            return await publish(run_id, event_type, *args, **kwargs)

        service.events.publish = interrupted
        await service.tick()

    asyncio.run(execute())


@pytest.mark.parametrize("after_audit", [False, True])
@pytest.mark.parametrize("policy", list(MissedRunPolicy))
@pytest.mark.asyncio
async def test_killed_dispatcher_releases_lease_without_duplicate_run(
    tmp_path, after_audit, policy,
):
    ledger, runs, service = build(tmp_path)
    project = ledger.create_project(ProjectCreate(name="Recovery", objective="No duplicate runs"))
    due = utc_now() - timedelta(seconds=10)
    schedule = ledger.create_schedule(Schedule(
        project_id=project.id, prompt="Inspect only", cron="*/5 * * * *",
        next_fire_at=due, missed_run_policy=policy,
    ))
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    child = context.Process(target=dispatch_until_killed, args=(tmp_path, ready, after_audit))
    child.start()
    try:
        assert await asyncio.to_thread(ready.wait, 10), "child did not reach crash boundary"
        await service.tick()  # Another OS process must not own the same dispatch.
        assert len(ledger.list_runs(project.id)) == 1
        await runs.recover_interrupted()
        assert ledger.list_runs(project.id)[0].status in {RunStatus.QUEUED, RunStatus.RUNNING}
    finally:
        child.terminate()
        await asyncio.to_thread(child.join, 10)
        if child.is_alive():
            child.kill()
            await asyncio.to_thread(child.join, 5)
        child.close()

    await runs.recover_interrupted()
    await service.reconcile()
    await service.tick()
    records = ledger.list_runs(project.id)
    assert len(records) == 1
    assert records[0].status == RunStatus.FAILED
    assert ledger.get_schedule(schedule.id).last_run_id == records[0].id
    assert ledger.get_schedule(schedule.id).next_fire_at > utc_now()
    assert sum(e.type == EventType.SCHEDULE_FIRED.value
               for e in ledger.list_events(records[0].id)) == 1


def append_events(root, run_id, ready, go):
    async def execute():
        ledger = FileLedger(root)
        publisher = EventPublisher(ledger)
        ready.set()
        assert await asyncio.to_thread(go.wait, 10)
        for index in range(40):
            await publisher.publish(run_id, EventType.TOOL_COMPLETED, {"index": index})

    asyncio.run(execute())


def test_process_writers_keep_event_sequence_and_summary_consistent(tmp_path):
    ledger, _, _ = build(tmp_path)
    project = ledger.create_project(ProjectCreate(name="Concurrent", objective="Replay integrity"))
    run = ledger.create_run(project.id, "No real Agent")
    context = multiprocessing.get_context("spawn")
    go = context.Event()
    ready = [context.Event(), context.Event()]
    children = [context.Process(target=append_events, args=(tmp_path, run.id, item, go))
                for item in ready]
    for child in children:
        child.start()
    try:
        assert all(item.wait(10) for item in ready)
        go.set()
        for child in children:
            child.join(10)
            assert child.exitcode == 0
    finally:
        for child in children:
            if child.is_alive():
                child.kill()
                child.join(5)
            child.close()

    events = ledger.list_events(run.id)
    assert [event.sequence for event in events] == list(range(1, 81))
    summary = ledger.get_run_summary(run.id)
    assert summary.last_sequence == 80
    assert summary.event_counts[EventType.TOOL_COMPLETED.value] == 80
