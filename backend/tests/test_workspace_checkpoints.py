from __future__ import annotations

import hashlib
import json
import os
import stat
from datetime import timedelta

import pytest

from ophira_backend import workspace_checkpoints as recovery
from ophira_backend.artifact_files import snapshot_relative
from ophira_backend.backup_files import BackupError, tree_inventory
from ophira_backend.constants import RunStatus
from ophira_backend.models import ProjectCreate, utc_now
from ophira_backend.storage import FileLedger
from ophira_backend.workspace_checkpoints import (
    LIMITS,
    PENDING_ARCHIVE,
    PENDING_RESTORE,
    PENDING_RETENTION,
    WorkspaceBusyError,
    WorkspaceCheckpoints,
    WorkspaceRecoveryError,
    fingerprint,
    workspace_lease,
)


@pytest.fixture
def source(tmp_path):
    ledger = FileLedger(tmp_path / "data")
    project = ledger.create_project(ProjectCreate(name="Recovery", objective="Keep every file"))
    workspace = ledger.workspace_for_project(project.id)
    (workspace / "empty").mkdir()
    (workspace / "report.md").write_bytes("原始报告\r\n".encode())
    (workspace / "tool.sh").write_bytes(b"#!/bin/sh\ntrue\n")
    (workspace / "tool.sh").chmod(0o755)
    (workspace / "binary.bin").write_bytes(bytes(range(256)))
    first = ledger.register_artifact(project.id, "report.md")
    run = ledger.create_run(project.id, "Change the workspace")
    service = WorkspaceCheckpoints(ledger)
    service.prepare_run(run.id)
    point = service.load(project.id, f"before-{run.id}")
    ledger.update_run(run.id, status=RunStatus.FAILED, started_at=utc_now(), finished_at=utc_now())
    return ledger, project.id, service, point, first


def review(source, **kwargs):
    _, project_id, service, point, _ = source
    return service.preview(project_id, point.id, **options(**kwargs))


def options(**kwargs):
    return {"created_by": "editor", "principal_session_id": "session",
            "authorize": lambda: None, "assert_idle": lambda: None, **kwargs}


def retention_options():
    return {"authorize": lambda: None, "assert_idle": lambda: None}


def change_workspace(source):
    ledger, project_id, *_ = source
    workspace = ledger.workspace_for_project(project_id)
    (workspace / "report.md").write_bytes(b"New draft")
    (workspace / "tool.sh").unlink()
    (workspace / "empty").rmdir()
    (workspace / "created.txt").write_bytes(b"Keep in recovery copy")
    ledger.register_artifact(project_id, "report.md")
    return workspace


def test_exact_tree_restore_retains_newer_work_and_version_provenance(source):
    ledger, project_id, service, point, first = source
    workspace = change_workspace(source)
    before = tree_inventory(workspace, LIMITS)
    preview = review(source)
    assert preview.removes_files == 1
    assert {(item.path, item.action) for item in preview.changes} == {
        ("report.md", "replace"), ("tool.sh", "restore"),
        ("empty", "restore"), ("created.txt", "remove"),
    }
    result = service.apply(project_id, preview.id, **options())
    assert result.state == "applied" and result.applied_at
    assert tree_inventory(workspace, LIMITS) == point.inventory()
    retained = workspace.parent / f"workspace-retained-{result.id}"
    assert tree_inventory(retained, LIMITS) == before
    assert service.load(project_id, result.safety_checkpoint_id).inventory() == before
    assert stat.S_IMODE((workspace / "tool.sh").stat().st_mode) == 0o700
    history = [item for item in ledger.get_project(project_id).artifacts
               if item.path == "report.md"]
    assert [item.version for item in history] == [1, 2, 3]
    assert history[-1].created_by == "editor" and history[-1].run_id is None
    assert first.id in history[-1].source_ids
    assert ledger.capture_artifact(project_id, history[1].id)[1] == b"New draft"
    assert service.apply(project_id, preview.id, **options()) == result
    assert len([item for item in ledger.get_project(project_id).artifacts
                if item.path == "report.md"]) == 3
    # Undoing a recovery is itself reviewed and preserves the intervening state.
    undo = service.preview(project_id, result.safety_checkpoint_id, **options())
    service.apply(project_id, undo.id, **options())
    assert tree_inventory(workspace, LIMITS) == before


