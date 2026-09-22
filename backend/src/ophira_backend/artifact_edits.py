"""Human text edits with version CAS, immutable history and crash reconciliation."""

from __future__ import annotations

import hashlib
import os
import secrets
from collections.abc import Callable
from pathlib import Path

from pydantic import Field

from .artifact_files import freeze_artifact, read_snapshot
from .constants import TERMINAL_RUN_STATUSES
from .knowledge import TEXT_SUFFIXES
from .models import APIModel, Artifact, new_id
from .skill_proposal_files import child_directory, directory_fd, read_at, relative_parts
from .storage import FileLedger

MAX_EDIT_BYTES = 512 * 1024
PENDING_EDIT = "pending-artifact-edit.json"


class ArtifactEditConflict(ValueError):
    pass


class ArtifactTextUpdate(APIModel):
    content: str = Field(max_length=MAX_EDIT_BYTES)
    expected_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")


class ArtifactTextView(APIModel):
    artifact: Artifact
    content: str
    latest_artifact_id: str
    editable: bool
    edit_notice: str | None = None


def _validate_text_path(path: str) -> None:
    parts = relative_parts(path)
    if (
        any(part.startswith(".") for part in parts)
        or Path(path).suffix.lower() not in TEXT_SUFFIXES
    ):
        raise ValueError("This file format is available through download only.")


def _latest(ledger: FileLedger, project_id: str, path: str) -> Artifact:
    return max(
        (item for item in ledger.get_project(project_id).artifacts if item.path == path),
        key=lambda item: (item.version, item.created_at, item.id),
    )


def _clear_pending(ledger: FileLedger, project_id: str) -> None:
    with directory_fd(ledger._project_path(project_id).parent) as fd:
        os.unlink(PENDING_EDIT, dir_fd=fd)
        os.fsync(fd)


def recover_pending_edit(ledger: FileLedger, project_id: str) -> None:
    """Under the project lock, observe a prior swap; never replay a file write.

If the intended bytes are absent, preserve the working file and discard the
uncommitted intent. Its frozen bytes remain harmless unreferenced history.
"""
    root = ledger._project_path(project_id).parent
    with directory_fd(root) as fd:
        try:
            raw = read_at(fd, PENDING_EDIT, 64 * 1024)
        except FileNotFoundError:
            return
    pending = Artifact.model_validate_json(raw)
    _validate_text_path(pending.path)
    if (
        pending.project_id != project_id or pending.content_storage != "snapshot"
        or not pending.edit_request_id or pending.run_id is not None
        or pending.created_by is None
    ):
        raise ValueError("invalid pending artifact edit")
    project = ledger.get_project(project_id)
    if any(item.id == pending.id for item in project.artifacts):
        _clear_pending(ledger, project_id)
        return
    latest = _latest(ledger, project_id, pending.path)
    if latest.id != pending.parent_artifact_id or pending.version != latest.version + 1:
        raise ArtifactEditConflict("A pending edit conflicts with the saved artifact history.")
    expected = read_snapshot(root, pending.checksum, MAX_EDIT_BYTES)
    if len(expected) != pending.size_bytes:
        raise ValueError("invalid pending artifact size")
    parts = relative_parts(pending.path)
    try:
        with (
            directory_fd(root / "workspace") as fd,
            child_directory(fd, parts[:-1]) as parent,
        ):
            current = read_at(parent, parts[-1], MAX_EDIT_BYTES)
    except (OSError, ValueError):
        current = None
    if current == expected:
        ledger.update_project(project_id, lambda item: item.artifacts.append(pending))
    _clear_pending(ledger, project_id)


def _active_project(
    ledger: FileLedger, project_id: str, is_executing: Callable[[str], bool],
) -> bool:
    return any(
        run.status not in TERMINAL_RUN_STATUSES or is_executing(run.id)
        for run in ledger.list_runs(project_id)
    )


def read_artifact_text(
    ledger: FileLedger, project_id: str, artifact_id: str, *,
    authorize: Callable[[], None], is_executing: Callable[[str], bool],
) -> ArtifactTextView:
    with ledger._lock_for(project_id):
        authorize()
        ledger.workspace_for_project(project_id)
        recover_pending_edit(ledger, project_id)
        artifact, raw = ledger.capture_artifact(project_id, artifact_id, limit=MAX_EDIT_BYTES)
        _validate_text_path(artifact.path)
        content = raw.decode("utf-8")
        if "\x00" in content:
            raise ValueError("This file cannot be edited as UTF-8 text.")
        latest = _latest(ledger, project_id, artifact.path)
        notice = None
        if latest.id != artifact.id:
            notice = "Historical version. Open the latest version to edit."
        elif _active_project(ledger, project_id, is_executing):
            notice = "Wait for all project runs to finish before editing files."
        else:
            parts = relative_parts(artifact.path)
            try:
                with (
                    directory_fd(ledger.workspace_for_project(project_id)) as fd,
                    child_directory(fd, parts[:-1]) as parent,
                ):
                    working = read_at(parent, parts[-1], MAX_EDIT_BYTES)
                if working != raw:
                    notice = (
                        "The working file changed. Register its current version before editing."
                    )
            except (OSError, ValueError):
                notice = (
                    "The working file is unavailable. Download the saved version to recover it."
                )
        authorize()
        return ArtifactTextView(
            artifact=artifact, content=content, latest_artifact_id=latest.id,
            editable=notice is None, edit_notice=notice,
        )


