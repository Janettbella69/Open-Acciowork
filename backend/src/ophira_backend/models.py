from __future__ import annotations

import math
import posixpath
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, JsonValue, model_validator

from .constants import (
    CLARIFYING_TOOL,
    DELETE_BUILTIN_TOOLS,
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
    ProjectStage,
    ProviderErrorCode,
    RunMode,
    RunRetryStrategy,
    RunStatus,
    ScheduleKind,
    StageDecisionValue,
    ValidationStatus,
)


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
    source_run_id: str | None = None
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


class RunRetryRequest(APIModel):
    strategy: RunRetryStrategy


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
    # AskUserQuestion 结构化追问的回答：{问题原文: 选项 label 或 label 列表}。
    answers: dict[str, str | list[str]] | None = None
    expires_at: datetime | None = None


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


class MemoryCreate(APIModel):
    scope: MemoryScope
    scope_id: str = Field(min_length=1, max_length=160)
    text: str = Field(min_length=1, max_length=4_000)
    kind: MemoryKind
    sensitivity: MemorySensitivity = MemorySensitivity.NORMAL


class MemoryDecision(APIModel):
    decision: Literal["approve", "reject", "disable"]
    reason: str = Field(default="", max_length=2_000)


class MemoryCorrection(APIModel):
    text: str = Field(min_length=1, max_length=4_000)
    kind: MemoryKind | None = None
    sensitivity: MemorySensitivity | None = None


class MemoryRollback(APIModel):
    version: int = Field(ge=1)


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
    intent: str = Field(min_length=1, max_length=2_000)
    cooldown_hours: float = Field(default=24, ge=0, le=24 * 30)
    max_triggers: int = Field(default=3, ge=1, le=100)
    expires_at: datetime
    triggered_count: int = 0
    last_triggered_at: datetime | None = None
    enabled: bool = True
    created_by: str = Field(default="user", max_length=160)
    created_at: datetime = Field(default_factory=utc_now)


class StandingIntentCreate(APIModel):
    project_id: str = Field(min_length=1, max_length=160)
    intent: str = Field(min_length=1, max_length=2_000)
    cooldown_hours: float = Field(default=24, ge=0, le=24 * 30)
    max_triggers: int = Field(default=3, ge=1, le=100)
    expires_days: int = Field(default=90, ge=1, le=365)


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


class CommercialActionReview(APIModel):
    decision: CommercialReviewDecision
    actor_id: str = Field(min_length=1, max_length=160)
    reason: str = Field(min_length=1, max_length=4_000)
    decided_at: datetime
    valid_until: datetime | None = None


class CommercialActionAuditEntry(APIModel):
    event: Literal[
        "staged",
        "approved",
        "rejected",
        "expired",
        "execution_requested",
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


class CommercialExecutionRequest(APIModel):
    id: str = Field(max_length=200, pattern=r"^execution_[A-Za-z0-9_-]{40,}$")
    action_id: str = Field(min_length=1, max_length=200)
    project_id: str = Field(min_length=1, max_length=200)
    source_run_id: str = Field(min_length=1, max_length=160)
    source_session_id: str = Field(min_length=1, max_length=500)
    requested_by: str = Field(min_length=1, max_length=160)
    request_reason: str = Field(min_length=1, max_length=4_000)
    requested_at: datetime
    projected_state: dict[str, JsonValue]
    expected_target_version: str
    project_revision_checked: int = Field(ge=1)
    project_constraints_checked: list[str] = Field(default_factory=list)
    org_policy_updated_at: datetime
    external_status: ExternalExecutionStatus = ExternalExecutionStatus.NOT_EXECUTED
    adapter_required: Literal[True] = True
    adapter_preconditions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def finite_projected_state(self) -> CommercialExecutionRequest:
        if not json_value_is_finite(self.projected_state):
            raise ValueError("commercial projected state requires finite JSON numbers")
        return self


class CommercialActionRecord(APIModel):
    id: str = Field(max_length=200, pattern=r"^action_[A-Za-z0-9_-]{40,}$")
    project_id: str = Field(min_length=1, max_length=200)
    source_run_id: str = Field(min_length=1, max_length=160)
    source_session_id: str = Field(min_length=1, max_length=500)
    staged_by: str = Field(min_length=1, max_length=160)
    operation: CommercialActionOperation
    target: CommercialActionTarget
    observed_state: dict[str, JsonValue]
    changes: list[CommercialFieldChange]
    reason: str
    project_revision_at_stage: int = Field(ge=1)
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
        if self.expires_at <= self.created_at or self.updated_at < self.created_at:
            raise ValueError("commercial action timestamps are inconsistent")
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
        elif self.status == CommercialActionStatus.EXECUTION_REQUESTED:
            if (
                self.review is None
                or self.review.decision != CommercialReviewDecision.APPROVE
                or self.execution_request is None
            ):
                raise ValueError("execution request must derive from an approval")
            execution = self.execution_request
            if (
                execution.action_id != self.id
                or execution.project_id != self.project_id
                or execution.source_run_id != self.source_run_id
                or execution.source_session_id != self.source_session_id
            ):
                raise ValueError("execution request scope does not match its staged action")
        elif self.execution_request is not None:
            raise ValueError("expired commercial action cannot contain an execution request")
        return self


class OrgPolicy(APIModel):
    permission_mode: PermissionModePolicy = PermissionModePolicy.DEFAULT
    allowed_provider_ids: list[str] = Field(default_factory=list)
    allowed_skill_ids: list[str] = Field(default_factory=list)
    browser_enabled: bool = True
    computer_control_enabled: bool = False
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


class FeedbackItem(APIModel):
    id: str = Field(default_factory=lambda: new_id("feedback"))
    project_id: str | None = None
    run_id: str | None = None
    evidence_id: str | None = None
    kind: FeedbackKind
    user_note: str = Field(min_length=1, max_length=4_000)
    status: Literal["open", "reviewed", "folded_into_skill", "rejected"] = "open"
    created_at: datetime = Field(default_factory=utc_now)


class FeedbackCreate(APIModel):
    project_id: str | None = None
    run_id: str | None = None
    evidence_id: str | None = None
    kind: FeedbackKind
    user_note: str = Field(min_length=1, max_length=4_000)


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
    channel: Literal["feishu", "wechat", "webhook"] = "webhook"
    text: str = Field(min_length=1, max_length=40_000)
    project_id: str | None = None
    sender: str = Field(default="channel", max_length=160)
