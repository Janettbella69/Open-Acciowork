"""Immutable, project-scoped templates and frozen reference material."""

from __future__ import annotations

import hashlib
import os
import re
import stat
from contextlib import contextmanager, nullcontext

from .artifact_files import SNAPSHOT_DIRECTORY, freeze_artifact, read_snapshot
from .auth import ROLE_PERMISSIONS, AuthorizationError, ProjectPermission
from .backup_files import parse_json, publish_exclusive, remove_staging
from .constants import PrincipalRole
from .events import redact
from .models import utc_now
from .project_scope import authorize_project_scope, project_actor, project_moderator
from .project_template_models import (
    MAX_TEMPLATE_HISTORY_BYTES,
    MAX_TEMPLATE_MUTATIONS,
    MAX_TEMPLATE_RECORD_BYTES,
    MAX_TEMPLATE_REFERENCE_BYTES,
    MAX_TEMPLATE_VERSIONS,
    MAX_TEMPLATES,
    TEMPLATE_ID,
    ProjectSeed,
    ProjectTemplate,
    TemplateArchive,
    TemplateMutation,
    TemplateReference,
    TemplateSave,
    TemplateVersion,
    fingerprint,
)
from .skill_proposal_files import directory_fd, fsync_directory, read_relative
from .storage import LedgerNotFoundError


class TemplateConflict(ValueError):
    pass


class TemplateUnavailable(ValueError):
    pass


def clean_seed(seed: ProjectSeed) -> ProjectSeed:
    fields = seed.model_dump()
    for name, value in fields.items():
        fields[name] = (
            [str(redact(item.strip())) for item in value]
            if isinstance(value, list)
            else str(redact(value.strip()))
        )
    try:
        return ProjectSeed.model_validate(fields)
    except ValueError as exc:
        raise TemplateConflict("Project text exceeds the template limits after redaction") from exc


