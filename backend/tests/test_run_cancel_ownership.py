from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient
from test_companion import PUBLIC_ORIGIN, approve, device_client, owner_client, pair

from ophira_backend.config import Settings
from ophira_backend.constants import EventType, RunStatus
from ophira_backend.events import EventPublisher
from ophira_backend.main import create_app
from ophira_backend.models import ProjectCreate
from ophira_backend.permissions import PermissionBroker
from ophira_backend.runs import RunCancelConflictError, RunManager
from ophira_backend.storage import FileLedger


class WaitingRuntime:
    def __init__(self):
        self.started = asyncio.Event()
        self.cancel_calls: list[str] = []

    async def run(self, _run):
        self.started.set()
        await asyncio.Event().wait()

    async def cancel(self, run_id):
        self.cancel_calls.append(run_id)


def manager(root, runtime=None):
    ledger = FileLedger(root)
    events = EventPublisher(ledger)
    return RunManager(
        ledger,
        events,
        PermissionBroker(ledger, events, timeout_seconds=30),
        runtime or WaitingRuntime(),
    )


@pytest.mark.parametrize("started", [False, True])
async def test_non_owning_manager_cannot_cancel_live_run_or_mutate_its_trace(tmp_path, started):
    runtime = WaitingRuntime()
    owner = manager(tmp_path, runtime)
    foreign = manager(tmp_path)
    foreign.permissions.deny_all = AsyncMock(wraps=foreign.permissions.deny_all)
    project = owner.ledger.create_project(ProjectCreate(name="Synthetic", objective="Ownership"))
    run = await owner.create_and_start(project.id, "Synthetic waiting task")
    if started:
        await runtime.started.wait()
    before = owner.ledger.get_run(run.id)
    before_events = owner.ledger.list_events(run.id)
    try:
        with pytest.raises(RunCancelConflictError, match="another live worker"):
            await foreign.cancel(run.id)
        assert owner.ledger.get_run(run.id) == before
        assert owner.ledger.list_events(run.id) == before_events
        assert not owner._tasks[run.id].done()
        assert runtime.cancel_calls == foreign.runtime.cancel_calls == []
        foreign.permissions.deny_all.assert_not_called()
    finally:
        await owner.stop()


@pytest.mark.parametrize(
    "status",
    [
        RunStatus.QUEUED,
        RunStatus.RUNNING,
        RunStatus.WAITING_PERMISSION,
        RunStatus.CANCELLING,
        RunStatus.PAUSED,
    ],
)
async def test_ownerless_orphan_can_be_cancelled_and_lease_is_released(tmp_path, status):
    runs = manager(tmp_path)
    other = manager(tmp_path)
    project = runs.ledger.create_project(ProjectCreate(name="Synthetic", objective="Orphan"))
    run = runs.ledger.create_run(project.id, "Synthetic orphan")
    runs.ledger.update_run(run.id, status=status)
    result = await runs.cancel(run.id)
    assert result.status == RunStatus.CANCELLED
    assert [event.type for event in runs.ledger.list_events(run.id)] == [
        EventType.RUN_CANCEL_REQUESTED.value,
        EventType.RUN_CANCELLED.value,
    ]
    lease = other._acquire_run_lease(run.id)
    assert lease is not None
    os.close(lease)


@pytest.mark.parametrize("started", [False, True])
async def test_owning_manager_cancels_queued_and_started_tasks(tmp_path, started):
    runtime = WaitingRuntime()
    owner = manager(tmp_path, runtime)
    other = manager(tmp_path)
    project = owner.ledger.create_project(ProjectCreate(name="Synthetic", objective="Owner"))
    run = await owner.create_and_start(project.id, "Synthetic waiting task")
    if started:
        await runtime.started.wait()
    try:
        result = await owner.cancel(run.id)
        assert result.status == RunStatus.CANCELLED
        assert runtime.cancel_calls == [run.id]
        assert (
            len(
                [
                    event
                    for event in owner.ledger.list_events(run.id)
                    if event.type == EventType.RUN_CANCELLED.value
                ]
            )
            == 1
        )
        lease = other._acquire_run_lease(run.id)
        assert lease is not None
        os.close(lease)
    finally:
        await owner.stop()


