"""The reviewed, portable project archive contract (not a runtime backup)."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

EXPORT_FORMAT = "ophira-project-export-v1"
EXPORT_NOTES = [
    "Workspace files, saved artifact versions and project Skill proposal packages retain their "
    "original bytes, including any private content you placed in them.",
    "Structured records redact recognized secrets and login, SDK and browser session identifiers "
    "without shortening business text.",
    "Conversations contain application-visible messages, events and run records. Native SDK "
    "transcripts, hidden reasoning and resumable runtime state are excluded.",
    "Credentials, browser login profiles, shared agent and organization memory, shared Skills, "
    "device registrations and external service copies are excluded.",
    "Templates include saved versions and references. Other users' private template creation "
    "requests are excluded.",
    "Workspace recovery checkpoints, derived search indexes, shared agent diaries and operator "
    "backup archives are excluded.",
    "This archive is for inspecting and retaining project data. It cannot restore a running "
    "application or resume an Agent session.",
]


class ExportEntry(BaseModel):
    path: str
    category: str
    size_bytes: int
    sha256: str


class ProjectExportPreview(BaseModel):
    format: Literal["ophira-project-export-v1"] = EXPORT_FORMAT
    project_id: str
    actor_id: str
    revision: int
    fingerprint: str
    file_count: int
    total_bytes: int
    directories: list[str]
    entries: list[ExportEntry]
    notes: list[str] = Field(default_factory=lambda: list(EXPORT_NOTES))


class ProjectExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class ProjectExportConflict(ValueError):
    """A safe, user-actionable reason to review a fresh archive."""
