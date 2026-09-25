from __future__ import annotations

import ipaddress
import math
import posixpath
import re
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    JsonValue,
    field_validator,
    model_validator,
)

from .constants import (
    CLARIFYING_TOOL,
    DELETE_BUILTIN_TOOLS,
    DESKTOP_TOOL_NAMES,
    SHELL_BUILTIN_TOOLS,
    TOOL_ACT_ON_PAGE,
    AgentTemplate,
    CommercialActionOperation,
    CommercialActionStatus,
    CommercialReviewDecision,
    DecisionStatus,
    Dimension,
    EvidenceKind,
    EvidenceSourceType,
    EvidenceStatus,
    ExternalExecutionStatus,
    FeedbackKind,
    MemoryKind,
    MemoryOrigin,
    MemoryScope,
    MemorySensitivity,
    MemorySource,
    MemoryStatus,
    MissedRunPolicy,
    PermissionDecision,
    PermissionModePolicy,
    PrincipalRole,
    ProjectStage,
    ProviderErrorCode,
    RunInterjectionMode,
    RunMode,
    RunRetryStrategy,
    RunStatus,
    ScheduleKind,
    StageDecisionValue,
    StandingIntentWatcherStatus,
    ValidationStatus,
)
from .project_template_models import ProjectTemplateOrigin


def utc_now() -> datetime:
    return datetime.now(UTC)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def json_values_equal(left: object, right: object) -> bool:
    """Compare JSON values without Python's bool/int type coercion."""

    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left == right
    if type(left) is not type(right):
        return False
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(
            json_values_equal(value, right[key]) for key, value in left.items()
        )
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            json_values_equal(left_value, right_value)
            for left_value, right_value in zip(left, right, strict=True)
        )
    return left == right


def json_value_is_finite(value: object) -> bool:
    """Return false for non-finite numbers anywhere in a JSON value."""

    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return all(json_value_is_finite(item) for item in value)
    if isinstance(value, dict):
        return all(json_value_is_finite(item) for item in value.values())
    return True


class APIModel(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=False)


class PrincipalContext(APIModel):
    """Immutable identity proven by one trusted-gateway request assertion."""

    model_config = ConfigDict(extra="forbid", use_enum_values=False, frozen=True)

    tenant_id: str = Field(
        min_length=1,
        max_length=160,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@+\-]{0,159}$",
    )
    principal_id: str = Field(
        min_length=1,
        max_length=160,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@+\-]{0,159}$",
    )
    role: PrincipalRole
    session_id: str = Field(
        min_length=16,
        max_length=500,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@+\-/]{15,499}$",
    )
    assertion_issued_at: datetime
    assertion_expires_at: datetime
    assertion_nonce: str = Field(
        min_length=20,
        max_length=160,
        pattern=r"^[A-Za-z0-9_-]{20,160}$",
    )
    assertion_version: Literal["trusted_gateway_v1"] = "trusted_gateway_v1"

    @model_validator(mode="after")
    def coherent_assertion_window(self) -> PrincipalContext:
        timestamps = (self.assertion_issued_at, self.assertion_expires_at)
        if any(value.tzinfo is None or value.utcoffset() is None for value in timestamps):
            raise ValueError("principal assertion timestamps must be timezone-aware")
        if self.assertion_expires_at <= self.assertion_issued_at:
            raise ValueError("principal assertion expiry must follow issuance")
        return self


class PrincipalBinding(APIModel):
    """Durable authorization origin for host-managed unattended work."""

    model_config = ConfigDict(extra="forbid", use_enum_values=False, frozen=True)

    tenant_id: str = Field(
        min_length=1,
        max_length=160,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@+\-]{0,159}$",
    )
    principal_id: str = Field(
        min_length=1,
        max_length=160,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@+\-]{0,159}$",
    )
    role: PrincipalRole
    session_id: str = Field(
        min_length=16,
        max_length=500,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@+\-/]{15,499}$",
    )

    @classmethod
    def from_context(cls, principal: PrincipalContext) -> PrincipalBinding:
        return cls(
            tenant_id=principal.tenant_id,
            principal_id=principal.principal_id,
            role=principal.role,
            session_id=principal.session_id,
        )


class ProjectCreate(APIModel):
    name: str = Field(min_length=1, max_length=160)
    objective: str = Field(min_length=1, max_length=4_000)
    product_brief: str = Field(default="", max_length=12_000)
    target_markets: list[str] = Field(default_factory=list, max_length=50)
    constraints: list[str] = Field(default_factory=list, max_length=100)


class Hypothesis(APIModel):
    id: str = Field(default_factory=lambda: new_id("hyp"))
    dimension: Dimension | None = None
    tags: list[str] = Field(default_factory=list, max_length=30)
    statement: str
    validation_criteria: list[str] = Field(default_factory=list)
    priority: int = Field(default=3, ge=1, le=5)
    status: ValidationStatus = ValidationStatus.UNVERIFIED
    evidence_ids: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)


class Evidence(APIModel):
    id: str = Field(default_factory=lambda: new_id("ev"))
    dimension: Dimension | None = None
    tags: list[str] = Field(default_factory=list, max_length=30)
    kind: EvidenceKind
    claim: str
    source_title: str
    source_url: HttpUrl | None = None
    source_type: EvidenceSourceType = EvidenceSourceType.WEB
    source_path: str | None = None
    # External web/tool observations are accepted from the Agent only through a
    # server-issued receipt.  This remains optional on the persistence model so
    # historical project snapshots continue to load; the ledger MCP enforces it
    # for new external observations.
    receipt_id: str | None = Field(
        default=None,
        max_length=200,
        pattern=r"^receipt_[A-Za-z0-9_-]{20,}$",
    )
    retrieved_at: datetime = Field(default_factory=utc_now)
    observed_at: datetime | None = None
    excerpt: str | None = Field(default=None, max_length=8_000)
    location: str | None = Field(default=None, max_length=1_000)
    freshness_note: str | None = Field(default=None, max_length=1_000)
    status: EvidenceStatus = EvidenceStatus.ACTIVE
    confidence: float = Field(ge=0, le=1)
    supports_hypothesis_ids: list[str] = Field(default_factory=list)
    recorded_by: str
    source_run_id: str | None = None
    recorded_at: datetime = Field(default_factory=utc_now)


class ToolReceipt(APIModel):
    """Host-issued provenance for one sanitized external tool observation.

    Historical/local-only records deliberately retain empty identity fields;
    authenticated records inherit tenant/principal/role from the durable Run.
    Project, run, and initialized SDK session remain mandatory and are verified
    by :class:`FileLedger`.
    """

    id: str = Field(
        default_factory=lambda: new_id("receipt"),
        max_length=200,
        pattern=r"^receipt_[A-Za-z0-9_-]{20,}$",
    )
    project_id: str = Field(min_length=1, max_length=200)
    run_id: str = Field(min_length=1, max_length=160)
    session_id: str = Field(min_length=1, max_length=500)
    tenant_id: str | None = Field(default=None, min_length=1, max_length=200)
    principal_id: str | None = Field(default=None, min_length=1, max_length=200)
    principal_role: PrincipalRole | None = None
    principal_session_id: str | None = Field(default=None, min_length=16, max_length=500)
    identity_binding: Literal[
        "project_run_session_only",
        "authenticated_principal",
    ] = "project_run_session_only"
    issuer: Literal["ophira_host"] = "ophira_host"
    tool: str = Field(min_length=1, max_length=300)
    source_type: EvidenceSourceType
    canonical_source: str = Field(min_length=1, max_length=2_000)
    retrieved_at: datetime
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    object_type: str = Field(
        min_length=1,
        max_length=160,
        pattern=r"^[A-Za-z][A-Za-z0-9_.-]{0,159}$",
    )
    object_key: str = Field(min_length=1, max_length=2_000)
    object_version: str = Field(min_length=1, max_length=500)
    object_metadata: dict[str, JsonValue] = Field(default_factory=dict, max_length=50)
    sanitized_payload_ref: str = Field(
        min_length=1,
        max_length=1_000,
        pattern=r"^receipts/[A-Za-z0-9_-]+/receipt_[A-Za-z0-9_-]+/payload\.json$",
    )
    expires_at: datetime
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def coherent_receipt_scope(self) -> ToolReceipt:
        timestamps = (self.retrieved_at, self.expires_at, self.created_at)
        if any(value.tzinfo is None or value.utcoffset() is None for value in timestamps):
            raise ValueError("tool receipt timestamps must be timezone-aware")
        if self.expires_at <= self.retrieved_at or self.created_at < self.retrieved_at:
            raise ValueError("tool receipt timestamps are inconsistent")
        identity_values = (
            self.tenant_id,
            self.principal_id,
            self.principal_role,
            self.principal_session_id,
        )
        authenticated = all(value is not None for value in identity_values)
        if any(value is not None for value in identity_values) and not authenticated:
            raise ValueError("receipt principal identity must be bound together")
        expected_binding = (
            "authenticated_principal" if authenticated else "project_run_session_only"
        )
        if self.identity_binding != expected_binding:
            raise ValueError("tool receipt identity binding is inconsistent")
        if not json_value_is_finite(self.object_metadata):
            raise ValueError("tool receipt metadata requires finite JSON numbers")
        return self


class ToolReceiptReplay(APIModel):
    receipt: ToolReceipt
    payload: JsonValue
    integrity_verified: Literal[True] = True
    expired: bool


class DimensionAssessment(APIModel):
    dimension: Dimension
    status: ValidationStatus = ValidationStatus.UNVERIFIED
    score: int = Field(default=0, ge=0, le=100)
    summary: str = ""
    gaps: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    updated_by: str = "system"
    updated_at: datetime = Field(default_factory=utc_now)


