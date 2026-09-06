"""Manual and due dispatch share one schedule's durable overlap boundary."""

import asyncio
from contextlib import contextmanager
from datetime import timedelta

import pytest
from httpx import ASGITransport, AsyncClient

from ophira_backend.config import Settings
from ophira_backend.constants import TERMINAL_RUN_STATUSES, EventType, MissedRunPolicy, RunStatus
from ophira_backend.events import EventPublisher
from ophira_backend.main import create_app
from ophira_backend.models import ProjectCreate, Schedule, utc_now
from ophira_backend.permissions import PermissionBroker
from ophira_backend.runs import RunManager
from ophira_backend.scheduler import ScheduleRunConflictError, ScheduleService
from ophira_backend.storage import FileLedger


class WaitingRuntime:
    async def run(self, run):
        await asyncio.Event().wait()

    async def cancel(self, run_id):
        return None


def build(root):
    ledger = FileLedger(root)
    events = EventPublisher(ledger)
    runs = RunManager(ledger, events, PermissionBroker(ledger, events, 30), WaitingRuntime())
    return ledger, runs, ScheduleService(ledger, runs, events)


def create_schedule(ledger, *, due=False, enabled=True):
    project = ledger.create_project(ProjectCreate(name="Overlap", objective="One task at a time"))
    return ledger.create_schedule(Schedule(
        project_id=project.id, prompt="Inspect only", cron="*/5 * * * *", enabled=enabled,
        next_fire_at=utc_now() + timedelta(seconds=-10 if due else 3600),
    ))


@pytest.mark.asyncio
async def test_api_rejects_overlapping_manual_runs_without_changing_cadence(tmp_path):
    app = create_app(Settings.model_construct(data_dir=tmp_path), runtime=WaitingRuntime())
    ledger = app.state.ledger
    schedule = create_schedule(ledger, enabled=False)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as c:
            first = await c.post(f"/api/schedules/{schedule.id}/run")
            assert first.status_code == 202
            second = await c.post(f"/api/schedules/{schedule.id}/run")
            assert second.status_code == 409
            assert second.json()["detail"] == (
                "This schedule already has an active run. Wait for it to finish or cancel it "
                "in the workspace before running again."
            )
            assert ledger.get_schedule(schedule.id) == schedule
            assert len(ledger.list_runs(schedule.project_id)) == 1
    finally:
        await app.state.runs.stop()


@pytest.mark.asyncio
async def test_due_dispatch_detects_manual_run_even_without_last_run_pointer(tmp_path):
    ledger, runs, service = build(tmp_path)
    schedule = create_schedule(ledger, due=True)
    try:
        manual = await service.run_now(schedule.id)
        assert ledger.get_schedule(schedule.id).last_run_id is None
        await service.tick()
        assert [r.id for r in ledger.list_runs(schedule.project_id)] == [manual.id]
        assert ledger.get_schedule(schedule.id).next_fire_at > utc_now()
    finally:
        await runs.stop()


@pytest.mark.parametrize("run_status", list(RunStatus))
@pytest.mark.asyncio
async def test_manual_overlap_uses_all_durable_statuses_not_last_run_pointer(tmp_path, run_status):
    ledger, runs, service = build(tmp_path)
    schedule = create_schedule(ledger)
    earlier = ledger.create_run(schedule.project_id, "Earlier manual", schedule_id=schedule.id)
    ledger.update_run(earlier.id, status=run_status)
    unrelated = ledger.create_run(schedule.project_id, "Interactive task")
    assert unrelated.status == RunStatus.QUEUED
    try:
        if run_status in TERMINAL_RUN_STATUSES:
            assert (await service.run_now(schedule.id)).id != earlier.id
        else:
            with pytest.raises(ScheduleRunConflictError, match="already has an active run"):
                await service.run_now(schedule.id)
        assert ledger.get_schedule(schedule.id) == schedule
    finally:
        await runs.stop()


