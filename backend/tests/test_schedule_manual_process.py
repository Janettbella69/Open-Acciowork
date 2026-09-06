"""Two spawned workers contend for real scheduler/run leases; no model is used."""

import asyncio
import multiprocessing
import os
from datetime import timedelta
from pathlib import Path

import pytest

from ophira_backend.constants import EventType, ProviderErrorCode, RunStatus
from ophira_backend.events import EventPublisher
from ophira_backend.models import ProjectCreate, Schedule, utc_now
from ophira_backend.permissions import PermissionBroker
from ophira_backend.runs import INTERRUPTED_RUN_MESSAGE, RunManager
from ophira_backend.scheduler import ScheduleRunConflictError, ScheduleService
from ophira_backend.storage import FileLedger


class WaitingRuntime:
    def __init__(self):
        self.started = []
        self.entered = asyncio.Event()

    async def run(self, run):
        self.started.append(run.id)
        self.entered.set()
        await asyncio.Event().wait()

    async def cancel(self, run_id):
        return None


def build(root):
    ledger = FileLedger(Path(root))
    events = EventPublisher(ledger)
    runtime = WaitingRuntime()
    runs = RunManager(ledger, events, PermissionBroker(ledger, events, 30), runtime)
    return ledger, runs, ScheduleService(ledger, runs, events), runtime


def manual_owner(root, schedule_id, phase, connection):
    """Pause at a precise dispatch boundary until the parent kills this process."""

    async def execute():
        _, runs, service, runtime = build(root)
        create = runs.create_and_start

        async def held_create(*args, **kwargs):
            if phase == "before_persist":
                connection.send({"pid": os.getpid(), "run_id": None})
                await asyncio.Event().wait()
            run = await create(*args, **kwargs)
            if phase == "after_persist":
                connection.send({"pid": os.getpid(), "run_id": run.id})
                await asyncio.Event().wait()
            return run

        runs.create_and_start = held_create
        run = await service.run_now(schedule_id)
        # This signal proves run_now returned and released the dispatch lease,
        # while the genuine RunManager still owns the execution lease.
        await asyncio.wait_for(runtime.entered.wait(), 5)
        connection.send({"pid": os.getpid(), "run_id": run.id})
        await asyncio.Event().wait()

    try:
        asyncio.run(execute())
    finally:
        connection.close()


def contending_worker(root, schedule_id, connection):
    async def execute():
        ledger, runs, service, runtime = build(root)
        try:
            before = ledger.get_schedule(schedule_id)
            try:
                await service.run_now(schedule_id)
            except ScheduleRunConflictError as exc:
                conflict = str(exc)
            else:
                conflict = None
            after_manual = ledger.get_schedule(schedule_id)
            await runs.recover_interrupted()
            await service.tick()
            records = ledger.list_runs(before.project_id)
            connection.send({
                "pid": os.getpid(), "conflict": conflict,
                "schedule_before": before.model_dump(mode="json"),
                "schedule_after_manual": after_manual.model_dump(mode="json"),
                "schedule_after_tick": ledger.get_schedule(schedule_id).model_dump(mode="json"),
                "runs": [run.model_dump(mode="json") for run in records],
                "runtime_started": runtime.started,
                "scheduled_events": sum(
                    event.type == EventType.SCHEDULE_FIRED.value
                    for run in records for event in ledger.list_events(run.id)
                ),
            })
        finally:
            await runs.stop()

    try:
        asyncio.run(execute())
    finally:
        connection.close()


def recovery_worker(root, schedule_id, old_run_id, connection):
    async def execute():
        ledger, runs, service, runtime = build(root)
        try:
            original_schedule = ledger.get_schedule(schedule_id)
            await runs.recover_interrupted()
            recovered = ledger.get_run(old_run_id)
            events = ledger.list_events(old_run_id)
            await runs.recover_interrupted()
            recovery_idempotent = ledger.list_events(old_run_id) == events
            implicit_starts = list(runtime.started)
            before_explicit = ledger.list_runs(original_schedule.project_id)

            fresh = await service.run_now(schedule_id)
            await asyncio.wait_for(runtime.entered.wait(), 5)
            connection.send({
                "pid": os.getpid(), "recovered": recovered.model_dump(mode="json"),
                "recovery_idempotent": recovery_idempotent,
                "failure_events": sum(event.type == EventType.RUN_FAILED.value for event in events),
                "implicit_runtime_started": implicit_starts,
                "runs_before_explicit": [run.id for run in before_explicit],
                "fresh": ledger.get_run(fresh.id).model_dump(mode="json"),
                "runtime_started": runtime.started,
                "schedule_unchanged": ledger.get_schedule(schedule_id) == original_schedule,
            })
        finally:
            await runs.stop()

    try:
        asyncio.run(execute())
    finally:
        connection.close()


def start_worker(context, target, *args):
    receiver, sender = context.Pipe(duplex=False)
    child = context.Process(target=target, args=(*args, sender))
    child.start()
    sender.close()
    return child, receiver


