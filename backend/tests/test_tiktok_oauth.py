from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
import pytest
from test_auth_api import FakeRuntime
from test_marketplaces import SHOP, response

from ophira_backend import web_tools
from ophira_backend.auth import build_gateway_assertion_headers
from ophira_backend.config import Settings
from ophira_backend.connectors import ConnectorRegistry
from ophira_backend.constants import PrincipalRole
from ophira_backend.main import _OAuthCallbackAccessLogFilter, create_app
from ophira_backend.marketplaces import (
    TIKTOK_AUTH_SCOPE,
    TIKTOK_OAUTH_CALLBACK_PATH,
    TIKTOK_PRODUCT_SCOPE,
    TIKTOK_REFRESH_PATH,
    TIKTOK_SHOP_ORIGIN,
    TIKTOK_SHOP_TYPE,
    TIKTOK_SHOPS_PATH,
    TIKTOK_TOKEN_PATH,
    MarketplaceError,
    MarketplaceReadRequest,
    TikTokShopReader,
    check_tiktok_seller_authorization_url,
    parse_tiktok_shop_credentials,
    tiktok_oauth_vault_keys,
)
from ophira_backend.providers import FileSecretVault

NOW = 1_800_000_000
SELLER_URL = "https://services.tiktokshop.com/open/authorize?service_id=7174201526112896773"
OAUTH_CREDENTIALS = {
    "auth_mode": "seller_oauth", "app_key": "app-key", "app_secret": "test-secret",
    "authorization_url": SELLER_URL,
}
TOKEN = "seller-access-0123456789"
REFRESH = "seller-refresh-0123456789"


def token_response(
    *, access: str = TOKEN, refresh: str = REFRESH,
    scopes: list[str] | None = None, user_type: int = 0, open_id: str = "seller-1",
    access_expiry: int = NOW + 600,
) -> httpx.Response:
    return response({
        "access_token": access, "refresh_token": refresh, "open_id": open_id,
        "access_token_expire_in": access_expiry,
        "refresh_token_expire_in": NOW + 3600,
        "user_type": user_type,
        "granted_scopes": scopes if scopes is not None else [
            TIKTOK_AUTH_SCOPE, TIKTOK_PRODUCT_SCOPE,
        ],
    })


@pytest.fixture
def registry(tmp_path: Path, monkeypatch) -> ConnectorRegistry:
    monkeypatch.setattr(web_tools, "_resolve_ips", lambda _host: ["93.184.216.34"])
    result = ConnectorRegistry(
        tmp_path / "connectors", vault=FileSecretVault(tmp_path / "vault"),
    )
    result.upsert(
        TIKTOK_SHOP_TYPE, type=TIKTOK_SHOP_TYPE, display_name="TikTok Shop",
        endpoint=TIKTOK_SHOP_ORIGIN, scope=f"{TIKTOK_AUTH_SCOPE} {TIKTOK_PRODUCT_SCOPE}",
        secret=json.dumps(OAUTH_CREDENTIALS),
    )
    return result


def reader(registry, handler, clock=lambda: NOW) -> TikTokShopReader:
    return TikTokShopReader(
        registry, transport_factory=lambda: httpx.MockTransport(handler), clock=clock,
    )


@pytest.mark.parametrize("url", [
    "http://services.tiktokshop.com/open/authorize?service_id=123",
    "https://services.tiktokshop.com.evil.test/open/authorize?service_id=123",
    "https://services.tiktokshop.com:8443/open/authorize?service_id=123",
    "https://services.tiktokshop.com/other?service_id=123",
    "https://services.tiktokshop.com/open/authorize?service_id=123&state=attacker",
    "https://services.tiktokshop.com/open/authorize?service_id=abc",
    "https://services.tiktokshop.com/open/authorize?service_id=123#fragment",
    "https://shop.tiktok.com/alliance/creator/auth?app_key=123",
])
def test_only_partner_center_seller_links_are_accepted(url):
    assert not check_tiktok_seller_authorization_url(url)
    with pytest.raises(ValueError, match="valid seller credential bundle"):
        parse_tiktok_shop_credentials(json.dumps({**OAUTH_CREDENTIALS, "authorization_url": url}))
    assert check_tiktok_seller_authorization_url(SELLER_URL)
    assert check_tiktok_seller_authorization_url(
        "https://services.us.tiktokshop.com/open/authorize?service_id=123"
    )


