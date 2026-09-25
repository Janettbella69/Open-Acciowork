"""Host-owned workspace recovery, including writes made by native Bash.

Only the workspace tree is restored. Project truth, sessions and commercial
receipts remain append-only history. No model call or tool replay is involved.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator

from .artifact_files import (
    MAX_ARTIFACT_BYTES,
    MAX_ARTIFACT_HISTORY_FILES,
    SNAPSHOT_DIRECTORY,
    freeze_artifact,
    read_snapshot,
)
from .backup_files import (
    BackupLimits,
    publish_exclusive,
    remove_staging,
    sync_directories,
    tree_inventory,
)
from .models import APIModel, new_id, utc_now
from .skill_proposal_files import directory_fd, read_relative, relative_parts

LIMITS = BackupLimits(
    files=10_000, entries=20_000, file_bytes=MAX_ARTIFACT_BYTES,
    total_bytes=2_000_000_000, manifest_bytes=16 * 1024 * 1024,
)
MAX_CHECKPOINTS = 500
MAX_RESTORES = 1000
MAX_ARCHIVED_RESTORES = 100_000
MAX_RETAINED_BYTES = 2_000_000_000
PENDING_RESTORE = "pending-workspace-restore.json"
PENDING_RETENTION = "pending-workspace-retention.json"
PENDING_ARCHIVE = "pending-workspace-review-archive.json"


class WorkspaceRecoveryError(ValueError):
    """Safe, stable failure messages without workspace content."""


class WorkspaceBusyError(WorkspaceRecoveryError):
    pass


class WorkspaceCaptureCancelled(WorkspaceRecoveryError):
    pass


class CheckpointFile(APIModel):
    path: str = Field(min_length=1, max_length=1000)
    size: int = Field(ge=0, le=MAX_ARTIFACT_BYTES)
    mode: int = Field(ge=0, le=0o700)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class CheckpointDirectory(APIModel):
    path: str = Field(min_length=1, max_length=1000)
    mode: int = Field(ge=0, le=0o700)


class WorkspaceCheckpoint(APIModel):
    schema_version: Literal[1] = 1
    id: str
    project_id: str
    run_id: str | None = None
    trace_id: str | None = None
    kind: Literal["run_start", "before_restore"]
    created_by: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    files: list[CheckpointFile] = Field(max_length=LIMITS.files)
    directories: list[CheckpointDirectory] = Field(max_length=LIMITS.entries)
    total_bytes: int = Field(ge=0, le=LIMITS.total_bytes)

    def inventory(self) -> dict:
        return {
            "files": [item.model_dump() for item in self.files],
            "directories": [item.model_dump() for item in self.directories],
        }


class CheckpointSummary(APIModel):
    id: str
    project_id: str
    run_id: str | None
    kind: Literal["run_start", "before_restore"]
    created_at: datetime
    files: int
    total_bytes: int


class WorkspaceChange(APIModel):
    path: str
    action: Literal["restore", "replace", "remove"]
    before_kind: Literal["file", "directory"] | None = None
    after_kind: Literal["file", "directory"] | None = None


class WorkspaceRestore(APIModel):
    schema_version: Literal[1] = 1
    id: str = Field(default_factory=lambda: new_id("restore"))
    project_id: str
    checkpoint_id: str
    target_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    current_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_by: str
    principal_session_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime = Field(default_factory=lambda: utc_now() + timedelta(minutes=10))
    state: Literal["preview", "applying", "applied"] = "preview"
    changes: list[WorkspaceChange] = Field(max_length=LIMITS.entries * 2)
    removes_files: int = Field(ge=0, le=LIMITS.files)
    safety_checkpoint_id: str | None = None
    retained_bytes: int = Field(default=0, ge=0, le=MAX_RETAINED_BYTES)
    retained_discarded_at: datetime | None = None
    archived_at: datetime | None = None
    archived_by: str | None = None
    before_device: int | None = None
    before_inode: int | None = None
    staged_device: int | None = None
    staged_inode: int | None = None
    applied_at: datetime | None = None


class RestorePreviewRequest(APIModel):
    checkpoint_id: str = Field(min_length=1, max_length=100)


class RestoreApplyRequest(APIModel):
    confirm_file_changes: Literal[True]

    @field_validator("confirm_file_changes", mode="before")
    @classmethod
    def explicit_boolean(cls, value):
        if value is not True:
            raise ValueError("File changes must be explicitly confirmed.")
        return value


class RetentionDeleteRequest(APIModel):
    expected_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirm_delete: Literal[True]

    @field_validator("confirm_delete", mode="before")
    @classmethod
    def explicit_boolean(cls, value):
        if value is not True:
            raise ValueError("History deletion must be explicitly confirmed.")
        return value


class RetentionCheckpoint(APIModel):
    id: str
    run_id: str | None
    kind: Literal["run_start", "before_restore"]
    created_at: datetime
    files: int
    total_bytes: int
    fingerprint: str
    referenced_by_reviews: int


class RetentionReview(APIModel):
    id: str
    checkpoint_id: str
    state: Literal["preview", "applying", "applied"]
    created_at: datetime
    expires_at: datetime
    fingerprint: str
    retained_bytes: int
    retained_discarded_at: datetime | None
    can_delete: bool
    can_archive: bool


class WorkspaceRetentionView(APIModel):
    project_id: str
    checkpoint_limit: int
    review_limit: int
    retained_bytes_limit: int
    retained_bytes: int
    archived_reviews: int
    snapshot_files: int
    snapshot_bytes: int
    reclaimable_files: int
    reclaimable_bytes: int
    reclaim_fingerprint: str
    checkpoints: list[RetentionCheckpoint]
    reviews: list[RetentionReview]


class RetentionArchivedReview(APIModel):
    id: str
    checkpoint_id: str
    created_at: datetime
    archived_at: datetime
    archived_by: str
    fingerprint: str


class RetentionArchivePage(APIModel):
    project_id: str
    reviews: list[RetentionArchivedReview]
    next_cursor: str | None


class RetentionDiscardIntent(APIModel):
    restore_id: str
    project_id: str
    current_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    safety_checkpoint_id: str
    retained_bytes: int = Field(ge=0, le=MAX_RETAINED_BYTES)
    retained_device: int
    retained_inode: int


class RetentionArchiveIntent(APIModel):
    restore_id: str
    project_id: str
    expected_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    archived_at: datetime
    archived_by: str


class WorkspaceRestoreView(APIModel):
    id: str
    project_id: str
    checkpoint_id: str
    created_at: datetime
    expires_at: datetime
    state: Literal["preview", "applying", "applied"]
    changes: list[WorkspaceChange]
    removes_files: int
    safety_checkpoint_id: str | None
    applied_at: datetime | None
    archived_at: datetime | None = None

    @classmethod
    def of(cls, value: WorkspaceRestore):
        return cls.model_validate({key: getattr(value, key) for key in cls.model_fields})


def fingerprint(inventory: dict) -> str:
    return hashlib.sha256(json.dumps(
        inventory, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()


def try_workspace_lease(ledger, project_id: str) -> int | None:
    directory = ledger.root / "workspace-leases"
    directory.mkdir(mode=0o700, exist_ok=True)
    with directory_fd(directory) as fd:
        descriptor = os.open(
            f"{ledger._safe_id(project_id)}.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
            0o600, dir_fd=fd,
        )
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise WorkspaceRecoveryError("Workspace lease is not a private regular file.")
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return descriptor
    except BlockingIOError:
        os.close(descriptor)
        return None
    except BaseException:
        os.close(descriptor)
        raise


@contextmanager
def workspace_lease(ledger, project_id: str):
    with ledger.lifecycle.lease(project_id):
        descriptor = try_workspace_lease(ledger, project_id)
        if descriptor is None:
            raise WorkspaceBusyError(
                "The project workspace is in use. Wait for its runs to finish.",
            )
        try:
            yield
        finally:
            os.close(descriptor)


class WorkspaceCheckpoints:
    def __init__(self, ledger):
        self.ledger = ledger

    def _root(self, project_id: str) -> Path:
        self.ledger.get_project(project_id)
        return self.ledger._project_path(project_id).parent

    def _directory(self, project_id: str, name: str) -> Path:
        root = self._root(project_id)
        with directory_fd(root) as fd:
            try:
                os.mkdir(name, mode=0o700, dir_fd=fd)
                os.fsync(fd)
            except FileExistsError:
                pass
        directory = root / name
        with directory_fd(directory):
            pass
        return directory

    def _read(self, project_id: str, directory: str, identifier: str, model):
        identifier = self.ledger._safe_id(identifier)
        raw = read_relative(
            self._root(project_id), f"{directory}/{identifier}.json", LIMITS.manifest_bytes,
        )
        value = model.model_validate_json(raw)
        if value.id != identifier or value.project_id != project_id:
            raise WorkspaceRecoveryError("Workspace recovery record does not match this project.")
        return value

    def load(self, project_id: str, checkpoint_id: str) -> WorkspaceCheckpoint:
        value = self._read(project_id, "workspace-checkpoints", checkpoint_id, WorkspaceCheckpoint)
        inventory = value.inventory()
        seen: set[str] = set()
        if len(value.files) + len(value.directories) > LIMITS.entries:
            raise WorkspaceRecoveryError("Workspace checkpoint exceeds its entry limit.")
        for item in [*value.files, *value.directories]:
            relative_parts(item.path)
            if item.mode & ~0o700:
                raise WorkspaceRecoveryError("Workspace checkpoint has unsafe permission bits.")
            if item.path.casefold() in seen:
                raise WorkspaceRecoveryError("Workspace checkpoint contains duplicate paths.")
            seen.add(item.path.casefold())
        directories = {item.path for item in value.directories}
        for item in [*value.files, *value.directories]:
            parent = Path(item.path).parent.as_posix()
            if parent != "." and parent not in directories:
                raise WorkspaceRecoveryError("Workspace checkpoint has an invalid directory tree.")
        if (
            fingerprint(inventory) != value.fingerprint
            or sum(item.size for item in value.files) != value.total_bytes
            or any(rows != sorted(rows, key=lambda row: row["path"])
                   for rows in inventory.values())
        ):
            raise WorkspaceRecoveryError("Workspace checkpoint failed its integrity check.")
        if value.kind == "run_start":
            from .conversation_deletion_state import ConversationDeletionStates
            from .storage import LedgerNotFoundError

            try:
                run = self.ledger.get_run(value.run_id or "")
            except LedgerNotFoundError:
                run = ConversationDeletionStates(self.ledger.lifecycle).origins(project_id).get(
                    value.run_id,
                )
                if run is None:
                    raise WorkspaceRecoveryError(
                        "Workspace checkpoint Run origin is unavailable",
                    ) from None
            if run.project_id != project_id or run.trace_id != value.trace_id:
                raise WorkspaceRecoveryError("Workspace checkpoint has inconsistent Run origin.")
        elif value.run_id is not None or value.trace_id is not None:
            raise WorkspaceRecoveryError("A recovery copy cannot claim a Run origin.")
        return value

    def _ids(self, project_id: str, name: str, limit: int) -> list[str]:
        directory = self._root(project_id) / name
        if not directory.exists() and not directory.is_symlink():
            return []
        with directory_fd(directory) as fd:
            names = os.listdir(fd)
        names = [name for name in names if not name.startswith(".")]
        if len(names) > limit or any(not name.endswith(".json") for name in names):
            raise WorkspaceRecoveryError("Recovery store is invalid or exceeds its limits.")
        return [self.ledger._safe_id(name.removesuffix(".json")) for name in names]

    def list(self, project_id: str) -> list[CheckpointSummary]:
        with self.ledger._lock_for(project_id):
            summaries = []
            for identifier in self._ids(project_id, "workspace-checkpoints", MAX_CHECKPOINTS):
                item = self.load(project_id, identifier)
                summaries.append(CheckpointSummary(
                    id=item.id, project_id=project_id, run_id=item.run_id, kind=item.kind,
                    created_at=item.created_at, files=len(item.files), total_bytes=item.total_bytes,
                ))
            return sorted(summaries, key=lambda item: item.created_at, reverse=True)

    def retention(
        self, project_id: str, *, authorize: Callable[[], None],
    ) -> WorkspaceRetentionView:
        with workspace_lease(self.ledger, project_id), self.ledger._lock_for(project_id):
            authorize()
            self.recover(project_id)
            self._recover_edit(project_id)
            reviews = [self.get_restore(project_id, identifier) for identifier in
                       self._ids(project_id, "workspace-restores", MAX_RESTORES)]
            references: dict[str, int] = {}
            for review in reviews:
                for identifier in (review.checkpoint_id, review.safety_checkpoint_id):
                    if identifier:
                        references[identifier] = references.get(identifier, 0) + 1
            checkpoints = [self.load(project_id, identifier) for identifier in
                           self._ids(project_id, "workspace-checkpoints", MAX_CHECKPOINTS)]
            snapshots, reclaimable, reclaim_fingerprint = self._snapshot_inventory(
                project_id, checkpoints,
            )
            now = utc_now()
            result = WorkspaceRetentionView(
                project_id=project_id, checkpoint_limit=MAX_CHECKPOINTS,
                review_limit=MAX_RESTORES, retained_bytes_limit=MAX_RETAINED_BYTES,
                retained_bytes=sum(review.retained_bytes for review in reviews),
                archived_reviews=len(self._ids(
                    project_id, "workspace-restores-archive", MAX_ARCHIVED_RESTORES,
                )),
                snapshot_files=len(snapshots),
                snapshot_bytes=sum(row["size"] for row in snapshots),
                reclaimable_files=len(reclaimable),
                reclaimable_bytes=sum(row["size"] for row in reclaimable),
                reclaim_fingerprint=reclaim_fingerprint,
                checkpoints=sorted((RetentionCheckpoint(
                    id=item.id, run_id=item.run_id, kind=item.kind,
                    created_at=item.created_at, files=len(item.files),
                    total_bytes=item.total_bytes, fingerprint=item.fingerprint,
                    referenced_by_reviews=references.get(item.id, 0),
                ) for item in checkpoints), key=lambda item: item.created_at, reverse=True),
                reviews=sorted((RetentionReview(
                    id=item.id, checkpoint_id=item.checkpoint_id, state=item.state,
                    created_at=item.created_at, expires_at=item.expires_at,
                    fingerprint=self._record_fingerprint(item),
                    retained_bytes=item.retained_bytes,
                    retained_discarded_at=item.retained_discarded_at,
                    can_delete=item.state == "preview" and item.expires_at <= now,
                    can_archive=item.state == "applied" and item.retained_discarded_at is not None,
                ) for item in reviews), key=lambda item: item.created_at, reverse=True),
            )
            authorize()
            return result

    def archived_reviews(
        self, project_id: str, *, cursor: str | None, authorize: Callable[[], None],
    ) -> RetentionArchivePage:
        with workspace_lease(self.ledger, project_id), self.ledger._lock_for(project_id):
            authorize()
            self.recover(project_id)
            if cursor is not None:
                self.ledger._safe_id(cursor)
            identifiers = sorted(self._ids(
                project_id, "workspace-restores-archive", MAX_ARCHIVED_RESTORES,
            ), reverse=True)
            page_ids = [identifier for identifier in identifiers
                        if cursor is None or identifier < cursor][:51]
            reviews = []
            for identifier in page_ids[:50]:
                value = self._archived_review(project_id, identifier)
                reviews.append(RetentionArchivedReview(
                    id=value.id, checkpoint_id=value.checkpoint_id,
                    created_at=value.created_at, archived_at=value.archived_at,
                    archived_by=value.archived_by,
                    fingerprint=self._record_fingerprint(value),
                ))
            authorize()
            return RetentionArchivePage(
                project_id=project_id, reviews=reviews,
                next_cursor=page_ids[49] if len(page_ids) > 50 else None,
            )

    def _archived_review(self, project_id: str, restore_id: str) -> WorkspaceRestore:
        review = self._read(
            project_id, "workspace-restores-archive", restore_id, WorkspaceRestore,
        )
        if (review.state != "applied" or review.archived_at is None
                or not review.archived_by or review.retained_discarded_at is None
                or review.retained_bytes != 0):
            raise WorkspaceRecoveryError("Recovery review archive failed verification.")
        return review

    def delete_archived_review(
        self, project_id: str, restore_id: str, *, expected_fingerprint: str,
        authorize: Callable[[], None], assert_idle: Callable[[], None],
    ) -> None:
        with workspace_lease(self.ledger, project_id), self.ledger._lock_for(project_id):
            authorize()
            assert_idle()
            self.recover(project_id)
            review = self._archived_review(project_id, restore_id)
            if self._record_fingerprint(review) != expected_fingerprint:
                raise WorkspaceRecoveryError("Recovery review changed. Review retention again.")
            authorize()
            assert_idle()
            self._unlink_record(project_id, "workspace-restores-archive", review.id)

    def _snapshot_inventory(
        self, project_id: str, checkpoints: list[WorkspaceCheckpoint],
    ) -> tuple[list[dict], list[dict], str]:
        root = self._root(project_id)
        directory = root / SNAPSHOT_DIRECTORY
        if not directory.exists() and not directory.is_symlink():
            snapshots: list[dict] = []
        else:
            snapshots = []
            with directory_fd(directory) as fd, os.scandir(fd) as entries:
                for entry in entries:
                    if entry.name.startswith(".staging-"):
                        continue
                    info = entry.stat(follow_symlinks=False)
                    if (not re.fullmatch(r"[0-9a-f]{64}", entry.name)
                            or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
                        raise WorkspaceRecoveryError("Artifact history contains an unsafe entry.")
                    snapshots.append({
                        "checksum": entry.name, "size": info.st_size,
                        "device": info.st_dev, "inode": info.st_ino,
                    })
            if len(snapshots) > MAX_ARTIFACT_HISTORY_FILES:
                raise WorkspaceRecoveryError("Artifact history file limit reached.")
            snapshots.sort(key=lambda row: row["checksum"])
        references = {
            artifact.checksum for artifact in self.ledger.get_project(project_id).artifacts
            if artifact.content_storage == "snapshot"
        }
        references.update(row.sha256 for point in checkpoints for row in point.files)
        reclaimable = [row for row in snapshots if row["checksum"] not in references]
        digest = hashlib.sha256(json.dumps(
            reclaimable, sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()
        return snapshots, reclaimable, digest

    def reclaim_snapshots(
        self, project_id: str, *, expected_fingerprint: str,
        authorize: Callable[[], None], assert_idle: Callable[[], None],
    ) -> None:
        with workspace_lease(self.ledger, project_id), self.ledger._lock_for(project_id):
            authorize()
            assert_idle()
            self.recover(project_id)
            self._recover_edit(project_id)
            checkpoints = [self.load(project_id, identifier) for identifier in
                           self._ids(project_id, "workspace-checkpoints", MAX_CHECKPOINTS)]
            _, reclaimable, digest = self._snapshot_inventory(project_id, checkpoints)
            if digest != expected_fingerprint:
                raise WorkspaceRecoveryError("Snapshot storage changed. Review retention again.")
            authorize()
            assert_idle()
            directory = self._root(project_id) / SNAPSHOT_DIRECTORY
            if not directory.exists() and not directory.is_symlink():
                return
            with directory_fd(directory) as fd:
                for row in reclaimable:
                    info = os.stat(row["checksum"], dir_fd=fd, follow_symlinks=False)
                    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                            or (info.st_size, info.st_dev, info.st_ino) != (
                                row["size"], row["device"], row["inode"],
                            )):
                        raise WorkspaceRecoveryError("Snapshot storage changed during deletion.")
                    os.unlink(row["checksum"], dir_fd=fd)
                os.fsync(fd)

    def delete_checkpoint(
        self, project_id: str, checkpoint_id: str, *, expected_fingerprint: str,
        authorize: Callable[[], None], assert_idle: Callable[[], None],
    ) -> None:
        with workspace_lease(self.ledger, project_id), self.ledger._lock_for(project_id):
            authorize()
            assert_idle()
            self.recover(project_id)
            item = self.load(project_id, checkpoint_id)
            if item.fingerprint != expected_fingerprint:
                raise WorkspaceRecoveryError("Recovery point changed. Review retention again.")
            for identifier in self._ids(project_id, "workspace-restores", MAX_RESTORES):
                review = self.get_restore(project_id, identifier)
                if item.id in (review.checkpoint_id, review.safety_checkpoint_id):
                    raise WorkspaceRecoveryError("Recovery point is used by a saved review.")
            authorize()
            assert_idle()
            self._unlink_record(project_id, "workspace-checkpoints", item.id)

    def delete_expired_review(
        self, project_id: str, restore_id: str, *, expected_fingerprint: str,
        authorize: Callable[[], None], assert_idle: Callable[[], None],
    ) -> None:
        with workspace_lease(self.ledger, project_id), self.ledger._lock_for(project_id):
            authorize()
            assert_idle()
            self.recover(project_id)
            review = self.get_restore(project_id, restore_id)
            if self._record_fingerprint(review) != expected_fingerprint:
                raise WorkspaceRecoveryError("Recovery review changed. Review retention again.")
            if review.state != "preview" or review.expires_at > utc_now():
                raise WorkspaceRecoveryError("Only expired, unused reviews can be deleted.")
            authorize()
            assert_idle()
            self._unlink_record(project_id, "workspace-restores", review.id)

    def discard_retained_copy(
        self, project_id: str, restore_id: str, *, expected_fingerprint: str,
        authorize: Callable[[], None], assert_idle: Callable[[], None],
    ) -> None:
        with workspace_lease(self.ledger, project_id), self.ledger._lock_for(project_id):
            authorize()
            assert_idle()
            self.recover(project_id)
            review = self.get_restore(project_id, restore_id)
            if self._record_fingerprint(review) != expected_fingerprint:
                raise WorkspaceRecoveryError("Recovery review changed. Review retention again.")
            if review.state != "applied" or review.retained_discarded_at is not None:
                raise WorkspaceRecoveryError("This recovery has no retained workspace copy.")
            safety = self.load(project_id, review.safety_checkpoint_id or "")
            if safety.fingerprint != review.current_fingerprint:
                raise WorkspaceRecoveryError("Recovery safety point failed verification.")
            root = self._root(project_id)
            retained = root / f"workspace-retained-{review.id}"
            identity = self._identity(retained)
            if (identity is None
                    or fingerprint(tree_inventory(retained, LIMITS)) != safety.fingerprint):
                raise WorkspaceRecoveryError("Retained workspace copy failed verification.")
            authorize()
            assert_idle()
            self._write(root / PENDING_RETENTION, RetentionDiscardIntent(
                restore_id=review.id, project_id=project_id,
                current_fingerprint=review.current_fingerprint,
                safety_checkpoint_id=safety.id, retained_bytes=review.retained_bytes,
                retained_device=identity[0], retained_inode=identity[1],
            ))
            self._recover_discard(project_id)

    def archive_review(
        self, project_id: str, restore_id: str, *, expected_fingerprint: str,
        archived_by: str, authorize: Callable[[], None], assert_idle: Callable[[], None],
    ) -> None:
        with workspace_lease(self.ledger, project_id), self.ledger._lock_for(project_id):
            authorize()
            assert_idle()
            self.recover(project_id)
            review = self._read(project_id, "workspace-restores", restore_id, WorkspaceRestore)
            if self._record_fingerprint(review) != expected_fingerprint:
                raise WorkspaceRecoveryError("Recovery review changed. Review retention again.")
            if review.state != "applied" or review.retained_discarded_at is None:
                raise WorkspaceRecoveryError(
                    "Discard the retained folder before archiving a review.",
                )
            if len(self._ids(
                project_id, "workspace-restores-archive", MAX_ARCHIVED_RESTORES,
            )) >= MAX_ARCHIVED_RESTORES:
                raise WorkspaceRecoveryError("Recovery review archive limit reached.")
            authorize()
            assert_idle()
            self._write(self._root(project_id) / PENDING_ARCHIVE, RetentionArchiveIntent(
                restore_id=review.id, project_id=project_id,
                expected_fingerprint=expected_fingerprint,
                archived_at=utc_now(), archived_by=archived_by,
            ))
            self._recover_archive(project_id)

    def _record_fingerprint(self, value: APIModel) -> str:
        return hashlib.sha256(self.ledger._dump(value).encode()).hexdigest()

    def _unlink_record(self, project_id: str, directory: str, identifier: str) -> None:
        with directory_fd(self._root(project_id) / directory) as fd:
            os.unlink(f"{self.ledger._safe_id(identifier)}.json", dir_fd=fd)
            os.fsync(fd)

    def _write(self, path: Path, value: APIModel) -> None:
        if len(self.ledger._dump(value).encode()) + 1 > LIMITS.manifest_bytes:
            raise WorkspaceRecoveryError("Workspace recovery record exceeds its size limit.")
        self.ledger._atomic_write(path, value)

    def capture(
        self, project_id: str, *, run_id: str | None = None, created_by: str | None = None,
        restore_id: str | None = None,
        check: Callable[[], None] | None = None,
    ) -> WorkspaceCheckpoint | None:
        """Caller excludes other runtime writers and holds the project lock."""
        run = self.ledger.get_run(run_id) if run_id else None
        identifier = (f"before-{run.id}" if run else
                      f"safety-{self.ledger._safe_id(restore_id)}" if restore_id
                      else new_id("checkpoint"))
        if run and (run.project_id != project_id or restore_id is not None):
            raise WorkspaceRecoveryError("Run does not belong to this workspace.")
        if run or restore_id:
            try:
                return self.load(project_id, identifier)
            except FileNotFoundError:
                if run and run.started_at is not None:
                    # A legacy resumed Run has no provable original start state.
                    return None
        if len(self._ids(project_id, "workspace-checkpoints", MAX_CHECKPOINTS)) >= MAX_CHECKPOINTS:
            raise WorkspaceRecoveryError("Workspace checkpoint count limit reached.")
        root = self._root(project_id)
        workspace = root / "workspace"
        inventory = tree_inventory(workspace, LIMITS, check=check)
        for item in inventory["files"]:
            if check is not None:
                check()
            raw = read_relative(workspace, item["path"], LIMITS.file_bytes)
            if len(raw) != item["size"] or hashlib.sha256(raw).hexdigest() != item["sha256"]:
                raise WorkspaceRecoveryError("Workspace changed while preparing recovery.")
            freeze_artifact(root, raw)
        if tree_inventory(workspace, LIMITS, check=check) != inventory:
            raise WorkspaceRecoveryError("Workspace changed while preparing recovery.")
        value = WorkspaceCheckpoint(
            id=identifier, project_id=project_id, run_id=run_id,
            trace_id=run.trace_id if run else None,
            kind="run_start" if run else "before_restore", created_by=created_by,
            fingerprint=fingerprint(inventory), files=inventory["files"],
            directories=inventory["directories"],
            total_bytes=sum(item["size"] for item in inventory["files"]),
        )
        directory = self._directory(project_id, "workspace-checkpoints")
        if check is not None:
            check()
        self._write(directory / f"{identifier}.json", value)
        return value

    def prepare_run(self, run_id: str, cancelled: Callable[[], bool] = lambda: False) -> None:
        def check():
            if cancelled():
                raise WorkspaceCaptureCancelled("Workspace checkpoint cancelled.")

        run = self.ledger.get_run(run_id)
        with self.ledger._lock_for(run.project_id):
            check()
            self.recover(run.project_id)
            self.ledger.workspace_for_project(run.project_id)
            self._recover_edit(run.project_id)
            self.capture(run.project_id, run_id=run_id, check=check)

    def _recover_edit(self, project_id: str) -> None:
        from .artifact_edits import recover_pending_edit

        recover_pending_edit(self.ledger, project_id)

    def get_restore(self, project_id: str, restore_id: str) -> WorkspaceRestore:
        try:
            return self._read(project_id, "workspace-restores", restore_id, WorkspaceRestore)
        except FileNotFoundError:
            return self._read(
                project_id, "workspace-restores-archive", restore_id, WorkspaceRestore,
            )

    def _save_restore(self, value: WorkspaceRestore) -> None:
        directory = self._directory(value.project_id, "workspace-restores")
        self._write(directory / f"{value.id}.json", value)

    def preview(
        self, project_id: str, checkpoint_id: str, *, created_by: str,
        principal_session_id: str | None, authorize: Callable[[], None],
        assert_idle: Callable[[], None],
    ) -> WorkspaceRestore:
        with workspace_lease(self.ledger, project_id), self.ledger._lock_for(project_id):
            authorize()
            assert_idle()
            self.recover(project_id)
            self._recover_edit(project_id)
            target = self.load(project_id, checkpoint_id)
            current = tree_inventory(self._root(project_id) / "workspace", LIMITS)
            before = {row["path"]: (kind, row) for kind, rows in (
                ("file", current["files"]), ("directory", current["directories"]),
            ) for row in rows}
            after = {row["path"]: (kind, row) for kind, rows in (
                ("file", target.inventory()["files"]),
                ("directory", target.inventory()["directories"]),
            ) for row in rows}
            changes = [WorkspaceChange(
                path=path, action="restore" if path not in before else (
                    "remove" if path not in after else "replace"
                ), before_kind=before[path][0] if path in before else None,
                after_kind=after[path][0] if path in after else None,
            ) for path in sorted(before.keys() | after.keys())
                if before.get(path) != after.get(path)]
            value = WorkspaceRestore(
                project_id=project_id, checkpoint_id=checkpoint_id,
                target_fingerprint=target.fingerprint, current_fingerprint=fingerprint(current),
                created_by=created_by, principal_session_id=principal_session_id, changes=changes,
                removes_files=sum(item.before_kind == "file" and item.after_kind != "file"
                                  for item in changes),
            )
            if len(self._ids(project_id, "workspace-restores", MAX_RESTORES)) >= MAX_RESTORES:
                raise WorkspaceRecoveryError("Workspace recovery review limit reached.")
            authorize()
            self._save_restore(value)
            return value

    def _materialize(self, root: Path, target: WorkspaceCheckpoint, stage: Path) -> None:
        stage.mkdir(mode=0o700)
        try:
            for item in target.directories:
                (stage / item.path).mkdir(mode=0o700, parents=True, exist_ok=True)
            for item in target.files:
                raw = read_snapshot(root, item.sha256, LIMITS.file_bytes)
                if len(raw) != item.size:
                    raise WorkspaceRecoveryError("Workspace snapshot size does not match.")
                with (stage / item.path).open("xb") as handle:
                    handle.write(raw)
                    handle.flush()
                    os.fsync(handle.fileno())
                    os.fchmod(handle.fileno(), item.mode)
            for item in reversed(target.directories):
                (stage / item.path).chmod(item.mode)
            sync_directories(stage)
            if tree_inventory(stage, LIMITS) != target.inventory():
                raise WorkspaceRecoveryError("Staged workspace failed verification.")
        except BaseException:
            remove_staging(stage)
            raise

    def apply(
        self, project_id: str, restore_id: str, *, created_by: str,
        principal_session_id: str | None, authorize: Callable[[], None],
        assert_idle: Callable[[], None],
    ) -> WorkspaceRestore:
        with workspace_lease(self.ledger, project_id), self.ledger._lock_for(project_id):
            authorize()
            assert_idle()
            self.recover(project_id)
            self._recover_edit(project_id)
            value = self.get_restore(project_id, restore_id)
            if (value.created_by, value.principal_session_id) != (created_by, principal_session_id):
                raise WorkspaceRecoveryError("Only the reviewer can apply this workspace recovery.")
            if value.state == "applied":
                return value
            if value.state != "preview" or value.expires_at <= utc_now():
                raise WorkspaceRecoveryError("Recovery preview expired. Review the changes again.")
            root = self._root(project_id)
            target = self.load(project_id, value.checkpoint_id)
            current = tree_inventory(root / "workspace", LIMITS)
            if (target.fingerprint != value.target_fingerprint
                    or fingerprint(current) != value.current_fingerprint):
                raise WorkspaceRecoveryError("Workspace changed after review. Preview again.")
            retained = sum(
                self.get_restore(project_id, identifier).retained_bytes
                for identifier in self._ids(project_id, "workspace-restores", MAX_RESTORES)
            )
            current_size = sum(row["size"] for row in current["files"])
            if retained + current_size > MAX_RETAINED_BYTES:
                raise WorkspaceRecoveryError("Recovery copies reached their storage limit.")
            safety = self.capture(project_id, created_by=created_by, restore_id=value.id)
            assert safety is not None
            if safety.fingerprint != value.current_fingerprint:
                raise WorkspaceRecoveryError("Recovery copy differs from the reviewed state.")
            value.safety_checkpoint_id = safety.id
            value.retained_bytes = current_size
            stage = root / f"workspace-stage-{value.id}"
            # A death before publishing intent can leave this review's private
            # staging directory. No original files have moved at that point.
            if self._identity(stage) is not None:
                remove_staging(stage)
            self._materialize(root, target, stage)
            try:
                authorize()
                assert_idle()
                if fingerprint(tree_inventory(root / "workspace", LIMITS)) != (
                    value.current_fingerprint
                ):
                    raise WorkspaceRecoveryError("Workspace changed after review. Preview again.")
                before, staged = (root / "workspace").lstat(), stage.lstat()
                value.before_device, value.before_inode = before.st_dev, before.st_ino
                value.staged_device, value.staged_inode = staged.st_dev, staged.st_ino
                value.state = "applying"
                # The final tree read can be slow; consume human authority only
                # after it, immediately before persisting the swap intent.
                authorize()
                assert_idle()
                # This single durable write authorizes the swap. Publishing the
                # mutable review record first would strand a retry on a crash.
                self._write(root / PENDING_RESTORE, value)
            except BaseException:
                # A write can fail after rename/fsync. Retain staging whenever
                # intent may have been published; recovery will verify it.
                if not (root / PENDING_RESTORE).exists():
                    remove_staging(stage)
                raise
            self.recover(project_id)
            return self.get_restore(project_id, restore_id)

    @staticmethod
    def _identity(path: Path) -> tuple[int, int] | None:
        try:
            info = path.lstat()
        except FileNotFoundError:
            return None
        if not stat.S_ISDIR(info.st_mode):
            raise WorkspaceRecoveryError("Recovery directory is a link or special file.")
        return info.st_dev, info.st_ino

    def recover(self, project_id: str) -> None:
        with self.ledger.lifecycle.lease(project_id):
            self._recover(project_id)

    def _recover(self, project_id: str) -> None:
        """Finish a reviewed directory swap under the project lock; no SDK work."""
        root = self._root(project_id)
        try:
            raw = read_relative(root, PENDING_RESTORE, LIMITS.manifest_bytes)
        except FileNotFoundError:
            # Death before authorization can leave private target copies, but
            # never moved originals. Bound disk use by discarding only staging
            # tied to an existing, still-uncommitted human review.
            for stage in root.glob("workspace-stage-*"):
                identifier = self.ledger._safe_id(stage.name.removeprefix("workspace-stage-"))
                if self.get_restore(project_id, identifier).state != "preview":
                    raise WorkspaceRecoveryError("Unknown recovery staging directory.") from None
                self._identity(stage)
                remove_staging(stage)
            self._recover_discard(project_id)
            self._recover_archive(project_id)
            return
        value = WorkspaceRestore.model_validate_json(raw)
        identifier = self.ledger._safe_id(value.id)
        review = self.get_restore(project_id, identifier)
        bindings = ("project_id", "checkpoint_id", "created_by", "principal_session_id",
                    "current_fingerprint", "target_fingerprint", "changes", "created_at")
        if (value.project_id != project_id or value.state != "applying"
                or not value.safety_checkpoint_id
                or any(getattr(value, key) != getattr(review, key) for key in bindings)):
            raise WorkspaceRecoveryError("Incomplete workspace recovery authorization record.")
        if review.state == "applied":
            # The artifact ledger and applied record were already fsynced. A
            # later manual edit must not prevent clearing the completed intent.
            self._clear_pending(root)
            return
        target = self.load(project_id, value.checkpoint_id)
        safety = self.load(project_id, value.safety_checkpoint_id)
        if (target.fingerprint != value.target_fingerprint
                or safety.fingerprint != value.current_fingerprint):
            raise WorkspaceRecoveryError("Recovery snapshots differ from the reviewed state.")
        workspace, stage, retained = (
            root / "workspace", root / f"workspace-stage-{identifier}",
            root / f"workspace-retained-{identifier}",
        )
        before_id = (value.before_device, value.before_inode)
        stage_id = (value.staged_device, value.staged_inode)
        if self._identity(stage) == stage_id and self._identity(retained) is None:
            if (self._identity(workspace) != before_id
                    or fingerprint(tree_inventory(workspace, LIMITS)) != value.current_fingerprint):
                raise WorkspaceRecoveryError("Workspace changed during recovery. Files preserved.")
            if fingerprint(tree_inventory(stage, LIMITS)) != target.fingerprint:
                raise WorkspaceRecoveryError("Recovery staging content changed.")
            publish_exclusive(workspace, retained)
            with directory_fd(root) as fd:
                os.fsync(fd)
        if self._identity(stage) == stage_id and self._identity(workspace) is None:
            if self._identity(retained) != before_id:
                raise WorkspaceRecoveryError("Retained workspace identity changed.")
            publish_exclusive(stage, workspace)
            with directory_fd(root) as fd:
                os.fsync(fd)
        if (self._identity(workspace) != stage_id or self._identity(retained) != before_id
                or self._identity(stage) is not None):
            raise WorkspaceRecoveryError("Workspace recovery directory state is inconsistent.")
        if fingerprint(tree_inventory(workspace, LIMITS)) != target.fingerprint:
            raise WorkspaceRecoveryError("Restored workspace changed before recovery confirmation.")
        # Keep version history and truth; restored files become new human versions.
        for item in target.files:
            project = self.ledger.get_project(project_id)
            source = next((row for row in reversed(project.artifacts)
                           if row.path == item.path and row.checksum == item.sha256), None)
            self.ledger._register_artifact_bytes(
                project_id, item.path, read_snapshot(root, item.sha256),
                source_ids=[source.id] if source else [], created_by=value.created_by,
            )
        value.state = "applied"
        value.applied_at = value.applied_at or utc_now()
        self._save_restore(value)
        self._clear_pending(root)
        self._recover_discard(project_id)
        self._recover_archive(project_id)

    def _recover_discard(self, project_id: str) -> None:
        root = self._root(project_id)
        try:
            raw = read_relative(root, PENDING_RETENTION, 1024)
        except FileNotFoundError:
            return
        intent = RetentionDiscardIntent.model_validate_json(raw)
        if intent.project_id != project_id:
            raise WorkspaceRecoveryError("Retained-copy deletion does not match this project.")
        review = self.get_restore(project_id, intent.restore_id)
        if (review.state != "applied"
                or review.current_fingerprint != intent.current_fingerprint
                or review.safety_checkpoint_id != intent.safety_checkpoint_id):
            raise WorkspaceRecoveryError("Retained-copy deletion authorization changed.")
        safety = self.load(project_id, intent.safety_checkpoint_id)
        if safety.fingerprint != intent.current_fingerprint:
            raise WorkspaceRecoveryError("Recovery safety point failed verification.")
        retained = root / f"workspace-retained-{self.ledger._safe_id(review.id)}"
        identity = self._identity(retained)
        if review.retained_discarded_at is not None:
            if identity is not None or review.retained_bytes != 0:
                raise WorkspaceRecoveryError("Retained-copy deletion state is inconsistent.")
            self._clear_pending(root, PENDING_RETENTION)
            return
        if review.retained_bytes != intent.retained_bytes:
            raise WorkspaceRecoveryError("Retained-copy deletion size changed.")
        if identity is not None:
            if identity != (intent.retained_device, intent.retained_inode):
                raise WorkspaceRecoveryError("Retained workspace identity changed.")
            remove_staging(retained)
            with directory_fd(root) as fd:
                os.fsync(fd)
        review.retained_bytes = 0
        review.retained_discarded_at = utc_now()
        self._save_restore(review)
        self._clear_pending(root, PENDING_RETENTION)

    def _recover_archive(self, project_id: str) -> None:
        root = self._root(project_id)
        try:
            raw = read_relative(root, PENDING_ARCHIVE, 1024)
        except FileNotFoundError:
            return
        intent = RetentionArchiveIntent.model_validate_json(raw)
        if intent.project_id != project_id:
            raise WorkspaceRecoveryError("Recovery review archive does not match this project.")
        try:
            active = self._read(
                project_id, "workspace-restores", intent.restore_id, WorkspaceRestore,
            )
        except FileNotFoundError:
            active = None
        try:
            archived = self._read(
                project_id, "workspace-restores-archive", intent.restore_id,
                WorkspaceRestore,
            )
        except FileNotFoundError:
            archived = None
        if active is not None and (
            self._record_fingerprint(active) != intent.expected_fingerprint
            or active.state != "applied" or active.retained_discarded_at is None
            or active.archived_at is not None
        ):
            raise WorkspaceRecoveryError("Recovery review changed during archiving.")
        if archived is not None:
            original = archived.model_copy(update={"archived_at": None, "archived_by": None})
            if (archived.state != "applied" or archived.retained_discarded_at is None
                    or archived.archived_at != intent.archived_at
                    or archived.archived_by != intent.archived_by
                    or self._record_fingerprint(original) != intent.expected_fingerprint):
                raise WorkspaceRecoveryError("Recovery review archive failed verification.")
        elif active is not None:
            archived = active.model_copy(update={
                "archived_at": intent.archived_at, "archived_by": intent.archived_by,
            })
            self._write(
                self._directory(project_id, "workspace-restores-archive")
                / f"{archived.id}.json", archived,
            )
        else:
            raise WorkspaceRecoveryError("Recovery review archive is incomplete.")
        if active is not None:
            self._unlink_record(project_id, "workspace-restores", active.id)
        self._clear_pending(root, PENDING_ARCHIVE)

    @staticmethod
    def _clear_pending(root: Path, name: str = PENDING_RESTORE) -> None:
        with directory_fd(root) as fd:
            os.unlink(name, dir_fd=fd)
            os.fsync(fd)
