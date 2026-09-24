"""Small remote-control surface; a device cookie never authenticates other APIs."""

from __future__ import annotations

import asyncio
import json
from typing import TypeVar
from urllib.parse import urlencode

from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import Field, ValidationError

from .auth import ProjectAccessStore, ProjectPermission
from .automation import NotificationStore
from .companion import (
    COMPANION_COOKIE,
    COMPANION_HEADER,
    DEVICE_MAX_AGE_SECONDS,
    PAIRING_TTL_SECONDS,
    CompanionError,
    CompanionStore,
    PairingClaim,
    PairingCreate,
    PairingDecision,
    PairingRecord,
    PairingStatus,
    PairingView,
)
from .companion_authorization import authorize_companion_project
from .config import Settings, companion_origin
from .constants import EventType, PermissionDecision, RunStatus
from .durable_permissions import DurablePermissionConflict
from .models import (
    APIModel,
    Artifact,
    EventActor,
    EventEnvelope,
    PermissionResolution,
    RunRecord,
)
from .permissions import PermissionBroker, PermissionNotFoundError
from .policy import PolicyDeniedError
from .providers import ProviderConfigurationError, ProviderNotFoundError
from .push_service import PushDeliveryService
from .push_transport import PushSubscription
from .runs import RunCancelConflictError, RunManager
from .storage import FileLedger, LedgerNotFoundError

Payload = TypeVar("Payload", bound=APIModel)


class CompanionRunCreate(APIModel):
    prompt: str = Field(min_length=1, max_length=40_000)


class CompanionActivityRequest(APIModel):
    after_sequence: int = Field(default=0, ge=0)
    wait_for_update: bool = False


class CompanionActivityPage(APIModel):
    events: list[EventEnvelope]
    artifacts: list[Artifact]
    next_sequence: int
    has_more: bool
    status: RunStatus


COMPANION_ACTIVITY_WAIT_SECONDS = 20


def _companion_activity_event(event: EventEnvelope) -> bool:
    return (
        event.type.startswith("task.")
        or event.type.startswith("run.")
        or event.type in {
            EventType.ARTIFACT_REGISTERED,
            EventType.ARTIFACT_UPDATED,
            EventType.TOOL_STARTED,
            EventType.TOOL_FAILED,
        }
        or (event.type == EventType.TOOL_COMPLETED
            and event.data.get("tool_name") in {
                "TodoWrite", "TaskCreate", "TaskUpdate", "TaskGet", "TaskList",
            })
    )


def validate_companion_request(settings: Settings, request: Request) -> None:
    if not settings.companion_enabled or not settings.companion_public_origin:
        raise HTTPException(status_code=503, detail="Secure companion access is not configured")
    headers = request.headers
    if any(len(headers.getlist(key)) > 1 for key in ("host", "origin", COMPANION_HEADER)):
        raise HTTPException(status_code=403, detail="Ambiguous companion request")
    try:
        origin = companion_origin(f"{request.url.scheme}://{headers.get('host', '')}")
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="Companion access requires HTTPS") from exc
    if origin != settings.companion_public_origin or request.scope.get("query_string"):
        raise HTTPException(status_code=403, detail="Companion request origin is not trusted")
    if headers.get(COMPANION_HEADER) != "1":
        raise HTTPException(status_code=403, detail="Companion request header is required")
    supplied_origin = headers.get("origin")
    if supplied_origin is not None and supplied_origin != settings.companion_public_origin:
        raise HTTPException(status_code=403, detail="Companion request origin is not trusted")
    if headers.get("sec-fetch-site") not in {None, "same-origin", "none"}:
        raise HTTPException(status_code=403, detail="Cross-site companion access is forbidden")
    if request.method not in {"GET", "HEAD"}:
        if (
            supplied_origin != settings.companion_public_origin
            or headers.get("content-type", "").split(";", 1)[0] != "application/json"
        ):
            raise HTTPException(status_code=403, detail="Same-origin JSON request required")
    cookies = ";".join(headers.getlist("cookie"))
    if sum(part.strip().startswith(f"{COMPANION_COOKIE}=")
           for part in cookies.split(";")) > 1:
        raise HTTPException(status_code=403, detail="Ambiguous companion credential")


