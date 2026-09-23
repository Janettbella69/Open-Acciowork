"""SHEIN consent and exchange contracts, using only synthetic credentials."""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import multiprocessing
import os
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from test_auth_api import FakeRuntime, request_as, trusted_settings
from test_marketplace_access import service_at
from test_shein import CREDENTIALS, SHOP_ID, STORE, configure, response

from ophira_backend import web_tools
from ophira_backend.connectors import ConnectorRegistry, ConnectorUnavailableError
from ophira_backend.constants import PrincipalRole
from ophira_backend.main import create_app
from ophira_backend.marketplace_access import MarketplaceGrantRequest
from ophira_backend.marketplaces import MarketplaceError, MarketplaceReadRequest
from ophira_backend.models import PrincipalContext, ProjectCreate, utc_now
from ophira_backend.secret_vault import FileSecretVault
from ophira_backend.shein import (
    SHEIN_AUTH_PATH,
    SHEIN_ORIGIN,
    SHEIN_STORE_PATH,
    SHEIN_STORE_SCOPE,
    parse_shein_credentials,
)
from ophira_backend.shein_authorization import (
    BeginSheinAuthorization,
    FinishSheinAuthorization,
    SheinAuthorizationService,
    SheinAuthorizationState,
    decrypt_shein_secret,
)

APP_ID = "synthetic-app-id"
APP_SECRET = "synthetic-app-secret-1234567890"
TEMP_TOKEN = "synthetic-temporary-token"
SELLER_KEY = "synthetic-new-seller-key"
OPEN_KEY = "synthetic-new-open-key"


def encrypt(value=SELLER_KEY):
    padder = padding.PKCS7(128).padder()
    data = padder.update(value.encode()) + padder.finalize()
    cipher = Cipher(
        algorithms.AES(APP_SECRET.encode()[:16]), modes.CBC(b"space-station-default-iv"[:16])
    ).encryptor()
    return base64.b64encode(cipher.update(data) + cipher.finalize()).decode()


@pytest.fixture
def service(tmp_path, monkeypatch):
    access = service_at(tmp_path)
    configure(access.connectors)
    monkeypatch.setattr(web_tools, "_resolve_ips", lambda _: ["93.184.216.34"])
    return SheinAuthorizationService(access)


def begin(service, *, principal=None, **changes):
    payload = BeginSheinAuthorization(
        **{
            "app_id": APP_ID,
            "endpoint": SHEIN_ORIGIN,
            "allow_products": True,
            "redirect_origin": "http://localhost:5173",
            **changes,
        }
    )
    result = service.begin("shein", payload, principal)
    params = parse_qs(urlsplit(result["authorization_url"]).fragment.split("?", 1)[1])
    return params["state"][0], result


def completion(state, **changes):
    return FinishSheinAuthorization(
        **{
            "state": state,
            "app_id": APP_ID,
            "app_secret": APP_SECRET,
            "temp_token": TEMP_TOKEN,
            **changes,
        }
    )


def handler_for(state, calls):
    def handler(request):
        calls.append(request)
        if request.url.path == SHEIN_AUTH_PATH:
            return response(
                {
                    "appid": APP_ID,
                    "state": state,
                    "supplierId": int(SHOP_ID),
                    "openKeyId": OPEN_KEY,
                    "secretKey": encrypt(),
                }
            )
        assert request.url.path == SHEIN_STORE_PATH
        return response(STORE)

    return handler


def transport(service, handler):
    service.access.reader.transport_factory = lambda: httpx.MockTransport(handler)


def test_official_manual_aes_vector():
    assert (
        decrypt_shein_secret("lr4JDSqQkRyZ2YAosX3ZRQ==", "14ABE7A4222647CB945DADC76740BA73")
        == "Hello World"
    )


@pytest.mark.parametrize(
    "ciphertext",
    ["not base64!", "AA==", "", "a" * 1025, None, base64.b64encode(b"x" * 16).decode()],
)
def test_invalid_ciphertext_is_rejected_without_echo(ciphertext):
    with pytest.raises(MarketplaceError) as failure:
        decrypt_shein_secret(ciphertext, APP_SECRET)
    assert APP_SECRET not in str(failure.value)