def save_artifact_text(
    ledger: FileLedger, project_id: str, artifact_id: str, payload: ArtifactTextUpdate, *,
    created_by: str, authorize: Callable[[], None], is_executing: Callable[[str], bool],
) -> Artifact:
    raw = payload.content.encode("utf-8")
    if len(raw) > MAX_EDIT_BYTES or b"\x00" in raw:
        raise ValueError("Edits must be UTF-8 text of at most 512 KiB without NUL bytes.")
    with ledger._lock_for(project_id):
        authorize()
        ledger.workspace_for_project(project_id)
        recover_pending_edit(ledger, project_id)
        original, before = ledger.capture_artifact(project_id, artifact_id, limit=MAX_EDIT_BYTES)
        _validate_text_path(original.path)
        if original.checksum != payload.expected_checksum:
            raise ArtifactEditConflict(
                "The selected version changed. Refresh the artifact history."
            )
        checksum = hashlib.sha256(raw).hexdigest()
        previous = next((
            item for item in ledger.get_project(project_id).artifacts
            if item.edit_request_id == payload.request_id
        ), None)
        if previous:
            if (
                previous.parent_artifact_id != original.id or previous.checksum != checksum
                or previous.created_by != created_by
            ):
                raise ArtifactEditConflict(
                    "This edit request was already used for different content."
                )
            authorize()
            return previous
        if _latest(ledger, project_id, original.path).id != original.id:
            raise ArtifactEditConflict("A newer version exists. Refresh and compare before saving.")
        if _active_project(ledger, project_id, is_executing):
            raise ArtifactEditConflict("Wait for all project runs to finish before editing files.")
        parts = relative_parts(original.path)
        with (
            directory_fd(ledger.workspace_for_project(project_id)) as fd,
            child_directory(fd, parts[:-1]) as parent,
        ):
            if read_at(parent, parts[-1], MAX_EDIT_BYTES) != before:
                raise ArtifactEditConflict(
                    "The working file changed. Your draft has not been saved."
                )
            before.decode("utf-8")
            if b"\x00" in before:
                raise ValueError("This file cannot be edited as UTF-8 text.")
            if before == raw:
                return original
            root = ledger._project_path(project_id).parent
            freeze_artifact(root, before)
            freeze_artifact(root, raw)
            if original.content_storage != "snapshot":
                def pin_legacy(project):
                    item = next(row for row in project.artifacts if row.id == original.id)
                    item.content_storage = "snapshot"
                ledger.update_project(project_id, pin_legacy)
            edited = Artifact(
                id=new_id("artifact"), project_id=project_id, path=original.path,
                name=original.name, mime=original.mime, version=original.version + 1,
                # The previous version already retains its own evidence sources.
                # Follow that chain instead of growing every future record's list.
                source_ids=[original.id], checksum=checksum,
                size_bytes=len(raw), download_url="pending", content_storage="snapshot",
                parent_artifact_id=original.id, created_by=created_by,
                edit_request_id=payload.request_id,
            )
            edited.download_url = f"/api/projects/{project_id}/artifacts/{edited.id}/download"
            authorize()
            ledger._atomic_write(root / PENDING_EDIT, edited)
            temporary = f".artifact-edit-{secrets.token_hex(16)}"
            descriptor = os.open(
                temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600, dir_fd=parent,
            )
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(raw)
                    handle.flush()
                    os.fsync(handle.fileno())
                # Recheck both identity and bytes after staging, before any swap.
                authorize()
                with (
                    directory_fd(ledger.workspace_for_project(project_id)) as live_fd,
                    child_directory(live_fd, parts[:-1]) as live_parent,
                ):
                    old_stat, live_stat = os.fstat(parent), os.fstat(live_parent)
                    if (old_stat.st_dev, old_stat.st_ino) != (live_stat.st_dev, live_stat.st_ino):
                        raise ArtifactEditConflict("The working directory changed. Refresh files.")
                    if read_at(parent, parts[-1], MAX_EDIT_BYTES) != before:
                        raise ArtifactEditConflict("The working file changed. Refresh files.")
                    os.replace(temporary, parts[-1], src_dir_fd=parent, dst_dir_fd=parent)
                    os.fsync(parent)
                ledger.update_project(project_id, lambda item: item.artifacts.append(edited))
                _clear_pending(ledger, project_id)
            finally:
                try:
                    os.unlink(temporary, dir_fd=parent)
                except FileNotFoundError:
                    pass
        return edited
