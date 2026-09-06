"""Human inspection/retirement of task experience, never per-note approval."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, HTTPException, Request

from .auth import ProjectPermission
from .models import PrincipalContext, Schedule, TaskExperienceRetire
from .storage import FileLedger, LedgerNotFoundError
from .task_experience import TaskExperienceConflict, TaskExperienceStore


def register_task_experience_routes(
    application: FastAPI,
    *,
    experiences: TaskExperienceStore,
    ledger: FileLedger,
    require_project_authorization: Callable[
        [str, PrincipalContext | None, ProjectPermission], None
    ],
    project_is_visible: Callable[..., bool],
) -> None:
    def lookup(schedule_id: str, request: Request) -> Schedule:
        try:
            schedule = ledger.get_schedule(schedule_id)
        except (LedgerNotFoundError, ValueError) as exc:
            raise HTTPException(404, "Schedule not found") from exc
        if not project_is_visible(schedule.project_id, getattr(request.state, "principal", None)):
            raise HTTPException(404, "Schedule not found")
        return schedule

    @application.get("/api/schedules/{schedule_id}/experiences")
    async def list_task_experience(schedule_id: str, request: Request) -> dict[str, Any]:
        schedule = lookup(schedule_id, request)
        require_project_authorization(
            schedule.project_id, getattr(request.state, "principal", None),
            ProjectPermission.PROJECT_READ,
        )
        return experiences.list(schedule.id)

    @application.post("/api/schedules/{schedule_id}/experiences/{entry_id}/retire")
    async def retire_task_experience(
        schedule_id: str, entry_id: str, request: Request, payload: TaskExperienceRetire,
    ) -> dict[str, Any]:
        schedule = lookup(schedule_id, request)
        principal = getattr(request.state, "principal", None)
        require_project_authorization(
            schedule.project_id, principal, ProjectPermission.PROJECT_WRITE,
        )
        actor = (
            f"{principal.tenant_id}/{principal.principal_id}" if principal else "local_owner"
        )
        try:
            return experiences.retire(schedule.id, entry_id, payload, actor=actor)
        except KeyError as exc:
            raise HTTPException(404, "Experience not found") from exc
        except TaskExperienceConflict as exc:
            raise HTTPException(409, str(exc)) from exc
