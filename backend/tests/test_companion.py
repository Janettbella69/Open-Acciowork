import asyncio
import json
import multiprocessing
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from ophira_backend.auth import build_gateway_assertion_headers
from ophira_backend.companion import (
    COMPANION_COOKIE,
    CompanionError,
    CompanionStore,
    PairingClaim,
    PairingCreate,
    PairingDecision,
    PairingStatus,
)
from ophira_backend.config import Settings
from ophira_backend.constants import PermissionDecision, PrincipalRole, RunStatus
from ophira_backend.main import create_app
from ophira_backend.models import PrincipalContext, ProjectCreate, utc_now

PUBLIC_ORIGIN = "https://companion.example"
DEVICE_HEADERS = {"X-Ophira-Companion": "1", "Origin": PUBLIC_ORIGIN}


class WaitingRuntime:
    async def run(self, run):
        await asyncio.Event().wait()

    async def cancel(self, run_id):
        return None


@pytest.fixture
def app(tmp_path):
    return create_app(Settings(
        _env_file=None, data_dir=tmp_path, companion_enabled=True,
        companion_public_origin=PUBLIC_ORIGIN,
    ), runtime=WaitingRuntime())


def owner_client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost")


def device_client(app):
    return AsyncClient(
        transport=ASGITransport(app=app, client=("203.0.113.1", 4567)),
        base_url=PUBLIC_ORIGIN, headers=DEVICE_HEADERS,
    )


def _claim_in_process(root, payload, ready, results):
    store = CompanionStore(root)
    ready.wait(10)
    try:
        store.claim(PairingClaim.model_validate(payload))
        results.put("claimed")
    except CompanionError:
        results.put("denied")


async def pair(owner, device, project_id, *, allow_run_start=False):
    response = await owner.post("/api/mobile/pairings", json={
        "project_ids": [project_id], "allow_run_start": allow_run_start,
    })
    assert response.status_code == 201, response.text
    fragment = parse_qs(urlsplit(response.json()["pairing_url"]).fragment)
    pairing_id = fragment["pairing"][0]
    claim = await device.post("/api/companion/pair", json={
        "pairing_id": pairing_id, "secret": fragment["secret"][0], "device_name": "Test phone",
    })
    assert claim.status_code == 200, claim.text
    code = claim.json()["device"]["verification_code"]
    return pairing_id, code, claim


async def approve(owner, device, pairing_id, code):
    approved = await owner.post(f"/api/mobile/pairings/{pairing_id}/decision", json={
        "decision": "approve", "verification_code": code,
    })
    assert approved.status_code == 200, approved.text
    session = await device.get("/api/companion/session")
    assert session.json()["status"] == "approved"
    assert COMPANION_COOKIE in session.headers["set-cookie"]


async def test_real_http_pairing_requires_desktop_confirmation_and_scopes_projects(app):
    project = app.state.ledger.create_project(ProjectCreate(name="Allowed", objective="Inspect"))
    other = app.state.ledger.create_project(ProjectCreate(name="Private", objective="Not shared"))
    async with owner_client(app) as owner, device_client(app) as device:
        pairing_id, code, claim = await pair(owner, device, project.id)
        cookie = claim.headers["set-cookie"]
        assert all(flag in cookie for flag in ("HttpOnly", "Secure", "SameSite=strict", "Path=/"))
        assert claim.headers["cache-control"] == "no-store"
        assert (await device.get("/api/companion/overview")).status_code == 403
        wrong = "000000" if code != "000000" else "111111"
        assert (await owner.post(f"/api/mobile/pairings/{pairing_id}/decision", json={
            "decision": "approve", "verification_code": wrong,
        })).status_code == 409
        pending_cookie = device.cookies.get(COMPANION_COOKIE)
        await approve(owner, device, pairing_id, code)
        assert device.cookies.get(COMPANION_COOKIE) != pending_cookie
        data = (await device.get("/api/companion/overview")).json()
        assert [item["id"] for item in data["projects"]] == [project.id]
        assert (await device.post(f"/api/companion/projects/{project.id}/runs", json={
            "prompt": "Not authorized to start",
        })).status_code == 403
        assert (await device.post(f"/api/companion/projects/{other.id}/runs", json={
            "prompt": "Cross-project",
        })).status_code == 403
        assert (await device.get("/api/providers")).status_code == 403
        assert (await device.get("/api/mobile/pairings")).status_code == 403
        assert (await device.get("/api/companion/session", headers={
            "Cookie": f"{COMPANION_COOKIE}={pending_cookie}",
        })).json()["status"] == "unpaired"


