from __future__ import annotations

import asyncio
import fcntl
import hashlib
import logging
import os
import threading
from collections.abc import Callable
from typing import Protocol

from .channel_delivery import ChannelDeliveryService, FeishuReplyTarget
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
from .data_operations import data_operation
from .distill import distill_run
from .durable_permissions import DurablePermissionConflict
from .events import EventPublisher, redact
from .feedback import FeedbackStore
from .memory import FileMemoryProvider
from .models import (
    EventActor,
    PermissionResolution,
    PrincipalBinding,
    PrincipalContext,
    RunRecord,
    new_id,
    utc_now,
)
from .permissions import PermissionBroker
from .policy import OrgPolicyStore, PolicyDeniedError
from .project_events import ProjectEventProjector
from .providers import (
    ProviderConfigurationError,
    ProviderRegistry,
    classify_provider_error,
    provider_readiness_failures,
)
from .skills import SkillRegistry
from .storage import FileLedger
from .workspace_checkpoints import WorkspaceCheckpoints, try_workspace_lease

INTERRUPTED_RUN_MESSAGE = (
    "The backend stopped before this run finished. Saved evidence, artifacts "
    "and SDK session are preserved. Review the saved work before retrying."
)
logger = logging.getLogger(__name__)


class AgentRuntime(Protocol):
    async def run(self, run: RunRecord) -> None: ...

    async def cancel(self, run_id: str) -> None: ...


class RunRetryConflictError(RuntimeError):
    """The persisted source run cannot support the requested retry strategy."""


class RunResumeConflictError(RuntimeError):
    """A client-supplied session locator is not a safe resumable lineage."""


