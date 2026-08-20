from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

from .constants import (
    AgentTemplate,
    DecisionStatus,
    Dimension,
    EvidenceKind,
    EvidenceSourceType,
    EvidenceStatus,
    MemoryKind,
    MemoryScope,
    MemorySensitivity,
    MemorySource,
    MemoryStatus,
    MissedRunPolicy,
    PermissionDecision,
    ProjectStage,
    ProviderErrorCode,
    RunMode,
    RunStatus,
    StageDecisionValue,
    ValidationStatus,
)


def utc_now() -> datetime:
    return datetime.now(UTC)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


class APIModel(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=False)


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
    retrieved_at: datetime = Field(default_factory=utc_now)
    observed_at: datetime | None = None
    excerpt: str | None = Field(default=None, max_length=8_000)
    location: str | None = Field(default=None, max_length=1_000)
    freshness_note: str | None = Field(default=None, max_length=1_000)
    status: EvidenceStatus = EvidenceStatus.ACTIVE
    confidence: float = Field(ge=0, le=1)
    supports_hypothesis_ids: list[str] = Field(default_factory=list)
    recorded_by: str
    recorded_at: datetime = Field(default_factory=utc_now)


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
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class OpportunityProject(APIModel):
    id: str = Field(default_factory=lambda: new_id("project"))
    name: str
    objective: str
    product_brief: str = ""
    target_markets: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
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


class EvidenceStatusUpdate(APIModel):
    status: EvidenceStatus


class EvidenceStatusChange(APIModel):
    evidence: Evidence
    affected_decision_ids: list[str] = Field(default_factory=list)


class RunCreate(APIModel):
    prompt: str = Field(min_length=1, max_length=40_000)
    agent_id: str | None = None
    resume_session_id: str | None = None
    mode: RunMode = RunMode.STANDARD
    provider_profile_id: str | None = None


class RunRecord(APIModel):
    id: str = Field(default_factory=lambda: new_id("run"))
    trace_id: str = Field(default_factory=lambda: new_id("trace"))
    project_id: str
    prompt: str
    agent_id: str | None = None
    schedule_id: str | None = None
    resume_session_id: str | None = None
    mode: RunMode = RunMode.STANDARD
    provider_profile_id: str | None = None
    provider_snapshot: dict[str, Any] | None = None
    skill_snapshot: list[dict[str, Any]] = Field(default_factory=list)
    status: RunStatus = RunStatus.QUEUED
    sdk_session_id: str | None = None
    error: str | None = None
    error_code: ProviderErrorCode | None = None
    created_at: datetime = Field(default_factory=utc_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None


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
    event_counts: dict[str, int] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class PermissionResolution(APIModel):
    decision: PermissionDecision
    reason: str = Field(default="", max_length=2_000)


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


class SkillVersion(APIModel):
    id: str
    namespace: str
    name: str
    description: str
    version: str
    content_hash: str
    source: Literal["application", "tenant"] = "application"
    enabled: bool = True
    path: str


class MemoryItem(APIModel):
    id: str = Field(default_factory=lambda: new_id("mem"))
    scope: MemoryScope
    scope_id: str
    text: str = Field(min_length=1, max_length=4_000)
    kind: MemoryKind
    source: MemorySource
    confidence: float = Field(default=1.0, ge=0, le=1)
    sensitivity: MemorySensitivity = MemorySensitivity.NORMAL
    status: MemoryStatus = MemoryStatus.PROPOSED
    version: int = 1
    created_by: str = Field(default="system", max_length=160)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class MemoryCreate(APIModel):
    scope: MemoryScope
    scope_id: str = Field(min_length=1, max_length=160)
    text: str = Field(min_length=1, max_length=4_000)
    kind: MemoryKind
    sensitivity: MemorySensitivity = MemorySensitivity.NORMAL


class MemoryDecision(APIModel):
    decision: Literal["approve", "reject", "disable"]
    reason: str = Field(default="", max_length=2_000)


class MemoryRollback(APIModel):
    version: int = Field(ge=1)


class Schedule(APIModel):
    id: str = Field(default_factory=lambda: new_id("schedule"))
    project_id: str
    agent_id: str | None = None
    provider_profile_id: str | None = None
    mode: RunMode = RunMode.STANDARD
    prompt: str = Field(min_length=1, max_length=40_000)
    cron: str | None = None
    run_once_at: datetime | None = None
    timezone: str = "UTC"
    missed_run_policy: MissedRunPolicy = MissedRunPolicy.SKIP
    enabled: bool = True
    next_fire_at: datetime | None = None
    last_fire_at: datetime | None = None
    last_run_id: str | None = None
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
    enabled: bool | None = None


class ScheduleCreate(APIModel):
    project_id: str = Field(min_length=1, max_length=160)
    agent_id: str | None = None
    provider_profile_id: str | None = None
    mode: RunMode = RunMode.STANDARD
    prompt: str = Field(min_length=1, max_length=40_000)
    cron: str | None = Field(default=None, min_length=9, max_length=120)
    run_once_at: datetime | None = None
    timezone: str = Field(default="UTC", max_length=64)
    missed_run_policy: MissedRunPolicy = MissedRunPolicy.SKIP

    @model_validator(mode="after")
    def exactly_one_trigger(self) -> ScheduleCreate:
        if (self.cron is None) == (self.run_once_at is None):
            raise ValueError("exactly one of cron or run_once_at is required")
        return self
