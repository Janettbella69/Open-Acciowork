"""Recovery after actual process death at the directory publication boundaries."""
# ruff: noqa: F811 -- imported pytest fixture

import multiprocessing
import os
from pathlib import Path

import pytest
from test_workspace_checkpoints import change_workspace, options, review, source  # noqa: F401

from ophira_backend import workspace_checkpoints as recovery
from ophira_backend.backup_files import tree_inventory
from ophira_backend.storage import FileLedger
from ophira_backend.workspace_checkpoints import LIMITS, PENDING_RESTORE, WorkspaceCheckpoints


def die(root, project_id, restore_id, boundary):
    ledger = FileLedger(Path(root))
    service = WorkspaceCheckpoints(ledger)
    original_swap, original_write = recovery.publish_exclusive, ledger._atomic_write

    def swap(src, dst):
        original_swap(src, dst)
        if ((boundary == "first_swap" and dst.name.startswith("workspace-retained"))
                or (boundary == "second_swap" and dst.name == "workspace")):
            os._exit(73)

    def write(path, value):
        original_write(path, value)
        if ((boundary == "intent" and path.name == PENDING_RESTORE)
                or (boundary == "artifact" and path.name == "project.json")
                or (boundary == "applied" and getattr(value, "state", None) == "applied")):
            os._exit(73)

    recovery.publish_exclusive = swap
    ledger._atomic_write = write
    service.apply(project_id, restore_id, **options())


@pytest.mark.parametrize("boundary", ["intent", "first_swap", "second_swap", "artifact", "applied"])
def test_restart_finishes_authorized_swap_and_keeps_both_trees(source, boundary):
    ledger, project_id, _, point, _ = source
    workspace = change_workspace(source)
    before = tree_inventory(workspace, LIMITS)
    preview = review(source)
    process = multiprocessing.get_context("spawn").Process(
        target=die, args=(str(ledger.root), project_id, preview.id, boundary),
    )
    process.start()
    try:
        process.join(15)
        assert process.exitcode == 73
        assert (workspace.parent / PENDING_RESTORE).is_file()
        restarted = FileLedger(ledger.root)
        restarted.create_run(project_id, "A subsequent run reconciles before publishing itself")
        assert tree_inventory(workspace, LIMITS) == point.inventory()
        result = WorkspaceCheckpoints(restarted).get_restore(project_id, preview.id)
        assert result.state == "applied"
        retained = workspace.parent / f"workspace-retained-{result.id}"
        assert tree_inventory(retained, LIMITS) == before
        assert len([item for item in restarted.get_project(project_id).artifacts
                    if item.path == "report.md"]) == 3
    finally:
        if process.is_alive():
            process.kill()
            process.join(5)


def test_unknown_workspace_created_after_first_swap_is_never_overwritten(source):
    ledger, project_id, _, _, _ = source
    workspace = change_workspace(source)
    preview = review(source)
    process = multiprocessing.get_context("spawn").Process(
        target=die, args=(str(ledger.root), project_id, preview.id, "first_swap"),
    )
    process.start()
    try:
        process.join(15)
        assert process.exitcode == 73
        workspace.mkdir()
        (workspace / "unknown").write_text("Unrelated user work")
        with pytest.raises(recovery.WorkspaceRecoveryError, match="inconsistent"):
            FileLedger(ledger.root).workspace_for_project(project_id)
        assert (workspace / "unknown").read_text() == "Unrelated user work"
        assert (workspace.parent / f"workspace-retained-{preview.id}/created.txt").is_file()
    finally:
        if process.is_alive():
            process.kill()
            process.join(5)
