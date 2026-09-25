"""Project controls and public Microsoft Graph notification ingress."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse

from .auth import ProjectPermission
from .constants import PrincipalRole
from .event_triggers import EventTrigger, EventTriggerStore
from .models import PrincipalContext
from .outlook_subscriptions import (
    MAX_NOTIFICATION_BODY,
    OUTLOOK_NOTIFICATIONS_PATH,
    OutlookSubscriptions,
)


def register_outlook_subscription_routes(
    application: FastAPI, *, subscriptions: OutlookSubscriptions,
    triggers: EventTriggerStore,
    require_project_authorization: Callable[
        [str, PrincipalContext | None, ProjectPermission], None
    ],
    authorize_trigger: Callable[[EventTrigger], None],
    read_bounded_body: Callable[..., Awaitable[bytes]],
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

    def trigger_for(project_id: str, trigger_id: str) -> EventTrigger:
        try:
            trigger = triggers.get(project_id, trigger_id)
        except (KeyError, ValueError):
            raise HTTPException(404, "Outlook trigger not found") from None
        if trigger.kind != "inquiry_email" or trigger.outlook_connector_id is None:
            raise HTTPException(404, "Outlook trigger not found")
        return trigger

    route = "/api/projects/{project_id}/event-triggers/{trigger_id}/outlook-subscription"

    @application.get(route)
    async def outlook_subscription_status(
        project_id: str, trigger_id: str, request: Request, response: Response,
    ):
        require_project_authorization(
            project_id, principal(request), ProjectPermission.PROJECT_READ,
        )
        trigger = trigger_for(project_id, trigger_id)
        response.headers["Cache-Control"] = "no-store"
        return subscriptions.status(trigger.outlook_connector_id, tenant(request), trigger)

    @application.post(route)
    async def outlook_subscription_register(
        project_id: str, trigger_id: str, request: Request,
    ):
        admin(request)
        require_project_authorization(
            project_id, principal(request), ProjectPermission.PROJECT_ACCESS_MANAGE,
        )
        with subscriptions.ledger.lifecycle.lease(project_id):
            trigger = trigger_for(project_id, trigger_id)
            if not trigger.enabled:
                raise HTTPException(409, "Outlook trigger is paused")
            authorize_trigger(trigger)
            return await subscriptions.register(
                trigger.outlook_connector_id, tenant(request), trigger,
            )

    @application.delete(route)
    async def outlook_subscription_stop(
        project_id: str, trigger_id: str, request: Request,
    ):
        admin(request)
        require_project_authorization(
            project_id, principal(request), ProjectPermission.PROJECT_ACCESS_MANAGE,
        )
        with subscriptions.ledger.lifecycle.lease(project_id):
            trigger = trigger_for(project_id, trigger_id)
            return await subscriptions.stop(
                trigger.outlook_connector_id, tenant(request), trigger,
            )

    @application.post(route + "/reconcile")
    async def outlook_subscription_reconcile(
        project_id: str, trigger_id: str, request: Request,
    ):
        admin(request)
        require_project_authorization(
            project_id, principal(request), ProjectPermission.PROJECT_ACCESS_MANAGE,
        )
        with subscriptions.ledger.lifecycle.lease(project_id):
            trigger = trigger_for(project_id, trigger_id)
            authorize_trigger(trigger)
            return await subscriptions.reconcile(
                trigger.outlook_connector_id, tenant(request), trigger,
            )

    @application.post(OUTLOOK_NOTIFICATIONS_PATH)
    async def outlook_notifications(connector_id: str, request: Request):
        tokens = request.query_params.getlist("validationToken")
        if tokens:
            if len(tokens) != 1 or not 1 <= len(tokens[0]) <= 1024:
                raise HTTPException(400, "Invalid Graph validation token")
            if subscriptions._watch(connector_id) is None:
                raise HTTPException(404, "Outlook subscription not found")
            return PlainTextResponse(
                tokens[0], status_code=200,
                headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
            )
        body = await read_bounded_body(
            request, max_bytes=MAX_NOTIFICATION_BODY,
            too_large_detail="Outlook notification body too large",
        )
        result = subscriptions.receive(connector_id, body)
        return Response(status_code=202, headers={
            "Cache-Control": "no-store",
            "X-Outlook-Notifications-Accepted": str(result["accepted"]),
        })