class StageDecision(APIModel):
    id: str = Field(default_factory=lambda: new_id("decision"))
    decision: StageDecisionValue
    current_stage: ProjectStage
    target_stage: ProjectStage
    weakest_dimension: Dimension
    rationale: str
    next_actions: list[str] = Field(default_factory=list)
    next_agent: str | None = None
    made_by: str
    created_at: datetime = Field(default_factory=utc_now)


class Decision(APIModel):
    id: str = Field(default_factory=lambda: new_id("decision"))
    question: str = Field(min_length=1, max_length=4_000)
    outcome: str = Field(min_length=1, max_length=8_000)
    decision_type: Literal["go", "test", "no_go", "ranking", "plan", "other"] = "other"
    rationale: str = Field(default="", max_length=12_000)
    evidence_ids: list[str] = Field(default_factory=list)
    hypothesis_ids: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list, max_length=100)
    conditions: list[str] = Field(default_factory=list, max_length=100)
    next_actions: list[str] = Field(default_factory=list, max_length=100)
    status: DecisionStatus = DecisionStatus.ACTIVE
    made_by: str
    source_run_id: str | None = None
    review_at: datetime | None = None
    invalidated_by_evidence_ids: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)


class Artifact(APIModel):
    id: str = Field(default_factory=lambda: new_id("artifact"))
    project_id: str
    path: str
    name: str
    mime: str = "application/octet-stream"
    version: int = Field(default=1, ge=1)
    run_id: str | None = None
    source_ids: list[str] = Field(default_factory=list)
    checksum: str
    size_bytes: int = Field(ge=0)
    download_url: str
    content_storage: Literal["workspace", "snapshot"] = "workspace"
    parent_artifact_id: str | None = None
    created_by: str | None = None
    edit_request_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class OpportunityProject(APIModel):
    id: str = Field(default_factory=lambda: new_id("project"))
    name: str
    objective: str
    product_brief: str = ""
    template_origin: ProjectTemplateOrigin | None = None
    target_markets: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    tenant_id: str | None = Field(default=None, min_length=1, max_length=160)
    owner_principal_id: str | None = Field(default=None, min_length=1, max_length=160)
    owner_role: PrincipalRole | None = None
    owner_session_id: str | None = Field(default=None, min_length=16, max_length=500)
    identity_binding: Literal["local_only", "authenticated_principal"] = "local_only"
    stage: ProjectStage = ProjectStage.DISCOVERY
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    # Legacy four-view assessments remain readable, but are no longer mandatory project structure.
    assessments: dict[Dimension, DimensionAssessment] = Field(default_factory=dict)
    stage_decisions: list[StageDecision] = Field(default_factory=list)
    decisions: list[Decision] = Field(default_factory=list)
    artifacts: list[Artifact] = Field(default_factory=list)
    revision: int = 1
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def coherent_project_identity(self) -> OpportunityProject:
        values = (
            self.tenant_id,
            self.owner_principal_id,
            self.owner_role,
            self.owner_session_id,
        )
        authenticated = self.identity_binding == "authenticated_principal"
        if authenticated != all(value is not None for value in values):
            raise ValueError("project identity binding is incomplete")
        if not authenticated and any(value is not None for value in values):
            raise ValueError("local-only project cannot claim an authenticated identity")
        if authenticated and self.owner_role != PrincipalRole.OWNER:
            raise ValueError("authenticated project owner role must be owner")
        return self


class ContextRevisionManifest(APIModel):
    """Version vector for the model-visible application context of one run."""

    schema_version: Literal[1] = 1
    project_id: str = Field(min_length=1, max_length=200)
    project_revision: int = Field(ge=1)
    project_section_hashes: dict[str, str] = Field(default_factory=dict, max_length=20)
    agent_id: str | None = Field(default=None, max_length=160)
    agent_revision: int | None = Field(default=None, ge=1)
    agent_core_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    memory_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    skill_snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    compact_epoch: int = Field(default=0, ge=0)
    manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def valid_project_section_hashes(self) -> ContextRevisionManifest:
        if any(
            not name
            or len(name) > 100
            or not re.fullmatch(r"[a-z][a-z0-9_]*", name)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
            for name, digest in self.project_section_hashes.items()
        ):
            raise ValueError("project section hashes must be named lowercase SHA-256 values")
        return self


class ContextRefreshRecord(APIModel):
    """Durable receipt for the context envelope sent at the start of a run."""

    mode: Literal["full", "delta", "reference"]
    base_run_id: str | None = Field(default=None, max_length=160)
    fallback_reason: str | None = Field(default=None, max_length=200)
    changed_domains: list[str] = Field(default_factory=list, max_length=20)
    changed_project_sections: list[str] = Field(default_factory=list, max_length=20)
    manifest: ContextRevisionManifest
    payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    # SHA-256 of the exact UTF-8 bytes appended to the SDK's stable system
    # prompt for this run.  Optional only for pre-migration run snapshots.
    prompt_prefix_hash: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )


class EvidenceStatusUpdate(APIModel):
    status: EvidenceStatus


class EvidenceStatusChange(APIModel):
    evidence: Evidence
    affected_decision_ids: list[str] = Field(default_factory=list)


class RunCreate(APIModel):
    prompt: str = Field(min_length=1, max_length=40_000)
    agent_id: str | None = None
    resume_session_id: str | None = Field(default=None, min_length=1, max_length=500)
    plan_source_run_id: str | None = Field(default=None, min_length=1, max_length=160)
    mode: RunMode = RunMode.STANDARD
    provider_profile_id: str | None = None
    max_budget_usd: float | None = Field(default=None, gt=0, le=1_000)


class RunRetryRequest(APIModel):
    strategy: RunRetryStrategy


class RunInterjectionRequest(APIModel):
    request_id: str = Field(
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    )
    prompt: str = Field(min_length=1, max_length=8_000)
    mode: RunInterjectionMode = RunInterjectionMode.NOW


class RunInterjectionReceipt(APIModel):
    request_id: str
    run_id: str
    prompt: str
    mode: RunInterjectionMode
    status: Literal["queued", "dispatching", "injected", "uncertain", "blocked", "carried"]
    next_run_id: str | None = None