def test_retention_only_deletes_expired_reviews_and_unreferenced_points(source):
    ledger, project_id, service, point, _ = source
    def authorize():
        pass
    preview = review(source)
    inventory = service.retention(project_id, authorize=authorize)
    assert inventory.checkpoints[0].referenced_by_reviews == 1
    with pytest.raises(WorkspaceRecoveryError, match="used by a saved review"):
        service.delete_checkpoint(
            project_id, point.id, expected_fingerprint=point.fingerprint, **retention_options(),
        )
    with pytest.raises(WorkspaceRecoveryError, match="Only expired"):
        service.delete_expired_review(
            project_id, preview.id, expected_fingerprint=inventory.reviews[0].fingerprint,
            **retention_options(),
        )
    preview.expires_at = utc_now() - timedelta(seconds=1)
    service._save_restore(preview)
    inventory = service.retention(project_id, authorize=authorize)
    assert inventory.reviews[0].can_delete
    with pytest.raises(WorkspaceRecoveryError, match="changed"):
        service.delete_expired_review(
            project_id, preview.id, expected_fingerprint="0" * 64, **retention_options(),
        )
    service.delete_expired_review(
        project_id, preview.id, expected_fingerprint=inventory.reviews[0].fingerprint,
        **retention_options(),
    )
    inventory = service.retention(project_id, authorize=authorize)
    assert inventory.reviews == [] and inventory.checkpoints[0].referenced_by_reviews == 0
    service.delete_checkpoint(
        project_id, point.id, expected_fingerprint=point.fingerprint, **retention_options(),
    )
    inventory = service.retention(project_id, authorize=authorize)
    assert inventory.checkpoints == [] and inventory.reclaimable_files >= 2
    service.reclaim_snapshots(
        project_id, expected_fingerprint=inventory.reclaim_fingerprint,
        **retention_options(),
    )
    assert service.retention(project_id, authorize=authorize).reclaimable_files == 0
    assert ledger.get_run(point.run_id).id == point.run_id


def test_retained_copy_discard_keeps_safety_checkpoint_and_recovers_interruption(
    source, monkeypatch,
):
    ledger, project_id, service, _, _ = source
    workspace = change_workspace(source)
    preview = review(source)
    applied = service.apply(project_id, preview.id, **options())
    retention = service.retention(project_id, authorize=lambda: None)
    review_item = retention.reviews[0]
    assert retention.retained_bytes == applied.retained_bytes > 0
    retained = workspace.parent / f"workspace-retained-{preview.id}"

    def interrupted(path):
        (path / "report.md").unlink()
        raise OSError("interrupted during deletion")

    with monkeypatch.context() as patch:
        patch.setattr(recovery, "remove_staging", interrupted)
        with pytest.raises(OSError, match="interrupted"):
            service.discard_retained_copy(
                project_id, preview.id, expected_fingerprint=review_item.fingerprint,
                **retention_options(),
            )
    assert (workspace.parent / PENDING_RETENTION).exists()
    assert retained.exists()
    restarted = WorkspaceCheckpoints(FileLedger(ledger.root))
    result = restarted.retention(project_id, authorize=lambda: None)
    assert result.retained_bytes == 0
    assert result.reviews[0].retained_discarded_at is not None
    assert not retained.exists() and not (workspace.parent / PENDING_RETENTION).exists()
    assert restarted.load(project_id, applied.safety_checkpoint_id).fingerprint == (
        applied.current_fingerprint
    )
    undo = restarted.preview(project_id, applied.safety_checkpoint_id, **options())
    restarted.apply(project_id, undo.id, **options())
    assert (workspace / "created.txt").read_bytes() == b"Keep in recovery copy"


def test_applied_review_and_safety_point_cannot_be_pruned(source):
    _, project_id, service, point, _ = source
    change_workspace(source)
    preview = review(source)
    applied = service.apply(project_id, preview.id, **options())
    retention = service.retention(project_id, authorize=lambda: None)
    with pytest.raises(WorkspaceRecoveryError, match="Only expired"):
        service.delete_expired_review(
            project_id, applied.id, expected_fingerprint=retention.reviews[0].fingerprint,
            **retention_options(),
        )
    with pytest.raises(WorkspaceRecoveryError, match="used by a saved review"):
        service.delete_checkpoint(
            project_id, point.id, expected_fingerprint=point.fingerprint, **retention_options(),
        )
    safety = service.load(project_id, applied.safety_checkpoint_id)
    with pytest.raises(WorkspaceRecoveryError, match="used by a saved review"):
        service.delete_checkpoint(
            project_id, safety.id, expected_fingerprint=safety.fingerprint, **retention_options(),
        )