@pytest.mark.parametrize("status", [RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED])
async def test_terminal_run_is_read_only_even_when_its_owner_still_holds_lease(tmp_path, status):
    owner = manager(tmp_path)
    foreign = manager(tmp_path)
    project = owner.ledger.create_project(ProjectCreate(name="Synthetic", objective="Terminal"))
    run = owner.ledger.create_run(project.id, "Synthetic terminal")
    run = owner.ledger.update_run(run.id, status=status)
    lease = owner._acquire_run_lease(run.id)
    try:
        assert await foreign.cancel(run.id) == run
        assert foreign.ledger.list_events(run.id) == []
        assert foreign.runtime.cancel_calls == []
        await foreign._finalize_cancel(run.id)
        assert foreign.ledger.get_run(run.id) == run
    finally:
        os.close(lease)


async def test_orphan_cleanup_holds_lease_across_awaits_and_blocks_recovery(tmp_path):
    runtime = WaitingRuntime()
    runs = manager(tmp_path, runtime)
    other = manager(tmp_path)
    project = runs.ledger.create_project(ProjectCreate(name="Synthetic", objective="Cleanup"))
    run = runs.ledger.create_run(project.id, "Synthetic orphan")
    reached = asyncio.Event()
    finish = asyncio.Event()

    async def wait_in_cancel(_run_id):
        reached.set()
        await finish.wait()

    runtime.cancel = wait_in_cancel
    pending = asyncio.create_task(runs.cancel(run.id))
    try:
        await reached.wait()
        assert other._acquire_run_lease(run.id) is None
        await other.recover_interrupted()
        assert other.ledger.get_run(run.id).status == RunStatus.CANCELLING
        with pytest.raises(RunCancelConflictError):
            await other.cancel(run.id)
        finish.set()
        assert (await pending).status == RunStatus.CANCELLED
    finally:
        finish.set()
        await asyncio.gather(pending, return_exceptions=True)


async def test_desktop_http_returns_conflict_when_request_hits_foreign_worker(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path)
    runtime = WaitingRuntime()
    owner = create_app(settings, runtime=runtime)
    foreign = create_app(settings, runtime=WaitingRuntime())
    project = owner.state.ledger.create_project(ProjectCreate(name="Synthetic", objective="HTTP"))
    run = await owner.state.runs.create_and_start(project.id, "Synthetic waiting task")
    await runtime.started.wait()
    events = owner.state.ledger.list_events(run.id)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=foreign), base_url="http://localhost"
        ) as client:
            response = await client.post(f"/api/runs/{run.id}/cancel")
        assert response.status_code == 409
        assert "owning worker" in response.json()["detail"]
        assert owner.state.ledger.get_run(run.id).status == RunStatus.RUNNING
        assert owner.state.ledger.list_events(run.id) == events
        assert not owner.state.runs._tasks[run.id].done()
    finally:
        await owner.state.runs.stop()


async def test_paired_companion_http_returns_conflict_on_non_owning_worker(tmp_path):
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        companion_enabled=True,
        companion_public_origin=PUBLIC_ORIGIN,
    )
    runtime = WaitingRuntime()
    owner_app = create_app(settings, runtime=runtime)
    foreign_app = create_app(settings, runtime=WaitingRuntime())
    project = owner_app.state.ledger.create_project(
        ProjectCreate(name="Synthetic", objective="Phone")
    )
    run = await owner_app.state.runs.create_and_start(project.id, "Synthetic waiting task")
    await runtime.started.wait()
    events = owner_app.state.ledger.list_events(run.id)
    try:
        async with owner_client(foreign_app) as owner, device_client(foreign_app) as device:
            pairing_id, code, _ = await pair(owner, device, project.id)
            await approve(owner, device, pairing_id, code)
            response = await device.post(f"/api/companion/runs/{run.id}/cancel", json={})
            assert response.status_code == 409
            assert "owning worker" in response.json()["detail"]
        assert owner_app.state.ledger.get_run(run.id).status == RunStatus.RUNNING
        assert owner_app.state.ledger.list_events(run.id) == events
        assert not owner_app.state.runs._tasks[run.id].done()
    finally:
        await owner_app.state.runs.stop()
