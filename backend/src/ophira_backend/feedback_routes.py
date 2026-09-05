"""Human feedback and review API. This module does not import evaluation code."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import FastAPI, HTTPException, Query, Request

from .auth import ProjectPermission
from .constants import PrincipalRole
from .feedback import FeedbackConflictError, FeedbackNotFoundError, FeedbackStore
from .models import FeedbackCreate, FeedbackItem, FeedbackReview, FeedbackStatus, PrincipalContext
from .skills import SkillRegistry
from .storage import FileLedger, LedgerNotFoundError


def register_feedback_routes(
    application: FastAPI,
    *,
    feedback: FeedbackStore,
    ledger: FileLedger,
    skills: SkillRegistry,
    require_project_authorization: Callable[
        [str, PrincipalContext | None, ProjectPermission], None
    ],
    project_is_visible: Callable[..., bool],
) -> None:
    def visible(item: FeedbackItem, principal: PrincipalContext | None) -> bool:
        if item.project_id:
            return project_is_visible(item.project_id, principal)
        # Historical unanchored items remain inspectable, but cannot be reviewed
        # into a supposedly traceable result or exported as an anchored candidate.
        if principal is None:
            return item.identity_binding == "local_only"
        return (
            item.identity_binding == "authenticated_principal"
            and item.tenant_id == principal.tenant_id
            and (
                item.principal_id == principal.principal_id
                or principal.role in {PrincipalRole.OWNER, PrincipalRole.ADMIN}
            )
        )

    def lookup(feedback_id: str, principal: PrincipalContext | None) -> FeedbackItem:
        try:
            item = feedback.get(feedback_id)
        except (FeedbackNotFoundError, ValueError, OSError) as exc:
            raise HTTPException(status_code=404, detail="Feedback not found") from exc
        if not visible(item, principal):
            raise HTTPException(status_code=404, detail="Feedback not found")
        return item

    @application.post("/api/feedback", response_model=FeedbackItem, status_code=201)
    async def create_feedback(request: Request, payload: FeedbackCreate) -> FeedbackItem:
        principal = getattr(request.state, "principal", None)
        try:
            project_id = payload.project_id
            if payload.run_id:
                run = ledger.get_run(payload.run_id)
                if project_id is not None and project_id != run.project_id:
                    raise ValueError("Run is outside feedback project")
                project_id = run.project_id
            if project_id is None:
                raise ValueError("feedback requires a project/run anchor")
            require_project_authorization(project_id, principal, ProjectPermission.PROJECT_WRITE)
            return feedback.add(payload, ledger=ledger, principal=principal)
        except (LedgerNotFoundError, FeedbackNotFoundError) as exc:
            raise HTTPException(status_code=404, detail="Feedback anchor not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @application.get("/api/feedback", response_model=list[FeedbackItem])
    async def list_feedback(
        request: Request,
        project_id: str | None = None,
        status: FeedbackStatus | None = None,
        limit: int = Query(default=200, ge=1, le=1_000),
        offset: int = Query(default=0, ge=0),
    ) -> list[FeedbackItem]:
        principal = getattr(request.state, "principal", None)
        # Authorization/filtering precede pagination: another project's reports
        # cannot hide this user's reports by occupying the first page.
        items = [
            item
            for item in feedback.list(limit=None)
            if (project_id is None or item.project_id == project_id)
            and (status is None or item.status == status)
            and visible(item, principal)
        ]
        return items[offset : offset + limit]

    @application.get("/api/feedback/{feedback_id}", response_model=FeedbackItem)
    async def get_feedback(request: Request, feedback_id: str) -> FeedbackItem:
        return lookup(feedback_id, getattr(request.state, "principal", None))

    @application.post("/api/feedback/{feedback_id}/review", response_model=FeedbackItem)
    async def review_feedback(
        request: Request,
        feedback_id: str,
        payload: FeedbackReview,
    ) -> FeedbackItem:
        principal = getattr(request.state, "principal", None)
        item = lookup(feedback_id, principal)
        if principal is not None and principal.role not in {
            PrincipalRole.OWNER,
            PrincipalRole.ADMIN,
        }:
            raise HTTPException(
                status_code=403, detail="Feedback review requires a tenant administrator"
            )
        if item.project_id:
            require_project_authorization(
                item.project_id, principal, ProjectPermission.PROJECT_WRITE
            )
        if payload.status == "folded_into_skill":
            try:
                versions = skills.versions(payload.skill_id or "")
            except (KeyError, ValueError, RuntimeError) as exc:
                raise HTTPException(
                    status_code=400, detail="Referenced Skill version is unavailable"
                ) from exc
            if not any(version.content_hash == payload.skill_content_hash for version in versions):
                raise HTTPException(
                    status_code=400, detail="Referenced Skill content hash is unavailable"
                )
        try:
            return feedback.review(feedback_id, payload, principal=principal)
        except FeedbackConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
