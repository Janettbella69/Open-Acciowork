import asyncio
import os

import pytest
from httpx import ASGITransport, AsyncClient

from ophira_backend.config import Settings
from ophira_backend.constants import EventType, RunStatus
from ophira_backend.events import EventPublisher
from ophira_backend.main import create_app
from ophira_backend.models import ProjectCreate, utc_now
from ophira_backend.permissions import PermissionBroker
from ophira_backend.runs import RunManager
from ophira_backend.storage import FileLedger


class OfflineRuntime:
    def __init__(self):
        self.seen = []

    async def run(self, run):
        self.seen.append(run)

    async def cancel(self, _run_id):
        return None


def manager(root, runtime):
    ledger = FileLedger(root)
    events = EventPublisher(ledger)
    permissions = PermissionBroker(ledger, events, timeout_seconds=30)
    return RunManager(ledger, events, permissions, runtime)


@pytest.mark.parametrize(
    "status", [RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.WAITING_PERMISSION, RunStatus.PAUSED]
)
async def test_startup_recovers_orphans_preserves_work_and_allows_resume(tmp_path, status):
    runtime = OfflineRuntime()
    settings = Settings(
        _env_file=None, data_dir=tmp_path, ANTHROPIC_API_KEY="", ANTHROPIC_AUTH_TOKEN=""
    )
    before = create_app(settings, runtime=runtime)
    ledger = before.state.ledger
    project = ledger.create_project(ProjectCreate(name="Recovery", objective="Preserve work"))
    source = ledger.create_run(project.id, "Original request")
    ledger.update_run(source.id, status=status, sdk_session_id="session_before_restart")
    artifact = ledger.save_upload(project.id, "saved.txt", b"saved evidence")
    partial = ledger.workspace_for_project(project.id) / "partial-report.md"
    partial.write_text("recoverable report", encoding="utf-8")
    await before.state.events.publish(
        source.id, EventType.PERMISSION_REQUESTED, {"request_id": "permission_before_restart"}
    )

    restarted = create_app(settings, runtime=runtime)
    async with restarted.router.lifespan_context(restarted):
        recovered = restarted.state.ledger.get_run(source.id)
        assert recovered.status == RunStatus.FAILED
        assert recovered.sdk_session_id == "session_before_restart"
        assert recovered.finished_at is not None
        assert "backend stopped" in recovered.error
        assert runtime.seen == []
        saved = restarted.state.ledger.get_project(project.id)
        assert artifact.id in {item.id for item in saved.artifacts}
        assert any(item.path == "partial-report.md" for item in saved.artifacts)
        events = restarted.state.ledger.list_events(source.id)
        assert events[-1].type == EventType.RUN_FAILED.value
        assert any(event.type == EventType.PERMISSION_TIMED_OUT.value for event in events)
        await restarted.state.runs.recover_interrupted()
        assert restarted.state.ledger.list_events(source.id) == events
        async with AsyncClient(
            transport=ASGITransport(app=restarted), base_url="http://localhost"
        ) as client:
            replay = await client.get(f"/api/runs/{source.id}/events")
            assert "run.failed" in replay.text
            response = await client.post(
                f"/api/runs/{source.id}/retry", json={"strategy": "resume"}
            )
            assert response.status_code == 202
            assert response.json()["resume_session_id"] == "session_before_restart"
            for _ in range(30):
                if runtime.seen:
                    break
                await asyncio.sleep(0)
            assert len(runtime.seen) == 1


async def test_recovery_finishes_cancellation_but_keeps_terminal_history(tmp_path):
    runs = manager(tmp_path, OfflineRuntime())
    project = runs.ledger.create_project(ProjectCreate(name="Recovery", objective="Cancel"))
    cancelling = runs.ledger.create_run(project.id, "Cancel requested")
    runs.ledger.update_run(cancelling.id, status=RunStatus.CANCELLING)
    terminal = []
    for status in (RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED):
        run = runs.ledger.create_run(project.id, status.value)
        terminal.append(runs.ledger.update_run(run.id, status=status, finished_at=utc_now()))
    await runs.recover_interrupted()
    assert runs.ledger.get_run(cancelling.id).status == RunStatus.CANCELLED
    assert runs.ledger.list_events(cancelling.id)[-1].type == EventType.RUN_CANCELLED.value
    for run in terminal:
        assert runs.ledger.get_run(run.id) == run
        assert runs.ledger.list_events(run.id) == []


class BlockingRuntime(OfflineRuntime):
    async def run(self, run):
        self.seen.append(run)
        await asyncio.Event().wait()


async def test_recovery_does_not_interrupt_another_workers_live_run(tmp_path):
    runtime = BlockingRuntime()
    owner = manager(tmp_path, runtime)
    other = manager(tmp_path, OfflineRuntime())
    project = owner.ledger.create_project(ProjectCreate(name="Live", objective="Keep executing"))
    run = await owner.create_and_start(project.id, "Still working")
    try:
        # The lease must already protect the queued state before execution begins.
        await other.recover_interrupted()
        assert other.ledger.get_run(run.id).status != RunStatus.FAILED
        await asyncio.sleep(0)
        await other.recover_interrupted()
        assert other.ledger.get_run(run.id).status == RunStatus.RUNNING
        assert not owner._tasks[run.id].done()
    finally:
        await owner.stop()
    assert owner.ledger.get_run(run.id).status == RunStatus.CANCELLED
    lease = other._acquire_run_lease(run.id)
    assert lease is not None
    os.close(lease)


async def test_shutdown_finalizes_a_run_cancelled_before_execution_starts(tmp_path):
    runtime = OfflineRuntime()
    runs = manager(tmp_path, runtime)
    project = runs.ledger.create_project(ProjectCreate(name="Queued", objective="Stop cleanly"))
    run = await runs.create_and_start(project.id, "Never started")
    await runs.stop()
    assert runs.ledger.get_run(run.id).status == RunStatus.CANCELLED
    assert runtime.seen == []


async def test_recovery_retries_a_terminal_event_lost_after_snapshot_write(tmp_path, monkeypatch):
    runs = manager(tmp_path, OfflineRuntime())
    project = runs.ledger.create_project(ProjectCreate(name="Interrupted", objective="Recover"))
    run = runs.ledger.create_run(project.id, "Unfinished")
    publish = runs.events.publish

    async def unavailable_event_log(*args, **kwargs):
        raise OSError("Simulated event log failure")

    monkeypatch.setattr(runs.events, "publish", unavailable_event_log)
    with pytest.raises(OSError, match="event log"):
        await runs.recover_interrupted()
    assert runs.ledger.get_run(run.id).status == RunStatus.FAILED
    monkeypatch.setattr(runs.events, "publish", publish)
    await runs.recover_interrupted()
    assert runs.ledger.list_events(run.id)[-1].type == EventType.RUN_FAILED.value
    await runs.recover_interrupted()
    assert len(runs.ledger.list_events(run.id)) == 1
