from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor

import pytest

from ophira_backend import artifact_edits
from ophira_backend.artifact_edits import (
    ArtifactEditConflict,
    ArtifactTextUpdate,
    read_artifact_text,
    recover_pending_edit,
    save_artifact_text,
)
from ophira_backend.constants import RunStatus
from ophira_backend.models import ProjectCreate
from ophira_backend.storage import FileLedger


@pytest.fixture
def source(tmp_path):
    ledger = FileLedger(tmp_path / "data")
    project = ledger.create_project(ProjectCreate(name="Edit", objective="Keep user changes"))
    artifact = ledger.save_upload(project.id, "report.md", b"Initial report\r\n")
    return ledger, project.id, artifact


def save(source, text="Updated report", request_id="edit_1", **kwargs):
    ledger, project_id, artifact = source
    return save_artifact_text(
        ledger, project_id, artifact.id,
        ArtifactTextUpdate(
            content=text, expected_checksum=artifact.checksum, request_id=request_id,
        ), created_by="editor", authorize=kwargs.get("authorize", lambda: None),
        is_executing=kwargs.get("is_executing", lambda _: False),
    )


def test_save_changes_workspace_and_keeps_exact_history_and_human_provenance(source):
    ledger, project_id, first = source
    second = save(source, "修改后的报告\r\n")
    assert second.version == 2 and second.run_id is None
    assert second.created_by == "editor" and second.parent_artifact_id == first.id
    assert first.id in second.source_ids
    assert ledger.capture_artifact(project_id, first.id)[1] == b"Initial report\r\n"
    assert ledger.capture_artifact(project_id, second.id)[1].decode() == "修改后的报告\r\n"
    assert (ledger.workspace_for_project(project_id) / first.path).read_bytes() == (
        "修改后的报告\r\n".encode()
    )
    assert not ledger.get_project(project_id).evidence
    historical = read_artifact_text(
        ledger, project_id, first.id, authorize=lambda: None, is_executing=lambda _: False,
    )
    assert not historical.editable and historical.latest_artifact_id == second.id


def test_retry_after_lost_response_returns_same_version_and_key_cannot_be_reused(source):
    first_save = save(source)
    assert save(source).id == first_save.id
    with pytest.raises(ArtifactEditConflict, match="different content"):
        save(source, "another draft")
    assert len(source[0].get_project(source[1]).artifacts) == 2


def test_stale_saved_version_and_unregistered_working_edit_are_never_overwritten(source):
    ledger, project_id, first = source
    path = ledger.workspace_for_project(project_id) / first.path
    path.write_bytes(b"Someone else's changes")
    with pytest.raises(ArtifactEditConflict, match="working file changed"):
        save(source)
    current = ledger.register_artifact(project_id, first.path)
    with pytest.raises(ArtifactEditConflict, match="newer version"):
        save(source)
    assert path.read_bytes() == b"Someone else's changes"
    assert ledger.capture_artifact(project_id, current.id)[1] == path.read_bytes()


@pytest.mark.parametrize("status", [RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.PAUSED])
def test_runs_prevent_editing_even_when_owned_by_another_worker(source, status):
    ledger, project_id, _ = source
    run = ledger.create_run(project_id, "Work")
    ledger.update_run(run.id, status=status)
    with pytest.raises(ArtifactEditConflict, match="runs to finish"):
        save(source)


def test_terminal_run_cleanup_also_prevents_editing(source):
    ledger, project_id, _ = source
    run = ledger.create_run(project_id, "Work")
    ledger.update_run(run.id, status=RunStatus.COMPLETED)
    with pytest.raises(ArtifactEditConflict, match="runs to finish"):
        save(source, is_executing=lambda _: True)


def test_two_concurrent_drafts_have_one_winner(source):
    ledger, project_id, first = source

    def attempt(number):
        try:
            return save(
                (FileLedger(ledger.root), project_id, first), f"Draft {number}", f"edit_{number}",
            )
        except ArtifactEditConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, [1, 2]))
    assert sum(item is not None for item in outcomes) == 1
    assert len(ledger.get_project(project_id).artifacts) == 2


@pytest.mark.parametrize("failure", ["before_swap", "after_swap", "after_commit"])
def test_crash_reconciliation_observes_file_state_without_replaying_writes(
    source, monkeypatch, failure,
):
    ledger, project_id, first = source
    original_write = ledger._atomic_write
    original_replace = os.replace

    def interrupt_write(path, model):
        if path.name == "project.json" and model.artifacts[-1].version == 2:
            if failure == "after_commit":
                original_write(path, model)
            raise OSError("interrupted after workspace swap")
        return original_write(path, model)

    def interrupt_replace(src, dst, **kwargs):
        if str(src).startswith(".artifact-edit-"):
            raise OSError("interrupted before workspace swap")
        return original_replace(src, dst, **kwargs)

    if failure == "before_swap":
        monkeypatch.setattr(artifact_edits.os, "replace", interrupt_replace)
    else:
        monkeypatch.setattr(ledger, "_atomic_write", interrupt_write)
    with pytest.raises(OSError):
        save(source)
    monkeypatch.undo()
    restarted = FileLedger(ledger.root)
    with restarted._lock_for(project_id):
        recover_pending_edit(restarted, project_id)
    rows = restarted.get_project(project_id).artifacts
    assert len(rows) == (1 if failure == "before_swap" else 2)
    assert restarted.capture_artifact(project_id, first.id)[1] == b"Initial report\r\n"
    retried = save((restarted, project_id, first))
    assert retried.version == 2
    assert len(restarted.get_project(project_id).artifacts) == 2


def test_revocation_during_staging_preserves_file_and_snapshot(source):
    ledger, project_id, first = source
    calls = 0

    def authorize():
        nonlocal calls
        calls += 1
        if calls >= 3:
            raise PermissionError("revoked")

    with pytest.raises(PermissionError):
        save(source, authorize=authorize)
    assert (ledger.workspace_for_project(project_id) / first.path).read_bytes() == (
        b"Initial report\r\n"
    )
    assert len(ledger.get_project(project_id).artifacts) == 1


@pytest.mark.parametrize("text", ["bad\x00text", "中" * 200_000])
def test_binary_and_over_byte_limit_edits_are_rejected(source, text):
    with pytest.raises(ValueError):
        save(source, text)
    assert len(source[0].get_project(source[1]).artifacts) == 1


def test_unchanged_save_keeps_original_bytes_and_version(source):
    assert save(source, "Initial report\r\n").id == source[2].id
