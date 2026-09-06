import asyncio
import base64
import hashlib
import json
import logging
import multiprocessing
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import http_ece
import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from pydantic import ValidationError
from test_companion import (
    PUBLIC_ORIGIN,
    WaitingRuntime,
    approve,
    device_client,
    owner_client,
    pair,
)

from ophira_backend.companion import PairingCreate, PairingDecision
from ophira_backend.config import Settings
from ophira_backend.constants import PrincipalRole
from ophira_backend.main import create_app
from ophira_backend.models import PrincipalContext, ProjectCreate, utc_now
from ophira_backend.push_service import PushDeliveryService
from ophira_backend.push_store import PushStore
from ophira_backend.push_transport import (
    WAKE_PAYLOAD,
    PushKeys,
    PushOutcome,
    PushSubscription,
    WebPushAdapter,
    decode_key,
)

ENDPOINT = "https://fcm.googleapis.com/wpush/very-private-capability"
AUTH = b"0123456789abcdef"
RECIPIENT = ec.derive_private_key(12345, ec.SECP256R1())


def b64(raw):
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def subscription(**changes):
    return PushSubscription.model_validate({
        "endpoint": ENDPOINT, "expirationTime": None,
        "keys": {"auth": b64(AUTH), "p256dh": b64(RECIPIENT.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint,
        ))}, **changes,
    })


def settings(tmp_path, **changes):
    return Settings(
        _env_file=None, data_dir=tmp_path, companion_enabled=True,
        companion_public_origin=PUBLIC_ORIGIN, push_enabled=True,
        push_vapid_private_key=b64((98765).to_bytes(32, "big")),
        push_vapid_subject="mailto:push-operator@example.com", **changes,
    )


class RecordingAdapter(WebPushAdapter):
    def __init__(self, config):
        super().__init__(config)
        self.calls = []
        self.outcomes = []
        self.wait = None

    async def send(self, sub, topic):
        self.calls.append((sub, topic))
        if self.wait:
            await self.wait.wait()
        return (
            self.outcomes.pop(0) if self.outcomes else PushOutcome("accepted", "service_accepted")
        )


@pytest.fixture
def app(tmp_path):
    config = settings(tmp_path)
    return create_app(config, runtime=WaitingRuntime(), push_adapter=RecordingAdapter(config))


async def paired(app, owner, device):
    project = app.state.ledger.create_project(ProjectCreate(name="Allowed", objective="Review"))
    device_id, code, _ = await pair(owner, device, project.id)
    await approve(owner, device, device_id, code)
    response = await device.post(
        "/api/companion/push/subscription", json=subscription().model_dump(),
    )
    assert response.status_code == 200, response.text
    return project, device_id


@pytest.mark.parametrize("endpoint", [
    "http://fcm.googleapis.com/push/key", "https://user@fcm.googleapis.com/push/key",
    "https://@fcm.googleapis.com/push/key", "https://fcm.googleapis.com:8443/push/key",
    "https://fcm.googleapis.com./push/key", "https://fcm.googleapis.com/push/key#fragment",
    "https://fcm.googleapis.com/push/secret\n", "https://fcm.googleapis.com\\@localhost/push",
    "https://fcm.googleapis.com%2fevil.com/push", "https://ＦＣＭ.googleapis.com/push",
])
def test_subscription_rejects_ambiguous_destinations(endpoint):
    with pytest.raises(ValidationError):
        subscription(endpoint=endpoint)


@pytest.mark.parametrize("keys", [
    {"auth": b64(b"short"), "p256dh": subscription().keys.p256dh},
    {"auth": b64(AUTH), "p256dh": b64(b"\x04" + b"\x00" * 64)},
    {"auth": b64(AUTH), "p256dh": b64(b"\x05" + b"\x00" * 64)},
])
def test_subscription_keys_validate_real_curve_and_auth(keys):
    with pytest.raises(ValidationError):
        PushKeys.model_validate(keys)