class RunRecord(APIModel):
    id: str = Field(default_factory=lambda: new_id("run"))
    trace_id: str = Field(default_factory=lambda: new_id("trace"))
    project_id: str
    prompt: str
    tenant_id: str | None = Field(default=None, min_length=1, max_length=160)
    principal_id: str | None = Field(default=None, min_length=1, max_length=160)
    principal_role: PrincipalRole | None = None
    principal_session_id: str | None = Field(default=None, min_length=16, max_length=500)
    identity_binding: Literal["local_only", "authenticated_principal"] = "local_only"
    agent_id: str | None = None
    schedule_id: str | None = None
    # Host-bound signed event origin. External payloads never supply this value.
    event_trigger_id: str | None = None
    event_trigger_revision: int | None = Field(default=None, ge=1)
    event_id_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    event_permission_ceiling: list[SchedulePermissionRule] = Field(
        default_factory=list, max_length=100
    )
    # Host fingerprint of the same Schedule snapshot used for the dispatch prompt.
    task_experience_config_hash: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$",
    )
    standing_intent_id: str | None = None
    automation_claim_id: str | None = None
    # Set only by the authenticated companion control plane, never a model/request claim.
    companion_device_id: str | None = Field(default=None, pattern=r"^pair_[a-f0-9]{32}$")
    resume_session_id: str | None = Field(default=None, min_length=1, max_length=500)
    # Resumption is resolved server-side to a durable run. A caller-supplied SDK
    # session ID is a locator, never an authority boundary.
    resume_source_run_id: str | None = Field(default=None, min_length=1, max_length=160)
    carried_interjection_ids: list[str] = Field(default_factory=list, max_length=20)
    # Host-created maintenance run. Generic Run creation never accepts this flag.
    manual_compaction: bool = False
    mode: RunMode = RunMode.STANDARD
    provider_profile_id: str | None = None
    provider_snapshot: dict[str, Any] | None = None
    skill_snapshot: list[dict[str, Any]] = Field(default_factory=list)
    # Reviewed remote MCP authority is pinned for this Run (never credentials).
    mcp_snapshot: list[dict[str, Any]] = Field(default_factory=list, max_length=10)
    # Per-run spend fuse in the operator's configured provider-cost units. The
    # SDK adapter applies the profile's budget_scale before setting max_budget_usd.
    max_budget_usd: float | None = Field(default=None, gt=0, le=1_000)
    status: RunStatus = RunStatus.QUEUED
    sdk_session_id: str | None = None
    # Native SDK deferred calls only. The invocation digest is bound to this
    # Run's immutable identity; raw tool arguments stay in the SDK transcript.
    permission_waits: list[DurablePermissionWait] = Field(default_factory=list)
    suspended_permission_id: str | None = None
    sdk_budget_usd: float | None = Field(default=None, gt=0)
    sdk_turn_limit: int | None = Field(default=None, ge=1)
    sdk_wall_clock_deadline: datetime | None = None
    sdk_permission_mode: str | None = None
    context_refresh: ContextRefreshRecord | None = None
    # Count only confirmed SDK compact_boundary messages. The manifest records
    # the value at injection; a later mismatch forces a full refresh on resume.
    session_compaction_epoch: int = Field(default=0, ge=0)
    error: str | None = None
    error_code: ProviderErrorCode | None = None
    created_at: datetime = Field(default_factory=utc_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @model_validator(mode="after")
    def coherent_run_identity(self) -> RunRecord:
        event_fields = (
            self.event_trigger_id, self.event_trigger_revision, self.event_id_digest,
        )
        if any(value is not None for value in event_fields) != all(
            value is not None for value in event_fields
        ):
            raise ValueError("event run binding is incomplete")
        if self.event_trigger_id is None and self.event_permission_ceiling:
            raise ValueError("ordinary runs cannot carry an event permission ceiling")
        values = (
            self.tenant_id,
            self.principal_id,
            self.principal_role,
            self.principal_session_id,
        )
        authenticated = self.identity_binding == "authenticated_principal"
        if authenticated != all(value is not None for value in values):
            raise ValueError("run identity binding is incomplete")
        if not authenticated and any(value is not None for value in values):
            raise ValueError("local-only run cannot claim an authenticated identity")
        return self


class EventActor(APIModel):
    type: str
    name: str
    agent_id: str | None = None


class EventEnvelope(APIModel):
    id: str
    sequence: int = Field(ge=1)
    type: str
    run_id: str
    project_id: str
    trace_id: str
    sdk_session_id: str | None = None
    provider_profile_id: str | None = None
    timestamp: datetime = Field(default_factory=utc_now)
    actor: EventActor
    parent_id: str | None = None
    tool_use_id: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class RunSummary(APIModel):
    run_id: str
    project_id: str
    trace_id: str
    status: RunStatus
    last_sequence: int = 0
    # Derived index checkpoint, not an event authority. A mismatch repairs the
    # summary from the durable JSONL before another writer allocates a sequence.
    event_log_bytes: int = Field(default=0, ge=0)
    event_counts: dict[str, int] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class PermissionResolution(APIModel):
    decision: PermissionDecision
    reason: str = Field(default="", max_length=2_000)
    # AskUserQuestion 结构化追问的回答：{问题原文: 选项 label 或 label 列表}。
    answers: dict[str, str | list[str]] | None = None
    expires_at: datetime | None = None


class DurablePermissionWait(APIModel):
    id: str = Field(default_factory=lambda: new_id("permission"))
    session_id: str
    tool_use_id: str
    tool_name: str
    input_digest: str
    identity_digest: str
    tool_input: dict[str, Any] = Field(default_factory=dict)
    agent_id: str | None = None
    scope: str
    destructive: bool
    persistable: bool
    state: Literal[
        "preparing", "pending", "resolved", "consumed", "uncertain", "expired", "cancelled"
    ] = "preparing"
    resolution: PermissionResolution | None = None
    actor: EventActor | None = None
    created_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime
    confirmed_at: datetime | None = None
    consumed_at: datetime | None = None


class PendingPermissionView(APIModel):
    id: str
    run_id: str
    project_id: str | None = None
    tool_name: str
    tool_input: dict[str, Any] = Field(default_factory=dict)
    agent_id: str | None = None
    scope: str
    destructive: bool
    persistable: bool
    timeout_seconds: int = 300
    created_at: datetime = Field(default_factory=utc_now)
    durable: bool = False
    state: str = "pending"
    tool_use_id: str | None = None
    expires_at: datetime | None = None


class PermissionGrant(APIModel):
    id: str = Field(default_factory=lambda: new_id("grant"))
    project_id: str | None = None
    tool_name: str
    path_prefix: str = ""
    decision: PermissionDecision = PermissionDecision.ALLOW_PERSISTENT
    expires_at: datetime
    created_at: datetime = Field(default_factory=utc_now)
    revoked: bool = False


class AgentProfile(APIModel):
    id: str = Field(default_factory=lambda: new_id("agent"))
    name: str = Field(min_length=1, max_length=160)
    template: AgentTemplate
    role_description: str = Field(default="", max_length=4_000)
    model: str | None = None
    revision: int = 1
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class AgentCreate(APIModel):
    name: str = Field(min_length=1, max_length=160)
    template: AgentTemplate
    role_description: str = Field(default="", max_length=4_000)
    model: str | None = None


class AgentUpdate(APIModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    role_description: str | None = Field(default=None, max_length=4_000)
    model: str | None = None


class ProviderCapabilities(APIModel):
    tool_use: bool = True
    streaming: bool = True
    images: bool = False
    prompt_caching: bool = False
    extended_thinking: bool = False
    tool_search: bool = False
    # Measured by the contract suite (never copied from vendor docs, PRD §4.9).
    # False means runs on this provider rely on the FR-23 ophira_web fallback.
    native_web_search: bool = False
    native_web_fetch: bool = False
    context_window_tokens: int | None = Field(default=None, ge=1)


class ProviderProfile(APIModel):
    id: str
    display_name: str = Field(min_length=1, max_length=160)
    base_url: str | None = Field(default=None, max_length=2_000)
    auth_env_name: Literal["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"]
    model: str = Field(min_length=1, max_length=300)
    default_haiku_model: str | None = None
    default_sonnet_model: str | None = None
    default_opus_model: str | None = None
    subagent_model: str | None = None
    region: str = Field(default="unspecified", max_length=160)
    production_eligible: bool = False
    terms_checked_at: datetime | None = None
    capabilities: ProviderCapabilities = Field(default_factory=ProviderCapabilities)
    # The SDK prices every run at Anthropic list prices; for third-party endpoints the
    # operator sets this multiplier so max_budget_usd is enforced against real spend.
    budget_scale: float = Field(default=1.0, gt=0, le=10_000)
    compatibility_suite_version: str = "1.0"
    enabled: bool = True
    secret_configured: bool = False
    last_verified_at: datetime | None = None
    last_error_code: ProviderErrorCode | None = None
    last_error_message: str | None = None


class ProviderProfileUpsert(APIModel):
    display_name: str = Field(min_length=1, max_length=160)
    base_url: str | None = Field(default=None, max_length=2_000)
    auth_env_name: Literal["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"]
    model: str = Field(min_length=1, max_length=300)
    secret: str | None = Field(default=None, min_length=1, max_length=8_000)
    default_haiku_model: str | None = None
    default_sonnet_model: str | None = None
    default_opus_model: str | None = None
    subagent_model: str | None = None
    region: str = Field(default="unspecified", max_length=160)
    production_eligible: bool = False
    terms_checked_at: datetime | None = None
    capabilities: ProviderCapabilities = Field(default_factory=ProviderCapabilities)
    budget_scale: float = Field(default=1.0, gt=0, le=10_000)


class CredentialActionRequest(APIModel):
    confirmation: Literal["export", "revoke"]


class ProviderCredentialActionRequest(CredentialActionRequest):
    pass


class ProviderPreflightResult(APIModel):
    provider_profile_id: str
    ok: bool
    error_code: ProviderErrorCode | None = None
    message: str
    sdk_session_id: str | None = None
    model: str | None = None
    mcp_connected: bool = False
    discovered_skills: list[str] = Field(default_factory=list)
    checked_at: datetime = Field(default_factory=utc_now)


class SkillCheck(APIModel):
    name: str
    status: Literal["passed", "warning", "failed"]
    detail: str = ""


class SkillTestResult(APIModel):
    skill_id: str
    ok: bool
    checks: list[SkillCheck] = Field(default_factory=list)
    checked_at: datetime = Field(default_factory=utc_now)


class Connector(APIModel):
    id: str
    # Changes to configuration or credentials invalidate in-flight health probes.
    revision: int = Field(default=0, ge=0)
    type: str = Field(min_length=1, max_length=80)
    display_name: str = Field(min_length=1, max_length=160)
    scope: str = Field(default="", max_length=400)
    # Reference to a credential location (env var name); never the credential itself.
    secret_env: str | None = Field(default=None, max_length=120)
    endpoint: str | None = Field(default=None, max_length=2_000)
    enabled: bool = True
    health: Literal["unknown", "ok", "no_credential", "unreachable"] = "unknown"
    last_checked_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)


class SkillVersionResponse(APIModel):
    """Public Skill metadata; filesystem locations stay server-internal."""

    id: str = Field(min_length=3, max_length=165)
    namespace: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=64)
    description: str = Field(min_length=1, max_length=4_000)
    version: str = Field(min_length=1, max_length=100)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source: Literal["application", "tenant"] = "application"
    enabled: bool = True


class SkillVersion(SkillVersionResponse):
    """Registry-internal Skill metadata including its reviewed package path."""

    path: str = Field(min_length=1, max_length=4_096)


class SkillSelectionToken(APIModel):
    """A complete selection revision, including changes away and back again."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    current_hash: str | None = Field(pattern=r"^[0-9a-f]{64}$")
    enabled: bool | None
    generation: int = Field(ge=0)


class SkillPublicationReceipt(APIModel):
    """Durable proof that one reviewed proposal committed its selection once."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    proposal_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
    skill_id: str = Field(pattern=r"^ophira-tenant:[a-z0-9][a-z0-9_-]{0,63}$")
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    review_revision: int = Field(ge=1)
    reviewer_binding: str = Field(pattern=r"^[0-9a-f]{64}$")
    previous_selection: SkillSelectionToken
    published_at: datetime

    @model_validator(mode="after")
    def timestamp_has_timezone(self) -> SkillPublicationReceipt:
        if self.published_at.utcoffset() is None:
            raise ValueError("publication timestamp must include a timezone")
        return self


class SkillRollbackRequest(APIModel):
    version: str | None = Field(default=None, min_length=1, max_length=100)
    content_hash: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )

    @model_validator(mode="after")
    def selects_one_immutable_version(self) -> SkillRollbackRequest:
        if (self.version is None) == (self.content_hash is None):
            raise ValueError("specify exactly one of version or content_hash")
        return self


