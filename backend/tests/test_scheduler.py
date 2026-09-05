import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from ophira_backend.automation import NotificationStore
from ophira_backend.constants import EventType, MissedRunPolicy, RunStatus
from ophira_backend.events import EventPublisher
from ophira_backend.models import (
    ProjectCreate,
    Schedule,
    ScheduleCreate,
    SchedulePermissionRule,
    ScheduleUpdate,
    utc_now,
)
from ophira_backend.permissions import PermissionBroker
from ophira_backend.policy import PolicyDeniedError
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


class ReadyProviders:
    def __init__(self, *, default_id: str = "approved") -> None:
        self.default_id = default_id

    def default_profile_id(self) -> str:
        return self.default_id

    def get(self, profile_id: str):
        return SimpleNamespace(
            id=profile_id,
            enabled=True,
            production_eligible=True,
            secret_configured=True,
            last_verified_at=utc_now(),
            last_error_code=None,
        )

    def snapshot(self, profile_id: str) -> dict[str, object]:
        return {"id": profile_id, "production_eligible": True}


class AllowOnlyPolicy:
    def __init__(self, allowed: set[str]) -> None:
        self.allowed = allowed
        self.seen: list[str | None] = []

    def allows_provider(self, provider_id: str | None) -> bool:
        self.seen.append(provider_id)
        return provider_id in self.allowed


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
    notifications = NotificationStore(tmp_path / "notifications")
    service = ScheduleService(ledger, runs, events, notifications=notifications)
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


@pytest.mark.parametrize(
    ("tool_name", "scope"),
    [
        ("Write*", "path:reports"),
        ("Write", "path:reports/../private"),
        ("Write", "path:/"),
        ("Write", "path:/tmp/reports"),
        ("Write", "path: reports"),
        ("Write", "url:https://EXAMPLE.com"),
        ("Write", "url:https://example.com/path"),
        ("Bash", "path:."),
        ("Delete", "path:reports"),
    ],
)
def test_schedule_permission_rule_rejects_unsafe_or_noncanonical_values(
    tool_name: str,
    scope: str,
) -> None:
    with pytest.raises(ValidationError):
        SchedulePermissionRule(tool_name=tool_name, scope=scope)


def test_schedule_create_defaults_to_empty_permission_ceiling() -> None:
    empty = ScheduleCreate(project_id="p", prompt="x", cron="*/5 * * * *")
    bounded = ScheduleCreate(
        project_id="p",
        prompt="x",
        cron="*/5 * * * *",
        permission_ceiling=[{"tool_name": "Write", "scope": "path:."}],
    )

    assert empty.permission_ceiling == []
    assert ScheduleUpdate(prompt="changed").permission_ceiling is None
    assert bounded.permission_ceiling == [
        SchedulePermissionRule(tool_name="Write", scope="path:.")
    ]


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


@pytest.mark.asyncio
async def test_two_workers_cannot_dispatch_the_same_occurrence(tmp_path: Path) -> None:
    ledger, runtime, first, project = build_service(tmp_path)
    _, _, second, _ = build_service(tmp_path)
    ledger.create_schedule(
        make_schedule(project.id, next_fire_at=utc_now() - timedelta(seconds=1))
    )
    started = asyncio.Event()
    release = asyncio.Event()
    original = first._start_run

    async def delayed_start(*args, **kwargs):
        run = await original(*args, **kwargs)
        started.set()
        await release.wait()
        return run

    first._start_run = delayed_start
    task = asyncio.create_task(first.tick())
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        await asyncio.wait_for(second.tick(), timeout=2)
    finally:
        release.set()
        await task

    await wait_for_runs(runtime)
    assert len(ledger.list_runs(project.id)) == 1