def test_consent_url_uses_fixed_host_encoded_callback_random_state_and_no_secrets(service):
    previous = service.connectors.get("shein")
    state, result = begin(service, endpoint="https://openapi.sheincorp.cn", allow_products=False)
    other, _ = begin(service)
    assert state != other and len(state) == 97
    parsed = urlsplit(result["authorization_url"])
    assert parsed.scheme == "https" and parsed.netloc == "openapi-sem.sheincorp.com"
    params = parse_qs(parsed.fragment.split("?", 1)[1])
    assert params["appid"] == [APP_ID]
    assert base64.b64decode(params["redirectUrl"][0]).decode() == (
        "http://localhost:5173/app/connectors/shein-authorize/shein"
    )
    assert service.connectors.get("shein") == previous
    raw = "".join(p.read_text() for p in service.root.glob("*.json"))
    assert state not in raw and APP_SECRET not in raw and TEMP_TOKEN not in raw
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in service.root.glob("*.json"))


@pytest.mark.parametrize(
    "origin",
    [
        "https://evil.test",
        "http://localhost:5173/",
        "null",
        "http://localhost:5173@evil.test",
        "file:///tmp",
        "*",
    ],
)
def test_callback_origin_cannot_be_selected_outside_deployment(service, origin):
    with pytest.raises(MarketplaceError):
        begin(service, redirect_origin=origin)
    assert not list(service.root.glob("*.json"))


async def test_exchange_signs_app_headers_verifies_seller_and_installs_bound_keys(service, caplog):
    state, _ = begin(service, endpoint="https://openapi.sheincorp.cn", allow_products=False)
    calls = []
    transport(service, handler_for(state, calls))
    with caplog.at_level(logging.INFO):
        result = await service.finish("shein", completion(state), None)
    assert result.endpoint == "https://openapi.sheincorp.cn" and result.scope == SHEIN_STORE_SCOPE
    assert len(calls) == 2
    request, profile = calls
    assert request.url.host == "93.184.216.34" and not request.url.query
    assert request.headers["host"] == "openapi.sheincorp.cn"
    assert request.extensions["sni_hostname"] == "openapi.sheincorp.cn"
    assert "x-lt-openKeyId" not in request.headers and request.headers["x-lt-appid"] == APP_ID
    assert json.loads(request.content) == {"tempToken": TEMP_TOKEN}
    nonce = request.headers["x-lt-signature"][:5]
    digest = hmac.new(
        (APP_SECRET + nonce).encode(),
        f"{APP_ID}&{request.headers['x-lt-timestamp']}&{SHEIN_AUTH_PATH}".encode(),
        hashlib.sha256,
    ).hexdigest()
    assert request.headers["x-lt-signature"] == nonce + base64.b64encode(digest.encode()).decode()
    assert profile.headers["x-lt-openKeyId"] == OPEN_KEY and "x-lt-appid" not in profile.headers
    saved = parse_shein_credentials(service.connectors.vault.get("shein"))
    assert saved.secret_key.get_secret_value() == SELLER_KEY and saved.supplier_id == SHOP_ID
    assert saved.bound_scope == SHEIN_STORE_SCOPE
    assert saved.authorization_id == state.split(".")[0]
    exposed = (
        result.model_dump_json()
        + caplog.text
        + "".join(p.read_text() for p in service.root.glob("*.json"))
    )
    assert all(
        secret not in exposed for secret in [APP_SECRET, TEMP_TOKEN, SELLER_KEY, OPEN_KEY, state]
    )
    assert service.view("shein", SheinAuthorizationState(state=state), None)["status"] == "complete"
    # A lost completion response cannot cause another remote exchange.
    again = await service.finish("shein", FinishSheinAuthorization(state=state), None)
    assert again == result and len(calls) == 2


