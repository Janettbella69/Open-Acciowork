import asyncio
from datetime import timedelta

import pytest
from fastapi import Request
from httpx import ASGITransport, AsyncClient
from test_auth_api import signed_headers, trusted_settings
from test_scheduler import build_service, make_schedule

from ophira_backend.config import Settings
from ophira_backend.constants import EventType, PrincipalRole, RunStatus
from ophira_backend.main import create_app
from ophira_backend.models import (
    ProjectCreate,
    ProviderPreflightResult,
    ProviderProfileUpsert,
    utc_now,
)
from ophira_backend.preflight_events import PreflightBusyError, PreflightEventService
from ophira_backend.providers import ProviderRegistry
from ophira_backend.storage import FileLedger


class OfflineRuntime:
    async def run(self, run):
        pass

    async def cancel(self, run_id):
        pass

    async def preflight(self, provider_id):
        return ProviderPreflightResult(provider_profile_id=provider_id, ok=True, message="Checked")


def provider_payload(**changes):
    return ProviderProfileUpsert(
        display_name="Offline fixture", auth_env_name="ANTHROPIC_API_KEY", model="fixture",
        secret="api_key=canary-provider-key", **changes,
    )


def event_store(app):
    return app.state.project_events.scoped


@pytest.mark.asyncio
async def test_no_run_project_changes_repair_coalesce_and_page_without_prose(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path)
    app = create_app(settings, runtime=OfflineRuntime())
    ledger = app.state.ledger
    project = ledger.create_project(ProjectCreate(name="First", objective="api_key=private-brief"))
    url = f"/api/projects/{project.id}/events/history"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        first_response = await client.get(url)
        assert first_response.headers["cache-control"] == "no-store"
        assert first_response.headers["x-content-type-options"] == "nosniff"
        first = first_response.json()
        assert first["last_sequence"] == 1
        assert first["events"][0]["run_id"] is None
        for name in ["Second", "Third"]:
            ledger.update_project(project.id, lambda item, name=name: setattr(item, "name", name))
        # No notification was executed by the write path, and a fresh worker must repair it.
        restarted = create_app(settings, runtime=OfflineRuntime())
        async with AsyncClient(
            transport=ASGITransport(app=restarted), base_url="http://localhost",
        ) as peer:
            repaired = (await peer.get(url, params={"after": 1})).json()
        assert len(repaired["events"]) == 1
        assert repaired["events"][0]["data"]["revision"] == ledger.get_project(project.id).revision
        replay = await client.get(url, params={"tail": True, "limit": 1})
        assert replay.json()["events"] == repaired["events"]
        assert replay.json()["last_sequence"] == 2
        assert "private-brief" not in replay.text
        assert (await client.get(url, params={"after": 3})).status_code == 400
        assert (await client.get(url, params={"after": 1, "tail": True})).status_code == 400
        assert ledger.list_runs(project.id) == []


@pytest.mark.asyncio
async def test_project_stream_resumes_after_watermark_and_stops_on_deleted_project(tmp_path):
    app = create_app(Settings(_env_file=None, data_dir=tmp_path), runtime=OfflineRuntime())
    ledger = app.state.ledger
    project = ledger.create_project(ProjectCreate(name="Live", objective="Observe"))
    app.state.project_events.reconcile_project(project.id)
    ledger.update_project(project.id, lambda item: setattr(item, "name", "Changed"))
    endpoint = next(route.endpoint for route in app.routes
                    if route.path == "/api/projects/{project_id}/events")

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    request = Request({"type": "http", "method": "GET", "path": "/", "headers": []}, receive)
    response = endpoint(request, project.id, 0, f"project:{project.id}:1")
    iterator = response.body_iterator
    event = await anext(iterator)
    assert event["id"] == f"project:{project.id}:2"
    assert event["event"] == EventType.PROJECT_CHANGED
    # A real lifecycle deletion marker is sufficient to deny the very next output.
    marker = ledger.root / "project-deletions" / project.id / "state.json"
    marker.parent.mkdir(parents=True)
    marker.write_text("{}")
    with pytest.raises(StopAsyncIteration):
        await anext(iterator)


