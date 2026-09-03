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
from .automation import (
    IntentNotFoundError,
    IntentStore,
    NotificationNotFoundError,
    NotificationStore,
)
from .channels import (
    MAX_FEISHU_BODY_BYTES,
    ChannelError,
    ChannelReplayGuard,
    decrypt_feishu_payload,
    feishu_challenge,
    feishu_event_id,
    feishu_should_handle,
    feishu_timestamp_is_fresh,
    to_inbound,
    verify_feishu_signature,
)
from .config import Settings, get_settings
from .connectors import ConnectorNotFoundError, ConnectorRegistry
from .constants import (
    AgentTemplate,
    Dimension,
    EventType,
    EvidenceKind,
    EvidenceStatus,
    FeedbackKind,
    MemoryScope,
    MemorySource,
    MemoryStatus,
    MissedRunPolicy,
    PermissionDecision,
    PermissionModePolicy,
    ProjectStage,
    ProviderErrorCode,
    RunMode,
    RunStatus,
    ScheduleKind,
)
from .events import EventPublisher
from .feedback import FeedbackStore
from .grants import GrantNotFoundError, PermissionGrantStore
from .memory import FileMemoryProvider, MemoryNotFoundError
from .models import (
    AgentCreate,
    AgentProfile,
    AgentUpdate,
    Artifact,
    Connector,
    ConnectorUpsert,
    ConstraintAppend,
    Evidence,
    EvidenceStatusChange,
    EvidenceStatusUpdate,
    FeedbackCreate,
    FeedbackItem,
    MemoryCorrection,
    MemoryCreate,
    MemoryDecision,
    MemoryItem,
    MemoryRollback,
    Notification,
    OpportunityProject,
    OrgPolicy,
    PendingPermissionView,
    PermissionGrant,
    PermissionResolution,
    ProjectCreate,
    ProviderPreflightResult,
    ProviderProfile,
    ProviderProfileUpsert,
    RunCreate,
    RunRecord,
    RunRetryRequest,
    Schedule,
    ScheduleCreate,
    ScheduleUpdate,
    SkillTestResult,
    SkillVersion,
    StandingIntent,
    StandingIntentCreate,
    utc_now,
)
from .permissions import PermissionBroker, PermissionNotFoundError
from .policy import OrgPolicyStore, PolicyDeniedError
from .providers import FileSecretVault, ProviderNotFoundError, ProviderRegistry
from .runs import AgentRuntime, RunManager, RunRetryConflictError
from .scheduler import ScheduleService, ScheduleValidationError, compute_next_fire, new_schedule
from .sdk_adapter import ClaudeAgentRuntime
from .skills import MAX_SKILL_ARCHIVE_BYTES, SkillNotFoundError, SkillRegistry
from .storage import FileLedger, LedgerNotFoundError
from .templates import TEMPLATE_SPECS

MAX_PROJECT_UPLOAD_BYTES = 50 * 1024 * 1024


