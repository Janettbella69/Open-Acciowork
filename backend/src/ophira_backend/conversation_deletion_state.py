"""Durable, content-free conversation deletion intents under the project lease."""

import json
import os
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from .constants import RunStatus
from .project_deletion_proofs import proof_hash
from .project_deletion_purge import _atomic_bytes, _json_bytes
from .project_lifecycle import MAX_DELETION_STATE_BYTES, ProjectLifecycle, _safe_id
from .skill_proposal_files import child_directory, read_at


class DeletedRunOrigin(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    project_id: str
    trace_id: str
    sdk_session_id: str | None
    started_at: datetime | None
    finished_at: datetime | None
    status: RunStatus
    identity_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    prompt_excerpt_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @classmethod
    def from_run(cls, run):
        return cls(
            **{
                name: getattr(run, name)
                for name in cls.model_fields
                if name not in {"identity_sha256", "prompt_excerpt_sha256"}
            },
            identity_sha256=run_identity_hash(run),
            prompt_excerpt_sha256=run_excerpt_hash(run),
        )


def run_identity_hash(run):
    if isinstance(run, DeletedRunOrigin):
        return run.identity_sha256
    return proof_hash(
        [run.tenant_id, run.principal_id, run.principal_role, run.principal_session_id]
    )


def run_excerpt_hash(run):
    if isinstance(run, DeletedRunOrigin):
        return run.prompt_excerpt_sha256
    from .events import redact

    return proof_hash(str(redact(run.prompt))[:240])


class ConversationDeletionStates:
    def __init__(self, lifecycle: ProjectLifecycle):
        self.lifecycle = lifecycle

    def read(self, project_id: str, operation_id: str):
        _safe_id(project_id)
        if str(UUID(operation_id)) != operation_id:
            raise ValueError("invalid conversation deletion operation")
        try:
            with self.lifecycle._directory("conversation-deletions") as parent:
                with child_directory(parent, (project_id,)) as directory:
                    raw = read_at(directory, operation_id + ".json", MAX_DELETION_STATE_BYTES)
        except FileNotFoundError:
            return None
        state = json.loads(raw)
        if (
            not isinstance(state, dict)
            or state.get("schema_version") != 1
            or state.get("project_id") != project_id
            or state.get("operation_id") != operation_id
            or state.get("status") not in {"deleting", "blocked", "deleted"}
        ):
            raise ValueError("invalid conversation deletion state")
        return state

    def list(self, project_id):
        _safe_id(project_id)
        try:
            with self.lifecycle._directory("conversation-deletions") as parent:
                with child_directory(parent, (project_id,)) as directory:
                    names = os.listdir(directory)
        except FileNotFoundError:
            return []
        if len(names) > 10_000:
            raise ValueError("conversation deletion history exceeds limit")
        states, size = [], 0
        for name in sorted(names):
            if name.startswith(".") and name.endswith(".tmp"):
                continue  # Uncommitted metadata-only staging is not an intent.
            if not name.endswith(".json"):
                raise ValueError("invalid conversation deletion state path")
            state = self.read(project_id, name[:-5])
            if state is None:
                continue
            size += len(_json_bytes(state))
            if size > MAX_DELETION_STATE_BYTES:
                raise ValueError("conversation deletion history exceeds limit")
            states.append(state)
        return states

    def write(self, state):
        project_id, operation_id = state["project_id"], state["operation_id"]
        _safe_id(project_id)
        if str(UUID(operation_id)) != operation_id:
            raise ValueError("invalid conversation deletion operation")
        if not self.lifecycle._held_exclusive(project_id):
            raise RuntimeError("conversation deletion requires exclusive project ownership")
        if state.get("schema_version") != 1 or state.get("status") not in {
            "deleting",
            "blocked",
            "deleted",
        }:
            raise ValueError("invalid conversation deletion state")
        previous = self.read(project_id, operation_id)
        mutable = {"status", "updated_at", "can_retry", "error_code", "error_message"}
        if previous is not None and (
            {k: v for k, v in previous.items() if k not in mutable}
            != {k: v for k, v in state.items() if k not in mutable}
            or (previous["status"] == "deleted" and previous != state)
        ):
            raise ValueError("conversation deletion intent is immutable")
        raw = _json_bytes(state)
        if len(raw) > MAX_DELETION_STATE_BYTES:
            raise ValueError("conversation deletion state exceeds limit")
        with self.lifecycle._directory("conversation-deletions", create=True) as parent:
            try:
                os.mkdir(project_id, 0o700, dir_fd=parent)
                os.fsync(parent)
            except FileExistsError:
                pass
        _atomic_bytes(
            self.lifecycle.root, f"conversation-deletions/{project_id}/{operation_id}.json", raw
        )

    def pending(self):
        try:
            with self.lifecycle._directory("conversation-deletions") as directory:
                names = os.listdir(directory)
        except FileNotFoundError:
            return []
        if len(names) > 10_000:
            raise ValueError("conversation deletion project count exceeds limit")
        return [
            state
            for name in sorted(names)
            for state in self.list(name)
            if state["status"] != "deleted"
        ]

    def origins(self, project_id):
        result = {}
        for state in self.list(project_id):
            if state["status"] != "deleted":
                continue
            if (
                proof_hash({**state["plan"], "requester": state["requester"]})
                != state["fingerprint"]
            ):
                raise ValueError("conversation deletion provenance changed")
            if {row["id"] for row in state["plan"]["origins"]} != set(state["run_ids"]):
                raise ValueError("conversation deletion provenance scope mismatch")
            for raw in state["plan"]["origins"]:
                origin = DeletedRunOrigin.model_validate(raw)
                if origin.project_id != project_id or origin.id in result:
                    raise ValueError("invalid deleted conversation provenance")
                result[origin.id] = origin
        return result