def test_applied_review_archive_preserves_result_and_releases_active_limits(source, monkeypatch):
    ledger, project_id, service, point, _ = source
    change_workspace(source)
    preview = review(source)
    applied = service.apply(project_id, preview.id, **options())
    row = service.retention(project_id, authorize=lambda: None).reviews[0]
    with pytest.raises(WorkspaceRecoveryError, match="Discard the retained folder"):
        service.archive_review(
            project_id, preview.id, expected_fingerprint=row.fingerprint,
            archived_by="admin", **retention_options(),
        )
    service.discard_retained_copy(
        project_id, preview.id, expected_fingerprint=row.fingerprint, **retention_options(),
    )
    row = service.retention(project_id, authorize=lambda: None).reviews[0]
    assert row.can_archive
    def interrupted(*args):
        raise OSError("interrupted after archive write")

    with monkeypatch.context() as patch:
        patch.setattr(service, "_unlink_record", interrupted)
        with pytest.raises(OSError, match="interrupted"):
            service.archive_review(
                project_id, preview.id, expected_fingerprint=row.fingerprint,
                archived_by="admin", **retention_options(),
            )
    assert (ledger._project_path(project_id).parent / PENDING_ARCHIVE).exists()
    restarted = WorkspaceCheckpoints(FileLedger(ledger.root))
    inventory = restarted.retention(project_id, authorize=lambda: None)
    assert inventory.reviews == [] and inventory.archived_reviews == 1
    assert restarted.get_restore(project_id, preview.id).archived_at is not None
    assert restarted.apply(project_id, preview.id, **options()).state == "applied"
    assert not (ledger._project_path(project_id).parent / PENDING_ARCHIVE).exists()
    restarted.delete_checkpoint(
        project_id, point.id, expected_fingerprint=point.fingerprint, **retention_options(),
    )
    safety = restarted.load(project_id, applied.safety_checkpoint_id)
    restarted.delete_checkpoint(
        project_id, safety.id, expected_fingerprint=safety.fingerprint, **retention_options(),
    )
    assert restarted.get_restore(project_id, preview.id).state == "applied"


def test_archived_reviews_are_paged_and_require_current_fingerprint_for_deletion(source):
    ledger, project_id, service, point, first = source
    change_workspace(source)
    preview = review(source)
    service.apply(project_id, preview.id, **options())
    row = service.retention(project_id, authorize=lambda: None).reviews[0]
    service.discard_retained_copy(
        project_id, preview.id, expected_fingerprint=row.fingerprint, **retention_options(),
    )
    row = service.retention(project_id, authorize=lambda: None).reviews[0]
    service.archive_review(
        project_id, preview.id, expected_fingerprint=row.fingerprint,
        archived_by="admin", **retention_options(),
    )
    archived = service.get_restore(project_id, preview.id)
    directory = service._directory(project_id, "workspace-restores-archive")
    for number in range(51):
        item = archived.model_copy(update={"id": f"restore_page_{number:02d}"})
        service._write(directory / f"{item.id}.json", item)
    first_page = service.archived_reviews(project_id, cursor=None, authorize=lambda: None)
    assert len(first_page.reviews) == 50 and first_page.next_cursor
    assert all(item.archived_by == "admin" for item in first_page.reviews)
    second_page = service.archived_reviews(
        project_id, cursor=first_page.next_cursor, authorize=lambda: None,
    )
    assert len(second_page.reviews) == 2 and second_page.next_cursor is None
    assert {item.id for item in first_page.reviews}.isdisjoint(
        item.id for item in second_page.reviews
    )
    target = second_page.reviews[0]
    with pytest.raises(WorkspaceRecoveryError, match="changed"):
        service.delete_archived_review(
            project_id, target.id, expected_fingerprint="0" * 64, **retention_options(),
        )

    def busy():
        raise WorkspaceBusyError("Wait for all project runs to finish before recovering files.")

    with pytest.raises(WorkspaceBusyError, match="Wait for all project runs"):
        service.delete_archived_review(
            project_id, target.id, expected_fingerprint=target.fingerprint,
            authorize=lambda: None, assert_idle=busy,
        )
    service.delete_archived_review(
        project_id, target.id, expected_fingerprint=target.fingerprint,
        **retention_options(),
    )
    with pytest.raises(FileNotFoundError):
        service.get_restore(project_id, target.id)
    assert service.load(project_id, point.id).id == point.id
    assert ledger.capture_artifact(project_id, first.id)[1]
    assert service.retention(project_id, authorize=lambda: None).archived_reviews == 51


