"""Actual process termination and competing writers on the shared file ledger."""

import multiprocessing
import os
from pathlib import Path

import pytest

from ophira_backend.artifact_edits import ArtifactTextUpdate, save_artifact_text
from ophira_backend.models import ProjectCreate
from ophira_backend.storage import FileLedger


def crash_after_swap(root, project_id, artifact_id, checksum):
    ledger = FileLedger(Path(root))
    original = ledger._atomic_write

    def crash(path, model):
        if path.name == "project.json" and model.artifacts[-1].version == 2:
            os._exit(73)
        return original(path, model)

    ledger._atomic_write = crash
    save_artifact_text(
        ledger, project_id, artifact_id,
        ArtifactTextUpdate(content="New report", expected_checksum=checksum, request_id="edit_1"),
        created_by="editor", authorize=lambda: None, is_executing=lambda _: False,
    )


def register_together(root, project_id, ready, start, result):
    try:
        ledger = FileLedger(Path(root))
        ready.put("ready")
        if not start.wait(15):
            raise TimeoutError("start signal missing")
        result.put(ledger.register_artifact(project_id, "report.txt").id)
    except BaseException as exc:
        result.put(type(exc).__name__)
        raise


def test_process_dies_after_file_swap_and_retry_recovers_one_version(tmp_path):
    ledger = FileLedger(tmp_path / "data")
    project = ledger.create_project(ProjectCreate(name="Crash", objective="Keep edits"))
    first = ledger.save_upload(project.id, "report.txt", b"Original report")
    process = multiprocessing.get_context("spawn").Process(
        target=crash_after_swap, args=(str(ledger.root), project.id, first.id, first.checksum),
    )
    process.start()
    try:
        process.join(15)
        assert process.exitcode == 73
        assert len(ledger.get_project(project.id).artifacts) == 1
        assert (ledger.workspace_for_project(project.id) / first.path).read_bytes() == b"New report"
        restarted = FileLedger(ledger.root)
        edited = save_artifact_text(
            restarted, project.id, first.id,
            ArtifactTextUpdate(
                content="New report", expected_checksum=first.checksum, request_id="edit_1",
            ), created_by="editor", authorize=lambda: None, is_executing=lambda _: False,
        )
        assert edited.version == 2 and edited.created_by == "editor"
        assert len(restarted.get_project(project.id).artifacts) == 2
        assert restarted.capture_artifact(project.id, first.id)[1] == b"Original report"
        assert restarted.capture_artifact(project.id, edited.id)[1] == b"New report"
    finally:
        if process.is_alive():
            process.kill()
            process.join(5)


@pytest.mark.parametrize("workers", [2, 4])
def test_processes_racing_to_register_share_one_version(tmp_path, workers):
    ledger = FileLedger(tmp_path / "data")
    project = ledger.create_project(ProjectCreate(name="Race", objective="One version"))
    (ledger.workspace_for_project(project.id) / "report.txt").write_text("Captured once")
    context = multiprocessing.get_context("spawn")
    ready, result, start = context.Queue(), context.Queue(), context.Event()
    processes = [context.Process(
        target=register_together,
        args=(str(ledger.root), project.id, ready, start, result),
    ) for _ in range(workers)]
    try:
        for process in processes:
            process.start()
        for _ in processes:
            assert ready.get(timeout=15) == "ready"
        start.set()
        identifiers = [result.get(timeout=15) for _ in processes]
        for process in processes:
            process.join(15)
            assert process.exitcode == 0
        assert len(set(identifiers)) == 1 and identifiers[0].startswith("artifact_")
        assert len(ledger.get_project(project.id).artifacts) == 1
    finally:
        for process in processes:
            if process.is_alive():
                process.kill()
                process.join(5)
        ready.close()
        result.close()
