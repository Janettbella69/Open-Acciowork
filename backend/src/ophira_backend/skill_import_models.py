"""Human package submissions into the deployment's private Skill repository."""

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from .models import (
    SkillPublicationReceipt,
    SkillSelectionToken,
    SkillTestResult,
    SkillVersionResponse,
)
from .skill_proposal_models import (
    SkillProposalFile,
    SkillProposalReview,
    SkillProposalReviewer,
    SkillProposalReviewRequest,
)

IMPORT_ID = r"^skill_import_[0-9a-f]{64}$"
IMPORT_REQUEST_ID = r"^[A-Za-z0-9_-]{16,100}$"
MAX_SKILL_IMPORTS = 100
MAX_SKILL_IMPORT_BYTES = 128_000_000
MAX_SKILL_IMPORT_RECORD_BYTES = 512_000
MAX_SKILL_IMPORT_REVIEWS = 32


class SkillRepositoryScope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1
    auth_mode: Literal["local_only", "trusted_gateway"]
    tenant_id: str | None

    @model_validator(mode="after")
    def coherent(self):
        if (self.auth_mode == "local_only") != (self.tenant_id is None):
            raise ValueError("Private Skill repository scope is inconsistent")
        return self


class SkillImportReviewRequest(SkillProposalReviewRequest):
    request_id: str = Field(pattern=IMPORT_REQUEST_ID)


class SkillRepositorySelection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    operation: Literal["enable", "disable", "rollback"]
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_target: SkillSelectionToken


class SkillImportReview(SkillProposalReview):
    request_id: str = Field(pattern=IMPORT_REQUEST_ID)
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class SkillImport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1
    id: str = Field(pattern=IMPORT_ID)
    request_id: str = Field(pattern=IMPORT_REQUEST_ID)
    archive_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    filename: str = Field(min_length=1, max_length=160)
    scope: SkillRepositoryScope
    uploaded_by: SkillProposalReviewer
    published_before_import: bool = False
    skill: SkillVersionResponse
    files: list[SkillProposalFile] = Field(min_length=1, max_length=128)
    scan: SkillTestResult
    revision: int = Field(ge=1)
    state: Literal["pending", "publishing", "published", "rejected"]
    history: list[SkillImportReview] = Field(
        default_factory=list, max_length=MAX_SKILL_IMPORT_REVIEWS
    )
    publication_receipt: SkillPublicationReceipt | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime

    @model_validator(mode="after")
    def coherent(self):
        if self.skill.source != "tenant" or self.skill.enabled:
            raise ValueError("Imported package must be unpublished tenant content")
        if (self.scope.auth_mode == "local_only") != (self.uploaded_by.kind == "local_owner"):
            raise ValueError("Imported uploader does not match the deployment scope")
        if self.scan.skill_id != self.skill.id:
            raise ValueError("Import scan has another Skill identity")
        if len({row.path for row in self.files}) != len(self.files):
            raise ValueError("Import paths must be unique")
        if (self.state == "published") != (self.publication_receipt is not None):
            raise ValueError("Imported publication has no matching receipt")
        if self.state in {"publishing", "published", "rejected"} and not self.history:
            raise ValueError("Imported publication has no human decision")
        if self.history:
            expected = "interrupted" if self.state == "pending" else self.state
            if self.history[-1].outcome != expected:
                raise ValueError("Import state does not match its human review")
        return self