def receive(child, connection):
    assert connection.poll(10), f"worker did not reach boundary; exit code {child.exitcode}"
    return connection.recv()


def cleanup(child, connection):
    if child.is_alive():
        child.terminate()
    child.join(5)
    if child.is_alive():
        child.kill()
        child.join(5)
    connection.close()
    child.close()


def create_schedule(root, *, due=True):
    ledger = FileLedger(root)
    project = ledger.create_project(ProjectCreate(name="Process overlap", objective="One dispatch"))
    schedule = ledger.create_schedule(Schedule(
        project_id=project.id, prompt="Synthetic local test", cron="*/5 * * * *",
        next_fire_at=utc_now() + timedelta(seconds=-10 if due else 3600),
    ))
    return ledger, schedule


@pytest.mark.parametrize("phase", ["before_persist", "after_persist", "after_return"])
def test_spawned_manual_owner_excludes_manual_and_tick_workers(tmp_path, phase):
    ledger, schedule = create_schedule(tmp_path)
    context = multiprocessing.get_context("spawn")
    owner, owner_pipe = start_worker(context, manual_owner, tmp_path, schedule.id, phase)
    peer = peer_pipe = None
    try:
        held = receive(owner, owner_pipe)
        assert ledger.get_schedule(schedule.id) == schedule
        peer, peer_pipe = start_worker(context, contending_worker, tmp_path, schedule.id)
        result = receive(peer, peer_pipe)
        peer.join(10)
        assert peer.exitcode == 0
        assert result["pid"] != held["pid"] and os.getpid() not in {result["pid"], held["pid"]}
        expected_conflict = "active run" if phase == "after_return" else "being dispatched"
        assert expected_conflict in result["conflict"]
        assert result["schedule_after_manual"] == result["schedule_before"]
        assert result["runtime_started"] == []
        assert result["scheduled_events"] == 0
        records = result["runs"]
        assert len(records) == (0 if phase == "before_persist" else 1)
        if records:
            assert records[0]["id"] == held["run_id"]
            assert records[0]["status"] in {RunStatus.QUEUED.value, RunStatus.RUNNING.value}
            assert records[0]["automation_claim_id"] is None
        if phase == "after_return":
            after = ledger.get_schedule(schedule.id)
            assert after.next_fire_at > schedule.next_fire_at
            # The automatic overlap skip can advance its due time, while manual
            # runs never pretend to be a scheduled fire or populate its pointers.
            assert after.model_copy(update={"next_fire_at": schedule.next_fire_at}) == schedule
        else:
            assert result["schedule_after_tick"] == result["schedule_before"]
    finally:
        if peer is not None:
            cleanup(peer, peer_pipe)
        cleanup(owner, owner_pipe)


@pytest.mark.parametrize("phase", ["after_persist", "after_return"])
def test_spawned_recovery_after_manual_owner_death_requires_explicit_new_run(tmp_path, phase):
    ledger, schedule = create_schedule(tmp_path, due=False)
    context = multiprocessing.get_context("spawn")
    owner, owner_pipe = start_worker(context, manual_owner, tmp_path, schedule.id, phase)
    peer = peer_pipe = None
    try:
        held = receive(owner, owner_pipe)
        owner.terminate()
        owner.join(10)
        assert not owner.is_alive() and owner.exitcode != 0
        # The second spawned process starts with no inherited tasks/locks and
        # recovers solely from the disk ledger after the OS releases ownership.
        peer, peer_pipe = start_worker(
            context, recovery_worker, tmp_path, schedule.id, held["run_id"],
        )
        result = receive(peer, peer_pipe)
        peer.join(10)
        assert peer.exitcode == 0
        assert result["pid"] != held["pid"]
        recovered = result["recovered"]
        assert recovered["id"] == held["run_id"]
        assert recovered["status"] == RunStatus.FAILED.value
        assert recovered["error_code"] == ProviderErrorCode.SDK_RUNTIME_FAILED.value
        assert recovered["error"] == INTERRUPTED_RUN_MESSAGE
        assert recovered["finished_at"] is not None
        assert result["failure_events"] == 1 and result["recovery_idempotent"]
        assert result["implicit_runtime_started"] == []
        assert result["runs_before_explicit"] == [held["run_id"]]
        fresh = result["fresh"]
        assert fresh["id"] != held["run_id"] and fresh["schedule_id"] == schedule.id
        assert fresh["status"] == RunStatus.RUNNING.value
        assert fresh["automation_claim_id"] is None
        assert result["runtime_started"] == [fresh["id"]]
        assert result["schedule_unchanged"]
        assert ledger.get_schedule(schedule.id) == schedule
        assert len(ledger.list_runs(schedule.project_id)) == 2
    finally:
        if peer is not None:
            cleanup(peer, peer_pipe)
        cleanup(owner, owner_pipe)
