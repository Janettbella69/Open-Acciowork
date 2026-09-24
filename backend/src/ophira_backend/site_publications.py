"""Reviewed, revocable publication of immutable project HTML artifacts."""

from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator

from .config import public_https_origin
from .models import APIModel, new_id, utc_now
from .skill_proposal_files import directory_fd, read_relative
from .storage import FileLedger, LedgerNotFoundError

MAX_SITE_HTML_BYTES = 512 * 1024
MAX_SITES_PER_PROJECT = 100
SITE_ID = re.compile(r"site_[0-9a-f]{32}")


class SitePublicationError(RuntimeError):
    pass


class SitePublicationConflict(SitePublicationError):
    pass


class SitePublicationStage(APIModel):
    artifact_id: str = Field(pattern=r"^artifact_[0-9a-f]{32}$")


class SitePublicationDecision(APIModel):
    expected_revision: int = Field(ge=1)
    expected_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirmation: Literal["publish"]


class SitePublicationRevoke(APIModel):
    expected_revision: int = Field(ge=1)
    confirmation: Literal["revoke"]


class SitePublication(APIModel):
    id: str = Field(pattern=r"^site_[0-9a-f]{32}$")
    project_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,200}$")
    artifact_id: str = Field(pattern=r"^artifact_[0-9a-f]{32}$")
    artifact_name: str = Field(min_length=1, max_length=255)
    artifact_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=1, le=MAX_SITE_HTML_BYTES)
    origin: str = Field(max_length=500)
    state: Literal["draft", "published", "revoked"]
    revision: int = Field(ge=1)
    created_by: str
    created_at: datetime
    published_by: str | None = None
    published_at: datetime | None = None
    revoked_by: str | None = None
    revoked_at: datetime | None = None

    @field_validator("origin")
    @classmethod
    def safe_origin(cls, value: str) -> str:
        return public_https_origin(value, "site origin")


class SitePublicationView(APIModel):
    publication: SitePublication
    public_url: str


class SitePublicationCatalog(APIModel):
    public_origin: str | None
    can_stage: bool
    can_publish: bool
    publications: list[SitePublicationView]