def test_callback_access_log_removes_authorization_code():
    path = TIKTOK_OAUTH_CALLBACK_PATH.format(connector_id=TIKTOK_SHOP_TYPE)
    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1", "GET", path + "?code=private-code&state=private-state", "1.1", 303),
        None,
    )
    assert _OAuthCallbackAccessLogFilter().filter(record)
    assert "private-code" not in record.getMessage()
    assert "private-state" not in record.getMessage()


async def test_seller_oauth_exchanges_one_time_code_and_refreshes_across_readers(registry):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.url.host == "93.184.216.34"
        if request.url.path in {TIKTOK_TOKEN_PATH, TIKTOK_REFRESH_PATH}:
            assert request.headers["host"] == "auth.tiktok-shops.com"
            assert request.extensions["sni_hostname"] == "auth.tiktok-shops.com"
            assert request.method == "GET"
            query = dict(request.url.params)
            assert query["app_key"] == "app-key"
            assert query["app_secret"] == "test-secret"
            if request.url.path == TIKTOK_TOKEN_PATH:
                assert query["auth_code"] == "once-only-code"
                assert query["grant_type"] == "authorized_code"
                return token_response()
            assert query["refresh_token"] == REFRESH
            assert query["grant_type"] == "refresh_token"
            return token_response(
                access="rotated-access-0123456789",
                refresh="rotated-refresh-0123456789", access_expiry=NOW + 1200,
            )
        assert request.url.path == TIKTOK_SHOPS_PATH
        assert request.headers["x-tts-access-token"] in {TOKEN, "rotated-access-0123456789"}
        return response({"shops": [SHOP]})

    first = reader(registry, handler)
    second = reader(registry, handler)
    assert not first.oauth_status(TIKTOK_SHOP_TYPE, "https://oauth.example.com")["authorized"]
    started = first.begin_oauth(TIKTOK_SHOP_TYPE, "https://oauth.example.com")
    assert started["authorization_url"].startswith(SELLER_URL + "&state=")
    assert started["callback_url"] == (
        "https://oauth.example.com/api/connectors/tiktok_shop/tiktok-shop-oauth/callback"
    )
    state = parse_qs(urlsplit(started["authorization_url"]).query)["state"][0]
    await first.complete_oauth(
        TIKTOK_SHOP_TYPE, f"code=once-only-code&state={state}&app_key=app-key".encode(),
    )
    assert first.oauth_status(TIKTOK_SHOP_TYPE, "https://oauth.example.com")["authorized"]
    with pytest.raises(MarketplaceError, match="expired or changed"):
        await first.complete_oauth(
            TIKTOK_SHOP_TYPE, f"code=once-only-code&state={state}".encode(),
        )
    result = await first.read(
        TIKTOK_SHOP_TYPE, MarketplaceReadRequest(operation="authorized_shops"),
    )
    assert result["shops"][0]["id"] == SHOP["id"]
    assert TOKEN not in json.dumps(result)
    current = NOW + 590
    first.clock = lambda: current
    second.clock = lambda: current
    await asyncio.gather(*(
        adapter.read(
            TIKTOK_SHOP_TYPE, MarketplaceReadRequest(operation="authorized_shops"),
        ) for adapter in (first, second)
    ))
    assert [call.url.path for call in calls].count(TIKTOK_REFRESH_PATH) == 1
    record = json.loads(registry.vault.get(tiktok_oauth_vault_keys(TIKTOK_SHOP_TYPE)[1]))
    assert record["access_token"] == "rotated-access-0123456789"
    assert record["refresh_token"] == "rotated-refresh-0123456789"


async def test_callback_fails_closed_on_replay_scope_identity_and_reconfiguration(registry):
    calls = []

    def handler(request):
        calls.append(request)
        return token_response(user_type=1)

    adapter = reader(registry, handler)
    started = adapter.begin_oauth(TIKTOK_SHOP_TYPE, "https://oauth.example.com")
    state = parse_qs(urlsplit(started["authorization_url"]).query)["state"][0]
    with pytest.raises(MarketplaceError) as caught:
        await adapter.complete_oauth(
            TIKTOK_SHOP_TYPE, f"code=one&state={state}&state={state}".encode(),
        )
    assert caught.value.code == "MARKETPLACE_OAUTH_INVALID"
    assert calls == []
    with pytest.raises(MarketplaceError) as caught:
        await adapter.complete_oauth(TIKTOK_SHOP_TYPE, f"code=one&state={state}".encode())
    assert caught.value.code == "MARKETPLACE_OAUTH_INVALID"
    assert len(calls) == 1
    assert not registry.vault.has(tiktok_oauth_vault_keys(TIKTOK_SHOP_TYPE)[1])
    with pytest.raises(MarketplaceError):
        await adapter.complete_oauth(TIKTOK_SHOP_TYPE, f"code=one&state={state}".encode())
    assert len(calls) == 1
    started = adapter.begin_oauth(TIKTOK_SHOP_TYPE, "https://oauth.example.com")
    state = parse_qs(urlsplit(started["authorization_url"]).query)["state"][0]
    registry.store_secret(TIKTOK_SHOP_TYPE, json.dumps(OAUTH_CREDENTIALS))
    with pytest.raises(MarketplaceError):
        await adapter.complete_oauth(TIKTOK_SHOP_TYPE, f"code=one&state={state}".encode())
    assert len(calls) == 1