@pytest.mark.parametrize(
    "field,value",
    [
        ("appid", "another-app"),
        ("state", "another-state"),
        ("supplierId", True),
        ("supplierId", 0),
        ("openKeyId", []),
        ("secretKey", "broken"),
    ],
)
async def test_mismatched_exchange_never_replaces_existing_connection(service, field, value):
    state, _ = begin(service)
    original = service.connectors.vault.get("shein")

    def handler(_request):
        return response(
            {
                "appid": APP_ID,
                "state": state,
                "supplierId": int(SHOP_ID),
                "openKeyId": OPEN_KEY,
                "secretKey": encrypt(),
                field: value,
            }
        )

    transport(service, handler)
    with pytest.raises(MarketplaceError):
        await service.finish("shein", completion(state), None)
    assert service.connectors.vault.get("shein") == original


async def test_decrypted_key_requires_matching_live_seller_profile(service):
    state, _ = begin(service)
    base = handler_for(state, [])
    transport(
        service,
        lambda request: (
            response({"storeInfo": {"supplierId": 99}})
            if request.url.path == SHEIN_STORE_PATH
            else base(request)
        ),
    )
    with pytest.raises(MarketplaceError, match="does not match"):
        await service.finish("shein", completion(state), None)
    assert json.loads(service.connectors.vault.get("shein")) == CREDENTIALS


@pytest.mark.parametrize(
    "mutation", ["revoke", "same_key", "configuration", "cancel", "supersede", "expiry"]
)
@pytest.mark.parametrize("phase", [SHEIN_AUTH_PATH, SHEIN_STORE_PATH])
async def test_every_network_boundary_rechecks_authority(service, mutation, phase):
    state, _ = begin(service)
    calls = []
    base = handler_for(state, calls)

    def handler(request):
        result = base(request)
        if request.url.path == phase:
            if mutation == "revoke":
                service.connectors.revoke("shein")
            elif mutation == "same_key":
                service.connectors.store_secret("shein", json.dumps(CREDENTIALS))
            elif mutation == "configuration":
                configure(service.connectors, scope=SHEIN_STORE_SCOPE)
            elif mutation == "cancel":
                service.cancel("shein", SheinAuthorizationState(state=state), None)
            elif mutation == "supersede":
                begin(service)
            else:
                now = service.clock()
                service.clock = lambda: now + 1801
        return result

    transport(service, handler)
    with pytest.raises(MarketplaceError):
        await service.finish("shein", completion(state), None)
    saved = service.connectors.vault.get("shein")
    assert saved is None or SELLER_KEY not in saved
    assert len(calls) == (1 if phase == SHEIN_AUTH_PATH else 2)


async def test_concurrent_completion_claim_and_restart_never_repeat_exchange(service, tmp_path):
    state, _ = begin(service)
    started, release = asyncio.Event(), asyncio.Event()
    calls = []
    base = handler_for(state, calls)

    async def handler(request):
        if request.url.path == SHEIN_AUTH_PATH:
            started.set()
            await release.wait()
        return base(request)

    transport(service, handler)
    pending = asyncio.create_task(service.finish("shein", completion(state), None))
    await started.wait()
    other = SheinAuthorizationService(service_at(tmp_path))
    transport(other, lambda _: pytest.fail("An existing claim cannot repeat the exchange"))
    with pytest.raises(MarketplaceError, match="previous exchange"):
        await other.finish("shein", completion(state), None)
    release.set()
    await pending
    assert len(calls) == 2


async def test_transport_failure_is_ambiguous_and_claim_survives_restart(service, tmp_path):
    state, _ = begin(service)

    def broken(request):
        raise httpx.ReadError(APP_SECRET, request=request)

    transport(service, broken)
    with pytest.raises(MarketplaceError, match="interrupted") as error:
        await service.finish("shein", completion(state), None)
    assert APP_SECRET not in str(error.value)
    other = SheinAuthorizationService(service_at(tmp_path))
    transport(other, lambda _: pytest.fail("No ambiguous retry"))
    with pytest.raises(MarketplaceError, match="previous exchange"):
        await other.finish("shein", completion(state), None)


