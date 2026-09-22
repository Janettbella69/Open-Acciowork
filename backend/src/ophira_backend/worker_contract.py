"""Host-owned resource envelope shared by storage, SDK launch and its supervisor."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

OUTPUT_CHUNK_BYTES = 65536

WorkerOutcome = Literal[
    "completed",
    "output_limit",
    "wall_clock_limit",
    "worker_failed",
    "launch_failed",
    "cleanup_failed",
    "descendants_remaining",
    "disconnected",
    "terminated",
    "parent_exited",
    "unconfirmed",
]


class WorkerReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    outcome: WorkerOutcome
    output_bytes: int = Field(ge=0)
    exit_code: int | None = None


class WorkerLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    mode: Literal["native", "systemd"] = "native"
    cpu_percent: int = Field(default=200, ge=1, le=6400)
    memory_bytes: int = Field(default=2 * 1024**3, ge=64 * 1024**2, le=64 * 1024**3)
    tasks_max: int = Field(default=128, ge=8, le=4096)
    output_bytes: int = Field(default=32 * 1024**2, ge=1024, le=1024**3)
    frame_bytes: int = Field(default=4 * 1024**2, ge=1024, le=16 * 1024**2)
    timeout_seconds: int = Field(default=14400, ge=1, le=86400)


class WorkerLaunchPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    version: Literal[1] = 1
    command: list[str] = Field(min_length=1, max_length=8)
    unit: str = Field(pattern=r"^ophira-sdk-[a-f0-9]{32}\.scope$")
    limits: WorkerLimits
    output_remaining: int = Field(ge=1, le=1024**3)
    deadline_epoch: float = Field(allow_inf_nan=False)
    environment_names: list[str]
