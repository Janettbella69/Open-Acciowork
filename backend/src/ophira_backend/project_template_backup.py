"""Read-only verification of template and completed creation links in cold copies."""

import re

from .artifact_files import read_snapshot
from .backup_files import BackupError, parse_json
from .project_template_apply import TemplateApplications
from .project_template_models import (
    APPLICATION_ID,
    MAX_TEMPLATE_APPLICATIONS,
    MAX_TEMPLATE_RECORD_BYTES,
    MAX_TEMPLATES,
    TEMPLATE_ID,
    ProjectTemplate,
    TemplateApplication,
    fingerprint,
)
from .skill_proposal_files import read_relative


def inspect_project_templates(data, projects, erased_projects=None):
    erased_projects = erased_projects or {}
    root = data / "collaboration" / "project-templates"
    origins = {
        item.template_origin.application_id: item
        for item in projects.values()
        if item.template_origin
    }
    completed = set()
    for application_id, target in origins.items():
        origin = target.template_origin
        source = erased_projects.get(origin.source_project_id)
        if source is None:
            continue
        if (source.template_copies.get(target.id) != fingerprint(origin)
                or source.identity_binding != target.identity_binding
                or source.tenant_id != target.tenant_id):
            raise BackupError("deleted_template_source_mismatch")
        by_id = {row.id: row for row in target.artifacts}
        for item in origin.references:
            artifact = by_id.get(item.artifact_id)
            if (artifact is None or artifact.checksum != item.source.checksum
                    or artifact.size_bytes != item.source.size_bytes):
                raise BackupError("deleted_template_copy_mismatch")
        completed.add(application_id)
    if len(origins) != sum(item.template_origin is not None for item in projects.values()):
        raise BackupError("project_template_origin_invalid")
    if root.exists():
        try:
            for directory in root.iterdir():
                source = projects[directory.name]
                families = list(directory.iterdir())
                if len(families) > MAX_TEMPLATES:
                    raise ValueError("too many templates")
                for family in families:
                    if family.name.startswith(".") and family.name.endswith(".stage"):
                        raise BackupError("pending_project_template")
                    if not re.fullmatch(TEMPLATE_ID, family.name):
                        raise ValueError("invalid template name")
                    template = ProjectTemplate.model_validate(
                        parse_json(
                            read_relative(
                                family,
                                "template.json",
                                MAX_TEMPLATE_RECORD_BYTES,
                            )
                        )
                    )
                    if (
                        template.id != family.name
                        or template.project_id != source.id
                        or template.identity_binding != source.identity_binding
                        or template.tenant_id != source.tenant_id
                    ):
                        raise ValueError("template scope mismatch")
                    for version in template.versions:
                        for reference in version.references:
                            if (
                                len(read_snapshot(family, reference.checksum, reference.size_bytes))
                                != reference.size_bytes
                            ):
                                raise ValueError("reference size mismatch")
                    applications = family / "applications"
                    if not applications.exists():
                        continue
                    rows = list(applications.iterdir())
                    if len(rows) > MAX_TEMPLATE_APPLICATIONS:
                        raise ValueError("too many creations")
                    for path in rows:
                        if path.name.startswith(".") and path.name.endswith(".stage"):
                            raise BackupError("pending_template_creation")
                        if path.name.startswith(".") and path.name.endswith(".tmp"):
                            continue
                        if path.suffix != ".json" or not re.fullmatch(APPLICATION_ID, path.stem):
                            raise ValueError("invalid creation name")
                        record = TemplateApplication.model_validate(
                            parse_json(
                                read_relative(
                                    applications,
                                    path.name,
                                    MAX_TEMPLATE_RECORD_BYTES,
                                )
                            )
                        )
                        if record.state != "applied":
                            raise BackupError("pending_template_creation")
                        version = template.versions[record.template_version - 1]
                        if (
                            record.id + ".json" != path.name
                            or record.source_project_id != source.id
                            or record.template_id != template.id
                            or record.template_hash != version.content_hash
                            or not set(record.request.artifact_ids)
                            <= {row.artifact_id for row in version.references}
                        ):
                            raise ValueError("creation scope mismatch")
                        target = projects[record.project_id]
                        if (
                            target.identity_binding != source.identity_binding
                            or target.tenant_id != source.tenant_id
                            or target.owner_principal_id
                            != (
                                record.actor_id
                                if source.identity_binding == "authenticated_principal"
                                else None
                            )
                        ):
                            raise ValueError("creation owner mismatch")
                        TemplateApplications.validate_origin(record, version, target)
                        by_id = {row.id: row for row in target.artifacts}
                        for item in target.template_origin.references:
                            artifact = by_id[item.artifact_id]
                            if (
                                artifact.checksum != item.source.checksum
                                or artifact.size_bytes != item.source.size_bytes
                            ):
                                raise ValueError("copied reference mismatch")
                        completed.add(record.id)
        except BackupError:
            raise
        except (OSError, ValueError, KeyError, IndexError) as exc:
            raise BackupError("project_templates_invalid") from exc
    if set(origins) != completed:
        raise BackupError("project_template_origin_invalid")
