from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Annotated

import uvicorn
from fastapi import FastAPI, Header, HTTPException, Query, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from sse_starlette.sse import EventSourceResponse

from .agent_core import create_agent_core
from .auth import (
    ROLE_PERMISSIONS,
    AuthAssertionExpiredError,
    AuthAssertionReplayError,
    AuthenticationError,
    AuthNonceStore,
    AuthorizationError,
    InvalidAuthAssertionError,
    PrincipalAuthenticator,
    ProjectAccessGrantRequest,
    ProjectAccessNotFoundError,
    ProjectAccessRecord,
    ProjectAccessStore,
    ProjectPermission,
    validate_local_request_source,
)
from .automation import (
    IntentNotFoundError,
    IntentStore,
    NotificationNotFoundError,
    NotificationStore,
)
from .channel_delivery import (
    ChannelDeliveryService,
    ChannelDeliveryStore,
    ChannelReplyAdapter,
    FeishuReplyAdapter,
)
from .channel_identity import (
    ChannelIdentityAmbiguousError,
    ChannelIdentityConflictError,
    ChannelIdentityError,
    ChannelIdentityIntegrityError,
    ChannelIdentityNotFoundError,
    FeishuIdentityBinding,
    FeishuIdentityBindingCreate,
    FeishuIdentityBindingStore,
    feishu_identity_selector,
)
from .channels import (
    MAX_FEISHU_BODY_BYTES,
    ChannelError,
    ChannelReplayGuard,
    decrypt_feishu_payload,
    feishu_challenge,
    feishu_event_id,
    feishu_reply_target,
    feishu_should_handle,
    feishu_timestamp_is_fresh,
    to_inbound,
    verify_feishu_signature,
)
from .commercial_actions import (
    CommercialActionConflictError,
    CommercialActionIntegrityError,
    CommercialActionNotFoundError,
    CommercialActionPolicyError,
    CommercialActionScopeError,
    CommercialActionService,
)
from .commercial_adapters import HttpsJsonMessageAdapter
from .companion import CompanionStore
from .companion_routes import register_companion_routes, validate_companion_request
from .config import Settings, get_settings
from .connectors import ConnectorNotFoundError, ConnectorRegistry
from .constants import (
    USER_SCOPE_ID,
    AgentTemplate,
    CommercialActionOperation,
    CommercialActionStatus,
    CommercialReviewDecision,
    Dimension,
    EventType,
    EvidenceKind,
    EvidenceStatus,
    ExternalExecutionStatus,
    FeedbackKind,
    MemoryScope,
    MemorySource,
    MemoryStatus,
    MissedRunPolicy,
    PermissionDecision,
    PermissionModePolicy,
    PresentationFrameType,
    PrincipalRole,
    ProjectStage,
    ProviderErrorCode,
    RunMode,
    RunStatus,
    ScheduleKind,
)
from .events import EventPublisher
from .feedback import FeedbackStore
from .feedback_routes import register_feedback_routes
from .grants import GrantNotFoundError, PermissionGrantStore
from .ingest import IngestConfig
from .intent_watcher import StandingIntentWatcher
from .memory import FileMemoryProvider, MemoryNotFoundError, public_memory_item
from .models import (
    AgentCreate,
    AgentProfile,
    AgentUpdate,
    Artifact,
    CommercialActionApplyControlRequest,
    CommercialActionApplyRequest,
    CommercialActionCancelRequest,
    CommercialActionRecord,
    CommercialActionReviewRequest,
    Connector,
    ConnectorUpsert,
    ConstraintAppend,
    Evidence,
    EvidenceStatusChange,
    EvidenceStatusUpdate,
    MemoryCorrection,
    MemoryCreate,
    MemoryDecision,
    MemoryDelete,
    MemoryExpiryUpdate,
    MemoryExportResponse,
    MemoryItem,
    MemoryRetrievalRequest,
    MemoryRetrievalResponse,
    MemoryRollback,
    MemoryTombstone,
    Notification,
    OpportunityProject,
    OrgPolicy,
    PendingPermissionView,
    PermissionGrant,
    PermissionResolution,
    PrincipalContext,
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
    SkillRollbackRequest,
    SkillTestResult,
    SkillVersion,
    SkillVersionResponse,
    StandingIntent,
    StandingIntentCheckResponse,
    StandingIntentCreate,
    ToolReceipt,
    ToolReceiptReplay,
    new_id,
    utc_now,
)
from .permissions import PermissionBroker, PermissionNotFoundError
from .policy import OrgPolicyStore, PolicyDeniedError
from .presentation_tools import PRESENTATION_COMPONENTS, PRESENTATION_SCHEMA_VERSION
from .providers import FileSecretVault, ProviderNotFoundError, ProviderRegistry
from .runs import (
    AgentRuntime,
    RunCancelConflictError,
    RunManager,
    RunResumeConflictError,
    RunRetryConflictError,
)
from .scheduler import ScheduleService, ScheduleValidationError, compute_next_fire, new_schedule
from .sdk_adapter import ClaudeAgentRuntime
from .skills import (
    MAX_SKILL_ARCHIVE_BYTES,
    SkillConflictError,
    SkillIdentifierError,
    SkillIntegrityError,
    SkillNotFoundError,
    SkillRegistry,
)
from .storage import (
    FileLedger,
    LedgerIdentityScopeError,
    LedgerNotFoundError,
    SourceReceiptIntegrityError,
    SourceReceiptNotFoundError,
)
from .telemetry import RunTelemetry, build_run_telemetry
from .templates import TEMPLATE_SPECS

MAX_PROJECT_UPLOAD_BYTES = 50 * 1024 * 1024
LOCAL_OPERATOR_ID = "local-operator"


def _request_principal(request: Request) -> PrincipalContext | None:
    """Return only middleware-verified identity; request fields are never authority."""

    return getattr(request.state, "principal", None)


