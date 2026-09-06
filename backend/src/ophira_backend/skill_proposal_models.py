"""Public contracts for frozen Skill candidates and explicit human review."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from .constants import RunStatus
from .models import (
    APIModel,
    SkillPublicationReceipt,
    SkillSelectionToken,
    SkillTestResult,
)

SafeId = Annotated[str, Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9_-]+$")]
ContentHash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
SkillProposalState = Literal["pending", "publishing", "published", "rejected"]


class SkillProposalManifest(APIModel):
    schema_version: Literal[1]
    kind: Literal["ophira.skill-proposal"]
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    version: str = Field(min_length=1, max_length=100)
    package_dir: Literal["package"]
    reason: str = Field(min_length=1, max_length=4_000)
    related_run_ids: list[SafeId] = Field(default_factory=list, max_length=20)

    @field_validator("schema_version", mode="before")
    @classmethod
    def strict_schema_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("schema_version must be an integer")
        return value

    @field_validator("version", "reason")
    @classmethod
    def nonblank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must contain non-whitespace text")
        return value

    @field_validator("version")
    @classmethod
    def explicit_version(cls, value: str) -> str:
        if value != value.strip() or any(ord(char) < 32 for char in value):
            raise ValueError("version must be an explicit single-line value")
        return value

    @field_validator("related_run_ids")
    @classmethod
    def unique_runs(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("related_run_ids must be unique")
        return value


class SkillProposalFile(APIModel):
    """A path relative to the frozen package, never a host filesystem location."""

    path: str = Field(min_length=1, max_length=1_000)
    content_hash: ContentHash
    size_bytes: int = Field(ge=0)
    preview_kind: Literal["text", "binary"]


class SkillProposalSourceRun(APIModel):
    """Safe submission-time metadata for explicitly cited historical Runs."""

    id: SafeId
    status: RunStatus
    prompt_excerpt: str = Field(max_length=240)
    started_at: datetime
    finished_at: datetime | None = None


class SkillProposalReviewer(APIModel):
    kind: Literal["local_owner", "principal"]
    id: str = Field(min_length=1, max_length=160)
    role: Literal["owner", "admin"]


class SkillProposalReviewRequest(APIModel):
    decision: Literal["approve", "reject"]
    revision: int = Field(ge=1, strict=True)
    content_hash: ContentHash
    expected_target: SkillSelectionToken | None = None
    note: str = Field(default="", max_length=4_000)

    @model_validator(mode="after")
    def explicit_review(self) -> SkillProposalReviewRequest:
        if self.decision == "reject" and not self.note.strip():
            raise ValueError("rejection requires a nonempty note")
        if self.decision == "approve" and self.expected_target is None:
            raise ValueError("approval requires the reviewed target selection")
        return self


class SkillProposalReview(APIModel):
    review_revision: int = Field(ge=1, strict=True)
    decision: Literal["approve", "reject"]
    content_hash: ContentHash
    expected_target: SkillSelectionToken | None
    reviewer: SkillProposalReviewer
    note: str = Field(max_length=4_000)
    reviewed_at: datetime
    reviewer_binding: ContentHash
    outcome: Literal["publishing", "published", "rejected", "interrupted"]


class SkillProposal(APIModel):
    """Public persisted metadata; current registry target/actions are joined by API."""

    id: str = Field(pattern=r"^skill_proposal_[0-9a-f]{64}$")
    revision: int = Field(ge=1, strict=True)
    state: SkillProposalState
    project_id: SafeId
    source_run_id: SafeId
    source_artifact_id: SafeId
    source_sdk_session_id: str = Field(min_length=1, max_length=500)
    manifest_checksum: ContentHash
    manifest: SkillProposalManifest
    skill_id: str = Field(min_length=3, max_length=165)
    description: str = Field(min_length=1, max_length=4_000)
    content_hash: ContentHash
    files: list[SkillProposalFile] = Field(min_length=1, max_length=128)
    scan: SkillTestResult
    scan_content_hash: ContentHash
    source_runs: list[SkillProposalSourceRun] = Field(default_factory=list, max_length=20)
    review: SkillProposalReview | None = None
    history: list[SkillProposalReview] = Field(default_factory=list, max_length=64)
    publication_receipt: SkillPublicationReceipt | None = None
    created_at: datetime
    updated_at: datetime


class SkillProposalRecoveryReport(APIModel):
    inspected: int = 0
    published: int = 0
    reset_pending: int = 0
    failed: int = 0