@pytest.mark.parametrize("first_manual", [True, False])
@pytest.mark.asyncio
async def test_dispatch_lease_covers_await_and_durable_run_covers_after_release(
    tmp_path, first_manual,
):
    ledger, runs, first = build(tmp_path)
    _, peer_runs, peer = build(tmp_path)
    schedule = create_schedule(ledger, due=True)
    persisted = asyncio.Event()
    release = asyncio.Event()
    publish = first.events.publish

    async def slow_publish(run_id, event_type, *args, **kwargs):
        event = await publish(run_id, event_type, *args, **kwargs)
        if event_type == EventType.RUN_CREATED:
            persisted.set()
            await release.wait()
        return event

    first.events.publish = slow_publish
    pending = asyncio.create_task(first.run_now(schedule.id) if first_manual else first.tick())
    try:
        await asyncio.wait_for(persisted.wait(), 3)
        with pytest.raises(ScheduleRunConflictError, match="being dispatched"):
            await peer.run_now(schedule.id)
        await peer.tick()
        assert ledger.get_schedule(schedule.id) == schedule
        assert len(ledger.list_runs(schedule.project_id)) == 1
        release.set()
        await pending
        with pytest.raises(ScheduleRunConflictError, match="already has an active run"):
            await peer.run_now(schedule.id)
        await peer.tick()
        assert len(ledger.list_runs(schedule.project_id)) == 1
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
        await runs.stop()
        await peer_runs.stop()


@pytest.mark.parametrize("policy", list(MissedRunPolicy))
@pytest.mark.parametrize("once", [False, True])
@pytest.mark.asyncio
async def test_manual_overlap_keeps_due_skip_and_one_time_semantics(tmp_path, policy, once):
    ledger, runs, service = build(tmp_path)
    schedule = create_schedule(ledger, due=True)
    ledger.update_schedule(
        schedule.id, missed_run_policy=policy,
        cron=None if once else schedule.cron,
        run_once_at=schedule.next_fire_at if once else None,
    )
    try:
        manual = await service.run_now(schedule.id)
        await service.reconcile()
        current = ledger.get_schedule(schedule.id)
        assert [r.id for r in ledger.list_runs(schedule.project_id)] == [manual.id]
        assert current.last_fire_at is None
        assert current.last_run_id is None
        assert current.next_fire_at is None if once else current.next_fire_at > utc_now()
        assert current.enabled is (not once)
    finally:
        await runs.stop()


@pytest.mark.asyncio
async def test_manual_reads_configuration_after_acquiring_lease(tmp_path):
    ledger, runs, service = build(tmp_path)
    schedule = create_schedule(ledger)
    original = service._dispatch_lease

    @contextmanager
    def edited_before_lease(schedule_id):
        ledger.update_schedule(schedule_id, prompt="The newly reviewed task", enabled=False)
        with original(schedule_id) as acquired:
            yield acquired

    service._dispatch_lease = edited_before_lease
    try:
        manual = await service.run_now(schedule.id)
        assert manual.prompt == "The newly reviewed task"
        assert ledger.get_schedule(schedule.id).enabled is False
        assert ledger.get_schedule(schedule.id).next_fire_at == schedule.next_fire_at
    finally:
        await runs.stop()


@pytest.mark.asyncio
async def test_other_schedules_can_run_and_completed_run_releases_manual_slot(tmp_path):
    ledger, runs, service = build(tmp_path)
    schedule = create_schedule(ledger)
    other = ledger.create_schedule(Schedule(
        project_id=schedule.project_id, prompt="Different task", cron=schedule.cron,
        next_fire_at=schedule.next_fire_at,
    ))
    try:
        first = await service.run_now(schedule.id)
        second = await service.run_now(other.id)
        assert first.id != second.id
        await runs.cancel(first.id)
        third = await service.run_now(schedule.id)
        assert third.id not in {first.id, second.id}
    finally:
        await runs.stop()
