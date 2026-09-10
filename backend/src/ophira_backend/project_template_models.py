"""Versioned, human-curated project seeds; no evidence or execution authority."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

TEMPLATE_ID = r"^template_[a-f0-9]{32}$"
APPLICATION_ID = r"^template_use_[a-f0-9]{32}$"
REQUEST_ID = r"^[A-Za-z0-9_-]{16,100}$"
IDENTIFIER = r"^[A-Za-z0-9_-]{1,200}$"
CHECKSUM = r"^[a-f0-9]{64}$"
MAX_TEMPLATES = 50
MAX_TEMPLATE_VERSIONS = 50
MAX_TEMPLATE_MUTATIONS = 200
MAX_TEMPLATE_APPLICATIONS = 500
MAX_TEMPLATE_RECORD_BYTES = 2 * 1024 * 1024
MAX_TEMPLATE_REFERENCES = 20
MAX_TEMPLATE_REFERENCE_BYTES = 50 * 1024 * 1024
MAX_TEMPLATE_HISTORY_BYTES = 200 * 1024 * 1024


def fingerprint(value) -> str:
    body = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    return hashlib.sha256(
        json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class ProjectSeed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(min_length=1, max_length=160)
    objective: str = Field(min_length=1, max_length=4000)
    product_brief: str = Field(default="", max_length=12000)
    target_markets: list[Annotated[str, Field(min_length=1, max_length=160)]] = Field(
        default_factory=list,
        max_length=50,
    )
    constraints: list[Annotated[str, Field(min_length=1, max_length=1000)]] = Field(
        default_factory=list,
        max_length=100,
    )

    @model_validator(mode="after")
    def nonblank(self):
        if not self.name.strip() or not self.objective.strip():
            raise ValueError("Project name and objective must not be blank")
        if any(not item.strip() for item in self.target_markets + self.constraints):
            raise ValueError("Project seed entries must not be blank")
        if len(json.dumps(self.model_dump(), ensure_ascii=False).encode()) > 64 * 1024:
            raise ValueError("Project seed exceeds its text budget")
        return self


class TemplateReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    artifact_id: str = Field(pattern=IDENTIFIER)
    name: str = Field(min_length=1, max_length=240)
    mime: str = Field(min_length=1, max_length=200)
    version: int = Field(ge=1)
    checksum: str = Field(pattern=CHECKSUM)
    size_bytes: int = Field(ge=0, le=MAX_TEMPLATE_REFERENCE_BYTES)


class TemplateVersion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=2000)
    seed: ProjectSeed
    references: list[TemplateReference] = Field(max_length=MAX_TEMPLATE_REFERENCES)
    source_project_revision: int = Field(ge=1)
    created_by: str = Field(min_length=1, max_length=160)
    created_at: AwareDatetime
    content_hash: str = Field(pattern=CHECKSUM)

    @model_validator(mode="after")
    def coherent(self):
        body = self.model_dump(mode="json", exclude={"content_hash"})
        if self.content_hash != fingerprint(body):
            raise ValueError("Template version content hash does not match")
        if len({item.artifact_id for item in self.references}) != len(self.references):
            raise ValueError("Template references must be unique")
        if sum(item.size_bytes for item in self.references) > MAX_TEMPLATE_REFERENCE_BYTES:
            raise ValueError("Template references exceed the byte budget")
        return self


class TemplateMutation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    revision: int = Field(ge=1)
    request_id: str = Field(pattern=REQUEST_ID)
    request_hash: str = Field(pattern=CHECKSUM)
    actor_id: str = Field(min_length=1, max_length=160)
    action: Literal["create", "version", "archive", "restore"]
    version: int = Field(ge=1)
    at: AwareDatetime


class ProjectTemplate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1
    id: str = Field(pattern=TEMPLATE_ID)
    project_id: str = Field(pattern=IDENTIFIER)
    identity_binding: Literal["local_only", "authenticated_principal"]
    tenant_id: str | None
    created_by: str = Field(min_length=1, max_length=160)
    versions: list[TemplateVersion] = Field(min_length=1, max_length=MAX_TEMPLATE_VERSIONS)
    history: list[TemplateMutation] = Field(min_length=1, max_length=MAX_TEMPLATE_MUTATIONS)

    @property
    def revision(self):
        return self.history[-1].revision

    @property
    def archived(self):
        return self.history[-1].action == "archive"

    @model_validator(mode="after")
    def coherent(self):
        if [item.version for item in self.versions] != list(range(1, len(self.versions) + 1)):
            raise ValueError("Template versions are not contiguous")
        if [item.revision for item in self.history] != list(range(1, len(self.history) + 1)):
            raise ValueError("Template history is not contiguous")
        first = self.history[0]
        expected = (
            "template_"
            + hashlib.sha256(
                f"{self.project_id}\0{self.created_by}\0{first.request_id}".encode()
            ).hexdigest()[:32]
        )
        if self.id != expected or first.action != "create" or first.actor_id != self.created_by:
            raise ValueError("Template origin is inconsistent")
        if len({(item.actor_id, item.request_id) for item in self.history}) != len(self.history):
            raise ValueError("Template request identities are ambiguous")
        version, archived = 0, False
        for entry in self.history:
            if entry.action in {"create", "version"}:
                if archived or (entry.action == "create") != (version == 0):
                    raise ValueError("Template version transition is invalid")
                version += 1
                if version > len(self.versions):
                    raise ValueError("Template history references a missing version")
                item = self.versions[version - 1]
                if item.created_by != entry.actor_id or item.created_at != entry.at:
                    raise ValueError("Template version authorship is inconsistent")
            elif (entry.action == "archive") == archived:
                raise ValueError("Template archive transition is invalid")
            else:
                archived = entry.action == "archive"
            if entry.version != version:
                raise ValueError("Template history has a different version")
        if version != len(self.versions):
            raise ValueError("Template history does not cover every version")
        return self


class TemplateSave(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: str = Field(pattern=REQUEST_ID)
    title: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=2000)
    seed: ProjectSeed
    artifact_ids: list[Annotated[str, Field(pattern=IDENTIFIER)]] = Field(
        default_factory=list,
        max_length=MAX_TEMPLATE_REFERENCES,
    )
    expected_revision: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def unique(self):
        if not self.title.strip() or len(set(self.artifact_ids)) != len(self.artifact_ids):
            raise ValueError("Template title is blank or references are duplicated")
        return self


class TemplateArchive(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: str = Field(pattern=REQUEST_ID)
    expected_revision: int = Field(ge=1)
    archived: bool


class TemplateApply(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: str = Field(pattern=REQUEST_ID)
    version: int = Field(ge=1, le=MAX_TEMPLATE_VERSIONS)
    content_hash: str = Field(pattern=CHECKSUM)
    seed: ProjectSeed
    artifact_ids: list[Annotated[str, Field(pattern=IDENTIFIER)]] = Field(
        max_length=MAX_TEMPLATE_REFERENCES,
    )

    @model_validator(mode="after")
    def unique(self):
        if len(set(self.artifact_ids)) != len(self.artifact_ids):
            raise ValueError("Template references are duplicated")
        return self


class TemplateApplication(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1
    id: str = Field(pattern=APPLICATION_ID)
    source_project_id: str = Field(pattern=IDENTIFIER)
    template_id: str = Field(pattern=TEMPLATE_ID)
    template_version: int = Field(ge=1)
    template_hash: str = Field(pattern=CHECKSUM)
    project_id: str = Field(pattern=IDENTIFIER)
    actor_id: str = Field(min_length=1, max_length=160)
    request_id: str = Field(pattern=REQUEST_ID)
    request_hash: str = Field(pattern=CHECKSUM)
    request: TemplateApply
    input_hash: str = Field(pattern=CHECKSUM)
    created_at: AwareDatetime
    state: Literal["preparing", "ready", "applied"] = "preparing"
    prepared_project_hash: str | None = Field(default=None, pattern=CHECKSUM)

    @model_validator(mode="after")
    def coherent(self):
        expected = (
            "template_use_"
            + hashlib.sha256(
                f"{self.source_project_id}\0{self.template_id}\0{self.actor_id}\0{self.request_id}".encode()
            ).hexdigest()[:32]
        )
        if (
            self.id != expected
            or self.project_id != "project_template_" + self.id[-32:]
            or self.request.request_id != self.request_id
            or self.request.version != self.template_version
            or self.request.content_hash != self.template_hash
            or self.input_hash != fingerprint(self.request)
            or (self.state == "preparing") != (self.prepared_project_hash is None)
        ):
            raise ValueError("Template application identity or state is inconsistent")
        return self


class AppliedTemplateReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    artifact_id: str = Field(pattern=IDENTIFIER)
    source: TemplateReference


class ProjectTemplateOrigin(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    source_project_id: str = Field(pattern=IDENTIFIER)
    template_id: str = Field(pattern=TEMPLATE_ID)
    version: int = Field(ge=1)
    content_hash: str = Field(pattern=CHECKSUM)
    seed_hash: str = Field(pattern=CHECKSUM)
    title: str = Field(min_length=1, max_length=160)
    application_id: str = Field(pattern=APPLICATION_ID)
    applied_by: str = Field(min_length=1, max_length=160)
    created_at: AwareDatetime
    references: list[AppliedTemplateReference] = Field(max_length=MAX_TEMPLATE_REFERENCES)
