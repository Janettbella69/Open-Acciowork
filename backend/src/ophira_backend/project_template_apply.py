"""Journaled creation of complete projects from reviewed template versions."""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

from .artifact_files import SNAPSHOT_DIRECTORY, read_snapshot
from .auth import AuthorizationError
from .backup_files import (
    BackupLimits,
    parse_json,
    publish_exclusive,
    remove_staging,
    sync_directories,
    tree_inventory,
)
from .deletion_models import ErasedProject
from .models import ProjectCreate, utc_now
from .project_scope import project_actor
from .project_template_models import (
    APPLICATION_ID,
    MAX_TEMPLATE_APPLICATIONS,
    MAX_TEMPLATE_RECORD_BYTES,
    MAX_TEMPLATE_REFERENCE_BYTES,
    AppliedTemplateReference,
    ProjectTemplateOrigin,
    TemplateApplication,
    TemplateApply,
    fingerprint,
)
from .project_templates import TemplateConflict, TemplateUnavailable, clean_seed
from .skill_proposal_files import child_directory, directory_fd, fsync_directory, read_relative
from .storage import FileLedger, LedgerNotFoundError

STAGE_LIMITS = BackupLimits(
    files=250, entries=500, file_bytes=MAX_TEMPLATE_REFERENCE_BYTES, total_bytes=150 * 1024 * 1024
)


