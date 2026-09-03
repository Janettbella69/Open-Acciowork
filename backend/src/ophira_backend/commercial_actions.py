"""Server-enforced stage -> human review -> execution-request workflow.

No external commerce adapter is installed today.  Consequently ``apply`` stops at a
durable, approved execution request and records ``not_executed``.  A future adapter must
re-check every listed precondition immediately before it commits an external mutation.
"""

from __future__ import annotations

import copy
import fcntl
import json
import os
import re
import secrets
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final

from .constants import (
    CommercialActionStatus,
    CommercialReviewDecision,
    ExternalExecutionStatus,
)
from .models import (
    CommercialActionApplyRequest,
    CommercialActionAuditEntry,
    CommercialActionRecord,
    CommercialActionReview,
    CommercialActionReviewRequest,
    CommercialActionStage,
    CommercialExecutionRequest,
    OpportunityProject,
    OrgPolicy,
    RunRecord,
    json_values_equal,
    utc_now,
)
from .policy import OrgPolicyStore
from .storage import FileLedger, LedgerNotFoundError

MAX_STAGED_ACTION_BYTES: Final = 64 * 1024
MAX_STORE_ID_CHARS: Final = 200
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")
_HUMAN_ACTOR_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+\-]{0,159}$")
_RESERVED_ACTORS = {"agent", "assistant", "claude", "main-agent", "system"}
_MISSING = object()
_PROCESS_LOCKS: dict[tuple[str, str], threading.RLock] = {}
_PROCESS_LOCKS_GUARD = threading.Lock()


class CommercialActionError(RuntimeError):
    """Base error for fail-closed commercial action handling."""


class CommercialActionNotFoundError(CommercialActionError):
    pass


class CommercialActionScopeError(CommercialActionError):
    pass


class CommercialActionConflictError(CommercialActionError):
    pass


class CommercialActionPolicyError(CommercialActionError):
    pass


class CommercialActionIntegrityError(CommercialActionError):
    pass


def new_opaque_id(prefix: str) -> str:
    """Return a server-issued identifier with 256 bits of cryptographic entropy."""

    return f"{prefix}_{secrets.token_urlsafe(32)}"


def validate_human_actor(actor_id: str) -> str:
    normalized = actor_id.strip()
    folded = normalized.casefold()
    if not _HUMAN_ACTOR_ID.fullmatch(normalized):
        raise CommercialActionScopeError("a valid authenticated user actor is required")
    if folded in _RESERVED_ACTORS or folded.startswith(("agent_", "run_", "sdk_")):
        raise CommercialActionScopeError("Agent and system identities cannot review or apply")
    return normalized


