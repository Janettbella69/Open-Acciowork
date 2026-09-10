import asyncio
import os
import threading

from test_run_cancel_ownership import manager
from test_workspace_checkpoints import options

from ophira_backend import workspace_checkpoints as recovery
from ophira_backend.constants import RunStatus
from ophira_backend.models import ProjectCreate
from ophira_backend.workspace_checkpoints import WorkspaceCheckpoints, try_workspace_lease


class ShellRuntime:
    async def run(self, run):
        process = await asyncio.create_subprocess_exec(
            "/bin/sh", "-c", "printf 'Changed by Bash' > report.txt; rm old.txt; "
            "mkdir new; printf 'new file' > new/file.txt",
            cwd=self.ledger.workspace_for_project(run.project_id),
        )
        assert await process.wait() == 0
        raise RuntimeError("Run failed after Bash edits")

    async def cancel(self, _run_id):
        pass


async def test_real_shell_writes_are_captured_without_native_edit_tools(tmp_path):
    runtime = ShellRuntime()
    runs = manager(tmp_path, runtime)
    runtime.ledger = runs.ledger
    project = runs.ledger.create_project(ProjectCreate(name="Bash", objective="Recover files"))
    workspace = runs.ledger.workspace_for_project(project.id)
    (workspace / "report.txt").write_text("Original")
    (workspace / "old.txt").write_text("Deleted by Bash")
    run = await runs.create_and_start(project.id, "Bash modification")
    await asyncio.wait_for(runs._tasks[run.id], 10)
    assert runs.ledger.get_run(run.id).status == RunStatus.FAILED
    service = WorkspaceCheckpoints(runs.ledger)
    point = service.load(project.id, f"before-{run.id}")
    assert point.trace_id == run.trace_id
    assert (workspace / "report.txt").read_text() == "Changed by Bash"
    preview = service.preview(project.id, point.id, **options())
    service.apply(project.id, preview.id, **options())
    assert (workspace / "report.txt").read_text() == "Original"
    assert (workspace / "old.txt").read_text() == "Deleted by Bash"
    assert not (workspace / "new").exists()
    assert runs.ledger.get_run(run.id).status == RunStatus.FAILED
    await runs.stop()


class ConcurrentRuntime:
    def __init__(self):
        self.started = asyncio.Queue()
        self.finished = {}

    async def run(self, run):
        self.finished[run.id] = asyncio.Event()
        await self.started.put(run.id)
        await self.finished[run.id].wait()

    async def cancel(self, _run_id):
        pass


async def test_same_project_runs_serialize_across_managers_and_other_projects_proceed(tmp_path):
    runtime = ConcurrentRuntime()
    first, second = manager(tmp_path, runtime), manager(tmp_path, runtime)
    project = first.ledger.create_project(ProjectCreate(name="Shared", objective="One writer"))
    other = first.ledger.create_project(ProjectCreate(name="Parallel", objective="Independent"))
    a = await first.create_and_start(project.id, "First")
    assert await asyncio.wait_for(runtime.started.get(), 5) == a.id
    b = await second.create_and_start(project.id, "Queued behind first")
    c = await second.create_and_start(other.id, "Other project")
    try:
        assert await asyncio.wait_for(runtime.started.get(), 5) == c.id
        assert second.ledger.get_run(b.id).status == RunStatus.QUEUED
        workspace = first.ledger.workspace_for_project(project.id)
        (workspace / "first-output.txt").write_text("From first")
        runtime.finished[a.id].set()
        assert await asyncio.wait_for(runtime.started.get(), 5) == b.id
        point = WorkspaceCheckpoints(second.ledger).load(project.id, f"before-{b.id}")
        assert point.files[0].path == "first-output.txt"
    finally:
        await first.stop()
        await second.stop()


async def test_cancel_while_checkpoint_thread_is_running_keeps_workspace_lease(
    tmp_path, monkeypatch,
):
    runtime = ConcurrentRuntime()
    runs = manager(tmp_path, runtime)
    project = runs.ledger.create_project(ProjectCreate(name="Cancel", objective="Release safely"))
    entered, finish = threading.Event(), threading.Event()

    def slow_capture(_self, _run_id, cancelled):
        entered.set()
        assert finish.wait(5)
        assert cancelled()
        raise recovery.WorkspaceCaptureCancelled()

    monkeypatch.setattr(WorkspaceCheckpoints, "prepare_run", slow_capture)
    run = await runs.create_and_start(project.id, "Cancel during checkpoint")
    await asyncio.to_thread(entered.wait, 5)
    task = runs._tasks[run.id]
    task.cancel()
    await asyncio.sleep(0)
    assert try_workspace_lease(runs.ledger, project.id) is None
    task.cancel()  # repeated cancellation must not detach the running worker
    finish.set()
    await asyncio.wait_for(task, 5)
    assert runtime.started.empty()
    assert runs.ledger.get_run(run.id).status == RunStatus.CANCELLED
    descriptor = try_workspace_lease(runs.ledger, project.id)
    assert descriptor is not None
    os.close(descriptor)
    await runs.stop()


async def test_workspace_lease_failure_records_a_terminal_failure(tmp_path, monkeypatch):
    from ophira_backend import runs as run_module

    runs = manager(tmp_path)
    project = runs.ledger.create_project(ProjectCreate(name="Failure", objective="Explain errors"))

    def unavailable(*_):
        raise OSError("Workspace lease unavailable")

    monkeypatch.setattr(run_module, "try_workspace_lease", unavailable)
    run = await runs.create_and_start(project.id, "Do not hang queued")
    await asyncio.wait_for(runs._tasks[run.id], 5)
    assert runs.ledger.get_run(run.id).status == RunStatus.FAILED
    assert "lease unavailable" in runs.ledger.get_run(run.id).error
    await runs.stop()


async def test_cancelling_a_queued_run_does_not_claim_the_active_runs_files(tmp_path):
    runtime = ConcurrentRuntime()
    owner, other = manager(tmp_path, runtime), manager(tmp_path, runtime)
    project = owner.ledger.create_project(ProjectCreate(name="Queue", objective="Keep provenance"))
    running = await owner.create_and_start(project.id, "Writing a report")
    assert await asyncio.wait_for(runtime.started.get(), 5) == running.id
    waiting = await other.create_and_start(project.id, "Queued")
    workspace = owner.ledger.workspace_for_project(project.id)
    (workspace / "in-progress.txt").write_text("First run's unfinished output")
    try:
        await other.cancel(waiting.id)
        assert owner.ledger.get_project(project.id).artifacts == []
        runtime.finished[running.id].set()
        await asyncio.wait_for(owner._tasks[running.id], 5)
        artifact = owner.ledger.get_project(project.id).artifacts[0]
        assert artifact.run_id == running.id
    finally:
        await owner.stop()
        await other.stop()
