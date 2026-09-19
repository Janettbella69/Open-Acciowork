"""Human-reviewed erasure of a whole durable resume chain."""

import hashlib
import json
import logging
from collections import defaultdict
from uuid import UUID

from .auth import AuthorizationError
from .conversation_deletion_files import (
    RETAINED,
    ConversationPurgePlan,
    capture_conversation,
    purge_conversation,
)
from .conversation_deletion_state import ConversationDeletionStates, DeletedRunOrigin
from .conversations import conversation_runs
from .models import RunRecord, utc_now
from .project_deletion import (
    DOMAIN_LABELS,
    ProjectDeletionService,
    ProjectDeletionUncertainError,
    _requester,
)
from .project_deletion_capture import DeletionCapture, ProjectDeletionConflict
from .project_deletion_models import (
    DeletionBlocker,
    DeletionCounts,
    DeletionDomain,
    DeletionRetention,
    ProjectDeletionOperation,
    ProjectDeletionPreview,
)
from .project_deletion_proofs import proof_hash
from .project_lifecycle import ProjectLifecycleBusyError, ProjectUnavailableError
from .project_session_cleanup import ProjectSessionCleanup, SessionCleanupPlan
from .storage import LedgerNotFoundError

logger = logging.getLogger(__name__)


class ConversationDeletionPreview(ProjectDeletionPreview):
    run_id: str
    run_ids: list[str]


class ConversationDeletionOperation(ProjectDeletionOperation):
    run_id: str
    run_ids: list[str]