class MemoryItem(APIModel):
    id: str = Field(default_factory=lambda: new_id("mem"))
    scope: MemoryScope
    scope_id: str
    tenant_id: str | None = Field(default=None, min_length=1, max_length=160)
    principal_id: str | None = Field(default=None, min_length=1, max_length=160)
    principal_role: PrincipalRole | None = None
    principal_session_id: str | None = Field(default=None, min_length=16, max_length=500)
    identity_binding: Literal["local_only", "authenticated_principal"] = "local_only"
    text: str = Field(min_length=1, max_length=4_000)
    kind: MemoryKind
    source: MemorySource
    origin: MemoryOrigin = MemoryOrigin.AGENT
    source_run_id: str | None = None
    source_anchor: str = Field(default="", max_length=500)
    confidence: float = Field(default=1.0, ge=0, le=1)
    sensitivity: MemorySensitivity = MemorySensitivity.NORMAL
    status: MemoryStatus = MemoryStatus.PROPOSED
    version: int = 1
    created_by: str = Field(default="system", max_length=160)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def coherent_memory_identity(self) -> MemoryItem:
        values = (
            self.tenant_id,
            self.principal_id,
            self.principal_role,
            self.principal_session_id,
        )
        authenticated = self.identity_binding == "authenticated_principal"
        if authenticated != all(value is not None for value in values):
            raise ValueError("memory identity binding is incomplete")
        if not authenticated and any(value is not None for value in values):
            raise ValueError("local-only memory cannot claim an authenticated identity")
        if self.expires_at is not None:
            if self.expires_at.tzinfo is None or self.expires_at.utcoffset() is None:
                raise ValueError("memory expiry must be timezone-aware")
            if self.expires_at <= self.created_at:
                raise ValueError("memory expiry must follow creation")
        return self


class MemoryCreate(APIModel):
    scope: MemoryScope
    scope_id: str = Field(min_length=1, max_length=160)
    text: str = Field(min_length=1, max_length=4_000)
    kind: MemoryKind
    sensitivity: MemorySensitivity = MemorySensitivity.NORMAL
    expires_at: datetime | None = None
    ttl_seconds: int | None = Field(default=None, ge=1, le=315_576_000)

    @model_validator(mode="after")
    def has_one_expiry_source(self) -> MemoryCreate:
        if self.expires_at is not None and self.ttl_seconds is not None:
            raise ValueError("specify expires_at or ttl_seconds, not both")
        if self.expires_at is not None and (
            self.expires_at.tzinfo is None or self.expires_at.utcoffset() is None
        ):
            raise ValueError("memory expiry must be timezone-aware")
        return self


class MemoryDecision(APIModel):
    decision: Literal["approve", "reject", "disable"]
    reason: str = Field(default="", max_length=2_000)


class MemoryCorrection(APIModel):
    text: str = Field(min_length=1, max_length=4_000)
    kind: MemoryKind | None = None
    sensitivity: MemorySensitivity | None = None


class MemoryRollback(APIModel):
    version: int = Field(ge=1)


class MemoryExpiryUpdate(APIModel):
    expires_at: datetime | None = None
    ttl_seconds: int | None = Field(default=None, ge=1, le=315_576_000)
    clear_expiry: bool = False

    @model_validator(mode="after")
    def selects_one_expiry_action(self) -> MemoryExpiryUpdate:
        selected = sum(
            (
                self.expires_at is not None,
                self.ttl_seconds is not None,
                self.clear_expiry,
            )
        )
        if selected != 1:
            raise ValueError("set expires_at, set ttl_seconds, or clear_expiry")
        if self.expires_at is not None and (
            self.expires_at.tzinfo is None or self.expires_at.utcoffset() is None
        ):
            raise ValueError("memory expiry must be timezone-aware")
        return self


class MemoryDelete(APIModel):
    reason: str = Field(min_length=1, max_length=2_000)

    @model_validator(mode="after")
    def normalizes_reason(self) -> MemoryDelete:
        self.reason = " ".join(self.reason.split())
        if not self.reason:
            raise ValueError("memory deletion reason cannot be empty")
        return self


class MemoryTombstone(APIModel):
    """Minimal durable deletion audit; deleted memory text is never retained."""

    schema_version: Literal[1] = 1
    memory_id: str = Field(pattern=r"^mem_[A-Za-z0-9_-]+$")
    scope: MemoryScope
    scope_id: str = Field(min_length=1, max_length=160)
    tenant_id: str | None = Field(default=None, min_length=1, max_length=160)
    principal_id: str | None = Field(default=None, min_length=1, max_length=160)
    identity_binding: Literal["local_only", "authenticated_principal"]
    last_version: int = Field(ge=1)
    last_status: MemoryStatus
    source: MemorySource
    origin: MemoryOrigin
    sensitivity: MemorySensitivity
    source_run_id: str | None = None
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    deleted_by: str = Field(min_length=1, max_length=160)
    reason: str = Field(min_length=1, max_length=2_000)
    deleted_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def coherent_deleted_identity(self) -> MemoryTombstone:
        authenticated = self.identity_binding == "authenticated_principal"
        if authenticated != (self.tenant_id is not None and self.principal_id is not None):
            raise ValueError("memory tombstone identity binding is incomplete")
        if not authenticated and (self.tenant_id is not None or self.principal_id is not None):
            raise ValueError("local-only tombstone cannot claim an authenticated identity")
        if self.deleted_at.tzinfo is None or self.deleted_at.utcoffset() is None:
            raise ValueError("memory deletion time must be timezone-aware")
        return self


class MemoryPublicItem(APIModel):
    """Authorized memory view without the principal-session credential binding."""

    id: str
    scope: MemoryScope
    scope_id: str
    identity_binding: Literal["local_only", "authenticated_principal"]
    tenant_id: str | None = None
    principal_id: str | None = None
    text: str
    kind: MemoryKind
    source: MemorySource
    origin: MemoryOrigin
    source_run_id: str | None = None
    source_anchor: str
    confidence: float
    sensitivity: MemorySensitivity
    status: MemoryStatus
    version: int
    created_by: str
    created_at: datetime
    updated_at: datetime
    expires_at: datetime | None = None
    expired: bool


class MemoryExportResponse(APIModel):
    schema_version: Literal["memory-export-v1"] = "memory-export-v1"
    exported_at: datetime
    exported_by: str
    count: int = Field(ge=0)
    items: list[MemoryPublicItem]
    tombstone_count: int = Field(ge=0)
    tombstones: list[MemoryTombstone]


class MemoryRetrievalRequest(APIModel):
    query: str = Field(min_length=1, max_length=2_000)
    project_id: str | None = Field(default=None, min_length=1, max_length=200)
    agent_id: str | None = Field(default=None, min_length=1, max_length=160)
    include_user_scope: bool = True
    limit: int = Field(default=8, ge=1, le=20)

    @model_validator(mode="after")
    def has_query_and_scope(self) -> MemoryRetrievalRequest:
        self.query = " ".join(self.query.split())
        if not self.query:
            raise ValueError("memory retrieval query cannot be empty")
        if not self.include_user_scope and self.project_id is None and self.agent_id is None:
            raise ValueError("memory retrieval requires at least one scope")
        return self


class MemoryRetrievalHit(APIModel):
    rank: int = Field(ge=1)
    exact_query_match: bool
    matched_terms: list[str]
    term_occurrences: int = Field(ge=1)
    item: MemoryPublicItem


class MemoryRetrievalMetadata(APIModel):
    algorithm: Literal["unicode_nfkc_casefold_all_terms_substring_v1"] = (
        "unicode_nfkc_casefold_all_terms_substring_v1"
    )
    query: str
    normalized_query: str
    terms: list[str]
    requested_scope_pairs: list[str]
    evaluated_at: datetime
    authorized_approved_candidates: int = Field(ge=0)
    excluded_expired: int = Field(ge=0)
    matched: int = Field(ge=0)
    returned: int = Field(ge=0)


class MemoryRetrievalResponse(APIModel):
    metadata: MemoryRetrievalMetadata
    hits: list[MemoryRetrievalHit]


class Notification(APIModel):
    """Inbox entry (FR-15/FR-26): proactive results land here instead of nagging."""

    id: str = Field(default_factory=lambda: new_id("notify"))
    project_id: str | None = None
    run_id: str | None = None
    schedule_id: str | None = None
    intent_id: str | None = None
    severity: Literal["info", "warning", "error"] = "info"
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(default="", max_length=4_000)
    read: bool = False
    created_at: datetime = Field(default_factory=utc_now)


