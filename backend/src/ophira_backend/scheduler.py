from __future__ import annotations

import asyncio
import fcntl
import hashlib
import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter

from .auth import AuthorizationError, ProjectAccessStore, ProjectPermission
from .automation import NotificationStore
from .constants import HEARTBEAT_OK, TERMINAL_RUN_STATUSES, EventType, MissedRunPolicy, ScheduleKind
from .data_operations import data_operation
from .events import EventPublisher, redact
from .models import (
    EventActor,
    PrincipalBinding,
    PrincipalContext,
    RunRecord,
    Schedule,
    ScheduleCreate,
    utc_now,
)
from .policy import PolicyDeniedError
from .providers import ProviderConfigurationError, ProviderNotFoundError
from .runs import RunManager
from .storage import FileLedger, LedgerNotFoundError
from .task_experience import schedule_experience_config_hash

logger = logging.getLogger(__name__)


class ScheduleValidationError(ValueError):
    pass


class ScheduleRunConflictError(RuntimeError):
    """The schedule already owns a dispatch or a nonterminal run."""


def compute_next_fire(
    cron: str | None,
    run_once_at: datetime | None,
    timezone: str,
    after: datetime,
) -> datetime | None:
    """Next fire time in UTC, or None when the schedule will never fire again."""
    try:
        tz = ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise ScheduleValidationError(f"unknown timezone: {timezone}") from exc
    if run_once_at is not None:
        moment = run_once_at
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=tz)
        moment = moment.astimezone(UTC)
        return moment if moment > after else None
    try:
        local_next = croniter(cron, after.astimezone(tz)).get_next(datetime)
    except (ValueError, KeyError) as exc:
        raise ScheduleValidationError(f"invalid cron expression: {cron}") from exc
    return local_next.astimezone(UTC)


def new_schedule(
    payload: ScheduleCreate,
    *,
    principal: PrincipalContext | PrincipalBinding | None = None,
) -> Schedule:
    principal_binding = (
        principal
        if isinstance(principal, PrincipalBinding)
        else PrincipalBinding.from_context(principal)
        if principal is not None
        else None
    )
    schedule = Schedule(
        **payload.model_dump(),
        principal_binding=principal_binding,
    )
    next_at = compute_next_fire(
        schedule.cron,
        schedule.run_once_at,
        schedule.timezone,
        utc_now(),
    )
    if next_at is None:
        raise ScheduleValidationError("schedule has no future fire time")
    return schedule.model_copy(update={"next_fire_at": next_at})


def wrap_schedule_prompt(schedule: Schedule) -> str:
    if schedule.kind != ScheduleKind.HEARTBEAT:
        return schedule.prompt
    return (
        f"这是心跳巡检。若相对上次观察没有实质变化，只回复 {HEARTBEAT_OK}，"
        "不要调用发送通知工具。若有变化，用不超过 400 字说明变化要点。\n\n"
        f"{schedule.prompt}"
    )


