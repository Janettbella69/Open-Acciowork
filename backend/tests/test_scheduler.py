import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from ophira_backend.constants import EventType, MissedRunPolicy, RunStatus
from ophira_backend.events import EventPublisher
from ophira_backend.models import ProjectCreate, Schedule, ScheduleCreate, utc_now
from ophira_backend.permissions import PermissionBroker
from ophira_backend.runs import RunManager
from ophira_backend.scheduler import (
    ScheduleService,
    ScheduleValidationError,
    compute_next_fire,
    new_schedule,
)
from ophira_backend.storage import FileLedger


class FakeRuntime:
    def __init__(self) -> None:
        self.runs: list[str] = []

    async def run(self, run) -> None:
        self.runs.append(run.id)

    async def cancel(self, run_id: str) -> None:
        return None


async def wait_for_runs(runtime: FakeRuntime, count: int = 1) -> None:
    for _ in range(200):
        if len(runtime.runs) >= count:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"expected {count} runs, got {len(runtime.runs)}")


def build_service(tmp_path: Path):
    ledger = FileLedger(tmp_path)
    events = EventPublisher(ledger)
    permissions = PermissionBroker(ledger, events, timeout_seconds=30)
    runtime = FakeRuntime()
    runs = RunManager(ledger, events, permissions, runtime)
    service = ScheduleService(ledger, runs, events)
    project = ledger.create_project(ProjectCreate(name="Kettle", objective="Validate"))
    return ledger, runtime, service, project


def make_schedule(
    project_id: str,
    *,
    cron: str | None = "*/5 * * * *",
    run_once_at: datetime | None = None,
    policy: MissedRunPolicy = MissedRunPolicy.SKIP,
    next_fire_at: datetime | None = None,
) -> Schedule:
    return Schedule(
        project_id=project_id,
        prompt="Daily check",
        cron=cron,
        run_once_at=run_once_at,
        missed_run_policy=policy,
        next_fire_at=next_fire_at,
    )


def test_compute_next_fire_cron() -> None:
    after = datetime(2026, 8, 5, 8, 0, tzinfo=UTC)
    next_at = compute_next_fire("*/5 * * * *", None, "UTC", after)

    assert next_at > after
    assert next_at <= after + timedelta(minutes=5)
    assert next_at.minute % 5 == 0


def test_compute_next_fire_run_once_and_validation() -> None:
    after = datetime(2026, 8, 5, 8, 0, tzinfo=UTC)
    future = after + timedelta(hours=1)
    assert compute_next_fire(None, future, "UTC", after) == future
    assert compute_next_fire(None, after - timedelta(hours=1), "UTC", after) is None

    with pytest.raises(ScheduleValidationError):
        compute_next_fire("not-a-cron", None, "UTC", after)
    with pytest.raises(ScheduleValidationError):
        compute_next_fire("*/5 * * * *", None, "Mars/Olympus", after)


def test_schedule_create_requires_exactly_one_trigger() -> None:
    with pytest.raises(ValidationError):
        ScheduleCreate(project_id="p", prompt="x")
    with pytest.raises(ValidationError):
        ScheduleCreate(
            project_id="p",
            prompt="x",
            cron="*/5 * * * *",
            run_once_at=datetime(2026, 8, 6, tzinfo=UTC),
        )


def test_new_schedule_computes_first_fire() -> None:
    schedule = new_schedule(
        ScheduleCreate(project_id="p", prompt="x", cron="*/5 * * * *")
    )
    assert schedule.next_fire_at is not None
    assert schedule.next_fire_at > utc_now()


@pytest.mark.asyncio
async def test_tick_fires_due_schedule(tmp_path: Path) -> None:
    ledger, runtime, service, project = build_service(tmp_path)
    overdue = make_schedule(
        project.id,
        next_fire_at=utc_now() - timedelta(seconds=1),
    )
    schedule = ledger.create_schedule(overdue)

    await service.tick()
    await wait_for_runs(runtime)

    assert len(runtime.runs) == 1
    updated = ledger.get_schedule(schedule.id)
    assert updated.last_run_id == runtime.runs[0]
    assert updated.next_fire_at > utc_now()
    events = ledger.list_events(runtime.runs[0])
    fired = [event for event in events if event.type == EventType.SCHEDULE_FIRED.value]
    assert fired and fired[0].data["missed"] is False


@pytest.mark.asyncio
async def test_tick_skips_when_previous_run_active(tmp_path: Path) -> None:
    ledger, runtime, service, project = build_service(tmp_path)
    active_run = ledger.create_run(project.id, "still running")
    overdue = make_schedule(
        project.id,
        next_fire_at=utc_now() - timedelta(seconds=1),
    )
    schedule = ledger.create_schedule(overdue)
    ledger.update_schedule(schedule.id, last_run_id=active_run.id)

    await service.tick()

    assert runtime.runs == []
    assert ledger.get_run(active_run.id).status == RunStatus.QUEUED
    assert ledger.get_schedule(schedule.id).next_fire_at > utc_now()


@pytest.mark.asyncio
async def test_reconcile_catch_up_fires_once_and_skip_does_not(tmp_path: Path) -> None:
    ledger, runtime, service, project = build_service(tmp_path)
    overdue = utc_now() - timedelta(hours=3)
    catch_up = ledger.create_schedule(
        make_schedule(
            project.id,
            policy=MissedRunPolicy.CATCH_UP,
            next_fire_at=overdue,
        )
    )
    skip = ledger.create_schedule(
        make_schedule(project.id, policy=MissedRunPolicy.SKIP, next_fire_at=overdue)
    )

    await service.reconcile()
    await wait_for_runs(runtime)

    assert len(runtime.runs) == 1
    events = ledger.list_events(runtime.runs[0])
    fired = [event for event in events if event.type == EventType.SCHEDULE_FIRED.value]
    assert fired[0].data["schedule_id"] == catch_up.id
    assert fired[0].data["missed"] is True
    assert ledger.get_schedule(skip.id).next_fire_at > utc_now()


@pytest.mark.asyncio
async def test_run_once_disables_after_fire(tmp_path: Path) -> None:
    ledger, runtime, service, project = build_service(tmp_path)
    once = ledger.create_schedule(
        make_schedule(
            project.id,
            cron=None,
            run_once_at=utc_now() + timedelta(seconds=3600),
            next_fire_at=utc_now() - timedelta(seconds=1),
        )
    )

    await service.tick()
    await wait_for_runs(runtime)

    updated = ledger.get_schedule(once.id)
    assert len(runtime.runs) == 1
    assert updated.enabled is False
    assert updated.next_fire_at is None


@pytest.mark.asyncio
async def test_paused_schedule_does_not_fire(tmp_path: Path) -> None:
    ledger, runtime, service, project = build_service(tmp_path)
    paused = ledger.create_schedule(
        make_schedule(
            project.id,
            next_fire_at=utc_now() - timedelta(seconds=1),
        )
    )
    ledger.update_schedule(paused.id, enabled=False)

    await service.tick()

    assert runtime.runs == []
