"""Project deletion gates must survive API workers and real process termination."""

import asyncio
import multiprocessing
import os

import pytest

from ophira_backend.project_lifecycle import (
    ProjectLifecycle,
    ProjectLifecycleBusyError,
    ProjectUnavailableError,
)


def intent(project_id="project_a", status="deleting"):
    return {
        "schema_version": 1, "project_id": project_id, "operation_id": "operation_a",
        "status": status, "plan": {},
    }


def hold_operation(root, ready, finish, delete):
    lifecycle = ProjectLifecycle(root)
    with lifecycle.lease("project_a", exclusive=delete):
        if delete:
            lifecycle.write_state("project_a", intent())
        ready.set()
        assert finish.wait(20)


def test_reentrant_leases_are_shared_between_instances_but_never_upgrade(tmp_path):
    first, second = ProjectLifecycle(tmp_path), ProjectLifecycle(tmp_path)
    with first.lease("project_a"), second.lease("project_a"):
        with pytest.raises(ProjectLifecycleBusyError):
            second.lease("project_a", exclusive=True).__enter__()
    with second.lease("project_a", exclusive=True), first.lease("project_a"):
        second.write_state("project_a", intent())
        first.assert_available("project_a")  # Private cleanup is inside the owning operation.
    with pytest.raises(ProjectUnavailableError), first.lease("project_a"):
        pass
    with second.lease("project_b", exclusive=True):
        pass


@pytest.mark.asyncio
async def test_child_async_task_cannot_inherit_another_tasks_exclusive_ownership(tmp_path):
    lifecycle = ProjectLifecycle(tmp_path)

    async def enter():
        with pytest.raises(ProjectLifecycleBusyError), lifecycle.lease("project_a"):
            pass

    with lifecycle.lease("project_a", exclusive=True):
        await asyncio.create_task(enter())
        await asyncio.to_thread(lambda: pytest.raises(
            ProjectLifecycleBusyError, lambda: lifecycle.lease("project_a").__enter__(),
        ))


@pytest.mark.parametrize("delete", [False, True])
def test_real_process_lease_excludes_deletion_and_crash_keeps_durable_intent(tmp_path, delete):
    context = multiprocessing.get_context("spawn")
    ready, finish = context.Event(), context.Event()
    process = context.Process(target=hold_operation, args=(tmp_path, ready, finish, delete))
    process.start()
    try:
        assert ready.wait(10)
        lifecycle = ProjectLifecycle(tmp_path)
        with pytest.raises(ProjectLifecycleBusyError), lifecycle.lease(
            "project_a", exclusive=True,
        ):
            pass
        with lifecycle.lease("project_b", exclusive=True):
            pass
        process.kill()
        process.join(10)
        assert not process.is_alive()
        recovered = ProjectLifecycle(tmp_path)
        if delete:
            assert recovered.read_state("project_a") == intent()
            with pytest.raises(ProjectUnavailableError), recovered.lease("project_a"):
                pass
            with recovered.lease("project_a", exclusive=True, allow_deleted=True):
                recovered.write_state("project_a", intent(status="deleted"))
            assert recovered.pending_ids() == []
        else:
            with recovered.lease("project_a", exclusive=True):
                pass
    finally:
        if process.is_alive():
            process.kill()
            process.join(5)


def test_state_requires_exclusive_ownership_and_cannot_be_replaced_or_undone(tmp_path):
    lifecycle = ProjectLifecycle(tmp_path)
    with pytest.raises(RuntimeError):
        lifecycle.write_state("project_a", intent())
    with lifecycle.lease("project_a", exclusive=True):
        lifecycle.write_state("project_a", intent())
        with pytest.raises(ValueError):
            lifecycle.write_state("project_a", {**intent(), "operation_id": "different"})
        lifecycle.write_state("project_a", intent(status="deleted"))
        with pytest.raises(ValueError):
            lifecycle.write_state("project_a", intent())
    assert lifecycle.read_state("project_a")["status"] == "deleted"
    with pytest.raises(ProjectUnavailableError), lifecycle.lease("project_a", exclusive=True):
        pass


def test_an_empty_directory_before_intent_commit_is_not_a_tombstone(tmp_path):
    directory = tmp_path / "project-deletions" / "project_a"
    directory.mkdir(parents=True)
    (directory / ".state-abandoned.tmp").write_text("partial intent")
    lifecycle = ProjectLifecycle(tmp_path)
    assert lifecycle.read_state("project_a") is None
    assert lifecycle.pending_ids() == []
    with lifecycle.lease("project_a"):
        pass


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "invalid"])
def test_unsafe_or_corrupt_intent_fails_closed_without_reading_foreign_content(tmp_path, kind):
    foreign = tmp_path / "foreign.json"
    foreign.write_text('{"private":"unrelated"}')
    directory = tmp_path / "project-deletions" / "project_a"
    directory.mkdir(parents=True)
    path = directory / "state.json"
    if kind == "symlink":
        path.symlink_to(foreign)
    elif kind == "hardlink":
        os.link(foreign, path)
    else:
        path.write_text("bad json")
    lifecycle = ProjectLifecycle(tmp_path)
    with pytest.raises(ProjectUnavailableError), lifecycle.lease("project_a"):
        pass
    with pytest.raises((ValueError, OSError)):
        lifecycle.read_state("project_a")
    assert foreign.read_text() == '{"private":"unrelated"}'


def test_failed_multi_project_acquisition_releases_all_preceding_leases(tmp_path):
    lifecycle = ProjectLifecycle(tmp_path)
    with lifecycle.lease("project_z"):
        with pytest.raises(ProjectLifecycleBusyError), lifecycle.exclusive_many([
            "project_a", "project_z",
        ]):
            pass
    with lifecycle.exclusive_many(["project_z", "project_a", "project_a"]):
        lifecycle.write_state("project_a", intent())


def test_ledger_cannot_read_write_recover_or_recreate_a_deleted_project(tmp_path):
    from ophira_backend.models import ProjectCreate
    from ophira_backend.storage import FileLedger, LedgerNotFoundError

    ledger = FileLedger(tmp_path)
    project = ledger.create_project(ProjectCreate(name="Private project", objective="Private"))
    run = ledger.create_run(project.id, "Private run")
    ledger.save_upload(project.id, "before.txt", b"Before deletion")
    with ledger.lifecycle.lease(project.id, exclusive=True):
        ledger.lifecycle.write_state(project.id, intent(project.id))
    assert ledger.list_projects() == []
    assert ledger.list_runs() == []
    for operation in (
        lambda: ledger.get_project(project.id), lambda: ledger.get_run(run.id),
        lambda: ledger.get_run_summary(run.id), lambda: ledger.list_events(run.id),
        lambda: ledger.workspace_for_project(project.id),
        lambda: ledger.save_upload(project.id, "after.txt", b"Must not be written"),
        lambda: ledger.update_project(project.id, lambda _project: None),
        lambda: ledger.update_run(run.id, prompt="Must not be written"),
        lambda: ledger.create_run(project.id, "Must not start"),
    ):
        with pytest.raises(LedgerNotFoundError):
            operation()
    with pytest.raises(ProjectUnavailableError):
        ledger.create_project(ProjectCreate(name="Replacement", objective="Forbidden"),
                              project_id=project.id)
    assert not list((tmp_path / "projects" / project.id).rglob("after.txt"))
