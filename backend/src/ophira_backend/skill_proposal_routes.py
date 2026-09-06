"""Authorized inspection of frozen packages and human-only Skill review."""

from __future__ import annotations

import base64
from collections.abc import Callable
from datetime import datetime
from pathlib import PurePosixPath
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Query, Request, Response
from pydantic import Field

from .auth import AuthenticationError, AuthorizationError, ProjectPermission
from .constants import PrincipalRole
from .models import APIModel, PrincipalContext, SkillSelectionToken
from .skill_proposal_models import (
    SkillProposal,
    SkillProposalManifest,
    SkillProposalReviewRequest,
    SkillProposalState,
)
from .skill_proposals import (
    SkillProposalConflictError,
    SkillProposalIntegrityError,
    SkillProposalNotFoundError,
    SkillProposalQuotaError,
    SkillProposalService,
)
from .skills import SkillIntegrityError, SkillRegistry
from .storage import FileLedger, LedgerNotFoundError

MAX_PREVIEW_BYTES = 200_000


class SkillProposalActions(APIModel):
    can_publish: bool
    can_reject: bool
    reason: str | None


class SkillProposalView(SkillProposal):
    project_name: str
    target: SkillSelectionToken
    actions: SkillProposalActions


class SkillProposalSummary(APIModel):
    id: str
    revision: int
    state: SkillProposalState
    project_id: str
    project_name: str
    source_run_id: str
    source_artifact_id: str
    skill_id: str
    manifest: SkillProposalManifest
    description: str
    content_hash: str
    scan_ok: bool
    created_at: datetime
    updated_at: datetime


class SkillProposalPage(APIModel):
    items: list[SkillProposalSummary]
    next_cursor: str | None


class SkillProposalFilePreview(APIModel):
    proposal_id: str
    content_hash: str
    path: str
    file_content_hash: str
    size_bytes: int
    preview_kind: str
    text: str | None
    complete: bool


class ProposalCursor(APIModel):
    created_at: datetime
    id: str = Field(pattern=r"^skill_proposal_[0-9a-f]{64}$")


def _decode_cursor(value: str | None) -> tuple[datetime, str] | None:
    if value is None:
        return None
    try:
        if len(value) > 512 or not value.isascii():
            raise ValueError
        raw = base64.b64decode(value, altchars=b"-_", validate=True)
        cursor = ProposalCursor.model_validate_json(raw)
        if cursor.created_at.utcoffset() is None:
            raise ValueError
        return cursor.created_at, cursor.id
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail="Invalid Skill proposal cursor") from exc


def _encode_cursor(item: SkillProposal | SkillProposalSummary) -> str:
    return base64.urlsafe_b64encode(ProposalCursor(
        created_at=item.created_at, id=item.id,
    ).model_dump_json().encode()).decode()


