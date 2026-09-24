from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from test_marketplaces import Body

from ophira_backend import web_tools
from ophira_backend.auth import build_gateway_assertion_headers
from ophira_backend.config import Settings
from ophira_backend.constants import PrincipalRole
from ophira_backend.main import create_app
from ophira_backend.marketplace_access import MarketplaceAccessRecord, MarketplaceBinding
from ophira_backend.models import utc_now
from ophira_backend.shopify import SHOPIFY_ORDER_WEBHOOK_FIELDS

GATEWAY_SECRET = "test-trusted-gateway-signing-secret-shopify-0001"
STORE = "https://skate-demo.myshopify.com"
TOKEN = "shopify-access-token-123456"


class FakeRuntime:
    async def run(self, _run) -> None:
        return None

    async def cancel(self, _run_id: str) -> None:
        return None


def gateway_headers(method: str, path: str, role: PrincipalRole = PrincipalRole.OWNER):
    return build_gateway_assertion_headers(
        GATEWAY_SECRET, method=method, path=path,
        tenant_id="tenant_shopify", principal_id="owner@example.com", role=role,
        session_id="gateway_session_shopify_owner", issued_at=datetime.now(UTC),
        nonce=f"nonce_{uuid4().hex}",
    )


@pytest.mark.asyncio
async def test_shopify_subscription_registration_is_server_scoped_and_idempotent(
    tmp_path: Path, monkeypatch,
):
    monkeypatch.setattr(web_tools, "_resolve_ips", lambda _host: ["93.184.216.34"])
    settings = Settings(
        data_dir=tmp_path, auth_mode="trusted_gateway",
        trusted_gateway_auth_secret=GATEWAY_SECRET,
        trusted_gateway_tenant_id="tenant_shopify",
        event_public_origin="https://events.example.com",
    )
    app = create_app(settings, runtime=FakeRuntime())
    credentials = {
        "client_id": "shopify-client-id-123456",
        "client_secret": "shopify-client-secret-123456",
    }
    app.state.connectors.upsert(
        "shopify", type="shopify", display_name="Shopify",
        scope="read_products read_orders", endpoint=STORE, secret=json.dumps(credentials),
    )
    seen_queries: list[str] = []
    registered: dict[str, object] = {}
    fail_next_create = [False]

    def shopify(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/admin/oauth/access_token"):
            return httpx.Response(200, stream=Body(json.dumps({
                "access_token": TOKEN, "scope": "read_products read_orders",
                "expires_in": 3600,
            }).encode()))
        assert request.headers["x-shopify-access-token"] == TOKEN
        payload = json.loads(request.content)
        query = payload["query"]
        seen_queries.append(query)
        if "query OphiraOrderWebhook" in query:
            uri = payload["variables"]["uri"]
            nodes = [registered] if registered.get("uri") == uri else []
            return httpx.Response(200, stream=Body(json.dumps({"data": {"webhookSubscriptions": {
                "nodes": nodes, "pageInfo": {"hasNextPage": False},
            }}}).encode()), headers={"x-request-id": "req-shopify-sub-1"})
        if "mutation OphiraOrderWebhookDelete" in query:
            subscription_id = payload["variables"]["id"]
            assert subscription_id == registered["id"]
            registered.clear()
            return httpx.Response(200, stream=Body(json.dumps({"data": {
                "webhookSubscriptionDelete": {
                    "deletedWebhookSubscriptionId": subscription_id,
                    "userErrors": [],
                },
            }}).encode()), headers={"x-request-id": "req-shopify-sub-3"})
        assert "mutation OphiraOrderWebhookCreate" in query
        variables = payload["variables"]["webhookSubscription"]
        assert variables["uri"].startswith("https://events.example.com/api/projects/")
        assert variables["uri"].endswith("/shopify")
        assert variables["includeFields"] == list(SHOPIFY_ORDER_WEBHOOK_FIELDS)
        if fail_next_create[0]:
            fail_next_create[0] = False
            raise httpx.ConnectError("simulated lost create response")
        registered.update({
            "id": "gid://shopify/WebhookSubscription/123456", "topic": "ORDERS_CREATE",
            "uri": variables["uri"], "includeFields": variables["includeFields"],
        })
        return httpx.Response(200, stream=Body(json.dumps({"data": {"webhookSubscriptionCreate": {
            "webhookSubscription": registered, "userErrors": [],
        }}}).encode()), headers={"x-request-id": "req-shopify-sub-2"})

    app.state.marketplace_reader.transport_factory = lambda: httpx.MockTransport(shopify)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://gateway.test",
    ) as client:
        async def request(method: str, path: str, **kwargs):
            return await client.request(
                method, path, headers=gateway_headers(method, path), **kwargs,
            )

        project_response = await request(
            "POST", "/api/projects", json={"name": "Shop", "objective": "Orders"},
        )
        assert project_response.status_code == 201, project_response.text
        project_id = project_response.json()["id"]
        access = app.state.marketplace_access
        now = utc_now()
        binding = MarketplaceBinding(
            connector_id="shopify", connector_identity=access._connector_identity("shopify"),
            shop_id="1234", shop_name="Skate Demo", region="US",
            created_by="owner@example.com", created_at=now,
            expires_at=now + timedelta(days=7),
        )
        app.state.ledger._atomic_write(
            access.root / f"{project_id}.json",
            MarketplaceAccessRecord(
                project_id=project_id, identity_binding="authenticated_principal",
                tenant_id="tenant_shopify", revision=1, bindings=[binding],
            ),
        )
        base = f"/api/projects/{project_id}/event-triggers"
        created = await request("POST", base, json={
            "name": "New orders", "kind": "store_order", "prompt": "Review each order",
            "max_budget_usd": 1, "shopify_binding_id": binding.id,
        })
        assert created.status_code == 201, created.text
        trigger_id = created.json()["trigger"]["id"]
        path = f"{base}/{trigger_id}/shopify-subscription"
        before = await request("GET", path)
        assert before.status_code == 200
        assert before.json()["status"] == "not_registered"
        assert before.json()["delivery_url"] == (
            f"https://events.example.com{base}/{trigger_id}/shopify"
        )
        registered_response = await request(
            "POST", path, json={"expected_revision": 1},
        )
        assert registered_response.status_code == 200, registered_response.text
        assert registered_response.json()["status"] == "confirmed"
        assert registered_response.json()["subscription_id"] == registered["id"]
        assert sum("mutation OphiraOrderWebhookCreate" in q for q in seen_queries) == 1
        again = await request("POST", path, json={"expected_revision": 1})
        assert again.status_code == 200
        assert again.json()["status"] == "confirmed"
        assert sum("mutation OphiraOrderWebhookCreate" in q for q in seen_queries) == 1
        assert TOKEN not in registered_response.text
        assert credentials["client_secret"] not in registered_response.text
        settings.event_public_origin = None
        origin_removed = await request("GET", path)
        assert origin_removed.status_code == 200
        assert origin_removed.json()["status"] == "origin_changed"
        assert origin_removed.json()["subscription_id"] == registered["id"]
        settings.event_public_origin = "https://events.example.com"

        second = await request("POST", base, json={
            "name": "More orders", "kind": "store_order", "prompt": "Review",
            "max_budget_usd": 1, "shopify_binding_id": binding.id,
        })
        assert second.status_code == 201
        second_id = second.json()["trigger"]["id"]
        second_path = f"{base}/{second_id}/shopify-subscription"
        fail_next_create[0] = True
        uncertain = await request("POST", second_path, json={"expected_revision": 1})
        assert uncertain.status_code == 502
        state = await request("GET", second_path)
        assert state.json()["status"] == "uncertain"
        mutations = sum("mutation OphiraOrderWebhookCreate" in q for q in seen_queries)
        checked = await request("POST", second_path, json={"expected_revision": 1})
        assert checked.status_code == 200
        assert checked.json()["status"] == "uncertain"
        assert sum("mutation OphiraOrderWebhookCreate" in q for q in seen_queries) == mutations
        retried = await request("POST", second_path, json={
            "expected_revision": 1, "retry_uncertain": True,
        })
        assert retried.status_code == 200
        assert retried.json()["status"] == "confirmed"
        assert sum("mutation OphiraOrderWebhookCreate" in q for q in seen_queries) == mutations + 1
        removed = await request(
            "DELETE", second_path, json={"expected_revision": 1},
        )
        assert removed.status_code == 200, removed.text
        assert removed.json()["status"] == "not_registered"
        assert (await request("GET", second_path)).json()["status"] == "not_registered"
        assert sum("mutation OphiraOrderWebhookDelete" in q for q in seen_queries) == 1
        registered.update({
            "id": "gid://shopify/WebhookSubscription/987654",
            "topic": "ORDERS_CREATE",
            "uri": f"https://events.example.com{base}/{second_id}/shopify",
            "includeFields": ["id"],
        })
        prior_mutations = sum("mutation OphiraOrderWebhookCreate" in q for q in seen_queries)
        conflict = await request("POST", second_path, json={"expected_revision": 1})
        assert conflict.status_code == 409
        assert sum(
            "mutation OphiraOrderWebhookCreate" in q for q in seen_queries
        ) == prior_mutations


@pytest.mark.parametrize("origin", [
    "http://events.example.com", "https://localhost", "https://127.0.0.1",
    "https://events.local", "https://events.example.com/path",
])
def test_event_public_origin_is_strict_https(tmp_path: Path, origin: str):
    with pytest.raises(ValueError, match="event_public_origin"):
        Settings(data_dir=tmp_path, event_public_origin=origin)
