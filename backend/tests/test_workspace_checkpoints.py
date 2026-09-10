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
    PENDING_RESTORE,
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