@pytest.mark.parametrize("change", ["state", "connector", "expiry", "superseded"])
async def test_invalid_callback_never_contacts_shein(service, change):
    state, _ = begin(service)
    connector_id = "shein"
    if change == "state":
        state = state[:-1] + ("a" if state[-1] != "a" else "b")
    elif change == "connector":
        connector_id = "shopify"
    elif change == "expiry":
        now = service.clock()
        service.clock = lambda: now + 1801
    else:
        begin(service)
    transport(service, lambda _: pytest.fail("Invalid callbacks must not exchange tokens"))
    with pytest.raises(MarketplaceError):
        await service.finish(connector_id, completion(state), None)
    assert json.loads(service.connectors.vault.get("shein")) == CREDENTIALS


def crash_authorization_process(root, state, phase):
    service = SheinAuthorizationService(service_at(root))
    web_tools._resolve_ips = lambda _: ["93.184.216.34"]
    transport(service, handler_for(state, []))
    save = service._save
    publish = service.connectors._write

    def save_then_exit(flow):
        if phase == "receipt" and flow.status == "complete":
            os._exit(73)
        save(flow)
        if phase == "claim" and flow.status == "exchanging":
            os._exit(73)

    def exit_before_publication(connector):
        if phase == "metadata" and connector.endpoint.endswith(".cn"):
            os._exit(73)
        publish(connector)

    service._save = save_then_exit
    service.connectors._write = exit_before_publication
    asyncio.run(service.finish("shein", completion(state), None))


@pytest.mark.parametrize("phase", ["claim", "metadata", "receipt"])
async def test_process_exit_preserves_claim_or_recovers_installation_without_replay(
    service,
    tmp_path,
    phase,
):
    state, _ = begin(service, endpoint="https://openapi.sheincorp.cn")
    worker = multiprocessing.get_context("spawn").Process(
        target=crash_authorization_process,
        args=(tmp_path, state, phase),
    )
    worker.start()
    try:
        await asyncio.to_thread(worker.join, timeout=15)
        assert worker.exitcode == 73
    finally:
        if worker.is_alive():
            worker.kill()
            await asyncio.to_thread(worker.join, timeout=5)
    other = SheinAuthorizationService(service_at(tmp_path))
    transport(other, lambda _: pytest.fail("A saved attempt cannot repeat remote exchange"))
    if phase == "claim":
        with pytest.raises(MarketplaceError, match="previous exchange"):
            await other.finish("shein", completion(state), None)
        assert json.loads(other.connectors.vault.get("shein")) == CREDENTIALS
    else:
        if phase == "metadata":
            with pytest.raises(ConnectorUnavailableError):
                other.connectors.require_shein_runtime("shein")
        result = await other.finish("shein", FinishSheinAuthorization(state=state), None)
        assert result.enabled and result.endpoint.endswith(".cn")
        assert (
            other.view("shein", SheinAuthorizationState(state=state), None)["status"] == "complete"
        )


@pytest.mark.parametrize("phase", ["metadata", "receipt"])
async def test_interrupted_installation_recovers_without_token_or_app_secret(
    service,
    tmp_path,
    monkeypatch,
    phase,
):
    state, _ = begin(service, endpoint="https://openapi.sheincorp.cn")
    transport(service, handler_for(state, []))
    if phase == "metadata":
        original = service.connectors._write

        def fail_publication(connector):
            if connector.endpoint.endswith(".cn"):
                raise OSError("synthetic publication failure")
            original(connector)

        monkeypatch.setattr(service.connectors, "_write", fail_publication)
    else:
        original = service._save

        def fail_receipt(flow):
            if flow.status == "complete":
                raise OSError("synthetic receipt failure")
            original(flow)

        monkeypatch.setattr(service, "_save", fail_receipt)
    with pytest.raises(MarketplaceError, match="interrupted"):
        await service.finish("shein", completion(state), None)
    if phase == "metadata":
        with pytest.raises(ConnectorUnavailableError):
            service.connectors.require_shein_runtime("shein")
    other = SheinAuthorizationService(service_at(tmp_path))
    transport(other, lambda _: pytest.fail("Recover from saved credential, not remote replay"))
    result = await other.finish("shein", FinishSheinAuthorization(state=state), None)
    assert result.endpoint.endswith(".cn") and result.enabled
    assert other.view("shein", SheinAuthorizationState(state=state), None)["status"] == "complete"