def test_configuration_key_and_origin_fail_closed(tmp_path):
    config = settings(tmp_path)
    assert WebPushAdapter(config).enabled
    config.push_vapid_private_key = None
    assert not WebPushAdapter(config).enabled
    with pytest.raises(ValidationError):
        settings(tmp_path, push_allowed_origins=["https://fcm.googleapis.com/path"])
    adapter = WebPushAdapter(settings(tmp_path))
    for endpoint in ["https://127.0.0.1/push", "https://evil.example/push"]:
        with pytest.raises(ValueError, match="origin"):
            adapter.validate(subscription(endpoint=endpoint))
    with pytest.raises(ValueError, match="expired"):
        adapter.validate(subscription(expirationTime=1))


async def test_standard_encrypted_payload_and_verified_vapid_with_pinned_tls(
    tmp_path, monkeypatch, caplog,
):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr("ophira_backend.web_tools._resolve_ips", lambda _host: ["1.1.1.1"])
    captured = []

    def handle(request):
        captured.append(request)
        return httpx.Response(201)

    adapter = WebPushAdapter(settings(tmp_path), transport=httpx.MockTransport(handle))
    assert (await adapter.send(subscription(), "safe-topic")).status == "accepted"
    request = captured[0]
    assert request.url.host == "1.1.1.1"
    assert request.headers["Host"] == "fcm.googleapis.com"
    assert request.extensions["sni_hostname"] == "fcm.googleapis.com"
    assert request.headers["TTL"] == "300"
    assert request.headers["Content-Encoding"] == "aes128gcm"
    assert AUTH not in request.content and b"project" not in request.content
    assert http_ece.decrypt(
        request.content, private_key=RECIPIENT, auth_secret=AUTH, version="aes128gcm",
    ) == WAKE_PAYLOAD
    token, public = request.headers["Authorization"].removeprefix("vapid t=").split(",k=")
    head, payload, signature = token.split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    assert claims["aud"] == "https://fcm.googleapis.com"
    assert claims["sub"] == "mailto:push-operator@example.com"
    assert 0 < claims["exp"] - utc_now().timestamp() <= 43_200
    raw = decode_key(signature, 64)
    signing_key = ec.EllipticCurvePublicKey.from_encoded_point(
        ec.SECP256R1(), decode_key(public, 65),
    )
    signing_key.verify(
        encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")),
        f"{head}.{payload}".encode(), ec.ECDSA(hashes.SHA256()),
    )
    assert "very-private-capability" not in caplog.text
    assert b64(AUTH) not in caplog.text
    assert token not in caplog.text
    # The contextual filter must not hide unrelated diagnostics after a send.
    logging.getLogger("httpx").info("ordinary request remains observable")
    assert "ordinary request remains observable" in caplog.text


@pytest.mark.parametrize("status, expected", [
    (201, "accepted"), (302, "failed"), (400, "failed"), (401, "disabled"),
    (403, "disabled"), (404, "disabled"), (410, "disabled"), (429, "retry"), (503, "retry"),
])
async def test_transport_bounded_statuses_and_no_redirects(tmp_path, monkeypatch, status, expected):
    monkeypatch.setattr("ophira_backend.web_tools._resolve_ips", lambda _host: ["1.1.1.1"])
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(status, headers={"Location": "https://127.0.0.1/secret",
                                              "Retry-After": "999999"})

    outcome = await WebPushAdapter(
        settings(tmp_path), transport=httpx.MockTransport(handle),
    ).send(subscription(), "topic")
    assert outcome.status == expected
    assert outcome.retry_after <= 3_600
    assert len(calls) == 1


async def test_private_or_mixed_dns_never_receives_a_push(tmp_path, monkeypatch):
    def forbidden(_request):
        pytest.fail("Private destination must not reach transport")

    adapter = WebPushAdapter(settings(tmp_path), transport=httpx.MockTransport(forbidden))
    monkeypatch.setattr("ophira_backend.web_tools._resolve_ips", lambda _host: ["1.1.1.1", "::1"])
    result = await adapter.send(subscription(), "topic")
    assert result == PushOutcome("retry", "destination_unavailable")


