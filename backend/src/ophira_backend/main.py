from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

import uvicorn
from fastapi import FastAPI, Header, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from sse_starlette.sse import EventSourceResponse

from .agent_core import create_agent_core
from .config import Settings, get_settings
from .constants import (
    AgentTemplate,
    Dimension,
    EventType,
    MemoryScope,
    MemorySource,
    MemoryStatus,
    MissedRunPolicy,
    PermissionDecision,
    ProjectStage,
    RunStatus,
)
from .events import EventPublisher
from .memory import FileMemoryProvider, MemoryNotFoundError
from .models import (
    AgentCreate,
    AgentProfile,
    AgentUpdate,
    MemoryCreate,
    MemoryDecision,
    MemoryItem,
    MemoryRollback,
    OpportunityProject,
    PermissionResolution,
    ProjectCreate,
    RunCreate,
    RunRecord,
    Schedule,
    ScheduleCreate,
    utc_now,
)
from .permissions import PermissionBroker, PermissionNotFoundError
from .runs import AgentRuntime, RunManager
from .scheduler import ScheduleService, ScheduleValidationError, compute_next_fire, new_schedule
from .sdk_adapter import ClaudeAgentRuntime
from .storage import FileLedger, LedgerNotFoundError
from .templates import TEMPLATE_SPECS


def create_app(
    settings: Settings | None = None,
    runtime: AgentRuntime | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    ledger = FileLedger(settings.data_dir)
    events = EventPublisher(ledger)
    memory = FileMemoryProvider(settings.data_dir / "memory")
    permissions = PermissionBroker(
        ledger,
        events,
        timeout_seconds=settings.permission_timeout_seconds,
    )
    runtime = runtime or ClaudeAgentRuntime(settings, ledger, events, permissions, memory)
    runs = RunManager(ledger, events, permissions, runtime)
    scheduler = ScheduleService(ledger, runs, events)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        await scheduler.start()
        try:
            yield
        finally:
            await scheduler.stop()

    application = FastAPI(title="OPHIRA API", version="0.1.0", lifespan=lifespan)
    application.state.ledger = ledger
    application.state.events = events
    application.state.memory = memory
    application.state.permissions = permissions
    application.state.runs = runs
    application.state.scheduler = scheduler
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
            "agent_templates": [item.value for item in AgentTemplate],
            "memory_scopes": [item.value for item in MemoryScope],
            "memory_statuses": [item.value for item in MemoryStatus],
            "missed_run_policies": [item.value for item in MissedRunPolicy],
        }

    @application.post(
        "/api/projects",
        response_model=OpportunityProject,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_project(payload: ProjectCreate) -> OpportunityProject:
        return ledger.create_project(payload)

    @application.get("/api/projects", response_model=list[OpportunityProject])
    async def list_projects() -> list[OpportunityProject]:
        return ledger.list_projects()

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
            if payload.agent_id:
                ledger.get_agent(payload.agent_id)
            return await runs.create_and_start(
                project_id,
                payload.prompt,
                agent_id=payload.agent_id,
                resume_session_id=payload.resume_session_id,
            )
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

    @application.post(
        "/api/agents",
        response_model=AgentProfile,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_agent(payload: AgentCreate) -> AgentProfile:
        agent = ledger.create_agent(payload)
        create_agent_core(
            ledger.agent_core_dir(agent.id),
            TEMPLATE_SPECS[agent.template],
            agent,
        )
        return agent

    @application.get("/api/agents", response_model=list[AgentProfile])
    async def list_agents() -> list[AgentProfile]:
        return ledger.list_agents()

    @application.get("/api/agents/{agent_id}", response_model=AgentProfile)
    async def get_agent(agent_id: str) -> AgentProfile:
        try:
            return ledger.get_agent(agent_id)
        except (LedgerNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="Agent not found") from exc

    @application.post("/api/agents/{agent_id}/update", response_model=AgentProfile)
    async def update_agent(agent_id: str, payload: AgentUpdate) -> AgentProfile:
        changes = payload.model_dump(exclude_unset=True)
        try:
            if not changes:
                return ledger.get_agent(agent_id)
            return ledger.update_agent(agent_id, **changes)
        except (LedgerNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="Agent not found") from exc

    @application.post(
        "/api/memory",
        response_model=MemoryItem,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_memory(payload: MemoryCreate) -> MemoryItem:
        return memory.propose(
            scope=payload.scope,
            scope_id=payload.scope_id,
            text=payload.text,
            kind=payload.kind,
            source=MemorySource.USER,
            sensitivity=payload.sensitivity,
            created_by="user",
        )

    @application.get("/api/memory", response_model=list[MemoryItem])
    async def list_memory(
        scope: MemoryScope | None = None,
        scope_id: str | None = None,
        status_filter: Annotated[MemoryStatus | None, Query(alias="status")] = None,
    ) -> list[MemoryItem]:
        return memory.list(scope=scope, scope_id=scope_id, status=status_filter)

    @application.post("/api/memory/{memory_id}/decision", response_model=MemoryItem)
    async def decide_memory(memory_id: str, payload: MemoryDecision) -> MemoryItem:
        try:
            return memory.decide(memory_id, payload.decision, payload.reason)
        except MemoryNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Memory not found") from exc

    @application.post("/api/memory/{memory_id}/rollback", response_model=MemoryItem)
    async def rollback_memory(memory_id: str, payload: MemoryRollback) -> MemoryItem:
        try:
            return memory.rollback(memory_id, payload.version)
        except (MemoryNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="Memory version not found") from exc

    @application.post(
        "/api/schedules",
        response_model=Schedule,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_schedule(payload: ScheduleCreate) -> Schedule:
        try:
            schedule = new_schedule(payload)
        except ScheduleValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        try:
            return ledger.create_schedule(schedule)
        except (LedgerNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="Project not found") from exc

    @application.get("/api/schedules", response_model=list[Schedule])
    async def list_schedules() -> list[Schedule]:
        return ledger.list_schedules()

    @application.post("/api/schedules/{schedule_id}/pause", response_model=Schedule)
    async def pause_schedule(schedule_id: str) -> Schedule:
        try:
            return ledger.update_schedule(schedule_id, enabled=False)
        except (LedgerNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="Schedule not found") from exc

    @application.post("/api/schedules/{schedule_id}/resume", response_model=Schedule)
    async def resume_schedule(schedule_id: str) -> Schedule:
        try:
            schedule = ledger.get_schedule(schedule_id)
            next_at = compute_next_fire(
                schedule.cron,
                schedule.run_once_at,
                schedule.timezone,
                utc_now(),
            )
            return ledger.update_schedule(schedule_id, enabled=True, next_fire_at=next_at)
        except (LedgerNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="Schedule not found") from exc

    @application.post(
        "/api/schedules/{schedule_id}/run",
        response_model=RunRecord,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def run_schedule_now(schedule_id: str) -> RunRecord:
        try:
            return await scheduler.run_now(schedule_id)
        except (LedgerNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="Schedule not found") from exc

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
