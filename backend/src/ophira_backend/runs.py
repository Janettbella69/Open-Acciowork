from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from typing import Protocol

from .constants import (
    HEARTBEAT_OK,
    TERMINAL_RUN_STATUSES,
    EventType,
    ProviderErrorCode,
    RunMode,
    RunRetryStrategy,
    RunStatus,
    ScheduleKind,
)
from .distill import distill_run
from .events import EventPublisher, redact
from .memory import FileMemoryProvider
from .models import EventActor, RunRecord, utc_now
from .permissions import PermissionBroker
from .policy import OrgPolicyStore, PolicyDeniedError
from .providers import ProviderConfigurationError, ProviderRegistry, classify_provider_error
from .skills import SkillRegistry
from .storage import FileLedger


class AgentRuntime(Protocol):
    async def run(self, run: RunRecord) -> None: ...

    async def cancel(self, run_id: str) -> None: ...


class RunRetryConflictError(RuntimeError):
    """The persisted source run cannot support the requested retry strategy."""


class RunManager:
    def __init__(
        self,
        ledger: FileLedger,
        events: EventPublisher,
        permissions: PermissionBroker,
        runtime: AgentRuntime,
        providers: ProviderRegistry | None = None,
        skills: SkillRegistry | None = None,
        notifications=None,
        memory: FileMemoryProvider | None = None,
        policy: OrgPolicyStore | None = None,
        settings=None,
    ) -> None:
        self.ledger = ledger
        self.events = events
        self.permissions = permissions
        self.runtime = runtime
        self.providers = providers
        self.skills = skills
        self.notifications = notifications
        self.memory = memory
        self.policy = policy
        self.settings = settings
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
        resolve_default_provider: bool = True,
    ) -> RunRecord:
        if resolve_default_provider and provider_profile_id is None and self.providers is not None:
            provider_profile_id = self.providers.default_profile_id()
        if provider_profile_id is not None and not provider_profile_id.strip():
            raise PolicyDeniedError("provider profile id must not be empty")
        if self.policy is not None and not self.policy.allows_provider(provider_profile_id):
            raise PolicyDeniedError("provider is not allowed by organization policy")
        if provider_profile_id is not None and self.providers is None:
            raise PolicyDeniedError("provider registry is unavailable")
        if provider_profile_id is not None and self.providers is not None:
            profile = self.providers.get(provider_profile_id)
            readiness_failures: list[str] = []
            if not profile.enabled:
                readiness_failures.append("disabled")
            if not profile.production_eligible:
                readiness_failures.append("not production eligible")
            if not profile.secret_configured:
                readiness_failures.append("credential missing")
            if profile.last_verified_at is None:
                readiness_failures.append("preflight not completed")
            if profile.last_error_code is not None:
                readiness_failures.append(f"preflight failed ({profile.last_error_code})")
            if readiness_failures:
                raise PolicyDeniedError(
                    f"provider {provider_profile_id} is not ready for runs: "
                    + ", ".join(readiness_failures)
                )
            if provider_snapshot is None:
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
            self._notify_schedule_failure(run_id, safe_error, error_code)
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
        self._distill_memory(run_id)
        await self._notify_heartbeat_if_changed(run_id)

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

    def _distill_memory(self, run_id: str) -> None:
        if self.memory is None:
            return
        try:
            distill_run(self.ledger, self.memory, self.ledger.get_run(run_id))
        except Exception:
            return

    def _result_text(self, run_id: str) -> str:
        text = ""
        for event in self.ledger.list_events(run_id):
            if event.type == EventType.RUN_RESULT.value:
                text = str(event.data.get("result") or "")
            elif event.type == EventType.ASSISTANT_MESSAGE_COMPLETED.value:
                text = str(event.data.get("text") or text)
        return text.strip()

    async def _notify_heartbeat_if_changed(self, run_id: str) -> None:
        if self.notifications is None:
            return
        try:
            run = self.ledger.get_run(run_id)
            if not run.schedule_id:
                return
            schedule = self.ledger.get_schedule(run.schedule_id)
            if schedule.kind != ScheduleKind.HEARTBEAT:
                return
            result = self._result_text(run_id)
            digest = hashlib.sha256(result.encode("utf-8")).hexdigest()
            silent = (
                result == HEARTBEAT_OK
                or result.startswith(f"{HEARTBEAT_OK}\n")
                or (
                    schedule.last_result_digest is not None
                    and schedule.last_result_digest == digest
                )
            )
            self.ledger.update_schedule(schedule.id, last_result_digest=digest)
            if silent:
                return
            notification = self.notifications.add(
                title="巡检发现变化",
                body=result[:1_000] or "Heartbeat reported a change without a digest.",
                severity="info",
                project_id=run.project_id,
                run_id=run.id,
                schedule_id=run.schedule_id,
            )
            await self._deliver_channel(notification)
        except Exception:
            return

    async def _deliver_channel(self, notification) -> None:
        if self.policy is None or self.settings is None:
            return
        if not self.policy.get().channel_delivery_enabled:
            return
        try:
            from .channels import deliver_notification

            await deliver_notification(self.settings, notification)
        except Exception:
            return

    def _notify_schedule_failure(self, run_id: str, error: str, error_code) -> None:
        """FR-15: failures and expired credentials of unattended runs must be visible."""
        try:
            run = self.ledger.get_run(run_id)
            if not run.schedule_id:
                return
            code = getattr(error_code, "value", str(error_code) if error_code else "")
            auth_invalid = code == ProviderErrorCode.AUTH_REVOKED_OR_INVALID.value
            if auth_invalid:
                self.ledger.update_schedule(run.schedule_id, enabled=False)
            if self.notifications is None:
                return
            notification = self.notifications.add(
                title=(
                    f"定时任务已暂停：{code}"
                    if auth_invalid
                    else f"定时任务运行失败{f'：{code}' if code else ''}"
                ),
                body=(
                    f"{error[:850]} 请更新 Provider 凭据、完成成功预检后再恢复任务。"
                    if auth_invalid
                    else error[:1_000]
                ),
                severity="error",
                project_id=run.project_id,
                run_id=run.id,
                schedule_id=run.schedule_id,
            )
            asyncio.get_running_loop().create_task(self._deliver_channel(notification))
        except Exception:
            # Inbox delivery must never break the run lifecycle.
            return

    async def retry(self, run_id: str, strategy: RunRetryStrategy) -> RunRecord:
        source = self.ledger.get_run(run_id)
        if source.status not in {RunStatus.FAILED, RunStatus.CANCELLED}:
            raise RunRetryConflictError("Only failed or cancelled runs can be retried.")
        if strategy == RunRetryStrategy.RESUME and not source.sdk_session_id:
            raise RunRetryConflictError(
                "This run has no recorded SDK session to resume. Start a new branch instead."
            )

        return await self.create_and_start(
            source.project_id,
            source.prompt,
            agent_id=source.agent_id,
            resume_session_id=(
                source.sdk_session_id if strategy == RunRetryStrategy.RESUME else None
            ),
            mode=source.mode,
            provider_profile_id=source.provider_profile_id,
            provider_snapshot=source.provider_snapshot,
            skill_snapshot=source.skill_snapshot,
            resolve_default_provider=False,
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
        await self._capture_artifacts(run_id)
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