class StandingIntent(APIModel):
    """User-authorized "tell me when X" watcher with an anti-nag budget (§4.16)."""

    id: str = Field(default_factory=lambda: new_id("intent"))
    project_id: str
    principal_binding: PrincipalBinding | None = None
    agent_id: str | None = Field(default=None, max_length=160)
    provider_profile_id: str | None = Field(default=None, max_length=160)
    intent: str = Field(min_length=1, max_length=2_000)
    cooldown_hours: float = Field(default=24, ge=0, le=24 * 30)
    max_triggers: int = Field(default=3, ge=1, le=100)
    expires_at: datetime
    triggered_count: int = 0
    last_triggered_at: datetime | None = None
    check_interval_minutes: int = Field(default=1_440, ge=15, le=7 * 24 * 60)
    check_timeout_seconds: int = Field(default=1_800, ge=60, le=7_200)
    per_check_budget_usd: float = Field(default=0.5, gt=0, le=100)
    total_budget_usd: float = Field(default=15, gt=0, le=10_000)
    # This is a conservative sum of the hard caps assigned to runs that actually
    # started. It is intentionally not presented as an authoritative provider bill.
    budget_committed_usd: float = Field(default=0, ge=0, le=10_000)
    # A short-lived reservation closes the crash window between claiming an intent
    # and durably creating its run. It becomes committed when the run is attached.
    budget_reserved_usd: float = Field(default=0, ge=0, le=100)
    check_count: int = Field(default=0, ge=0)
    next_check_at: datetime | None = None
    last_check_at: datetime | None = None
    last_check_run_id: str | None = Field(default=None, max_length=160)
    active_check_run_id: str | None = Field(default=None, max_length=160)
    active_claim_id: str | None = Field(default=None, max_length=200)
    active_claim_owner: str | None = Field(default=None, max_length=200)
    check_lease_expires_at: datetime | None = None
    consecutive_failures: int = Field(default=0, ge=0)
    last_check_error: str | None = Field(default=None, max_length=1_000)
    last_check_error_at: datetime | None = None
    watcher_status: StandingIntentWatcherStatus = StandingIntentWatcherStatus.IDLE
    enabled: bool = True
    created_by: str = Field(default="user", max_length=160)
    authorization_source: Literal["user_api", "user_approved_agent"] | None = None
    authorized_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def coherent_watcher_envelope(self) -> StandingIntent:
        timestamps = [
            self.expires_at,
            self.created_at,
            self.last_triggered_at,
            self.next_check_at,
            self.last_check_at,
            self.check_lease_expires_at,
            self.last_check_error_at,
            self.authorized_at,
        ]
        if any(
            value is not None and (value.tzinfo is None or value.utcoffset() is None)
            for value in timestamps
        ):
            raise ValueError("standing intent timestamps must be timezone-aware")
        if self.expires_at <= self.created_at:
            raise ValueError("standing intent must expire after it is created")
        if self.total_budget_usd < self.per_check_budget_usd:
            raise ValueError("total budget must cover at least one check")
        if (
            self.budget_committed_usd
            + self.budget_reserved_usd
            > self.total_budget_usd + 1e-9
        ):
            raise ValueError("standing intent budget commitments exceed the total budget")
        active_values = (
            self.active_claim_id,
            self.active_claim_owner,
            self.check_lease_expires_at,
        )
        if any(value is not None for value in active_values) and not all(
            value is not None for value in active_values
        ):
            raise ValueError("standing intent claim metadata is incomplete")
        if self.active_check_run_id is not None and self.active_claim_id is None:
            raise ValueError("standing intent run requires an active claim")
        if self.budget_reserved_usd > 0 and self.active_claim_id is None:
            raise ValueError("standing intent reservation requires an active claim")
        return self


class StandingIntentCreate(APIModel):
    project_id: str = Field(min_length=1, max_length=160)
    agent_id: str | None = Field(default=None, max_length=160)
    provider_profile_id: str | None = Field(default=None, max_length=160)
    intent: str = Field(min_length=1, max_length=2_000)
    cooldown_hours: float = Field(default=24, ge=0, le=24 * 30)
    max_triggers: int = Field(default=3, ge=1, le=100)
    expires_days: int = Field(default=90, ge=1, le=365)
    check_interval_minutes: int = Field(default=1_440, ge=15, le=7 * 24 * 60)
    check_timeout_seconds: int = Field(default=1_800, ge=60, le=7_200)
    per_check_budget_usd: float = Field(default=0.5, gt=0, le=100)
    total_budget_usd: float = Field(default=15, gt=0, le=10_000)

    @model_validator(mode="after")
    def total_budget_covers_one_check(self) -> StandingIntentCreate:
        if self.total_budget_usd < self.per_check_budget_usd:
            raise ValueError("total budget must cover at least one check")
        return self


class StandingIntentCheckResponse(APIModel):
    outcome: Literal[
        "started",
        "busy",
        "paused",
        "backoff",
        "completed",
        "disabled",
        "unauthorized",
        "expired",
        "trigger_exhausted",
        "cooldown",
        "budget_exhausted",
        "claim_expired",
        "not_due",
    ]
    intent: StandingIntent
    run: RunRecord | None = None


class SchedulePermissionRule(APIModel):
    """One exact tool and normalized scope approved for unattended execution."""

    tool_name: str = Field(min_length=1, max_length=300)
    scope: str = Field(min_length=1, max_length=1_000)

    @model_validator(mode="after")
    def normalized_and_persistable(self) -> SchedulePermissionRule:
        if self.tool_name != self.tool_name.strip() or any(
            ord(character) < 32 or character in "*?[]" for character in self.tool_name
        ):
            raise ValueError("schedule permission tool_name must be exact and normalized")
        leaf = self.tool_name.rsplit("__", 1)[-1]
        never_persistable = {
            CLARIFYING_TOOL,
            *SHELL_BUILTIN_TOOLS,
            *DELETE_BUILTIN_TOOLS,
            TOOL_ACT_ON_PAGE.rsplit("__", 1)[-1],
            *(name.rsplit("__", 1)[-1] for name in DESKTOP_TOOL_NAMES),
        }
        if leaf in never_persistable:
            raise ValueError("schedule permission tool is restricted to per-invocation approval")

        if any(ord(character) < 32 for character in self.scope):
            raise ValueError("schedule permission scope contains control characters")
        if self.scope.startswith("path:"):
            path = self.scope.removeprefix("path:")
            if (
                not path
                or path.startswith("/")
                or path != path.strip()
                or path.startswith("~")
                or "\\" in path
                or any(character in path for character in "*?[")
                or ".." in path.split("/")
                or posixpath.normpath(path) != path
            ):
                raise ValueError("schedule permission path scope must be normalized and bounded")
            return self

        if self.scope.startswith("url:"):
            origin = self.scope.removeprefix("url:")
            try:
                parsed = urlsplit(origin)
                if (
                    parsed.scheme not in {"http", "https"}
                    or not parsed.hostname
                    or parsed.username is not None
                    or parsed.password is not None
                    or parsed.path
                    or parsed.query
                    or parsed.fragment
                ):
                    raise ValueError
                hostname = parsed.hostname.rstrip(".").casefold()
                if ":" not in hostname:
                    hostname = hostname.encode("idna").decode("ascii")
                else:
                    hostname = f"[{hostname}]"
                port = parsed.port
            except (UnicodeError, ValueError) as exc:
                raise ValueError(
                    "schedule permission URL scope must be an http(s) origin"
                ) from exc
            default_port = 443 if parsed.scheme == "https" else 80
            port_suffix = f":{port}" if port is not None and port != default_port else ""
            if origin != f"{parsed.scheme}://{hostname}{port_suffix}":
                raise ValueError("schedule permission URL scope must be a normalized origin")
            return self

        raise ValueError("schedule permission scope must start with path: or url:")


class Schedule(APIModel):
    id: str = Field(default_factory=lambda: new_id("schedule"))
    project_id: str
    principal_binding: PrincipalBinding | None = None
    agent_id: str | None = None
    provider_profile_id: str | None = None
    mode: RunMode = RunMode.STANDARD
    kind: ScheduleKind = ScheduleKind.TASK
    prompt: str = Field(min_length=1, max_length=40_000)
    cron: str | None = None
    run_once_at: datetime | None = None
    timezone: str = "UTC"
    missed_run_policy: MissedRunPolicy = MissedRunPolicy.SKIP
    permission_ceiling: list[SchedulePermissionRule] = Field(
        default_factory=list, max_length=100
    )
    enabled: bool = True
    next_fire_at: datetime | None = None
    last_fire_at: datetime | None = None
    last_run_id: str | None = None
    last_result_digest: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class ScheduleUpdate(APIModel):
    """PATCH body: only explicitly sent fields change; cron/run_once_at may be
    cleared with null as long as exactly one trigger remains."""

    prompt: str | None = Field(default=None, min_length=1, max_length=40_000)
    cron: str | None = Field(default=None, max_length=120)
    run_once_at: datetime | None = None
    timezone: str | None = Field(default=None, min_length=1, max_length=64)
    missed_run_policy: MissedRunPolicy | None = None
    provider_profile_id: str | None = None
    mode: RunMode | None = None
    kind: ScheduleKind | None = None
    permission_ceiling: list[SchedulePermissionRule] | None = Field(
        default=None, max_length=100
    )
    enabled: bool | None = None


class ScheduleCreate(APIModel):
    project_id: str = Field(min_length=1, max_length=160)
    agent_id: str | None = None
    provider_profile_id: str | None = None
    mode: RunMode = RunMode.STANDARD
    kind: ScheduleKind = ScheduleKind.TASK
    prompt: str = Field(min_length=1, max_length=40_000)
    cron: str | None = Field(default=None, min_length=9, max_length=120)
    run_once_at: datetime | None = None
    timezone: str = Field(default="UTC", max_length=64)
    missed_run_policy: MissedRunPolicy = MissedRunPolicy.SKIP
    permission_ceiling: list[SchedulePermissionRule] = Field(
        default_factory=list, max_length=100
    )

    @model_validator(mode="after")
    def exactly_one_trigger(self) -> ScheduleCreate:
        if (self.cron is None) == (self.run_once_at is None):
            raise ValueError("exactly one of cron or run_once_at is required")
        return self


TASK_EXPERIENCE_MAX_ENTRIES = 24
TASK_EXPERIENCE_MAX_VERSIONS = 4
TASK_EXPERIENCE_MAX_RUN_WRITES = 8
TASK_EXPERIENCE_CONTEXT_CHARS = 12_000


class TaskExperienceCreate(APIModel):
    """Observations only: scope, authority and provenance are host-owned."""

    key: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9][a-z0-9_.-]*$")
    category: Literal["selector_failure", "threshold_observation", "retrieval_lesson"]
    observation: str = Field(min_length=1, max_length=800)
    lesson: str = Field(min_length=1, max_length=800)
    source_event_ids: list[str] = Field(default_factory=list, max_length=5)

    @model_validator(mode="after")
    def substantive_note(self) -> TaskExperienceCreate:
        if not self.observation.strip() or not self.lesson.strip():
            raise ValueError("experience observation and lesson must not be blank")
        if len(set(self.source_event_ids)) != len(self.source_event_ids):
            raise ValueError("experience source events must be unique")
        if any(len(value) > 200 for value in self.source_event_ids):
            raise ValueError("experience event identifier is too long")
        return self