class TemplateApplications:
    def __init__(self, templates):
        self.templates = templates
        self.ledger = templates.ledger

    def root(self, project_id, template_id):
        return self.templates.directory(
            self.templates.folder(project_id, template_id) / "applications"
        )

    def path(self, project_id, template_id, application_id):
        if not re.fullmatch(APPLICATION_ID, application_id):
            raise LedgerNotFoundError("Template project creation not found")
        return self.root(project_id, template_id) / f"{application_id}.json"

    def load(self, project_id, template_id, application_id):
        path = self.path(project_id, template_id, application_id)
        try:
            record = TemplateApplication.model_validate(
                parse_json(
                    read_relative(
                        path.parent,
                        path.name,
                        MAX_TEMPLATE_RECORD_BYTES,
                    )
                )
            )
        except FileNotFoundError as exc:
            raise LedgerNotFoundError("Template project creation not found") from exc
        except (ValueError, OSError) as exc:
            raise TemplateUnavailable("Saved project creation could not be verified") from exc
        if (
            record.id != application_id
            or record.source_project_id != project_id
            or record.template_id != template_id
        ):
            raise TemplateUnavailable("Saved project creation has a different scope")
        return record

    @staticmethod
    def require_creator(principal, record=None):
        from .project_templates import ProjectTemplates

        if not ProjectTemplates.can_apply(principal) or (
            record and record.actor_id != project_actor(principal)
        ):
            raise AuthorizationError("Creating a project requires its authenticated owner")

    def ids(self, project_id, template_id):
        identifiers = []
        with directory_fd(self.root(project_id, template_id)) as fd, os.scandir(fd) as entries:
            for index, entry in enumerate(entries):
                if index >= MAX_TEMPLATE_APPLICATIONS * 2:
                    raise TemplateConflict("Template project creation history is full")
                if entry.name.startswith(".") and entry.name.endswith(".stage"):
                    if not entry.is_dir(follow_symlinks=False):
                        raise TemplateUnavailable("Project creation staging must not be linked")
                    continue
                if entry.name.startswith(".") and entry.name.endswith(".tmp"):
                    continue
                if not entry.name.endswith(".json") or not re.fullmatch(
                    APPLICATION_ID, entry.name[:-5]
                ):
                    raise TemplateUnavailable("Project creation storage contains an unknown entry")
                identifiers.append(entry.name[:-5])
        if len(identifiers) > MAX_TEMPLATE_APPLICATIONS:
            raise TemplateConflict("Template project creation history is full")
        return identifiers

    def version_for(self, record, template):
        version = self.templates.version(template, record.template_version)
        if version.content_hash != record.template_hash or not set(record.request.artifact_ids) <= {
            row.artifact_id for row in version.references
        }:
            raise TemplateUnavailable("Project creation references another template version")
        return version

    def view(self, record, template):
        version = self.version_for(record, template)
        return {
            "id": record.id,
            "source_project_id": record.source_project_id,
            "template_id": record.template_id,
            "template_version": record.template_version,
            "project_id": record.project_id,
            "state": record.state,
            "created_at": record.created_at,
            "created_by": record.actor_id,
            "seed": record.request.seed.model_dump(mode="json"),
            "references": [
                row.model_dump(mode="json")
                for row in version.references
                if row.artifact_id in record.request.artifact_ids
            ],
            "can_resume": record.state != "applied" and not template.archived,
        }

    def list(self, project_id, template_id, principal, *, before=None, limit=20):
        with self.templates.transaction(project_id, principal):
            self.require_creator(principal)
            template = self.templates.load(project_id, template_id)
            records = [
                self.load(project_id, template_id, identifier)
                for identifier in self.ids(project_id, template_id)
            ]
            records = [row for row in records if row.actor_id == project_actor(principal)]
            records.sort(key=lambda row: (row.created_at, row.id), reverse=True)
            if before:
                boundary = next((row for row in records if row.id == before), None)
                if boundary is None:
                    raise LedgerNotFoundError("Project creation cursor not found")
                records = [
                    row
                    for row in records
                    if (row.created_at, row.id) < (boundary.created_at, boundary.id)
                ]
            page = records[:limit]
            return {
                "project_id": project_id,
                "template_id": template_id,
                "items": [self.view(row, template) for row in page],
                "next_before": page[-1].id if len(records) > limit else None,
            }

    def apply(self, project_id, template_id, payload: TemplateApply, principal):
        with self.templates.transaction(project_id, principal):
            self.require_creator(principal)
            template = self.templates.load(project_id, template_id)
            version = self.templates.version(template, payload.version)
            if version.content_hash != payload.content_hash:
                raise TemplateConflict("Template version changed; review it again")
            if not set(payload.artifact_ids) <= {row.artifact_id for row in version.references}:
                raise ValueError("Selected file is outside this template version")
            actor, request_hash = project_actor(principal), fingerprint(payload)
            identifier = (
                "template_use_"
                + hashlib.sha256(
                    f"{project_id}\0{template_id}\0{actor}\0{payload.request_id}".encode()
                ).hexdigest()[:32]
            )
            target_id = "project_template_" + identifier[-32:]
            try:
                existing = self.ledger.read_project_entry(target_id)
            except LedgerNotFoundError:
                pass
            else:
                if isinstance(existing, ErasedProject):
                    raise TemplateConflict("This template creation was permanently deleted")
            try:
                record = self.load(project_id, template_id, identifier)
            except LedgerNotFoundError:
                if template.archived:
                    raise TemplateConflict(
                        "Restore this template before creating a project"
                    ) from None
                if len(self.ids(project_id, template_id)) >= MAX_TEMPLATE_APPLICATIONS:
                    raise TemplateConflict("Template project creation history is full") from None
                cleaned = payload.model_copy(update={"seed": clean_seed(payload.seed)})
                record = TemplateApplication(
                    id=identifier,
                    source_project_id=project_id,
                    template_id=template_id,
                    template_version=version.version,
                    template_hash=version.content_hash,
                    project_id="project_template_" + identifier[-32:],
                    actor_id=actor,
                    request_id=payload.request_id,
                    request_hash=request_hash,
                    request=cleaned,
                    input_hash=fingerprint(cleaned),
                    created_at=utc_now(),
                )
                self.templates.write(self.path(project_id, template_id, identifier), record)
            self.require_creator(principal, record)
            if record.request_hash != request_hash:
                raise TemplateConflict("This project creation request has different content")
            return self.finish(record, template, principal)

    def resume(self, project_id, template_id, application_id, principal):
        with self.templates.transaction(project_id, principal):
            record = self.load(project_id, template_id, application_id)
            self.require_creator(principal, record)
            template = self.templates.load(project_id, template_id)
            return self.finish(record, template, principal)

    def stage_root(self, record):
        return self.root(record.source_project_id, record.template_id) / f".{record.id}.stage"

    def discard(self, stage):
        if stage.exists() or stage.is_symlink():
            # A damaged or linked directory is an integrity problem, not an invitation to delete it.
            tree_inventory(stage, STAGE_LIMITS)
            remove_staging(stage)

    def prepare(self, record, version, principal):
        stage = self.stage_root(record)
        self.discard(stage)
        self.templates.directory(stage)
        staged = FileLedger(stage, ingest_config=self.ledger.ingest_config)
        project = staged.create_project(
            ProjectCreate(**record.request.seed.model_dump()),
            principal=principal,
            project_id=record.project_id,
        )
        references = []
        source = self.templates.folder(record.source_project_id, record.template_id)
        for index, reference in enumerate(version.references, 1):
            if reference.artifact_id not in record.request.artifact_ids:
                continue
            try:
                raw = read_snapshot(source, reference.checksum, reference.size_bytes)
            except (ValueError, OSError) as exc:
                raise TemplateUnavailable("Template reference could not be verified") from exc
            if len(raw) != reference.size_bytes:
                raise TemplateUnavailable("Template reference size does not match")
            safe = Path(staged._safe_filename(reference.name))
            filename = f"template_{index:02d}_{safe.stem[:200]}{safe.suffix[:16]}"
            artifact = staged.save_upload(project.id, filename, raw, mime=reference.mime)
            references.append(AppliedTemplateReference(artifact_id=artifact.id, source=reference))
        origin = ProjectTemplateOrigin(
            source_project_id=record.source_project_id,
            template_id=record.template_id,
            version=record.template_version,
            content_hash=record.template_hash,
            seed_hash=fingerprint(record.request.seed),
            title=version.title,
            application_id=record.id,
            applied_by=record.actor_id,
            created_at=record.created_at,
            references=references,
        )

        def provenance(project):
            project.template_origin = origin
            for artifact in project.artifacts:
                artifact.created_by = record.actor_id

        staged.update_project(project.id, provenance)
        directory = staged._project_path(project.id).parent
        inventory = tree_inventory(directory, STAGE_LIMITS)
        for row in inventory["files"]:
            parts = tuple(row["path"].split("/"))
            with directory_fd(directory) as root, child_directory(root, parts[:-1]) as parent:
                descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        sync_directories(directory)
        raw = read_relative(directory, "project.json", MAX_TEMPLATE_RECORD_BYTES)
        ready = record.model_copy(
            update={"state": "ready", "prepared_project_hash": hashlib.sha256(raw).hexdigest()}
        )
        self.validate_stage(ready, version, staged)
        self.templates.write(
            self.path(record.source_project_id, record.template_id, record.id), ready
        )
        return ready

    @staticmethod
    def validate_origin(record, version, project):
        origin = project.template_origin
        expected = [
            row for row in version.references if row.artifact_id in record.request.artifact_ids
        ]
        if (
            origin is None
            or project.id != record.project_id
            or origin.application_id != record.id
            or origin.source_project_id != record.source_project_id
            or origin.template_id != record.template_id
            or origin.version != version.version
            or origin.content_hash != record.template_hash
            or origin.seed_hash != fingerprint(record.request.seed)
            or origin.applied_by != record.actor_id
            or origin.title != version.title
            or origin.created_at != record.created_at
            or [row.source for row in origin.references] != expected
        ):
            raise TemplateUnavailable("Created project has a different template origin")

    def validate_stage(self, record, version, staged):
        directory = staged._project_path(record.project_id).parent
        raw = read_relative(directory, "project.json", MAX_TEMPLATE_RECORD_BYTES)
        if hashlib.sha256(raw).hexdigest() != record.prepared_project_hash:
            raise TemplateUnavailable("Prepared project changed before publication")
        project = staged.get_project(record.project_id)
        self.validate_origin(record, version, project)
        if (
            project.hypotheses
            or project.evidence
            or project.decisions
            or project.assessments
            or project.stage_decisions
            or project.name != record.request.seed.name
            or project.objective != record.request.seed.objective
            or project.product_brief != record.request.seed.product_brief
            or project.target_markets != record.request.seed.target_markets
            or project.constraints != record.request.seed.constraints
        ):
            raise TemplateUnavailable("Prepared project does not match its reviewed seed")
        allowed = {"project.json"}
        for artifact in project.artifacts:
            staged.capture_artifact(project.id, artifact.id)
            workspace = read_relative(
                directory, f"workspace/{artifact.path}", STAGE_LIMITS.file_bytes
            )
            if hashlib.sha256(workspace).hexdigest() != artifact.checksum:
                raise TemplateUnavailable("Prepared reference file does not match its snapshot")
            allowed.update(
                {f"workspace/{artifact.path}", f"{SNAPSHOT_DIRECTORY}/{artifact.checksum}"}
            )
        if {row["path"] for row in tree_inventory(directory, STAGE_LIMITS)["files"]} != allowed:
            raise TemplateUnavailable("Prepared project contains unexpected files")
        by_id = {row.id: row for row in project.artifacts}
        for reference in project.template_origin.references:
            item = by_id.get(reference.artifact_id)
            if (
                item is None
                or item.checksum != reference.source.checksum
                or item.size_bytes != reference.source.size_bytes
            ):
                raise TemplateUnavailable("Prepared project reference origin does not match")

    def finish(self, record, template, principal):
        version = self.version_for(record, template)
        target = self.ledger._project_path(record.project_id).parent
        path = self.path(record.source_project_id, record.template_id, record.id)
        with self.ledger._lock_for(record.project_id):
            if target.exists() or target.is_symlink():
                if record.state == "preparing":
                    raise TemplateConflict("Project creation target already exists")
                with directory_fd(target):
                    project = self.templates.authorize(record.project_id, principal)
                self.validate_origin(record, version, project)
            else:
                if record.state == "applied":
                    raise TemplateUnavailable("The created project is no longer available")
                if template.archived:
                    raise TemplateConflict("Restore this template before creating a project")
                if record.state == "preparing":
                    record = self.prepare(record, version, principal)
                stage = self.stage_root(record)
                with directory_fd(stage):
                    staged = FileLedger(stage, ingest_config=self.ledger.ingest_config)
                    self.validate_stage(record, version, staged)
                staged_project = staged.get_project(record.project_id)
                source_project = self.templates.authorize(record.source_project_id, principal)
                if (
                    staged_project.identity_binding != source_project.identity_binding
                    or staged_project.tenant_id != source_project.tenant_id
                    or staged_project.owner_principal_id != (record.actor_id if principal else None)
                ):
                    raise TemplateUnavailable("Prepared project has a different owner scope")
                if principal:
                    self.templates.access.bind_project(record.project_id, principal)
                self.templates.authorize(record.source_project_id, principal)
                publish_exclusive(staged._project_path(record.project_id).parent, target)
                fsync_directory(target.parent)
                project = self.templates.authorize(record.project_id, principal)
                self.validate_origin(record, version, project)
            applied = record.model_copy(update={"state": "applied"})
            self.templates.write(path, applied)
            self.discard(self.stage_root(record))
            return {"project": project, "application": self.view(applied, template)}
