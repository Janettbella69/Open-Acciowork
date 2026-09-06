"""Native SDK deferral checkpoints, not an alternate tool executor.

All state transitions share the Run's existing cross-process record lock. A
decision is consumed durably *before* returning allow/deny to PreToolUse. After
that boundary, a crashed worker has an uncertain outcome and must not replay.
"""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from typing import Any

from .constants import TERMINAL_RUN_STATUSES, PermissionDecision, RunStatus
from .destructive import is_destructive_delete
from .events import redact
from .grants import is_persistable_permission, normalized_permission_scope
from .models import (
    DurablePermissionWait,
    EventActor,
    PendingPermissionView,
    PermissionResolution,
    RunRecord,
    utc_now,
)
from .storage import FileLedger


class DurablePermissionConflict(RuntimeError):
    """The request cannot be resolved/resumed without risking a stale approval."""


def _digest(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _identity(run: RunRecord) -> str:
    return _digest(
        [
            run.id,
            run.project_id,
            run.tenant_id,
            run.principal_id,
            run.principal_role,
            run.principal_session_id,
            run.identity_binding,
            run.agent_id,
            run.mode,
            run.schedule_id,
            run.standing_intent_id,
            run.provider_profile_id,
            run.provider_snapshot,
            run.skill_snapshot,
        ]
    )


class DurablePermissionStore:
    def __init__(self, ledger: FileLedger, timeout_seconds: int) -> None:
        self.ledger = ledger
        self.timeout_seconds = timeout_seconds

    def _save(self, run: RunRecord, wait: DurablePermissionWait, **changes: Any) -> RunRecord:
        waits = [wait if entry.id == wait.id else entry for entry in run.permission_waits]
        if not any(entry.id == wait.id for entry in waits):
            waits.append(wait)
        return self.ledger.update_run(run.id, permission_waits=waits, **changes)

    @staticmethod
    def active(run: RunRecord) -> DurablePermissionWait | None:
        return next(
            (wait for wait in run.permission_waits if wait.id == run.suspended_permission_id), None
        )

    def lookup(self, run_id: str, request_id: str) -> DurablePermissionWait | None:
        return next(
            (
                wait
                for wait in self.ledger.get_run(run_id).permission_waits
                if wait.id == request_id
            ),
            None,
        )

    def prepare(
        self,
        run_id: str,
        session_id: str,
        tool_use_id: str,
        tool_name: str,
        tool_input: dict[str, Any],
        agent_id: str | None,
    ) -> DurablePermissionWait:
        if not session_id or not tool_use_id or agent_id:
            raise DurablePermissionConflict("Only an anchored main-Agent call can be deferred.")
        with self.ledger._lock_for(run_id):
            run = self.ledger.get_run(run_id)
            if run.status in TERMINAL_RUN_STATUSES or run.status == RunStatus.CANCELLING:
                raise DurablePermissionConflict("This Run no longer accepts tool calls.")
            digest = _digest(tool_input)
            existing = next(
                (
                    item
                    for item in run.permission_waits
                    if item.session_id == session_id and item.tool_use_id == tool_use_id
                ),
                None,
            )
            if existing is not None:
                self._match(run, existing, session_id, tool_use_id, tool_name, tool_input, agent_id)
                return existing
            active = self.active(run)
            if active is not None and active.state in {"preparing", "pending", "resolved"}:
                raise DurablePermissionConflict("Another deferred invocation is unresolved.")
            safe_input = redact(tool_input)
            wait = DurablePermissionWait(
                session_id=session_id,
                tool_use_id=tool_use_id,
                tool_name=tool_name,
                input_digest=digest,
                identity_digest=_identity(run),
                tool_input=safe_input if isinstance(safe_input, dict) else {},
                agent_id=agent_id,
                scope=str(redact(normalized_permission_scope(tool_input))),
                destructive=is_destructive_delete(tool_name, tool_input),
                persistable=is_persistable_permission(tool_name, tool_input),
                expires_at=utc_now() + timedelta(seconds=self.timeout_seconds),
            )
            self._save(run, wait, suspended_permission_id=wait.id)
            return wait

    @staticmethod
    def _match(
        run: RunRecord,
        wait: DurablePermissionWait,
        session_id: str,
        tool_use_id: str,
        tool_name: str,
        tool_input: dict[str, Any],
        agent_id: str | None,
    ) -> None:
        if (
            wait.identity_digest != _identity(run)
            or wait.session_id != session_id
            or wait.tool_use_id != tool_use_id
            or wait.tool_name != tool_name
            or wait.agent_id != agent_id
            or wait.input_digest != _digest(tool_input)
        ):
            raise DurablePermissionConflict("Deferred invocation or identity no longer matches.")

    def confirm(
        self,
        run_id: str,
        session_id: str,
        tool_use_id: str,
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> DurablePermissionWait:
        """Called only after the SDK emits a successful tool_deferred result."""
        with self.ledger._lock_for(run_id):
            run = self.ledger.get_run(run_id)
            wait = self.active(run)
            if wait is None or wait.state not in {"preparing", "pending"}:
                raise DurablePermissionConflict("The SDK returned an unrequested deferred call.")
            self._match(run, wait, session_id, tool_use_id, tool_name, tool_input, None)
            if run.status in TERMINAL_RUN_STATUSES or run.status == RunStatus.CANCELLING:
                raise DurablePermissionConflict("The Run was cancelled before deferral completed.")
            wait = wait.model_copy(update={"state": "pending", "confirmed_at": utc_now()})
            self._save(
                run,
                wait,
                status=RunStatus.WAITING_PERMISSION,
                sdk_session_id=session_id,
                finished_at=None,
            )
            return wait

    def resolve(
        self, run_id: str, request_id: str, resolution: PermissionResolution, actor: EventActor
    ) -> DurablePermissionWait:
        with self.ledger._lock_for(run_id):
            run = self.ledger.get_run(run_id)
            wait = self.active(run)
            if (
                wait is None
                or wait.id != request_id
                or wait.state != "pending"
                or run.status != RunStatus.WAITING_PERMISSION
                or wait.identity_digest != _identity(run)
            ):
                raise DurablePermissionConflict(
                    "Approval is stale, already resolved, or not suspended."
                )
            if wait.expires_at <= utc_now():
                self._save(run, wait.model_copy(update={"state": "expired"}))
                raise DurablePermissionConflict("The deferred approval has expired.")
            if not wait.persistable and resolution.decision in {
                PermissionDecision.ALLOW_RUN,
                PermissionDecision.ALLOW_PERSISTENT,
            }:
                resolution = resolution.model_copy(
                    update={
                        "decision": PermissionDecision.ALLOW_ONCE,
                        "expires_at": None,
                    }
                )
            wait = wait.model_copy(
                update={"state": "resolved", "resolution": resolution, "actor": actor}
            )
            self._save(run, wait)
            return wait

    def claim_resume(self, run_id: str) -> RunRecord:
        """RunManager must hold the existing execution lease around this call."""
        with self.ledger._lock_for(run_id):
            run = self.ledger.get_run(run_id)
            wait = self.active(run)
            if (
                wait is None
                or wait.state != "resolved"
                or wait.confirmed_at is None
                or run.status != RunStatus.WAITING_PERMISSION
                or run.sdk_session_id != wait.session_id
                or wait.identity_digest != _identity(run)
            ):
                raise DurablePermissionConflict("There is no approved native checkpoint to resume.")
            if wait.expires_at <= utc_now():
                self._save(run, wait.model_copy(update={"state": "expired"}))
                raise DurablePermissionConflict("The deferred approval has expired.")
            return self.ledger.update_run(
                run_id, status=RunStatus.QUEUED, error=None, error_code=None, finished_at=None
            )

    def consume(
        self,
        run_id: str,
        session_id: str,
        tool_use_id: str,
        tool_name: str,
        tool_input: dict[str, Any],
        agent_id: str | None,
    ) -> PermissionResolution:
        with self.ledger._lock_for(run_id):
            run = self.ledger.get_run(run_id)
            wait = self.active(run)
            if wait is None:
                raise DurablePermissionConflict("No deferred approval exists for this call.")
            self._match(run, wait, session_id, tool_use_id, tool_name, tool_input, agent_id)
            if (
                wait.state != "resolved"
                or wait.resolution is None
                or run.status != RunStatus.RUNNING
            ):
                raise DurablePermissionConflict("Approval was already consumed or is unavailable.")
            if wait.expires_at <= utc_now():
                self._save(run, wait.model_copy(update={"state": "expired"}))
                raise DurablePermissionConflict("The deferred approval has expired.")
            self._save(run, wait.model_copy(update={"state": "consumed", "consumed_at": utc_now()}))
            return wait.resolution

    def retain_unavailable(
        self,
        run_id: str,
        session_id: str,
        tool_use_id: str,
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> None:
        """Only the SDK's tool_deferred_unavailable proves no hook/tool ran."""
        with self.ledger._lock_for(run_id):
            run = self.ledger.get_run(run_id)
            wait = self.active(run)
            if wait is None or wait.state != "resolved":
                raise DurablePermissionConflict("No unconsumed approval can be retained.")
            self._match(run, wait, session_id, tool_use_id, tool_name, tool_input, None)
            self._save(
                run,
                wait,
                status=RunStatus.WAITING_PERMISSION,
                error="The deferred SDK tool is unavailable; restore it before resuming.",
                finished_at=None,
            )

    def invalidate(self, run_id: str, *, cancelled: bool = False) -> None:
        with self.ledger._lock_for(run_id):
            run = self.ledger.get_run(run_id)
            waits = [
                wait.model_copy(update={"state": "cancelled" if cancelled else "uncertain"})
                if wait.id == run.suspended_permission_id
                and wait.state in {"preparing", "pending", "resolved", "consumed"}
                else wait
                for wait in run.permission_waits
            ]
            self.ledger.update_run(run_id, permission_waits=waits)

    def recover(self, run_id: str) -> bool:
        """Preserve a confirmed unconsumed wait; never execute during recovery."""
        with self.ledger._lock_for(run_id):
            run = self.ledger.get_run(run_id)
            wait = self.active(run)
            if (
                wait is None
                or wait.state not in {"pending", "resolved", "expired"}
                or wait.confirmed_at is None
                or wait.identity_digest != _identity(run)
                or run.status in TERMINAL_RUN_STATUSES
                or run.status == RunStatus.CANCELLING
            ):
                return False
            if wait.expires_at <= utc_now():
                wait = wait.model_copy(update={"state": "expired"})
            self._save(run, wait, status=RunStatus.WAITING_PERMISSION)
            return True

    def list_pending(self) -> list[PendingPermissionView]:
        views = []
        for run in self.ledger.list_runs():
            wait = self.active(run)
            if (
                wait is None
                or run.status != RunStatus.WAITING_PERMISSION
                or wait.state not in {"pending", "resolved", "expired"}
                or wait.confirmed_at is None
            ):
                continue
            state = "expired" if wait.expires_at <= utc_now() else wait.state
            views.append(
                PendingPermissionView(
                    id=wait.id,
                    run_id=run.id,
                    project_id=run.project_id,
                    tool_name=wait.tool_name,
                    tool_input=wait.tool_input,
                    agent_id=wait.agent_id,
                    scope=wait.scope,
                    destructive=wait.destructive,
                    persistable=wait.persistable,
                    created_at=wait.created_at,
                    timeout_seconds=self.timeout_seconds,
                    durable=True,
                    state=state,
                    tool_use_id=wait.tool_use_id,
                    expires_at=wait.expires_at,
                )
            )
        return views