@pytest.mark.asyncio
async def test_scope_history_access_is_project_and_admin_bound(tmp_path):
    app = create_app(trusted_settings(tmp_path), runtime=OfflineRuntime())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        created = await client.post(
            "/api/projects", json={"name": "Private", "objective": "Check"},
            headers=signed_headers("POST", "/api/projects", "owner", PrincipalRole.OWNER),
        )
        identifier = created.json()["id"]
        path = f"/api/projects/{identifier}/events/history"
        assert (await client.get(path, headers=signed_headers(
            "GET", path, "stranger", PrincipalRole.MEMBER,
        ))).status_code == 403
        assert (await client.get(path, headers=signed_headers(
            "GET", path, "owner", PrincipalRole.OWNER,
        ))).status_code == 200
        app.state.providers.upsert("fixture", provider_payload())
        path = "/api/providers/fixture/events/history"
        assert (await client.get(path, headers=signed_headers(
            "GET", path, "viewer", PrincipalRole.MEMBER,
        ))).status_code == 403
        observed = await client.get(path, headers=signed_headers(
            "GET", path, "owner", PrincipalRole.OWNER,
        ))
        assert observed.status_code == 200
        assert "tenant-" in observed.json()["stream_id"]


@pytest.mark.asyncio
async def test_preflight_api_observes_without_run_or_credentials(tmp_path):
    app = create_app(Settings(_env_file=None, data_dir=tmp_path), runtime=OfflineRuntime())
    app.state.providers.upsert("fixture", provider_payload())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        assert (await client.post("/api/providers/fixture/preflight")).json()["ok"]
        response = await client.get("/api/providers/fixture/events/history")
    records = response.json()["events"]
    assert response.headers["cache-control"] == "no-store"
    assert [item["type"] for item in records] == [
        EventType.PROVIDER_PREFLIGHT_STARTED, EventType.PROVIDER_PREFLIGHT_PASSED,
    ]
    assert records[0]["trace_id"] == records[1]["trace_id"]
    assert records[0]["data"]["preflight_id"] == records[1]["data"]["preflight_id"]
    assert all(item["run_id"] is None and item["project_id"] is None for item in records)
    assert "canary-provider-key" not in response.text


@pytest.mark.asyncio
async def test_preflight_peer_cannot_repair_live_check_but_repairs_lost_terminal(
    tmp_path, monkeypatch,
):
    ledger = FileLedger(tmp_path)
    providers = ProviderRegistry(Settings(_env_file=None, data_dir=tmp_path))
    providers.upsert("fixture", provider_payload())
    service = PreflightEventService(ledger, providers)
    peer = PreflightEventService(FileLedger(tmp_path), providers)
    started = asyncio.Event()
    finish = asyncio.Event()

    async def check(provider_id):
        started.set()
        await finish.wait()
        return ProviderPreflightResult(provider_profile_id=provider_id, ok=True, message="Checked")

    task = asyncio.create_task(service.execute("fixture", check))
    await started.wait()
    peer.reconcile("fixture")
    stream = service.events.provider_stream("fixture")
    assert len(peer.events.read(stream)) == 1
    with pytest.raises(PreflightBusyError):
        await peer.execute("fixture", check)
    original = service.events.append

    def lose_terminal(stream_id, event_type, *args, **kwargs):
        if event_type == EventType.PROVIDER_PREFLIGHT_PASSED:
            raise OSError("injected event write failure")
        return original(stream_id, event_type, *args, **kwargs)

    monkeypatch.setattr(service.events, "append", lose_terminal)
    finish.set()
    with pytest.raises(OSError):
        await task
    peer.reconcile("fixture")
    peer.reconcile("fixture")
    recovered = peer.events.read(stream)
    assert len(recovered) == 2
    assert recovered[-1].data["reason"] == "interrupted"
    assert recovered[-1].type == EventType.PROVIDER_PREFLIGHT_FAILED