@pytest.mark.parametrize("after_publish", [False, True])
@pytest.mark.parametrize("policy", list(MissedRunPolicy))
@pytest.mark.asyncio
async def test_restart_finishes_dispatch_without_repeating_a_persisted_run(
    tmp_path: Path, after_publish: bool, policy: MissedRunPolicy,
) -> None:
    ledger, runtime, first, project = build_service(tmp_path)
    schedule = ledger.create_schedule(make_schedule(
        project.id, policy=policy, next_fire_at=utc_now() - timedelta(seconds=1),
    ))
    publish = first.events.publish

    async def interrupted_publish(run_id, event_type, *args, **kwargs):
        if event_type == EventType.SCHEDULE_FIRED:
            if after_publish:
                await publish(run_id, event_type, *args, **kwargs)
            raise OSError("simulated crash before schedule bookkeeping")
        return await publish(run_id, event_type, *args, **kwargs)

    first.events.publish = interrupted_publish
    await first.tick()
    await wait_for_runs(runtime)
    initial_runs = ledger.list_runs(project.id)
    assert len(initial_runs) == 1
    # A new service with a new publisher represents process restart. Neither
    # CATCH_UP nor SKIP may launch a second Agent for an existing occurrence.
    _, _, restarted, _ = build_service(tmp_path)
    await restarted.reconcile()
    await restarted.tick()

    assert len(ledger.list_runs(project.id)) == 1
    current = ledger.get_schedule(schedule.id)
    assert current.last_run_id == initial_runs[0].id
    assert current.next_fire_at > utc_now()
    fired = [e for e in ledger.list_events(initial_runs[0].id)
             if e.type == EventType.SCHEDULE_FIRED.value]
    assert len(fired) == 1


@pytest.mark.asyncio
async def test_dispatch_bookkeeping_does_not_overwrite_a_concurrent_schedule_edit(
    tmp_path: Path,
) -> None:
    ledger, runtime, service, project = build_service(tmp_path)
    schedule = ledger.create_schedule(make_schedule(
        project.id, next_fire_at=utc_now() - timedelta(seconds=1),
    ))
    changed_next = utc_now() + timedelta(days=2)
    original = service._start_run

    async def edit_during_start(*args, **kwargs):
        run = await original(*args, **kwargs)
        FileLedger(tmp_path).update_schedule(
            schedule.id, cron="0 12 * * *", next_fire_at=changed_next, enabled=False,
        )
        return run

    service._start_run = edit_during_start
    await service.tick()
    await wait_for_runs(runtime)

    current = ledger.get_schedule(schedule.id)
    assert current.next_fire_at == changed_next
    assert current.enabled is False
    assert current.last_run_id == runtime.runs[0]


@pytest.mark.asyncio
async def test_tick_continues_after_one_due_schedule_is_denied(tmp_path: Path) -> None:
    ledger = FileLedger(tmp_path)
    events = EventPublisher(ledger)
    permissions = PermissionBroker(ledger, events, timeout_seconds=30)
    runtime = FakeRuntime()
    policy = AllowOnlyPolicy({"approved"})
    runs = RunManager(
        ledger,
        events,
        permissions,
        runtime,
        providers=ReadyProviders(),
        policy=policy,
    )
    notifications = NotificationStore(tmp_path / "notifications")
    service = ScheduleService(ledger, runs, events, notifications=notifications)
    project = ledger.create_project(ProjectCreate(name="Kettle", objective="Validate"))
    due = utc_now() - timedelta(seconds=1)
    good = ledger.create_schedule(make_schedule(project.id, next_fire_at=due))
    bad = ledger.create_schedule(
        make_schedule(
            project.id,
            cron=None,
            run_once_at=due,
            next_fire_at=due,
        ).model_copy(
            update={"provider_profile_id": "blocked"}
        )
    )

    await service.tick()
    await wait_for_runs(runtime)

    paused = ledger.get_schedule(bad.id)
    assert paused.last_run_id is None
    assert paused.enabled is False
    assert paused.next_fire_at == due
    good_run_id = ledger.get_schedule(good.id).last_run_id
    assert good_run_id is not None
    good_run = ledger.get_run(good_run_id)
    assert good_run.provider_profile_id == "approved"
    assert good_run.provider_snapshot == {"id": "approved", "production_eligible": True}
    assert policy.seen[:2] == ["blocked", "approved"]
    notices = notifications.list()
    assert len(notices) == 1
    assert notices[0].schedule_id == bad.id
    assert "已暂停" in notices[0].title