def test_snapshot_reclaim_keeps_artifacts_and_recovery_points(source):
    ledger, project_id, service, point, first = source
    root = ledger._project_path(project_id).parent
    orphan = recovery.freeze_artifact(root, b"orphan snapshot")
    inventory = service.retention(project_id, authorize=lambda: None)
    assert inventory.reclaimable_files == 1
    assert inventory.reclaimable_bytes == len(b"orphan snapshot")
    later = recovery.freeze_artifact(root, b"later orphan")
    with pytest.raises(WorkspaceRecoveryError, match="changed"):
        service.reclaim_snapshots(
            project_id, expected_fingerprint=inventory.reclaim_fingerprint,
            **retention_options(),
        )
    inventory = service.retention(project_id, authorize=lambda: None)
    service.reclaim_snapshots(
        project_id, expected_fingerprint=inventory.reclaim_fingerprint,
        **retention_options(),
    )
    assert not (root / snapshot_relative(orphan)).exists()
    assert not (root / snapshot_relative(later)).exists()
    assert service.retention(project_id, authorize=lambda: None).reclaimable_files == 0
    assert ledger.capture_artifact(project_id, first.id)[1]
    assert service.load(project_id, point.id).fingerprint == point.fingerprint


def test_file_directory_replacement_and_hidden_files_are_included(source):
    ledger, project_id, service, _, _ = source
    workspace = change_workspace(source)
    (workspace / "report.md").unlink()
    (workspace / "report.md").mkdir()
    (workspace / "report.md/.hidden").write_bytes(b"Keep this too")
    preview = review(source)
    change = next(item for item in preview.changes if item.path == "report.md")
    assert change.before_kind == "directory" and change.after_kind == "file"
    result = service.apply(project_id, preview.id, **options())
    assert (workspace / "report.md").is_file()
    assert (workspace.parent / f"workspace-retained-{result.id}/report.md/.hidden").read_bytes()


@pytest.mark.parametrize("mutation", ["bytes", "new_file", "mode"])
def test_stale_preview_preserves_every_file(source, mutation):
    ledger, project_id, service, *_ = source
    workspace = change_workspace(source)
    preview = review(source)
    if mutation == "bytes":
        (workspace / "report.md").write_bytes(b"Later user changes")
    elif mutation == "new_file":
        (workspace / "later").touch()
    else:
        (workspace / "report.md").chmod(0o400)
    before = tree_inventory(workspace, LIMITS)
    with pytest.raises(WorkspaceRecoveryError, match="changed after review"):
        service.apply(project_id, preview.id, **options())
    assert tree_inventory(workspace, LIMITS) == before
    assert not (workspace.parent / PENDING_RESTORE).exists()


@pytest.mark.parametrize("changes", [{"created_by": "other"}, {"principal_session_id": "other"}])
def test_preview_cannot_be_applied_by_another_reviewer_or_session(source, changes):
    _, project_id, service, *_ = source
    preview = review(source)
    with pytest.raises(WorkspaceRecoveryError, match="Only the reviewer"):
        service.apply(project_id, preview.id, **options(**changes))


def test_expired_review_and_active_execution_lease_are_rejected(source):
    ledger, project_id, service, *_ = source
    preview = review(source)
    preview.expires_at = utc_now() - timedelta(seconds=1)
    service._save_restore(preview)
    with pytest.raises(WorkspaceRecoveryError, match="expired"):
        service.apply(project_id, preview.id, **options())
    with workspace_lease(ledger, project_id), pytest.raises(WorkspaceBusyError):
        review(source)


@pytest.mark.parametrize("failure", ["stage", "intent_before", "intent_after", "first_swap",
                                     "second_swap", "artifact", "applied"])
