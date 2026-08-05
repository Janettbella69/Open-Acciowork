from __future__ import annotations

import json
from typing import Annotated

import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from sse_starlette.sse import EventSourceResponse

from .config import Settings, get_settings
from .constants import Dimension, EventType, PermissionDecision, ProjectStage, RunStatus
from .events import EventPublisher
from .models import OpportunityProject, PermissionResolution, ProjectCreate, RunCreate, RunRecord
from .permissions import PermissionBroker, PermissionNotFoundError
from .runs import AgentRuntime, RunManager
from .sdk_adapter import ClaudeAgentRuntime
from .storage import FileLedger, LedgerNotFoundError


def create_app(
    settings: Settings | None = None,
    runtime: AgentRuntime | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    ledger = FileLedger(settings.data_dir)
    events = EventPublisher(ledger)
    permissions = PermissionBroker(
        ledger,
        events,
        timeout_seconds=settings.permission_timeout_seconds,
    )
    runtime = runtime or ClaudeAgentRuntime(settings, ledger, events, permissions)
    runs = RunManager(ledger, events, permissions, runtime)

    application = FastAPI(title="OPHIRA API", version="0.1.0")
    application.state.ledger = ledger
    application.state.events = events
    application.state.permissions = permissions
    application.state.runs = runs
    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @application.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": "ophira-backend"}

    @application.get("/api/meta/protocol")
    async def protocol_metadata() -> dict[str, list[str] | str]:
        return {
            "version": "0.1.0",
            "event_types": [item.value for item in EventType],
            "run_statuses": [item.value for item in RunStatus],
            "dimensions": [item.value for item in Dimension],
            "project_stages": [item.value for item in ProjectStage],
            "permission_decisions": [item.value for item in PermissionDecision],
        }

    @application.post(
        "/api/projects",
        response_model=OpportunityProject,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_project(payload: ProjectCreate) -> OpportunityProject:
        return ledger.create_project(payload)

    @application.get("/api/projects/{project_id}", response_model=OpportunityProject)
    async def get_project(project_id: str) -> OpportunityProject:
        try:
            return ledger.get_project(project_id)
        except (LedgerNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="Project not found") from exc

    @application.post(
        "/api/projects/{project_id}/runs",
        response_model=RunRecord,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def start_run(project_id: str, payload: RunCreate) -> RunRecord:
        try:
            return await runs.create_and_start(project_id, payload.prompt)
        except (LedgerNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="Project not found") from exc

    @application.get("/api/runs/{run_id}", response_model=RunRecord)
    async def get_run(run_id: str) -> RunRecord:
        try:
            return ledger.get_run(run_id)
        except (LedgerNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="Run not found") from exc

    @application.get("/api/runs/{run_id}/events")
    async def stream_run_events(
        request: Request,
        run_id: str,
        last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    ) -> EventSourceResponse:
        try:
            ledger.get_run(run_id)
        except (LedgerNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="Run not found") from exc
        after_sequence = _parse_last_event_id(last_event_id)

        async def event_generator():
            async for event in events.stream(run_id, after_sequence):
                if await request.is_disconnected():
                    return
                if event is None:
                    yield {"comment": "keepalive"}
                    continue
                yield {
                    "event": event.type,
                    "id": event.id,
                    "data": json.dumps(event.model_dump(mode="json"), ensure_ascii=False),
                }

        return EventSourceResponse(event_generator(), ping=None)

    @application.post(
        "/api/runs/{run_id}/permissions/{request_id}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    async def resolve_permission(
        run_id: str,
        request_id: str,
        payload: PermissionResolution,
    ) -> None:
        try:
            await permissions.resolve(run_id, request_id, payload)
        except PermissionNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Permission request not found") from exc

    @application.post("/api/runs/{run_id}/cancel", response_model=RunRecord)
    async def cancel_run(run_id: str) -> RunRecord:
        try:
            return await runs.cancel(run_id)
        except (LedgerNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="Run not found") from exc

    return application


def _parse_last_event_id(value: str | None) -> int:
    if not value:
        return 0
    raw_sequence = value.rsplit(":", maxsplit=1)[-1]
    try:
        return max(0, int(raw_sequence))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid Last-Event-ID") from exc


app = create_app()


def run() -> None:
    settings = get_settings()
    uvicorn.run(
        "ophira_backend.main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
    )
