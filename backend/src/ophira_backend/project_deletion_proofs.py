"""Content-free provenance retained when project-owned source material is erased."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .backup_files import parse_json
from .models import SkillPublicationReceipt
from .skill_proposal_files import directory_fd, read_relative

IDENTIFIER = r"^[A-Za-z0-9_-]{1,200}$"
HASH = r"^[a-f0-9]{64}$"
PROOF_SCAN_BYTES = 512 * 1024 * 1024


def _deletion_names(directory: Path) -> list[str]:
    if not directory.exists() and not directory.is_symlink():
        return []
    names = []
    with directory_fd(directory) as fd, os.scandir(fd) as children:
        for child in children:
            names.append(child.name)
            if len(names) > 10000:
                raise ValueError("project deletion proof count exceeds its limit")
    if len({name.casefold() for name in names}) != len(names):
        raise ValueError("ambiguous project deletion identities")
    return sorted(names)


def proof_hash(value) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()


class TemplateDeletionProof(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    application_id: str = Field(pattern=r"^template_use_[a-f0-9]{32}$")
    source_project_id: str = Field(pattern=IDENTIFIER)
    target_project_id: str = Field(pattern=IDENTIFIER)
    template_id: str = Field(pattern=r"^template_[a-f0-9]{32}$")
    template_version: int = Field(ge=1)
    template_hash: str = Field(pattern=HASH)
    origin_hash: str | None = Field(default=None, pattern=HASH)
    identity_binding: Literal["local_only", "authenticated_principal"]
    tenant_id: str | None = None
    target_owner_principal_id: str | None = None


class ProjectDeletionProofs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1
    project_id: str = Field(pattern=IDENTIFIER)
    publications: list[SkillPublicationReceipt] = Field(default_factory=list, max_length=5000)
    templates: list[TemplateDeletionProof] = Field(default_factory=list, max_length=25000)


def load_deletion_proofs(root: Path) -> dict[str, ProjectDeletionProofs]:
    result = {}
    scanned_bytes = 0
    for name in _deletion_names(root / "project-deletions"):
        if not re.fullmatch(IDENTIFIER, name):
            raise ValueError("invalid project deletion proof identity")
        try:
            state_raw = read_relative(
                root, f"project-deletions/{name}/state.json", 64 * 1024 * 1024
            )
        except FileNotFoundError:
            continue  # A directory alone is not a committed deletion intent.
        scanned_bytes += len(state_raw)
        if scanned_bytes > PROOF_SCAN_BYTES:
            raise ValueError("project deletion proof scan exceeds its byte limit")
        state = parse_json(state_raw)
        if (
            not isinstance(state, dict)
            or state.get("schema_version") != 1
            or state.get("project_id") != name
            or state.get("status") not in {"deleting", "blocked", "deleted"}
        ):
            raise ValueError("invalid project deletion state")
        try:
            raw = read_relative(root, f"project-deletions/{name}/proofs.json", 16 * 1024 * 1024)
        except FileNotFoundError:
            if state.get("status") == "deleted":
                raise ValueError("completed project deletion proof is missing") from None
            continue  # Intent is persisted before the first resumable purge step.
        scanned_bytes += len(raw)
        if scanned_bytes > PROOF_SCAN_BYTES:
            raise ValueError("project deletion proof scan exceeds its byte limit")
        proof = ProjectDeletionProofs.model_validate_json(raw)
        if proof.project_id != name:
            raise ValueError("project deletion proof scope mismatch")
        plans = state.get("plan")
        if not isinstance(plans, dict) or not isinstance(plans.get("application"), dict):
            raise ValueError("project deletion application plan is missing")
        plan = plans["application"]
        if plan.get("proofs") != proof.model_dump(mode="json"):
            raise ValueError("project deletion proof does not match its durable intent")
        for link in proof.templates:
            if name not in {link.source_project_id, link.target_project_id}:
                raise ValueError("template proof does not belong to the deleted project")
            if link.target_project_id != "project_template_" + link.application_id[-32:]:
                raise ValueError("template proof target identity mismatch")
        result[name] = proof
    return result


def template_deletion_proofs(root: Path) -> dict[str, TemplateDeletionProof]:
    result = {}
    for proof in load_deletion_proofs(root).values():
        for link in proof.templates:
            previous = result.get(link.application_id)
            if previous is not None and previous != link:
                raise ValueError("conflicting template deletion proofs")
            result[link.application_id] = link
    return result


def deleted_project_ids(root: Path) -> set[str]:
    result = set()
    scanned_bytes = 0
    for name in _deletion_names(root / "project-deletions"):
        if not re.fullmatch(IDENTIFIER, name):
            raise ValueError("invalid project deletion identity")
        try:
            raw = read_relative(root, f"project-deletions/{name}/state.json", 64 * 1024 * 1024)
        except FileNotFoundError:
            continue
        scanned_bytes += len(raw)
        if scanned_bytes > PROOF_SCAN_BYTES:
            raise ValueError("project deletion proof scan exceeds its byte limit")
        value = parse_json(raw)
        if (
            not isinstance(value, dict)
            or value.get("project_id") != name
            or value.get("schema_version") != 1
            or value.get("status") not in {"deleting", "blocked", "deleted"}
        ):
            raise ValueError("invalid project deletion state")
        result.add(name)
    return result


def project_is_deleted(root: Path, project_id: str) -> bool:
    """Bounded exact lookup for application retries; no global proof scan."""
    if not re.fullmatch(IDENTIFIER, project_id):
        raise ValueError("invalid project deletion identity")
    try:
        raw = read_relative(root, f"project-deletions/{project_id}/state.json", 64 * 1024 * 1024)
    except FileNotFoundError:
        return False
    state = parse_json(raw)
    if (
        not isinstance(state, dict)
        or state.get("project_id") != project_id
        or state.get("schema_version") != 1
        or state.get("status") not in {"deleting", "blocked", "deleted"}
    ):
        raise ValueError("invalid project deletion state")
    return True
