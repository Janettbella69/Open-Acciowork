"""Dispatch recovery never turns an observation failure into another Agent Run."""

from datetime import timedelta

import pytest

from ophira_backend.constants import EventType, MissedRunPolicy, RunStatus
from ophira_backend.events import EventPublisher
from ophira_backend.models import ProjectCreate, Schedule, utc_now
from ophira_backend.policy import PolicyDeniedError
from ophira_backend.project_events import ProjectEventProjector
from ophira_backend.scheduler import ScheduleService, compute_next_fire
from ophira_backend.storage import FileLedger


class ImmediateRuns:
    """Commit immediately terminal Runs, the dangerous manual-retry boundary."""

    def __init__(self, ledger):
        self.ledger = ledger
        self.calls = 0
        self.denied = False

    async def create_and_start(self, project_id, prompt, **options):
        self.calls += 1
        if self.denied:
            raise PolicyDeniedError("Synthetic policy denied dispatch")
        run = self.ledger.create_run(
            project_id, prompt, schedule_id=options["schedule_id"],
            automation_claim_id=options.get("automation_claim_id"),
        )
        return self.ledger.update_run(run.id, status=RunStatus.COMPLETED)


@pytest.fixture
def dispatch(tmp_path):
    ledger = FileLedger(tmp_path)
    events = EventPublisher(ledger)
    runs = ImmediateRuns(ledger)
    service = ScheduleService(ledger, runs, events)
    project = ledger.create_project(ProjectCreate(name="Dispatch", objective="Recover decisions"))
    schedule = ledger.create_schedule(Schedule(
        project_id=project.id, prompt="Synthetic scheduled task", cron="*/5 * * * *",
        next_fire_at=utc_now() - timedelta(seconds=10),
        missed_run_policy=MissedRunPolicy.CATCH_UP,
    ))
    return ledger, runs, service, schedule


def pending_path(ledger, schedule):
    return ledger.schedules_dir / schedule.id / "pending-dispatch-failure.json"


def failed_events(service, schedule):
    stream = service.scoped.project_stream(schedule.project_id)
    return [event for event in service.scoped.read(stream)
            if event.type == EventType.SCHEDULE_FAILED]


@pytest.mark.asyncio
async def test_manual_committed_run_survives_observation_failure_and_is_repaired(
    dispatch, monkeypatch,
):
    ledger, runs, service, schedule = dispatch

    def fail_observation(_run):
        raise OSError("Synthetic event store failure")

    monkeypatch.setattr(service.projector, "observe_schedule_run", fail_observation)
    run = await service.run_now(schedule.id)
    assert run.status == RunStatus.COMPLETED
    assert runs.calls == 1
    assert [item.id for item in ledger.list_runs(schedule.project_id)] == [run.id]
    repaired = ProjectEventProjector(ledger, EventPublisher(ledger))
    repaired.reconcile_project(schedule.project_id)
    events = service.scoped.read(service.scoped.project_stream(schedule.project_id))
    assert [event.type for event in events if event.run_id == run.id] == [
        EventType.SCHEDULE_FIRED, EventType.SCHEDULE_COMPLETED,
    ]
    assert runs.calls == 1


@pytest.mark.asyncio
async def test_failure_event_outage_still_pauses_and_replays_once(dispatch, monkeypatch):
    ledger, runs, service, schedule = dispatch
    runs.denied = True

    def fail_event(*_args, **_kwargs):
        raise OSError("Synthetic event outage")

    with monkeypatch.context() as patch:
        patch.setattr(service.scoped, "append", fail_event)
        with pytest.raises(PolicyDeniedError):
            await service.run_now(schedule.id)
    assert ledger.get_schedule(schedule.id).enabled is False
    assert pending_path(ledger, schedule).exists()
    runs.denied = False
    restarted = ScheduleService(ledger, runs, EventPublisher(ledger))
    await restarted.tick()
    await restarted.tick()
    assert runs.calls == 1
    assert ledger.list_runs(schedule.project_id) == []
    assert len(failed_events(restarted, schedule)) == 1
    assert not pending_path(ledger, schedule).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_boundary", ["before_pause", "after_pause"])
