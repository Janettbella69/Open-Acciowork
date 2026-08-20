from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter

from .constants import TERMINAL_RUN_STATUSES, EventType, MissedRunPolicy
from .events import EventPublisher
from .models import EventActor, RunRecord, Schedule, ScheduleCreate, utc_now
from .runs import RunManager
from .storage import FileLedger, LedgerNotFoundError


class ScheduleValidationError(ValueError):
    pass


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


def new_schedule(payload: ScheduleCreate) -> Schedule:
    schedule = Schedule(**payload.model_dump())
    next_at = compute_next_fire(
        schedule.cron,
        schedule.run_once_at,
        schedule.timezone,
        utc_now(),
    )
    if next_at is None:
        raise ScheduleValidationError("schedule has no future fire time")
    return schedule.model_copy(update={"next_fire_at": next_at})


class ScheduleService:
    """Local asyncio scheduler: due-scan loop plus missed-run reconciliation."""

    def __init__(
        self,
        ledger: FileLedger,
        runs: RunManager,
        events: EventPublisher,
        interval_seconds: float = 30,
    ) -> None:
        self.ledger = ledger
        self.runs = runs
        self.events = events
        self.interval_seconds = interval_seconds
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
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
            await self.tick()

    async def reconcile(self) -> None:
        """On startup: overdue enabled schedules catch up once or skip ahead."""
        now = utc_now()
        for schedule in self.ledger.list_schedules():
            if not schedule.enabled or schedule.next_fire_at is None:
                continue
            if schedule.next_fire_at > now:
                continue
            if schedule.missed_run_policy == MissedRunPolicy.CATCH_UP:
                await self._fire(schedule, now, missed=True)
            else:
                self._advance(schedule, now)

    async def tick(self) -> None:
        now = utc_now()
        for schedule in self.ledger.list_schedules():
            if not schedule.enabled or schedule.next_fire_at is None:
                continue
            if schedule.next_fire_at <= now:
                await self._fire(schedule, now)

    async def _fire(self, schedule: Schedule, now: datetime, *, missed: bool = False) -> None:
        if schedule.last_run_id:
            try:
                last_run = self.ledger.get_run(schedule.last_run_id)
            except LedgerNotFoundError:
                last_run = None
            if last_run is not None and last_run.status not in TERMINAL_RUN_STATUSES:
                self._advance(schedule, now)
                return
        run = await self.runs.create_and_start(
            schedule.project_id,
            schedule.prompt,
            agent_id=schedule.agent_id,
            schedule_id=schedule.id,
            mode=schedule.mode,
            provider_profile_id=schedule.provider_profile_id,
        )
        await self.events.publish(
            run.id,
            EventType.SCHEDULE_FIRED,
            {"schedule_id": schedule.id, "missed": missed},
            actor=EventActor(type="system", name="scheduler"),
        )
        changes: dict = {"last_fire_at": now, "last_run_id": run.id}
        if schedule.run_once_at is not None:
            changes["enabled"] = False
            changes["next_fire_at"] = None
        else:
            changes["next_fire_at"] = compute_next_fire(
                schedule.cron, None, schedule.timezone, now
            )
        self.ledger.update_schedule(schedule.id, **changes)

    def _advance(self, schedule: Schedule, now: datetime) -> None:
        next_at = compute_next_fire(
            schedule.cron, schedule.run_once_at, schedule.timezone, now
        )
        changes: dict = {"next_fire_at": next_at}
        if next_at is None:
            changes["enabled"] = False
        self.ledger.update_schedule(schedule.id, **changes)

    async def run_now(self, schedule_id: str) -> RunRecord:
        """Manual test run; does not change the schedule's fire bookkeeping."""
        schedule = self.ledger.get_schedule(schedule_id)
        return await self.runs.create_and_start(
            schedule.project_id,
            schedule.prompt,
            agent_id=schedule.agent_id,
            schedule_id=schedule.id,
            mode=schedule.mode,
            provider_profile_id=schedule.provider_profile_id,
        )
