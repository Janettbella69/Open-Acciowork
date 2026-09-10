"""Host-owned workspace recovery, including writes made by native Bash.

Only the workspace tree is restored. Project truth, sessions and commercial
receipts remain append-only history. No model call or tool replay is involved.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator

from .artifact_files import MAX_ARTIFACT_BYTES, freeze_artifact, read_snapshot
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
MAX_RETAINED_BYTES = 2_000_000_000
PENDING_RESTORE = "pending-workspace-restore.json"


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
    descriptor = try_workspace_lease(ledger, project_id)
    if descriptor is None:
        raise WorkspaceBusyError("The project workspace is in use. Wait for its runs to finish.")
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
            run = self.ledger.get_run(value.run_id or "")
            if run.project_id != project_id or run.trace_id != value.trace_id:
                raise WorkspaceRecoveryError("Workspace checkpoint has inconsistent Run origin.")
        elif value.run_id is not None or value.trace_id is not None:
            raise WorkspaceRecoveryError("A recovery copy cannot claim a Run origin.")
        return value

    def _ids(self, project_id: str, name: str, limit: int) -> list[str]:
        directory = self._root(project_id) / name
        if not directory.exists():
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
        return self._read(project_id, "workspace-restores", restore_id, WorkspaceRestore)

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

    @staticmethod
    def _clear_pending(root: Path) -> None:
        with directory_fd(root) as fd:
            os.unlink(PENDING_RESTORE, dir_fd=fd)
            os.fsync(fd)
