"""Keep independent template copies verifiable after their source is erased."""

from .deletion_models import ErasedProject, erased_project
from .erasure_files import encode
from .models import OpportunityProject
from .project_template_apply import TemplateApplications
from .project_template_models import ProjectTemplate, TemplateApplication, fingerprint


def validate_copy(selection, project):
    origin = project.template_origin
    family = f"collaboration/project-templates/{origin.source_project_id}/{origin.template_id}"
    value, _ = selection.record(f"{family}/template.json")
    template = ProjectTemplate.model_validate(value)
    if (
        template.id != origin.template_id
        or template.project_id != origin.source_project_id
        or template.tenant_id != project.tenant_id
        or template.identity_binding != project.identity_binding
        or origin.version > len(template.versions)
    ):
        raise ValueError("Template copy source does not match its project.")
    path = f"{family}/applications/{origin.application_id}.json"
    value, _ = selection.record(path)
    application = TemplateApplication.model_validate(value)
    version = template.versions[origin.version - 1]
    if (
        application.state != "applied"
        or application.template_version != origin.version
        or application.template_hash != version.content_hash
    ):
        raise ValueError("Finish template creation recovery before project deletion.")
    TemplateApplications.validate_origin(application, version, project)
    return family, path, application


def template_relations(selection, project):
    copies = {}
    for name, _ in selection.children("projects"):
        value, _ = selection.record(f"projects/{name}/project.json")
        other = erased_project(value) or OpportunityProject.model_validate(value)
        if other.id != name:
            raise ValueError("Project identity is invalid.")
        if isinstance(other, ErasedProject) or other.template_origin is None:
            continue
        origin = other.template_origin
        if origin.source_project_id != project.id:
            continue
        if (
            other.tenant_id != project.tenant_id
            or other.identity_binding != project.identity_binding
        ):
            raise ValueError("Template copy crosses project identity scope.")
        validate_copy(selection, other)
        copies[other.id] = fingerprint(origin)
    origin = project.template_origin
    if origin is not None:
        value, _ = selection.record(f"projects/{origin.source_project_id}/project.json")
        parent = erased_project(value)
        if parent is not None:
            if (
                parent.id != origin.source_project_id
                or parent.tenant_id != project.tenant_id
                or parent.identity_binding != project.identity_binding
                or parent.template_copies.get(project.id) != fingerprint(origin)
            ):
                raise ValueError("Deleted template source proof does not match.")
            del parent.template_copies[project.id]
            selection.change(
                f"projects/{parent.id}/project.json", encode(parent.model_dump(mode="json"))
            )
        else:
            parent = OpportunityProject.model_validate(value)
            if (parent.id != origin.source_project_id or parent.tenant_id != project.tenant_id
                    or parent.identity_binding != project.identity_binding):
                raise ValueError("Template source project identity does not match.")
            family, path, application = validate_copy(selection, project)
            selection.change(path)
            selection.remove_entry(f"{family}/applications", f".{application.id}.stage")
    selection.remove_entry("collaboration/project-templates", project.id)
    return copies