@pytest.mark.parametrize("retry_after, expected", [
    ("Sat, 05 Sep 2026 12:02:00 GMT", 120),
    ("Sat, 05 Sep 2026 13:30:00 GMT", 3_600),
    ("Sat, 05 Sep 2026 11:59:00 GMT", 0),
    ("999999999999", 3_600),
    ("-1", 0),
    ("not a date", 0),
])
async def test_transport_honors_both_retry_after_formats(
    tmp_path, monkeypatch, retry_after, expected,
):
    monkeypatch.setattr("ophira_backend.web_tools._resolve_ips", lambda _host: ["1.1.1.1"])
    monkeypatch.setattr(
        "ophira_backend.push_transport.utc_now", lambda: datetime(2026, 9, 5, 12, tzinfo=UTC),
    )
    adapter = WebPushAdapter(settings(tmp_path), transport=httpx.MockTransport(
        lambda _request: httpx.Response(503, headers={"Retry-After": retry_after}),
    ))
    assert await adapter.send(subscription(), "topic") == PushOutcome(
        "retry", "service_busy", expected,
    )


def test_existing_push_directory_is_made_private(tmp_path):
    directory = tmp_path / "restored-push"
    directory.mkdir(mode=0o755)
    directory.chmod(0o755)
    PushStore(directory)
    assert directory.stat().st_mode & 0o777 == 0o700


async def test_push_http_scope_no_secrets_idempotence_and_revoke(app):
    async with owner_client(app) as owner, device_client(app) as device:
        assert (await device.get("/api/companion/push/status")).status_code == 401
        project, device_id = await paired(app, owner, device)
        push = app.state.companion_push
        state = push.store.get(device_id)
        status = (await device.get("/api/companion/push/status")).json()
        assert status["enabled"] and status["subscribed"]
        assert status["subscription_fingerprint"] == hashlib.sha256(ENDPOINT.encode()).hexdigest()
        assert ENDPOINT not in json.dumps(status) and b64(AUTH) not in json.dumps(status)
        assert (push.store.root.stat().st_mode & 0o777) == 0o700
        assert (push.store._path(device_id).stat().st_mode & 0o777) == 0o600
        assert (await device.post("/api/companion/push/subscription", json={
            **subscription().model_dump(), "project_id": project.id,
        })).status_code == 422
        invalid = subscription().model_dump()
        invalid["keys"]["auth"] = "VERY_SECRET_INVALID_AUTH"
        response = await device.post("/api/companion/push/subscription", json=invalid)
        assert response.status_code == 422 and "VERY_SECRET" not in response.text
        await device.post("/api/companion/push/subscription", json=subscription().model_dump())
        assert push.store.get(device_id).generation == state.generation
        await owner.post(f"/api/mobile/pairings/{device_id}/revoke")
        assert push.store.get(device_id).subscription is None
        assert not push.store.save(state)
        assert (await device.get("/api/companion/push/status")).status_code == 403


async def test_wakes_only_new_authorized_unread_items_and_persist_dedupe(app):
    inbox = app.state.notifications
    other = app.state.ledger.create_project(ProjectCreate(name="Other", objective="Private"))
    async with owner_client(app) as owner, device_client(app) as device:
        project, device_id = await paired(app, owner, device)
        push = app.state.companion_push
        inbox.add(title="Private", project_id=other.id)
        inbox.add(title="Global", project_id=None)
        await push.tick()
        assert not push.adapter.calls
        for _ in range(3):
            inbox.add(title="PRIVATE BUSINESS CONTENT", body="NOT IN PUSH", project_id=project.id)
        await push.tick()
        assert len(push.adapter.calls) == 1
        persisted = push.store.get(device_id)
        assert persisted.wake.state == "accepted"
        assert persisted.last_accepted_at is not None
        assert "PRIVATE BUSINESS CONTENT" not in persisted.model_dump_json()
        # Independent store/service reload does not resend existing unread rows.
        reloaded = PushDeliveryService(
            push.settings, PushStore(push.store.root), push.adapter, push.companions,
            push.ledger, push.access, push.permissions, push.notifications,
        )
        await reloaded.tick(now=utc_now() + timedelta(seconds=60))
        assert len(push.adapter.calls) == 1