async def test_companion_can_start_cancel_and_approve_only_granted_project(app):
    ledger = app.state.ledger
    project = ledger.create_project(ProjectCreate(name="Allowed", objective="Inspect"))
    other = ledger.create_project(ProjectCreate(name="Private", objective="Not shared"))
    async with owner_client(app) as owner, device_client(app) as device:
        pairing_id, code, _ = await pair(owner, device, project.id, allow_run_start=True)
        await approve(owner, device, pairing_id, code)
        started = await device.post(f"/api/companion/projects/{project.id}/runs", json={
            "prompt": "Inspect the existing project",
        })
        assert started.status_code == 202, started.text
        run_id = started.json()["id"]
        assert ledger.get_run(run_id).companion_device_id == pairing_id
        assert (await device.post(f"/api/companion/projects/{other.id}/runs", json={
            "prompt": "Wrong project",
        })).status_code == 403
        pending = asyncio.create_task(app.state.permissions.request(
            run_id, "Write", {"file_path": "report.txt", "content": "Review"},
            agent_id=None, context={},
        ))
        try:
            for _ in range(30):
                if app.state.permissions.list_pending():
                    break
                await asyncio.sleep(0)
            view = (await device.get("/api/companion/overview")).json()
            request_id = view["pending_permissions"][0]["id"]
            path = f"/api/companion/runs/{run_id}/permissions/{request_id}"
            persistent = await device.post(path, json={"decision": "allow_persistent"})
            assert persistent.status_code == 403
            assert (await device.post(path, json={"decision": "allow_once"})).status_code == 204
            assert (await pending).decision == PermissionDecision.ALLOW_ONCE
            events = ledger.list_events(run_id)
            resolved = [event for event in events if event.type == "permission.resolved"][-1]
            assert resolved.actor.name == f"paired-device:{pairing_id}"
            cancelled = await device.post(f"/api/companion/runs/{run_id}/cancel", json={})
            assert cancelled.status_code == 200
            assert ledger.get_run(run_id).status == RunStatus.CANCELLED
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            await app.state.runs.stop()


async def test_revocation_and_logout_take_effect_without_restart(app):
    project = app.state.ledger.create_project(ProjectCreate(name="One", objective="Inspect"))
    async with owner_client(app) as owner, device_client(app) as device:
        pairing_id, code, _ = await pair(owner, device, project.id)
        await approve(owner, device, pairing_id, code)
        assert (await owner.post(f"/api/mobile/pairings/{pairing_id}/revoke")).status_code == 200
        assert (await device.get("/api/companion/overview")).status_code == 403
        assert (await device.get("/api/companion/session")).json()["status"] == "revoked"
        assert (await device.post("/api/companion/logout", json={})).status_code == 200
        assert (await device.get("/api/companion/session")).json()["status"] == "unpaired"