def register_skill_proposal_routes(
    application: FastAPI,
    *,
    proposals: SkillProposalService,
    ledger: FileLedger,
    skills: SkillRegistry,
    require_project_authorization: Callable[
        [str, PrincipalContext | None, ProjectPermission], None
    ],
    project_is_visible: Callable[..., bool],
) -> None:
    def visible(item: SkillProposal, principal: PrincipalContext | None, *, write=False) -> bool:
        try:
            return project_is_visible(
                item.project_id, principal,
                ProjectPermission.PROJECT_WRITE if write else ProjectPermission.PROJECT_READ,
            )
        except AuthenticationError:
            return False

    def unavailable(exc: Exception) -> HTTPException:
        if isinstance(exc, SkillProposalNotFoundError | LedgerNotFoundError):
            return HTTPException(status_code=404, detail="Skill proposal not found")
        if isinstance(exc, SkillProposalConflictError | SkillProposalQuotaError):
            return HTTPException(status_code=409, detail=str(exc))
        if isinstance(exc, AuthenticationError):
            return HTTPException(status_code=401, detail="Human authentication has expired")
        if isinstance(exc, AuthorizationError):
            return HTTPException(status_code=403, detail="Skill proposal review is not authorized")
        if isinstance(exc, SkillProposalIntegrityError | SkillIntegrityError | OSError):
            return HTTPException(status_code=503, detail="Skill proposal storage is unavailable")
        return HTTPException(status_code=400, detail="Invalid Skill proposal request")

    storage_errors = (
        SkillProposalNotFoundError, SkillProposalIntegrityError, SkillIntegrityError,
        LedgerNotFoundError, AuthenticationError, AuthorizationError, ValueError, OSError,
    )

    def lookup(proposal_id: str, principal: PrincipalContext | None) -> SkillProposal:
        try:
            item = proposals.peek(proposal_id)
        except storage_errors as exc:
            # Unreadable metadata cannot establish a project authorization scope.
            raise HTTPException(status_code=404, detail="Skill proposal not found") from exc
        if not visible(item, principal):
            raise HTTPException(status_code=404, detail="Skill proposal not found")
        try:
            return proposals.get(proposal_id)
        except storage_errors as exc:
            raise unavailable(exc) from exc

    def actions(item: SkillProposal, principal: PrincipalContext | None) -> SkillProposalActions:
        permitted = (
            (principal is None or principal.role in {PrincipalRole.OWNER, PrincipalRole.ADMIN})
            and visible(item, principal, write=True)
        )
        if not permitted:
            reason = "Only a current project owner or administrator can review this proposal."
        elif item.state != "pending":
            reason = "This proposal is no longer awaiting review."
        elif item.scan_content_hash != item.content_hash or not item.scan.ok:
            reason = "The frozen package must pass its safety scan before publication."
        else:
            reason = None
        return SkillProposalActions(
            can_publish=reason is None,
            can_reject=permitted and item.state == "pending",
            reason=reason,
        )

    def view(item: SkillProposal, principal: PrincipalContext | None) -> SkillProposalView:
        try:
            project = ledger.get_project(item.project_id)
            target = skills.selection_token(item.skill_id)
        except storage_errors as exc:
            raise unavailable(exc) from exc
        if not visible(item, principal):
            raise HTTPException(status_code=404, detail="Skill proposal not found")
        return SkillProposalView(
            **item.model_dump(), project_name=project.name, target=target,
            actions=actions(item, principal),
        )

    @application.get("/api/skill-proposals", response_model=SkillProposalPage)
    async def list_proposals(
        request: Request,
        state: SkillProposalState | None = None,
        limit: int = Query(default=50, ge=1, le=100),
        cursor: str | None = Query(default=None, max_length=512),
    ) -> SkillProposalPage:
        principal = getattr(request.state, "principal", None)
        checkpoint = _decode_cursor(cursor)
        try:
            candidates = proposals.list(recover=False, skip_invalid=True)
        except storage_errors as exc:
            raise unavailable(exc) from exc
        candidates = sorted(
            (item for item in candidates
             if visible(item, principal)
             and (checkpoint is None or (item.created_at, item.id) < checkpoint)),
            key=lambda item: (item.created_at, item.id), reverse=True,
        )
        summaries = []
        for item in candidates:
            if not visible(item, principal):
                continue
            try:
                # Converge only authorized candidates. A recovery may change the
                # state being filtered, so apply the state filter afterwards.
                item = proposals.get(item.id)
                project = ledger.get_project(item.project_id)
            except storage_errors as exc:
                raise unavailable(exc) from exc
            if not visible(item, principal) or (state is not None and item.state != state):
                continue
            summaries.append(SkillProposalSummary(
                **{name: getattr(item, name) for name in SkillProposalSummary.model_fields
                   if name not in {"project_name", "scan_ok"}},
                project_name=project.name, scan_ok=item.scan.ok
                and item.scan_content_hash == item.content_hash,
            ))
            if len(summaries) > limit:
                break
        return SkillProposalPage(
            items=summaries[:limit],
            next_cursor=_encode_cursor(summaries[limit - 1]) if len(summaries) > limit else None,
        )

    @application.get("/api/skill-proposals/{proposal_id}", response_model=SkillProposalView)
    async def get_proposal(request: Request, proposal_id: str) -> SkillProposalView:
        principal = getattr(request.state, "principal", None)
        return view(lookup(proposal_id, principal), principal)

    def read_file(proposal_id: str, path: str, content_hash: str, principal):
        item = lookup(proposal_id, principal)
        try:
            metadata, payload = proposals.read_file(proposal_id, path, content_hash=content_hash)
        except storage_errors as exc:
            raise unavailable(exc) from exc
        if not visible(item, principal):
            raise HTTPException(status_code=404, detail="Skill proposal not found")
        return metadata, payload

    @application.get(
        "/api/skill-proposals/{proposal_id}/files", response_model=SkillProposalFilePreview,
    )
    async def preview_file(
        request: Request, proposal_id: str,
        path: str = Query(min_length=1, max_length=1_000),
        content_hash: str = Query(pattern=r"^[0-9a-f]{64}$"),
    ) -> SkillProposalFilePreview:
        metadata, payload = read_file(
            proposal_id, path, content_hash, getattr(request.state, "principal", None),
        )
        text = None
        if metadata.preview_kind == "text":
            text = payload[:MAX_PREVIEW_BYTES].decode("utf-8", errors="ignore")
        return SkillProposalFilePreview(
            proposal_id=proposal_id, content_hash=content_hash, path=metadata.path,
            file_content_hash=metadata.content_hash, size_bytes=metadata.size_bytes,
            preview_kind=metadata.preview_kind, text=text,
            complete=metadata.preview_kind == "text" and len(payload) <= MAX_PREVIEW_BYTES,
        )

    @application.get("/api/skill-proposals/{proposal_id}/download")
    async def download_file(
        request: Request, proposal_id: str,
        path: str = Query(min_length=1, max_length=1_000),
        content_hash: str = Query(pattern=r"^[0-9a-f]{64}$"),
    ) -> Response:
        metadata, payload = read_file(
            proposal_id, path, content_hash, getattr(request.state, "principal", None),
        )
        filename = quote(PurePosixPath(metadata.path).name)
        return Response(payload, media_type="application/octet-stream", headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{filename}",
            "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff",
        })

    @application.post("/api/skill-proposals/{proposal_id}/review", response_model=SkillProposalView)
    async def review_proposal(
        request: Request, proposal_id: str, payload: SkillProposalReviewRequest,
    ) -> SkillProposalView:
        principal = getattr(request.state, "principal", None)
        item = lookup(proposal_id, principal)
        if (principal is not None
                and principal.role not in {PrincipalRole.OWNER, PrincipalRole.ADMIN}):
            raise HTTPException(
                status_code=403, detail="Skill review requires a project administrator",
            )
        try:
            require_project_authorization(
                item.project_id, principal, ProjectPermission.PROJECT_WRITE,
            )
            reviewed = proposals.review(proposal_id, payload, principal)
        except storage_errors as exc:
            raise unavailable(exc) from exc
        return view(reviewed, principal)