async def test_retry_durable_budget_and_resolved_updates_cancel(app):
    async with owner_client(app) as owner, device_client(app) as device:
        project, device_id = await paired(app, owner, device)
        push = app.state.companion_push
        push.adapter.outcomes = [PushOutcome("retry", "service_busy", 60)]
        notification = app.state.notifications.add(title="Wake", project_id=project.id)
        now = utc_now()
        await push.tick(now=now)
        record = push.store.get(device_id)
        assert record.wake.state == "pending" and record.wake.attempts == 1
        await push.tick(now=now + timedelta(seconds=30))
        assert len(push.adapter.calls) == 1
        await push.tick(now=now + timedelta(seconds=61))
        assert len(push.adapter.calls) == 2
        assert push.adapter.calls[0][1] == push.adapter.calls[1][1]
        assert push.store.get(device_id).wake.state == "accepted"
        notification2 = app.state.notifications.add(title="Another", project_id=project.id)
        push.adapter.outcomes = [PushOutcome("retry", "service_busy", 60)]
        await push.tick(now=now + timedelta(seconds=80))
        app.state.notifications.mark_read(notification.id)
        app.state.notifications.mark_read(notification2.id)
        await push.tick(now=now + timedelta(seconds=160))
        assert len(push.adapter.calls) == 3
        assert push.store.get(device_id).wake.state == "cancelled"


async def test_disabled_subscription_scrubbed_and_no_automatic_renewal(app):
    async with owner_client(app) as owner, device_client(app) as device:
        project, device_id = await paired(app, owner, device)
        push = app.state.companion_push
        push.adapter.outcomes = [PushOutcome("disabled", "subscription_rejected")]
        app.state.notifications.add(title="Wake", project_id=project.id)
        await push.tick()
        assert push.store.get(device_id).subscription is None
        assert "very-private-capability" not in push.store._path(device_id).read_text()
        await push.tick(now=utc_now() + timedelta(seconds=60))
        assert len(push.adapter.calls) == 1
        assert not (await device.get("/api/companion/push/status")).json()["subscribed"]


async def test_unsubscribe_during_inflight_send_cannot_be_undone(app):
    async with owner_client(app) as owner, device_client(app) as device:
        project, device_id = await paired(app, owner, device)
        push = app.state.companion_push
        app.state.notifications.add(title="Wake", project_id=project.id)
        push.adapter.wait = asyncio.Event()
        task = asyncio.create_task(push.tick())
        for _ in range(50):
            if push.adapter.calls:
                break
            await asyncio.sleep(0)
        assert push.adapter.calls
        # A second worker observes the lease and cannot send this same wake.
        await push.tick()
        assert len(push.adapter.calls) == 1
        await device.post("/api/companion/push/unsubscribe", json={})
        push.adapter.wait.set()
        await task
        assert push.store.get(device_id).subscription is None
        assert push.store.get(device_id).wake.state == "cancelled"


async def test_permissions_only_pending_unexpired_in_project_can_wake(app, monkeypatch):
    async with owner_client(app) as owner, device_client(app) as device:
        project, _ = await paired(app, owner, device)
        push = app.state.companion_push
        now = utc_now()
        items = [SimpleNamespace(
            id="permission", project_id=project.id, state="resolved", created_at=now,
            expires_at=now + timedelta(hours=1),
        )]
        monkeypatch.setattr(push.permissions, "list_pending", lambda: items)
        await push.tick()
        assert not push.adapter.calls
        items[0].state = "pending"
        items[0].expires_at = now - timedelta(seconds=1)
        await push.tick()
        assert not push.adapter.calls
        items[0].expires_at = now + timedelta(hours=1)
        await push.tick()
        assert len(push.adapter.calls) == 1


def _lease_process(root, device_id, acquired, release):
    with PushStore(root).delivery_lease(device_id) as owned:
        acquired.put(owned)
        release.wait(10)


def test_delivery_lease_is_cross_process(tmp_path):
    store = PushStore(tmp_path / "push")
    device_id = "pair_" + "a" * 32
    ctx = multiprocessing.get_context("spawn")
    acquired, release = ctx.Queue(), ctx.Event()
    process = ctx.Process(target=_lease_process, args=(store.root, device_id, acquired, release))
    process.start()
    try:
        assert acquired.get(timeout=10) is True
        with store.delivery_lease(device_id) as owned:
            assert owned is False
    finally:
        release.set()
        process.join(10)
    assert process.exitcode == 0
    with store.delivery_lease(device_id) as owned:
        assert owned is True