class TaskExperienceRevision(TaskExperienceCreate):
    version: int = Field(ge=1)
    origin: Literal["agent_observation"] = "agent_observation"
    trust: Literal["untrusted_task_data"] = "untrusted_task_data"
    source_run_id: str
    source_trace_id: str
    source_session_id: str
    write_event_id: str
    source_event_hashes: dict[str, str]
    config_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime = Field(default_factory=utc_now)


class TaskExperienceRetire(APIModel):
    expected_version: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=500)


class TaskExperienceEntry(APIModel):
    id: str
    key: str
    revisions: list[TaskExperienceRevision] = Field(
        min_length=1, max_length=TASK_EXPERIENCE_MAX_VERSIONS
    )
    pruned_versions: int = Field(default=0, ge=0)
    retired_at: datetime | None = None
    retired_by: str | None = None
    retirement_reason: str | None = None


class TaskExperienceLedger(APIModel):
    schema_version: Literal[1] = 1
    project_id: str
    schedule_id: str
    revision: int = Field(default=0, ge=0)
    entries: list[TaskExperienceEntry] = Field(
        default_factory=list, max_length=TASK_EXPERIENCE_MAX_ENTRIES
    )
    evicted_retired_entries: int = Field(default=0, ge=0)


CommercialFieldPath = Annotated[
    str,
    Field(
        min_length=1,
        max_length=519,
        pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}(?:\.[A-Za-z][A-Za-z0-9_-]{0,63}){0,7}$",
    ),
]

CommercialConnectorId = Annotated[
    str,
    Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9_-]+$"),
]


class CommercialActionTarget(APIModel):
    connector_id: CommercialConnectorId
    resource_type: str = Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9_-]+$")
    resource_key: str = Field(min_length=1, max_length=500)
    # A real adapter must compare this opaque version/ETag immediately before commit.
    # For create operations callers use an explicit sentinel such as "absent".
    expected_version: str = Field(min_length=1, max_length=500)


class CommercialFieldChange(APIModel):
    field: CommercialFieldPath
    before: JsonValue
    after: JsonValue

    @model_validator(mode="after")
    def changes_value(self) -> CommercialFieldChange:
        if not json_value_is_finite(self.before) or not json_value_is_finite(self.after):
            raise ValueError("commercial field changes require finite JSON numbers")
        if json_values_equal(self.before, self.after):
            raise ValueError("commercial field change must modify the value")
        return self


class CommercialActionStage(APIModel):
    source_run_id: str = Field(min_length=1, max_length=160)
    source_session_id: str = Field(min_length=1, max_length=500)
    operation: CommercialActionOperation
    target: CommercialActionTarget
    observed_state: dict[str, JsonValue] = Field(default_factory=dict, max_length=200)
    changes: list[CommercialFieldChange] = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=4_000)
    expires_in_seconds: int = Field(default=86_400, ge=60, le=7 * 86_400)

    @model_validator(mode="after")
    def unique_changed_fields(self) -> CommercialActionStage:
        if not json_value_is_finite(self.observed_state):
            raise ValueError("commercial observed state requires finite JSON numbers")
        fields = [change.field for change in self.changes]
        if len(fields) != len(set(fields)):
            raise ValueError("commercial action cannot change the same field twice")
        return self


class CommercialActionReceiptStage(APIModel):
    """Server-scoped staging request hydrated from a trusted target receipt.

    The Agent may choose the proposed changes and explain why, but it cannot
    nominate the operation, connector, resource, external version, or observed
    state.  Those fields are recovered from ``receipt_id`` by trusted host code.
    """

    source_run_id: str = Field(min_length=1, max_length=160)
    source_session_id: str = Field(min_length=1, max_length=500)
    receipt_id: str = Field(
        min_length=20,
        max_length=200,
        pattern=r"^receipt_[A-Za-z0-9_-]{20,}$",
    )
    changes: list[CommercialFieldChange] = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=4_000)
    expires_in_seconds: int = Field(default=86_400, ge=60, le=7 * 86_400)

    @model_validator(mode="after")
    def unique_changed_fields(self) -> CommercialActionReceiptStage:
        fields = [change.field for change in self.changes]
        if len(fields) != len(set(fields)):
            raise ValueError("commercial action cannot change the same field twice")
        return self


class CommercialActionReviewRequest(APIModel):
    decision: CommercialReviewDecision
    reason: str = Field(min_length=1, max_length=4_000)
    valid_for_seconds: int | None = Field(default=None, ge=60, le=7 * 86_400)

    @model_validator(mode="after")
    def approval_has_expiry(self) -> CommercialActionReviewRequest:
        if self.decision == CommercialReviewDecision.APPROVE:
            if self.valid_for_seconds is None:
                raise ValueError("approved commercial actions require valid_for_seconds")
        elif self.valid_for_seconds is not None:
            raise ValueError("rejected commercial actions cannot have an approval expiry")
        return self


class CommercialActionApplyRequest(APIModel):
    source_run_id: str = Field(min_length=1, max_length=160)
    source_session_id: str = Field(min_length=1, max_length=500)
    reason: str = Field(min_length=1, max_length=4_000)


class CommercialActionApplyControlRequest(APIModel):
    """Human control-plane input; source lineage is hydrated by the server."""

    reason: str = Field(min_length=1, max_length=4_000)


class CommercialActionCancelRequest(APIModel):
    """Authenticated human request to withdraw an unexecuted proposal."""

    reason: str = Field(min_length=1, max_length=4_000)


class CommercialActionReview(APIModel):
    decision: CommercialReviewDecision
    actor_id: str = Field(min_length=1, max_length=160)
    tenant_id: str | None = Field(default=None, min_length=1, max_length=160)
    actor_role: PrincipalRole | None = None
    actor_session_id: str | None = Field(default=None, min_length=16, max_length=500)
    reason: str = Field(min_length=1, max_length=4_000)
    decided_at: datetime
    valid_until: datetime | None = None
    # Policy version evaluated for this human decision. Optional only so
    # historical snapshots created before policy-bound review remain readable.
    policy_updated_at: datetime | None = None

    @model_validator(mode="after")
    def coherent_reviewer_identity(self) -> CommercialActionReview:
        values = (self.tenant_id, self.actor_role, self.actor_session_id)
        if any(value is not None for value in values) and not all(
            value is not None for value in values
        ):
            raise ValueError("commercial reviewer identity is incomplete")
        if self.policy_updated_at is not None and (
            self.policy_updated_at.tzinfo is None
            or self.policy_updated_at.utcoffset() is None
        ):
            raise ValueError("commercial review policy time must be timezone-aware")
        return self


class CommercialActionAuditEntry(APIModel):
    event: Literal[
        "staged",
        "approved",
        "rejected",
        "cancelled",
        "expired",
        "execution_requested",
        "execution_started",
        "execution_retryable_failed",
        "execution_failed",
        "applied",
        "apply_idempotent",
    ]
    actor_id: str = Field(min_length=1, max_length=160)
    reason: str = Field(min_length=1, max_length=4_000)
    recorded_at: datetime


class CommercialNumericLimit(APIModel):
    field: CommercialFieldPath
    minimum: float | None = Field(default=None, allow_inf_nan=False)
    maximum: float | None = Field(default=None, allow_inf_nan=False)

    @model_validator(mode="after")
    def valid_range(self) -> CommercialNumericLimit:
        if self.minimum is None and self.maximum is None:
            raise ValueError("commercial numeric limit requires a minimum or maximum")
        if (
            self.minimum is not None
            and self.maximum is not None
            and self.minimum > self.maximum
        ):
            raise ValueError("commercial numeric limit minimum exceeds maximum")
        return self


class CommercialExecutionReceipt(APIModel):
    """Server-issued proof of a successful idempotent external commit.

    The response payload is deliberately represented only by a canonical hash;
    remote bodies may contain untrusted or sensitive fields and are never copied
    into the project ledger.
    """

    id: str = Field(
        max_length=200,
        pattern=r"^external_receipt_[A-Za-z0-9_-]{40,}$",
    )
    action_id: str = Field(min_length=1, max_length=200)
    execution_id: str = Field(min_length=1, max_length=200)
    connector_id: CommercialConnectorId
    operation: CommercialActionOperation
    idempotency_key: str = Field(min_length=40, max_length=200)
    remote_action_id: str = Field(min_length=1, max_length=500)
    remote_version: str | None = Field(default=None, min_length=1, max_length=500)
    http_status: int = Field(ge=200, le=299)
    response_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    adapter_attempts: int = Field(ge=1, le=100)
    issued_at: datetime

    @model_validator(mode="after")
    def timezone_aware_issue_time(self) -> CommercialExecutionReceipt:
        if self.issued_at.tzinfo is None or self.issued_at.utcoffset() is None:
            raise ValueError("commercial execution receipt time must be timezone-aware")
        return self