class SitePublicationStore:
    def __init__(self, ledger: FileLedger, public_origin: str | None) -> None:
        self.ledger = ledger
        self.public_origin = public_origin

    def _directory(self, project_id: str) -> Path:
        return self.ledger._project_path(project_id).parent / "site-publications"

    def _path(self, project_id: str, site_id: str) -> Path:
        if not SITE_ID.fullmatch(site_id):
            raise LedgerNotFoundError(site_id)
        return self._directory(project_id) / f"{site_id}.json"

    def _read(self, project_id: str, site_id: str) -> SitePublication:
        path = self._path(project_id, site_id)
        try:
            raw = read_relative(
                self.ledger.root,
                f"projects/{project_id}/site-publications/{site_id}.json",
                32 * 1024,
            )
        except FileNotFoundError as exc:
            raise LedgerNotFoundError(site_id) from exc
        record = SitePublication.model_validate_json(raw)
        if record.id != site_id or record.project_id != project_id or path.is_symlink():
            raise SitePublicationConflict("Site publication binding is invalid")
        return record

    def _view(self, record: SitePublication) -> SitePublicationView:
        return SitePublicationView(
            publication=record,
            public_url=(
                f"{record.origin}/published/{record.project_id}/{record.id}"
            ),
        )

    def list(self, project_id: str) -> list[SitePublicationView]:
        with self.ledger.lifecycle.lease(project_id), self.ledger._lock_for(project_id):
            self.ledger.get_project(project_id)
            directory = self._directory(project_id)
            if not directory.exists():
                return []
            with directory_fd(directory) as descriptor, os.scandir(descriptor) as entries:
                names = [entry.name for entry in entries if entry.name.endswith(".json")]
            if len(names) > MAX_SITES_PER_PROJECT:
                raise SitePublicationConflict("Too many site publications")
            records = [self._read(project_id, name[:-5]) for name in sorted(names)]
            return [self._view(record) for record in sorted(
                records, key=lambda item: item.created_at, reverse=True,
            )]

    def get(self, project_id: str, site_id: str) -> SitePublicationView:
        with self.ledger.lifecycle.lease(project_id), self.ledger._lock_for(project_id):
            self.ledger.get_project(project_id)
            return self._view(self._read(project_id, site_id))

    def _html(self, record: SitePublication) -> bytes:
        artifact, raw = self.ledger.capture_artifact(
            record.project_id, record.artifact_id, limit=MAX_SITE_HTML_BYTES,
        )
        if (
            artifact.checksum != record.artifact_checksum
            or artifact.size_bytes != record.size_bytes
            or artifact.content_storage != "snapshot"
        ):
            raise SitePublicationConflict("Published artifact version changed")
        return raw

    @staticmethod
    def _validate_html(name: str, raw: bytes) -> None:
        if not name.lower().endswith((".html", ".htm")) or not raw or b"\x00" in raw:
            raise SitePublicationConflict("Select a saved HTML artifact")
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SitePublicationConflict("HTML must be UTF-8") from exc

    def stage(
        self, project_id: str, artifact_id: str, *, created_by: str,
    ) -> SitePublicationView:
        if self.public_origin is None:
            raise SitePublicationConflict("Configure a public site origin first")
        with self.ledger.lifecycle.lease(project_id), self.ledger._lock_for(project_id):
            if len(self.list(project_id)) >= MAX_SITES_PER_PROJECT:
                raise SitePublicationConflict("Too many site publications")
            artifact, raw = self.ledger.capture_artifact(
                project_id, artifact_id, limit=MAX_SITE_HTML_BYTES,
            )
            if artifact.content_storage != "snapshot":
                raise SitePublicationConflict("Save an immutable artifact version first")
            self._validate_html(artifact.name, raw)
            record = SitePublication(
                id=new_id("site"), project_id=project_id,
                artifact_id=artifact.id, artifact_name=artifact.name,
                artifact_checksum=artifact.checksum, size_bytes=artifact.size_bytes,
                origin=self.public_origin, state="draft", revision=1,
                created_by=created_by, created_at=utc_now(),
            )
            self.ledger._atomic_write(self._path(project_id, record.id), record)
            return self._view(record)

    def preview(self, project_id: str, site_id: str) -> bytes:
        with self.ledger.lifecycle.lease(project_id), self.ledger._lock_for(project_id):
            record = self._read(project_id, site_id)
            return self._html(record)

    def publish(
        self, project_id: str, site_id: str, decision: SitePublicationDecision, *,
        approved_by: str,
    ) -> SitePublicationView:
        with self.ledger.lifecycle.lease(project_id), self.ledger._lock_for(project_id):
            record = self._read(project_id, site_id)
            if record.state != "draft" or record.revision != decision.expected_revision:
                raise SitePublicationConflict("Site draft changed; review it again")
            if (
                self.public_origin is None or record.origin != self.public_origin
                or record.artifact_checksum != decision.expected_checksum
            ):
                raise SitePublicationConflict("Site origin or reviewed HTML changed")
            self._validate_html(record.artifact_name, self._html(record))
            updated = record.model_copy(update={
                "state": "published", "revision": record.revision + 1,
                "published_by": approved_by, "published_at": utc_now(),
            })
            self.ledger._atomic_write(self._path(project_id, site_id), updated)
            return self._view(updated)

    def revoke(
        self, project_id: str, site_id: str, request: SitePublicationRevoke, *,
        revoked_by: str,
    ) -> SitePublicationView:
        with self.ledger.lifecycle.lease(project_id), self.ledger._lock_for(project_id):
            record = self._read(project_id, site_id)
            if record.revision != request.expected_revision or record.state != "published":
                raise SitePublicationConflict("Site publication changed; refresh before revoking")
            updated = record.model_copy(update={
                "state": "revoked", "revision": record.revision + 1,
                "revoked_by": revoked_by, "revoked_at": utc_now(),
            })
            self.ledger._atomic_write(self._path(project_id, site_id), updated)
            return self._view(updated)

    def public_html(self, project_id: str, site_id: str) -> bytes:
        with self.ledger.lifecycle.lease(project_id), self.ledger._lock_for(project_id):
            record = self._read(project_id, site_id)
            if record.state != "published" or record.origin != self.public_origin:
                raise LedgerNotFoundError(site_id)
            return self._html(record)