async def test_daily_budget_survives_subscription_replacement(app):
    async with owner_client(app) as owner, device_client(app) as device:
        project, device_id = await paired(app, owner, device)
        push = app.state.companion_push
        push.settings.push_max_attempts_per_day = 1
        app.state.notifications.add(title="First", project_id=project.id)
        await push.tick()
        replacement = subscription(endpoint=ENDPOINT + "-renewed")
        push.subscribe(device_id, replacement)
        app.state.notifications.add(title="Second", project_id=project.id)
        await push.tick(now=utc_now() + timedelta(seconds=30))
        assert len(push.adapter.calls) == 1
        record = push.store.get(device_id)
        assert record.wake.reason == "daily_attempt_budget_exhausted"
        assert record.daily_attempts == 1


@pytest.mark.parametrize("replace_subscription", [False, True])
@pytest.mark.parametrize("end_wake", ["cancel", "expire"])
async def test_retry_after_survives_wake_cancellation_and_subscription_replacement(
    app, replace_subscription, end_wake,
):
    async with owner_client(app) as owner, device_client(app) as device:
        project, device_id = await paired(app, owner, device)
        push = app.state.companion_push
        delay = 180 if end_wake == "cancel" else 480
        push.adapter.outcomes = [PushOutcome("retry", "service_busy", delay)]
        first = app.state.notifications.add(title="First", project_id=project.id)
        now = utc_now()
        await push.tick(now=now)
        if end_wake == "cancel":
            app.state.notifications.mark_read(first.id)
        ended_at = 20 if end_wake == "cancel" else 301
        await push.tick(now=now + timedelta(seconds=ended_at))
        assert push.store.get(device_id).wake.state == (
            "cancelled" if end_wake == "cancel" else "failed"
        )
        if replace_subscription:
            push.subscribe(device_id, subscription(endpoint=ENDPOINT + "-renewed"))
        app.state.notifications.add(title="New update", project_id=project.id)
        reloaded = PushDeliveryService(
            push.settings, PushStore(push.store.root), push.adapter, push.companions,
            push.ledger, push.access, push.permissions, push.notifications,
        )
        await reloaded.tick(now=now + timedelta(seconds=ended_at + 1))
        assert len(push.adapter.calls) == 1
        await reloaded.tick(now=now + timedelta(seconds=delay - 1))
        assert len(push.adapter.calls) == 1
        await reloaded.tick(now=now + timedelta(seconds=delay + 1))
        assert len(push.adapter.calls) == 2
        assert push.store.get(device_id).wake.state == "accepted"


async def test_retry_after_begins_when_service_responds(app, monkeypatch):
    async with owner_client(app) as owner, device_client(app) as device:
        project, device_id = await paired(app, owner, device)
        push = app.state.companion_push
        push.adapter.outcomes = [PushOutcome("retry", "service_busy", 180)]
        app.state.notifications.add(title="Wake", project_id=project.id)
        times = iter([10.0, 17.0])
        monkeypatch.setattr("ophira_backend.push_service.monotonic", lambda: next(times))
        now = utc_now()
        await push.tick(now=now)
        record = push.store.get(device_id)
        assert record.retry_not_before == now + timedelta(seconds=187)
        assert record.wake.next_attempt_at == record.retry_not_before