@pytest.mark.parametrize("operation", ["start", "approve"])
async def test_revocation_during_slow_request_body_is_rechecked(app, operation):
    project = app.state.ledger.create_project(ProjectCreate(name="One", objective="Inspect"))
    async with owner_client(app) as owner, device_client(app) as device:
        pairing_id, code, _ = await pair(owner, device, project.id, allow_run_start=True)
        await approve(owner, device, pairing_id, code)
        pending = None
        if operation == "start":
            path = f"/api/companion/projects/{project.id}/runs"
            body = {"prompt": "Should never start"}
        else:
            run = app.state.ledger.create_run(project.id, "Wait for approval")
            pending = asyncio.create_task(app.state.permissions.request(
                run.id, "Write", {"file_path": "report.txt"}, agent_id=None, context={},
            ))
            for _ in range(30):
                if app.state.permissions.list_pending():
                    break
                await asyncio.sleep(0)
            request_id = app.state.permissions.list_pending()[0].id
            path = f"/api/companion/runs/{run.id}/permissions/{request_id}"
            body = {"decision": "allow_once"}

        async def slow_body():
            yield b" "
            revoked = await owner.post(f"/api/mobile/pairings/{pairing_id}/revoke")
            assert revoked.status_code == 200
            yield json.dumps(body).encode()

        try:
            response = await device.post(path, content=slow_body(), headers={
                "Content-Type": "application/json",
            })
            assert response.status_code == 403, response.text
            if operation == "start":
                assert app.state.ledger.list_runs(project.id) == []
            else:
                assert not pending.done()
        finally:
            if pending is not None:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            await app.state.runs.stop()


@pytest.mark.parametrize("headers", [
    {"Origin": "https://evil.example"}, {"Origin": "null"}, {"Origin": ""},
    {"X-Ophira-Companion": ""}, {"Sec-Fetch-Site": "cross-site"},
    {"Host": "localhost"}, {"Host": "companion.example.evil.example"},
    {"Content-Type": "text/plain"},
])
async def test_companion_rejects_csrf_and_host_confusion(app, headers):
    async with device_client(app) as device:
        response = await device.post("/api/companion/pair", headers=headers, json={})
        assert response.status_code == 403


async def test_companion_requires_https_and_never_accepts_url_or_js_tokens(app):
    async with device_client(app) as device:
        insecure = await device.get("http://companion.example/api/companion/session")
        assert insecure.status_code == 403
        assert (await device.get("/api/companion/session?token=do-not-accept")).status_code == 403
        response = await device.post("/api/companion/pair", json={
            "pairing_id": "invalid", "secret": "private-canary-value", "device_name": "Test",
        })
        assert response.status_code == 422
        assert "private-canary-value" not in response.text
        # Public HTTPS cannot turn back into the privileged local admin surface
        # by supplying localhost as Host, even behind a loopback TLS terminator.
        assert (await device.get("/api/projects", headers={"Host": "localhost"})).status_code == 403


def test_pairing_secret_is_single_use_hashed_and_expiring(tmp_path):
    store = CompanionStore(tmp_path / "devices")
    offered, secret = store.create(PairingCreate(project_ids=["project_one"]), None)
    claim = PairingClaim(pairing_id=offered.id, secret=secret, device_name="Phone")
    pending, cookie = store.claim(claim)
    with pytest.raises(CompanionError):
        store.claim(claim)
    record = store._path(offered.id).read_text()
    assert secret not in record and cookie.split(".")[1] not in record
    assert store._path(offered.id).stat().st_mode & 0o777 == 0o600
    with pytest.raises(CompanionError):
        store.decide(offered.id, PairingDecision(
            decision="approve", verification_code=pending.verification_code,
        ), None, now=offered.expires_at + timedelta(seconds=1))
    expired, expired_secret = store.create(
        PairingCreate(project_ids=["project_two"]), None,
        now=utc_now() - timedelta(hours=1),
    )
    with pytest.raises(CompanionError):
        store.claim(PairingClaim(
            pairing_id=expired.id, secret=expired_secret, device_name="Too late",
        ))
    assert store.owner_record(expired.id, None).view().status == PairingStatus.EXPIRED


def test_pairing_claim_is_single_use_across_real_processes(tmp_path):
    root = tmp_path / "devices"
    store = CompanionStore(root)
    offered, secret = store.create(PairingCreate(project_ids=["project_one"]), None)
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    results = context.Queue()
    payload = {"pairing_id": offered.id, "secret": secret, "device_name": "Phone"}
    workers = [context.Process(target=_claim_in_process, args=(root, payload, ready, results))
               for _ in range(2)]
    try:
        for worker in workers:
            worker.start()
        ready.set()
        assert sorted(results.get(timeout=20) for _ in workers) == ["claimed", "denied"]
        for worker in workers:
            worker.join(timeout=10)
            assert worker.exitcode == 0
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=10)
        results.close()


