import asyncio
import json
import multiprocessing
import os
from pathlib import Path
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from test_application_backup import source as application_source

from ophira_backend import maintenance_console_store as store
from ophira_backend.application_backup import inspect_application
from ophira_backend.backup_files import BackupError, remove_staging
from ophira_backend.maintenance import DataDirectoryBusyError, data_directory_lease
from ophira_backend.maintenance_console import SESSION_SECONDS, create_console_app
from ophira_backend.maintenance_console_models import MaintenanceConfirm, MaintenancePrepare
from ophira_backend.storage import FileLedger


@pytest.fixture
def backup_source(tmp_path, monkeypatch):
    yield from application_source.__wrapped__(tmp_path, monkeypatch)


def console(backup_source, tmp_path):
    return store.MaintenanceConsole(backup_source[0], backup_source[1], tmp_path / "maintenance")


def prepare(service, *, kind="backup", backup_id=None):
    return service.prepare(
        MaintenancePrepare(
            request_id=uuid4().hex,
            kind=kind,
            backup_id=backup_id,
        )
    )


def confirm(service, reviewed):
    return service.confirm(
        reviewed["request_id"],
        MaintenanceConfirm(
            fingerprint=reviewed["fingerprint"],
            confirmed=True,
        ),
    )


def test_full_backup_review_and_restore_after_source_loss_without_starting_jobs(
    backup_source, tmp_path
):
    service = console(backup_source, tmp_path)
    reviewed = prepare(service)
    assert reviewed["state"] == "reviewed" and reviewed["summary"]["projects"] == 1
    assert not Path(reviewed["destination"]).exists()
    result = confirm(service, reviewed)
    assert result["state"] == "completed" and confirm(service, reviewed) == result
    remove_staging(backup_source[0])
    remove_staging(backup_source[1])
    restarted = store.MaintenanceConsole(service.data, service.plugin, service.root)
    backup = restarted.status()["backups"][0]
    restore = prepare(restarted, kind="restore", backup_id=backup["id"])
    assert restore["summary"]["provider_invoked"] is False
    assert restore["summary"]["canonical_preserved"] is True
    assert restore["summary"]["proposal_states"] == {"pending": 2, "published": 2, "rejected": 1}
    target = Path(confirm(restarted, restore)["destination"])
    assert (
        FileLedger(target / "data").get_project(backup_source[5].id).name == backup_source[5].name
    )
    assert inspect_application(target / "data", target / "application-plugin")["runs"] == [
        backup_source[6].id
    ]
    assert not service.data.exists() and not service.plugin.exists()
    # Reviews contain scope and counts, never source prompts or credentials.
    records = b"".join(path.read_bytes() for path in (service.root / "reviews").glob("*.json"))
    assert b"Synthetic proposal source" not in records
    assert b"private-backup-canary" not in records
    assert "private-backup-canary" in (target / "data/secrets/provider-secrets.json").read_text()


@pytest.mark.asyncio
async def test_http_rechecks_session_expiry_before_committing(backup_source, tmp_path, monkeypatch):
    service = console(backup_source, tmp_path)
    reviewed = prepare(service)
    assets = tmp_path / "assets"
    (assets / "assets").mkdir(parents=True)
    current = [0]
    origin, token = "http://127.0.0.1:8765", "a" * 43
    app = create_console_app(
        service, token=token, origin=origin, assets=assets, clock=lambda: current[0]
    )
    verify = service._verify_candidate

    def expires_after_verification(*args):
        verify(*args)
        current[0] = SESSION_SECONDS

    monkeypatch.setattr(service, "_verify_candidate", expires_after_verification)
    async with AsyncClient(
        transport=ASGITransport(app=app, client=("127.0.0.1", 1)),
        base_url=origin,
    ) as client:
        response = await client.post(
            f"/api/maintenance/reviews/{reviewed['request_id']}/confirm",
            headers={"Authorization": f"Bearer {token}", "Origin": origin},
            json={"fingerprint": reviewed["fingerprint"], "confirmed": True},
        )
    assert response.status_code == 403
    assert not await asyncio.to_thread(Path(reviewed["destination"]).exists)
    assert service._record(reviewed["request_id"]).state == "reviewed"


def test_busy_source_requires_shutdown_then_retries_same_preparation_and_can_discard(
    backup_source, tmp_path
):
    service = console(backup_source, tmp_path)
    request = MaintenancePrepare(request_id=uuid4().hex, kind="backup")
    with data_directory_lease(service.data), pytest.raises(DataDirectoryBusyError):
        service.prepare(request)
    assert service.status()["operations"][0]["state"] == "preparing"
    reviewed = service.prepare(request)
    assert service.prepare(request) == reviewed
    with pytest.raises(BackupError, match="finish_current"):
        prepare(service)
    assert service.discard(request.request_id)["state"] == "discarded"
    assert not list((service.root / "backups").iterdir())
    assert service.prepare(request)["state"] == "discarded"
    assert backup_source[2].get_project(backup_source[5].id)


