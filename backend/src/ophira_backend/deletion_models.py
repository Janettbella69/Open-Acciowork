"""Content-free provenance after human-authorized conversation erasure."""

from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from .models import APIModel

SAFE_ID = r"^[A-Za-z0-9_-]{1,200}$"


class ErasedRun(APIModel):
    kind: Literal["erased_run"] = "erased_run"
    schema_version: Literal[1] = 1
    id: str = Field(pattern=SAFE_ID)
    project_id: str = Field(pattern=SAFE_ID)
    identity_binding: Literal["local_only", "authenticated_principal"]
    tenant_id: str | None
    principal_id: str | None
    trace_id: str = Field(pattern=SAFE_ID)
    sdk_session_id: str | None = Field(max_length=500)
    resume_session_id: str | None = Field(max_length=500)
    started_at: AwareDatetime | None
    created_at: AwareDatetime
    request_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    review_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    deleted_at: AwareDatetime
    deleted_by: str = Field(min_length=1, max_length=160)

    @model_validator(mode="after")
    def identity(self):
        values = (self.tenant_id, self.principal_id)
        if self.identity_binding == "authenticated_principal":
            if not all(values):
                raise ValueError("deleted run identity is incomplete")
        elif any(value is not None for value in values):
            raise ValueError("local deleted run contains authenticated identity")
        return self


class ConversationDeletionRequest(APIModel):
    request_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    reviewed_at: AwareDatetime
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    confirm_deletion: bool = Field(strict=True)

    @model_validator(mode="after")
    def confirmed(self):
        if not self.confirm_deletion:
            raise ValueError("explicit deletion confirmation required")
        return self


def erased_run(value):
    """Recognize only a validated tombstone, never silently accept malformed Runs."""
    if isinstance(value, dict) and value.get("kind") == "erased_run":
        return ErasedRun.model_validate(value)
    return None