def test_interruption_retry_never_loses_newer_files_or_duplicates_versions(
    source, monkeypatch, failure,
):
    ledger, project_id, service, point, _ = source
    workspace = change_workspace(source)
    preview = review(source)
    original_write, original_swap = ledger._atomic_write, recovery.publish_exclusive
    original_materialize = service._materialize

    def write(path, value):
        if path.name == PENDING_RESTORE and failure in {"intent_before", "intent_after"}:
            if failure == "intent_after":
                original_write(path, value)
            raise OSError("power failure")
        if failure == "artifact" and path.name == "project.json":
            raise OSError("power failure")
        original_write(path, value)
        if failure == "applied" and getattr(value, "state", None) == "applied":
            raise OSError("power failure")

    def swap(src, dst):
        original_swap(src, dst)
        if ((failure == "first_swap" and dst.name.startswith("workspace-retained"))
                or (failure == "second_swap" and dst.name == "workspace")):
            raise OSError("power failure")

    def materialize(root, target, stage):
        if failure == "stage":
            stage.mkdir()
            (stage / "partial").write_bytes(b"partial staged file")
            raise OSError("power failure")
        original_materialize(root, target, stage)

    monkeypatch.setattr(ledger, "_atomic_write", write)
    monkeypatch.setattr(recovery, "publish_exclusive", swap)
    monkeypatch.setattr(service, "_materialize", materialize)
    with pytest.raises(OSError):
        service.apply(project_id, preview.id, **options())
    monkeypatch.undo()
    restarted = FileLedger(ledger.root)
    retry = WorkspaceCheckpoints(restarted)
    # Access must reconcile a missing workspace, never mkdir an empty replacement.
    restarted.workspace_for_project(project_id)
    result = retry.apply(project_id, preview.id, **options())
    assert tree_inventory(workspace, LIMITS) == point.inventory()
    assert (workspace.parent / f"workspace-retained-{result.id}/created.txt").read_bytes()
    assert not (workspace.parent / PENDING_RESTORE).exists()
    assert len([item for item in restarted.get_project(project_id).artifacts
                if item.path == "report.md"]) == 3


def test_revocation_while_staging_never_swaps_the_workspace(source, monkeypatch):
    _, project_id, service, *_ = source
    workspace = change_workspace(source)
    preview = review(source)
    original = service._materialize
    allowed = True

    def materialize(*args):
        nonlocal allowed
        original(*args)
        allowed = False

    def authorize():
        if not allowed:
            raise PermissionError("revoked")

    monkeypatch.setattr(service, "_materialize", materialize)
    before = tree_inventory(workspace, LIMITS)
    with pytest.raises(PermissionError):
        service.apply(project_id, preview.id, **options(authorize=authorize))
    assert tree_inventory(workspace, LIMITS) == before
    assert not list(workspace.parent.glob("workspace-stage-*"))


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo"])
def test_linked_and_special_files_fail_closed_before_a_run(source, kind):
    ledger, project_id, service, *_ = source
    workspace = ledger.workspace_for_project(project_id)
    path = workspace / "unsafe"
    if kind == "symlink":
        path.symlink_to(workspace / "report.md")
    elif kind == "hardlink":
        os.link(workspace / "report.md", path)
    else:
        os.mkfifo(path)
    run = ledger.create_run(project_id, "Do not start")
    with pytest.raises((BackupError, ValueError), match="linked|private"):
        service.prepare_run(run.id)
    assert not (workspace.parent / f"workspace-checkpoints/before-{run.id}.json").exists()


@pytest.mark.parametrize("bad_path", ["../outside", "/absolute", "dir/../report.md"])
def test_tampered_checkpoint_paths_cannot_escape_the_workspace(source, bad_path):
    ledger, project_id, service, point, _ = source
    path = ledger._project_path(project_id).parent / f"workspace-checkpoints/{point.id}.json"
    value = json.loads(path.read_bytes())
    value["files"][0]["path"] = bad_path
    value["fingerprint"] = fingerprint({key: value[key] for key in ("files", "directories")})
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        service.load(project_id, point.id)


def test_corrupt_snapshot_and_mismatched_run_origin_cannot_be_restored(source):
    ledger, project_id, service, point, _ = source
    workspace = change_workspace(source)
    preview = review(source)
    blob = workspace.parent / snapshot_relative(point.files[0].sha256)
    blob.chmod(0o600)
    blob.write_bytes(b"bad bytes")
    before = tree_inventory(workspace, LIMITS)
    with pytest.raises(ValueError, match="integrity|corrupt"):
        service.apply(project_id, preview.id, **options())
    assert tree_inventory(workspace, LIMITS) == before
    other = ledger.create_project(ProjectCreate(name="Other", objective="Different scope"))
    ledger.update_run(point.run_id, project_id=other.id)
    with pytest.raises(WorkspaceRecoveryError, match="Run origin"):
        service.load(project_id, point.id)


