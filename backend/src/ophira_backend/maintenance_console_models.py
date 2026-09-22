"""Local operator review contracts; never accept filesystem paths from HTTP."""

from typing import Literal

from pydantic import AwareDatetime, Field, StrictBool, model_validator

from .models import APIModel, utc_now

REQUEST_ID = r"^[a-f0-9]{32}$"
CHECKSUM = r"^[a-f0-9]{64}$"


class MaintenancePrepare(APIModel):
    request_id: str = Field(pattern=REQUEST_ID)
    kind: Literal["backup", "restore"]
    backup_id: str | None = Field(default=None, pattern=CHECKSUM)

    @model_validator(mode="after")
    def source(self):
        if (self.kind == "restore") != (self.backup_id is not None):
            raise ValueError("restore requires a configured backup")
        return self


class MaintenanceConfirm(APIModel):
    fingerprint: str = Field(pattern=CHECKSUM)
    confirmed: StrictBool

    @model_validator(mode="after")
    def confirm(self):
        if not self.confirmed:
            raise ValueError("explicit confirmation required")
        return self


class MaintenanceRecord(MaintenancePrepare):
    schema_version: Literal[1] = 1
    scope: str = Field(pattern=CHECKSUM)
    state: Literal["preparing", "reviewed", "publishing", "completed", "discarded"] = "preparing"
    created_at: AwareDatetime = Field(default_factory=utc_now)
    reviewed_at: AwareDatetime | None = None
    stage_name: str | None = Field(default=None, pattern=r"^\.console-[a-f0-9]{32}-[a-z0-9_]+$")
    stage_identity: tuple[int, int] | None = None
    candidate_identity: tuple[int, int] | None = None
    tree_hash: str | None = Field(default=None, pattern=CHECKSUM)
    implementation_hash: str | None = Field(default=None, pattern=CHECKSUM)
    fingerprint: str | None = Field(default=None, pattern=CHECKSUM)
    summary: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def coherent(self):
        if (self.stage_name is None) != (self.stage_identity is None):
            raise ValueError("incomplete maintenance staging identity")
        if self.stage_name and not self.stage_name.startswith(f".console-{self.request_id}-"):
            raise ValueError("maintenance staging belongs to another request")
        if self.state in {"reviewed", "publishing", "completed"} and not all(
            (
                self.reviewed_at,
                self.stage_name,
                self.stage_identity,
                self.candidate_identity,
                self.tree_hash,
                self.implementation_hash,
                self.fingerprint,
            )
        ):
            raise ValueError("incomplete maintenance review")
        return self
