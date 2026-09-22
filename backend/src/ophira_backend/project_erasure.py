"""Reviewed whole-project erasure, with explicit independent-copy boundaries."""

import stat
from datetime import timedelta
from uuid import uuid4

from .auth import AuthorizationError, ProjectPermission
from .constants import TERMINAL_RUN_STATUSES, PrincipalRole
from .deletion_models import ErasedProject, ErasedRun, erased_run
from .erasure_files import ErasureError, FileErasure, digest, encode, operation_lease
from .erasure_selection import ErasureSelection
from .models import RunRecord, utc_now
from .project_erasure_native import select_project_native
from .project_erasure_related import project_related
from .project_erasure_templates import template_relations
from .project_scope import authorize_project_scope, project_actor
from .storage import LedgerNotFoundError

RETAINED = [
    "other_projects_and_independently_copied_templates_or_files",
    "published_shared_skills_and_global_credentials",
    "shared_agent_handwritten_files_and_unattributed_memories",
    "other_device_project_scopes_and_content_free_provenance",
    "existing_backups_external_services_and_unattributed_os_caches",
]


class ProjectErasure:
    def __init__(self, ledger, access, settings, runs, memory):
        self.ledger, self.access, self.settings = ledger, access, settings
        self.runs, self.memory = runs, memory

    def authorize(self, project_id, principal, *, allow_deleted=False):
        entry = self.ledger.read_project_entry(project_id)
        if isinstance(entry, ErasedProject):
            if not allow_deleted:
                raise LedgerNotFoundError(project_id)
            if self.settings.auth_mode == "local_only":
                if principal is not None or entry.identity_binding != "local_only":
                    raise AuthorizationError("Deleted project is outside local scope.")
            elif (
                principal is None
                or entry.identity_binding != "authenticated_principal"
                or principal.tenant_id != entry.tenant_id
                or entry.tenant_id != self.settings.trusted_gateway_tenant_id
                or principal.principal_id != entry.deleted_by
                or principal.role not in {PrincipalRole.OWNER, PrincipalRole.ADMIN}
                or principal.assertion_expires_at <= utc_now()
            ):
                raise AuthorizationError("Only the current deleting principal can retry.")
            return entry
        return authorize_project_scope(
            self.ledger,
            self.access,
            self.settings,
            project_id,
            principal,
            ProjectPermission.PROJECT_ACCESS_MANAGE,
        )

    def _select(self, project_id, principal, request_id, reviewed_at):
        project = self.authorize(project_id, principal)
        selection = ErasureSelection(
            self.ledger.root, lambda: self.authorize(project_id, principal)
        )
        runs, foreign_sessions = [], set()
        for name, info in selection.children("runs"):
            if not stat.S_ISDIR(info.st_mode):
                raise ErasureError("Run storage could not be verified.")
            value, _ = selection.record(f"runs/{name}/run.json")
            run = erased_run(value) or RunRecord.model_validate(value)
            if run.id != name:
                raise ErasureError("Run identity could not be verified.")
            if run.project_id != project_id:
                foreign_sessions.update(
                    value for value in (run.sdk_session_id, run.resume_session_id) if value
                )
                continue
            if (
                run.identity_binding != project.identity_binding
                or run.tenant_id != project.tenant_id
            ):
                raise ErasureError("Project conversation identity could not be verified.")
            if isinstance(run, RunRecord) and (
                run.status not in TERMINAL_RUN_STATUSES or self.runs.is_executing(run.id)
            ):
                raise ErasureError("Finish all project conversations before deletion.")
            runs.append(run)
        sessions = {
            value for run in runs for value in (run.sdk_session_id, run.resume_session_id) if value
        }
        if sessions & foreign_sessions:
            raise ErasureError("Project conversations share a session with another project.")
        select_project_native(selection, project_id, runs, foreign_sessions)
        actor = project_actor(principal)
        memory_count = self.memory.plan_project_erasure(
            selection,
            project,
            runs,
            deleted_by=actor,
            deleted_at=reviewed_at,
        )
        counts = project_related(selection, project, runs)
        copies = template_relations(selection, project)
        fields = {
            name: getattr(project, name)
            for name in (
                "id",
                "identity_binding",
                "tenant_id",
                "owner_principal_id",
                "created_at",
            )
        }
        marker = ErasedProject(
            **fields,
            request_id=request_id,
            review_fingerprint="0" * 64,
            deleted_at=reviewed_at,
            deleted_by=actor,
            template_copies=copies,
        )
        path = f"projects/{project_id}/project.json"
        selection.change(path, encode(marker.model_dump(mode="json")))
        selection.remove_tree(f"projects/{project_id}", keep={path})
        for run in runs:
            # Minimal run origins reserve IDs without keeping execution context.
            fields = {
                name: getattr(run, name)
                for name in (
                    "id",
                    "project_id",
                    "identity_binding",
                    "tenant_id",
                    "principal_id",
                    "trace_id",
                    "sdk_session_id",
                    "resume_session_id",
                    "started_at",
                    "created_at",
                )
            }
            marker = ErasedRun(
                **fields,
                request_id=request_id,
                review_fingerprint="0" * 64,
                deleted_at=reviewed_at,
                deleted_by=actor,
            )
            path = f"runs/{run.id}/run.json"
            selection.change(path, encode(marker.model_dump(mode="json")))
            selection.remove_tree(f"runs/{run.id}", keep={path})
        scope = dict(
            kind="project",
            project_id=project_id,
            project_revision=project.revision,
            request_id=request_id,
            reviewed_at=reviewed_at.isoformat(),
            deleted_by=actor,
            run_ids=sorted(run.id for run in runs),
            memory_count=memory_count,
            independent_copies=len(copies),
            retained=RETAINED,
            **counts,
        )
        plan, replacements = selection.plan(scope)
        return project, plan, replacements

    def preview(self, project_id, principal):
        project, plan, _ = self._select(project_id, principal, uuid4().hex, utc_now())
        return {
            **plan.scope,
            "project_name": project.name,
            "fingerprint": plan.fingerprint(),
            "file_count": len(plan.files),
            "source_bytes": sum(item.before_size or 0 for item in plan.files),
            "native_session_count": len(plan.native_sessions),
        }

    def delete(self, project_id, principal, payload):
        with operation_lease(self.ledger.root):
            entry = self.authorize(project_id, principal, allow_deleted=True)
            if isinstance(entry, ErasedProject):
                if (
                    entry.request_id != payload.request_id
                    or entry.review_fingerprint != payload.fingerprint
                ):
                    raise ErasureError("This project was deleted by a different request.")
                return {
                    "status": "deleted",
                    "project_id": project_id,
                    "request_id": payload.request_id,
                }
        if not timedelta(seconds=-60) <= utc_now() - payload.reviewed_at <= timedelta(days=1):
            raise ErasureError("Deletion review expired. Review the project again.")

        def prepare():
            project, plan, replacements = self._select(
                project_id, principal, payload.request_id, payload.reviewed_at
            )
            if (
                project.name != payload.confirm_project_name
                or plan.fingerprint() != payload.fingerprint
            ):
                raise ErasureError(
                    "Project data or confirmation changed. Review the deletion again."
                )
            for item in plan.files:
                if item.path == f"projects/{project_id}/project.json":
                    marker = ErasedProject.model_validate_json(replacements[item.path])
                elif item.path.startswith("runs/") and item.path.endswith("/run.json"):
                    marker = ErasedRun.model_validate_json(replacements[item.path])
                else:
                    continue
                marker.review_fingerprint = payload.fingerprint
                raw = encode(marker.model_dump(mode="json"))
                replacements[item.path] = raw
                item.after_size, item.after_sha256 = len(raw), digest(raw)
            plan.scope["review_fingerprint"] = payload.fingerprint
            return plan, replacements

        FileErasure(self.ledger.root).commit_prepared(
            prepare,
            authorize=lambda: self.authorize(project_id, principal),
        )
        return {"status": "deleted", "project_id": project_id, "request_id": payload.request_id}