@pytest.mark.asyncio
async def test_preflight_cancel_and_configuration_change_are_not_success(tmp_path):
    providers = ProviderRegistry(Settings(_env_file=None, data_dir=tmp_path))
    providers.upsert("fixture", provider_payload())
    service = PreflightEventService(FileLedger(tmp_path), providers)

    async def change(provider_id):
        providers.upsert(provider_id, provider_payload())
        return ProviderPreflightResult(
            provider_profile_id=provider_id, ok=True, message="Old config",
        )

    result = await service.execute("fixture", change)
    assert not result.ok
    stream = service.events.provider_stream("fixture")
    assert service.events.read(stream)[-1].data["reason"] == "configuration_changed"

    async def cancelled(provider_id):
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await service.execute("fixture", cancelled)
    service.reconcile("fixture")
    assert len(service.events.read(stream)) == 4
    assert service.events.read(stream)[-1].data["reason"] == "interrupted"


@pytest.mark.asyncio
async def test_preflight_waits_for_brief_history_recovery_instead_of_false_conflict(tmp_path):
    providers = ProviderRegistry(Settings(_env_file=None, data_dir=tmp_path))
    providers.upsert("fixture", provider_payload())
    service = PreflightEventService(FileLedger(tmp_path), providers)
    peer = PreflightEventService(FileLedger(tmp_path), providers)
    entered = asyncio.Event()

    async def history_repair():
        with peer._lease("fixture") as acquired:
            assert acquired
            entered.set()
            await asyncio.sleep(0.06)

    reader = asyncio.create_task(history_repair())
    await entered.wait()
    result = await service.execute("fixture", OfflineRuntime().preflight)
    await reader
    assert result.ok
    rows = service.events.read(service.events.provider_stream("fixture"))
    assert [row.type for row in rows] == [
        EventType.PROVIDER_PREFLIGHT_STARTED, EventType.PROVIDER_PREFLIGHT_PASSED,
    ]


@pytest.mark.asyncio
async def test_skip_receipt_survives_crash_before_advancement(tmp_path, monkeypatch):
    ledger, runtime, service, project = build_service(tmp_path)
    schedule = make_schedule(project.id, next_fire_at=utc_now() - timedelta(minutes=1))
    ledger.create_schedule(schedule)
    original = service._advance

    def fail_advance(*args):
        raise OSError("injected crash after skip")

    monkeypatch.setattr(service, "_advance", fail_advance)
    await service.reconcile()
    assert runtime.runs == []
    monkeypatch.setattr(service, "_advance", original)
    await service.tick()  # A normal tick would execute it without the committed skip guard.
    assert runtime.runs == []
    rows = service.scoped.read(service.scoped.project_stream(project.id))
    assert len(rows) == 1 and rows[0].type == EventType.SCHEDULE_SKIPPED
    assert rows[0].data["reason"] == "missed_run_policy"
    assert ledger.get_schedule(schedule.id).next_fire_at > utc_now()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED])
async def test_schedule_terminal_repair_has_real_run_and_never_reruns(tmp_path, status):
    ledger, runtime, service, project = build_service(tmp_path)
    schedule = make_schedule(project.id, next_fire_at=utc_now() + timedelta(minutes=1))
    ledger.create_schedule(schedule)
    run = ledger.create_run(project.id, "Actual scheduled attempt", schedule_id=schedule.id)
    ledger.update_run(run.id, status=status)
    service.projector.reconcile_project(project.id)
    service.projector.reconcile_project(project.id)
    rows = [event for event in service.scoped.read(service.scoped.project_stream(project.id))
            if event.type.startswith("schedule.")]
    assert [row.type for row in rows] == ["schedule.fired", f"schedule.{status.value}"]
    assert all(row.run_id == run.id and row.trace_id == run.trace_id for row in rows)
    assert runtime.runs == []
