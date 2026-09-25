"""Outlook OAuth and project mailbox grant control plane routes."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse

from .auth import AuthenticationError, AuthorizationError, ProjectPermission
from .constants import PrincipalRole
from .models import PrincipalContext
from .outlook import OUTLOOK_CALLBACK_PATH, OutlookError, OutlookService
from .outlook_access import OutlookAccessService, OutlookGrantRequest, OutlookRevokeRequest
from .storage import LedgerNotFoundError


def register_outlook_routes(
    application: FastAPI, *, service: OutlookService, access: OutlookAccessService,
    require_project_authorization: Callable[
        [str, PrincipalContext | None, ProjectPermission], None
    ],
) -> None:
    def principal(request: Request) -> PrincipalContext | None:
        return getattr(request.state, "principal", None)

    def tenant(request: Request) -> str | None:
        user = principal(request)
        return user.tenant_id if user else None

    def admin(request: Request) -> None:
        user = principal(request)
        if user is not None and user.role not in {PrincipalRole.OWNER, PrincipalRole.ADMIN}:
            raise HTTPException(403, "Tenant administrator required")

    @application.exception_handler(OutlookError)
    async def outlook_failure(_request: Request, exc: OutlookError):
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": f"{exc.code}: {exc}"},
            headers={"Cache-Control": "no-store"},
        )

    @application.get("/api/connectors/{connector_id}/outlook-oauth")
    async def outlook_oauth_status(connector_id: str, request: Request, response: Response):
        admin(request)
        response.headers["Cache-Control"] = "no-store"
        return service.oauth_status(connector_id, tenant(request))

    @application.post("/api/connectors/{connector_id}/outlook-oauth/start")
    async def outlook_oauth_start(connector_id: str, request: Request, response: Response):
        admin(request)
        result = service.begin_oauth(connector_id, tenant(request))
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return result

    @application.get(OUTLOOK_CALLBACK_PATH)
    async def outlook_oauth_callback(connector_id: str, request: Request):
        await service.complete_oauth(connector_id, request.scope.get("query_string", b""))
        return RedirectResponse(
            "/app/plugins/connectors?outlook=connected", status_code=303,
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )

    @application.get("/api/projects/{project_id}/outlook-access")
    async def outlook_access_status(project_id: str, request: Request, response: Response):
        require_project_authorization(
            project_id, principal(request), ProjectPermission.PROJECT_READ,
        )
        try:
            result = access.view(project_id)
        except (AuthenticationError, AuthorizationError, LedgerNotFoundError):
            raise HTTPException(403, "Project Outlook access is unavailable") from None
        require_project_authorization(
            project_id, principal(request), ProjectPermission.PROJECT_READ,
        )
        response.headers["Cache-Control"] = "no-store"
        return result

    @application.post("/api/projects/{project_id}/outlook-access")
    async def grant_outlook_access(
        project_id: str, payload: OutlookGrantRequest, request: Request, response: Response,
    ):
        admin(request)
        require_project_authorization(
            project_id, principal(request), ProjectPermission.PROJECT_ACCESS_MANAGE,
        )
        try:
            result = access.grant(project_id, payload, principal(request))
        except (AuthenticationError, AuthorizationError, LedgerNotFoundError):
            raise HTTPException(403, "Project administrator access is unavailable") from None
        require_project_authorization(
            project_id, principal(request), ProjectPermission.PROJECT_ACCESS_MANAGE,
        )
        response.headers["Cache-Control"] = "no-store"
        return result

    @application.post("/api/projects/{project_id}/outlook-access/{grant_id}/revoke")
    async def revoke_outlook_access(
        project_id: str, grant_id: str, payload: OutlookRevokeRequest,
        request: Request, response: Response,
    ):
        admin(request)
        require_project_authorization(
            project_id, principal(request), ProjectPermission.PROJECT_ACCESS_MANAGE,
        )
        try:
            result = access.revoke(
                project_id, grant_id, payload.expected_revision, principal(request),
            )
        except (AuthenticationError, AuthorizationError, LedgerNotFoundError):
            raise HTTPException(403, "Project administrator access is unavailable") from None
        require_project_authorization(
            project_id, principal(request), ProjectPermission.PROJECT_ACCESS_MANAGE,
        )
        response.headers["Cache-Control"] = "no-store"
        return result