async def test_refresh_rejects_scope_loss_and_clears_on_revoke(registry):
    adapter = reader(registry, lambda _: token_response())
    started = adapter.begin_oauth(TIKTOK_SHOP_TYPE, "https://oauth.example.com")
    state = parse_qs(urlsplit(started["authorization_url"]).query)["state"][0]
    await adapter.complete_oauth(TIKTOK_SHOP_TYPE, f"code=one&state={state}".encode())
    adapter.clock = lambda: NOW + 590
    adapter.transport_factory = lambda: httpx.MockTransport(
        lambda _: token_response(scopes=[TIKTOK_AUTH_SCOPE], access_expiry=NOW + 1200),
    )
    with pytest.raises(MarketplaceError) as caught:
        await adapter.read(
            TIKTOK_SHOP_TYPE, MarketplaceReadRequest(operation="products", shop_id="shop-1"),
        )
    assert caught.value.code == "MARKETPLACE_SCOPE_REQUIRED"
    registry.revoke(TIKTOK_SHOP_TYPE)
    assert not any(registry.vault.has(key) for key in tiktok_oauth_vault_keys(TIKTOK_SHOP_TYPE))


async def test_public_callback_host_and_admin_start_boundary(tmp_path, monkeypatch):
    monkeypatch.setattr(web_tools, "_resolve_ips", lambda _host: ["93.184.216.34"])
    secret = "tiktok-gateway-secret-0123456789-abcdef"
    app = create_app(Settings(
        _env_file=None, data_dir=tmp_path, auth_mode="trusted_gateway",
        trusted_gateway_auth_secret=secret, trusted_gateway_tenant_id="tenant_tiktok",
        tiktok_shop_oauth_public_origin="https://oauth.example.com",
    ), runtime=FakeRuntime())
    app.state.connectors.upsert(
        TIKTOK_SHOP_TYPE, type=TIKTOK_SHOP_TYPE, display_name="TikTok Shop",
        endpoint=TIKTOK_SHOP_ORIGIN, scope=TIKTOK_AUTH_SCOPE,
        secret=json.dumps(OAUTH_CREDENTIALS),
    )
    app.state.marketplace_reader.transport_factory = lambda: httpx.MockTransport(
        lambda _: token_response(),
    )
    start_path = "/api/connectors/tiktok_shop/tiktok-shop-oauth/start"
    headers = build_gateway_assertion_headers(
        secret, method="POST", path=start_path, tenant_id="tenant_tiktok",
        principal_id="owner@example.com", role=PrincipalRole.OWNER,
        session_id="gateway_session_tiktok_owner", issued_at=datetime.now(UTC),
        nonce=f"nonce_{uuid4().hex}",
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://work.example.com",
    ) as client:
        assert (await client.post(start_path)).status_code != 200
        started = await client.post(start_path, headers=headers)
        assert started.status_code == 200, started.text
        callback = started.json()["callback_url"]
        assert callback == "https://oauth.example.com" + TIKTOK_OAUTH_CALLBACK_PATH.format(
            connector_id=TIKTOK_SHOP_TYPE,
        )
        state = parse_qs(urlsplit(started.json()["authorization_url"]).query)["state"][0]
        query = f"?state={state}&code=once-only-code"
        wrong_host = await client.get(callback.removeprefix("https://oauth.example.com") + query)
        assert wrong_host.status_code == 404
        completed = await client.get(callback + query, follow_redirects=False)
        assert completed.status_code == 303, completed.text
        assert completed.headers["location"] == "/app/plugins/connectors?tiktok_shop=connected"
        replay = await client.get(callback + query, follow_redirects=False)
        assert replay.status_code == 403
        assert TOKEN not in started.text + completed.text + replay.text