@pytest.mark.parametrize(
    "change", ["fingerprint", "file", "link", "target", "expired", "authority"]
)
def test_review_changes_refuse_before_publication(backup_source, tmp_path, monkeypatch, change):
    service = console(backup_source, tmp_path)
    reviewed = prepare(service)
    record = service._record(reviewed["request_id"])
    candidate = service._stage(record) / "candidate"
    target = Path(reviewed["destination"])
    confirmation = MaintenanceConfirm(fingerprint=reviewed["fingerprint"], confirmed=True)
    if change == "fingerprint":
        confirmation.fingerprint = "0" * 64
    elif change == "file":
        (candidate / "manifest.json").write_text("modified-private-canary")
    elif change == "link":
        (candidate / "escape").symlink_to(backup_source[0])
    elif change == "target":
        target.mkdir()
        (target / "keep").write_text("preexisting")
    elif change == "expired":
        from datetime import timedelta

        monkeypatch.setattr(store, "utc_now", lambda: record.reviewed_at + timedelta(days=2))
    checks = 0

    def authorize():
        nonlocal checks
        checks += 1
        if change == "authority" and checks == 2:
            raise PermissionError("expired")

    with pytest.raises((OSError, ValueError)):
        service.confirm(record.request_id, confirmation, authorize=authorize)
    assert service._record(record.request_id).state == "reviewed"
    assert (
        (target / "keep").read_text() == "preexisting"
        if change == "target"
        else not target.exists()
    )


def _crash_confirmation(data, plugin, root, reviewed, window):
    service = store.MaintenanceConsole(data, plugin, root)
    if window == "commit":
        original = service._write

        def crash(record):
            original(record)
            if record.state == "publishing":
                os._exit(41)

        service._write = crash
    else:
        original = store.publish_exclusive

        def crash(*args):
            original(*args)
            os._exit(42)

        store.publish_exclusive = crash
    confirm(service, reviewed)


@pytest.mark.parametrize("window", ["commit", "rename"])
def test_real_process_crash_recovers_the_same_committed_copy(backup_source, tmp_path, window):
    service = console(backup_source, tmp_path)
    reviewed = prepare(service)
    process = multiprocessing.get_context("spawn").Process(
        target=_crash_confirmation,
        args=(service.data, service.plugin, service.root, reviewed, window),
    )
    process.start()
    process.join(20)
    try:
        assert process.exitcode == (41 if window == "commit" else 42)
    finally:
        if process.is_alive():
            process.kill()
            process.join(5)
    assert service._record(reviewed["request_id"]).state == "publishing"
    with pytest.raises(BackupError, match="already_committed"):
        service.discard(reviewed["request_id"])
    result = confirm(service, reviewed)
    assert result["state"] == "completed" and confirm(service, reviewed) == result
    assert len(service.status()["backups"]) == 1


def test_restore_refuses_corrupt_input_and_staging_never_overwrites_unrelated_files(
    backup_source, tmp_path
):
    service = console(backup_source, tmp_path)
    backup = confirm(service, prepare(service))
    (Path(backup["destination"]) / "manifest.json").write_text("credential-canary-corrupt")
    identifier = service.status()["backups"][0]["id"]
    with pytest.raises(ValueError):
        prepare(service, kind="restore", backup_id=identifier)
    assert not list((service.root / "restores").glob("restore-*"))
    record = service._record(service.status()["operations"][0]["request_id"])
    stage = service._stage(record)
    stage.rename(stage.with_suffix(".moved"))
    stage.mkdir()
    (stage / "keep").write_text("unrelated")
    with pytest.raises(BackupError, match="staging_changed"):
        service.discard(record.request_id)
    assert (stage / "keep").read_text() == "unrelated"


@pytest.mark.asyncio
async def test_http_operator_capability_origin_confirmation_and_expiry(backup_source, tmp_path):
    service = console(backup_source, tmp_path)
    assets = tmp_path / "assets"
    (assets / "assets").mkdir(parents=True)
    (assets / "maintenance.html").write_text("<!doctype html><title>Maintenance</title>")
    current = [0]
    origin, token = "http://127.0.0.1:8765", "a" * 43
    app = create_console_app(
        service, token=token, origin=origin, assets=assets, clock=lambda: current[0]
    )
    headers = {"Authorization": f"Bearer {token}", "Origin": origin}
    transport = ASGITransport(app=app, client=("127.0.0.1", 1234))
    async with AsyncClient(transport=transport, base_url=origin) as client:
        static = await client.get("/")
        assert static.status_code == 200 and token not in static.text
        assert "frame-ancestors 'none'" in static.headers["content-security-policy"]
        assert (await client.get("/api/maintenance")).status_code == 403
        for changed in ({"Origin": "https://attacker.example"}, {"Host": "attacker.example"}):
            response = await client.get("/api/maintenance", headers={**headers, **changed})
            assert response.status_code == 403
        payload = {"request_id": uuid4().hex, "kind": "backup"}
        missing_origin = await client.post(
            "/api/maintenance/reviews", json=payload, headers={"Authorization": f"Bearer {token}"}
        )
        assert missing_origin.status_code == 403
        invalid = await client.post(
            "/api/maintenance/reviews", headers=headers, json={**payload, "path": "/private"}
        )
        assert invalid.status_code == 422
        response = await client.post("/api/maintenance/reviews", json=payload, headers=headers)
        assert response.status_code == 200, response.text
        reviewed = response.json()
        path = f"/api/maintenance/reviews/{payload['request_id']}/confirm"
        invalid = await client.post(
            path,
            headers=headers,
            json={"fingerprint": reviewed["fingerprint"], "confirmed": "true"},
        )
        assert invalid.status_code == 422
        result = await client.post(
            path, headers=headers, json={"fingerprint": reviewed["fingerprint"], "confirmed": True}
        )
        assert result.status_code == 200 and result.json()["state"] == "completed"
        current[0] = SESSION_SECONDS
        assert (await client.get("/api/maintenance", headers=headers)).status_code == 403
        assert "credential-canary" not in json.dumps(result.json())
    remote = ASGITransport(app=app, client=("192.0.2.1", 1234))
    async with AsyncClient(transport=remote, base_url=origin) as client:
        assert (await client.get("/", headers=headers)).status_code == 403