class ProjectTemplates:
    def __init__(self, ledger, access, settings):
        self.ledger, self.access, self.settings = ledger, access, settings
        self.root = ledger.root / "collaboration" / "project-templates"
        self.directory(self.root.parent)
        self.directory(self.root)

    @staticmethod
    def directory(path):
        with directory_fd(path.parent):
            path.mkdir(mode=0o700, exist_ok=True)
        with directory_fd(path):
            pass
        return path

    def authorize(self, project_id, principal, *, write=False):
        return authorize_project_scope(
            self.ledger,
            self.access,
            self.settings,
            project_id,
            principal,
            ProjectPermission.PROJECT_WRITE if write else ProjectPermission.PROJECT_READ,
        )

    @contextmanager
    def transaction(self, project_id, principal, *, write=False):
        from .project_lifecycle import ProjectLifecycleBusyError, ProjectUnavailableError

        authority = self.access._transaction(project_id) if principal else nullcontext()
        try:
            with (
                self.ledger.lifecycle.lease(project_id),
                authority,
                self.ledger._lock_for(f"project-templates:{project_id}"),
            ):
                project = self.authorize(project_id, principal, write=write)
                self.directory(self.root / self.ledger._safe_id(project_id))
                yield project
                self.authorize(project_id, principal, write=write)
        except ProjectUnavailableError as exc:
            raise LedgerNotFoundError(project_id) from exc
        except ProjectLifecycleBusyError as exc:
            raise TemplateConflict("Project has active operations; retry later") from exc

    def folder(self, project_id, template_id):
        if not re.fullmatch(TEMPLATE_ID, template_id):
            raise LedgerNotFoundError("Project template not found")
        return self.root / self.ledger._safe_id(project_id) / template_id

    def load(self, project_id, template_id):
        folder = self.folder(project_id, template_id)
        try:
            raw = read_relative(folder, "template.json", MAX_TEMPLATE_RECORD_BYTES)
            record = ProjectTemplate.model_validate(parse_json(raw))
        except FileNotFoundError as exc:
            raise LedgerNotFoundError("Project template not found") from exc
        except (OSError, ValueError) as exc:
            raise TemplateUnavailable("Project template could not be verified") from exc
        project = self.ledger.get_project(project_id)
        if (
            record.id != template_id
            or record.project_id != project_id
            or record.identity_binding != project.identity_binding
            or record.tenant_id != project.tenant_id
        ):
            raise TemplateUnavailable("Project template has a different scope")
        return record

    def write(self, path, record):
        validated = type(record).model_validate(record.model_dump())
        if len((self.ledger._dump(validated) + "\n").encode()) > MAX_TEMPLATE_RECORD_BYTES:
            raise TemplateConflict("Project template history is full")
        with directory_fd(path.parent):
            if path.is_symlink():
                raise TemplateUnavailable("Project template history must not be linked")
            self.ledger._atomic_write(path, validated)

    def ids(self, project_id):
        root = self.root / self.ledger._safe_id(project_id)
        identifiers = []
        with directory_fd(root) as descriptor, os.scandir(descriptor) as entries:
            for index, entry in enumerate(entries):
                if index >= MAX_TEMPLATES * 2:
                    raise TemplateConflict("Project template storage has reached its entry limit")
                if not entry.is_dir(follow_symlinks=False):
                    raise TemplateUnavailable("Project template storage contains an unsafe entry")
                if re.fullmatch(TEMPLATE_ID, entry.name):
                    identifiers.append(entry.name)
                elif not (entry.name.startswith(".template_") and entry.name.endswith(".stage")):
                    raise TemplateUnavailable("Project template storage contains an unknown entry")
        if len(identifiers) > MAX_TEMPLATES:
            raise TemplateConflict("Project template capacity reached")
        return identifiers

    @staticmethod
    def can_apply(principal):
        return principal is None or principal.role == PrincipalRole.OWNER

    @staticmethod
    def can_manage(record, principal):
        allowed = (
            principal is None or ProjectPermission.PROJECT_WRITE in ROLE_PERMISSIONS[principal.role]
        )
        return allowed and (
            record.created_by == project_actor(principal) or project_moderator(principal)
        )

    def view(self, record, principal):
        current = record.versions[-1]
        return {
            "id": record.id,
            "project_id": record.project_id,
            "created_by": record.created_by,
            "revision": record.revision,
            "archived": record.archived,
            "title": current.title,
            "description": current.description,
            "latest_version": current.version,
            "content_hash": current.content_hash,
            "created_at": record.history[0].at,
            "updated_at": record.history[-1].at,
            "can_manage": self.can_manage(record, principal),
            "can_apply": self.can_apply(principal) and not record.archived,
            "versions": [
                {
                    "version": item.version,
                    "title": item.title,
                    "content_hash": item.content_hash,
                    "created_by": item.created_by,
                    "created_at": item.created_at,
                    "file_count": len(item.references),
                    "size_bytes": sum(row.size_bytes for row in item.references),
                }
                for item in reversed(record.versions)
            ],
        }

    def list(self, project_id, principal):
        with self.transaction(project_id, principal):
            records = [self.load(project_id, item) for item in self.ids(project_id)]
            records.sort(key=lambda item: (item.history[0].at, item.id), reverse=True)
            return {
                "project_id": project_id,
                "actor_id": project_actor(principal),
                "can_save": principal is None
                or ProjectPermission.PROJECT_WRITE in ROLE_PERMISSIONS[principal.role],
                "can_create_project": self.can_apply(principal),
                "items": [self.view(item, principal) for item in records],
            }

    def detail(self, project_id, template_id, version, principal):
        with self.transaction(project_id, principal):
            record = self.load(project_id, template_id)
            selected = self.version(record, version)
            return {
                "project_id": project_id,
                "template": self.view(record, principal),
                "version": selected.model_dump(mode="json"),
            }

    @staticmethod
    def version(record, version):
        if not 1 <= version <= len(record.versions):
            raise LedgerNotFoundError("Project template version not found")
        return record.versions[version - 1]

    def reference(self, project_id, template_id, version, artifact_id, principal):
        with self.transaction(project_id, principal):
            record = self.load(project_id, template_id)
            item = next(
                (
                    item
                    for item in self.version(record, version).references
                    if item.artifact_id == artifact_id
                ),
                None,
            )
            if item is None:
                raise LedgerNotFoundError("Template reference not found")
            try:
                raw = read_snapshot(
                    self.folder(project_id, template_id), item.checksum, item.size_bytes
                )
            except (ValueError, OSError) as exc:
                raise TemplateUnavailable("Template reference could not be verified") from exc
            if len(raw) != item.size_bytes:
                raise TemplateUnavailable("Template reference size does not match")
            return item, raw

    def freeze(self, folder, captured):
        existing = {}
        history = folder / SNAPSHOT_DIRECTORY
        if history.exists() or history.is_symlink():
            with directory_fd(history) as descriptor, os.scandir(descriptor) as entries:
                for index, entry in enumerate(entries):
                    info = entry.stat(follow_symlinks=False)
                    if index >= 2000 or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                        raise TemplateUnavailable("Template reference storage is unsafe")
                    existing[entry.name] = info.st_size
        additions = {
            item.checksum: len(raw) for item, raw in captured if item.checksum not in existing
        }
        if sum(existing.values()) + sum(additions.values()) > MAX_TEMPLATE_HISTORY_BYTES:
            raise TemplateConflict("Template reference storage is full")
        for item, raw in captured:
            if freeze_artifact(folder, raw) != item.checksum:
                raise TemplateUnavailable("Template reference checksum does not match")

    def capture_version(self, project_id, payload, principal, version):
        with self.ledger._lock_for(project_id):
            project = self.authorize(project_id, principal, write=True)
            captured, remaining = [], MAX_TEMPLATE_REFERENCE_BYTES
            for identifier in payload.artifact_ids:
                source = next((item for item in project.artifacts if item.id == identifier), None)
                if source is None:
                    raise LedgerNotFoundError("Template reference not found in this project")
                if source.size_bytes > remaining:
                    raise TemplateConflict("Selected references exceed 50 MiB")
                try:
                    item, raw = self.ledger.capture_artifact(
                        project_id, identifier, limit=remaining
                    )
                except (ValueError, OSError) as exc:
                    raise TemplateUnavailable("Source reference could not be verified") from exc
                reference = TemplateReference(
                    artifact_id=item.id,
                    name=str(redact(item.name))[:240],
                    mime=item.mime,
                    version=item.version,
                    checksum=item.checksum,
                    size_bytes=len(raw),
                )
                captured.append((reference, raw))
                remaining -= len(raw)
            body = {
                "version": version,
                "title": str(redact(payload.title.strip()))[:160],
                "description": str(redact(payload.description.strip()))[:2000],
                "seed": clean_seed(payload.seed).model_dump(mode="json"),
                "references": [item.model_dump(mode="json") for item, _ in captured],
                "source_project_revision": project.revision,
                "created_by": project_actor(principal),
                "created_at": utc_now().isoformat().replace("+00:00", "Z"),
            }
            return TemplateVersion(**body, content_hash=fingerprint(body)), captured

    def save(self, project_id, payload: TemplateSave, principal, *, template_id=None):
        actor, request_hash = project_actor(principal), fingerprint(payload)
        creating = template_id is None
        if creating:
            if payload.expected_revision is not None:
                raise ValueError("A new template must not claim an existing revision")
            template_id = (
                "template_"
                + hashlib.sha256(
                    f"{project_id}\0{actor}\0{payload.request_id}".encode()
                ).hexdigest()[:32]
            )
        elif payload.expected_revision is None:
            raise ValueError("A new version requires the current template revision")
        with self.transaction(project_id, principal, write=True) as project:
            try:
                record = self.load(project_id, template_id)
            except LedgerNotFoundError:
                if not creating:
                    raise
                record = None
            if record is not None:
                if not self.can_manage(record, principal):
                    raise AuthorizationError(
                        "Template management requires its author or an administrator"
                    )
                previous = next(
                    (
                        row
                        for row in record.history
                        if row.actor_id == actor and row.request_id == payload.request_id
                    ),
                    None,
                )
                if previous:
                    if previous.request_hash != request_hash:
                        raise TemplateConflict("This template request has different content")
                    return self.view(record, principal)
                if creating or record.revision != payload.expected_revision:
                    raise TemplateConflict("Template changed; reload before saving")
                if record.archived:
                    raise TemplateConflict("Restore this template before creating another version")
                if len(record.versions) >= MAX_TEMPLATE_VERSIONS:
                    raise TemplateConflict("Template version limit reached")
                if len(record.history) >= MAX_TEMPLATE_MUTATIONS:
                    raise TemplateConflict("Project template history is full")
            elif len(self.ids(project_id)) >= MAX_TEMPLATES:
                raise TemplateConflict("Project template capacity reached")
            version, captured = self.capture_version(
                project_id, payload, principal, len(record.versions) + 1 if record else 1
            )
            entry = TemplateMutation(
                revision=record.revision + 1 if record else 1,
                request_id=payload.request_id,
                request_hash=request_hash,
                actor_id=actor,
                action="version" if record else "create",
                version=version.version,
                at=version.created_at,
            )
            updated = (
                record.model_copy(
                    update={
                        "versions": [*record.versions, version],
                        "history": [*record.history, entry],
                    }
                )
                if record
                else ProjectTemplate(
                    id=template_id,
                    project_id=project_id,
                    identity_binding=project.identity_binding,
                    tenant_id=project.tenant_id,
                    created_by=actor,
                    versions=[version],
                    history=[entry],
                )
            )
            folder = self.folder(project_id, template_id)
            if creating:
                stage = folder.with_name(f".{template_id}.stage")
                if stage.exists() or stage.is_symlink():
                    with directory_fd(stage):
                        remove_staging(stage)
                self.directory(stage)
                self.freeze(stage, captured)
                self.write(stage / "template.json", updated)
                self.authorize(project_id, principal, write=True)
                publish_exclusive(stage, folder)
                fsync_directory(folder.parent)
            else:
                self.freeze(folder, captured)
                self.authorize(project_id, principal, write=True)
                self.write(folder / "template.json", updated)
            return self.view(updated, principal)

    def archive(self, project_id, template_id, payload: TemplateArchive, principal):
        with self.transaction(project_id, principal, write=True):
            record = self.load(project_id, template_id)
            if not self.can_manage(record, principal):
                raise AuthorizationError(
                    "Template management requires its author or an administrator"
                )
            actor, request_hash = project_actor(principal), fingerprint(payload)
            previous = next(
                (
                    row
                    for row in record.history
                    if row.actor_id == actor and row.request_id == payload.request_id
                ),
                None,
            )
            if previous:
                if previous.request_hash != request_hash:
                    raise TemplateConflict("This template request has different content")
                return self.view(record, principal)
            if record.revision != payload.expected_revision:
                raise TemplateConflict("Template changed; reload before saving")
            if record.archived == payload.archived:
                raise TemplateConflict("Template is already in that state")
            if len(record.history) >= MAX_TEMPLATE_MUTATIONS:
                raise TemplateConflict("Project template history is full")
            entry = TemplateMutation(
                revision=record.revision + 1,
                request_id=payload.request_id,
                request_hash=request_hash,
                actor_id=actor,
                action="archive" if payload.archived else "restore",
                version=len(record.versions),
                at=utc_now(),
            )
            updated = record.model_copy(update={"history": [*record.history, entry]})
            self.authorize(project_id, principal, write=True)
            self.write(self.folder(project_id, template_id) / "template.json", updated)
            return self.view(updated, principal)
