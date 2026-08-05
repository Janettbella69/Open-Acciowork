from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from .constants import EventType, PermissionDecision, RunStatus
from .events import EventPublisher, redact
from .models import EventActor, PermissionResolution, new_id
from .storage import FileLedger


class PermissionNotFoundError(KeyError):
    pass


@dataclass
class PendingPermission:
    id: str
    run_id: str
    tool_name: str
    tool_input: dict[str, Any]
    agent_id: str | None
    future: asyncio.Future[PermissionResolution]


class PermissionBroker:
    def __init__(
        self,
        ledger: FileLedger,
        events: EventPublisher,
        timeout_seconds: int,
    ) -> None:
        self.ledger = ledger
        self.events = events
        self.timeout_seconds = timeout_seconds
        self._pending: dict[str, PendingPermission] = {}
        self._run_allowed_tools: dict[str, set[str]] = {}
        self._lock = asyncio.Lock()

    async def request(
        self,
        run_id: str,
        tool_name: str,
        tool_input: dict[str, Any],
        *,
        agent_id: str | None,
        context: dict[str, Any],
    ) -> PermissionResolution:
        if tool_name in self._run_allowed_tools.get(run_id, set()):
            return PermissionResolution(decision=PermissionDecision.ALLOW_RUN)

        request_id = new_id("permission")
        loop = asyncio.get_running_loop()
        pending = PendingPermission(
            id=request_id,
            run_id=run_id,
            tool_name=tool_name,
            tool_input=tool_input,
            agent_id=agent_id,
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
    ) -> None:
        async with self._lock:
            pending = self._pending.get(request_id)
            if pending is None or pending.run_id != run_id:
                raise PermissionNotFoundError(request_id)
            if resolution.decision == PermissionDecision.ALLOW_RUN:
                self._run_allowed_tools.setdefault(run_id, set()).add(pending.tool_name)
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
            actor=EventActor(type="user", name="frontend-user"),
        )

    async def deny_all(self, run_id: str, reason: str) -> None:
        resolution = PermissionResolution(
            decision=PermissionDecision.DENY,
            reason=reason,
        )
        async with self._lock:
            matching = [item for item in self._pending.values() if item.run_id == run_id]
            for pending in matching:
                if not pending.future.done():
                    pending.future.set_result(resolution)
