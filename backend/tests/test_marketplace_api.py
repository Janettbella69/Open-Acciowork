import json
from pathlib import Path

import httpx
import pytest
from test_auth_api import signed_headers, trusted_settings
from test_marketplaces import CREDENTIALS, PRODUCT, SHOP, response

from ophira_backend.config import Settings
from ophira_backend.constants import PrincipalRole
from ophira_backend.main import create_app
from ophira_backend.marketplaces import TIKTOK_SHOP_ORIGIN, TIKTOK_SHOP_TYPE


class FakeRuntime:
    async def run(self, _run):
        pass

    async def cancel(self, _run_id):
        pass


def configure(app):
    app.state.connectors.upsert(
        TIKTOK_SHOP_TYPE, type=TIKTOK_SHOP_TYPE, display_name="TikTok Shop",
        endpoint=TIKTOK_SHOP_ORIGIN, scope="seller.authorization.info seller.product.basic",
        secret=json.dumps({**CREDENTIALS, "access_token_expires_at": 9999999999}),
    )


async def test_production_routes_read_real_adapter_and_exclude_all_vault_material(
    tmp_path: Path, monkeypatch,
):
    from ophira_backend import web_tools

    monkeypatch.setattr(web_tools, "_resolve_ips", lambda _: ["93.184.216.34"])
    app = create_app(Settings(data_dir=tmp_path), runtime=FakeRuntime())
    configure(app)
    requests = []

    def handler(request):
        requests.append(request)
        return response(
            {"shops": [SHOP]} if request.method == "GET" else {"products": [PRODUCT]},
        )

    app.state.marketplace_reader.transport_factory = lambda: httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost",
    ) as client:
        guides = await client.get("/api/connectors/marketplaces")
        assert guides.status_code == 200
        assert len(guides.json()) == 6
        assert [g["type"] for g in guides.json() if g["read_adapter"]] == [
            "shein", "tiktok_shop", "shopify",
        ]
        assert next(g for g in guides.json() if g["type"] == "aliexpress")["name"] == "AliExpress"
        result = await client.post("/api/connectors/tiktok_shop/marketplace-read", json={
            "operation": "products", "shop_id": "shop-1",
        })
        assert result.status_code == 200, result.text
        assert result.headers["cache-control"] == "no-store"
        assert result.json()["products"] == [PRODUCT]
        assert len(requests) == 2
        listing = await client.get("/api/connectors")
        for secret in ("seller-token", "test-secret", "cipher-1", "sign="):
            assert secret not in listing.text + result.text
        assert app.state.ledger.list_projects() == []
        revoked = await client.post("/api/connectors/tiktok_shop/revoke")
        assert revoked.status_code == 200
        result = await client.post("/api/connectors/tiktok_shop/marketplace-read", json={
            "operation": "authorized_shops",
        })
        assert result.status_code == 409
        assert "MARKETPLACE_DISABLED" in result.text
        assert len(requests) == 2


@pytest.mark.parametrize("secret", ["leak-token" * 1000, {"token": "leak-token"}, "leak-token"])
async def test_invalid_credential_inputs_are_never_reflected(tmp_path: Path, secret):
    app = create_app(Settings(data_dir=tmp_path), runtime=FakeRuntime())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost",
    ) as client:
        result = await client.put("/api/connectors/tiktok_shop", json={
            "type": "tiktok_shop", "display_name": "TikTok Shop",
            "endpoint": TIKTOK_SHOP_ORIGIN, "secret": secret,
        })
    assert result.status_code in {400, 422}
    assert "leak-token" not in result.text
    assert not app.state.connectors.vault.has("tiktok_shop")


@pytest.mark.parametrize("role", [PrincipalRole.MEMBER, PrincipalRole.VIEWER])
async def test_non_admin_cannot_read_seller_data(tmp_path: Path, role):
    app = create_app(trusted_settings(tmp_path), runtime=FakeRuntime())
    configure(app)

    def unexpected(_request):
        pytest.fail("non-admin must not send seller API requests")

    app.state.marketplace_reader.transport_factory = lambda: httpx.MockTransport(unexpected)
    path = "/api/connectors/tiktok_shop/marketplace-read"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost",
    ) as client:
        result = await client.post(path, json={"operation": "authorized_shops"}, headers=(
            signed_headers("POST", path, "viewer-1", role)
        ))
    assert result.status_code == 403


async def test_read_contract_rejects_writes_before_adapter(tmp_path: Path):
    app = create_app(Settings(data_dir=tmp_path), runtime=FakeRuntime())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost",
    ) as client:
        result = await client.post("/api/connectors/tiktok_shop/marketplace-read", json={
            "operation": "update_price", "shop_id": "shop-1",
        })
    assert result.status_code == 422
