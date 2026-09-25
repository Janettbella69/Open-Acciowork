"""Gmail OAuth and project-trigger watch control plane routes."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse

from .auth import AuthenticationError, AuthorizationError, ProjectPermission
from .constants import PrincipalRole
from .event_triggers import EventTrigger, EventTriggerStore
from .gmail import (
    GMAIL_CALLBACK_PATH,
    GMAIL_PUSH_PATH,
    MAX_PUSH_BODY,
    GmailError,
    GmailService,
)
from .gmail_access import GmailAccessService, GmailGrantRequest, GmailRevokeRequest
from .models import PrincipalContext
from .storage import LedgerNotFoundError


def register_gmail_routes(
    application: FastAPI,
    *,
    service: GmailService,
    access: GmailAccessService,
    triggers: EventTriggerStore,
    require_project_authorization: Callable[
        [str, PrincipalContext | None, ProjectPermission], None
    ],
    read_bounded_body: Callable[..., Awaitable[bytes]],
    dispatch: Callable[[EventTrigger, str, bytes, dict[str, str]], Awaitable[dict[str, object]]],
    authorize_trigger: Callable[[EventTrigger], None],
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

    def gmail_trigger(project_id: str, trigger_id: str) -> EventTrigger:
        try:
            trigger = triggers.get(project_id, trigger_id)
        except (KeyError, ValueError):
            raise HTTPException(404, "Gmail trigger not found") from None
        if trigger.kind != "inquiry_email" or trigger.gmail_connector_id is None:
            raise HTTPException(404, "Gmail trigger not found")
        return trigger

    @application.exception_handler(GmailError)
    async def gmail_failure(_request: Request, exc: GmailError):
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": f"{exc.code}: {exc}"},
            headers={"Cache-Control": "no-store"},
        )

    @application.get("/api/connectors/{connector_id}/gmail-oauth")
    async def gmail_oauth_status(connector_id: str, request: Request, response: Response):
        admin(request)
        response.headers["Cache-Control"] = "no-store"
        return service.oauth_status(connector_id, tenant(request))

    @application.get("/api/projects/{project_id}/gmail-access")
    async def gmail_access_status(project_id: str, request: Request, response: Response):
        require_project_authorization(
            project_id, principal(request), ProjectPermission.PROJECT_READ,
        )
        try:
            result = access.view(project_id)
        except (AuthenticationError, AuthorizationError, LedgerNotFoundError):
            raise HTTPException(403, "Project Gmail access is unavailable") from None
        require_project_authorization(
            project_id, principal(request), ProjectPermission.PROJECT_READ,
        )
        response.headers["Cache-Control"] = "no-store"
        return result

    @application.post("/api/projects/{project_id}/gmail-access")
    async def grant_gmail_access(
        project_id: str, payload: GmailGrantRequest, request: Request, response: Response,
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

    @application.post("/api/projects/{project_id}/gmail-access/{grant_id}/revoke")
    async def revoke_gmail_access(
        project_id: str, grant_id: str, payload: GmailRevokeRequest,
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

    @application.post("/api/connectors/{connector_id}/gmail-oauth/start")
    async def gmail_oauth_start(connector_id: str, request: Request, response: Response):
        admin(request)
        result = service.begin_oauth(connector_id, tenant(request))
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return result

    @application.get(GMAIL_CALLBACK_PATH)
    async def gmail_oauth_callback(connector_id: str, request: Request):
        await service.complete_oauth(connector_id, request.scope.get("query_string", b""))
        return RedirectResponse(
            "/app/plugins/connectors?gmail=connected",
            status_code=303,
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )

    @application.get(
        "/api/projects/{project_id}/event-triggers/{trigger_id}/gmail-watch",
    )
    async def gmail_watch_status(
        project_id: str,
        trigger_id: str,
        request: Request,
        response: Response,
    ):
        require_project_authorization(
            project_id,
            principal(request),
            ProjectPermission.PROJECT_READ,
        )
        trigger = gmail_trigger(project_id, trigger_id)
        response.headers["Cache-Control"] = "no-store"
        return service.watch_status(trigger.gmail_connector_id, tenant(request), trigger)

    @application.post(
        "/api/projects/{project_id}/event-triggers/{trigger_id}/gmail-watch",
    )
    async def gmail_watch_register(
        project_id: str,
        trigger_id: str,
        request: Request,
        reset_stale: bool = False,
    ):
        require_project_authorization(
            project_id,
            principal(request),
            ProjectPermission.PROJECT_ACCESS_MANAGE,
        )
        if reset_stale:
            admin(request)
        with service.ledger.lifecycle.lease(project_id):
            trigger = gmail_trigger(project_id, trigger_id)
            if not trigger.enabled:
                raise HTTPException(409, "Gmail trigger is paused")
            authorize_trigger(trigger)
            return await service.register_watch(
                trigger.gmail_connector_id,
                tenant(request),
                reset_stale=reset_stale,
                trigger=trigger,
            )

    @application.delete(
        "/api/projects/{project_id}/event-triggers/{trigger_id}/gmail-watch",
    )
    async def gmail_watch_stop(project_id: str, trigger_id: str, request: Request):
        require_project_authorization(
            project_id,
            principal(request),
            ProjectPermission.PROJECT_ACCESS_MANAGE,
        )
        admin(request)
        with service.ledger.lifecycle.lease(project_id):
            trigger = gmail_trigger(project_id, trigger_id)
            return await service.stop_watch(
                trigger.gmail_connector_id, tenant(request), trigger,
            )

    @application.post(GMAIL_PUSH_PATH)
    async def gmail_push(connector_id: str, request: Request):
        await service.verify_push(connector_id, request.headers.get("authorization", ""))
        body = await read_bounded_body(
            request,
            max_bytes=MAX_PUSH_BODY,
            too_large_detail="Gmail push body too large",
        )
        return await service.process_push(connector_id, body, dispatch)
