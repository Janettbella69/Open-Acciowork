"""Owner/admin reviewed conversation deletion, preserving saved project outputs."""

import stat
from datetime import timedelta
from uuid import uuid4

from .auth import ProjectPermission
from .constants import TERMINAL_RUN_STATUSES
from .conversation_erasure_context import shared_context
from .deletion_models import ErasedRun, erased_run
from .erasure_files import ErasureError, FileErasure, encode, operation_lease
from .erasure_selection import ErasureSelection
from .models import RunRecord, utc_now
from .project_scope import authorize_project_scope, project_actor
from .sdk_session_erasure import find_native_session, select_native_side_data
from .storage import LedgerNotFoundError

RETAINED = [
    "saved_project_files_artifacts_evidence_decisions_checkpoints",
    "saved_comments_templates_skills_and_commercial_audit_records",
    "other_conversations_and_independent_copies",
    "schedule_intent_bookmarks_and_content_free_provenance",
    "credentials_browser_logins_external_services_and_existing_backups",
    "unattributed_runtime_caches_and_os_temporary_files",
]


class ConversationErasure:
    def __init__(self, ledger, access, settings, runs, memory):
        self.ledger, self.access, self.settings = ledger, access, settings
        self.runs, self.memory = runs, memory

    def authorize(self, project_id, principal):
        project = authorize_project_scope(
            self.ledger,
            self.access,
            self.settings,
            project_id,
            principal,
            ProjectPermission.PROJECT_ACCESS_MANAGE,
        )
        if project.id != project_id:
            raise ErasureError("Project identity could not be verified.")
        return project

    def _select(self, project_id, run_id, principal, request_id, reviewed_at):
        project = self.authorize(project_id, principal)
        selection = ErasureSelection(
            self.ledger.root, lambda: self.authorize(project_id, principal)
        )
        entries = []
        for name, info in selection.children("runs"):
            if not stat.S_ISDIR(info.st_mode):
                raise ErasureError("Run storage could not be verified.")
            value, _ = selection.record(f"runs/{name}/run.json")
            item = erased_run(value) or RunRecord.model_validate(value)
            if item.id != name:
                raise ErasureError("Run identity could not be verified.")
            entries.append(item)
        seed = next(
            (
                item
                for item in entries
                if item.id == run_id
                and item.project_id == project_id
                and isinstance(item, RunRecord)
            ),
            None,
        )
        if seed is None:
            raise LedgerNotFoundError(run_id)
        ids, sessions = (
            {seed.id},
            {value for value in (seed.sdk_session_id, seed.resume_session_id) if value},
        )
        # Include all durable continuation edges, including a failed resume which
        # did not receive a new session-init event. Cross-project edges fail closed.
        while True:
            before = len(ids)
            for run in entries:
                related = {value for value in (run.sdk_session_id, run.resume_session_id) if value}
                if (
                    run.id in ids
                    or sessions & related
                    or getattr(run, "resume_source_run_id", None) in ids
                ):
                    if isinstance(run, ErasedRun) or run.project_id != project_id:
                        raise ErasureError("Session ownership is ambiguous.")
                    ids.add(run.id)
                    if run.resume_source_run_id:
                        ids.add(run.resume_source_run_id)
                    sessions.update(related)
            if len(ids) == before:
                break
        runs = sorted((item for item in entries if item.id in ids), key=lambda item: item.id)
        if len(runs) != len(ids):
            raise ErasureError("Conversation continuation source could not be verified.")
        actor = project_actor(principal)
        for run in runs:
            if run.status not in TERMINAL_RUN_STATUSES or self.runs.is_executing(run.id):
                raise ErasureError("Finish the selected conversation before deletion.")
            if (
                run.tenant_id != project.tenant_id
                or run.identity_binding != project.identity_binding
            ):
                raise ErasureError("Conversation identity could not be verified.")
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
        native = {
            (run.provider_profile_id or "environment", value)
            for run in runs
            for value in (run.sdk_session_id, run.resume_session_id)
            if value
        }
        for profile, session in sorted(native):
            target = find_native_session(selection, profile, project_id, session)
            if target is not None:
                selection.native_sessions.append(target)
                selection.change(target.transcript)
                child = target.transcript.removesuffix(".jsonl")
                selection.remove_entry(child.rsplit("/", 1)[0], session)
            select_native_side_data(selection, profile, session)
        shared_context(selection, project_id, runs)
        memory_count = self.memory.plan_run_erasure(
            selection,
            runs,
            deleted_by=actor,
            deleted_at=reviewed_at,
        )
        scope = dict(
            kind="conversation",
            project_id=project_id,
            selected_run_id=run_id,
            project_revision=project.revision,
            run_ids=sorted(ids),
            request_id=request_id,
            reviewed_at=reviewed_at.isoformat(),
            deleted_by=actor,
            memory_count=memory_count,
            retained=RETAINED,
        )
        return selection.plan(scope)

    def preview(self, project_id, principal, *, run_id):
        plan, _ = self._select(project_id, run_id, principal, uuid4().hex, utc_now())
        return {
            **plan.scope,
            "fingerprint": plan.fingerprint(),
            "file_count": len(plan.files),
            "source_bytes": sum(item.before_size or 0 for item in plan.files),
            "native_session_count": len(plan.native_sessions),
        }

    def delete(self, project_id, run_id, principal, payload):
        with operation_lease(self.ledger.root):
            completed = self._completed(project_id, run_id, principal, payload)
        if completed is not None:
            return completed
        return self._delete_reviewed(project_id, run_id, principal, payload)

    def _completed(self, project_id, run_id, principal, payload):
        self.authorize(project_id, principal)
        entry = self.ledger.read_run_entry(run_id)
        if isinstance(entry, ErasedRun):
            if (
                entry.project_id != project_id
                or entry.request_id != payload.request_id
                or entry.review_fingerprint != payload.fingerprint
            ):
                raise ErasureError("This conversation was deleted by a different request.")
            inventory = ErasureSelection(
                self.ledger.root, lambda: self.authorize(project_id, principal)
            )
            selected = []
            for name, _ in inventory.children("runs"):
                value, _ = inventory.record(f"runs/{name}/run.json")
                marker = erased_run(value)
                if (
                    marker is not None
                    and marker.project_id == project_id
                    and marker.request_id == payload.request_id
                    and marker.review_fingerprint == payload.fingerprint
                ):
                    selected.append(marker.id)
            return {
                "status": "deleted",
                "project_id": project_id,
                "request_id": payload.request_id,
                "run_ids": sorted(selected),
            }
        return None

    def _delete_reviewed(self, project_id, run_id, principal, payload):
        if not timedelta(seconds=-60) <= utc_now() - payload.reviewed_at <= timedelta(days=1):
            raise ErasureError("Deletion review expired. Review the conversation again.")

        def prepare():
            plan, replacements = self._select(
                project_id, run_id, principal, payload.request_id, payload.reviewed_at
            )
            if plan.fingerprint() != payload.fingerprint:
                raise ErasureError("Conversation data changed. Review the deletion again.")
            for path, raw in list(replacements.items()):
                if path.startswith("runs/") and path.endswith("/run.json"):
                    marker = ErasedRun.model_validate_json(raw)
                    marker.review_fingerprint = payload.fingerprint
                    replacements[path] = encode(marker.model_dump(mode="json"))
                    from .erasure_files import digest

                    for item in plan.files:
                        if item.path == path:
                            item.after_sha256 = digest(replacements[path])
                            item.after_size = len(replacements[path])
            plan.scope["review_fingerprint"] = payload.fingerprint
            return plan, replacements

        receipt = FileErasure(self.ledger.root).commit_prepared(
            prepare,
            authorize=lambda: self.authorize(project_id, principal),
        )
        return {
            "status": "deleted",
            "project_id": project_id,
            "request_id": payload.request_id,
            "run_ids": receipt["scope"]["run_ids"],
        }