class ConversationDeletionService:
    def __init__(self, ledger, access):
        self.ledger = ledger
        self.lifecycle = ledger.lifecycle
        self.states = ConversationDeletionStates(self.lifecycle)
        self.projects = ProjectDeletionService(ledger, access)
        self.sessions = ProjectSessionCleanup(ledger.root)

    def _capture(self, project_id, run_id, principal):
        project = self.projects._authorize(project_id, principal)
        cap = DeletionCapture(self.ledger.root)
        all_runs = []
        for identifier in cap.names("runs"):
            run = RunRecord.model_validate(cap.json(f"runs/{identifier}/run.json"))
            if run.id != identifier:
                raise ProjectDeletionConflict("Conversation Run identity is invalid")
            all_runs.append(run)
        runs = conversation_runs(project_id, run_id, all_runs)
        application = capture_conversation(self.ledger.root, project, runs)
        native = self.sessions.preview_conversation(project_id, runs, all_runs)
        origins = [DeletedRunOrigin.from_run(run).model_dump(mode="json") for run in runs]
        plan = {
            "application": application.model_dump(mode="json"),
            "sessions": native.model_dump(mode="json"),
            "origins": origins,
        }
        grouped = defaultdict(lambda: [0, 0])
        for item in application.entries:
            grouped[item.domain][0] += 1
            grouped[item.domain][1] += item.size_bytes
        if native.relative_files:
            grouped["native-sessions"] = [
                len(native.relative_files),
                sum(item.size_bytes for item in native.relative_files),
            ]
        domains = [
            DeletionDomain(
                id=key, label=DOMAIN_LABELS.get(key, key), files=values[0], bytes=values[1]
            )
            for key, values in sorted(grouped.items())
        ]
        blockers = [
            DeletionBlocker(code="conversation_requires_attention", message=message)
            for message in application.blockers
        ]
        if native.blockers:
            blockers.append(
                DeletionBlocker(
                    code="native_session_scope_unproven",
                    message=(
                        "Native conversation ownership could not be fully verified. "
                        "Shared checkpoints, session indexes or unowned native copies "
                        "must be resolved before deletion."
                    ),
                )
            )
        preview = ConversationDeletionPreview(
            project_id=project_id,
            project_name=project.name,
            run_id=run_id,
            run_ids=application.run_ids,
            fingerprint=proof_hash({**plan, "requester": _requester(principal)}),
            domains=domains,
            counts=DeletionCounts(
                files=sum(item.files for item in domains),
                bytes=sum(item.bytes for item in domains),
                runs=len(runs),
                sessions=len(native.session_groups),
                memories=0,
                shops=0,
            ),
            retained=[
                DeletionRetention(code=f"retained_{i}", message=message)
                for i, message in enumerate([*RETAINED, *native.retained])
            ],
            blockers=blockers,
            can_delete=not blockers,
        )
        return preview, plan, application

    def preview(self, project_id, run_id, principal):
        project = self.projects._authorize(project_id, principal)
        source = self.ledger.get_run(run_id)
        if source.project_id != project_id:
            raise LedgerNotFoundError(run_id)
        try:
            with self.lifecycle.lease(project_id, exclusive=True):
                preview, _plan, application = self._capture(project_id, run_id, principal)
                with self.projects._idle_workers(application):
                    return preview
        except ProjectLifecycleBusyError:
            return ConversationDeletionPreview(
                project_id=project_id,
                project_name=project.name,
                run_id=run_id,
                run_ids=[],
                fingerprint="0" * 64,
                domains=[],
                retained=[],
                counts=DeletionCounts(files=0, bytes=0, runs=0, sessions=0, memories=0, shops=0),
                blockers=[
                    DeletionBlocker(
                        code="project_active",
                        message=(
                            "Project work is still active. Stop runs and close shop sessions, "
                            "then refresh this review."
                        ),
                    )
                ],
                can_delete=False,
            )

    def _state_for(self, project_id, operation_id, principal):
        if str(UUID(operation_id)) != operation_id:
            raise LedgerNotFoundError(operation_id)
        state = self.states.read(project_id, operation_id)
        if state is None:
            raise LedgerNotFoundError(operation_id)
        if state.get("requester") != _requester(principal):
            raise AuthorizationError("conversation deletion belongs to another principal session")
        return state

    @staticmethod
    def _operation(state):
        return ConversationDeletionOperation.model_validate(
            {name: state[name] for name in ConversationDeletionOperation.model_fields}
        )

    def get(self, project_id, operation_id, principal):
        return self._operation(self._state_for(project_id, operation_id, principal))

    def start(self, project_id, run_id, request, principal):
        existing = self.states.read(project_id, request.operation_id)
        if existing is not None:
            state = self._state_for(project_id, request.operation_id, principal)
            if state["run_id"] != run_id or state["request_hash"] != proof_hash(request):
                raise ProjectDeletionConflict("Conversation deletion request changed")
            return self._operation(state)
        with self.lifecycle.lease(project_id, exclusive=True):
            preview, plan, application = self._capture(project_id, run_id, principal)
            if (
                not preview.can_delete
                or request.fingerprint != preview.fingerprint
                or request.confirmation_name != preview.project_name
            ):
                raise ProjectDeletionConflict("Conversation deletion review changed or is blocked")
            with self.projects._idle_workers(application):
                pass
            now = utc_now().isoformat()
            state = {
                "schema_version": 1,
                "project_id": project_id,
                "run_id": run_id,
                "run_ids": application.run_ids,
                "operation_id": request.operation_id,
                "status": "deleting",
                "created_at": now,
                "updated_at": now,
                "requester": _requester(principal),
                "request_hash": proof_hash(request),
                "fingerprint": preview.fingerprint,
                "reason_sha256": hashlib.sha256(request.reason.encode()).hexdigest(),
                "domains": [item.model_dump(mode="json") for item in preview.domains],
                "retained": [item.model_dump(mode="json") for item in preview.retained],
                "error_code": None,
                "error_message": None,
                "can_retry": True,
                "plan": plan,
            }
            try:
                self.states.write(state)
                return self._erase(state)
            except Exception as exc:
                raise ProjectDeletionUncertainError(
                    "Check the original conversation deletion operation"
                ) from exc

    def _erase(self, state):
        try:
            application = ConversationPurgePlan.model_validate(state["plan"]["application"])
            native = SessionCleanupPlan.model_validate_json(json.dumps(state["plan"]["sessions"]))
            if (
                proof_hash({**state["plan"], "requester": state["requester"]})
                != state["fingerprint"]
                or application.project_id != state["project_id"]
                or application.run_ids != state["run_ids"]
                or state["run_id"] not in state["run_ids"]
                or native.project_id != state["project_id"]
                or native.scope != "conversation"
            ):
                raise ProjectDeletionConflict("Conversation deletion intent changed")
            with self.projects._idle_workers(application):
                self.sessions.erase(native)
                purge_conversation(self.ledger.root, application)
            state.update(status="deleted", can_retry=False, error_code=None, error_message=None)
        except (OSError, ValueError, KeyError, ProjectLifecycleBusyError):
            logger.warning("Conversation deletion requires attention: %s", state["operation_id"])
            state.update(
                status="blocked",
                can_retry=True,
                error_code="deletion_requires_attention",
                error_message="Conversation deletion is incomplete. The project is temporarily "
                "unavailable until this operation finishes. Resolve changed files and retry.",
            )
        state["updated_at"] = utc_now().isoformat()
        self.states.write(state)
        return self._operation(state)

    def retry(self, project_id, operation_id, principal):
        return self._resume(self._state_for(project_id, operation_id, principal))

    def _resume(self, state):
        if state["status"] == "deleted":
            return self._operation(state)
        with self.lifecycle.lease(state["project_id"], exclusive=True, allow_deleted=True):
            if self.lifecycle.has_deletion(state["project_id"]):
                raise ProjectDeletionConflict("Project deletion takes precedence")
            current = self.states.read(state["project_id"], state["operation_id"])
            if current["status"] == "deleted":
                return self._operation(current)
            try:
                return self._erase(current)
            except Exception as exc:
                raise ProjectDeletionUncertainError(
                    "Check the original conversation deletion operation"
                ) from exc

    def recover(self):
        for state in self.states.pending():
            try:
                self._resume(state)
            except (
                OSError,
                ValueError,
                KeyError,
                ProjectLifecycleBusyError,
                ProjectUnavailableError,
                ProjectDeletionUncertainError,
            ):
                logger.warning("Conversation deletion recovery deferred: %s", state["operation_id"])
