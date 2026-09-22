"""Content-free provenance after human-authorized conversation erasure."""

from typing import Annotated, Literal

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


class ProjectDeletionRequest(ConversationDeletionRequest):
    confirm_project_name: str = Field(min_length=1, max_length=160)


class ErasedProject(APIModel):
    kind: Literal["erased_project"] = "erased_project"
    schema_version: Literal[1] = 1
    id: str = Field(pattern=SAFE_ID)
    identity_binding: Literal["local_only", "authenticated_principal"]
    tenant_id: str | None
    owner_principal_id: str | None
    created_at: AwareDatetime
    request_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    review_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    deleted_at: AwareDatetime
    deleted_by: str = Field(min_length=1, max_length=160)
    # Exact origin digests preserve independently copied projects without keeping
    # source template text, names, files, application requests or membership lists.
    template_copies: dict[
        Annotated[str, Field(pattern=SAFE_ID)], Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    ] = Field(
        default_factory=dict,
        max_length=10_000,
    )

    @model_validator(mode="after")
    def identity(self):
        values = (self.tenant_id, self.owner_principal_id)
        if self.identity_binding == "authenticated_principal":
            if not all(values):
                raise ValueError("deleted project identity is incomplete")
        elif any(value is not None for value in values):
            raise ValueError("local deleted project contains authenticated identity")
        return self


def erased_project(value):
    if isinstance(value, dict) and value.get("kind") == "erased_project":
        return ErasedProject.model_validate(value)
    return None


def erased_run(value):
    """Recognize only a validated tombstone, never silently accept malformed Runs."""
    if isinstance(value, dict) and value.get("kind") == "erased_run":
        return ErasedRun.model_validate(value)
    return None
