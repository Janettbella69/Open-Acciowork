from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .constants import EventType, PermissionDecision, RunStatus
from .destructive import is_destructive_delete
from .durable_permissions import DurablePermissionStore
from .events import EventPublisher, redact
from .grants import (
    PermissionGrantStore,
    is_persistable_permission,
    normalized_permission_scope,
    permission_scope_covers,
)
from .models import EventActor, PendingPermissionView, PermissionResolution, new_id, utc_now
from .storage import FileLedger, LedgerNotFoundError


class PermissionNotFoundError(KeyError):
    pass


def _schedule_scope_covers(
    allowed_scope: str,
    requested_scope: str,
    *,
    workspace: Path,
) -> bool:
    if allowed_scope.startswith("url:") or requested_scope.startswith("url:"):
        return permission_scope_covers(allowed_scope, requested_scope)
    if not allowed_scope.startswith("path:") or not requested_scope.startswith("path:"):
        return False

    def resolve_in_workspace(scope: str) -> Path | None:
        raw_path = Path(scope.removeprefix("path:"))
        candidate = raw_path if raw_path.is_absolute() else workspace / raw_path
        try:
            resolved = candidate.resolve()
            resolved.relative_to(workspace)
        except (OSError, RuntimeError, ValueError):
            return None
        return resolved

    allowed_path = resolve_in_workspace(allowed_scope)
    requested_path = resolve_in_workspace(requested_scope)
    if allowed_path is None or requested_path is None:
        return False
    return requested_path == allowed_path or allowed_path in requested_path.parents


@dataclass
class PendingPermission:
    id: str
    run_id: str
    tool_name: str
    tool_input: dict[str, Any]
    agent_id: str | None
    scope: str
    destructive: bool
    persistable: bool
    created_at: datetime
    future: asyncio.Future[PermissionResolution]