@pytest.mark.parametrize(
    "profile_overrides",
    [
        {"enabled": False},
        {"production_eligible": False},
        {"secret_configured": False},
        {"last_verified_at": None},
        {"last_error_code": "AUTH_REVOKED_OR_INVALID"},
    ],
)
@pytest.mark.parametrize("provider_profile_id", [None, "approved"])
@pytest.mark.asyncio
async def test_tick_pauses_and_notifies_when_provider_is_not_ready(
    tmp_path: Path,
    profile_overrides: dict[str, object],
    provider_profile_id: str | None,
) -> None:
    class UnreadyProviders(ReadyProviders):
        def get(self, profile_id: str):
            profile = super().get(profile_id)
            for key, value in profile_overrides.items():
                setattr(profile, key, value)
            return profile

    ledger = FileLedger(tmp_path)
    events = EventPublisher(ledger)
    runtime = FakeRuntime()
    notifications = NotificationStore(tmp_path / "notifications")
    runs = RunManager(
        ledger,
        events,
        PermissionBroker(ledger, events, timeout_seconds=30),
        runtime,
        providers=UnreadyProviders(),
        policy=AllowOnlyPolicy({"approved"}),
    )
    service = ScheduleService(ledger, runs, events, notifications=notifications)
    project = ledger.create_project(ProjectCreate(name="Kettle", objective="Validate"))
    due = utc_now() - timedelta(seconds=1)
    schedule = ledger.create_schedule(
        make_schedule(project.id, next_fire_at=due).model_copy(
            update={"provider_profile_id": provider_profile_id}
        )
    )

    await service.tick()

    paused = ledger.get_schedule(schedule.id)
    assert paused.enabled is False
    assert paused.last_run_id is None
    assert paused.next_fire_at == due
    assert runtime.runs == []
    notices = notifications.list()
    assert len(notices) == 1
    assert notices[0].schedule_id == schedule.id
    assert notices[0].severity == "error"


@pytest.mark.asyncio
async def test_scheduler_loop_survives_a_tick_exception(tmp_path: Path) -> None:
    _ledger, _runtime, service, _project = build_service(tmp_path)
    service.interval_seconds = 0.001
    recovered = asyncio.Event()
    calls = 0

    async def flaky_tick() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("transient ledger failure")
        recovered.set()

    service.tick = flaky_tick  # type: ignore[method-assign]
    service._task = asyncio.create_task(service._loop())
    try:
        await asyncio.wait_for(recovered.wait(), timeout=1)
    finally:
        await service.stop()

    assert calls >= 2


@pytest.mark.parametrize(
    "profile_overrides",
    [
        {"enabled": False},
        {"production_eligible": False},
        {"secret_configured": False},
        {"last_verified_at": None},
        {"last_error_code": "AUTH_REVOKED_OR_INVALID"},
    ],
)
@pytest.mark.parametrize("provider_profile_id", [None, "approved"])
@pytest.mark.asyncio
async def test_run_manager_rejects_provider_that_is_not_production_ready(
    tmp_path: Path,
    profile_overrides: dict[str, object],
    provider_profile_id: str | None,
) -> None:
    class UnreadyProviders(ReadyProviders):
        def get(self, profile_id: str):
            profile = super().get(profile_id)
            for key, value in profile_overrides.items():
                setattr(profile, key, value)
            return profile

    ledger = FileLedger(tmp_path)
    events = EventPublisher(ledger)
    permissions = PermissionBroker(ledger, events, timeout_seconds=30)
    runs = RunManager(
        ledger,
        events,
        permissions,
        FakeRuntime(),
        providers=UnreadyProviders(),
        policy=AllowOnlyPolicy({"approved"}),
    )
    project = ledger.create_project(ProjectCreate(name="Kettle", objective="Validate"))

    with pytest.raises(PolicyDeniedError, match="not ready for runs"):
        await runs.create_and_start(
            project.id,
            "Start",
            provider_profile_id=provider_profile_id,
            provider_snapshot={"forged": "snapshot cannot bypass live checks"},
        )

    assert ledger.list_runs(project.id) == []
