"""Durable plugin import reports backed by the private Skill review store."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from .backup_files import BackupLimits, parse_json, remove_staging, tree_inventory
from .models import utc_now
from .plugin_archives import PluginArchive, inspect_plugin_archive
from .skill_import_models import IMPORT_ID, IMPORT_REQUEST_ID, SkillRepositoryScope
from .skill_proposal_files import directory_fd, read_relative, write_package
from .skill_proposal_models import SkillProposalReviewer
from .skill_repository import SkillRepository, digest
from .skills import (
    CONTENT_HASH_PATTERN,
    MAX_SKILL_ARCHIVE_BYTES,
    MAX_SKILL_ARCHIVE_ENTRIES,
    SkillConflictError,
    SkillIntegrityError,
)
from .storage import LedgerNotFoundError

PLUGIN_IMPORT_ID = r"^plugin_import_[0-9a-f]{64}$"
MAX_PLUGIN_IMPORTS = 50


class PluginImportedSkill(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    path: str = Field(min_length=1, max_length=300)
    import_id: str = Field(pattern=IMPORT_ID)
    skill_id: str = Field(min_length=1, max_length=200)
    content_hash: str = Field(pattern=rf"^{CONTENT_HASH_PATTERN.pattern}$")
    scan_ok: bool


class PluginMcpCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(min_length=1, max_length=100)
    url: str = Field(min_length=1, max_length=2000)
    credentials_required: bool


class PluginImportRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1
    id: str = Field(pattern=PLUGIN_IMPORT_ID)
    request_id: str = Field(pattern=IMPORT_REQUEST_ID)
    archive_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    scope: SkillRepositoryScope
    uploaded_by: SkillProposalReviewer
    name: str = Field(min_length=1, max_length=100)
    version: str | None = Field(default=None, max_length=100)
    description: str | None = Field(default=None, max_length=500)
    skills: list[PluginImportedSkill] = Field(default_factory=list, max_length=20)
    mcp_servers: list[PluginMcpCandidate] = Field(default_factory=list, max_length=20)
    ignored_agents: list[str] = Field(default_factory=list, max_length=256)
    ignored_components: list[str] = Field(default_factory=list, max_length=256)
    created_at: AwareDatetime

    @model_validator(mode="after")
    def coherent_scope(self):
        if (self.scope.auth_mode == "local_only") != (self.uploaded_by.kind == "local_owner"):
            raise ValueError("Plugin uploader does not match deployment scope")
        return self


class PluginImportService:
    def __init__(self, repository: SkillRepository) -> None:
        self.repository = repository
        self.root = repository.ledger.root / "plugin-imports"
        with directory_fd(self.root.parent):
            self.root.mkdir(mode=0o700, exist_ok=True)
        with directory_fd(self.root):
            pass

    def _id(self, request_id: str, actor: SkillProposalReviewer) -> str:
        return "plugin_import_" + digest([
            self.repository.scope.model_dump(), actor.kind, actor.id, request_id,
        ])

    def _path(self, identifier: str) -> Path:
        if not re.fullmatch(PLUGIN_IMPORT_ID, identifier):
            raise LedgerNotFoundError("Plugin import not found")
        return self.root / f"{identifier}.json"

    def _load(self, identifier: str) -> PluginImportRecord:
        try:
            raw = read_relative(self.root, f"{identifier}.json", 64 * 1024)
        except FileNotFoundError as exc:
            raise LedgerNotFoundError("Plugin import not found") from exc
        try:
            parse_json(raw)
            record = PluginImportRecord.model_validate_json(raw)
            if (
                record.id != identifier or record.scope != self.repository.scope
                or record.id != self._id(record.request_id, record.uploaded_by)
            ):
                raise ValueError("Plugin import binding mismatch")
            return record
        except ValueError as exc:
            raise SkillIntegrityError("Plugin import record is invalid") from exc

    def _view(self, record: PluginImportRecord, principal) -> dict:
        states = []
        for skill in record.skills:
            current = self.repository.detail(skill.import_id, principal)["import"]
            if (
                current["skill"]["id"] != skill.skill_id
                or current["skill"]["content_hash"] != skill.content_hash
            ):
                raise SkillIntegrityError("Plugin Skill import binding changed")
            states.append({"path": skill.path, "import_id": skill.import_id,
                           "skill_id": skill.skill_id, "state": current["state"],
                           "scan_ok": current["scan"]["ok"]})
        return {"import": record.model_dump(mode="json"), "skill_states": states}

    def _clean_staging(self) -> None:
        # The import lock excludes a live uploader. Only host-created staging
        # entries with known names and safe file types can be removed.
        changed = False
        with directory_fd(self.root) as descriptor:
            for name in os.listdir(descriptor):
                path = self.root / name
                if name.startswith(".plugin-inspect-"):
                    tree_inventory(path, BackupLimits(
                        files=256, entries=1024, file_bytes=5_000_000,
                        total_bytes=6_000_000,
                    ))
                    remove_staging(path)
                    changed = True
                elif re.fullmatch(r"\.plugin_import_[0-9a-f]{64}\.json\.[A-Za-z0-9_-]+\.tmp", name):
                    info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                        raise SkillIntegrityError("Plugin import staging is unsafe")
                    os.unlink(name, dir_fd=descriptor)
                    changed = True
            if changed:
                os.fsync(descriptor)

    def list(self, principal) -> list[PluginImportRecord]:
        self.repository.authorize(principal, manage=True)
        with self.repository.ledger._lock_for("plugin-imports"):
            self._clean_staging()
            with directory_fd(self.root) as fd, os.scandir(fd) as entries:
                names = [row.name for row in entries]
            if any(not name.endswith(".json") for name in names):
                raise SkillIntegrityError("Plugin import storage contains an unsafe entry")
            if len(names) > MAX_PLUGIN_IMPORTS:
                raise SkillConflictError("Plugin import capacity reached")
            records = [self._load(name[:-5]) for name in sorted(names)]
            return sorted(records, key=lambda row: row.created_at, reverse=True)

    def detail(self, identifier: str, principal) -> dict:
        self.repository.authorize(principal, manage=True)
        with self.repository.ledger._lock_for("plugin-imports"):
            return self._view(self._load(identifier), principal)

    def submit(self, payload: bytes, request_id: str, principal) -> dict:
        if not re.fullmatch(IMPORT_REQUEST_ID, request_id):
            raise ValueError("Plugin import requires a request ID")
        self.repository.authorize(principal, manage=True)
        actor = self.repository.reviewer(principal)
        identifier = self._id(request_id, actor)
        archive_hash = hashlib.sha256(payload).hexdigest()
        with self.repository.ledger._lock_for("plugin-imports"):
            self._clean_staging()
            try:
                existing = self._load(identifier)
            except LedgerNotFoundError:
                existing = None
            if existing is not None:
                if existing.archive_hash != archive_hash:
                    raise SkillConflictError("This Plugin import request has different bytes")
                return self._view(existing, principal)
            if len(self.list(principal)) >= MAX_PLUGIN_IMPORTS:
                raise SkillConflictError("Plugin import capacity reached")
            archive: PluginArchive = inspect_plugin_archive(payload)
            report_size = len(json.dumps({
                "name": archive.name, "version": archive.version,
                "description": archive.description,
                "skills": [item.path for item in archive.skills],
                "mcp": [item.__dict__ for item in archive.mcp_servers],
                "agents": archive.ignored_agents,
                "ignored": archive.ignored_components,
            }, ensure_ascii=False).encode("utf-8"))
            if report_size > 40_000:
                raise ValueError("Plugin component report exceeds its size limit")
            inspection = Path(tempfile.mkdtemp(prefix=".plugin-inspect-", dir=self.root))
            try:
                identities = set()
                for index, package in enumerate(archive.skills):
                    if (
                        len(package.files) > MAX_SKILL_ARCHIVE_ENTRIES
                        or len(package.zip_bytes()) > MAX_SKILL_ARCHIVE_BYTES
                    ):
                        raise ValueError("Plugin Skill exceeds the private import limits")
                    directory = inspection / str(index)
                    write_package(directory, package.files)
                    skill, _scan = self.repository.registry.inspect_candidate(directory)
                    if skill.id in identities:
                        raise ValueError("Plugin contains duplicate Skill names")
                    identities.add(skill.id)
            finally:
                remove_staging(inspection)
            if not archive.skills:
                # An MCP-only plugin still binds the shared review store to the
                # deployment scope before its report becomes durable.
                with self.repository.transaction(principal, manage=True):
                    pass
            imports = []
            for package in archive.skills:
                child_request = hashlib.sha256(
                    f"{identifier}:{package.path}".encode()
                ).hexdigest()
                result = self.repository.submit(
                    package.zip_bytes(),
                    f"{archive.name}-{package.path.replace('/', '-')}.zip",
                    child_request, principal,
                )
                item = result["import"]
                imports.append(PluginImportedSkill(
                    path=package.path, import_id=item["id"],
                    skill_id=item["skill"]["id"],
                    content_hash=item["skill"]["content_hash"],
                    scan_ok=item["scan"]["ok"],
                ))
            record = PluginImportRecord(
                id=identifier, request_id=request_id, archive_hash=archive_hash,
                scope=self.repository.scope, uploaded_by=actor,
                name=archive.name, version=archive.version, description=archive.description,
                skills=imports, mcp_servers=[PluginMcpCandidate(
                    name=item.name, url=item.url,
                    credentials_required=item.credentials_required,
                ) for item in archive.mcp_servers],
                ignored_agents=list(archive.ignored_agents),
                ignored_components=list(archive.ignored_components),
                created_at=utc_now(),
            )
            if len(record.model_dump_json().encode("utf-8")) > 64 * 1024:
                raise SkillConflictError("Plugin import record exceeds its size limit")
            self.repository.authorize(principal, manage=True)
            with directory_fd(self.root):
                self.repository.ledger._atomic_write(self._path(identifier), record)
            return self._view(record, principal)