class PermissionBroker:
    def __init__(
        self,
        ledger: FileLedger,
        events: EventPublisher,
        timeout_seconds: int,
        grants: PermissionGrantStore | None = None,
    ) -> None:
        self.ledger = ledger
        self.events = events
        self.timeout_seconds = timeout_seconds
        self.grants = grants
        self.durable = DurablePermissionStore(ledger, timeout_seconds)
        self._pending: dict[str, PendingPermission] = {}
        self._run_allowed_scopes: dict[str, set[tuple[str, str]]] = {}
        self._lock = asyncio.Lock()

    def list_pending(self) -> list[PendingPermissionView]:
        views: list[PendingPermissionView] = []
        for pending in self._pending.values():
            project_id = None
            try:
                project_id = self.ledger.get_run(pending.run_id).project_id
            except Exception:
                pass
            safe_input = redact(pending.tool_input)
            views.append(
                PendingPermissionView(
                    id=pending.id,
                    run_id=pending.run_id,
                    project_id=project_id,
                    tool_name=pending.tool_name,
                    tool_input=safe_input if isinstance(safe_input, dict) else {},
                    agent_id=pending.agent_id,
                    scope=str(redact(pending.scope)),
                    destructive=pending.destructive,
                    persistable=pending.persistable,
                    timeout_seconds=self.timeout_seconds,
                    created_at=pending.created_at,
                )
            )
        return [*views, *self.durable.list_pending()]

    def automatic_resolution(
        self,
        run_id: str,
        tool_name: str,
        tool_input: dict[str, Any],
        *,
        agent_id: str | None,
        context: dict[str, Any],
    ) -> PermissionResolution | None:
        scope = normalized_permission_scope(tool_input)
        destructive = is_destructive_delete(tool_name, tool_input)
        persistable = is_persistable_permission(tool_name, tool_input)
        try:
            run = self.ledger.get_run(run_id)
        except (LedgerNotFoundError, ValueError):
            run = None
        if run is not None and run.schedule_id is not None:
            try:
                schedule = self.ledger.get_schedule(run.schedule_id)
            except (LedgerNotFoundError, ValueError):
                return PermissionResolution(
                    decision=PermissionDecision.DENY,
                    reason="The originating schedule no longer exists.",
                )
            if destructive:
                return PermissionResolution(
                    decision=PermissionDecision.DENY,
                    reason="Scheduled runs cannot perform destructive actions.",
                )
            if not persistable or not scope:
                return PermissionResolution(
                    decision=PermissionDecision.DENY,
                    reason=(
                        "This action requires per-invocation approval and cannot run unattended."
                    ),
                )
            workspace = self.ledger.workspace_for_project(run.project_id).resolve()
            covered = any(
                rule.tool_name == tool_name
                and _schedule_scope_covers(rule.scope, scope, workspace=workspace)
                for rule in schedule.permission_ceiling
            )
            if not covered:
                return PermissionResolution(
                    decision=PermissionDecision.DENY,
                    reason="This action is outside the schedule permission ceiling.",
                )
            return PermissionResolution(
                decision=PermissionDecision.ALLOW_ONCE,
                reason="Allowed by the user-approved schedule permission ceiling.",
            )

        if self.grants is not None and persistable:
            try:
                project_id = self.ledger.get_run(run_id).project_id
            except Exception:
                project_id = None
            grant = self.grants.match(tool_name, tool_input, project_id=project_id)
            if grant is not None:
                return PermissionResolution(
                    decision=PermissionDecision.ALLOW_PERSISTENT,
                    reason=f"persistent grant {grant.id}",
                    expires_at=grant.expires_at,
                )

        allowed_scopes = self._run_allowed_scopes.get(run_id, set())
        if persistable and any(
            allowed_tool == tool_name and permission_scope_covers(allowed_scope, scope)
            for allowed_tool, allowed_scope in allowed_scopes
        ):
            return PermissionResolution(decision=PermissionDecision.ALLOW_RUN)
        # A Run-scoped approval must survive SDK subprocess replacement too.
        if persistable and run is not None and any(
            wait.state == "consumed" and wait.tool_name == tool_name
            and wait.resolution is not None
            and wait.resolution.decision == PermissionDecision.ALLOW_RUN
            and permission_scope_covers(wait.scope, scope)
            for wait in run.permission_waits
        ):
            return PermissionResolution(decision=PermissionDecision.ALLOW_RUN)
        return None

    async def request(
        self, run_id: str, tool_name: str, tool_input: dict[str, Any], *,
        agent_id: str | None, context: dict[str, Any],
    ) -> PermissionResolution:
        automatic = self.automatic_resolution(
            run_id, tool_name, tool_input, agent_id=agent_id, context=context,
        )
        if automatic is not None:
            return automatic
        scope = normalized_permission_scope(tool_input)
        destructive = is_destructive_delete(tool_name, tool_input)
        persistable = is_persistable_permission(tool_name, tool_input)
        request_id = new_id("permission")
        loop = asyncio.get_running_loop()
        pending = PendingPermission(
            id=request_id,
            run_id=run_id,
            tool_name=tool_name,
            tool_input=tool_input,
            agent_id=agent_id,
            scope=scope,
            destructive=destructive,
            persistable=persistable,
            created_at=utc_now(),
            future=loop.create_future(),
        )
        async with self._lock:
            self._pending[request_id] = pending
        self.ledger.update_run(run_id, status=RunStatus.WAITING_PERMISSION)
        await self.events.publish(
            run_id,
            EventType.RUN_STATUS,
            {"status": RunStatus.WAITING_PERMISSION},
            actor=EventActor(type="system", name="permission-broker"),
        )
        await self.events.publish(
            run_id,
            EventType.PERMISSION_REQUESTED,
            {
                "request_id": request_id,
                "tool_name": tool_name,
                "tool_input": redact(tool_input),
                "context": redact(context),
                "timeout_seconds": self.timeout_seconds,
                "scope": redact(scope),
                "destructive": destructive,
                "persistable": persistable,
            },
            actor=EventActor(type="agent", name="sdk-permission", agent_id=agent_id),
        )
        try:
            resolution = await asyncio.wait_for(
                pending.future,
                timeout=self.timeout_seconds,
            )
        except TimeoutError:
            resolution = PermissionResolution(
                decision=PermissionDecision.DENY,
                reason="Permission request timed out.",
            )
            await self.events.publish(
                run_id,
                EventType.PERMISSION_TIMED_OUT,
                {"request_id": request_id, "tool_name": tool_name},
                actor=EventActor(type="system", name="permission-broker"),
            )
        finally:
            async with self._lock:
                self._pending.pop(request_id, None)
                still_waiting = any(item.run_id == run_id for item in self._pending.values())
            run = self.ledger.get_run(run_id)
            if not still_waiting and run.status == RunStatus.WAITING_PERMISSION:
                self.ledger.update_run(run_id, status=RunStatus.RUNNING)
                await self.events.publish(
                    run_id,
                    EventType.RUN_STATUS,
                    {"status": RunStatus.RUNNING},
                    actor=EventActor(type="system", name="permission-broker"),
                )
        return resolution

    async def resolve(
        self,
        run_id: str,
        request_id: str,
        resolution: PermissionResolution,
        *,
        actor: EventActor | None = None,
    ) -> None:
        async with self._lock:
            pending = self._pending.get(request_id)
            # A second device/late retry must never change the first decision or
            # create a reusable grant after denial, cancellation or timeout.
            if pending is None or pending.run_id != run_id or pending.future.done():
                raise PermissionNotFoundError(request_id)
            if (
                resolution.decision
                in {PermissionDecision.ALLOW_RUN, PermissionDecision.ALLOW_PERSISTENT}
                and not pending.persistable
            ):
                resolution = resolution.model_copy(
                    update={
                        "decision": PermissionDecision.ALLOW_ONCE,
                        "reason": resolution.reason
                        or "This action requires confirmation for every invocation.",
                        "expires_at": None,
                    }
                )
            elif resolution.decision == PermissionDecision.ALLOW_RUN:
                self._run_allowed_scopes.setdefault(run_id, set()).add(
                    (pending.tool_name, pending.scope)
                )
            elif resolution.decision == PermissionDecision.ALLOW_PERSISTENT and self.grants:
                try:
                    project_id = self.ledger.get_run(run_id).project_id
                except Exception:
                    project_id = None
                grant = self.grants.record(
                    tool_name=pending.tool_name,
                    tool_input=pending.tool_input,
                    project_id=project_id,
                )
                resolution = resolution.model_copy(update={"expires_at": grant.expires_at})
            elif resolution.decision == PermissionDecision.ALLOW_PERSISTENT:
                resolution = resolution.model_copy(
                    update={
                        "decision": PermissionDecision.ALLOW_ONCE,
                        "reason": resolution.reason
                        or "Persistent permission storage is unavailable.",
                        "expires_at": None,
                    }
                )
            if not pending.future.done():
                pending.future.set_result(resolution)
        await self.events.publish(
            run_id,
            EventType.PERMISSION_RESOLVED,
            {
                "request_id": request_id,
                "tool_name": pending.tool_name,
                "decision": resolution.decision,
                "reason": resolution.reason,
            },
            actor=actor or EventActor(type="user", name="frontend-user"),
        )

    async def deny_all(self, run_id: str, reason: str) -> None:
        self.durable.invalidate(run_id, cancelled=True)
        resolution = PermissionResolution(
            decision=PermissionDecision.DENY,
            reason=reason,
        )
        async with self._lock:
            matching = [item for item in self._pending.values() if item.run_id == run_id]
            for pending in matching:
                if not pending.future.done():
                    pending.future.set_result(resolution)