async def test_attempting_crash_retains_budget_and_retries_only_with_live_lease(app):
    async with owner_client(app) as owner, device_client(app) as device:
        project, device_id = await paired(app, owner, device)
        push = app.state.companion_push
        app.state.notifications.add(title="Wake", project_id=project.id)
        push.adapter.wait = asyncio.Event()
        task = asyncio.create_task(push.tick())
        for _ in range(50):
            if push.adapter.calls:
                break
            await asyncio.sleep(0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        record = push.store.get(device_id)
        assert record.wake.state == "attempting" and record.wake.attempts == 1
        push.adapter.wait = None
        await push.tick(now=record.wake.next_attempt_at + timedelta(seconds=1))
        assert push.store.get(device_id).wake.state == "accepted"
        assert push.store.get(device_id).wake.attempts == 2
        assert push.adapter.calls[0][1] == push.adapter.calls[1][1]


async def test_revocation_while_reading_subscription_body_is_rechecked(app):
    async with owner_client(app) as owner, device_client(app) as device:
        _, device_id = await paired(app, owner, device)

        async def delayed():
            yield b" "
            await owner.post(f"/api/mobile/pairings/{device_id}/revoke")
            yield subscription().model_dump_json().encode()

        response = await device.post(
            "/api/companion/push/subscription", content=delayed(),
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 403
        assert app.state.companion_push.store.get(device_id).subscription is None


def test_replaced_subscription_is_not_scrubbed_by_an_old_worker(tmp_path):
    store = PushStore(tmp_path)
    device_id = "pair_" + "a" * 32
    old = store.subscribe(device_id, subscription(), "public-key")
    new = store.subscribe(device_id, subscription(endpoint=ENDPOINT + "-new"), "public-key")
    store.unsubscribe(device_id, "expired", generation=old.generation)
    assert store.get(device_id).generation == new.generation
    assert store.get(device_id).subscription is not None
    assert not store.save(old)


def test_equivalent_push_endpoint_cannot_bind_two_devices(tmp_path):
    store = PushStore(tmp_path)
    original = subscription()
    alternate = subscription(endpoint=ENDPOINT.replace(
        "https://fcm.googleapis.com/", "https://FCM.GOOGLEAPIS.COM:443/",
    ))
    assert original.destination_identity == alternate.destination_identity
    assert original.fingerprint != alternate.fingerprint
    store.subscribe("pair_" + "a" * 32, original, "public-key")
    with pytest.raises(ValueError, match="already bound"):
        store.subscribe("pair_" + "b" * 32, alternate, "public-key")


async def test_filter_project_scope_before_notification_limit(app):
    async with owner_client(app) as owner, device_client(app) as device:
        project, _ = await paired(app, owner, device)
        other = app.state.ledger.create_project(ProjectCreate(name="Other", objective="Private"))
        app.state.notifications.add(title="Allowed", project_id=project.id)
        for _ in range(205):
            app.state.notifications.add(title="Not allowed", project_id=other.id)
        await app.state.companion_push.tick()
        assert len(app.state.companion_push.adapter.calls) == 1


@pytest.mark.parametrize("change", ["membership_revoked", "role_changed", "tenant_changed"])
async def test_delivery_revalidates_authenticated_principal_scope(tmp_path, change):
    config = settings(
        tmp_path, auth_mode="trusted_gateway", trusted_gateway_tenant_id="tenant_one",
        trusted_gateway_auth_secret="test-only-gateway-secret-longer-than-32-bytes",
    )
    app = create_app(config, runtime=WaitingRuntime(), push_adapter=RecordingAdapter(config))
    owner = PrincipalContext(
        tenant_id="tenant_one", principal_id="owner", role=PrincipalRole.OWNER,
        session_id="owner_session_000001", assertion_issued_at=utc_now(),
        assertion_expires_at=utc_now() + timedelta(minutes=1),
        assertion_nonce="nonce_push_0000000000000000001",
    )
    member = owner.model_copy(update={"principal_id": "member", "role": PrincipalRole.ADMIN})
    project = app.state.ledger.create_project(
        ProjectCreate(name="Allowed", objective="Inspect"), principal=owner,
    )
    app.state.project_access.bind_project(project.id, owner)
    app.state.project_access.grant(
        project.id, owner, principal_id=member.principal_id, role=member.role,
    )
    offer, secret = app.state.companions.create(PairingCreate(project_ids=[project.id]), member)
    async with device_client(app) as device:
        claimed = await device.post("/api/companion/pair", json={
            "pairing_id": offer.id, "secret": secret, "device_name": "Member phone",
        })
        app.state.companions.decide(offer.id, PairingDecision(
            decision="approve", verification_code=claimed.json()["device"]["verification_code"],
        ), member)
        assert (await device.get("/api/companion/session")).json()["status"] == "approved"
        assert (await device.post(
            "/api/companion/push/subscription", json=subscription().model_dump(),
        )).status_code == 200
        app.state.notifications.add(title="Private update", project_id=project.id)
        if change == "membership_revoked":
            app.state.project_access.revoke(project.id, owner, principal_id=member.principal_id)
        elif change == "role_changed":
            app.state.project_access.grant(
                project.id, owner, principal_id=member.principal_id, role=PrincipalRole.VIEWER,
            )
        else:
            config.trusted_gateway_tenant_id = "tenant_changed"
        await app.state.companion_push.tick()
        assert not app.state.companion_push.adapter.calls
        assert app.state.companion_push.store.get(offer.id).subscription is None
        assert (await device.post(
            "/api/companion/push/subscription", json=subscription().model_dump(),
        )).status_code == 403