def test_repeated_and_legacy_resumes_do_not_invent_a_new_original_state(source):
    ledger, project_id, service, point, _ = source
    change_workspace(source)
    service.prepare_run(point.run_id)
    assert service.load(project_id, point.id) == point
    legacy = ledger.create_run(project_id, "Legacy resumed run")
    ledger.update_run(legacy.id, started_at=utc_now())
    service.prepare_run(legacy.id)
    assert len(service.list(project_id)) == 1


def test_snapshot_limits_cancel_before_publication_and_never_truncate_files(source, monkeypatch):
    ledger, project_id, service, *_ = source
    run = ledger.create_run(project_id, "bounded")
    monkeypatch.setattr(recovery, "MAX_CHECKPOINTS", 1)
    with pytest.raises(WorkspaceRecoveryError, match="limit"):
        service.prepare_run(run.id)
    monkeypatch.undo()
    with pytest.raises(recovery.WorkspaceCaptureCancelled):
        service.prepare_run(run.id, lambda: True)
    assert len(service.list(project_id)) == 1
    assert hashlib.sha256(b"New draft").hexdigest() not in str(service.list(project_id))


def test_completed_intent_cleanup_preserves_later_manual_changes(source, monkeypatch):
    ledger, project_id, service, *_ = source
    workspace = change_workspace(source)
    preview = review(source)
    original = service._clear_pending

    def interrupted(_root):
        raise OSError("response lost after completion")

    monkeypatch.setattr(service, "_clear_pending", interrupted)
    with pytest.raises(OSError):
        service.apply(project_id, preview.id, **options())
    assert service.get_restore(project_id, preview.id).state == "applied"
    (workspace / "report.md").write_bytes(b"Subsequent manual work")
    monkeypatch.setattr(service, "_clear_pending", original)
    ledger.workspace_for_project(project_id)
    assert (workspace / "report.md").read_bytes() == b"Subsequent manual work"
    assert not (workspace.parent / PENDING_RESTORE).exists()


def test_pending_text_edit_is_registered_before_workspace_review(source, monkeypatch):
    from ophira_backend.artifact_edits import ArtifactTextUpdate, save_artifact_text

    ledger, project_id, service, _, first = source
    original = ledger._atomic_write

    def interrupted(path, value):
        if path.name == "project.json":
            raise OSError("edit committed but record absent")
        original(path, value)

    monkeypatch.setattr(ledger, "_atomic_write", interrupted)
    with pytest.raises(OSError):
        save_artifact_text(
            ledger, project_id, first.id,
            ArtifactTextUpdate(
                content="Reviewed user draft", expected_checksum=first.checksum,
                request_id="edit_1",
            ), created_by="editor", authorize=lambda: None, is_executing=lambda _: False,
        )
    monkeypatch.undo()
    preview = review(source)
    edited = next(item for item in ledger.get_project(project_id).artifacts
                  if item.edit_request_id == "edit_1")
    service.apply(project_id, preview.id, **options())
    assert ledger.capture_artifact(project_id, edited.id)[1] == b"Reviewed user draft"
    assert len([item for item in ledger.get_project(project_id).artifacts
                if item.path == first.path]) == 3


def test_revocation_during_final_tree_check_cannot_publish_swap_intent(source, monkeypatch):
    _, project_id, service, *_ = source
    workspace = change_workspace(source)
    preview = review(source)
    original = recovery.tree_inventory
    workspace_reads = 0
    allowed = True

    def inventory(path, *args, **kwargs):
        nonlocal workspace_reads, allowed
        value = original(path, *args, **kwargs)
        if path == workspace:
            workspace_reads += 1
            # Initial check, safety capture + verification, final pre-swap read.
            if workspace_reads == 4:
                allowed = False
        return value

    def authorize():
        if not allowed:
            raise PermissionError("revoked during final read")

    monkeypatch.setattr(recovery, "tree_inventory", inventory)
    with pytest.raises(PermissionError):
        service.apply(project_id, preview.id, **options(authorize=authorize))
    assert workspace_reads == 4
    assert (workspace / "report.md").read_bytes() == b"New draft"
    assert not (workspace.parent / PENDING_RESTORE).exists()