async def test_revoke_after_saved_credential_prevents_recovery_resurrection(service, monkeypatch):
    state, _ = begin(service)
    transport(service, handler_for(state, []))
    original = service._save

    def interrupt(flow):
        if flow.status == "complete":
            raise OSError("lost completion")
        original(flow)

    monkeypatch.setattr(service, "_save", interrupt)
    with pytest.raises(MarketplaceError):
        await service.finish("shein", completion(state), None)
    service.connectors.revoke("shein")
    with pytest.raises(MarketplaceError, match="changed"):
        await service.finish("shein", FinishSheinAuthorization(state=state), None)
    assert not service.connectors.get("shein").enabled
    assert service.connectors.vault.get("shein") is None


async def test_authorized_keys_support_existing_project_grants_and_pin_supplier(service):
    state, _ = begin(service)
    transport(service, handler_for(state, []))
    await service.finish("shein", completion(state), None)
    project = service.ledger.create_project(
        ProjectCreate(name="SHEIN", objective="Inspect catalog")
    )
    grant = await service.access.grant(
        project.id,
        MarketplaceGrantRequest(
            connector_id="shein",
            shop_id=SHOP_ID,
            expected_revision=0,
        ),
        None,
    )
    assert grant["bindings"][0]["shop_id"] == SHOP_ID
    transport(service, lambda _: response({"storeInfo": {"supplierId": 99}}))
    with pytest.raises(MarketplaceError, match="no longer matches"):
        await service.access.reader.read(
            "shein", MarketplaceReadRequest(operation="authorized_shops")
        )


def principal(**changes):
    return PrincipalContext(
        **{
            "tenant_id": "tenant-a",
            "principal_id": "owner",
            "role": PrincipalRole.OWNER,
            "session_id": "synthetic-session-1234",
            "assertion_nonce": "synthetic-nonce-1234567890",
            "assertion_issued_at": utc_now(),
            "assertion_expires_at": utc_now() + timedelta(minutes=5),
            **changes,
        }
    )


@pytest.mark.parametrize(
    "change",
    [
        {"principal_id": "other"},
        {"session_id": "different-session-1234"},
        {"tenant_id": "other"},
        {"role": PrincipalRole.MEMBER},
        {
            "assertion_issued_at": utc_now() - timedelta(minutes=1),
            "assertion_expires_at": utc_now() - timedelta(seconds=1),
        },
    ],
)
def test_callback_binds_current_administrator_tenant_and_session(service, change):
    service.settings.auth_mode = "trusted_gateway"
    service.settings.trusted_gateway_tenant_id = "tenant-a"
    state, _ = begin(service, principal=principal())
    with pytest.raises(MarketplaceError):
        service.view("shein", SheinAuthorizationState(state=state), principal(**change))


@pytest.mark.parametrize("role", [PrincipalRole.MEMBER, PrincipalRole.VIEWER, PrincipalRole.OWNER])
async def test_http_start_is_administrator_only_and_private(tmp_path, role):
    app = create_app(trusted_settings(tmp_path), runtime=FakeRuntime())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://gateway.test"
    ) as client:
        path = "/api/connectors/shein/shein-authorization/start"
        result = await request_as(
            client,
            "POST",
            path,
            "actor",
            role,
            json={
                "app_id": APP_ID,
                "endpoint": SHEIN_ORIGIN,
                "allow_products": True,
                "redirect_origin": "http://localhost:5173",
            },
        )
    assert result.status_code == (200 if role == PrincipalRole.OWNER else 403)
    if role == PrincipalRole.OWNER:
        assert result.headers["cache-control"] == "no-store"


