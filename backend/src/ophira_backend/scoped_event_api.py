"""Authorized replay and live invalidation outside the Run event sequence."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Annotated

from fastapi import FastAPI, Header, HTTPException, Query, Request, Response
from sse_starlette.sse import EventSourceResponse
from starlette.concurrency import run_in_threadpool

from .auth import AuthenticationError, ProjectPermission
from .constants import PrincipalRole
from .models import PrincipalContext, utc_now
from .preflight_events import PreflightEventService
from .project_events import ProjectEventProjector
from .project_lifecycle import ProjectLifecycleBusyError, ProjectUnavailableError
from .providers import ProviderNotFoundError, ProviderRegistry
from .scoped_events import ScopedEventCursorError, ScopedEventStore
from .storage import LedgerNotFoundError


def install_scoped_event_routes(
    app: FastAPI, projector: ProjectEventProjector, providers: ProviderRegistry,
    preflights: PreflightEventService,
    principal_from_request: Callable[[Request], PrincipalContext | None],
    authorize_project: Callable[[str, PrincipalContext | None, ProjectPermission], None],
) -> None:
    store: ScopedEventStore = projector.scoped

    def scope(request: Request, identifier: str, *, provider: bool):
        principal = principal_from_request(request)

        def authorize():
            if provider:
                if principal is not None and (
                    principal.role not in {PrincipalRole.OWNER, PrincipalRole.ADMIN}
                    or utc_now() >= principal.assertion_expires_at
                ):
                    raise HTTPException(403, "Provider history requires administrator access")
                try:
                    providers.get(identifier)
                except (ProviderNotFoundError, ValueError) as exc:
                    raise HTTPException(404, "Provider not found") from exc
            else:
                authorize_project(identifier, principal, ProjectPermission.PROJECT_READ)

        authorize()
        if provider:
            tenant = principal.tenant_id if principal else None
            stream = store.provider_stream(identifier, tenant)
            def reconcile():
                preflights.reconcile(identifier, tenant)
        else:
            stream = store.project_stream(identifier)
            def reconcile():
                projector.reconcile_project(identifier)
        return stream, authorize, reconcile

    def history(request: Request, identifier: str, after: int, limit: int, tail: bool, *, provider):
        stream, authorize, reconcile = scope(request, identifier, provider=provider)
        if tail and after:
            raise HTTPException(400, "tail and after cannot be combined")
        try:
            reconcile()
            last = store.last_sequence(stream)
            if after > last:
                raise HTTPException(400, "Cursor is ahead of the event log")
            page = store.read(stream, max(0, last - limit) if tail else after, limit)
            # A writer may append after last_sequence. Include such records only
            # in a forward page; the tail snapshot ends at its declared watermark.
            if tail:
                page = [event for event in page if event.sequence <= last]
            elif page:
                last = max(last, page[-1].sequence)
            authorize()
            return {"stream_id": stream, "events": page, "last_sequence": last}
        except (
            ValueError, OSError, LedgerNotFoundError,
            ProjectUnavailableError, ProjectLifecycleBusyError,
        ) as exc:
            raise HTTPException(503, "Event history could not be verified") from exc

    def live(request: Request, identifier: str, after: int, cursor: str | None, *, provider):
        stream, authorize, reconcile = scope(request, identifier, provider=provider)
        try:
            reconcile()
            if cursor is not None:
                after = store.parse_cursor(stream, cursor)
            elif after > store.last_sequence(stream):
                raise ScopedEventCursorError("cursor ahead of stream")
        except ScopedEventCursorError as exc:
            raise HTTPException(400, "Invalid event cursor") from exc
        except (
            ValueError, OSError, LedgerNotFoundError,
            ProjectUnavailableError, ProjectLifecycleBusyError,
        ) as exc:
            raise HTTPException(503, "Event history unavailable") from exc

        async def generate():
            position = after
            while not await request.is_disconnected():
                try:
                    authorize()
                    await run_in_threadpool(reconcile)
                    page = await run_in_threadpool(store.read, stream, position)
                    for event in page:
                        # Authorization changes, deletion and expiring gateway
                        # assertions must stop an already-open connection too.
                        authorize()
                        if await request.is_disconnected():
                            return
                        yield {"event": event.type, "id": event.id,
                               "data": json.dumps(event.model_dump(mode="json"))}
                        position = event.sequence
                    if len(page) == 200:
                        await asyncio.sleep(0)
                        continue
                    authorize()
                    yield {"comment": "keepalive"}
                except (
                    HTTPException, AuthenticationError, ValueError, OSError, LedgerNotFoundError,
                    ProjectUnavailableError, ProjectLifecycleBusyError,
                ):
                    return  # Do not acknowledge a damaged or unauthorized tail.
                await asyncio.sleep(1)

        return EventSourceResponse(generate(), ping=None, headers={
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
        })

    @app.get("/api/projects/{project_id}/events/history")
    def project_history(
        request: Request, response: Response, project_id: str, after: int = Query(0, ge=0),
        limit: int = Query(200, ge=1, le=200), tail: bool = False,
    ):
        response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
        return history(request, project_id, after, limit, tail, provider=False)

    @app.get("/api/providers/{provider_id}/events/history")
    def provider_history(
        request: Request, response: Response, provider_id: str, after: int = Query(0, ge=0),
        limit: int = Query(200, ge=1, le=200), tail: bool = False,
    ):
        response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
        return history(request, provider_id, after, limit, tail, provider=True)

    @app.get("/api/projects/{project_id}/events")
    def project_stream(
        request: Request, project_id: str, after: int = Query(0, ge=0),
        last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    ):
        return live(request, project_id, after, last_event_id, provider=False)

    @app.get("/api/providers/{provider_id}/events")
    def provider_stream(
        request: Request, provider_id: str, after: int = Query(0, ge=0),
        last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    ):
        return live(request, provider_id, after, last_event_id, provider=True)