class CommercialExecutionRequest(APIModel):
    id: str = Field(max_length=200, pattern=r"^execution_[A-Za-z0-9_-]{40,}$")
    action_id: str = Field(min_length=1, max_length=200)
    project_id: str = Field(min_length=1, max_length=200)
    source_run_id: str = Field(min_length=1, max_length=160)
    source_session_id: str = Field(min_length=1, max_length=500)
    requested_by: str = Field(min_length=1, max_length=160)
    requested_tenant_id: str | None = Field(default=None, min_length=1, max_length=160)
    requested_role: PrincipalRole | None = None
    requested_session_id: str | None = Field(default=None, min_length=16, max_length=500)
    request_reason: str = Field(min_length=1, max_length=4_000)
    requested_at: datetime
    projected_state: dict[str, JsonValue]
    expected_target_version: str
    project_revision_checked: int = Field(ge=1)
    project_constraints_checked: list[str] = Field(default_factory=list)
    org_policy_updated_at: datetime
    external_status: ExternalExecutionStatus = ExternalExecutionStatus.NOT_EXECUTED
    adapter_required: Literal[True] = True
    adapter_type: str | None = Field(default=None, max_length=80)
    # New requests copy the preview's server-issued stable key. Keeping this
    # optional lets historical snapshots reload; the service deterministically
    # falls back to the execution ID for those records.
    idempotency_key: str | None = Field(default=None, min_length=40, max_length=200)
    attempt_count: int = Field(default=0, ge=0, le=10_000)
    attempt_started_at: datetime | None = None
    attempt_lease_until: datetime | None = None
    retry_not_before: datetime | None = None
    last_error_code: str | None = Field(default=None, max_length=120)
    last_error_message: str | None = Field(default=None, max_length=1_000)
    receipt: CommercialExecutionReceipt | None = None
    adapter_preconditions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def finite_projected_state(self) -> CommercialExecutionRequest:
        if not json_value_is_finite(self.projected_state):
            raise ValueError("commercial projected state requires finite JSON numbers")
        identity_values = (
            self.requested_tenant_id,
            self.requested_role,
            self.requested_session_id,
        )
        if any(value is not None for value in identity_values) and not all(
            value is not None for value in identity_values
        ):
            raise ValueError("commercial execution requester identity is incomplete")
        timestamps = (
            self.requested_at,
            self.attempt_started_at,
            self.attempt_lease_until,
            self.retry_not_before,
        )
        if any(
            value is not None and (value.tzinfo is None or value.utcoffset() is None)
            for value in timestamps
        ):
            raise ValueError("commercial execution timestamps must be timezone-aware")
        if self.external_status == ExternalExecutionStatus.NOT_EXECUTED:
            if self.receipt is not None:
                raise ValueError("an unexecuted request cannot contain a receipt")
        elif self.external_status == ExternalExecutionStatus.ATTEMPTING:
            if (
                self.attempt_count < 1
                or self.attempt_started_at is None
                or self.attempt_lease_until is None
            ):
                raise ValueError("an active execution requires an attempt lease")
            if self.receipt is not None:
                raise ValueError("an active execution cannot contain a receipt")
        elif self.external_status == ExternalExecutionStatus.RETRYABLE_FAILED:
            if self.attempt_count < 1 or self.retry_not_before is None:
                raise ValueError("a retryable execution failure requires a retry time")
            if self.receipt is not None:
                raise ValueError("a retryable failure cannot contain a receipt")
        elif self.external_status == ExternalExecutionStatus.SUCCEEDED:
            if self.receipt is None:
                raise ValueError("a successful execution requires a server receipt")
            if self.last_error_code is not None or self.last_error_message is not None:
                raise ValueError("a successful execution cannot retain an error")
        elif self.external_status == ExternalExecutionStatus.TERMINAL_FAILED:
            if not self.last_error_code or not self.last_error_message:
                raise ValueError("a terminal execution failure requires an error")
            if self.receipt is not None:
                raise ValueError("a terminal failure cannot contain a receipt")
        if self.receipt is not None:
            if (
                self.receipt.action_id != self.action_id
                or self.receipt.execution_id != self.id
                or self.receipt.idempotency_key != (self.idempotency_key or self.id)
            ):
                raise ValueError("commercial execution receipt scope does not match")
        return self


class CommercialActionRecord(APIModel):
    id: str = Field(max_length=200, pattern=r"^action_[A-Za-z0-9_-]{40,}$")
    project_id: str = Field(min_length=1, max_length=200)
    source_run_id: str = Field(min_length=1, max_length=160)
    source_session_id: str = Field(min_length=1, max_length=500)
    staged_by: str = Field(min_length=1, max_length=160)
    tenant_id: str | None = Field(default=None, min_length=1, max_length=160)
    staged_role: PrincipalRole | None = None
    staged_principal_session_id: str | None = Field(
        default=None,
        min_length=16,
        max_length=500,
    )
    identity_binding: Literal["local_only", "authenticated_principal"] = "local_only"
    # Issued with the immutable preview, before human review. Historical
    # snapshots may omit it; those retain their execution ID as the fallback.
    idempotency_key: str | None = Field(default=None, min_length=40, max_length=200)
    # New production actions are hydrated from this server-issued connector
    # observation.  ``None`` is retained only for historical/local-only records.
    source_receipt_id: str | None = Field(
        default=None,
        min_length=20,
        max_length=200,
        pattern=r"^receipt_[A-Za-z0-9_-]{20,}$",
    )
    source_connector_config_hash: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    operation: CommercialActionOperation
    target: CommercialActionTarget
    observed_state: dict[str, JsonValue]
    changes: list[CommercialFieldChange]
    reason: str
    project_revision_at_stage: int = Field(ge=1)
    # Version of the organization policy visible when the immutable preview was
    # staged. Optional only for historical snapshots.
    staged_policy_updated_at: datetime | None = None
    status: CommercialActionStatus = CommercialActionStatus.STAGED
    review: CommercialActionReview | None = None
    execution_request: CommercialExecutionRequest | None = None
    history: list[CommercialActionAuditEntry] = Field(default_factory=list)
    created_at: datetime
    expires_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def coherent_transition_state(self) -> CommercialActionRecord:
        if not json_value_is_finite(self.observed_state):
            raise ValueError("commercial observed state requires finite JSON numbers")
        if (self.source_receipt_id is None) != (
            self.source_connector_config_hash is None
        ):
            raise ValueError("commercial source receipt binding is incomplete")
        identity_values = (
            self.tenant_id,
            self.staged_role,
            self.staged_principal_session_id,
        )
        authenticated = self.identity_binding == "authenticated_principal"
        if authenticated != all(value is not None for value in identity_values):
            raise ValueError("commercial action identity binding is incomplete")
        if not authenticated and any(value is not None for value in identity_values):
            raise ValueError("local-only action cannot claim an authenticated identity")
        if authenticated and self.staged_by in {"agent", "main-agent", "system"}:
            raise ValueError("authenticated commercial action requires a human staging identity")
        if self.expires_at <= self.created_at or self.updated_at < self.created_at:
            raise ValueError("commercial action timestamps are inconsistent")
        if self.staged_policy_updated_at is not None and (
            self.staged_policy_updated_at.tzinfo is None
            or self.staged_policy_updated_at.utcoffset() is None
        ):
            raise ValueError("commercial staged policy time must be timezone-aware")
        execution = self.execution_request
        if execution is not None:
            if (
                execution.action_id != self.id
                or execution.project_id != self.project_id
                or execution.source_run_id != self.source_run_id
                or execution.source_session_id != self.source_session_id
            ):
                raise ValueError("execution request scope does not match its staged action")
            if self.idempotency_key is not None and (
                execution.idempotency_key != self.idempotency_key
            ):
                raise ValueError("execution idempotency key does not match its preview")
            if execution.receipt is not None and (
                execution.receipt.connector_id != self.target.connector_id
                or execution.receipt.operation != self.operation
            ):
                raise ValueError("execution receipt target does not match its staged action")
        if self.status == CommercialActionStatus.STAGED:
            if self.review is not None or self.execution_request is not None:
                raise ValueError("staged commercial action cannot contain review or execution")
        elif self.status == CommercialActionStatus.APPROVED:
            if (
                self.review is None
                or self.review.decision != CommercialReviewDecision.APPROVE
                or self.review.valid_until is None
                or self.execution_request is not None
            ):
                raise ValueError("approved commercial action has inconsistent review state")
        elif self.status == CommercialActionStatus.REJECTED:
            if (
                self.review is None
                or self.review.decision != CommercialReviewDecision.REJECT
                or self.execution_request is not None
            ):
                raise ValueError("rejected commercial action has inconsistent review state")
        elif self.status == CommercialActionStatus.CANCELLED:
            if self.review is not None and (
                self.review.decision != CommercialReviewDecision.APPROVE
                or self.review.valid_until is None
            ):
                raise ValueError("cancelled commercial action has inconsistent review state")
            if self.execution_request is not None and (
                self.review is None
                or self.execution_request.external_status
                != ExternalExecutionStatus.NOT_EXECUTED
            ):
                raise ValueError(
                    "cancelled commercial action has an unsafe external execution state"
                )
        elif self.status == CommercialActionStatus.EXECUTION_REQUESTED:
            if (
                self.review is None
                or self.review.decision != CommercialReviewDecision.APPROVE
                or self.execution_request is None
            ):
                raise ValueError("execution request must derive from an approval")
            execution = self.execution_request
            if execution.external_status in {
                ExternalExecutionStatus.SUCCEEDED,
                ExternalExecutionStatus.TERMINAL_FAILED,
            }:
                raise ValueError("final external state requires a final action status")
        elif self.status == CommercialActionStatus.APPLIED:
            if (
                self.review is None
                or self.review.decision != CommercialReviewDecision.APPROVE
                or self.execution_request is None
                or self.execution_request.external_status
                != ExternalExecutionStatus.SUCCEEDED
            ):
                raise ValueError("applied commercial action requires a successful receipt")
        elif self.status == CommercialActionStatus.EXECUTION_FAILED:
            if (
                self.review is None
                or self.review.decision != CommercialReviewDecision.APPROVE
                or self.execution_request is None
                or self.execution_request.external_status
                != ExternalExecutionStatus.TERMINAL_FAILED
            ):
                raise ValueError("failed commercial action requires a terminal failure")
        elif self.execution_request is not None:
            raise ValueError("expired commercial action cannot contain an execution request")
        return self


