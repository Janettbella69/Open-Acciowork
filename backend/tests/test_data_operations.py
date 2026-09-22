import asyncio
import threading
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from test_auth_api import FakeRuntime, principal, request_as, trusted_settings

from ophira_backend.constants import PrincipalRole
from ophira_backend.data_operations import (
    DataOperationMiddleware,
    data_operation,
    protected_call,
)
from ophira_backend.erasure_files import (
    JOURNAL,
    DataOperationBusy,
    ErasureFile,
    ErasurePlan,
    FileErasure,
    digest,
    operation_lease,
)
from ophira_backend.main import create_app
from ophira_backend.models import ProjectCreate


@pytest.mark.asyncio
async def test_gate_covers_authentication_nonce_writes_and_reopens_after_release(tmp_path):
    app = create_app(trusted_settings(tmp_path), runtime=FakeRuntime())
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://gateway.test"
    ) as client:
        before = {p: p.read_bytes() for p in (tmp_path / "auth/nonces").rglob("*") if p.is_file()}
        with operation_lease(tmp_path, exclusive=True):
            response = await request_as(
                client, "GET", "/api/projects", "owner", PrincipalRole.OWNER
            )
            assert response.status_code == 409 and response.headers["cache-control"] == "no-store"
            assert response.headers["retry-after"] == "1"
        after = {p: p.read_bytes() for p in (tmp_path / "auth/nonces").rglob("*") if p.is_file()}
        assert before == after
        response = await request_as(client, "GET", "/api/projects", "owner", PrincipalRole.OWNER)
        assert response.status_code == 200


@pytest.mark.asyncio
async def test_busy_response_preserves_configured_cors_without_authorizing_a_write(tmp_path):
    settings = trusted_settings(tmp_path).model_copy(
        update={"cors_origins": ["https://workspace.example"]}
    )
    app = create_app(settings, runtime=FakeRuntime())
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://gateway.test"
    ) as client:
        with operation_lease(tmp_path, exclusive=True):
            response = await client.get(
                "/api/projects", headers={"Origin": "https://workspace.example"}
            )
            assert response.status_code == 409
            assert response.headers["access-control-allow-origin"] == "https://workspace.example"
            response = await client.get(
                "/api/projects", headers={"Origin": "https://untrusted.example"}
            )
            assert "access-control-allow-origin" not in response.headers
            response = await client.options(
                "/api/projects",
                headers={
                    "Origin": "https://workspace.example",
                    "Access-Control-Request-Method": "POST",
                },
            )
            assert response.status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize("finish", ["normal", "disconnect"])
async def test_stream_owns_lease_until_actual_completion_or_disconnect(tmp_path, finish):
    started, release = asyncio.Event(), asyncio.Event()

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        started.set()
        await release.wait()
        await send({"type": "http.response.body", "body": b"done"})

    async def send(message):
        pass

    task = asyncio.create_task(
        DataOperationMiddleware(app, tmp_path)(
            {"type": "http", "path": "/api/runs/fixture/events"},
            None,
            send,
        )
    )
    await asyncio.wait_for(started.wait(), 2)
    with pytest.raises(DataOperationBusy):
        with operation_lease(tmp_path, exclusive=True):
            pytest.fail("stream released its gate before completion")
    if finish == "disconnect":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        release.set()
        await task
    with operation_lease(tmp_path, exclusive=True):
        pass


@pytest.mark.asyncio
async def test_detached_run_waits_without_inheriting_a_closed_request_lease(tmp_path):
    started, release = asyncio.Event(), asyncio.Event()

    class Worker:
        ledger = SimpleNamespace(root=tmp_path)

        @data_operation(wait=True)
        async def execute(self):
            started.set()
            await release.wait()

    with operation_lease(tmp_path, exclusive=True):
        task = asyncio.create_task(Worker().execute())
        await asyncio.sleep(0.08)
        assert not started.is_set() and not task.done()
    await asyncio.wait_for(started.wait(), 2)
    with pytest.raises(DataOperationBusy):
        with operation_lease(tmp_path, exclusive=True):
            pytest.fail("detached work has no lease")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with operation_lease(tmp_path, exclusive=True):
        pass


