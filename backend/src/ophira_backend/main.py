from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

import uvicorn
from fastapi import FastAPI, Header, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from sse_starlette.sse import EventSourceResponse

from .agent_core import create_agent_core
from .config import Settings, get_settings
from .connectors import ConnectorNotFoundError, ConnectorRegistry
from .constants import (
    AgentTemplate,
    Dimension,
    EventType,
    EvidenceKind,
    EvidenceStatus,
    MemoryScope,
    MemorySource,
    MemoryStatus,
    MissedRunPolicy,
    PermissionDecision,
    ProjectStage,
    ProviderErrorCode,
    RunMode,
    RunStatus,
)
from .events import EventPublisher
from .memory import FileMemoryProvider, MemoryNotFoundError
from .models import (
    AgentCreate,
    AgentProfile,
    AgentUpdate,
    Artifact,
    Connector,
    Evidence,
    EvidenceStatusChange,
    EvidenceStatusUpdate,
    MemoryCreate,
    MemoryDecision,
    MemoryItem,
    MemoryRollback,
    OpportunityProject,
    PermissionResolution,
    ProjectCreate,
    ProviderPreflightResult,
    ProviderProfile,
    ProviderProfileUpsert,
    RunCreate,
    RunRecord,
    Schedule,
    ScheduleCreate,
    ScheduleUpdate,
    SkillTestResult,
    SkillVersion,
    utc_now,
)
from .permissions import PermissionBroker, PermissionNotFoundError
from .providers import ProviderNotFoundError, ProviderRegistry
from .runs import AgentRuntime, RunManager
from .scheduler import ScheduleService, ScheduleValidationError, compute_next_fire, new_schedule
from .sdk_adapter import ClaudeAgentRuntime
from .skills import SkillNotFoundError, SkillRegistry
from .storage import FileLedger, LedgerNotFoundError
from .templates import TEMPLATE_SPECS