class OrgPolicy(APIModel):
    permission_mode: PermissionModePolicy = PermissionModePolicy.DEFAULT
    allowed_provider_ids: list[str] = Field(default_factory=list)
    allowed_skill_ids: list[str] = Field(default_factory=list)
    remote_mcp_enabled: bool = True
    # Custom hosts require explicit organization approval. The built-in catalog
    # has its own narrow, reviewed host list.
    allowed_mcp_hosts: list[str] = Field(default_factory=list, max_length=100)
    browser_enabled: bool = True
    computer_control_enabled: bool = False
    # Native L3 never targets arbitrary installed apps. Web browsers use L1/L2
    # where URL/domain checks are available; this is an exact native-app list.
    allowed_desktop_bundle_ids: list[str] = Field(default_factory=list, max_length=50)
    channel_delivery_enabled: bool = False
    domain_allowlist: list[str] = Field(default_factory=list)
    # High-consequence commerce commits are default-deny. Enabling the feature still
    # requires explicit operation and connector allowlists.
    commercial_actions_enabled: bool = False
    allowed_commercial_operations: list[CommercialActionOperation] = Field(
        default_factory=list, max_length=20
    )
    allowed_commercial_connector_ids: list[CommercialConnectorId] = Field(
        default_factory=list, max_length=100
    )
    commercial_max_changes_per_action: int = Field(default=20, ge=1, le=100)
    commercial_forbidden_fields: list[CommercialFieldPath] = Field(
        default_factory=list, max_length=100
    )
    commercial_numeric_limits: list[CommercialNumericLimit] = Field(
        default_factory=list, max_length=100
    )
    updated_at: datetime = Field(default_factory=utc_now)

    @field_validator("allowed_mcp_hosts")
    @classmethod
    def validate_mcp_hosts(cls, hosts: list[str]) -> list[str]:
        normalized = [host.strip().rstrip(".").casefold() for host in hosts]
        pattern = re.compile(
            r"(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
            r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
        )
        if len(normalized) != len(set(normalized)):
            raise ValueError("remote MCP host allowlist contains duplicates")
        for host in normalized:
            if not pattern.fullmatch(host):
                raise ValueError("remote MCP allowlist requires exact DNS hostnames")
            try:
                ipaddress.ip_address(host)
            except ValueError:
                pass
            else:
                raise ValueError("remote MCP allowlist requires DNS hostnames")
        return normalized

    @field_validator("allowed_desktop_bundle_ids")
    @classmethod
    def validate_desktop_bundle_ids(cls, values: list[str]) -> list[str]:
        normalized = [value.strip().casefold() for value in values]
        if len(normalized) != len(set(normalized)):
            raise ValueError("desktop app allowlist contains duplicates")
        pattern = re.compile(r"(?=.{3,200}$)(?:[a-z0-9][a-z0-9-]*\.)+[a-z0-9][a-z0-9-]*")
        for bundle_id in normalized:
            if not pattern.fullmatch(bundle_id):
                raise ValueError("desktop app allowlist requires exact bundle identifiers")
        return normalized

    @model_validator(mode="after")
    def unique_commercial_policy_entries(self) -> OrgPolicy:
        operation_values = list(self.allowed_commercial_operations)
        if len(operation_values) != len(set(operation_values)):
            raise ValueError("commercial operation allowlist contains duplicates")
        if len(self.allowed_commercial_connector_ids) != len(
            set(self.allowed_commercial_connector_ids)
        ):
            raise ValueError("commercial connector allowlist contains duplicates")
        limit_fields = [limit.field for limit in self.commercial_numeric_limits]
        if len(limit_fields) != len(set(limit_fields)):
            raise ValueError("commercial numeric limits contain duplicate fields")
        return self


FeedbackStatus = Literal["open", "reviewed", "folded_into_skill", "rejected"]


class FeedbackContext(APIModel):
    """Server-captured anchor, never supplied by a feedback caller."""

    trace_id: str
    run_status: RunStatus
    event_id: str | None = None
    event_type: str | None = None
    message_text: str | None = Field(default=None, max_length=20_000)
    evidence: Evidence | None = None
    captured_at: datetime = Field(default_factory=utc_now)


class FeedbackReview(APIModel):
    expected_revision: int = Field(ge=1)
    status: FeedbackStatus
    note: str = Field(min_length=1, max_length=4_000)
    skill_id: str | None = Field(default=None, max_length=200)
    skill_content_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    regression_report: str | None = Field(default=None, max_length=1_000)
    manual_skill_update_confirmed: bool = False
    l2_regression_passed_confirmed: bool = False

    @model_validator(mode="after")
    def justified_review(self) -> FeedbackReview:
        self.note = self.note.strip()
        if not self.note:
            raise ValueError("review requires a written reason")
        references = (self.skill_id, self.skill_content_hash, self.regression_report)
        if self.status == "folded_into_skill":
            if not all(value and value.strip() for value in references) or not (
                self.manual_skill_update_confirmed and self.l2_regression_passed_confirmed
            ):
                raise ValueError("Skill folding requires manual change and L2 report attestations")
        elif any(value is not None for value in references) or (
            self.manual_skill_update_confirmed or self.l2_regression_passed_confirmed
        ):
            raise ValueError("Skill attestations are only valid when folding into a Skill")
        return self


class FeedbackReviewRecord(APIModel):
    from_status: FeedbackStatus
    to_status: FeedbackStatus
    note: str
    reviewed_at: datetime = Field(default_factory=utc_now)
    reviewer_tenant_id: str | None = None
    reviewer_principal_id: str | None = None
    reviewer_role: PrincipalRole | None = None
    # A human attestation is not an independently verified release gate.
    skill_id: str | None = None
    skill_content_hash: str | None = None
    regression_report: str | None = None
    manual_skill_update_confirmed: bool = False
    l2_regression_passed_confirmed: bool = False


class FeedbackItem(APIModel):
    id: str = Field(default_factory=lambda: new_id("feedback"))
    project_id: str | None = None
    run_id: str | None = None
    evidence_id: str | None = None
    message_id: str | None = None
    context: FeedbackContext | None = None
    source: Literal["user", "system"] = "user"
    # This denotes an UNLABELED sampling candidate, not a gold label or verdict.
    candidate_state: Literal["unlabeled"] = "unlabeled"
    tenant_id: str | None = Field(default=None, min_length=1, max_length=160)
    principal_id: str | None = Field(default=None, min_length=1, max_length=160)
    principal_role: PrincipalRole | None = None
    principal_session_id: str | None = Field(default=None, min_length=16, max_length=500)
    identity_binding: Literal["local_only", "authenticated_principal"] = "local_only"
    kind: FeedbackKind
    user_note: str = Field(min_length=1, max_length=4_000)
    status: FeedbackStatus = "open"
    revision: int = Field(default=1, ge=1)
    reviews: list[FeedbackReviewRecord] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def coherent_feedback_identity(self) -> FeedbackItem:
        values = (
            self.tenant_id,
            self.principal_id,
            self.principal_role,
            self.principal_session_id,
        )
        authenticated = self.identity_binding == "authenticated_principal"
        if authenticated != all(value is not None for value in values):
            raise ValueError("feedback identity binding is incomplete")
        if not authenticated and any(value is not None for value in values):
            raise ValueError("local-only feedback cannot claim authenticated identity")
        return self


class FeedbackCreate(APIModel):
    project_id: str | None = Field(default=None, min_length=1, max_length=200)
    run_id: str | None = Field(default=None, min_length=1, max_length=200)
    evidence_id: str | None = Field(default=None, min_length=1, max_length=200)
    # Persisted assistant.message.completed event ID, not arbitrary client text.
    message_id: str | None = Field(default=None, min_length=1, max_length=250)
    kind: FeedbackKind
    user_note: str = Field(min_length=1, max_length=4_000)

    @model_validator(mode="after")
    def anchored_feedback(self) -> FeedbackCreate:
        self.user_note = self.user_note.strip()
        if not self.user_note:
            raise ValueError("feedback requires a written reason")
        if self.kind == FeedbackKind.CONCLUSION_WRONG:
            if not self.run_id or not self.message_id or self.evidence_id is not None:
                raise ValueError("conclusion feedback requires a run and assistant message anchor")
        elif self.kind == FeedbackKind.EVIDENCE_ISSUE:
            if not self.evidence_id or not (self.project_id or self.run_id):
                raise ValueError("evidence feedback requires an evidence and project/run anchor")
            if self.message_id is not None:
                raise ValueError("evidence feedback cannot claim a message anchor")
        elif not self.run_id or self.message_id is not None or self.evidence_id is not None:
            raise ValueError("run-failure feedback requires only a run anchor")
        return self


class ConnectorUpsert(APIModel):
    type: str = Field(min_length=1, max_length=80)
    display_name: str = Field(min_length=1, max_length=160)
    scope: str = Field(default="", max_length=400)
    secret_env: str | None = Field(default=None, max_length=120)
    endpoint: str | None = Field(default=None, max_length=2_000)
    secret: str | None = Field(default=None, min_length=1, max_length=8_000)


class ConstraintAppend(APIModel):
    constraints: list[str] = Field(min_length=1, max_length=20)


class ChannelInbound(APIModel):
    channel: Literal["feishu", "dingtalk", "wecom", "wechat", "webhook"] = "webhook"
    text: str = Field(min_length=1, max_length=40_000)
    project_id: str | None = None
    sender: str = Field(default="channel", max_length=160)
