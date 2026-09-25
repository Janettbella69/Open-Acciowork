"""Authorized, reviewed and crash-resumable erasure of one project.

The durable intent fences all cooperating writers before the first deletion.
Only metadata, hashes and provenance survive; native SDK erasure precedes the
application workspace so SDK cwd ownership can still be verified on a retry.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import stat
from collections import defaultdict
from contextlib import ExitStack, contextmanager
from uuid import UUID

from .auth import AuthorizationError, ProjectAccessStore, ProjectPermission
from .event_triggers import ShopifySubscriptionRecord
from .gmail import GmailEnrollment
from .models import PrincipalBinding, PrincipalContext, utc_now
from .outlook_subscriptions import OutlookEnrollment, OutlookNotification
from .project_deletion_capture import (
    ProjectDeletionConflict,
    PurgePlan,
    capture_project_deletion,
)
from .project_deletion_models import (
    DeletionBlocker,
    DeletionCounts,
    DeletionDomain,
    DeletionRetention,
    ProjectDeletionOperation,
    ProjectDeletionPreview,
    ProjectDeletionRequest,
)
from .project_deletion_proofs import proof_hash
from .project_deletion_purge import purge_project_deletion
from .project_deletion_sockets import ShopSocketPlan
from .project_deletion_sockets import erase as erase_shop_sockets
from .project_deletion_sockets import preview as preview_shop_sockets
from .project_lifecycle import ProjectLifecycleBusyError, ProjectUnavailableError
from .project_session_cleanup import (
    ProjectSessionCleanup,
    SessionCleanupConflict,
    SessionCleanupPlan,
)
from .skill_proposal_files import child_directory, directory_fd, read_relative, relative_parts
from .storage import FileLedger, LedgerNotFoundError

logger = logging.getLogger(__name__)

DOMAIN_LABELS = {
    "project": "Project, workspace, uploads and checkpoints",
    "conversations": "Conversations and run records",
    "automation": "Schedules and task experience",
    "memory": "Project memories and history",
    "browser": "Shop browser profiles and sessions",
    "browser-sockets": "Owned shop browser connection endpoints",
    "native-sessions": "Native SDK sessions and file checkpoints",
    "sources": "Source receipts",
    "commercial": "Commercial action records",
    "access": "Project access and collaboration",
    "templates": "Project templates",
    "template-applications": "Template application records",
    "companion": "Companion project access",
    "companion-push": "Companion notification anchors",
    "skill-proposals": "Project Skill proposals",
    "staging": "Abandoned project staging files",
    "intents": "Standing intents",
    "notifications": "Project notifications",
    "grants": "Project permission grants",
    "feedback": "Project feedback",
    "channel-deliveries": "Channel reply obligations",
    "shop-browsers": "Shop browser records",
    "channels/feishu-bindings": "Project channel bindings",
    "event-triggers": "Event trigger signing keys",
}


class ProjectDeletionUncertainError(RuntimeError):
    """A commit was attempted; clients must query the original operation ID."""


def _requester(principal: PrincipalContext | None) -> dict:
    if principal is None:
        return {"identity_binding": "local_only"}
    if utc_now() >= principal.assertion_expires_at:
        raise AuthorizationError("principal assertion has expired")
    return {
        "identity_binding": "authenticated_principal",
        **PrincipalBinding.from_context(principal).model_dump(mode="json"),
    }


class ProjectDeletionService:
    def __init__(self, ledger: FileLedger, access: ProjectAccessStore):
        self.ledger, self.access = ledger, access
        self.lifecycle = ledger.lifecycle
        self.sessions = ProjectSessionCleanup(ledger.root)

    def _authorize(self, project_id: str, principal: PrincipalContext | None):
        project = self.ledger.get_project(project_id)
        if principal is None:
            if project.identity_binding != "local_only":
                raise AuthorizationError("authenticated project requires a principal")
        else:
            if (project.identity_binding != "authenticated_principal"
                    or project.tenant_id != principal.tenant_id):
                raise AuthorizationError("principal is outside the project tenant")
            self.access.authorize(project_id, principal, ProjectPermission.PROJECT_DELETE)
        return project

    @contextmanager
    def _leases(self, project_id, extra=(), *, resume=False):
        with ExitStack() as stack:
            for identifier in sorted({project_id, *extra}):
                stack.enter_context(self.lifecycle.lease(
                    identifier, exclusive=True, allow_deleted=resume and identifier == project_id,
                ))
            yield

    @contextmanager
    def _idle_workers(self, plan: PurgePlan):
        """Check the publication/startup gaps and retain existing lock inodes."""
        with ExitStack() as stack, directory_fd(self.ledger.root) as root:
            paths = [f"run-leases/{run_id}.lock" for run_id in plan.run_ids]
            paths += [f"shop-browsers/{shop_id}/{kind}.lock"
                      for shop_id in plan.shop_ids for kind in ("owner", "launch")]
            for path in paths:
                parts = relative_parts(path)
                try:
                    with child_directory(root, parts[:-1]) as parent:
                        descriptor = os.open(
                            parts[-1], os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent,
                        )
                except FileNotFoundError:
                    continue
                stack.callback(os.close, descriptor)
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ProjectDeletionConflict("Worker lease is not a private regular file")
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise ProjectLifecycleBusyError("Project workers are still active") from exc
            yield

    def _capture(self, project_id: str, principal):
        project = self._authorize(project_id, principal)
        application = capture_project_deletion(self.ledger.root, project_id)
        runs = self.ledger._iter_runs()
        native = self.sessions.preview(
            project_id, [run for run in runs if run.project_id == project_id], runs,
        )
        sockets = preview_shop_sockets(self.ledger.root, application.shop_ids)
        grouped = defaultdict(lambda: [0, 0])
        for entry in application.entries:
            grouped[entry.domain][0] += 1
            grouped[entry.domain][1] += entry.size_bytes
        if native.relative_files:
            grouped["native-sessions"] = [
                len(native.relative_files), sum(item.size_bytes for item in native.relative_files),
            ]
        socket_count = sum(item.kind == "socket" for item in sockets.entries)
        if socket_count:
            grouped["browser-sockets"] = [socket_count, 0]
        domains = [DeletionDomain(id=key, label=DOMAIN_LABELS.get(key, key),
                                  files=value[0], bytes=value[1])
                   for key, value in sorted(grouped.items())]
        retained_messages = [*application.retained, *native.retained]
        subscription_prefix = f"projects/{project_id}/event-triggers/subscriptions/"
        for entry in application.entries:
            if not entry.path.startswith(subscription_prefix):
                continue
            try:
                if entry.kind != "file":
                    raise ValueError("invalid subscription record")
                record = ShopifySubscriptionRecord.model_validate_json(
                    read_relative(self.ledger.root, entry.path, 16 * 1024),
                )
                if (
                    record.project_id != project_id
                    or entry.path != f"{subscription_prefix}{record.trigger_id}.json"
                ):
                    raise ValueError("subscription record binding changed")
            except (OSError, ValueError):
                record = None
            if record is None or record.status != "rejected":
                retained_messages.append(
                    "A Shopify order webhook may remain active after project deletion. "
                    "Remove it from Shopify before deleting this project."
                )
                break
        gmail_prefix = f"projects/{project_id}/event-triggers/gmail-enrollments/"
        for entry in application.entries:
            if not entry.path.startswith(gmail_prefix):
                continue
            try:
                if entry.kind != "file":
                    raise ValueError("invalid Gmail enrollment record")
                record = GmailEnrollment.model_validate_json(
                    read_relative(self.ledger.root, entry.path, 16 * 1024),
                )
                if (
                    record.project_id != project_id
                    or entry.path != f"{gmail_prefix}{record.trigger_id}.json"
                ):
                    raise ValueError("Gmail enrollment binding changed")
            except (OSError, ValueError):
                pass  # An uncertain enrollment still requires remote review.
            retained_messages.append(
                "A Gmail mailbox watch may remain active or be shared after project deletion. "
                "Stop the last watch or review the other project triggers first."
            )
            break
        outlook_prefix = f"projects/{project_id}/event-triggers/outlook-enrollments/"
        for entry in application.entries:
            if not entry.path.startswith(outlook_prefix):
                continue
            try:
                if entry.kind != "file":
                    raise ValueError("invalid Outlook enrollment record")
                record = OutlookEnrollment.model_validate_json(
                    read_relative(self.ledger.root, entry.path, 16 * 1024),
                )
                if (
                    record.project_id != project_id
                    or entry.path != f"{outlook_prefix}{record.trigger_id}.json"
                ):
                    raise ValueError("Outlook enrollment binding changed")
            except (OSError, ValueError):
                pass  # An uncertain enrollment still requires remote review.
            retained_messages.append(
                "A Microsoft Graph Inbox subscription may remain active or be shared after "
                "project deletion. Stop this trigger's subscription before deleting the project."
            )
            break
        retained = [DeletionRetention(code=f"retained_{index}", message=message)
                    for index, message in enumerate(dict.fromkeys(retained_messages))]
        blockers = [DeletionBlocker(code="inventory_requires_attention", message=message)
                    for message in application.blockers]
        for path in (self.ledger.root / "outlook-subscriptions").glob("*/*.json"):
            try:
                relative = path.relative_to(self.ledger.root).as_posix()
                notice = OutlookNotification.model_validate_json(
                    read_relative(self.ledger.root, relative, 256 * 1024),
                )
            except (OSError, ValueError):
                blockers.append(DeletionBlocker(
                    code="outlook_queue_uncertain",
                    message="An Outlook notification queue record needs review before deletion.",
                ))
                break
            if notice.finished_at is None and any(
                target.startswith(project_id + ":") for target in notice.targets
            ):
                blockers.append(DeletionBlocker(
                    code="outlook_queue_pending",
                    message="Finish this project's queued Outlook notifications before deletion.",
                ))
                break
        blockers += [DeletionBlocker(code="native_session_scope_unproven", message=(
            "Native session ownership could not be fully verified. "
            "Resolve the stored session boundary before deleting this project."
        ))] if native.blockers else []
        memories = 0
        try:
            raw = read_relative(self.ledger.root, f"memory/project/{project_id}/items.json",
                                16 * 1024 * 1024)
            items = json.loads(raw)
            if not isinstance(items, list):
                raise ValueError("invalid memory inventory")
            memories = len(items)
        except FileNotFoundError:
            pass
        preview = ProjectDeletionPreview(
            project_id=project_id, project_name=project.name,
            fingerprint=proof_hash({
                "application": application.model_dump(mode="json"),
                "sessions": native.model_dump(mode="json"),
                "sockets": sockets.model_dump(mode="json"),
                "requester": _requester(principal),
            }),
            domains=domains,
            counts=DeletionCounts(
                files=sum(item.files for item in domains),
                bytes=sum(item.bytes for item in domains),
                runs=len(application.run_ids), sessions=len(native.session_groups),
                memories=memories,
                shops=len(application.shop_ids),
            ),
            retained=retained, blockers=blockers, can_delete=not blockers,
        )
        return preview, application, native, sockets

    @contextmanager
    def _review(self, project_id, principal):
        extra = set()
        # Template provenance can add another project lock. Release and acquire
        # the entire sorted set, then recapture under that final set of leases.
        for _ in range(3):
            with self._leases(project_id, extra):
                captured = self._capture(project_id, principal)
                discovered = set(captured[1].extra_project_ids) - {project_id}
                if discovered.issubset(extra):
                    with self._idle_workers(captured[1]):
                        pass
                    yield captured
                    return
                extra |= discovered
        raise ProjectDeletionConflict("Project dependencies changed; review again")

    def preview(self, project_id: str, principal: PrincipalContext | None):
        project = self._authorize(project_id, principal)
        try:
            with self._review(project_id, principal) as (preview, _application, _native, _sockets):
                return preview
        except ProjectLifecycleBusyError:
            return ProjectDeletionPreview(
                project_id=project_id, project_name=project.name, fingerprint="0" * 64,
                domains=[], counts=DeletionCounts(files=0, bytes=0, runs=0, sessions=0,
                                                  memories=0, shops=0),
                retained=[], blockers=[DeletionBlocker(
                    code="project_active",
                    message="Project work is still active. Stop runs and close shop sessions, "
                            "then refresh this review.",
                )], can_delete=False,
            )

    def _state_for(self, project_id, operation_id, principal):
        if str(UUID(operation_id)) != operation_id:
            raise LedgerNotFoundError(operation_id)
        state = self.lifecycle.read_state(project_id)
        if state is None or state.get("operation_id") != operation_id:
            raise LedgerNotFoundError(operation_id)
        if state.get("requester") != _requester(principal):
            raise AuthorizationError("deletion operation belongs to another principal session")
        return state

    @staticmethod
    def _operation(state):
        return ProjectDeletionOperation.model_validate({
            name: state[name] for name in ProjectDeletionOperation.model_fields
        })

    def get(self, project_id, operation_id, principal):
        return self._operation(self._state_for(project_id, operation_id, principal))

    def start(self, project_id, request: ProjectDeletionRequest, principal):
        request_hash = proof_hash(request)
        existing = self.lifecycle.read_state(project_id)
        if existing is not None:
            state = self._state_for(project_id, request.operation_id, principal)
            if state.get("request_hash") != request_hash:
                raise ProjectDeletionConflict("Deletion operation request has changed")
            return self._operation(state)
        with self._review(project_id, principal) as (preview, application, native, sockets):
            if not preview.can_delete:
                raise ProjectDeletionConflict("Deletion inventory requires attention")
            if (request.fingerprint != preview.fingerprint
                    or request.confirmation_name != preview.project_name):
                raise ProjectDeletionConflict("Project changed or its name was not confirmed")
            now = utc_now().isoformat()
            state = {
                "schema_version": 1, "project_id": project_id,
                "operation_id": request.operation_id, "status": "deleting",
                "created_at": now, "updated_at": now,
                "requester": _requester(principal), "request_hash": request_hash,
                "fingerprint": preview.fingerprint,
                "reason_sha256": hashlib.sha256(request.reason.encode()).hexdigest(),
                "domains": [item.model_dump(mode="json") for item in preview.domains],
                "retained": [item.model_dump(mode="json") for item in preview.retained],
                "error_code": None, "error_message": None, "can_retry": True,
                "plan": {"application": application.model_dump(mode="json"),
                         "sessions": native.model_dump(mode="json"),
                         "sockets": sockets.model_dump(mode="json")},
            }
            try:
                self.lifecycle.write_state(project_id, state)
                return self._erase(state)
            except Exception as exc:
                # rename can succeed before fsync fails. Never claim this was
                # a rejected review or allow the caller to assume no deletion.
                raise ProjectDeletionUncertainError(
                    "Check the original deletion operation",
                ) from exc

    def _erase(self, state):
        project_id = state["project_id"]
        try:
            application = PurgePlan.model_validate_json(json.dumps(state["plan"]["application"]))
            native = SessionCleanupPlan.model_validate_json(json.dumps(state["plan"]["sessions"]))
            sockets = ShopSocketPlan.model_validate_json(json.dumps(state["plan"]["sockets"]))
            if (application.project_id != project_id or native.project_id != project_id
                    or sockets.data_root != str(self.ledger.root.resolve())
                    or {item.shop_id for item in sockets.entries} != set(application.shop_ids)):
                raise ProjectDeletionConflict("Deletion plan scope does not match its intent")
            reviewed = proof_hash({**state["plan"], "requester": state["requester"]})
            if reviewed != state["fingerprint"]:
                raise ProjectDeletionConflict("Deletion plan changed after confirmation")
            with self._idle_workers(application):
                erase_shop_sockets(sockets)
                result = self.sessions.erase(native)
                if not result.complete:
                    raise SessionCleanupConflict("native_erasure_incomplete")
                purge_project_deletion(self.ledger.root, application)
            state.update(status="deleted", error_code=None, error_message=None, can_retry=False)
        except (OSError, ValueError, KeyError, ProjectLifecycleBusyError):
            logger.warning("Project deletion needs attention: %s", project_id)
            state.update(
                status="blocked", error_code="deletion_requires_attention",
                error_message="Deletion is incomplete. The project remains unavailable. "
                              "Resolve active work or changed files, then retry this operation.",
                can_retry=True,
            )
        state["updated_at"] = utc_now().isoformat()
        self.lifecycle.write_state(project_id, state)
        return self._operation(state)

    def retry(self, project_id, operation_id, principal):
        state = self._state_for(project_id, operation_id, principal)
        if state["status"] == "deleted":
            return self._operation(state)
        return self._resume(state)

    def _resume(self, state):
        project_id = state["project_id"]
        application = PurgePlan.model_validate_json(json.dumps(state["plan"]["application"]))
        with self._leases(project_id, application.extra_project_ids, resume=True):
            current = self.lifecycle.read_state(project_id)
            if current["status"] == "deleted":
                return self._operation(current)
            try:
                current.update(status="deleting", updated_at=utc_now().isoformat())
                self.lifecycle.write_state(project_id, current)
                return self._erase(current)
            except Exception as exc:
                raise ProjectDeletionUncertainError(
                    "Check the original deletion operation",
                ) from exc

    def recover(self):
        """Run before constructing any content-recovering stores or workers."""
        for project_id in self.lifecycle.pending_ids():
            try:
                self._resume(self.lifecycle.read_state(project_id))
            except (OSError, ValueError, KeyError, ProjectLifecycleBusyError,
                    ProjectUnavailableError, ProjectDeletionUncertainError):
                logger.warning("Project deletion recovery deferred: %s", project_id)
