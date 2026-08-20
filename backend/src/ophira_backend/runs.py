from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Protocol

from .constants import TERMINAL_RUN_STATUSES, EventType, RunMode, RunStatus
from .events import EventPublisher, redact
from .models import EventActor, RunRecord, utc_now
from .permissions import PermissionBroker
from .providers import ProviderConfigurationError, ProviderRegistry, classify_provider_error
from .skills import SkillRegistry
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
        providers: ProviderRegistry | None = None,
        skills: SkillRegistry | None = None,
    ) -> None:
        self.ledger = ledger
        self.events = events
        self.permissions = permissions
        self.runtime = runtime
        self.providers = providers
        self.skills = skills
        self._tasks: dict[str, asyncio.Task[None]] = {}

    async def create_and_start(
        self,
        project_id: str,
        prompt: str,
        *,
        agent_id: str | None = None,
        schedule_id: str | None = None,
        resume_session_id: str | None = None,
        mode: RunMode = RunMode.STANDARD,
        provider_profile_id: str | None = None,
        provider_snapshot: dict[str, object] | None = None,
        skill_snapshot: list[dict[str, object]] | None = None,
    ) -> RunRecord:
        if provider_profile_id and provider_snapshot is None and self.providers:
            provider_snapshot = self.providers.snapshot(provider_profile_id)
        if skill_snapshot is None and self.skills:
            skill_snapshot = self.skills.snapshot()
        run = self.ledger.create_run(
            project_id,
            prompt,
            agent_id=agent_id,
            schedule_id=schedule_id,
            resume_session_id=resume_session_id,
            mode=mode,
            provider_profile_id=provider_profile_id,
            provider_snapshot=provider_snapshot,
            skill_snapshot=skill_snapshot,
        )
        await self.events.publish(
            run.id,
            EventType.RUN_CREATED,
            {
                "status": run.status,
                "prompt": run.prompt,
                "mode": run.mode,
                "provider_profile_id": run.provider_profile_id,
                "skill_snapshot": run.skill_snapshot,
            },
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
            error_code = (
                exc.code
                if isinstance(exc, ProviderConfigurationError)
                else classify_provider_error(exc)
            )
            self.ledger.update_run(
                run_id,
                status=RunStatus.FAILED,
                error=safe_error,
                error_code=error_code,
                finished_at=utc_now(),
            )
            await self.events.publish(
                run_id,
                EventType.RUN_FAILED,
                {
                    "status": RunStatus.FAILED,
                    "error": safe_error,
                    "error_code": error_code,
                },
                actor=EventActor(type="system", name="run-manager"),
            )
            await self._capture_artifacts(run_id)
            self._write_diary(run_id, RunStatus.FAILED, safe_error)
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
        await self._capture_artifacts(run_id)
        self._write_diary(run_id, RunStatus.COMPLETED, "")

    async def _capture_artifacts(self, run_id: str) -> None:
        try:
            run = self.ledger.get_run(run_id)
            artifacts = self.ledger.scan_workspace(run.project_id, run_id=run.id)
            for artifact in artifacts:
                if artifact.run_id != run.id:
                    continue
                await self.events.publish(
                    run_id,
                    EventType.ARTIFACT_REGISTERED,
                    artifact.model_dump(mode="json"),
                    actor=EventActor(type="system", name="artifact-ledger"),
                )
        except Exception:
            # Artifact discovery must not rewrite a completed run as failed.
            return

    def _write_diary(self, run_id: str, status: RunStatus, note: str) -> None:
        """Append a work-journal entry for the finished run (PRD diary layer)."""
        try:
            run = self.ledger.get_run(run_id)
            now = utc_now()
            path = self.ledger.diary_dir(run) / f"{now.date().isoformat()}.md"
            prompt = " ".join(run.prompt.split())[:200]
            lines = [
                f"## {now.time().strftime('%H:%M:%S')} · {run.id}",
                f"- 目标：{prompt}",
                f"- 状态：{status.value}",
            ]
            if run.schedule_id:
                lines.append(f"- 来源：定时任务 {run.schedule_id}")
            if note:
                lines.append(f"- 备注：{note[:300]}")
            with path.open("a", encoding="utf-8") as handle:
                handle.write("\n".join(lines) + "\n\n")
        except Exception:
            # Diary is an audit aid; it must never break the run lifecycle.
            pass

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
        self._write_diary(run_id, RunStatus.CANCELLED, "")


RuntimeFactory = Callable[[FileLedger, EventPublisher, PermissionBroker], AgentRuntime]
