"""A terminal Run can still have an owner and a durable tail in another process."""

import asyncio
import multiprocessing
import os
from pathlib import Path

import pytest

from ophira_backend.constants import EventType, RunStatus
from ophira_backend.events import EventPublisher
from ophira_backend.models import ProjectCreate, utc_now
from ophira_backend.permissions import PermissionBroker
from ophira_backend.runs import RunManager
from ophira_backend.storage import FileLedger


class UnusedRuntime:
    async def run(self, run):
        raise AssertionError("This process test must not invoke a model runtime")

    async def cancel(self, run_id):
        raise AssertionError("This process test does not cancel model execution")


def build_manager(root):
    ledger = FileLedger(Path(root))
    events = EventPublisher(ledger)
    return RunManager(ledger, events, PermissionBroker(ledger, events, 30), UnusedRuntime())


def hold_terminal_lease(root, run_id, release, append_tail, connection):
    """Hold the real RunManager lease beyond its persisted terminal status."""
    manager = build_manager(root)
    lease = manager._acquire_run_lease(run_id)
    assert lease is not None
    try:
        manager.ledger.update_run(run_id, status=RunStatus.COMPLETED, finished_at=utc_now())
        asyncio.run(manager.events.publish(
            run_id, EventType.RUN_COMPLETED, {"status": RunStatus.COMPLETED.value},
        ))
        connection.send({"pid": os.getpid(), "phase": "terminal_with_lease"})
        assert release.poll(15), "parent did not release the terminal owner"
        assert release.recv() == "release"
        if append_tail:
            # This publisher and its Condition exist only in the spawned owner.
            asyncio.run(manager.events.publish(
                run_id, EventType.TOOL_COMPLETED, {"synthetic_tail": True},
            ))
    finally:
        os.close(lease)
        release.close()
    connection.send({"pid": os.getpid(), "phase": "lease_released"})
    connection.close()


def start_owner(context, root, run_id, *, append_tail=False):
    receive_release, release = context.Pipe(duplex=False)
    receive, send = context.Pipe(duplex=False)
    owner = context.Process(
        target=hold_terminal_lease,
        args=(root, run_id, receive_release, append_tail, send),
    )
    owner.start()
    send.close()
    receive_release.close()
    return owner, receive, release


def cleanup(owner, connection, release):
    # An Event/Condition can retain a non-robust multiprocessing semaphore when
    # its owner is killed. A pipe lets this fixture clean up without that lock.
    if owner.is_alive():
        try:
            release.send("release")
        except (BrokenPipeError, OSError):
            pass
    owner.join(10)
    if owner.is_alive():
        owner.kill()
        owner.join(5)
    connection.close()
    release.close()
    owner.close()


def create_run(root):
    manager = build_manager(root)
    project = manager.ledger.create_project(ProjectCreate(
        name="Cross-process stream tail", objective="Wait for the durable completion boundary",
    ))
    return manager, manager.ledger.create_run(project.id, "Local synthetic fixture")


@pytest.mark.parametrize("release_mode", ["graceful", "killed"])
def test_execution_probe_sees_terminal_owner_in_another_process(tmp_path, release_mode):
    manager, run = create_run(tmp_path)
    assert manager.is_executing(run.id) is False
    context = multiprocessing.get_context("spawn")
    owner, connection, release = start_owner(context, tmp_path, run.id)
    try:
        assert connection.poll(15), "spawned owner did not acquire its lease"
        ready = connection.recv()
        assert ready["phase"] == "terminal_with_lease"
        assert ready["pid"] != os.getpid()
        assert manager.ledger.get_run(run.id).status == RunStatus.COMPLETED
        # Empty local task bookkeeping cannot override the foreign OS lease.
        assert manager._tasks == {}
        assert manager.is_executing(run.id) is True
        independent = build_manager(tmp_path)
        assert independent.is_executing(run.id) is True
        if release_mode == "graceful":
            release.send("release")
            assert connection.poll(15)
            assert connection.recv()["phase"] == "lease_released"
        else:
            owner.kill()
        owner.join(15)
        assert owner.exitcode == 0 if release_mode == "graceful" else owner.exitcode != 0
        assert manager.is_executing(run.id) is False
        assert independent.is_executing(run.id) is False
        # Repeated probes close their own descriptor instead of retaining a lease.
        assert build_manager(tmp_path).is_executing(run.id) is False
    finally:
        cleanup(owner, connection, release)


@pytest.mark.asyncio
async def test_terminal_stream_waits_for_foreign_tail_without_a_shared_condition(tmp_path):
    manager, run = create_run(tmp_path)
    context = multiprocessing.get_context("spawn")
    owner, connection, release = start_owner(context, tmp_path, run.id, append_tail=True)
    consumer = None
    try:
        assert await asyncio.to_thread(connection.poll, 15)
        ready = connection.recv()
        assert ready["pid"] != os.getpid()
        subscriber = EventPublisher(FileLedger(tmp_path))
        assert subscriber is not manager.events
        held = asyncio.Event()
        finish_checks = []

        async def before_finish():
            busy = manager.is_executing(run.id)
            finish_checks.append(busy)
            if busy:
                held.set()
            return not busy

        async def consume():
            return [event async for event in subscriber.stream(
                run.id, heartbeat_seconds=30, before_finish=before_finish,
            ) if event is not None]

        consumer = asyncio.create_task(consume())
        await asyncio.wait_for(held.wait(), 5)
        assert not consumer.done()
        assert manager.ledger.get_run(run.id).status == RunStatus.COMPLETED
        release.send("release")
        # The owner appends through a separate process's EventPublisher, then
        # releases its OS lease. No same-process Condition notification is sent.
        assert await asyncio.to_thread(connection.poll, 15)
        assert connection.recv()["phase"] == "lease_released"
        replayed = await asyncio.wait_for(consumer, 5)
        await asyncio.to_thread(owner.join, 10)
        assert owner.exitcode == 0
        assert [event.type for event in replayed] == [
            EventType.RUN_COMPLETED.value, EventType.TOOL_COMPLETED.value,
        ]
        assert [event.sequence for event in replayed] == [1, 2]
        assert replayed[1].data == {"synthetic_tail": True}
        assert replayed == manager.ledger.list_events(run.id)
        assert all(event.run_id == run.id and event.project_id == run.project_id
                   for event in replayed)
        assert finish_checks[0] is True and finish_checks[-1] is False
        assert manager.is_executing(run.id) is False
        assert manager.ledger.get_run_summary(run.id).last_sequence == 2
    finally:
        if consumer is not None and not consumer.done():
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)
        cleanup(owner, connection, release)