async def test_crash_between_failure_decision_and_pause_never_retries_automatically(
    dispatch, monkeypatch, failure_boundary,
):
    ledger, runs, service, schedule = dispatch
    runs.denied = True
    atomic_write = ledger._atomic_write

    def interrupt_pause(path, record):
        if failure_boundary == "before_pause" and path.name == "schedule.json":
            raise OSError("Synthetic crash before pause")
        if (failure_boundary == "after_pause" and path.name == "pending-dispatch-failure.json"
                and record.phase == "paused"):
            raise OSError("Synthetic crash before pause acknowledgement")
        return atomic_write(path, record)

    with monkeypatch.context() as patch:
        patch.setattr(ledger, "_atomic_write", interrupt_pause)
        await service.tick()
    assert pending_path(ledger, schedule).exists()
    runs.denied = False
    restarted = ScheduleService(ledger, runs, EventPublisher(ledger))
    await restarted.reconcile()
    await restarted.tick()
    assert runs.calls == 1
    assert ledger.get_schedule(schedule.id).enabled is False
    assert ledger.list_runs(schedule.project_id) == []
    assert len(failed_events(restarted, schedule)) == 1


@pytest.mark.asyncio
async def test_resume_changes_occurrence_and_pending_failure_does_not_override_it(
    dispatch, monkeypatch,
):
    ledger, runs, service, schedule = dispatch
    runs.denied = True
    atomic_write = ledger._atomic_write

    def interrupt_before_pause(path, record):
        if path.name == "schedule.json":
            raise OSError("Synthetic crash before pause")
        return atomic_write(path, record)

    with monkeypatch.context() as patch:
        patch.setattr(ledger, "_atomic_write", interrupt_before_pause)
        await service.tick()
    # This is the existing /resume contract: explicitly calculate a new fire time.
    next_at = compute_next_fire(schedule.cron, None, schedule.timezone, utc_now())
    ledger.update_schedule(schedule.id, enabled=True, next_fire_at=next_at)
    runs.denied = False
    restarted = ScheduleService(ledger, runs, EventPublisher(ledger))
    await restarted.tick()
    resumed = ledger.get_schedule(schedule.id)
    assert resumed.enabled is True
    assert resumed.next_fire_at == next_at
    assert len(failed_events(restarted, schedule)) == 1
    assert failed_events(restarted, schedule)[0].data["scheduled_for"] == (
        schedule.next_fire_at.isoformat()
    )
    await restarted._fire(resumed, next_at)
    assert runs.calls == 2
    assert len(ledger.list_runs(schedule.project_id)) == 1


@pytest.mark.asyncio
async def test_manual_explicit_retry_after_recovery_is_allowed(dispatch, monkeypatch):
    ledger, runs, service, schedule = dispatch
    runs.denied = True

    def fail_event(*_args, **_kwargs):
        raise OSError("Synthetic event outage")

    with monkeypatch.context() as patch:
        patch.setattr(service.scoped, "append", fail_event)
        with pytest.raises(PolicyDeniedError):
            await service.run_now(schedule.id)
    runs.denied = False
    restarted = ScheduleService(ledger, runs, EventPublisher(ledger))
    run = await restarted.run_now(schedule.id)
    assert run.status == RunStatus.COMPLETED
    assert runs.calls == 2
    assert len(failed_events(restarted, schedule)) == 1
    assert not pending_path(ledger, schedule).exists()


@pytest.mark.asyncio
async def test_failure_receipt_cleanup_crash_does_not_duplicate_event(dispatch, monkeypatch):
    ledger, runs, service, schedule = dispatch
    runs.denied = True
    path_type = type(pending_path(ledger, schedule))
    unlink = path_type.unlink

    def interrupt_cleanup(path, *args, **kwargs):
        if path.name == "pending-dispatch-failure.json":
            raise OSError("Synthetic crash after failure observation")
        return unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(path_type, "unlink", interrupt_cleanup)
        await service.tick()
    assert len(failed_events(service, schedule)) == 1
    assert pending_path(ledger, schedule).exists()
    runs.denied = False
    restarted = ScheduleService(ledger, runs, EventPublisher(ledger))
    await restarted.tick()
    assert len(failed_events(restarted, schedule)) == 1
    assert runs.calls == 1
    assert not pending_path(ledger, schedule).exists()


@pytest.mark.asyncio
async def test_unverifiable_failure_receipt_blocks_future_dispatch(dispatch):
    ledger, runs, service, schedule = dispatch
    pending_path(ledger, schedule).write_text('{"schema_version":1}')
    await service.tick()
    assert runs.calls == 0
    assert ledger.list_runs(schedule.project_id) == []