async def test_http_validation_does_not_echo_callback_secrets(tmp_path):
    app = create_app(trusted_settings(tmp_path), runtime=FakeRuntime())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://gateway.test"
    ) as client:
        path = "/api/connectors/shein/shein-authorization/finish"
        result = await request_as(
            client,
            "POST",
            path,
            "owner",
            PrincipalRole.OWNER,
            json={
                "state": "invalid",
                "app_secret": APP_SECRET * 100,
                "temp_token": TEMP_TOKEN,
            },
        )
    assert result.status_code == 422 and result.headers["cache-control"] == "no-store"
    assert APP_SECRET not in result.text and TEMP_TOKEN not in result.text


async def test_http_flow_accepts_fresh_assertions_for_same_session_and_enforces_all_routes(
    tmp_path,
    monkeypatch,
):
    app = create_app(trusted_settings(tmp_path), runtime=FakeRuntime())
    service = app.state.shein_authorization
    monkeypatch.setattr(web_tools, "_resolve_ips", lambda _: ["93.184.216.34"])
    calls = []
    path = "/api/connectors/shein/shein-authorization/"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://gateway.test",
    ) as client:

        async def post(operation, body, role=PrincipalRole.OWNER):
            return await request_as(client, "POST", path + operation, "owner", role, json=body)

        started = await post(
            "start",
            {
                "app_id": APP_ID,
                "endpoint": SHEIN_ORIGIN,
                "allow_products": True,
                "redirect_origin": "http://localhost:5173",
            },
        )
        assert started.status_code == 200
        state = parse_qs(urlsplit(started.json()["authorization_url"]).fragment.split("?", 1)[1])[
            "state"
        ][0]
        transport(service, handler_for(state, calls))
        for operation in ("status", "finish", "cancel"):
            denied = await post(operation, {"state": state}, PrincipalRole.MEMBER)
            assert denied.status_code == 403
            assert denied.headers["cache-control"] == "no-store"
        status = await post("status", {"state": state})
        assert status.json()["status"] == "pending" and not calls
        saved = await post(
            "finish",
            {
                "state": state,
                "app_id": APP_ID,
                "app_secret": APP_SECRET,
                "temp_token": TEMP_TOKEN,
            },
        )
        assert saved.status_code == 200 and saved.json()["enabled"]
        assert len(calls) == 2 and saved.headers["cache-control"] == "no-store"
        repeated = await post("finish", {"state": state})
        assert repeated.status_code == 200 and len(calls) == 2
        status = await post("status", {"state": state})
        assert status.json()["status"] == "complete"
        cancelled = await post("cancel", {"state": state})
        assert cancelled.status_code == 409
        assert all(value not in saved.text + status.text for value in (APP_SECRET, TEMP_TOKEN))


async def test_http_invalid_start_has_private_error_response(tmp_path):
    app = create_app(trusted_settings(tmp_path), runtime=FakeRuntime())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://gateway.test",
    ) as client:
        result = await request_as(
            client,
            "POST",
            "/api/connectors/missing/shein-authorization/start",
            "owner",
            PrincipalRole.OWNER,
            json={
                "app_id": APP_ID,
                "endpoint": SHEIN_ORIGIN,
                "allow_products": True,
                "redirect_origin": "http://localhost:5173",
            },
        )
    assert result.status_code == 400
    assert result.headers["cache-control"] == "no-store"


def test_health_result_cannot_undo_a_concurrent_revoke(service, monkeypatch):
    original = service.connectors.credential
    other = ConnectorRegistry(
        service.connectors.root, FileSecretVault(service.connectors.vault.root)
    )

    def read_then_revoke(*args, **kwargs):
        value = original(*args, **kwargs)
        other.revoke("shein")
        return value

    monkeypatch.setattr(service.connectors, "credential", read_then_revoke)
    assert service.connectors.health_check("shein").enabled is False
    assert service.connectors.get("shein").enabled is False