async def _payload(request: Request, model: type[Payload]) -> Payload:
    # Never echo a pairing secret in FastAPI's default validation-error input.
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > 65_536:
            raise HTTPException(status_code=413, detail="Companion request is too large")
        body.extend(chunk)
    try:
        return model.model_validate(json.loads(body))
    except (ValueError, ValidationError) as exc:
        raise HTTPException(status_code=422, detail="Invalid companion request") from exc


def register_companion_routes(
    application: FastAPI, *, settings: Settings, store: CompanionStore,
    ledger: FileLedger, runs: RunManager, permissions: PermissionBroker,
    notifications: NotificationStore, access: ProjectAccessStore,
    push: PushDeliveryService,
    require_project_authorization,
) -> None:
    def principal(request: Request):
        return getattr(request.state, "principal", None)

    def owner_project_check(record: PairingRecord, permission: ProjectPermission, project_id: str):
        try:
            authorize_companion_project(settings, ledger, access, record, permission, project_id)
        except CompanionError as exc:
            raise HTTPException(
                status_code=403, detail="Device project access was revoked",
            ) from exc

    def authenticated(request: Request, *, approved: bool = True) -> PairingRecord:
        try:
            record = store.authenticate(request.cookies.get(COMPANION_COOKIE))
        except CompanionError as exc:
            raise HTTPException(
                status_code=401, detail="Companion authentication required",
            ) from exc
        if approved and (
            record.view().status != PairingStatus.APPROVED or not record.cookie_activated
        ):
            raise HTTPException(status_code=403, detail="Device is not approved or has expired")
        return record

    def run_authority(request: Request, run_id: str) -> PairingRecord:
        record = authenticated(request)
        try:
            run = ledger.get_run(run_id)
        except (LedgerNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="Run not found") from exc
        owner_project_check(record, ProjectPermission.PROJECT_WRITE, run.project_id)
        return record

    @application.get("/api/mobile/status")
    async def mobile_status():
        return {
            "enabled": settings.companion_enabled,
            "public_origin": settings.companion_public_origin,
            "pairing_ttl_seconds": PAIRING_TTL_SECONDS,
            "device_max_age_seconds": DEVICE_MAX_AGE_SECONDS,
        }

    @application.get("/api/mobile/pairings", response_model=list[PairingView])
    async def list_pairings(request: Request):
        return store.list(principal(request))

    @application.post("/api/mobile/pairings", status_code=201)
    async def offer(request: Request, payload: PairingCreate, response: Response):
        if not settings.companion_enabled or not settings.companion_public_origin:
            raise HTTPException(status_code=503, detail="Configure secure companion access first")
        for project_id in payload.project_ids:
            require_project_authorization(
                project_id, principal(request), ProjectPermission.PROJECT_WRITE,
            )
            if payload.allow_run_start:
                require_project_authorization(
                    project_id, principal(request), ProjectPermission.RUN_CREATE,
                )
        try:
            pairing, secret = store.create(payload, principal(request))
        except CompanionError as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        response.headers["Cache-Control"] = "no-store"
        return {
            "pairing": pairing,
            "pairing_url": settings.companion_public_origin + "/app/mobile#" + urlencode({
                "pairing": pairing.id, "secret": secret,
            }),
        }

    @application.post("/api/mobile/pairings/{pairing_id}/decision", response_model=PairingView)
    async def decide(request: Request, pairing_id: str, payload: PairingDecision):
        try:
            record = store.owner_record(pairing_id, principal(request))
            if payload.decision == "approve":
                for project_id in record.project_ids:
                    require_project_authorization(
                        project_id, principal(request), ProjectPermission.PROJECT_WRITE,
                    )
                    owner_project_check(record, ProjectPermission.PROJECT_WRITE, project_id)
                    if record.allow_run_start:
                        require_project_authorization(
                            project_id, principal(request), ProjectPermission.RUN_CREATE,
                        )
                        owner_project_check(record, ProjectPermission.RUN_CREATE, project_id)
            return store.decide(pairing_id, payload, principal(request))
        except CompanionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @application.post("/api/mobile/pairings/{pairing_id}/revoke", response_model=PairingView)
    async def revoke(request: Request, pairing_id: str):
        try:
            result = store.revoke(pairing_id, principal(request))
            push.store.unsubscribe(pairing_id, "device_authority_revoked")
            return result
        except CompanionError as exc:
            raise HTTPException(status_code=404, detail="Device not found") from exc

    @application.post("/api/companion/pair")
    async def claim(request: Request, response: Response):
        payload = await _payload(request, PairingClaim)
        try:
            device, cookie = store.claim(payload)
        except CompanionError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        response.set_cookie(
            COMPANION_COOKIE, cookie, max_age=DEVICE_MAX_AGE_SECONDS,
            secure=True, httponly=True, samesite="strict", path="/",
        )
        return {"status": "pending", "device": device}

    @application.get("/api/companion/session")
    async def session(request: Request, response: Response):
        try:
            record = store.authenticate(request.cookies.get(COMPANION_COOKIE))
            record, renewed_cookie = store.activate(request.cookies[COMPANION_COOKIE])
        except CompanionError:
            return {"status": "unpaired", "device": None}
        if renewed_cookie is not None:
            response.set_cookie(
                COMPANION_COOKIE, renewed_cookie, max_age=DEVICE_MAX_AGE_SECONDS,
                secure=True, httponly=True, samesite="strict", path="/",
            )
        device = record.view()
        return {"status": device.status, "device": device}

    @application.get("/api/companion/overview")
    async def overview(request: Request):
        record = authenticated(request)
        project_ids = []
        for project_id in record.project_ids:
            try:
                owner_project_check(record, ProjectPermission.PROJECT_WRITE, project_id)
            except HTTPException:
                continue
            project_ids.append(project_id)
        return {
            "device": record.view(),
            "projects": [ledger.get_project(project_id) for project_id in project_ids],
            "pending_permissions": [item for item in permissions.list_pending()
                                    if item.project_id in project_ids],
            "notifications": [item for item in notifications.list()
                              if item.project_id in project_ids],
            "runs": sorted(
                [run for project_id in project_ids for run in ledger.list_runs(project_id)],
                key=lambda run: run.created_at, reverse=True,
            )[:100],
        }

    @application.post(
        "/api/companion/runs/{run_id}/activity",
        response_model=CompanionActivityPage,
    )
    async def run_activity(
        request: Request, response: Response, run_id: str,
    ) -> CompanionActivityPage:
        payload = await _payload(request, CompanionActivityRequest)
        record = run_authority(request, run_id)
        response.headers["Cache-Control"] = "no-store"
        run = ledger.get_run(run_id)
        try:
            last_sequence = ledger.get_run_summary(run_id).last_sequence
        except (LedgerNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="Run activity is unavailable") from exc
        if payload.after_sequence > last_sequence:
            raise HTTPException(status_code=400, detail="Activity cursor is ahead of the Run")

        def activity_page(after_sequence: int) -> CompanionActivityPage:
            try:
                history = ledger.list_events(run_id, after_sequence)
                project = ledger.get_project(run.project_id)
            except (LedgerNotFoundError, ValueError) as exc:
                raise HTTPException(status_code=404, detail="Run activity is unavailable") from exc
            selected: list[EventEnvelope] = []
            cursor = after_sequence
            has_more = False
            for event in history:
                relevant = _companion_activity_event(event)
                if relevant and len(selected) >= 60:
                    has_more = True
                    break
                cursor = event.sequence
                if relevant:
                    selected.append(event)
            artifacts = sorted(
                (item for item in project.artifacts if item.run_id == run.id),
                key=lambda item: item.updated_at, reverse=True,
            )[:30]
            return CompanionActivityPage(
                events=selected, artifacts=artifacts, next_sequence=cursor,
                has_more=has_more, status=ledger.get_run(run_id).status,
            )

        page = activity_page(payload.after_sequence)
        if (
            payload.wait_for_update and not page.events and not page.has_more
            and page.status not in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}
        ):
            try:
                async with asyncio.timeout(COMPANION_ACTIVITY_WAIT_SECONDS):
                    async for event in runs.events.stream(
                        run_id, page.next_sequence, heartbeat_seconds=1,
                    ):
                        # A connection never extends device or project authority.
                        run_authority(request, run_id)
                        if event is not None and _companion_activity_event(event):
                            break
                        if ledger.get_run(run_id).status in {
                            RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED,
                        }:
                            break
            except TimeoutError:
                pass
            except (LedgerNotFoundError, ValueError) as exc:
                raise HTTPException(status_code=404, detail="Run activity is unavailable") from exc
            page = activity_page(page.next_sequence)
        owner_project_check(record, ProjectPermission.PROJECT_WRITE, run.project_id)
        authenticated(request)
        return page

    @application.post("/api/companion/runs/{run_id}/artifacts/{artifact_id}/preview")
    async def preview_run_image(
        request: Request, run_id: str, artifact_id: str,
    ) -> Response:
        record = run_authority(request, run_id)
        run = ledger.get_run(run_id)
        try:
            item = next(
                artifact for artifact in ledger.get_project(run.project_id).artifacts
                if artifact.id == artifact_id and artifact.run_id == run_id
            )
        except (LedgerNotFoundError, StopIteration) as exc:
            raise HTTPException(status_code=404, detail="Run image not found") from exc
        if (
            item.mime not in {"image/png", "image/jpeg", "image/webp"}
            or item.size_bytes > 5_000_000
        ):
            raise HTTPException(status_code=415, detail="Run image preview is unavailable")
        try:
            verified, content = ledger.capture_artifact(run.project_id, artifact_id)
        except (LedgerNotFoundError, OSError, ValueError) as exc:
            raise HTTPException(status_code=409, detail="Run image failed verification") from exc
        if (
            verified.run_id != run_id
            or verified.mime not in {"image/png", "image/jpeg", "image/webp"}
            or len(content) > 5_000_000
        ):
            raise HTTPException(status_code=409, detail="Run image changed during preview")
        owner_project_check(record, ProjectPermission.PROJECT_WRITE, run.project_id)
        authenticated(request)
        return Response(content, media_type=verified.mime, headers={
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
        })

    @application.post("/api/companion/runs/{run_id}/permissions/{request_id}", status_code=204)
    async def resolve(request: Request, run_id: str, request_id: str):
        payload = await _payload(request, PermissionResolution)
        # A slow request body must not extend a revoked device/member's authority.
        record = run_authority(request, run_id)
        if payload.decision not in {PermissionDecision.ALLOW_ONCE, PermissionDecision.DENY}:
            raise HTTPException(status_code=403, detail="Companion approval is per invocation only")
        try:
            await runs.resolve_permission(
                run_id, request_id, payload,
                actor=EventActor(type="user", name=f"paired-device:{record.id}"),
            )
        except DurablePermissionConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except PermissionNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Permission request not found") from exc

    @application.post("/api/companion/runs/{run_id}/resume-permission", response_model=RunRecord,
                      status_code=202)
    async def resume_permission(request: Request, run_id: str):
        run_authority(request, run_id)
        try:
            return await runs.resume_permission(run_id)
        except DurablePermissionConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @application.post("/api/companion/projects/{project_id}/runs", response_model=RunRecord,
                      status_code=202)
    async def start_run(request: Request, project_id: str):
        payload = await _payload(request, CompanionRunCreate)
        record = authenticated(request)
        if not record.allow_run_start:
            raise HTTPException(status_code=403, detail="Device may not start runs")
        owner_project_check(record, ProjectPermission.RUN_CREATE, project_id)
        try:
            return await runs.create_and_start(
                project_id, payload.prompt, principal=record.principal_binding,
                companion_device_id=record.id,
            )
        except (PolicyDeniedError, ProviderConfigurationError, ProviderNotFoundError) as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    @application.post("/api/companion/runs/{run_id}/cancel", response_model=RunRecord)
    async def cancel(request: Request, run_id: str):
        record = run_authority(request, run_id)
        try:
            return await runs.cancel(
                run_id, actor=EventActor(type="user", name=f"paired-device:{record.id}"),
            )
        except RunCancelConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @application.post("/api/companion/logout")
    async def logout(request: Request, response: Response):
        record = authenticated(request, approved=False)
        store.revoke_device(request.cookies[COMPANION_COOKIE])
        push.store.unsubscribe(record.id, "device_authority_revoked")
        response.delete_cookie(COMPANION_COOKIE, secure=True, httponly=True, samesite="strict")
        return {}

    @application.get("/api/companion/push/status")
    async def push_status(request: Request):
        record = authenticated(request)
        return push.status(record.id)

    @application.post("/api/companion/push/subscription")
    async def push_subscribe(request: Request):
        payload = await _payload(request, PushSubscription)
        record = authenticated(request)
        # A browser subscription never widens the paired device's current scope.
        for project_id in record.project_ids:
            owner_project_check(record, ProjectPermission.PROJECT_WRITE, project_id)
        try:
            return push.subscribe(record.id, payload)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Push subscription is unavailable") from exc

    @application.post("/api/companion/push/unsubscribe")
    async def push_unsubscribe(request: Request):
        await _payload(request, APIModel)
        record = authenticated(request)
        push.store.unsubscribe(record.id)
        return push.status(record.id)