def _project_route_permission(method: str, path: str) -> ProjectPermission:
    if "/access" in path:
        return ProjectPermission.PROJECT_ACCESS_MANAGE
    if "/commercial-actions" in path:
        if path.endswith("/review"):
            return ProjectPermission.COMMERCIAL_REVIEW
        if path.endswith("/apply"):
            return ProjectPermission.COMMERCIAL_APPLY
        if path.endswith("/cancel"):
            return ProjectPermission.COMMERCIAL_CANCEL
        return ProjectPermission.COMMERCIAL_READ
    if "/source-receipts" in path:
        return ProjectPermission.SOURCE_RECEIPT_READ
    if path.endswith("/runs"):
        return (
            ProjectPermission.RUN_READ
            if method == "GET"
            else ProjectPermission.RUN_CREATE
        )
    return (
        ProjectPermission.PROJECT_READ
        if method in {"GET", "HEAD"}
        else ProjectPermission.PROJECT_WRITE
    )


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
    channel_reply_adapter: ChannelReplyAdapter | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    ledger = FileLedger(settings.data_dir, ingest_config=IngestConfig.from_settings(settings))
    events = EventPublisher(ledger)
    memory = FileMemoryProvider(
        settings.data_dir / "memory",
        agents_root=settings.data_dir / "agents",
    )
    grants = PermissionGrantStore(settings.data_dir / "grants")
    policy = OrgPolicyStore(settings.data_dir / "org" / "policy.json")
    feedback = FeedbackStore(settings.data_dir / "feedback")
    companions = CompanionStore(settings.data_dir / "companion-devices")
    authenticator = PrincipalAuthenticator(
        settings,
        AuthNonceStore(settings.data_dir / "auth" / "nonces"),
    )
    project_access = ProjectAccessStore(settings.data_dir / "auth" / "projects")
    channel_identity_bindings = FeishuIdentityBindingStore(
        settings.data_dir / "channels" / "feishu-bindings",
        project_access,
    )
    channel_replays = ChannelReplayGuard(settings.data_dir / "channels" / "replays.json")
    channel_delivery = ChannelDeliveryService(
        ledger,
        ChannelDeliveryStore(settings.data_dir / "channel-deliveries"),
        channel_reply_adapter or FeishuReplyAdapter(settings),
        enabled=lambda: policy.get().channel_delivery_enabled,
        max_attempts=settings.feishu_delivery_max_attempts,
        retry_base_seconds=settings.feishu_delivery_retry_base_seconds,
        retry_max_seconds=settings.feishu_delivery_retry_max_seconds,
        worker_interval_seconds=settings.feishu_delivery_worker_interval_seconds,
    )
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
    commercial_action_adapter = HttpsJsonMessageAdapter(connectors)
    commercial_actions = CommercialActionService(
        settings.data_dir / "commercial-actions",
        ledger=ledger,
        policy=policy,
        executor=commercial_action_adapter,
        target_reader=commercial_action_adapter,
        access=project_access,
    )
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
        commercial_actions=commercial_actions,
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
        channel_delivery=channel_delivery,
        feedback=feedback,
    )
    scheduler = ScheduleService(
        ledger,
        runs,
        events,
        notifications=notifications,
        access=project_access,
    )
    intent_watcher = StandingIntentWatcher(
        ledger,
        runs,
        intents,
        notifications,
        access=project_access,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        await runs.recover_interrupted()
        feedback.reconcile_terminal(ledger)
        await channel_delivery.start()
        try:
            await scheduler.start()
            try:
                await intent_watcher.start()
                try:
                    yield
                finally:
                    await intent_watcher.stop()
            finally:
                await scheduler.stop()
        finally:
            try:
                await runs.stop()
            finally:
                await channel_delivery.stop()

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
    application.state.intent_watcher = intent_watcher
    application.state.connectors = connectors
    application.state.notifications = notifications
    application.state.intents = intents
    application.state.grants = grants
    application.state.policy = policy
    application.state.feedback = feedback
    application.state.companions = companions
    application.state.authenticator = authenticator
    application.state.project_access = project_access
    application.state.channel_identity_bindings = channel_identity_bindings
    application.state.commercial_actions = commercial_actions
    application.state.commercial_action_adapter = commercial_action_adapter
    application.state.channel_delivery = channel_delivery
    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    def authorize_project_request(
        project_id: str,
        principal: PrincipalContext | None,
        permission: ProjectPermission,
    ) -> None:
        project = ledger.get_project(project_id)
        if principal is None:
            if project.identity_binding != "local_only":
                raise AuthorizationError(
                    "authenticated projects are unavailable in local-only mode"
                )
            return
        if (
            project.identity_binding != "authenticated_principal"
            or project.tenant_id != principal.tenant_id
        ):
            raise AuthorizationError("principal is outside the project tenant")
        project_access.authorize(project_id, principal, permission)

    def require_project_authorization(
        project_id: str,
        principal: PrincipalContext | None,
        permission: ProjectPermission,
    ) -> None:
        try:
            authorize_project_request(project_id, principal, permission)
        except (LedgerNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="Project not found") from exc
        except AuthorizationError as exc:
            raise HTTPException(
                status_code=403,
                detail="principal is not authorized for this resource",
            ) from exc

    def project_is_visible(
        project_id: str,
        principal: PrincipalContext | None,
        permission: ProjectPermission = ProjectPermission.PROJECT_READ,
    ) -> bool:
        try:
            authorize_project_request(project_id, principal, permission)
            return True
        except (AuthorizationError, LedgerNotFoundError, ValueError):
            return False

    def require_memory_authorization(
        item: MemoryItem | MemoryTombstone,
        principal: PrincipalContext | None,
        permission: ProjectPermission,
    ) -> None:
        if principal is None:
            if item.identity_binding != "local_only":
                raise HTTPException(status_code=403, detail="Memory is outside local scope")
            return
        if (
            item.identity_binding != "authenticated_principal"
            or item.tenant_id != principal.tenant_id
        ):
            raise HTTPException(status_code=403, detail="Memory is outside principal scope")
        if item.scope == MemoryScope.PROJECT:
            require_project_authorization(item.scope_id, principal, permission)
            return
        if (
            item.principal_id != principal.principal_id
            or permission not in ROLE_PERMISSIONS[principal.role]
        ):
            raise HTTPException(status_code=403, detail="Memory is outside principal scope")

    def notification_is_visible(
        notification: Notification,
        principal: PrincipalContext | None,
        *,
        write: bool = False,
    ) -> bool:
        if notification.project_id is not None:
            return project_is_visible(
                notification.project_id,
                principal,
                (
                    ProjectPermission.PROJECT_WRITE
                    if write
                    else ProjectPermission.PROJECT_READ
                ),
            )
        return principal is None or principal.role in {
            PrincipalRole.OWNER,
            PrincipalRole.ADMIN,
        }

    register_feedback_routes(
        application, feedback=feedback, ledger=ledger, skills=skills,
        require_project_authorization=require_project_authorization,
        project_is_visible=project_is_visible,
    )
    register_companion_routes(
        application, settings=settings, store=companions, ledger=ledger, runs=runs,
        permissions=permissions, notifications=notifications, access=project_access,
        require_project_authorization=require_project_authorization,
    )

    @application.middleware("http")
    async def authenticate_control_plane(request: Request, call_next):
        path = request.url.path
        if not (path == "/api" or path.startswith("/api/")):
            return await call_next(request)
        # This is a deliberately separate, project-scoped device API. Its cookie
        # is NEVER accepted by the normal control-plane routes below.
        if path == "/api/companion" or path.startswith("/api/companion/"):
            try:
                validate_companion_request(settings, request)
            except HTTPException as exc:
                return JSONResponse(
                    status_code=exc.status_code, content={"detail": exc.detail},
                    headers={"Cache-Control": "no-store"},
                )
            response = await call_next(request)
            response.headers["Cache-Control"] = "no-store"
            response.headers["Referrer-Policy"] = "no-referrer"
            return response
        # Feishu has its own body-bound signature.  In trusted-gateway mode the
        # handler validates it, then refuses dispatch until a durable channel
        # identity mapping exists; it never falls back to an arbitrary project.
        if path == "/api/channels/feishu":
            return await call_next(request)
        if settings.auth_mode == "local_only" and any(
            name in request.headers
            for name in ("forwarded", "x-forwarded-for", "x-real-ip")
        ):
            return JSONResponse(
                status_code=status.HTTP_403_FORBIDDEN,
                content={"detail": "local-only control plane rejects proxied requests"},
            )
        if (
            settings.auth_mode == "local_only" and settings.companion_enabled
            and (request.url.scheme == "https" or any(
                name in request.headers for name in ("x-forwarded-host", "x-forwarded-proto")
            ))
        ):
            return JSONResponse(
                status_code=403,
                content={
                    "detail": "paired local deployments expose only the companion API over HTTPS",
                },
            )
        if settings.auth_mode == "local_only":
            try:
                validate_local_request_source(
                    request.headers.items(),
                    scheme=request.url.scheme,
                    allowed_origins=settings.cors_origins,
                )
            except AuthenticationError:
                return JSONResponse(
                    status_code=status.HTTP_403_FORBIDDEN,
                    content={"detail": "local-only request source is not trusted"},
                )
        raw_path_value = request.scope.get("raw_path")
        try:
            raw_path = (
                bytes(raw_path_value).decode("ascii")
                if raw_path_value is not None
                else path.encode("ascii").decode("ascii")
            )
            principal = authenticator.authenticate(
                request.headers,
                method=request.method,
                path=raw_path,
                client_host=request.client.host if request.client else None,
            )
        except (
            AuthAssertionExpiredError,
            AuthAssertionReplayError,
            InvalidAuthAssertionError,
            UnicodeError,
        ):
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={"detail": "trusted gateway authentication failed"},
            )
        except AuthenticationError:
            return JSONResponse(
                status_code=(
                    status.HTTP_403_FORBIDDEN
                    if settings.auth_mode == "local_only"
                    else status.HTTP_503_SERVICE_UNAVAILABLE
                ),
                content={"detail": "control-plane authentication is unavailable"},
            )
        request.state.principal = principal

        admin_prefixes = (
            "/api/agents",
            "/api/channels/feishu/bindings",
            "/api/connectors",
            "/api/org/",
            "/api/providers",
            "/api/skills",
        )
        if (
            principal is not None
            and request.method not in {"GET", "HEAD", "OPTIONS"}
            and path.startswith(admin_prefixes)
            and principal.role not in {PrincipalRole.OWNER, PrincipalRole.ADMIN}
        ):
            return JSONResponse(
                status_code=status.HTTP_403_FORBIDDEN,
                content={"detail": "tenant administrator authorization is required"},
            )

        # Resolve authorization only from server-owned project/run records.  Route
        # payloads and identity-shaped headers never select the principal.
        segments = [segment for segment in path.split("/") if segment]
        try:
            if len(segments) >= 3 and segments[:2] == ["api", "projects"]:
                project_id = segments[2]
                authorize_project_request(
                    project_id,
                    principal,
                    _project_route_permission(request.method, path),
                )
            elif len(segments) >= 3 and segments[:2] == ["api", "runs"]:
                run = ledger.get_run(segments[2])
                permission = (
                    ProjectPermission.RUN_READ
                    if request.method in {"GET", "HEAD"}
                    else (
                        ProjectPermission.RUN_CREATE
                        if path.endswith("/retry")
                        else ProjectPermission.PROJECT_WRITE
                    )
                )
                authorize_project_request(run.project_id, principal, permission)
        except (LedgerNotFoundError, ValueError):
            # Preserve each route's existing not-found contract.
            pass
        except AuthorizationError:
            return JSONResponse(
                status_code=status.HTTP_403_FORBIDDEN,
                content={"detail": "principal is not authorized for this resource"},
            )
        return await call_next(request)

    @application.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": "ophira-backend"}

    @application.get("/api/runtime/provider")
    async def runtime_provider() -> dict[str, str | bool | None]:
        return settings.provider_status()

    @application.get("/api/auth/status")
    async def authentication_status() -> dict[str, str | bool]:
        return settings.auth_status()

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
            "commercial_action_operations": [
                item.value for item in CommercialActionOperation
            ],
            "commercial_action_statuses": [item.value for item in CommercialActionStatus],
            "commercial_review_decisions": [
                item.value for item in CommercialReviewDecision
            ],
            "external_execution_statuses": [
                item.value for item in ExternalExecutionStatus
            ],
            "presentation_frame_types": [item.value for item in PresentationFrameType],
            "presentation_schema_versions": [PRESENTATION_SCHEMA_VERSION],
            "presentation_components": list(PRESENTATION_COMPONENTS),
        }

    @application.post(
        "/api/projects",
        response_model=OpportunityProject,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_project(
        request: Request,
        payload: ProjectCreate,
    ) -> OpportunityProject:
        principal = _request_principal(request)
        try:
            if principal is None:
                return ledger.create_project(payload)
            project_id = new_id("project")
            project_access.bind_project(project_id, principal)
            try:
                return ledger.create_project(
                    payload,
                    principal=principal,
                    project_id=project_id,
                )
            except Exception:
                # Access is published first so no authenticated project can be
                # observed without an authorization record. Compensate ordinary
                # write failures; a process crash can leave only an unreachable
                # access record, which remains fail-closed.
                project_access.remove_initial_binding(project_id, principal)
                raise
        except (AuthorizationError, LedgerIdentityScopeError) as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    @application.get("/api/projects", response_model=list[OpportunityProject])
    async def list_projects(request: Request) -> list[OpportunityProject]:
        principal = _request_principal(request)
        visible: list[OpportunityProject] = []
        for project in ledger.list_projects():
            try:
                authorize_project_request(
                    project.id,
                    principal,
                    ProjectPermission.PROJECT_READ,
                )
            except (AuthorizationError, ProjectAccessNotFoundError):
                continue
            visible.append(project)
        return visible

    @application.get("/api/projects/{project_id}", response_model=OpportunityProject)
    async def get_project(project_id: str) -> OpportunityProject:
        try:
            return ledger.get_project(project_id)
        except (LedgerNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="Project not found") from exc

    @application.get(
        "/api/projects/{project_id}/access",
        response_model=ProjectAccessRecord,
    )
    async def get_project_access(
        request: Request,
        project_id: str,
    ) -> ProjectAccessRecord:
        if _request_principal(request) is None:
            raise HTTPException(
                status_code=403,
                detail="Project membership is unavailable in local-only mode",
            )
        try:
            return project_access.get(project_id)
        except ProjectAccessNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Project access not found") from exc
        except AuthorizationError as exc:
            raise HTTPException(
                status_code=500,
                detail="Project access integrity validation failed",
            ) from exc

    @application.post(
        "/api/projects/{project_id}/access",
        response_model=ProjectAccessRecord,
    )
    async def grant_project_access(
        request: Request,
        project_id: str,
        payload: ProjectAccessGrantRequest,
    ) -> ProjectAccessRecord:
        principal = _request_principal(request)
        if principal is None:
            raise HTTPException(
                status_code=403,
                detail="Project membership is unavailable in local-only mode",
            )
        try:
            return project_access.grant(
                project_id,
                principal,
                principal_id=payload.principal_id,
                role=payload.role,
            )
        except ProjectAccessNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Project access not found") from exc
        except AuthorizationError as exc:
            raise HTTPException(
                status_code=403,
                detail="principal is not authorized to grant project access",
            ) from exc

    @application.delete(
        "/api/projects/{project_id}/access/{principal_id}",
        response_model=ProjectAccessRecord,
    )
    async def revoke_project_access(
        request: Request,
        project_id: str,
        principal_id: str,
    ) -> ProjectAccessRecord:
        principal = _request_principal(request)
        if principal is None:
            raise HTTPException(
                status_code=403,
                detail="Project membership is unavailable in local-only mode",
            )
        try:
            return project_access.revoke(
                project_id,
                principal,
                principal_id=principal_id,
            )
        except ProjectAccessNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Project access not found") from exc
        except AuthorizationError as exc:
            raise HTTPException(
                status_code=403,
                detail="principal is not authorized to revoke project access",
            ) from exc

    @application.get(
        "/api/projects/{project_id}/commercial-actions",
        response_model=list[CommercialActionRecord],
    )
    async def list_commercial_actions(project_id: str) -> list[CommercialActionRecord]:
        try:
            return commercial_actions.list(project_id)
        except CommercialActionNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except CommercialActionIntegrityError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @application.get(
        "/api/projects/{project_id}/commercial-actions/{action_id}",
        response_model=CommercialActionRecord,
    )
    async def get_commercial_action(
        project_id: str,
        action_id: str,
    ) -> CommercialActionRecord:
        try:
            return commercial_actions.get(project_id, action_id)
        except CommercialActionNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except CommercialActionIntegrityError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @application.post(
        "/api/projects/{project_id}/commercial-actions/{action_id}/review",
        response_model=CommercialActionRecord,
    )
    async def review_commercial_action(
        request: Request,
        project_id: str,
        action_id: str,
        payload: CommercialActionReviewRequest,
    ) -> CommercialActionRecord:
        principal = _request_principal(request)
        try:
            return commercial_actions.review(
                project_id,
                action_id,
                payload,
                principal=principal,
                actor_id=LOCAL_OPERATOR_ID if principal is None else None,
            )
        except CommercialActionNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except CommercialActionScopeError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except CommercialActionPolicyError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except CommercialActionConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except CommercialActionIntegrityError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @application.post(
        "/api/projects/{project_id}/commercial-actions/{action_id}/cancel",
        response_model=CommercialActionRecord,
    )
    async def cancel_commercial_action(
        request: Request,
        project_id: str,
        action_id: str,
        payload: CommercialActionCancelRequest,
    ) -> CommercialActionRecord:
        principal = _request_principal(request)
        try:
            return commercial_actions.cancel(
                project_id,
                action_id,
                payload,
                principal=principal,
                actor_id=LOCAL_OPERATOR_ID if principal is None else None,
            )
        except CommercialActionNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except CommercialActionScopeError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except CommercialActionConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except CommercialActionIntegrityError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @application.post(
        "/api/projects/{project_id}/commercial-actions/{action_id}/apply",
        response_model=CommercialActionRecord,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def apply_commercial_action(
        request: Request,
        project_id: str,
        action_id: str,
        payload: CommercialActionApplyControlRequest,
    ) -> CommercialActionRecord:
        principal = _request_principal(request)
        try:
            action = commercial_actions.get(project_id, action_id)
            internal_request = CommercialActionApplyRequest(
                source_run_id=action.source_run_id,
                source_session_id=action.source_session_id,
                reason=payload.reason,
            )
            return commercial_actions.apply(
                project_id,
                action_id,
                internal_request,
                principal=principal,
                actor_id=LOCAL_OPERATOR_ID if principal is None else None,
            )
        except CommercialActionNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except CommercialActionScopeError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except CommercialActionPolicyError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except CommercialActionConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except CommercialActionIntegrityError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

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
            return await asyncio.to_thread(
                ledger.save_upload, project_id, filename, content, mime=mime,
            )
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

    @application.get(
        "/api/projects/{project_id}/source-receipts",
        response_model=list[ToolReceipt],
    )
    async def list_source_receipts(
        project_id: str,
        run_id: Annotated[str | None, Query(max_length=160)] = None,
    ) -> list[ToolReceipt]:
        try:
            return ledger.list_tool_receipts(project_id, run_id=run_id)
        except (LedgerNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="Project not found") from exc
        except SourceReceiptIntegrityError as exc:
            raise HTTPException(
                status_code=500,
                detail="Source receipt integrity validation failed",
            ) from exc

    @application.get(
        "/api/projects/{project_id}/source-receipts/{receipt_id}",
        response_model=ToolReceiptReplay,
    )
    async def replay_source_receipt(
        project_id: str,
        receipt_id: str,
    ) -> ToolReceiptReplay:
        try:
            return ledger.replay_tool_receipt(project_id, receipt_id)
        except (SourceReceiptNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="Source receipt not found") from exc
        except SourceReceiptIntegrityError as exc:
            raise HTTPException(
                status_code=500,
                detail="Source receipt integrity validation failed",
            ) from exc

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
    async def start_run(
        request: Request,
        project_id: str,
        payload: RunCreate,
    ) -> RunRecord:
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
                principal=_request_principal(request),
            )
        except PolicyDeniedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except RunResumeConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (
            LedgerIdentityScopeError,
            LedgerNotFoundError,
            ProviderNotFoundError,
            ValueError,
        ) as exc:
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

    @application.get("/api/skills", response_model=list[SkillVersionResponse])
    async def list_skills() -> list[SkillVersion]:
        return skills.list()

    @application.post(
        "/api/skills/{skill_id:path}/enable", response_model=SkillVersionResponse
    )
    async def enable_skill(skill_id: str, enabled: bool = True) -> SkillVersion:
        try:
            return skills.set_enabled(skill_id, enabled)
        except SkillIdentifierError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except SkillNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Skill not found") from exc
        except SkillIntegrityError as exc:
            raise HTTPException(
                status_code=500,
                detail="Skill registry integrity validation failed",
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @application.post("/api/skills/{skill_id:path}/test", response_model=SkillTestResult)
    async def test_skill(skill_id: str) -> SkillTestResult:
        try:
            return skills.test(skill_id)
        except SkillIdentifierError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except SkillNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Skill not found") from exc
        except SkillIntegrityError as exc:
            raise HTTPException(
                status_code=500,
                detail="Skill registry integrity validation failed",
            ) from exc

    @application.get(
        "/api/skills/{skill_id:path}/versions",
        response_model=list[SkillVersionResponse],
    )
    async def list_skill_versions(skill_id: str) -> list[SkillVersion]:
        try:
            return skills.versions(skill_id)
        except SkillIdentifierError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except SkillNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Skill history not found") from exc
        except (SkillIntegrityError, ValueError) as exc:
            raise HTTPException(
                status_code=500,
                detail="Skill registry integrity validation failed",
            ) from exc

    @application.post(
        "/api/skills/{skill_id:path}/rollback",
        response_model=SkillVersionResponse,
    )
    async def rollback_skill(
        skill_id: str,
        payload: SkillRollbackRequest,
    ) -> SkillVersion:
        try:
            return skills.rollback(
                skill_id,
                version=payload.version,
                content_hash=payload.content_hash,
            )
        except SkillIdentifierError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except SkillNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Skill version not found") from exc
        except SkillConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (SkillIntegrityError, ValueError) as exc:
            raise HTTPException(
                status_code=500,
                detail="Skill registry integrity validation failed",
            ) from exc

    @application.post(
        "/api/skills/install",
        response_model=SkillVersionResponse,
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
        except SkillConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except SkillIntegrityError as exc:
            raise HTTPException(
                status_code=500,
                detail="Skill registry integrity validation failed",
            ) from exc
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

    @application.get("/api/runs/{run_id}/telemetry", response_model=RunTelemetry)
    async def get_run_telemetry(run_id: str) -> RunTelemetry:
        try:
            run = ledger.get_run(run_id)
            return build_run_telemetry(run, ledger.list_events(run_id))
        except LedgerNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Run not found") from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=500,
                detail="Run telemetry source is inconsistent",
            ) from exc

    @application.post(
        "/api/runs/{run_id}/retry",
        response_model=RunRecord,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def retry_run(
        request: Request,
        run_id: str,
        payload: RunRetryRequest,
    ) -> RunRecord:
        try:
            return await runs.retry(
                run_id,
                payload.strategy,
                principal=_request_principal(request),
            )
        except RunRetryConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (AuthorizationError, PolicyDeniedError, ProviderNotFoundError) as exc:
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
        except RunCancelConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
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
    async def create_memory(
        request: Request,
        payload: MemoryCreate,
    ) -> MemoryItem:
        principal = _request_principal(request)
        if payload.scope == MemoryScope.PROJECT:
            require_project_authorization(
                payload.scope_id,
                principal,
                ProjectPermission.MEMORY_WRITE,
            )
        elif (
            principal is not None
            and ProjectPermission.MEMORY_WRITE not in ROLE_PERMISSIONS[principal.role]
        ):
            raise HTTPException(status_code=403, detail="Memory write is not authorized")
        expires_at = payload.expires_at
        if payload.ttl_seconds is not None:
            expires_at = utc_now() + timedelta(seconds=payload.ttl_seconds)
        try:
            return memory.propose(
                scope=payload.scope,
                scope_id=payload.scope_id,
                text=payload.text,
                kind=payload.kind,
                source=MemorySource.USER,
                sensitivity=payload.sensitivity,
                created_by=LOCAL_OPERATOR_ID,
                principal=principal,
                expires_at=expires_at,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @application.get("/api/memory", response_model=list[MemoryItem])
    async def list_memory(
        request: Request,
        scope: MemoryScope | None = None,
        scope_id: str | None = None,
        status_filter: Annotated[MemoryStatus | None, Query(alias="status")] = None,
    ) -> list[MemoryItem]:
        principal = _request_principal(request)
        visible: list[MemoryItem] = []
        for item in memory.list(scope=scope, scope_id=scope_id, status=status_filter):
            try:
                require_memory_authorization(
                    item,
                    principal,
                    ProjectPermission.MEMORY_READ,
                )
            except HTTPException:
                continue
            visible.append(item)
        return visible

    @application.get("/api/memory/export", response_model=MemoryExportResponse)
    async def export_memory(
        request: Request,
        response: Response,
        scope: MemoryScope | None = None,
        scope_id: str | None = None,
    ) -> MemoryExportResponse:
        if scope_id is not None and scope is None:
            raise HTTPException(status_code=400, detail="scope_id requires scope")
        principal = _request_principal(request)
        if scope == MemoryScope.PROJECT and scope_id is not None:
            require_project_authorization(
                scope_id,
                principal,
                ProjectPermission.MEMORY_READ,
            )
        evaluated_at = utc_now()
        items = []
        for item in memory.list(scope=scope, scope_id=scope_id):
            try:
                require_memory_authorization(
                    item,
                    principal,
                    ProjectPermission.MEMORY_READ,
                )
            except HTTPException:
                continue
            items.append(public_memory_item(item, at=evaluated_at))
        tombstones = []
        for tombstone in memory.list_tombstones(scope=scope, scope_id=scope_id):
            try:
                require_memory_authorization(
                    tombstone,
                    principal,
                    ProjectPermission.MEMORY_READ,
                )
            except HTTPException:
                continue
            tombstones.append(tombstone)
        response.headers["Content-Disposition"] = (
            'attachment; filename="ophira-memory-export.json"'
        )
        return MemoryExportResponse(
            exported_at=evaluated_at,
            exported_by=(principal.principal_id if principal else LOCAL_OPERATOR_ID),
            count=len(items),
            items=items,
            tombstone_count=len(tombstones),
            tombstones=tombstones,
        )

    @application.post("/api/memory/retrieve", response_model=MemoryRetrievalResponse)
    async def retrieve_memory(
        request: Request,
        payload: MemoryRetrievalRequest,
    ) -> MemoryRetrievalResponse:
        principal = _request_principal(request)
        pairs: list[tuple[MemoryScope, str]] = []
        if payload.include_user_scope:
            pairs.append((MemoryScope.USER, USER_SCOPE_ID))
        if payload.project_id is not None:
            require_project_authorization(
                payload.project_id,
                principal,
                ProjectPermission.MEMORY_READ,
            )
            pairs.append((MemoryScope.PROJECT, payload.project_id))
        if payload.agent_id is not None:
            pairs.append((MemoryScope.AGENT, payload.agent_id))
        try:
            return memory.retrieve(
                payload.query,
                pairs=pairs,
                identity=principal,
                limit=payload.limit,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @application.post("/api/memory/{memory_id}/decision", response_model=MemoryItem)
    async def decide_memory(
        request: Request,
        memory_id: str,
        payload: MemoryDecision,
    ) -> MemoryItem:
        try:
            require_memory_authorization(
                memory.get(memory_id),
                _request_principal(request),
                ProjectPermission.MEMORY_WRITE,
            )
            return memory.decide(memory_id, payload.decision, payload.reason)
        except MemoryNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Memory not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @application.patch("/api/memory/{memory_id}", response_model=MemoryItem)
    async def correct_memory(
        request: Request,
        memory_id: str,
        payload: MemoryCorrection,
    ) -> MemoryItem:
        try:
            require_memory_authorization(
                memory.get(memory_id),
                _request_principal(request),
                ProjectPermission.MEMORY_WRITE,
            )
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
    async def rollback_memory(
        request: Request,
        memory_id: str,
        payload: MemoryRollback,
    ) -> MemoryItem:
        try:
            require_memory_authorization(
                memory.get(memory_id),
                _request_principal(request),
                ProjectPermission.MEMORY_WRITE,
            )
            return memory.rollback(memory_id, payload.version)
        except (MemoryNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="Memory version not found") from exc

    @application.patch("/api/memory/{memory_id}/expiry", response_model=MemoryItem)
    async def update_memory_expiry(
        request: Request,
        memory_id: str,
        payload: MemoryExpiryUpdate,
    ) -> MemoryItem:
        try:
            require_memory_authorization(
                memory.get(memory_id),
                _request_principal(request),
                ProjectPermission.MEMORY_WRITE,
            )
            expires_at = payload.expires_at
            if payload.ttl_seconds is not None:
                expires_at = utc_now() + timedelta(seconds=payload.ttl_seconds)
            return memory.set_expiry(memory_id, expires_at)
        except MemoryNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Memory not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @application.delete("/api/memory/{memory_id}", response_model=MemoryTombstone)
    async def delete_memory(
        request: Request,
        memory_id: str,
        payload: MemoryDelete,
    ) -> MemoryTombstone:
        principal = _request_principal(request)
        try:
            require_memory_authorization(
                memory.get(memory_id),
                principal,
                ProjectPermission.MEMORY_WRITE,
            )
            return memory.delete(
                memory_id,
                deleted_by=(principal.principal_id if principal else LOCAL_OPERATOR_ID),
                reason=payload.reason,
            )
        except MemoryNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Memory not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @application.post(
        "/api/schedules",
        response_model=Schedule,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_schedule(
        request: Request,
        payload: ScheduleCreate,
    ) -> Schedule:
        principal = _request_principal(request)
        require_project_authorization(
            payload.project_id,
            principal,
            ProjectPermission.RUN_CREATE,
        )
        try:
            schedule = new_schedule(payload, principal=principal)
        except ScheduleValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        try:
            return ledger.create_schedule(schedule)
        except (LedgerNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="Project not found") from exc

    @application.get("/api/schedules", response_model=list[Schedule])
    async def list_schedules(request: Request) -> list[Schedule]:
        principal = _request_principal(request)
        return [
            schedule
            for schedule in ledger.list_schedules()
            if project_is_visible(schedule.project_id, principal)
        ]

    @application.post("/api/schedules/{schedule_id}/pause", response_model=Schedule)
    async def pause_schedule(request: Request, schedule_id: str) -> Schedule:
        try:
            schedule = ledger.get_schedule(schedule_id)
            require_project_authorization(
                schedule.project_id,
                _request_principal(request),
                ProjectPermission.PROJECT_WRITE,
            )
            return ledger.update_schedule(schedule_id, enabled=False)
        except (LedgerNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="Schedule not found") from exc

    @application.post("/api/schedules/{schedule_id}/resume", response_model=Schedule)
    async def resume_schedule(request: Request, schedule_id: str) -> Schedule:
        try:
            schedule = ledger.get_schedule(schedule_id)
            require_project_authorization(
                schedule.project_id,
                _request_principal(request),
                ProjectPermission.PROJECT_WRITE,
            )
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
        request: Request,
        unread: bool = False,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> list[Notification]:
        principal = _request_principal(request)
        return [
            item
            for item in notifications.list(unread_only=unread, limit=limit)
            if notification_is_visible(item, principal)
        ]

    @application.post("/api/notifications/{notification_id}/read", response_model=Notification)
    async def read_notification(request: Request, notification_id: str) -> Notification:
        try:
            item = notifications.get(notification_id)
            if not notification_is_visible(
                item,
                _request_principal(request),
                write=True,
            ):
                raise HTTPException(status_code=403, detail="Notification is not authorized")
            return notifications.mark_read(notification_id)
        except (NotificationNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="Notification not found") from exc

    @application.post("/api/notifications/read-all")
    async def read_all_notifications(request: Request) -> dict[str, int]:
        principal = _request_principal(request)
        marked = 0
        for item in notifications.list(unread_only=True, limit=10_000):
            if notification_is_visible(item, principal, write=True):
                notifications.mark_read(item.id)
                marked += 1
        return {"marked": marked}

    @application.get("/api/intents", response_model=list[StandingIntent])
    async def list_intents(
        request: Request,
        project_id: str | None = None,
    ) -> list[StandingIntent]:
        principal = _request_principal(request)
        return [
            intent
            for intent in intents.list(project_id=project_id)
            if project_is_visible(intent.project_id, principal)
        ]

    @application.post(
        "/api/intents",
        response_model=StandingIntent,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_intent(
        request: Request,
        payload: StandingIntentCreate,
    ) -> StandingIntent:
        principal = _request_principal(request)
        require_project_authorization(
            payload.project_id,
            principal,
            ProjectPermission.RUN_CREATE,
        )
        try:
            ledger.get_project(payload.project_id)
        except (LedgerNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="Project not found") from exc
        if payload.agent_id is not None:
            try:
                ledger.get_agent(payload.agent_id)
            except (LedgerNotFoundError, ValueError) as exc:
                raise HTTPException(status_code=404, detail="Agent not found") from exc
        if payload.provider_profile_id is not None:
            try:
                providers.get(payload.provider_profile_id)
            except (ProviderNotFoundError, ValueError) as exc:
                raise HTTPException(status_code=404, detail="Provider not found") from exc
        return intents.create(payload, principal=principal)

    @application.post("/api/intents/{intent_id}/pause", response_model=StandingIntent)
    async def pause_intent(request: Request, intent_id: str) -> StandingIntent:
        try:
            intent = intents.get(intent_id)
            require_project_authorization(
                intent.project_id,
                _request_principal(request),
                ProjectPermission.PROJECT_WRITE,
            )
            return intents.set_enabled(intent_id, False)
        except (IntentNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="Intent not found") from exc

    @application.post("/api/intents/{intent_id}/resume", response_model=StandingIntent)
    async def resume_intent(request: Request, intent_id: str) -> StandingIntent:
        try:
            intent = intents.get(intent_id)
            require_project_authorization(
                intent.project_id,
                _request_principal(request),
                ProjectPermission.PROJECT_WRITE,
            )
            return intents.set_enabled(
                intent_id,
                True,
                authorization_source="user_api",
            )
        except IntentNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Intent not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @application.post(
        "/api/intents/{intent_id}/check",
        response_model=StandingIntentCheckResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def check_intent_now(
        request: Request,
        intent_id: str,
    ) -> StandingIntentCheckResponse:
        try:
            intent = intents.get(intent_id)
            require_project_authorization(
                intent.project_id,
                _request_principal(request),
                ProjectPermission.RUN_CREATE,
            )
            dispatch = await intent_watcher.run_now(intent_id)
        except (IntentNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="Intent not found") from exc
        return StandingIntentCheckResponse(
            outcome=dispatch.outcome,
            intent=dispatch.intent,
            run=dispatch.run,
        )

    @application.patch("/api/schedules/{schedule_id}", response_model=Schedule)
    async def patch_schedule(
        request: Request,
        schedule_id: str,
        payload: ScheduleUpdate,
    ) -> Schedule:
        # Keep nested SchedulePermissionRule instances typed; model_dump would turn
        # them into dicts and model_copy(update=...) intentionally skips validation.
        changes = {field: getattr(payload, field) for field in payload.model_fields_set}
        if changes.get("permission_ceiling", ...) is None:
            changes.pop("permission_ceiling", None)
        try:
            schedule = ledger.get_schedule(schedule_id)
            require_project_authorization(
                schedule.project_id,
                _request_principal(request),
                ProjectPermission.PROJECT_WRITE,
            )
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
    async def run_schedule_now(request: Request, schedule_id: str) -> RunRecord:
        try:
            schedule = ledger.get_schedule(schedule_id)
            require_project_authorization(
                schedule.project_id,
                _request_principal(request),
                ProjectPermission.RUN_CREATE,
            )
            return await scheduler.run_now(schedule_id)
        except (AuthorizationError, PolicyDeniedError, ProviderNotFoundError) as exc:
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
    async def list_pending_permissions(request: Request) -> list[PendingPermissionView]:
        principal = _request_principal(request)
        return [
            item
            for item in permissions.list_pending()
            if (
                project_is_visible(item.project_id, principal)
                if item.project_id is not None
                else principal is None
                or principal.role in {PrincipalRole.OWNER, PrincipalRole.ADMIN}
            )
        ]

    @application.get("/api/permissions/grants", response_model=list[PermissionGrant])
    async def list_permission_grants(request: Request) -> list[PermissionGrant]:
        principal = _request_principal(request)
        return [
            grant
            for grant in grants.list()
            if (
                project_is_visible(
                    grant.project_id,
                    principal,
                    ProjectPermission.PROJECT_ACCESS_MANAGE,
                )
                if grant.project_id is not None
                else principal is None
                or principal.role in {PrincipalRole.OWNER, PrincipalRole.ADMIN}
            )
        ]

    @application.post("/api/permissions/grants/{grant_id}/revoke", response_model=PermissionGrant)
    async def revoke_permission_grant(
        request: Request,
        grant_id: str,
    ) -> PermissionGrant:
        try:
            grant = grants.get(grant_id)
            principal = _request_principal(request)
            if grant.project_id is not None:
                require_project_authorization(
                    grant.project_id,
                    principal,
                    ProjectPermission.PROJECT_ACCESS_MANAGE,
                )
            elif principal is not None and principal.role not in {
                PrincipalRole.OWNER,
                PrincipalRole.ADMIN,
            }:
                raise HTTPException(
                    status_code=403,
                    detail="Tenant administrator authorization is required",
                )
            return grants.revoke(grant_id)
        except (GrantNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="Grant not found") from exc

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

    def require_feishu_binding_admin(request: Request) -> PrincipalContext:
        principal = _request_principal(request)
        if principal is None or principal.role not in {
            PrincipalRole.OWNER,
            PrincipalRole.ADMIN,
        }:
            raise HTTPException(
                status_code=403,
                detail="Tenant administrator authorization is required",
            )
        return principal

    @application.get(
        "/api/channels/feishu/bindings",
        response_model=list[FeishuIdentityBinding],
    )
    async def list_feishu_identity_bindings(
        request: Request,
        include_revoked: bool = False,
    ) -> list[FeishuIdentityBinding]:
        principal = require_feishu_binding_admin(request)
        try:
            return channel_identity_bindings.list_for_tenant(
                principal.tenant_id,
                include_revoked=include_revoked,
            )
        except ChannelIdentityIntegrityError as exc:
            raise HTTPException(
                status_code=500,
                detail="Channel identity binding integrity validation failed",
            ) from exc

    @application.post(
        "/api/channels/feishu/bindings",
        response_model=FeishuIdentityBinding,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_feishu_identity_binding(
        request: Request,
        payload: FeishuIdentityBindingCreate,
    ) -> FeishuIdentityBinding:
        principal = require_feishu_binding_admin(request)
        try:
            return channel_identity_bindings.create(payload, actor=principal)
        except (ChannelIdentityConflictError, ChannelIdentityAmbiguousError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except AuthorizationError as exc:
            raise HTTPException(
                status_code=403,
                detail="Principal is not authorized to create this channel binding",
            ) from exc
        except ChannelIdentityError as exc:
            raise HTTPException(
                status_code=500,
                detail="Channel identity binding could not be persisted",
            ) from exc

    @application.get(
        "/api/channels/feishu/bindings/{binding_id}",
        response_model=FeishuIdentityBinding,
    )
    async def get_feishu_identity_binding(
        request: Request,
        binding_id: str,
    ) -> FeishuIdentityBinding:
        principal = require_feishu_binding_admin(request)
        try:
            return channel_identity_bindings.get_for_tenant(
                binding_id,
                principal.tenant_id,
            )
        except ChannelIdentityNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Channel binding not found") from exc
        except ChannelIdentityIntegrityError as exc:
            raise HTTPException(
                status_code=500,
                detail="Channel identity binding integrity validation failed",
            ) from exc

    @application.delete(
        "/api/channels/feishu/bindings/{binding_id}",
        response_model=FeishuIdentityBinding,
    )
    async def revoke_feishu_identity_binding(
        request: Request,
        binding_id: str,
    ) -> FeishuIdentityBinding:
        principal = require_feishu_binding_admin(request)
        try:
            return channel_identity_bindings.revoke(binding_id, actor=principal)
        except ChannelIdentityNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Channel binding not found") from exc
        except AuthorizationError as exc:
            raise HTTPException(
                status_code=403,
                detail="Principal is not authorized to revoke this channel binding",
            ) from exc
        except ChannelIdentityIntegrityError as exc:
            raise HTTPException(
                status_code=500,
                detail="Channel identity binding could not be revoked",
            ) from exc

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
        supplied_event_id = feishu_event_id(payload)
        verified_event_id = supplied_event_id or hashlib.sha256(
            f"{timestamp}\0{nonce}\0{signature}".encode()
        ).hexdigest()
        challenge = feishu_challenge(payload)
        if challenge:
            replay_token = channel_replays.claim(
                f"feishu:challenge:v1\0{verified_event_id}"
            )
            if replay_token is None:
                return {"duplicate": True, "channel": "feishu"}
            return challenge
        if not feishu_should_handle(payload, settings.feishu_bot_open_id):
            return {"ignored": True, "reason": "bot_not_mentioned", "channel": "feishu"}

        resolved_identity = None
        if settings.auth_mode == "trusted_gateway":
            try:
                selector = feishu_identity_selector(payload)
                resolved_identity = channel_identity_bindings.resolve(selector)
            except (ChannelIdentityError, AuthorizationError) as exc:
                raise HTTPException(
                    status_code=503,
                    detail=(
                        "Feishu dispatch requires a valid server-owned "
                        "channel-to-principal/project binding"
                    ),
                ) from exc
        if not policy.get().channel_delivery_enabled:
            raise HTTPException(
                status_code=403,
                detail="Feishu channel delivery is disabled by organization policy",
            )
        if not channel_delivery.configured:
            raise HTTPException(
                status_code=503,
                detail="Feishu outbound reply is not configured",
            )

        if resolved_identity is not None:
            binding = resolved_identity.binding
            project_id = binding.project_id
            principal = resolved_identity.principal
            replay_identity = (
                f"feishu:business:v1\0{binding.tenant_key}\0{binding.chat_id}\0"
                f"{binding.user_open_id}\0{verified_event_id}"
            )
        else:
            principal = None
            project_id = request.query_params.get("project_id")
            if not project_id:
                projects = ledger.list_projects()
                if not projects:
                    raise HTTPException(
                        status_code=400,
                        detail="no project to attach the channel message",
                    )
                project_id = projects[0].id
            replay_identity = f"feishu:business:v1\0local\0{verified_event_id}"

        try:
            reply_target = feishu_reply_target(
                payload,
                verified_event_id=verified_event_id,
            )
            inbound = to_inbound(payload, project_id=project_id)
        except ChannelError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        replay_token = channel_replays.claim(replay_identity)
        if replay_token is None:
            return {"duplicate": True, "channel": "feishu"}

        # From this point onward a durable Run may already exist even if a later
        # write fails. Keep the replay claim on every outcome rather than risk a
        # second Run for one signed Feishu event.
        run = await runs.create_and_start(
            project_id,
            inbound.text,
            channel_reply_target=reply_target,
            principal=principal,
        )
        delivery = channel_delivery.store.get_for_run(run.id)
        if delivery is None:  # pragma: no cover - bind-before-task invariant
            raise HTTPException(
                status_code=500,
                detail="Feishu delivery obligation was not persisted",
            )
        return {
            "run_id": run.id,
            "project_id": project_id,
            "channel": inbound.channel,
            "delivery_id": delivery.id,
            "delivery_status": delivery.status,
        }

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
