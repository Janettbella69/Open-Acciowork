from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Protocol

from .constants import TERMINAL_RUN_STATUSES, EventType, RunStatus
from .events import EventPublisher, redact
from .models import EventActor, RunRecord, utc_now
from .permissions import PermissionBroker
from .storage import FileLedger


class AgentRuntime(Protocol):
    async def run(self, run: RunRecord) -> None: ...

    async def cancel(self, run_id: str) -> None: ...


class RunManager:
    def __init__(
        self,
        ledger: FileLedger,
        events: EventPublisher,
        permissions: PermissionBroker,
        runtime: AgentRuntime,
    ) -> None:
        self.ledger = ledger
        self.events = events
        self.permissions = permissions
        self.runtime = runtime
        self._tasks: dict[str, asyncio.Task[None]] = {}

    async def create_and_start(
        self,
        project_id: str,
        prompt: str,
        *,
        agent_id: str | None = None,
        schedule_id: str | None = None,
        resume_session_id: str | None = None,
    ) -> RunRecord:
        run = self.ledger.create_run(
            project_id,
            prompt,
            agent_id=agent_id,
            schedule_id=schedule_id,
            resume_session_id=resume_session_id,
        )
        await self.events.publish(
            run.id,
            EventType.RUN_CREATED,
            {"status": run.status, "prompt": run.prompt},
            actor=EventActor(type="system", name="run-manager"),
        )
        task = asyncio.create_task(self._execute(run.id), name=f"ophira:{run.id}")
        self._tasks[run.id] = task
        task.add_done_callback(lambda _task: self._tasks.pop(run.id, None))
        return run

    async def _execute(self, run_id: str) -> None:
        run = self.ledger.update_run(
            run_id,
            status=RunStatus.RUNNING,
            started_at=utc_now(),
        )
        await self.events.publish(
            run_id,
            EventType.RUN_STARTED,
            {"status": RunStatus.RUNNING},
            actor=EventActor(type="system", name="run-manager"),
        )
        try:
            await self.runtime.run(run)
        except asyncio.CancelledError:
            await self._finalize_cancel(run_id)
            return
        except Exception as exc:
            safe_error = str(redact(str(exc)))
            self.ledger.update_run(
                run_id,
                status=RunStatus.FAILED,
                error=safe_error,
                finished_at=utc_now(),
            )
            await self.events.publish(
                run_id,
                EventType.RUN_FAILED,
                {"status": RunStatus.FAILED, "error": safe_error},
                actor=EventActor(type="system", name="run-manager"),
            )
            return
        self.ledger.update_run(
            run_id,
            status=RunStatus.COMPLETED,
            finished_at=utc_now(),
        )
        await self.events.publish(
            run_id,
            EventType.RUN_COMPLETED,
            {"status": RunStatus.COMPLETED},
            actor=EventActor(type="system", name="run-manager"),
        )

    async def cancel(self, run_id: str) -> RunRecord:
        run = self.ledger.get_run(run_id)
        if run.status in TERMINAL_RUN_STATUSES:
            return run
        run = self.ledger.update_run(run_id, status=RunStatus.CANCELLING)
        await self.events.publish(
            run_id,
            EventType.RUN_CANCEL_REQUESTED,
            {"status": RunStatus.CANCELLING},
            actor=EventActor(type="user", name="frontend-user"),
        )
        await self.permissions.deny_all(run_id, "Run cancelled by the user.")
        await self.runtime.cancel(run_id)
        task = self._tasks.get(run_id)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self._finalize_cancel(run_id)
        return self.ledger.get_run(run_id)

    async def _finalize_cancel(self, run_id: str) -> None:
        run = self.ledger.get_run(run_id)
        if run.status == RunStatus.CANCELLED:
            return
        self.ledger.update_run(
            run_id,
            status=RunStatus.CANCELLED,
            finished_at=utc_now(),
        )
        await self.events.publish(
            run_id,
            EventType.RUN_CANCELLED,
            {"status": RunStatus.CANCELLED},
            actor=EventActor(type="system", name="run-manager"),
        )


RuntimeFactory = Callable[[FileLedger, EventPublisher, PermissionBroker], AgentRuntime]