@pytest.mark.parametrize("change", [
    "role_downgrade", "membership_revoked", "tenant_changed", "current_assertion_role",
])
async def test_companion_rechecks_durable_project_authority(tmp_path, change):
    settings = Settings(
        _env_file=None, data_dir=tmp_path, companion_enabled=True,
        companion_public_origin=PUBLIC_ORIGIN, auth_mode="trusted_gateway",
        trusted_gateway_auth_secret="test-companion-gateway-secret-at-least-32-bytes",
        trusted_gateway_tenant_id="tenant_acme",
    )
    app = create_app(settings, runtime=WaitingRuntime())
    owner = PrincipalContext(
        tenant_id="tenant_acme", principal_id="owner", role=PrincipalRole.OWNER,
        session_id="gateway_session_owner", assertion_issued_at=utc_now(),
        assertion_expires_at=utc_now() + timedelta(minutes=5),
        assertion_nonce="nonce_owner_0000000000000001",
    )
    member = owner.model_copy(update={"principal_id": "member", "role": PrincipalRole.ADMIN})
    project = app.state.ledger.create_project(
        ProjectCreate(name="Authorized", objective="Inspect"), principal=owner,
    )
    access = app.state.project_access
    access.bind_project(project.id, owner)
    access.grant(project.id, owner, principal_id=member.principal_id, role=member.role)
    store = app.state.companions
    offered, secret = store.create(PairingCreate(
        project_ids=[project.id], allow_run_start=True,
    ), member)
    with pytest.raises(CompanionError):
        store.revoke(offered.id, owner)  # Shared project does not transfer device ownership.
    async with device_client(app) as device:
        claimed = await device.post("/api/companion/pair", json={
            "pairing_id": offered.id, "secret": secret, "device_name": "Member phone",
        })
        assert claimed.status_code == 200
        if change == "current_assertion_role":
            path = f"/api/mobile/pairings/{offered.id}/decision"
            headers = build_gateway_assertion_headers(
                settings.trusted_gateway_auth_secret.get_secret_value(),
                method="POST", path=path, tenant_id=member.tenant_id,
                principal_id=member.principal_id, role=PrincipalRole.VIEWER,
                session_id=member.session_id, issued_at=utc_now(),
                nonce="nonce_downgrade_0000000000000001",
            )
            denied = await device.post(path, json={
                "decision": "approve",
                "verification_code": claimed.json()["device"]["verification_code"],
            }, headers=headers)
            assert denied.status_code == 403, denied.text
            assert store.owner_record(offered.id, member).status == PairingStatus.PENDING
            return
        store.decide(offered.id, PairingDecision(
            decision="approve", verification_code=claimed.json()["device"]["verification_code"],
        ), member)
        assert (await device.get("/api/companion/session")).json()["status"] == "approved"
        assert len((await device.get("/api/companion/overview")).json()["projects"]) == 1
        assert (await device.get("/api/providers")).status_code == 401
        if change == "role_downgrade":
            access.grant(project.id, owner, principal_id=member.principal_id,
                         role=PrincipalRole.VIEWER)
        elif change == "membership_revoked":
            access.revoke(project.id, owner, principal_id=member.principal_id)
        else:
            settings.trusted_gateway_tenant_id = "tenant_changed"
        overview = (await device.get("/api/companion/overview")).json()
        assert overview["projects"] == [] and overview["runs"] == []
        denied = await device.post(f"/api/companion/projects/{project.id}/runs", json={
            "prompt": "No longer authorized",
        })
        assert denied.status_code == 403
        assert app.state.ledger.list_runs(project.id) == []


@pytest.mark.parametrize("origin", [
    "http://example.org", "https://example.org/path", "https://u:p@example.org",
    "https://@example.org", "https://:secret@example.org",
    "https://example.org#secret", "https://example.org?key=1", "https://example.org.evil/..",
])
def test_companion_config_requires_one_explicit_https_origin(origin):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, companion_enabled=True, companion_public_origin=origin)