async def _read_bounded_body(
    request: Request,
    *,
    max_bytes: int,
    too_large_detail: str,
) -> bytes:
    """Read an HTTP body without trusting Content-Length or buffering past the cap."""
    declared_length = request.headers.get("content-length")
    if declared_length:
        try:
            declared_bytes = int(declared_length)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid Content-Length") from exc
        if declared_bytes < 0:
            raise HTTPException(status_code=400, detail="invalid Content-Length")
        if declared_bytes > max_bytes:
            raise HTTPException(status_code=413, detail=too_large_detail)

    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > max_bytes:
            raise HTTPException(status_code=413, detail=too_large_detail)
        body.extend(chunk)
    return bytes(body)


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
    grants = PermissionGrantStore(settings.data_dir / "grants")
    policy = OrgPolicyStore(settings.data_dir / "org" / "policy.json")
    feedback = FeedbackStore(settings.data_dir / "feedback")
    channel_replays = ChannelReplayGuard(settings.data_dir / "channels" / "replays.json")
    permissions = PermissionBroker(
        ledger,
        events,
        timeout_seconds=settings.permission_timeout_seconds,
        grants=grants,
    )
    providers = ProviderRegistry(settings)
    skills = SkillRegistry(settings.capability_plugin_dir, settings.data_dir / "skills")
    notifications = NotificationStore(settings.data_dir / "notifications")
    intents = IntentStore(settings.data_dir / "intents")
    connectors = ConnectorRegistry(
        settings.data_dir / "connectors",
        vault=FileSecretVault(settings.data_dir / "secrets", filename="connector-secrets.json"),
    )
    connectors.ensure_catalog()
    runtime = runtime or ClaudeAgentRuntime(
        settings,
        ledger,
        events,
        permissions,
        memory,
        providers=providers,
        skills=skills,
        intents=intents,
        notifications=notifications,
        connectors=connectors,
        policy=policy,
    )
    runs = RunManager(
        ledger,
        events,
        permissions,
        runtime,
        providers=providers,
        skills=skills,
        notifications=notifications,
        memory=memory,
        policy=policy,
        settings=settings,
    )
    scheduler = ScheduleService(ledger, runs, events, notifications=notifications)
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
    application.state.notifications = notifications
    application.state.intents = intents
    application.state.grants = grants
    application.state.policy = policy
    application.state.feedback = feedback
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
            "permission_modes": [item.value for item in PermissionModePolicy],
            "schedule_kinds": [item.value for item in ScheduleKind],
            "feedback_kinds": [item.value for item in FeedbackKind],
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
        content = await _read_bounded_body(
            request,
            max_bytes=MAX_PROJECT_UPLOAD_BYTES,
            too_large_detail="File exceeds the 50 MB upload limit",
        )
        if not content:
            raise HTTPException(status_code=400, detail="File is empty")
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
        except PolicyDeniedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
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
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @application.post("/api/skills/{skill_id:path}/test", response_model=SkillTestResult)
    async def test_skill(skill_id: str) -> SkillTestResult:
        try:
            return skills.test(skill_id)
        except SkillNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Skill not found") from exc

    @application.post(
        "/api/skills/install",
        response_model=SkillVersion,
        status_code=status.HTTP_201_CREATED,
    )
    async def install_skill(request: Request) -> SkillVersion:
        content = await _read_bounded_body(
            request,
            max_bytes=MAX_SKILL_ARCHIVE_BYTES,
            too_large_detail="Skill archive exceeds the upload limit",
        )
        try:
            return skills.install_zip(content)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @application.get("/api/connectors", response_model=list[Connector])
    async def list_connectors() -> list[Connector]:
        return connectors.list()

    @application.post("/api/connectors/{connector_id}/health-check", response_model=Connector)
    async def connector_health_check(connector_id: str) -> Connector:
        try:
            return connectors.health_check(connector_id)
        except (ConnectorNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="Connector not found") from exc

    @application.put("/api/connectors/{connector_id}", response_model=Connector)
    async def upsert_connector(connector_id: str, payload: ConnectorUpsert) -> Connector:
        try:
            return connectors.upsert(
                connector_id,
                type=payload.type,
                display_name=payload.display_name,
                scope=payload.scope,
                secret_env=payload.secret_env,
                endpoint=payload.endpoint,
                secret=payload.secret,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @application.post("/api/connectors/{connector_id}/revoke", response_model=Connector)
    async def revoke_connector(connector_id: str) -> Connector:
        try:
            return connectors.revoke(connector_id)
        except (ConnectorNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="Connector not found") from exc

    @application.get("/api/runs/{run_id}", response_model=RunRecord)
    async def get_run(run_id: str) -> RunRecord:
        try:
            return ledger.get_run(run_id)
        except (LedgerNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="Run not found") from exc

    @application.post(
        "/api/runs/{run_id}/retry",
        response_model=RunRecord,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def retry_run(run_id: str, payload: RunRetryRequest) -> RunRecord:
        try:
            return await runs.retry(run_id, payload.strategy)
        except RunRetryConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (PolicyDeniedError, ProviderNotFoundError) as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except (LedgerNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="Run or project not found") from exc

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
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @application.patch("/api/memory/{memory_id}", response_model=MemoryItem)
    async def correct_memory(memory_id: str, payload: MemoryCorrection) -> MemoryItem:
        try:
            return memory.correct(
                memory_id,
                text=payload.text,
                kind=payload.kind,
                sensitivity=payload.sensitivity,
            )
        except MemoryNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Memory not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

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

    @application.get("/api/notifications", response_model=list[Notification])
    async def list_notifications(
        unread: bool = False,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> list[Notification]:
        return notifications.list(unread_only=unread, limit=limit)

    @application.post("/api/notifications/{notification_id}/read", response_model=Notification)
    async def read_notification(notification_id: str) -> Notification:
        try:
            return notifications.mark_read(notification_id)
        except (NotificationNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="Notification not found") from exc

    @application.post("/api/notifications/read-all")
    async def read_all_notifications() -> dict[str, int]:
        return {"marked": notifications.mark_all_read()}

    @application.get("/api/intents", response_model=list[StandingIntent])
    async def list_intents(project_id: str | None = None) -> list[StandingIntent]:
        return intents.list(project_id=project_id)

    @application.post(
        "/api/intents",
        response_model=StandingIntent,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_intent(payload: StandingIntentCreate) -> StandingIntent:
        try:
            ledger.get_project(payload.project_id)
        except (LedgerNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="Project not found") from exc
        return intents.create(payload)

    @application.post("/api/intents/{intent_id}/pause", response_model=StandingIntent)
    async def pause_intent(intent_id: str) -> StandingIntent:
        try:
            return intents.set_enabled(intent_id, False)
        except (IntentNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="Intent not found") from exc

    @application.post("/api/intents/{intent_id}/resume", response_model=StandingIntent)
    async def resume_intent(intent_id: str) -> StandingIntent:
        try:
            return intents.set_enabled(intent_id, True)
        except (IntentNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="Intent not found") from exc

    @application.patch("/api/schedules/{schedule_id}", response_model=Schedule)
    async def patch_schedule(schedule_id: str, payload: ScheduleUpdate) -> Schedule:
        # Keep nested SchedulePermissionRule instances typed; model_dump would turn
        # them into dicts and model_copy(update=...) intentionally skips validation.
        changes = {field: getattr(payload, field) for field in payload.model_fields_set}
        if changes.get("permission_ceiling", ...) is None:
            changes.pop("permission_ceiling", None)
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
        except (PolicyDeniedError, ProviderNotFoundError) as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except (LedgerNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="Schedule not found") from exc

    @application.get("/api/org/policy", response_model=OrgPolicy)
    async def get_org_policy() -> OrgPolicy:
        return policy.get()

    @application.put("/api/org/policy", response_model=OrgPolicy)
    async def put_org_policy(payload: OrgPolicy) -> OrgPolicy:
        return policy.save(payload)

    @application.get("/api/permissions/pending", response_model=list[PendingPermissionView])
    async def list_pending_permissions() -> list[PendingPermissionView]:
        return permissions.list_pending()

    @application.get("/api/permissions/grants", response_model=list[PermissionGrant])
    async def list_permission_grants() -> list[PermissionGrant]:
        return grants.list()

    @application.post("/api/permissions/grants/{grant_id}/revoke", response_model=PermissionGrant)
    async def revoke_permission_grant(grant_id: str) -> PermissionGrant:
        try:
            return grants.revoke(grant_id)
        except (GrantNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="Grant not found") from exc

    @application.post(
        "/api/feedback",
        response_model=FeedbackItem,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_feedback(payload: FeedbackCreate) -> FeedbackItem:
        return feedback.add(payload)

    @application.get("/api/feedback", response_model=list[FeedbackItem])
    async def list_feedback() -> list[FeedbackItem]:
        return feedback.list()

    @application.post(
        "/api/projects/{project_id}/constraints",
        response_model=OpportunityProject,
    )
    async def append_constraints(project_id: str, payload: ConstraintAppend) -> OpportunityProject:
        try:
            def mutate(project: OpportunityProject) -> None:
                for line in payload.constraints:
                    if line not in project.constraints:
                        project.constraints.append(line)

            return ledger.update_project(project_id, mutate)
        except (LedgerNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="Project not found") from exc

    @application.post("/api/channels/feishu")
    async def feishu_channel(request: Request) -> dict:
        body = await _read_bounded_body(
            request,
            max_bytes=MAX_FEISHU_BODY_BYTES,
            too_large_detail="channel payload too large",
        )
        try:
            payload = json.loads(body or b"{}")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid JSON") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="channel payload must be an object")
        encrypt_key = (
            settings.feishu_encrypt_key.get_secret_value() if settings.feishu_encrypt_key else ""
        )
        if not encrypt_key:
            raise HTTPException(status_code=503, detail="Feishu webhook is not configured")
        timestamp = request.headers.get("x-lark-request-timestamp", "")
        nonce = request.headers.get("x-lark-request-nonce", "")
        signature = request.headers.get("x-lark-signature", "")
        if not feishu_timestamp_is_fresh(timestamp) or not verify_feishu_signature(
            timestamp,
            nonce,
            signature,
            encrypt_key,
            body,
        ):
            raise HTTPException(status_code=401, detail="invalid channel signature")
        try:
            payload = decrypt_feishu_payload(payload, encrypt_key)
        except ChannelError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        identity = feishu_event_id(payload) or f"{timestamp}:{nonce}:{signature}"
        replay_token = channel_replays.claim(identity)
        if replay_token is None:
            return {"duplicate": True, "channel": "feishu"}
        challenge = feishu_challenge(payload)
        if challenge:
            return challenge
        if not feishu_should_handle(payload, settings.feishu_bot_open_id):
            return {"ignored": True, "reason": "bot_not_mentioned", "channel": "feishu"}
        try:
            inbound = to_inbound(payload, project_id=request.query_params.get("project_id"))
        except ChannelError as exc:
            channel_replays.release(replay_token)
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        project_id = inbound.project_id
        if not project_id:
            projects = ledger.list_projects()
            if not projects:
                channel_replays.release(replay_token)
                raise HTTPException(
                    status_code=400,
                    detail="no project to attach the channel message",
                )
            project_id = projects[0].id
        try:
            run = await runs.create_and_start(project_id, inbound.text)
        except Exception:
            channel_replays.release(replay_token)
            raise
        return {"run_id": run.id, "project_id": project_id, "channel": inbound.channel}

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