def create_app(
    settings: Settings | None = None,
    runtime: AgentRuntime | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    ledger = FileLedger(settings.data_dir)
    events = EventPublisher(ledger)
    memory = FileMemoryProvider(
        settings.data_dir / "memory",
        agents_root=settings.data_dir / "agents",
    )
    permissions = PermissionBroker(
        ledger,
        events,
        timeout_seconds=settings.permission_timeout_seconds,
    )
    providers = ProviderRegistry(settings)
    skills = SkillRegistry(settings.capability_plugin_dir, settings.data_dir / "skills")
    runtime = runtime or ClaudeAgentRuntime(
        settings,
        ledger,
        events,
        permissions,
        memory,
        providers=providers,
        skills=skills,
    )
    runs = RunManager(
        ledger,
        events,
        permissions,
        runtime,
        providers=providers,
        skills=skills,
    )
    scheduler = ScheduleService(ledger, runs, events)
    connectors = ConnectorRegistry(settings.data_dir / "connectors")

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
    application.state.providers = providers
    application.state.skills = skills
    application.state.runtime = runtime
    application.state.runs = runs
    application.state.scheduler = scheduler
    application.state.connectors = connectors
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

    @application.get("/api/runtime/provider")
    async def runtime_provider() -> dict[str, str | bool | None]:
        return settings.provider_status()

    @application.get("/api/meta/protocol")
    async def protocol_metadata() -> dict[str, list[str] | str]:
        return {
            "version": "0.1.0",
            "event_types": [item.value for item in EventType],
            "run_statuses": [item.value for item in RunStatus],
            "run_modes": [item.value for item in RunMode],
            "provider_error_codes": [item.value for item in ProviderErrorCode],
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

    @application.get("/api/projects/{project_id}/runs", response_model=list[RunRecord])
    async def list_project_runs(project_id: str) -> list[RunRecord]:
        try:
            return ledger.list_runs(project_id)
        except (LedgerNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="Project not found") from exc

    @application.post(
        "/api/projects/{project_id}/files",
        response_model=Artifact,
        status_code=status.HTTP_201_CREATED,
    )
    async def upload_project_file(
        project_id: str,
        request: Request,
        filename: Annotated[str, Query(min_length=1, max_length=240)],
        mime: Annotated[str | None, Query(max_length=200)] = None,
    ) -> Artifact:
        content = await request.body()
        if not content:
            raise HTTPException(status_code=400, detail="File is empty")
        if len(content) > 50 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="File exceeds the 50 MB MVP limit")
        try:
            return ledger.save_upload(project_id, filename, content, mime=mime)
        except (LedgerNotFoundError, ValueError) as exc:
            raise HTTPException(
                status_code=404,
                detail="Project not found or path invalid",
            ) from exc

    @application.get("/api/projects/{project_id}/evidence", response_model=list[Evidence])
    async def list_project_evidence(
        project_id: str,
        q: Annotated[str, Query(max_length=500)] = "",
        kind: EvidenceKind | None = None,
        tag: Annotated[str | None, Query(max_length=120)] = None,
        status_filter: Annotated[EvidenceStatus | None, Query(alias="status")] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> list[Evidence]:
        try:
            return ledger.search_evidence(
                project_id, query=q, kind=kind, tag=tag, status=status_filter, limit=limit
            )
        except (LedgerNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="Project not found") from exc

    @application.post(
        "/api/projects/{project_id}/evidence/{evidence_id}/status",
        response_model=EvidenceStatusChange,
    )
    async def update_evidence_status(
        project_id: str,
        evidence_id: str,
        payload: EvidenceStatusUpdate,
    ) -> EvidenceStatusChange:
        try:
            evidence, affected = ledger.update_evidence_status(
                project_id, evidence_id, payload.status
            )
        except (LedgerNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="Project or evidence not found") from exc
        return EvidenceStatusChange(evidence=evidence, affected_decision_ids=affected)

    @application.get("/api/projects/{project_id}/artifacts", response_model=list[Artifact])
    async def list_project_artifacts(project_id: str) -> list[Artifact]:
        try:
            return ledger.get_project(project_id).artifacts
        except (LedgerNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="Project not found") from exc

    @application.get("/api/projects/{project_id}/artifacts/{artifact_id}/download")
    async def download_project_artifact(project_id: str, artifact_id: str) -> FileResponse:
        try:
            artifact, path = ledger.get_artifact(project_id, artifact_id)
        except (LedgerNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="Artifact not found") from exc
        return FileResponse(path, media_type=artifact.mime, filename=artifact.name)

    @application.post(
        "/api/projects/{project_id}/runs",
        response_model=RunRecord,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def start_run(project_id: str, payload: RunCreate) -> RunRecord:
        try:
            if payload.agent_id:
                ledger.get_agent(payload.agent_id)
            provider_profile_id = payload.provider_profile_id or providers.default_profile_id()
            provider_snapshot = (
                providers.snapshot(provider_profile_id) if provider_profile_id else None
            )
            return await runs.create_and_start(
                project_id,
                payload.prompt,
                agent_id=payload.agent_id,
                resume_session_id=payload.resume_session_id,
                mode=payload.mode,
                provider_profile_id=provider_profile_id,
                provider_snapshot=provider_snapshot,
                skill_snapshot=skills.snapshot(),
            )
        except (LedgerNotFoundError, ProviderNotFoundError, ValueError) as exc:
            raise HTTPException(
                status_code=404,
                detail="Project, Agent, or Provider not found",
            ) from exc

    @application.get("/api/providers", response_model=list[ProviderProfile])
    async def list_providers() -> list[ProviderProfile]:
        return providers.list()

    @application.put("/api/providers/{provider_id}", response_model=ProviderProfile)
    async def upsert_provider(
        provider_id: str,
        payload: ProviderProfileUpsert,
    ) -> ProviderProfile:
        try:
            return providers.upsert(provider_id, payload)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @application.post(
        "/api/providers/{provider_id}/preflight",
        response_model=ProviderPreflightResult,
    )
    async def preflight_provider(provider_id: str) -> ProviderPreflightResult:
        try:
            providers.get(provider_id)
            preflight = getattr(runtime, "preflight", None)
            if preflight is None:
                raise HTTPException(
                    status_code=501,
                    detail="The configured test runtime does not support Provider preflight.",
                )
            return await preflight(provider_id)
        except ProviderNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Provider not found") from exc

    @application.get("/api/skills", response_model=list[SkillVersion])
    async def list_skills() -> list[SkillVersion]:
        return skills.list()

    @application.post("/api/skills/{skill_id:path}/enable", response_model=SkillVersion)
    async def enable_skill(skill_id: str, enabled: bool = True) -> SkillVersion:
        try:
            return skills.set_enabled(skill_id, enabled)
        except SkillNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Skill not found") from exc

    @application.post("/api/skills/{skill_id:path}/test", response_model=SkillTestResult)
    async def test_skill(skill_id: str) -> SkillTestResult:
        try:
            return skills.test(skill_id)
        except SkillNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Skill not found") from exc

    @application.get("/api/connectors", response_model=list[Connector])
    async def list_connectors() -> list[Connector]:
        return connectors.list()

    @application.post("/api/connectors/{connector_id}/health-check", response_model=Connector)
    async def connector_health_check(connector_id: str) -> Connector:
        try:
            return connectors.health_check(connector_id)
        except (ConnectorNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="Connector not found") from exc

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

    @application.patch("/api/schedules/{schedule_id}", response_model=Schedule)
    async def patch_schedule(schedule_id: str, payload: ScheduleUpdate) -> Schedule:
        changes = payload.model_dump(exclude_unset=True)
        try:
            schedule = ledger.get_schedule(schedule_id)
        except (LedgerNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="Schedule not found") from exc
        merged = schedule.model_copy(update=changes)
        if (merged.cron is None) == (merged.run_once_at is None):
            raise HTTPException(
                status_code=400, detail="exactly one of cron or run_once_at is required"
            )
        if {"cron", "run_once_at", "timezone"} & changes.keys():
            try:
                next_at = compute_next_fire(
                    merged.cron, merged.run_once_at, merged.timezone, utc_now()
                )
            except ScheduleValidationError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            if next_at is None:
                raise HTTPException(
                    status_code=400, detail="schedule has no future fire time"
                )
            changes["next_fire_at"] = next_at
        return ledger.update_schedule(schedule_id, **changes)

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
