"""Human review and resumable project erasure HTTP contracts."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator

from .models import APIModel


class DeletionDomain(APIModel):
    id: str
    label: str
    files: int = Field(ge=0)
    bytes: int = Field(ge=0)


class DeletionRetention(APIModel):
    code: str
    message: str
    count: int = Field(default=0, ge=0)


class DeletionBlocker(APIModel):
    code: str
    message: str


class DeletionCounts(APIModel):
    files: int = Field(ge=0)
    bytes: int = Field(ge=0)
    runs: int = Field(ge=0)
    sessions: int = Field(ge=0)
    memories: int = Field(ge=0)
    shops: int = Field(ge=0)


class ProjectDeletionPreview(APIModel):
    schema_version: Literal[1] = 1
    project_id: str
    project_name: str
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    domains: list[DeletionDomain]
    counts: DeletionCounts
    retained: list[DeletionRetention]
    blockers: list[DeletionBlocker]
    can_delete: bool


class ProjectDeletionRequest(APIModel):
    operation_id: str = Field(min_length=36, max_length=36)
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    confirmation_name: str = Field(min_length=1, max_length=160)
    reason: str = Field(min_length=1, max_length=1000)

    @field_validator("operation_id")
    @classmethod
    def canonical_uuid(cls, value):
        if str(UUID(value)) != value:
            raise ValueError("operation id must be a canonical UUID")
        return value

    @field_validator("reason")
    @classmethod
    def meaningful_reason(cls, value):
        if not value.strip():
            raise ValueError("deletion reason must not be empty")
        return value.strip()


class ProjectDeletionOperation(APIModel):
    schema_version: Literal[1] = 1
    project_id: str
    operation_id: str
    status: Literal["deleting", "blocked", "deleted"]
    created_at: datetime
    updated_at: datetime
    domains: list[DeletionDomain]
    retained: list[DeletionRetention]
    error_code: str | None = None
    error_message: str | None = None
    can_retry: bool