@pytest.mark.asyncio
async def test_cancelled_request_cannot_release_a_still_running_upload_thread(tmp_path):
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def operation():
        started.set()
        assert release.wait(3)
        (tmp_path / "upload.txt").write_text("completed after request cancellation")

    def thread():
        try:
            protected_call(tmp_path, operation)
        finally:
            finished.set()

    task = asyncio.create_task(asyncio.to_thread(thread))
    assert await asyncio.to_thread(started.wait, 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    try:
        with pytest.raises(DataOperationBusy):
            with operation_lease(tmp_path, exclusive=True):
                pytest.fail("cancelled coroutine released the thread's lease")
    finally:
        release.set()
    assert await asyncio.to_thread(finished.wait, 2)
    with operation_lease(tmp_path, exclusive=True):
        assert (tmp_path / "upload.txt").is_file()


@pytest.mark.asyncio
async def test_background_services_and_browser_host_refuse_exclusive_maintenance(tmp_path):
    from ophira_backend.shop_browser_host import serve

    app = create_app(trusted_settings(tmp_path), runtime=FakeRuntime())
    with operation_lease(tmp_path, exclusive=True):
        for operation in (
            app.state.scheduler.tick,
            app.state.intent_watcher.tick,
            app.state.channel_delivery.reconcile,
            app.state.companion_push.tick,
            lambda: serve(tmp_path, "unread-shop"),
        ):
            with pytest.raises(DataOperationBusy):
                await operation()


@pytest.mark.asyncio
async def test_pending_deletion_blocks_old_worker_and_is_recovered_before_new_stores_open(tmp_path):
    settings = trusted_settings(tmp_path)
    app = create_app(settings, runtime=FakeRuntime())
    owner = principal("owner", PrincipalRole.OWNER)
    project = app.state.ledger.create_project(
        ProjectCreate(name="Erase", objective="Fixture"), principal=owner
    )
    path = f"projects/{project.id}/project.json"
    original = (tmp_path / path).read_bytes()
    plan = ErasurePlan(
        scope={"project_id": project.id},
        files=[
            ErasureFile(
                path=path,
                before_size=len(original),
                before_sha256=digest(original),
            )
        ],
        directories=[f"projects/{project.id}"],
    )
    eraser = FileErasure(tmp_path)
    eraser._recover_locked = lambda: None
    eraser.commit(plan, {}, authorize=lambda: None)
    assert (tmp_path / JOURNAL / "pending").is_dir()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://gateway.test"
    ) as client:
        response = await request_as(client, "GET", "/api/projects", "owner", PrincipalRole.OWNER)
        assert response.status_code == 409
    restarted = create_app(settings, runtime=FakeRuntime())
    assert restarted.state.ledger.list_projects() == []
    assert not (tmp_path / path).exists()
    assert not (tmp_path / JOURNAL / "pending").exists()


def test_starting_a_worker_cannot_recover_a_live_eraser(tmp_path):
    settings = trusted_settings(tmp_path)
    with operation_lease(tmp_path, exclusive=True):
        with pytest.raises(DataOperationBusy):
            create_app(settings, runtime=FakeRuntime())
    assert not (tmp_path / "projects").exists()


def test_cold_backup_rejects_incomplete_erasure_even_without_a_serving_worker(tmp_path):
    from ophira_backend.application_backup import inspect_application
    from ophira_backend.backup_files import BackupError

    (tmp_path / "data" / JOURNAL / "pending").mkdir(parents=True)
    with pytest.raises(BackupError, match="pending_data_erasure"):
        inspect_application(tmp_path / "data", tmp_path / "unused-plugin")
