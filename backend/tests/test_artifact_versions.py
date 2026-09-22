from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor

import pytest

from ophira_backend import artifact_files
from ophira_backend.models import ProjectCreate
from ophira_backend.storage import FileLedger


@pytest.fixture
def source(tmp_path):
    ledger = FileLedger(tmp_path / "data")
    project = ledger.create_project(ProjectCreate(name="Artifacts", objective="Keep versions"))
    return ledger, project.id


def test_each_version_downloads_its_original_bytes_after_edit_and_deletion(source):
    ledger, project_id = source
    first = ledger.save_upload(project_id, "report.md", b"First report")
    path = ledger.workspace_for_project(project_id) / first.path
    path.write_bytes(b"Second report")
    second = ledger.register_artifact(project_id, first.path)
    path.unlink()
    restarted = FileLedger(ledger.root)
    assert restarted.capture_artifact(project_id, first.id)[1] == b"First report"
    assert restarted.capture_artifact(project_id, second.id)[1] == b"Second report"
    assert second.version == 2 and second.parent_artifact_id == first.id


def test_reverting_content_creates_a_new_version_but_unchanged_registration_is_idempotent(source):
    ledger, project_id = source
    first = ledger.save_upload(project_id, "report.txt", b"A")
    path = ledger.workspace_for_project(project_id) / first.path
    path.write_bytes(b"B")
    second = ledger.register_artifact(project_id, first.path)
    path.write_bytes(b"A")
    third = ledger.register_artifact(project_id, first.path)
    assert [first.version, second.version, third.version] == [1, 2, 3]
    assert third.parent_artifact_id == second.id
    assert ledger.register_artifact(project_id, first.path).id == third.id


def test_concurrent_registration_has_one_durable_version(source):
    ledger, project_id = source
    (ledger.workspace_for_project(project_id) / "report.txt").write_bytes(b"A")
    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(
            lambda _: FileLedger(ledger.root).register_artifact(project_id, "report.txt"),
            range(8),
        ))
    assert len({row.id for row in rows}) == 1
    assert len(ledger.get_project(project_id).artifacts) == 1


@pytest.mark.parametrize("attack", ["symlink", "parent_symlink", "hardlink", "fifo"])
def test_registration_never_captures_links_or_special_files(source, tmp_path, attack):
    ledger, project_id = source
    workspace = ledger.workspace_for_project(project_id)
    external = tmp_path / "private.txt"
    external.write_text("private-canary")
    if attack == "symlink":
        (workspace / "file.txt").symlink_to(external)
    elif attack == "parent_symlink":
        (workspace / "linked").symlink_to(tmp_path, target_is_directory=True)
    elif attack == "hardlink":
        os.link(external, workspace / "file.txt")
    else:
        os.mkfifo(workspace / "file.txt")
    relative = "linked/private.txt" if attack == "parent_symlink" else "file.txt"
    with pytest.raises((OSError, ValueError)):
        ledger.register_artifact(project_id, relative)
    assert not ledger.get_project(project_id).artifacts


def test_legacy_records_never_download_newer_bytes_and_can_be_pinned_if_unchanged(source):
    ledger, project_id = source
    artifact = ledger.save_upload(project_id, "legacy.txt", b"original")

    def make_legacy(project):
        project.artifacts[0].content_storage = "workspace"

    ledger.update_project(project_id, make_legacy)
    assert ledger.capture_artifact(project_id, artifact.id)[1] == b"original"
    source_path = ledger.workspace_for_project(project_id) / artifact.path
    source_path.write_bytes(b"updated")
    with pytest.raises(ValueError, match="changed"):
        ledger.capture_artifact(project_id, artifact.id)
    source_path.write_bytes(b"original")
    pinned = ledger.register_artifact(project_id, artifact.path)
    assert pinned.id == artifact.id and pinned.content_storage == "snapshot"
    source_path.unlink()
    assert ledger.capture_artifact(project_id, artifact.id)[1] == b"original"


def test_corrupt_or_missing_snapshot_does_not_fall_back_to_working_file(source):
    ledger, project_id = source
    artifact = ledger.save_upload(project_id, "report.md", b"report")
    root = ledger._project_path(project_id).parent
    snapshot = root / artifact_files.snapshot_relative(artifact.checksum)
    snapshot.chmod(0o600)
    snapshot.write_bytes(b"corrupt")
    with pytest.raises(ValueError):
        ledger.capture_artifact(project_id, artifact.id)
    snapshot.unlink()
    with pytest.raises(FileNotFoundError):
        ledger.capture_artifact(project_id, artifact.id)


def test_history_quota_refuses_new_version_without_losing_old_copy(source, monkeypatch):
    ledger, project_id = source
    artifact = ledger.save_upload(project_id, "report.txt", b"abc")
    monkeypatch.setattr(artifact_files, "MAX_ARTIFACT_HISTORY_BYTES", 5)
    path = ledger.workspace_for_project(project_id) / artifact.path
    path.write_bytes(b"def")
    with pytest.raises(ValueError, match="byte limit"):
        ledger.register_artifact(project_id, artifact.path)
    assert len(ledger.get_project(project_id).artifacts) == 1
    assert ledger.capture_artifact(project_id, artifact.id)[1] == b"abc"


def test_registration_interruption_never_commits_a_version_without_its_bytes(source, monkeypatch):
    ledger, project_id = source
    path = ledger.workspace_for_project(project_id) / "report.txt"
    path.write_bytes(b"pending")

    def interrupt(*args):
        raise OSError("project commit failed")

    monkeypatch.setattr(ledger, "update_project", interrupt)
    with pytest.raises(OSError):
        ledger.register_artifact(project_id, "report.txt")
    restarted = FileLedger(ledger.root)
    assert not restarted.get_project(project_id).artifacts
    artifact = restarted.register_artifact(project_id, "report.txt")
    assert artifact.version == 1
    assert restarted.capture_artifact(project_id, artifact.id)[1] == b"pending"