class RunCancelConflictError(RuntimeError):
    """Cancellation cannot be dispatched by a worker that does not own the run."""


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
        channel_delivery: ChannelDeliveryService | None = None,
        feedback: FeedbackStore | None = None,
        project_events: ProjectEventProjector | None = None,
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
        self.channel_delivery = channel_delivery
        self.feedback = feedback
        self.project_events = project_events or ProjectEventProjector(ledger, events, memory=memory)
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._workspace_owners: set[str] = set()

    def _acquire_run_lease(self, run_id: str) -> int | None:
        # The lease exists before run.json is published and is held until the
        # execution task has finished, including its cancellation cleanup.
        directory = self.ledger.root / "run-leases"
        directory.mkdir(exist_ok=True)
        path = directory / f"{self.ledger._safe_id(run_id)}.lock"
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(descriptor)
            return None
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor

    def is_executing(self, run_id: str) -> bool:
        """Include cleanup after terminal status, even when another worker owns it."""
        lease = self._acquire_run_lease(run_id)
        if lease is None:
            return True
        os.close(lease)
        return False

    @data_operation()
    async def recover_interrupted(self) -> None:
        """Make ownerless runs retryable without replaying any model or external action."""

        for candidate in self.ledger.list_runs():
            if (
                candidate.status in TERMINAL_RUN_STATUSES
                and candidate.error != INTERRUPTED_RUN_MESSAGE
            ):
                await self.project_events.reconcile_run(candidate.id)
                continue
            lease = self._acquire_run_lease(candidate.id)
            if lease is None:
                continue  # Another live worker still owns this run.
            try:
                run = self.ledger.get_run(candidate.id)
                if run.status in TERMINAL_RUN_STATUSES:
                    # Complete the crash window between the recovery snapshot
                    # and its terminal event, without rewriting existing history.
                    if (
                        run.status == RunStatus.FAILED
                        and run.error == INTERRUPTED_RUN_MESSAGE
                        and not any(
                            event.type == EventType.RUN_FAILED.value
                            for event in self.ledger.list_events(run.id)
                        )
                    ):
                        await self.events.publish(
                            run.id,
                            EventType.RUN_FAILED,
                            {
                                "status": run.status,
                                "error": run.error,
                                "error_code": run.error_code,
                            },
                            actor=EventActor(type="system", name="run-manager"),
                        )
                    self._capture_terminal_feedback(run.id)
                    continue
                message = INTERRUPTED_RUN_MESSAGE
                if self.permissions.durable.recover(run.id):
                    # SDK confirmed the call never executed. Keep its approval
                    # available across workers; recovery never dispatches tools.
                    continue
                if run.permission_waits:
                    self.permissions.durable.invalidate(run.id)
                # Close historical permission prompts whose in-memory futures
                # disappeared with the worker; never restore an old approval.
                pending: set[str] = set()
                for event in self.ledger.list_events(run.id):
                    request_id = event.data.get("request_id")
                    if not isinstance(request_id, str):
                        continue
                    if event.type == EventType.PERMISSION_REQUESTED.value:
                        pending.add(request_id)
                    elif event.type in {
                        EventType.PERMISSION_RESOLVED.value,
                        EventType.PERMISSION_TIMED_OUT.value,
                    }:
                        pending.discard(request_id)
                for request_id in sorted(pending):
                    await self.events.publish(
                        run.id,
                        EventType.PERMISSION_TIMED_OUT,
                        {"request_id": request_id, "reason": message},
                        actor=EventActor(type="system", name="run-manager"),
                    )
                if run.status == RunStatus.CANCELLING:
                    await self._finalize_cancel(run.id)
                    continue
                await self._capture_artifacts(run.id)
                self.ledger.update_run(
                    run.id,
                    status=RunStatus.FAILED,
                    error=message,
                    error_code=ProviderErrorCode.SDK_RUNTIME_FAILED,
                    finished_at=utc_now(),
                )
                await self.events.publish(
                    run.id,
                    EventType.RUN_FAILED,
                    {
                        "status": RunStatus.FAILED,
                        "error": message,
                        "error_code": ProviderErrorCode.SDK_RUNTIME_FAILED,
                    },
                    actor=EventActor(type="system", name="run-manager"),
                )
                self._write_diary(run.id, RunStatus.FAILED, message)
                self._notify_schedule_failure(run.id, message, ProviderErrorCode.SDK_RUNTIME_FAILED)
                self._capture_terminal_feedback(run.id)
            finally:
                try:
                    await self.project_events.reconcile_run(candidate.id)
                finally:
                    os.close(lease)

    async def stop(self) -> None:
        # Schedulers stop first so no new run can race with this snapshot.
        owned = list(self._tasks.items())
        for _, task in owned:
            task.cancel()
        if owned:
            await asyncio.gather(*(task for _, task in owned), return_exceptions=True)
        for run_id, _ in owned:
            if self.ledger.get_run(run_id).status not in TERMINAL_RUN_STATUSES:
                # A queued task cancelled before its first instruction never
                # enters _execute's cancellation handler.
                await self._finalize_cancel(run_id)

    async def resolve_permission(
        self, run_id: str, request_id: str, resolution: PermissionResolution,
        *, actor: EventActor | None = None,
    ) -> None:
        if self.permissions.durable.lookup(run_id, request_id) is None:
            await self.permissions.resolve(run_id, request_id, resolution, actor=actor)
            return
        if not getattr(self.runtime, "durable_resume_supported", False):
            raise DurablePermissionConflict("This SDK runtime has no verified durable resume gate.")
        lease = self._acquire_run_lease(run_id)
        if lease is None:
            raise DurablePermissionConflict(
                "The SDK is still stopping; refresh and retry approval."
            )
        try:
            validate = getattr(self.runtime, "validate_deferred_resume", None)
            if validate is not None:
                validate(self.ledger.get_run(run_id))
            wait = self.permissions.durable.resolve(
                run_id, request_id, resolution,
                actor or EventActor(type="user", name="frontend-user"),
            )
            await self.events.publish(
                run_id, EventType.PERMISSION_RESOLVED,
                {"request_id": request_id, "tool_name": wait.tool_name,
                 "decision": wait.resolution.decision, "reason": wait.resolution.reason,
                 "durable": True, "tool_use_id": wait.tool_use_id},
                actor=wait.actor,
            )
            self._start_deferred(run_id, lease)
        except BaseException:
            os.close(lease)
            raise

    @data_operation()
    async def resume_permission(self, run_id: str) -> RunRecord:
        if not getattr(self.runtime, "durable_resume_supported", False):
            raise DurablePermissionConflict("This SDK runtime has no verified durable resume gate.")
        lease = self._acquire_run_lease(run_id)
        if lease is None:
            raise DurablePermissionConflict("Another worker owns this Run; refresh and retry.")
        try:
            return self._start_deferred(run_id, lease)
        except BaseException:
            os.close(lease)
            raise

    def _start_deferred(self, run_id: str, lease: int) -> RunRecord:
        validate = getattr(self.runtime, "validate_deferred_resume", None)
        if validate is not None:
            validate(self.ledger.get_run(run_id))
        run = self.permissions.durable.claim_resume(run_id)
        task = asyncio.create_task(self._execute(run_id), name=f"ophira:resume:{run_id}")
        self._tasks[run_id] = task

        def release_run(_task: asyncio.Task[None]) -> None:
            self._tasks.pop(run_id, None)
            os.close(lease)

        task.add_done_callback(release_run)
        return run

    @data_operation()
    async def create_and_start(
        self,
        project_id: str,
        prompt: str,
        *,
        agent_id: str | None = None,
        schedule_id: str | None = None,
        task_experience_config_hash: str | None = None,
        standing_intent_id: str | None = None,
        automation_claim_id: str | None = None,
        companion_device_id: str | None = None,
        resume_session_id: str | None = None,
        mode: RunMode = RunMode.STANDARD,
        provider_profile_id: str | None = None,
        provider_snapshot: dict[str, object] | None = None,
        skill_snapshot: list[dict[str, object]] | None = None,
        max_budget_usd: float | None = None,
        resolve_default_provider: bool = True,
        channel_reply_target: FeishuReplyTarget | None = None,
        principal: PrincipalContext | PrincipalBinding | None = None,
    ) -> RunRecord:
        if (schedule_id is None) != (task_experience_config_hash is None):
            raise ValueError("scheduled dispatch requires its host experience configuration hash")
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
            readiness_failures = provider_readiness_failures(profile)
            if readiness_failures:
                raise PolicyDeniedError(
                    f"provider {provider_profile_id} is not ready for runs: "
                    + ", ".join(readiness_failures)
                )
            if provider_snapshot is None:
                provider_snapshot = self.providers.snapshot(provider_profile_id)
        if skill_snapshot is None and self.skills:
            skill_snapshot = self.skills.snapshot()
        resume_source = self._resolve_resume_source(
            resume_session_id,
            project_id=project_id,
            agent_id=agent_id,
            provider_profile_id=provider_profile_id,
            principal=principal,
        )
        run_id = new_id("run")
        lease = self._acquire_run_lease(run_id)
        if lease is None:
            raise RuntimeError("Unable to acquire a new run lease")
        try:
            run = self.ledger.create_run(
                project_id,
                prompt,
                agent_id=agent_id,
                schedule_id=schedule_id,
                standing_intent_id=standing_intent_id,
                automation_claim_id=automation_claim_id,
                companion_device_id=companion_device_id,
                resume_session_id=resume_session_id,
                resume_source_run_id=resume_source.id if resume_source else None,
                session_compaction_epoch=(
                    resume_source.session_compaction_epoch if resume_source else 0
                ),
                mode=mode,
                provider_profile_id=provider_profile_id,
                provider_snapshot=provider_snapshot,
                skill_snapshot=skill_snapshot,
                max_budget_usd=max_budget_usd,
                principal=principal,
                run_id=run_id,
            )
            if task_experience_config_hash is not None:
                # Persist before publishing RUN_CREATED or starting the SDK task.
                # A crash before this write cannot initialize experience later
                # from a different, mutable Schedule definition.
                run = self.ledger.update_run(
                    run.id, task_experience_config_hash=task_experience_config_hash,
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
                    "standing_intent_id": run.standing_intent_id,
                    "companion_device_id": run.companion_device_id,
                    "max_budget_usd": run.max_budget_usd,
                    "resume_source_run_id": run.resume_source_run_id,
                },
                actor=EventActor(type="system", name="run-manager"),
            )
            if channel_reply_target is not None:
                try:
                    if self.channel_delivery is None:
                        raise RuntimeError("channel delivery service unavailable")
                    self.channel_delivery.bind_run(run, channel_reply_target)
                except Exception as exc:
                    safe_error = (
                        "Verified channel reply target could not be persisted; "
                        "the Agent run was not started."
                    )
                    self.ledger.update_run(
                        run.id,
                        status=RunStatus.FAILED,
                        error=safe_error,
                        finished_at=utc_now(),
                    )
                    await self.events.publish(
                        run.id,
                        EventType.RUN_FAILED,
                        {"status": RunStatus.FAILED, "error": safe_error},
                        actor=EventActor(type="system", name="run-manager"),
                    )
                    self._capture_terminal_feedback(run.id)
                    raise RuntimeError(safe_error) from exc
            task = asyncio.create_task(self._execute(run.id), name=f"ophira:{run.id}")
            self._tasks[run.id] = task

            def release_run(_task: asyncio.Task[None]) -> None:
                self._tasks.pop(run.id, None)
                os.close(lease)

            task.add_done_callback(release_run)
            return run
        except BaseException:
            os.close(lease)
            raise

    def _resolve_resume_source(
        self,
        session_id: str | None,
        *,
        project_id: str,
        agent_id: str | None,
        provider_profile_id: str | None,
        principal: PrincipalContext | PrincipalBinding | None,
    ) -> RunRecord | None:
        if session_id is None:
            return None
        bound = self.ledger.runs_for_sdk_session(session_id)
        if not bound:
            raise RunResumeConflictError(
                "Unknown SDK session. Start a new conversation instead of resuming it."
            )
        if any(run.project_id != project_id for run in bound):
            raise RunResumeConflictError(
                "SDK sessions cannot be resumed across project boundaries."
            )
        if principal is None:
            if any(run.identity_binding != "local_only" for run in bound):
                raise RunResumeConflictError(
                    "Authenticated SDK sessions require their bound principal."
                )
        elif any(
            run.identity_binding != "authenticated_principal"
            or run.tenant_id != principal.tenant_id
            or run.principal_id != principal.principal_id
            or run.principal_role != principal.role
            or run.principal_session_id != principal.session_id
            for run in bound
        ):
            raise RunResumeConflictError(
                "SDK sessions cannot be resumed across principal boundaries."
            )
        active_claims = [
            run
            for run in self.ledger.runs_claiming_sdk_session(session_id)
            if run.status not in TERMINAL_RUN_STATUSES
        ]
        if active_claims:
            raise RunResumeConflictError(
                "This SDK session already has an active run and cannot be resumed concurrently."
            )
        source = bound[0]
        if any(item.suspended_permission_id for item in bound):
            raise RunResumeConflictError(
                "A deferred or uncertain invocation must use its original approval checkpoint; "
                "it cannot be replayed through a normal conversation or retry."
            )
        if source.status not in TERMINAL_RUN_STATUSES:
            raise RunResumeConflictError("Only a terminal run session can be resumed.")
        if source.agent_id != agent_id:
            raise RunResumeConflictError(
                "A resumed SDK session must keep the same Agent. Start a new conversation "
                "to switch Agents."
            )
        if source.provider_profile_id != provider_profile_id:
            raise RunResumeConflictError(
                "A resumed SDK session must keep the same Provider. Start a new conversation "
                "to switch Providers."
            )
        return source

    @data_operation(wait=True)
    async def _execute(self, run_id: str) -> None:
        workspace_lease = None
        try:
            project_id = self.ledger.get_run(run_id).project_id
            while workspace_lease is None:
                workspace_lease = try_workspace_lease(self.ledger, project_id)
                if workspace_lease is None:
                    await asyncio.sleep(0.05)
            self._workspace_owners.add(run_id)
            await self._execute_runtime(run_id)
        except asyncio.CancelledError:
            await self._finalize_cancel(run_id)
        except Exception as exc:
            if self.ledger.get_run(run_id).status not in TERMINAL_RUN_STATUSES:
                await self._fail_run(run_id, exc)
            else:
                raise
        finally:
            # A committed record may outlive a failed notification or tool
            # response. Repair observations without repeating business writes.
            try:
                await self.project_events.reconcile_run(run_id)
            finally:
                if workspace_lease is not None:
                    self._workspace_owners.discard(run_id)
                    os.close(workspace_lease)

    async def _checkpoint_workspace(self, run_id: str) -> None:
        cancelled = threading.Event()
        capture = asyncio.create_task(asyncio.to_thread(
            WorkspaceCheckpoints(self.ledger).prepare_run, run_id, cancelled.is_set,
        ))
        interrupted = False
        # The worker owns filesystem descriptors until it finishes. Cancellation
        # requests cooperative stop and retains the lease through actual cleanup.
        while not capture.done():
            try:
                await asyncio.shield(capture)
            except asyncio.CancelledError:
                interrupted = True
                cancelled.set()
            except Exception:
                break
        if interrupted:
            try:
                capture.result()
            except BaseException:
                pass
            raise asyncio.CancelledError
        capture.result()

    async def _execute_runtime(self, run_id: str) -> None:
        try:
            await self._checkpoint_workspace(run_id)
            run = self.ledger.update_run(
                run_id, status=RunStatus.RUNNING,
                started_at=self.ledger.get_run(run_id).started_at or utc_now(),
            )
            await self.events.publish(
                run_id, EventType.RUN_STARTED, {"status": RunStatus.RUNNING},
                actor=EventActor(type="system", name="run-manager"),
            )
            await self.runtime.run(run)
        except asyncio.CancelledError:
            await self._finalize_cancel(run_id)
            return
        except Exception as exc:
            await self._fail_run(run_id, exc)
            return
        current = self.ledger.get_run(run_id)
        wait = self.permissions.durable.active(current)
        if (current.status == RunStatus.WAITING_PERMISSION and wait is not None
                and wait.state in {"pending", "resolved"} and wait.confirmed_at is not None):
            # A successful SDK tool_deferred result is not a completed task.
            return
        self.ledger.update_run(
            run_id,
            status=RunStatus.COMPLETED,
            finished_at=utc_now(),
            suspended_permission_id=None,
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
        await self._finalize_channel_reply(run_id)

    async def _fail_run(self, run_id: str, exc: Exception) -> None:
        if self.ledger.get_run(run_id).permission_waits:
            self.permissions.durable.invalidate(run_id)
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
        self._capture_terminal_feedback(run_id)
        await self._finalize_channel_reply(run_id)

    async def _finalize_channel_reply(self, run_id: str) -> None:
        """Best-effort delivery never changes the already durable Run outcome."""

        if self.channel_delivery is None:
            return
        try:
            await self.channel_delivery.finalize_run(run_id)
        except Exception:
            # The delivery obligation is durable and the recovery worker retries it.
            return

    async def _capture_artifacts(self, run_id: str) -> None:
        lease = None
        try:
            run = self.ledger.get_run(run_id)
            if run_id not in self._workspace_owners:
                # Queued cancellation and orphan recovery have no runtime lease.
                # Never claim files being written by another Run as their output.
                lease = try_workspace_lease(self.ledger, run.project_id)
                if lease is None:
                    return
            artifacts = self.ledger.scan_workspace(run.project_id, run_id=run.id)
            for artifact in artifacts:
                if artifact.run_id != run.id:
                    continue
                await self.project_events.artifact_registered(run.project_id, artifact.id)
        except Exception:
            # Artifact discovery must not rewrite a completed run as failed.
            return
        finally:
            if lease is not None:
                os.close(lease)

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
            if run.standing_intent_id:
                lines.append(f"- 来源：前瞻意图 {run.standing_intent_id}")
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

    @data_operation()
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

    async def retry(
        self,
        run_id: str,
        strategy: RunRetryStrategy,
        *,
        principal: PrincipalContext | PrincipalBinding | None = None,
    ) -> RunRecord:
        source = self.ledger.get_run(run_id)
        if source.standing_intent_id is not None:
            raise RunRetryConflictError(
                "Standing-intent checks must be re-dispatched through their watcher budget."
            )
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
            principal=principal,
        )

    @data_operation()
    async def cancel(self, run_id: str, *, actor: EventActor | None = None) -> RunRecord:
        run = self.ledger.get_run(run_id)
        if run.status in TERMINAL_RUN_STATUSES:
            return run
        task = self._tasks.get(run_id)
        lease = None
        if task is None or task.done():
            # An empty local task map says nothing about another API worker.
            # Only an acquired lease proves this is an ownerless orphan.
            lease = self._acquire_run_lease(run_id)
            if lease is None:
                run = self.ledger.get_run(run_id)
                if run.status in TERMINAL_RUN_STATUSES:
                    return run
                raise RunCancelConflictError(
                    "Run is owned by another live worker. Route cancellation to "
                    "the owning worker; no cancellation was issued."
                )
        try:
            run = self.ledger.get_run(run_id)
            if run.status in TERMINAL_RUN_STATUSES:
                return run
            self.ledger.update_run(run_id, status=RunStatus.CANCELLING)
            await self.events.publish(
                run_id,
                EventType.RUN_CANCEL_REQUESTED,
                {"status": RunStatus.CANCELLING},
                actor=actor or EventActor(type="user", name="frontend-user"),
            )
            await self.permissions.deny_all(run_id, "Run cancelled by the user.")
            await self.runtime.cancel(run_id)
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            run = self.ledger.get_run(run_id)
            if run.status in TERMINAL_RUN_STATUSES:
                return run
            if lease is None:
                # A queued task cancelled before _execute starts never runs its
                # cleanup. Its done callback released the original lease;
                # acquire it again before publishing an ownerless terminal state.
                lease = self._acquire_run_lease(run_id)
                if lease is None:
                    raise RunCancelConflictError(
                        "Cancellation cleanup is owned by another worker. "
                        "Refresh the recorded run status before retrying."
                    )
            await self._finalize_cancel(run_id)
            return self.ledger.get_run(run_id)
        finally:
            if lease is not None:
                os.close(lease)

    @data_operation()
    async def _finalize_cancel(self, run_id: str) -> None:
        run = self.ledger.get_run(run_id)
        if run.status in TERMINAL_RUN_STATUSES:
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
        self._capture_terminal_feedback(run_id)

    def _capture_terminal_feedback(self, run_id: str) -> None:
        if self.feedback is None:
            return
        try:
            self.feedback.capture_terminal(
                self.ledger.get_run(run_id), self.ledger.list_events(run_id),
            )
        except Exception as exc:
            # Candidate collection cannot turn a completed cancellation/failure
            # into a different Run outcome; startup reconciliation retries it.
            logger.error("terminal feedback capture failed for %s: %s", run_id, redact(str(exc)))


RuntimeFactory = Callable[[FileLedger, EventPublisher, PermissionBroker], AgentRuntime]