class CommercialActionStore:
    """Atomic JSON snapshots serialized by project across threads and processes."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _safe_id(value: str) -> str:
        if (
            not value
            or len(value) > MAX_STORE_ID_CHARS
            or value in {".", ".."}
            or not _SAFE_ID.fullmatch(value)
        ):
            raise CommercialActionNotFoundError("commercial action not found")
        return value

    def _process_lock_for(self, project_id: str) -> threading.RLock:
        safe_project = self._safe_id(project_id)
        key = (str(self.root), safe_project)
        with _PROCESS_LOCKS_GUARD:
            return _PROCESS_LOCKS.setdefault(key, threading.RLock())

    @contextmanager
    def transaction(self, project_id: str) -> Iterator[None]:
        """Hold one project transaction across the complete read-modify-write cycle.

        ``flock`` is the cross-process authority. The process-wide ``RLock`` also
        serializes independent service/store instances in one interpreter, where
        platform flock semantics can otherwise differ.
        """

        safe_project = self._safe_id(project_id)
        project_directory = self.root / safe_project
        process_lock = self._process_lock_for(safe_project)
        with process_lock:
            try:
                project_directory.mkdir(parents=True, exist_ok=True)
                valid_directory = (
                    not project_directory.is_symlink()
                    and project_directory.is_dir()
                )
            except OSError as exc:
                raise CommercialActionIntegrityError(
                    "commercial action project directory is unavailable"
                ) from exc
            if not valid_directory:
                raise CommercialActionIntegrityError(
                    "commercial action project directory failed integrity validation"
                )
            # Persist the project directory entry itself, not only later files
            # below it. Repeating this fsync for existing projects is cheap and
            # also closes the uncertainty window after a prior fsync failure.
            self._fsync_directory(self.root)
            lock_path = project_directory / ".transaction.lock"
            flags = os.O_CREAT | os.O_RDWR
            flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(lock_path, flags, 0o600)
            except OSError as exc:
                raise CommercialActionIntegrityError(
                    "commercial action transaction lock is unavailable"
                ) from exc
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
            except OSError as exc:
                self._close_descriptor(descriptor)
                raise CommercialActionIntegrityError(
                    "commercial action transaction lock could not be acquired"
                ) from exc
            try:
                yield
            except BaseException:
                # Cleanup failures must not hide the operation's real error.
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                except OSError:
                    pass
                self._close_descriptor(descriptor)
                raise

            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            except OSError:
                # Closing the descriptor below is also an authoritative flock
                # release. Do not turn an already committed operation into an
                # ambiguous 500 when that close succeeds.
                pass
            try:
                os.close(descriptor)
            except OSError as exc:
                raise CommercialActionIntegrityError(
                    "commercial action transaction lock could not be released"
                ) from exc

    @staticmethod
    def _cleanup_temporary(path: Path) -> None:
        """Best-effort cleanup that never obscures commit/error semantics."""

        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    @staticmethod
    def _close_descriptor(descriptor: int) -> None:
        if descriptor < 0:
            return
        try:
            os.close(descriptor)
        except OSError:
            pass

    def _path(self, project_id: str, action_id: str) -> Path:
        return (
            self.root
            / self._safe_id(project_id)
            / "actions"
            / f"{self._safe_id(action_id)}.json"
        )

    def get_unlocked(self, project_id: str, action_id: str) -> CommercialActionRecord:
        path = self._path(project_id, action_id)
        try:
            valid_file = path.is_file() and not path.is_symlink()
        except OSError as exc:
            raise CommercialActionIntegrityError(
                "commercial action path could not be inspected"
            ) from exc
        if not valid_file:
            raise CommercialActionNotFoundError("commercial action not found")
        try:
            record = CommercialActionRecord.model_validate_json(path.read_text("utf-8"))
        except (OSError, ValueError) as exc:
            raise CommercialActionIntegrityError(
                "stored commercial action failed integrity validation"
            ) from exc
        if record.project_id != project_id or record.id != action_id:
            raise CommercialActionIntegrityError(
                "stored commercial action does not match its ledger scope"
            )
        return record

    def get(self, project_id: str, action_id: str) -> CommercialActionRecord:
        with self.transaction(project_id):
            return self.get_unlocked(project_id, action_id)

    def list(self, project_id: str) -> list[CommercialActionRecord]:
        with self.transaction(project_id):
            directory = self.root / self._safe_id(project_id) / "actions"
            records: list[CommercialActionRecord] = []
            try:
                if not directory.exists():
                    return records
                if directory.is_symlink() or not directory.is_dir():
                    raise CommercialActionIntegrityError(
                        "commercial action store contains an invalid directory"
                    )
                paths = list(directory.glob("*.json"))
            except CommercialActionIntegrityError:
                raise
            except OSError as exc:
                raise CommercialActionIntegrityError(
                    "commercial action directory could not be inspected"
                ) from exc
            for path in paths:
                try:
                    if path.is_symlink() or not path.is_file():
                        raise CommercialActionIntegrityError(
                            "commercial action store contains an invalid entry"
                        )
                    record = CommercialActionRecord.model_validate_json(path.read_text("utf-8"))
                except CommercialActionIntegrityError:
                    raise
                except (OSError, ValueError) as exc:
                    raise CommercialActionIntegrityError(
                        "stored commercial action failed integrity validation"
                    ) from exc
                if record.project_id != project_id or path.stem != record.id:
                    raise CommercialActionIntegrityError(
                        "stored commercial action does not match its ledger scope"
                    )
                records.append(record)
            return sorted(records, key=lambda record: record.created_at, reverse=True)

    @staticmethod
    def _write_unique_temporary(
        path: Path,
        record: CommercialActionRecord,
    ) -> Path:
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(16)}.tmp")
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(temporary, flags, 0o600)
        except OSError as exc:
            raise CommercialActionIntegrityError(
                "commercial action temporary file is unavailable"
            ) from exc
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                descriptor = -1
                handle.write(record.model_dump_json(indent=2) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            CommercialActionStore._cleanup_temporary(temporary)
            raise CommercialActionIntegrityError(
                "commercial action temporary file could not be committed"
            ) from exc
        except BaseException:
            CommercialActionStore._cleanup_temporary(temporary)
            raise
        finally:
            CommercialActionStore._close_descriptor(descriptor)
        return temporary

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_DIRECTORY", 0)
        try:
            descriptor = os.open(path, flags)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError as exc:
            raise CommercialActionIntegrityError(
                "commercial action directory could not be committed"
            ) from exc

    @staticmethod
    def _prepare_action_directory(path: Path) -> None:
        try:
            existed = path.parent.exists()
            path.parent.mkdir(parents=True, exist_ok=True)
            valid_directory = not path.parent.is_symlink() and path.parent.is_dir()
        except OSError as exc:
            raise CommercialActionIntegrityError(
                "commercial action directory is unavailable"
            ) from exc
        if not valid_directory:
            raise CommercialActionIntegrityError(
                "commercial action store contains an invalid directory"
            )
        if not existed:
            CommercialActionStore._fsync_directory(path.parent.parent)

    def create_unlocked(self, record: CommercialActionRecord) -> None:
        path = self._path(record.project_id, record.id)
        self._prepare_action_directory(path)
        temporary = self._write_unique_temporary(path, record)
        try:
            try:
                os.link(temporary, path)
            except FileExistsError as exc:
                raise CommercialActionConflictError(
                    "server-issued commercial action ID collision"
                ) from exc
            except OSError as exc:
                raise CommercialActionIntegrityError(
                    "commercial action snapshot could not be published"
                ) from exc
            self._fsync_directory(path.parent)
        finally:
            self._cleanup_temporary(temporary)

    def write_unlocked(self, record: CommercialActionRecord) -> None:
        path = self._path(record.project_id, record.id)
        self._prepare_action_directory(path)
        temporary = self._write_unique_temporary(path, record)
        try:
            try:
                os.replace(temporary, path)
            except OSError as exc:
                raise CommercialActionIntegrityError(
                    "commercial action snapshot could not be replaced"
                ) from exc
            self._fsync_directory(path.parent)
        finally:
            self._cleanup_temporary(temporary)


class CommercialActionService:
    def __init__(
        self,
        root: Path,
        *,
        ledger: FileLedger,
        policy: OrgPolicyStore,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.store = CommercialActionStore(root)
        self.ledger = ledger
        self.policy = policy
        self._clock = clock

    def list(self, project_id: str) -> list[CommercialActionRecord]:
        self._get_project(project_id)
        return self.store.list(project_id)

    def get(self, project_id: str, action_id: str) -> CommercialActionRecord:
        self._get_project(project_id)
        return self.store.get(project_id, action_id)

    def stage(
        self,
        project_id: str,
        payload: CommercialActionStage,
    ) -> CommercialActionRecord:
        self._validate_payload_size(payload)
        with self.store.transaction(project_id):
            project = self._get_project(project_id)
            run = self._validate_source(
                project_id,
                payload.source_run_id,
                payload.source_session_id,
            )
            self._project_state(payload)
            now = self._clock()
            record = CommercialActionRecord(
                id=new_opaque_id("action"),
                project_id=project_id,
                source_run_id=run.id,
                source_session_id=payload.source_session_id,
                staged_by=run.agent_id or "main-agent",
                operation=payload.operation,
                target=payload.target,
                observed_state=copy.deepcopy(payload.observed_state),
                changes=copy.deepcopy(payload.changes),
                reason=payload.reason,
                project_revision_at_stage=project.revision,
                created_at=now,
                expires_at=now + timedelta(seconds=payload.expires_in_seconds),
                updated_at=now,
                history=[
                    CommercialActionAuditEntry(
                        event="staged",
                        actor_id=run.agent_id or "main-agent",
                        reason=payload.reason,
                        recorded_at=now,
                    )
                ],
            )
            self.store.create_unlocked(record)
            return record

    def review(
        self,
        project_id: str,
        action_id: str,
        payload: CommercialActionReviewRequest,
        *,
        actor_id: str,
    ) -> CommercialActionRecord:
        actor = validate_human_actor(actor_id)
        with self.store.transaction(project_id):
            record = self.store.get_unlocked(project_id, action_id)
            if actor == record.staged_by:
                raise CommercialActionScopeError("the staging identity cannot review its action")
            if record.status != CommercialActionStatus.STAGED:
                raise CommercialActionConflictError(
                    f"commercial action is already {record.status.value}"
                )
            self._validate_source(
                record.project_id,
                record.source_run_id,
                record.source_session_id,
            )
            now = self._clock()
            if now >= record.expires_at:
                self._expire_unlocked(record, now)
                raise CommercialActionConflictError("commercial action has expired")

            valid_until: datetime | None = None
            if payload.decision == CommercialReviewDecision.APPROVE:
                assert payload.valid_for_seconds is not None
                valid_until = min(
                    record.expires_at,
                    now + timedelta(seconds=payload.valid_for_seconds),
                )
                status = CommercialActionStatus.APPROVED
                event = "approved"
            else:
                status = CommercialActionStatus.REJECTED
                event = "rejected"

            review = CommercialActionReview(
                decision=payload.decision,
                actor_id=actor,
                reason=payload.reason,
                decided_at=now,
                valid_until=valid_until,
            )
            updated = record.model_copy(
                update={
                    "status": status,
                    "review": review,
                    "updated_at": now,
                    "history": [
                        *record.history,
                        CommercialActionAuditEntry(
                            event=event,
                            actor_id=actor,
                            reason=payload.reason,
                            recorded_at=now,
                        ),
                    ],
                }
            )
            self.store.write_unlocked(updated)
            return updated

    def apply(
        self,
        project_id: str,
        action_id: str,
        payload: CommercialActionApplyRequest,
        *,
        actor_id: str,
    ) -> CommercialActionRecord:
        actor = validate_human_actor(actor_id)
        with self.store.transaction(project_id):
            record = self.store.get_unlocked(project_id, action_id)
            self._validate_action_scope(record, project_id, payload)

            # Returning the existing request is idempotent: no second request, audit event,
            # or external side effect is produced.
            if (
                record.status == CommercialActionStatus.EXECUTION_REQUESTED
                and record.execution_request is not None
            ):
                return record

            if record.status != CommercialActionStatus.APPROVED or record.review is None:
                raise CommercialActionConflictError(
                    f"commercial action is {record.status.value}, not approved"
                )
            now = self._clock()
            approval_deadline = record.review.valid_until
            if now >= record.expires_at or approval_deadline is None or now >= approval_deadline:
                self._expire_unlocked(record, now)
                raise CommercialActionConflictError("commercial action approval has expired")

            self._validate_source(
                project_id,
                payload.source_run_id,
                payload.source_session_id,
            )
            project = self._get_project(project_id)
            if project.revision != record.project_revision_at_stage:
                raise CommercialActionConflictError(
                    "project state changed after staging; create and approve a new action"
                )

            current_policy = self.policy.get()
            projected_state = self._project_state(record)
            self._enforce_current_policy(record, projected_state, current_policy)

            execution = CommercialExecutionRequest(
                id=new_opaque_id("execution"),
                action_id=record.id,
                project_id=record.project_id,
                source_run_id=record.source_run_id,
                source_session_id=record.source_session_id,
                requested_by=actor,
                request_reason=payload.reason,
                requested_at=now,
                projected_state=projected_state,
                expected_target_version=record.target.expected_version,
                project_revision_checked=project.revision,
                project_constraints_checked=list(project.constraints),
                org_policy_updated_at=current_policy.updated_at,
                external_status=ExternalExecutionStatus.NOT_EXECUTED,
                adapter_required=True,
                adapter_preconditions=[
                    "Reload the target from the external system.",
                    "Match the current external version to expected_target_version.",
                    "Re-evaluate the current organization policy and numeric limits.",
                    "Verify the projected resulting state before committing atomically.",
                    "Persist the external receipt before reporting execution success.",
                ],
            )
            updated = record.model_copy(
                update={
                    "status": CommercialActionStatus.EXECUTION_REQUESTED,
                    "execution_request": execution,
                    "updated_at": now,
                    "history": [
                        *record.history,
                        CommercialActionAuditEntry(
                            event="execution_requested",
                            actor_id=actor,
                            reason=payload.reason,
                            recorded_at=now,
                        ),
                    ],
                }
            )
            self.store.write_unlocked(updated)
            return updated

    @staticmethod
    def _validate_payload_size(payload: CommercialActionStage) -> None:
        try:
            encoded = json.dumps(
                payload.model_dump(mode="json"),
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise CommercialActionConflictError("commercial action must be valid JSON") from exc
        if len(encoded) > MAX_STAGED_ACTION_BYTES:
            raise CommercialActionConflictError("commercial action exceeds the 64 KB limit")

    def _get_project(self, project_id: str) -> OpportunityProject:
        try:
            return self.ledger.get_project(project_id)
        except (LedgerNotFoundError, ValueError) as exc:
            raise CommercialActionNotFoundError("project not found") from exc

    def _validate_source(
        self,
        project_id: str,
        run_id: str,
        session_id: str,
    ) -> RunRecord:
        try:
            run = self.ledger.get_run(run_id)
        except (LedgerNotFoundError, ValueError) as exc:
            raise CommercialActionScopeError("source run/session is not valid") from exc
        if (
            run.project_id != project_id
            or not run.sdk_session_id
            or run.sdk_session_id != session_id
        ):
            raise CommercialActionScopeError("source run/session is not valid for this project")
        return run

    @staticmethod
    def _validate_action_scope(
        record: CommercialActionRecord,
        project_id: str,
        payload: CommercialActionApplyRequest,
    ) -> None:
        if (
            record.project_id != project_id
            or record.source_run_id != payload.source_run_id
            or record.source_session_id != payload.source_session_id
        ):
            raise CommercialActionScopeError("action source scope does not match")

    @staticmethod
    def _get_path(state: dict, field: str) -> object:
        value: object = state
        for segment in field.split("."):
            if not isinstance(value, dict) or segment not in value:
                return _MISSING
            value = value[segment]
        return value

    @staticmethod
    def _set_path(state: dict, field: str, value: object) -> None:
        cursor = state
        segments = field.split(".")
        for segment in segments[:-1]:
            nested = cursor.get(segment)
            if nested is None:
                nested = {}
                cursor[segment] = nested
            if not isinstance(nested, dict):
                raise CommercialActionConflictError(
                    f"field path {field!r} crosses a non-object value"
                )
            cursor = nested
        cursor[segments[-1]] = copy.deepcopy(value)

    @classmethod
    def _project_state(cls, action: CommercialActionStage | CommercialActionRecord) -> dict:
        projected = copy.deepcopy(action.observed_state)
        for change in action.changes:
            current = cls._get_path(projected, change.field)
            if current is _MISSING:
                if change.before is not None:
                    raise CommercialActionConflictError(
                        f"observed state is missing field {change.field!r}"
                    )
            elif not json_values_equal(current, change.before):
                raise CommercialActionConflictError(
                    f"observed value for {change.field!r} does not match before"
                )
            cls._set_path(projected, change.field, change.after)
        return projected

    @staticmethod
    def _enforce_current_policy(
        action: CommercialActionRecord,
        projected_state: dict,
        policy: OrgPolicy,
    ) -> None:
        if not policy.commercial_actions_enabled:
            raise CommercialActionPolicyError("commercial actions are disabled by policy")
        if action.operation not in policy.allowed_commercial_operations:
            raise CommercialActionPolicyError(
                "commercial operation is not in the current policy allowlist"
            )
        if action.target.connector_id not in policy.allowed_commercial_connector_ids:
            raise CommercialActionPolicyError(
                "commercial connector is not in the current policy allowlist"
            )
        if len(action.changes) > policy.commercial_max_changes_per_action:
            raise CommercialActionPolicyError(
                "commercial action exceeds the current change-count limit"
            )

        forbidden = policy.commercial_forbidden_fields
        for change in action.changes:
            if any(
                change.field == blocked
                or change.field.startswith(f"{blocked}.")
                or blocked.startswith(f"{change.field}.")
                for blocked in forbidden
            ):
                raise CommercialActionPolicyError(
                    f"field {change.field!r} is forbidden by current policy"
                )

        for limit in policy.commercial_numeric_limits:
            value = CommercialActionService._get_path(projected_state, limit.field)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise CommercialActionPolicyError(
                    f"resulting field {limit.field!r} must be present and numeric "
                    "under current policy"
                )
            if limit.minimum is not None and value < limit.minimum:
                raise CommercialActionPolicyError(
                    f"field {limit.field!r} is below the current minimum"
                )
            if limit.maximum is not None and value > limit.maximum:
                raise CommercialActionPolicyError(
                    f"field {limit.field!r} exceeds the current maximum"
                )

    def _expire_unlocked(self, record: CommercialActionRecord, now: datetime) -> None:
        expired = record.model_copy(
            update={
                "status": CommercialActionStatus.EXPIRED,
                "updated_at": now,
                "history": [
                    *record.history,
                    CommercialActionAuditEntry(
                        event="expired",
                        actor_id="system",
                        reason="staging or approval validity window elapsed",
                        recorded_at=now,
                    ),
                ],
            }
        )
        self.store.write_unlocked(expired)
