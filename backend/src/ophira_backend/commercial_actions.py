"""Server-enforced stage -> human review -> idempotent external execution workflow.

Without an injected execution port, ``apply`` remains truthful and stops at a durable
``not_executed`` request.  With a port, the service persists a stable idempotency key,
rechecks local and remote preconditions, and records ``applied`` only after the external
system confirms success and the server-issued receipt is durable.
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

from .auth import (
    AuthenticationError,
    AuthorizationError,
    ProjectAccessStore,
    ProjectPermission,
)
from .commercial_adapters import (
    CommercialAdapterError,
    CommercialExecutionPort,
    CommercialTargetObservation,
    CommercialTargetReadPort,
    ExternalCommitResult,
    ExternalTargetSnapshot,
)
from .constants import (
    CommercialActionOperation,
    CommercialActionStatus,
    CommercialReviewDecision,
    ExternalExecutionStatus,
    PrincipalRole,
)
from .models import (
    CommercialActionApplyRequest,
    CommercialActionAuditEntry,
    CommercialActionCancelRequest,
    CommercialActionReceiptStage,
    CommercialActionRecord,
    CommercialActionReview,
    CommercialActionReviewRequest,
    CommercialActionStage,
    CommercialExecutionReceipt,
    CommercialExecutionRequest,
    OpportunityProject,
    OrgPolicy,
    PrincipalContext,
    RunRecord,
    json_values_equal,
    utc_now,
)
from .policy import OrgPolicyStore
from .source_receipts import (
    VerifiedCommercialTargetReceipt,
    issue_commercial_target_receipt,
    verify_commercial_target_receipt,
)
from .storage import FileLedger, LedgerNotFoundError, SourceReceiptError
from .untrusted import UntrustedSource, sanitize_untrusted_payload

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

    def find_by_source_receipt_unlocked(
        self,
        project_id: str,
        receipt_id: str,
    ) -> CommercialActionRecord | None:
        """Find a consumed target receipt while the project transaction is held."""

        directory = self.root / self._safe_id(project_id) / "actions"
        try:
            if not directory.exists():
                return None
            if directory.is_symlink() or not directory.is_dir():
                raise CommercialActionIntegrityError(
                    "commercial action store contains an invalid directory"
                )
            paths = list(directory.glob("action_*.json"))
        except CommercialActionIntegrityError:
            raise
        except OSError as exc:
            raise CommercialActionIntegrityError(
                "commercial action directory could not be inspected"
            ) from exc
        for path in paths:
            if path.is_symlink() or not path.is_file():
                raise CommercialActionIntegrityError(
                    "commercial action store contains an invalid entry"
                )
            record = self.get_unlocked(project_id, path.stem)
            if record.source_receipt_id == receipt_id:
                return record
        return None

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
        executor: CommercialExecutionPort | None = None,
        target_reader: CommercialTargetReadPort | None = None,
        execution_lease_seconds: int = 120,
        access: ProjectAccessStore | None = None,
    ) -> None:
        if not 10 <= execution_lease_seconds <= 3_600:
            raise ValueError("commercial execution lease must be between 10 and 3600 seconds")
        self.store = CommercialActionStore(root)
        self.ledger = ledger
        self.policy = policy
        self._clock = clock
        self.executor = executor
        self.target_reader = target_reader
        self.execution_lease_seconds = execution_lease_seconds
        self.access = access

    def list(self, project_id: str) -> list[CommercialActionRecord]:
        self._get_project(project_id)
        return self.store.list(project_id)

    def get(self, project_id: str, action_id: str) -> CommercialActionRecord:
        self._get_project(project_id)
        return self.store.get(project_id, action_id)

    def observe_target(
        self,
        project_id: str,
        *,
        source_run_id: str,
        source_session_id: str,
        connector_id: str,
        resource_type: str,
        resource_key: str,
    ) -> VerifiedCommercialTargetReceipt:
        """Read through the host adapter and durably bind what the Agent may stage."""

        run = self._validate_source(project_id, source_run_id, source_session_id)
        self._authorize_staging_run(run)
        if self.target_reader is None:
            raise CommercialActionConflictError(
                "no trusted commercial target reader is configured"
            )
        observation = self.target_reader.read_target(
            connector_id,
            resource_type,
            resource_key,
        )
        if not isinstance(observation, CommercialTargetObservation) or (
            observation.connector_id != connector_id
            or observation.resource_type != resource_type
            or observation.resource_key != resource_key
        ):
            raise CommercialActionIntegrityError(
                "commercial target reader returned a mismatched observation"
            )
        current_config_hash = self.target_reader.current_connector_config_hash(
            connector_id
        )
        if current_config_hash != observation.connector_config_hash:
            raise CommercialActionConflictError(
                "commercial connector configuration changed during target read"
            )
        return issue_commercial_target_receipt(
            self.ledger,
            run.id,
            observation,
            now=self._clock(),
        )

    def stage(
        self,
        project_id: str,
        payload: CommercialActionStage,
    ) -> CommercialActionRecord:
        """Legacy local-only staging path.

        Production MCP staging uses :meth:`stage_from_receipt`; authenticated
        runs cannot downgrade to caller-supplied target/version/state values.
        """

        self._validate_payload_size(payload)
        with self.store.transaction(project_id):
            project = self._get_project(project_id)
            run = self._validate_source(
                project_id,
                payload.source_run_id,
                payload.source_session_id,
            )
            self._authorize_staging_run(run)
            if run.identity_binding == "authenticated_principal":
                raise CommercialActionScopeError(
                    "authenticated commercial staging requires a server-issued "
                    "connector target receipt"
                )
            self._project_state(payload)
            return self._stage_unlocked(project, run, payload)

    def stage_from_receipt(
        self,
        project_id: str,
        payload: CommercialActionReceiptStage,
    ) -> CommercialActionRecord:
        """Hydrate all external authority fields from one scoped host receipt."""

        with self.store.transaction(project_id):
            project = self._get_project(project_id)
            run = self._validate_source(
                project_id,
                payload.source_run_id,
                payload.source_session_id,
            )
            self._authorize_staging_run(run)
            try:
                verified = verify_commercial_target_receipt(
                    self.ledger,
                    project_id,
                    run.id,
                    payload.source_session_id,
                    payload.receipt_id,
                    now=self._clock(),
                )
            except SourceReceiptError as exc:
                raise CommercialActionScopeError(str(exc)) from exc
            if self.store.find_by_source_receipt_unlocked(
                project_id,
                verified.receipt.id,
            ) is not None:
                raise CommercialActionConflictError(
                    "commercial target receipt has already been consumed"
                )
            if self.target_reader is None:
                raise CommercialActionConflictError(
                    "no trusted commercial target reader is configured"
                )
            try:
                current_config_hash = (
                    self.target_reader.current_connector_config_hash(
                        verified.payload.target.connector_id
                    )
                )
            except CommercialAdapterError as exc:
                raise CommercialActionConflictError(
                    f"commercial target connector cannot be revalidated: {exc}"
                ) from exc
            if current_config_hash != verified.payload.connector_config_hash:
                raise CommercialActionConflictError(
                    "commercial connector configuration changed after target read"
                )
            hydrated = CommercialActionStage(
                source_run_id=run.id,
                source_session_id=payload.source_session_id,
                operation=verified.payload.operation,
                target=verified.payload.target,
                observed_state=copy.deepcopy(verified.payload.observed_state),
                changes=copy.deepcopy(payload.changes),
                reason=payload.reason,
                expires_in_seconds=payload.expires_in_seconds,
            )
            self._validate_payload_size(hydrated)
            self._project_state(hydrated)
            return self._stage_unlocked(
                project,
                run,
                hydrated,
                source_receipt_id=verified.receipt.id,
                source_connector_config_hash=(
                    verified.payload.connector_config_hash
                ),
            )

    def _stage_unlocked(
        self,
        project: OpportunityProject,
        run: RunRecord,
        payload: CommercialActionStage,
        *,
        source_receipt_id: str | None = None,
        source_connector_config_hash: str | None = None,
    ) -> CommercialActionRecord:
        now = self._clock()
        staged_by = run.principal_id or run.agent_id or "main-agent"
        record = CommercialActionRecord(
            id=new_opaque_id("action"),
            project_id=project.id,
            source_run_id=run.id,
            source_session_id=payload.source_session_id,
            staged_by=staged_by,
            tenant_id=run.tenant_id,
            staged_role=run.principal_role,
            staged_principal_session_id=run.principal_session_id,
            identity_binding=run.identity_binding,
            idempotency_key=new_opaque_id("idempotency"),
            source_receipt_id=source_receipt_id,
            source_connector_config_hash=source_connector_config_hash,
            operation=payload.operation,
            target=payload.target,
            observed_state=copy.deepcopy(payload.observed_state),
            changes=copy.deepcopy(payload.changes),
            reason=payload.reason,
            project_revision_at_stage=project.revision,
            staged_policy_updated_at=self.policy.get().updated_at,
            created_at=now,
            expires_at=now + timedelta(seconds=payload.expires_in_seconds),
            updated_at=now,
            history=[
                CommercialActionAuditEntry(
                    event="staged",
                    actor_id=staged_by,
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
        principal: PrincipalContext | None = None,
        actor_id: str | None = None,
    ) -> CommercialActionRecord:
        with self.store.transaction(project_id):
            record = self.store.get_unlocked(project_id, action_id)
            actor, trusted_principal = self._resolve_actor(
                record,
                principal=principal,
                actor_id=actor_id,
                permission=ProjectPermission.COMMERCIAL_REVIEW,
            )
            if actor == record.staged_by:
                raise CommercialActionScopeError("the staging identity cannot review its action")
            if record.status != CommercialActionStatus.STAGED:
                raise CommercialActionConflictError(
                    f"commercial action is already {record.status.value}"
                )
            source_run = self._validate_source(
                record.project_id,
                record.source_run_id,
                record.source_session_id,
            )
            self._authorize_staging_run(source_run)
            now = self._clock()
            if now >= record.expires_at:
                self._expire_unlocked(record, now)
                raise CommercialActionConflictError("commercial action has expired")

            valid_until: datetime | None = None
            review_policy_updated_at: datetime | None = None
            if payload.decision == CommercialReviewDecision.APPROVE:
                assert payload.valid_for_seconds is not None
                current_policy = self.policy.get()
                self._enforce_current_policy(
                    record,
                    self._project_state(record),
                    current_policy,
                )
                review_policy_updated_at = current_policy.updated_at
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
                tenant_id=(
                    trusted_principal.tenant_id if trusted_principal is not None else None
                ),
                actor_role=(
                    trusted_principal.role if trusted_principal is not None else None
                ),
                actor_session_id=(
                    trusted_principal.session_id if trusted_principal is not None else None
                ),
                reason=payload.reason,
                decided_at=now,
                valid_until=valid_until,
                policy_updated_at=review_policy_updated_at,
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

    def cancel(
        self,
        project_id: str,
        action_id: str,
        payload: CommercialActionCancelRequest,
        *,
        principal: PrincipalContext | None = None,
        actor_id: str | None = None,
    ) -> CommercialActionRecord:
        """Cancel a proposal only while no external result can be ambiguous."""

        with self.store.transaction(project_id):
            record = self.store.get_unlocked(project_id, action_id)
            stager_withdrawal = bool(
                principal is not None
                and principal.principal_id == record.staged_by
                and record.review is None
                and record.status
                in {
                    CommercialActionStatus.STAGED,
                    CommercialActionStatus.CANCELLED,
                }
            )
            actor, _trusted_principal = self._resolve_actor(
                record,
                principal=principal,
                actor_id=actor_id,
                permission=(
                    ProjectPermission.COMMERCIAL_STAGE
                    if stager_withdrawal
                    else ProjectPermission.COMMERCIAL_APPLY
                ),
            )
            if record.status == CommercialActionStatus.CANCELLED:
                return record

            execution = record.execution_request
            cancellable = record.status in {
                CommercialActionStatus.STAGED,
                CommercialActionStatus.APPROVED,
            }
            if record.status == CommercialActionStatus.EXECUTION_REQUESTED:
                if execution is None:
                    raise CommercialActionIntegrityError(
                        "execution-requested action has no durable request"
                    )
                if execution.external_status in {
                    ExternalExecutionStatus.ATTEMPTING,
                    ExternalExecutionStatus.RETRYABLE_FAILED,
                    ExternalExecutionStatus.SUCCEEDED,
                }:
                    raise CommercialActionConflictError(
                        "commercial action cannot be cancelled after external execution starts"
                    )
                cancellable = (
                    execution.external_status == ExternalExecutionStatus.NOT_EXECUTED
                )
            if not cancellable:
                raise CommercialActionConflictError(
                    f"commercial action is {record.status.value} and cannot be cancelled"
                )

            now = self._clock()
            cancelled = record.model_copy(
                update={
                    "status": CommercialActionStatus.CANCELLED,
                    "updated_at": now,
                    "history": [
                        *record.history,
                        CommercialActionAuditEntry(
                            event="cancelled",
                            actor_id=actor,
                            reason=payload.reason,
                            recorded_at=now,
                        ),
                    ],
                }
            )
            self.store.write_unlocked(cancelled)
            return cancelled

    def apply(
        self,
        project_id: str,
        action_id: str,
        payload: CommercialActionApplyRequest,
        *,
        principal: PrincipalContext | None = None,
        actor_id: str | None = None,
    ) -> CommercialActionRecord:
        with self.store.transaction(project_id):
            record = self.store.get_unlocked(project_id, action_id)
            actor, trusted_principal = self._resolve_actor(
                record,
                principal=principal,
                actor_id=actor_id,
                permission=ProjectPermission.COMMERCIAL_APPLY,
            )
            self._validate_action_scope(record, project_id, payload)

            # Final states and adapter-less execution requests are immutable and
            # idempotent. An installed executor may recover a retryable or leased
            # request using the already durable remote idempotency key.
            if record.status in {
                CommercialActionStatus.APPLIED,
                CommercialActionStatus.EXECUTION_FAILED,
            }:
                return record
            if record.status == CommercialActionStatus.EXECUTION_REQUESTED:
                if record.execution_request is None:
                    raise CommercialActionIntegrityError(
                        "execution-requested action has no durable request"
                    )
                if self.executor is None or not self.executor.supports(record):
                    return record
            elif record.status != CommercialActionStatus.APPROVED or record.review is None:
                raise CommercialActionConflictError(
                    f"commercial action is {record.status.value}, not approved"
                )

            if record.status == CommercialActionStatus.APPROVED:
                now = self._clock()
                approval_deadline = record.review.valid_until
                if (
                    now >= record.expires_at
                    or approval_deadline is None
                    or now >= approval_deadline
                ):
                    self._expire_unlocked(record, now)
                    raise CommercialActionConflictError("commercial action approval has expired")

                self._validate_source(
                    project_id,
                    payload.source_run_id,
                    payload.source_session_id,
                )
                self._revalidate_authenticated_participants(record)
                project = self._get_project(project_id)
                if project.revision != record.project_revision_at_stage:
                    raise CommercialActionConflictError(
                        "project state changed after staging; create and approve a new action"
                    )

                current_policy = self.policy.get()
                projected_state = self._project_state(record)
                self._enforce_current_policy(record, projected_state, current_policy)
                if (
                    record.review.policy_updated_at is not None
                    and current_policy.updated_at
                    != record.review.policy_updated_at
                ):
                    raise CommercialActionConflictError(
                        "organization policy changed after approval; "
                        "create and approve a new action"
                    )

                execution_id = new_opaque_id("execution")
                execution = CommercialExecutionRequest(
                    id=execution_id,
                    action_id=record.id,
                    project_id=record.project_id,
                    source_run_id=record.source_run_id,
                    source_session_id=record.source_session_id,
                    requested_by=actor,
                    requested_tenant_id=(
                        trusted_principal.tenant_id
                        if trusted_principal is not None
                        else None
                    ),
                    requested_role=(
                        trusted_principal.role if trusted_principal is not None else None
                    ),
                    requested_session_id=(
                        trusted_principal.session_id
                        if trusted_principal is not None
                        else None
                    ),
                    request_reason=payload.reason,
                    requested_at=now,
                    projected_state=projected_state,
                    expected_target_version=record.target.expected_version,
                    project_revision_checked=project.revision,
                    project_constraints_checked=list(project.constraints),
                    org_policy_updated_at=current_policy.updated_at,
                    external_status=ExternalExecutionStatus.NOT_EXECUTED,
                    adapter_required=True,
                    adapter_type=(
                        self.executor.adapter_type
                        if self.executor and self.executor.supports(record)
                        else None
                    ),
                    idempotency_key=record.idempotency_key or execution_id,
                    adapter_preconditions=[
                        "Reload the target from the external system.",
                        "Match the current external version to expected_target_version.",
                        "Re-evaluate scope, before-state, recipient/content, and policy.",
                        "Use the durable execution ID as the remote idempotency key.",
                        "Persist a server-issued external receipt before reporting success.",
                    ],
                )
                record = record.model_copy(
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
                # The remote idempotency key is durable before any adapter can
                # inspect or mutate the external target.
                self.store.write_unlocked(record)

        if self.executor is None or not self.executor.supports(record):
            return record
        return self._execute_requested(project_id, action_id)

    def _execute_requested(
        self,
        project_id: str,
        action_id: str,
    ) -> CommercialActionRecord:
        assert self.executor is not None
        claimed, should_execute = self._claim_execution(project_id, action_id)
        if not should_execute:
            return claimed
        execution = claimed.execution_request
        assert execution is not None

        try:
            snapshot = self.executor.inspect_target(claimed, execution)
        except CommercialAdapterError as exc:
            return self._record_adapter_failure(project_id, action_id, execution, exc)

        # Keep the project/action lock through the final precondition check and
        # commit. A second process can only observe the durable claim or final
        # result, while the remote API independently deduplicates the stable key.
        with self.store.transaction(project_id):
            current = self.store.get_unlocked(project_id, action_id)
            if not self._owns_attempt(current, execution):
                return current
            try:
                self._validate_execution_preconditions(current, self._clock())
                self._validate_external_snapshot(current, snapshot)
                result = self.executor.commit(current, execution, snapshot)
            except CommercialAdapterError as exc:
                return self._record_adapter_failure_unlocked(current, exc)
            except CommercialActionError as exc:
                return self._record_terminal_failure_unlocked(
                    current,
                    code="LOCAL_PRECONDITION_CHANGED",
                    message=str(exc),
                )
            return self._record_success_unlocked(current, result)

    def _claim_execution(
        self,
        project_id: str,
        action_id: str,
    ) -> tuple[CommercialActionRecord, bool]:
        assert self.executor is not None
        with self.store.transaction(project_id):
            record = self.store.get_unlocked(project_id, action_id)
            if record.status in {
                CommercialActionStatus.APPLIED,
                CommercialActionStatus.EXECUTION_FAILED,
                CommercialActionStatus.CANCELLED,
            }:
                return record, False
            execution = record.execution_request
            if (
                record.status != CommercialActionStatus.EXECUTION_REQUESTED
                or execution is None
            ):
                raise CommercialActionIntegrityError(
                    "commercial action is missing its durable execution request"
                )
            now = self._clock()
            if (
                execution.external_status == ExternalExecutionStatus.ATTEMPTING
                and execution.attempt_lease_until is not None
                and now < execution.attempt_lease_until
            ):
                return record, False
            if (
                execution.external_status == ExternalExecutionStatus.RETRYABLE_FAILED
                and execution.retry_not_before is not None
                and now < execution.retry_not_before
            ):
                return record, False

            self._validate_execution_preconditions(record, now)
            attempt_count = execution.attempt_count + 1
            claimed_execution = execution.model_copy(
                update={
                    "adapter_type": self.executor.adapter_type,
                    "idempotency_key": record.idempotency_key or execution.id,
                    "external_status": ExternalExecutionStatus.ATTEMPTING,
                    "attempt_count": attempt_count,
                    "attempt_started_at": now,
                    "attempt_lease_until": now
                    + timedelta(seconds=self.execution_lease_seconds),
                    "retry_not_before": None,
                    "last_error_code": None,
                    "last_error_message": None,
                    "receipt": None,
                }
            )
            claimed = record.model_copy(
                update={
                    "execution_request": claimed_execution,
                    "updated_at": now,
                    "history": [
                        *record.history,
                        CommercialActionAuditEntry(
                            event="execution_started",
                            actor_id=execution.requested_by,
                            reason=f"External execution attempt {attempt_count} claimed.",
                            recorded_at=now,
                        ),
                    ],
                }
            )
            self.store.write_unlocked(claimed)
            return claimed, True

    def _validate_execution_preconditions(
        self,
        record: CommercialActionRecord,
        now: datetime,
    ) -> None:
        execution = record.execution_request
        review = record.review
        if execution is None or review is None:
            raise CommercialActionIntegrityError("commercial action execution scope is incomplete")
        self._revalidate_authenticated_participants(record)
        approval_deadline = review.valid_until
        if now >= record.expires_at or approval_deadline is None or now >= approval_deadline:
            raise CommercialActionConflictError("commercial action approval has expired")
        self._validate_source(
            record.project_id,
            record.source_run_id,
            record.source_session_id,
        )
        project = self._get_project(record.project_id)
        if project.revision != record.project_revision_at_stage:
            raise CommercialActionConflictError(
                "project state changed after staging; create and approve a new action"
            )
        projected_state = self._project_state(record)
        if not json_values_equal(projected_state, execution.projected_state):
            raise CommercialActionIntegrityError(
                "commercial execution projected state no longer matches the staged action"
            )
        if execution.expected_target_version != record.target.expected_version:
            raise CommercialActionIntegrityError(
                "commercial execution target version no longer matches the staged action"
            )
        expected_idempotency_key = record.idempotency_key or execution.id
        if (execution.idempotency_key or execution.id) != expected_idempotency_key:
            raise CommercialActionIntegrityError("commercial idempotency key failed validation")
        current_policy = self.policy.get()
        self._enforce_current_policy(record, projected_state, current_policy)
        if current_policy.updated_at != execution.org_policy_updated_at:
            raise CommercialActionConflictError(
                "organization policy changed before external commit; "
                "create and approve a new action"
            )

    @classmethod
    def _validate_external_snapshot(
        cls,
        record: CommercialActionRecord,
        snapshot: ExternalTargetSnapshot,
    ) -> None:
        if (
            record.source_connector_config_hash is not None
            and snapshot.connector_config_hash
            != record.source_connector_config_hash
        ):
            raise CommercialActionConflictError(
                "commercial connector configuration changed after staging"
            )
        if snapshot.version != record.target.expected_version:
            raise CommercialActionConflictError(
                "external target version changed after staging; create and approve a new action"
            )
        state = snapshot.state
        if record.source_receipt_id is not None:
            sanitized = sanitize_untrusted_payload(
                state,
                UntrustedSource.BUSINESS_RECORD,
            )
            if (
                sanitized.truncated
                or sanitized.removed_boundaries
                or not isinstance(sanitized.value, dict)
            ):
                raise CommercialActionConflictError(
                    "current external target cannot be represented exactly for review"
                )
            state = sanitized.value
        for change in record.changes:
            current = cls._get_path(state, change.field)
            if current is _MISSING or not json_values_equal(current, change.before):
                raise CommercialActionConflictError(
                    f"external before value for {change.field!r} changed after staging"
                )
        if record.operation == CommercialActionOperation.SEND_MESSAGE:
            for field in ("recipient", "content"):
                observed = cls._get_path(record.observed_state, field)
                current = cls._get_path(state, field)
                if observed is _MISSING or current is _MISSING:
                    raise CommercialActionConflictError(
                        f"message {field} is missing from the reviewed external state"
                    )
                if not json_values_equal(observed, current):
                    raise CommercialActionConflictError(
                        f"message {field} changed after staging"
                    )

    @staticmethod
    def _owns_attempt(
        record: CommercialActionRecord,
        execution: CommercialExecutionRequest,
    ) -> bool:
        current = record.execution_request
        return bool(
            record.status == CommercialActionStatus.EXECUTION_REQUESTED
            and current is not None
            and current.id == execution.id
            and current.attempt_count == execution.attempt_count
            and current.external_status == ExternalExecutionStatus.ATTEMPTING
        )

    def _record_adapter_failure(
        self,
        project_id: str,
        action_id: str,
        execution: CommercialExecutionRequest,
        error: CommercialAdapterError,
    ) -> CommercialActionRecord:
        with self.store.transaction(project_id):
            record = self.store.get_unlocked(project_id, action_id)
            if not self._owns_attempt(record, execution):
                return record
            return self._record_adapter_failure_unlocked(record, error)

    def _record_adapter_failure_unlocked(
        self,
        record: CommercialActionRecord,
        error: CommercialAdapterError,
    ) -> CommercialActionRecord:
        if error.retryable:
            delay = max(error.retry_after_seconds or 1, 0.001)
            return self._record_failure_unlocked(
                record,
                external_status=ExternalExecutionStatus.RETRYABLE_FAILED,
                action_status=CommercialActionStatus.EXECUTION_REQUESTED,
                event="execution_retryable_failed",
                code=error.code,
                message=str(error),
                retry_not_before=self._clock() + timedelta(seconds=delay),
            )
        return self._record_terminal_failure_unlocked(
            record,
            code=error.code,
            message=str(error),
        )

    def _record_terminal_failure_unlocked(
        self,
        record: CommercialActionRecord,
        *,
        code: str,
        message: str,
    ) -> CommercialActionRecord:
        return self._record_failure_unlocked(
            record,
            external_status=ExternalExecutionStatus.TERMINAL_FAILED,
            action_status=CommercialActionStatus.EXECUTION_FAILED,
            event="execution_failed",
            code=code,
            message=message,
            retry_not_before=None,
        )

    def _record_failure_unlocked(
        self,
        record: CommercialActionRecord,
        *,
        external_status: ExternalExecutionStatus,
        action_status: CommercialActionStatus,
        event: str,
        code: str,
        message: str,
        retry_not_before: datetime | None,
    ) -> CommercialActionRecord:
        execution = record.execution_request
        if execution is None:
            raise CommercialActionIntegrityError("commercial execution request is missing")
        now = self._clock()
        failed_execution = execution.model_copy(
            update={
                "external_status": external_status,
                "attempt_lease_until": None,
                "retry_not_before": retry_not_before,
                "last_error_code": code[:120],
                "last_error_message": message[:1_000],
                "receipt": None,
            }
        )
        failed = record.model_copy(
            update={
                "status": action_status,
                "execution_request": failed_execution,
                "updated_at": now,
                "history": [
                    *record.history,
                    CommercialActionAuditEntry(
                        event=event,  # type: ignore[arg-type]
                        actor_id="system",
                        reason=message[:4_000],
                        recorded_at=now,
                    ),
                ],
            }
        )
        self.store.write_unlocked(failed)
        return failed

    def _record_success_unlocked(
        self,
        record: CommercialActionRecord,
        result: ExternalCommitResult,
    ) -> CommercialActionRecord:
        execution = record.execution_request
        if execution is None:
            raise CommercialActionIntegrityError("commercial execution request is missing")
        now = self._clock()
        receipt = CommercialExecutionReceipt(
            id=new_opaque_id("external_receipt"),
            action_id=record.id,
            execution_id=execution.id,
            connector_id=record.target.connector_id,
            operation=record.operation,
            idempotency_key=execution.idempotency_key or execution.id,
            remote_action_id=result.remote_action_id,
            remote_version=result.remote_version,
            http_status=result.http_status,
            response_hash=result.response_hash,
            adapter_attempts=result.adapter_attempts,
            issued_at=now,
        )
        succeeded_execution = execution.model_copy(
            update={
                "external_status": ExternalExecutionStatus.SUCCEEDED,
                "attempt_lease_until": None,
                "retry_not_before": None,
                "last_error_code": None,
                "last_error_message": None,
                "receipt": receipt,
            }
        )
        applied = record.model_copy(
            update={
                "status": CommercialActionStatus.APPLIED,
                "execution_request": succeeded_execution,
                "updated_at": now,
                "history": [
                    *record.history,
                    CommercialActionAuditEntry(
                        event="applied",
                        actor_id=execution.requested_by,
                        reason=(
                            "External connector confirmed the idempotent action; "
                            f"receipt {receipt.id}."
                        ),
                        recorded_at=now,
                    ),
                ],
            }
        )
        # If this durable write fails after the remote commit, the prior
        # ATTEMPTING claim remains. Recovery repeats the POST with the exact same
        # key after lease expiry and can safely reconstruct a server receipt.
        self.store.write_unlocked(applied)
        return applied

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
        project = self._get_project(project_id)
        if project.identity_binding != run.identity_binding:
            raise CommercialActionScopeError("source run identity is not valid for this project")
        if run.identity_binding == "authenticated_principal" and (
            project.tenant_id != run.tenant_id
            or run.principal_id is None
            or run.principal_role is None
            or run.principal_session_id is None
        ):
            raise CommercialActionScopeError("source run principal scope is incomplete")
        return run

    def _authorize_staging_run(self, run: RunRecord) -> None:
        if run.identity_binding != "authenticated_principal":
            return
        if (
            self.access is None
            or run.tenant_id is None
            or run.principal_id is None
            or run.principal_role is None
        ):
            raise CommercialActionScopeError(
                "authenticated commercial staging requires project authorization"
            )
        try:
            self.access.authorize_bound(
                run.project_id,
                tenant_id=run.tenant_id,
                principal_id=run.principal_id,
                role=run.principal_role,
                permission=ProjectPermission.COMMERCIAL_STAGE,
            )
        except AuthorizationError as exc:
            raise CommercialActionScopeError(str(exc)) from exc

    def _resolve_actor(
        self,
        record: CommercialActionRecord,
        *,
        principal: PrincipalContext | None,
        actor_id: str | None,
        permission: ProjectPermission,
    ) -> tuple[str, PrincipalContext | None]:
        if record.identity_binding == "authenticated_principal":
            if principal is None or actor_id is not None or self.access is None:
                raise CommercialActionScopeError(
                    "authenticated commercial actions require PrincipalContext"
                )
            if principal.tenant_id != record.tenant_id:
                raise CommercialActionScopeError("principal is outside the action tenant")
            try:
                self.access.authorize(record.project_id, principal, permission)
            except (AuthenticationError, AuthorizationError) as exc:
                raise CommercialActionScopeError(str(exc)) from exc
            return validate_human_actor(principal.principal_id), principal

        if principal is not None:
            raise CommercialActionScopeError(
                "legacy local-only actions must be restaged after project migration"
            )
        if actor_id is None:
            raise CommercialActionScopeError("a local authenticated actor is required")
        return validate_human_actor(actor_id), None

    def _revalidate_authenticated_participants(
        self,
        record: CommercialActionRecord,
    ) -> None:
        if record.identity_binding != "authenticated_principal":
            return
        review = record.review
        if (
            self.access is None
            or record.tenant_id is None
            or record.staged_role is None
            or review is None
            or review.tenant_id is None
            or review.actor_role is None
        ):
            raise CommercialActionScopeError(
                "authenticated commercial participant scope is incomplete"
            )
        checks: list[tuple[str, str, PrincipalRole, ProjectPermission]] = [
            (
                record.tenant_id,
                record.staged_by,
                record.staged_role,
                ProjectPermission.COMMERCIAL_STAGE,
            ),
            (
                review.tenant_id,
                review.actor_id,
                review.actor_role,
                ProjectPermission.COMMERCIAL_REVIEW,
            ),
        ]
        execution = record.execution_request
        if execution is not None:
            if (
                execution.requested_tenant_id is None
                or execution.requested_role is None
            ):
                raise CommercialActionScopeError(
                    "authenticated commercial requester scope is incomplete"
                )
            checks.append(
                (
                    execution.requested_tenant_id,
                    execution.requested_by,
                    execution.requested_role,
                    ProjectPermission.COMMERCIAL_APPLY,
                )
            )
        try:
            for tenant_id, principal_id, role, permission in checks:
                self.access.authorize_bound(
                    record.project_id,
                    tenant_id=tenant_id,
                    principal_id=principal_id,
                    role=role,
                    permission=permission,
                )
        except AuthorizationError as exc:
            raise CommercialActionScopeError(str(exc)) from exc

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
        action: CommercialActionStage | CommercialActionRecord,
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