class ScheduleService:
    """Due scanner with cross-process leases and durable occurrence identity."""

    def __init__(
        self,
        ledger: FileLedger,
        runs: RunManager,
        events: EventPublisher,
        interval_seconds: float = 30,
        notifications: NotificationStore | None = None,
        access: ProjectAccessStore | None = None,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("scheduler interval must be positive")
        self.ledger = ledger
        self.runs = runs
        self.events = events
        self.interval_seconds = interval_seconds
        self.notifications = notifications
        self.access = access
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        await self.reconcile()
        self._task = asyncio.create_task(self._loop(), name="ophira:scheduler")

    async def stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        self._task = None

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.interval_seconds)
            try:
                await self.tick()
            except Exception as exc:
                # A transient ledger failure must not permanently stop the scheduler task.
                logger.error("scheduler tick failed: %s", redact(str(exc)))

    async def reconcile(self) -> None:
        """On startup: overdue enabled schedules catch up once or skip ahead."""
        await self._scan(missed=True)

    async def tick(self) -> None:
        await self._scan(missed=False)

    @contextmanager
    def _dispatch_lease(self, schedule_id: str) -> Iterator[bool]:
        # Non-blocking and held across dispatch, including awaits. A peer worker
        # skips immediately; OS process exit releases the lease automatically.
        directory = self.ledger.schedules_dir / self.ledger._safe_id(schedule_id)
        if directory.is_symlink():
            raise ValueError("schedule directory must not be a symlink")
        flags = os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC
        descriptor = os.open(directory / ".dispatch.lock", flags, 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            yield True
        finally:
            os.close(descriptor)

    @data_operation()
    async def _scan(self, *, missed: bool) -> None:
        now = utc_now()
        for snapshot in self.ledger.list_schedules():
            try:
                with self._dispatch_lease(snapshot.id) as acquired:
                    if not acquired:
                        continue
                    # Never use the pre-lease snapshot to dispatch or advance.
                    schedule = self.ledger.get_schedule(snapshot.id)
                    if schedule.next_fire_at is None or schedule.next_fire_at > now:
                        continue
                    await self._fire(schedule, now, missed=missed)
            except Exception as exc:
                logger.error(
                    "failed to dispatch schedule %s: %s", snapshot.id, redact(str(exc)),
                )

    @staticmethod
    def _occurrence_id(schedule: Schedule) -> str:
        assert schedule.next_fire_at is not None
        moment = schedule.next_fire_at.astimezone(UTC).isoformat()
        digest = hashlib.sha256(f"{schedule.id}:{moment}".encode()).hexdigest()
        return f"schedule_fire_{digest}"

    async def _fire(self, schedule: Schedule, now: datetime, *, missed: bool = False) -> None:
        claim_id = self._occurrence_id(schedule)
        # RunManager persists this claim in run.json BEFORE starting the Agent.
        # A crash between creation, audit and schedule advancement therefore
        # recovers the same run, even if it already finished or failed. It never
        # automatically repeats a possibly effectful Agent run.
        schedule_runs = self._schedule_runs(schedule)
        existing = next((run for run in schedule_runs
                         if run.automation_claim_id == claim_id), None)
        if existing is not None:
            await self._finish_fire(schedule, existing, now, missed=missed)
            return
        if not schedule.enabled:
            return
        if missed and schedule.missed_run_policy == MissedRunPolicy.SKIP:
            self._advance(schedule, now)
            return
        if any(run.status not in TERMINAL_RUN_STATUSES for run in schedule_runs):
            self._advance(schedule, now)
            return
        run = await self._start_run(schedule, automation_claim_id=claim_id)
        await self._finish_fire(schedule, run, now, missed=missed)

    async def _finish_fire(
        self, schedule: Schedule, run: RunRecord, now: datetime, *, missed: bool,
    ) -> None:
        if not any(event.type == EventType.SCHEDULE_FIRED.value
                   for event in self.ledger.list_events(run.id)):
            await self.events.publish(
                run.id,
                EventType.SCHEDULE_FIRED,
                {"schedule_id": schedule.id, "missed": missed,
                 "scheduled_for": schedule.next_fire_at,
                 "automation_claim_id": run.automation_claim_id},
                actor=EventActor(type="system", name="scheduler"),
            )
        next_at = (None if schedule.run_once_at is not None else
                   compute_next_fire(schedule.cron, None, schedule.timezone, now))
        assert schedule.next_fire_at is not None
        self.ledger.advance_schedule(
            schedule.id, expected_fire_at=schedule.next_fire_at, next_fire_at=next_at,
            last_fire_at=run.created_at, last_run_id=run.id,
        )

    async def _start_run(
        self, schedule: Schedule, *, automation_claim_id: str | None = None,
    ) -> RunRecord:
        try:
            binding = schedule.principal_binding
            project = self.ledger.get_project(schedule.project_id)
            if project.identity_binding == "authenticated_principal":
                if binding is None or self.access is None:
                    raise AuthorizationError(
                        "authenticated schedule has no durable principal authority"
                    )
                self.access.authorize_bound(
                    schedule.project_id,
                    tenant_id=binding.tenant_id,
                    principal_id=binding.principal_id,
                    role=binding.role,
                    permission=ProjectPermission.RUN_CREATE,
                )
            elif binding is not None:
                raise AuthorizationError(
                    "local-only schedules cannot claim authenticated authority"
                )
            return await self.runs.create_and_start(
                schedule.project_id,
                wrap_schedule_prompt(schedule),
                agent_id=schedule.agent_id,
                schedule_id=schedule.id,
                task_experience_config_hash=schedule_experience_config_hash(schedule),
                automation_claim_id=automation_claim_id,
                mode=schedule.mode,
                provider_profile_id=schedule.provider_profile_id,
                principal=binding,
            )
        except (
            AuthorizationError,
            PolicyDeniedError,
            ProviderConfigurationError,
            ProviderNotFoundError,
        ) as exc:
            self._pause_unstartable(schedule, exc)
            raise

    def _pause_unstartable(self, schedule: Schedule, error: Exception) -> None:
        """Pause a schedule whose configured provider/policy cannot start a run."""
        try:
            self.ledger.update_schedule(schedule.id, enabled=False)
        except (LedgerNotFoundError, ValueError):
            return
        if self.notifications is None:
            return
        safe_error = str(redact(str(error)))[:1_000]
        try:
            self.notifications.add(
                title="定时任务已暂停：Provider 或策略未就绪",
                body=(
                    f"本次运行未启动：{safe_error}。请修复凭据、完成成功预检或调整组织策略后"
                    "再恢复此任务。"
                ),
                severity="error",
                project_id=schedule.project_id,
                schedule_id=schedule.id,
            )
        except Exception as exc:
            logger.error(
                "failed to notify paused schedule %s: %s",
                schedule.id,
                redact(str(exc)),
            )

    def _advance(self, schedule: Schedule, now: datetime) -> None:
        next_at = compute_next_fire(
            schedule.cron, schedule.run_once_at, schedule.timezone, now
        )
        assert schedule.next_fire_at is not None
        self.ledger.advance_schedule(
            schedule.id, expected_fire_at=schedule.next_fire_at, next_fire_at=next_at,
        )

    async def run_now(self, schedule_id: str) -> RunRecord:
        """Manual test run; does not change the schedule's fire bookkeeping."""
        self.ledger.get_schedule(schedule_id)
        with self._dispatch_lease(schedule_id) as acquired:
            if not acquired:
                raise ScheduleRunConflictError(
                    "This schedule is already being dispatched. Refresh before running again."
                )
            schedule = self.ledger.get_schedule(schedule_id)
            if any(run.status not in TERMINAL_RUN_STATUSES
                   for run in self._schedule_runs(schedule)):
                raise ScheduleRunConflictError(
                    "This schedule already has an active run. Wait for it to finish or cancel it "
                    "in the workspace before running again."
                )
            # Keep the lease until create_and_start has persisted the queued Run.
            # Peers then observe it even before the runtime executes or when this
            # process dies before returning the HTTP response. Waiting approvals
            # and cancellation in progress remain active until truly terminal.
            return await self._start_run(schedule)

    def _schedule_runs(self, schedule: Schedule) -> list[RunRecord]:
        # last_run_id describes scheduled fire bookkeeping; manual runs deliberately
        # leave it untouched. The durable Run bindings cover both entry points.
        return [run for run in self.ledger.list_runs(
            schedule.project_id, sort_by_created_at=False,
        ) if run.schedule_id == schedule.id]
