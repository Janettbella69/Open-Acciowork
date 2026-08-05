from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

from .constants import (
    AgentTemplate,
    Dimension,
    EvidenceKind,
    MemoryKind,
    MemoryScope,
    MemorySensitivity,
    MemorySource,
    MemoryStatus,
    MissedRunPolicy,
    PermissionDecision,
    ProjectStage,
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
    dimension: Dimension
    statement: str
    validation_criteria: list[str] = Field(default_factory=list)
    priority: int = Field(default=3, ge=1, le=5)
    status: ValidationStatus = ValidationStatus.UNVERIFIED
    evidence_ids: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)


class Evidence(APIModel):
    id: str = Field(default_factory=lambda: new_id("ev"))
    dimension: Dimension
    kind: EvidenceKind
    claim: str
    source_title: str
    source_url: HttpUrl | None = None
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


def default_assessments() -> dict[Dimension, DimensionAssessment]:
    return {dimension: DimensionAssessment(dimension=dimension) for dimension in Dimension}


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
    assessments: dict[Dimension, DimensionAssessment] = Field(default_factory=default_assessments)
    stage_decisions: list[StageDecision] = Field(default_factory=list)
    revision: int = 1
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class RunCreate(APIModel):
    prompt: str = Field(min_length=1, max_length=40_000)
    agent_id: str | None = None
    resume_session_id: str | None = None


class RunRecord(APIModel):
    id: str = Field(default_factory=lambda: new_id("run"))
    trace_id: str = Field(default_factory=lambda: new_id("trace"))
    project_id: str
    prompt: str
    agent_id: str | None = None
    schedule_id: str | None = None
    resume_session_id: str | None = None
    status: RunStatus = RunStatus.QUEUED
    sdk_session_id: str | None = None
    error: str | None = None
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


class ScheduleCreate(APIModel):
    project_id: str = Field(min_length=1, max_length=160)
    agent_id: str | None = None
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
