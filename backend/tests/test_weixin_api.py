"""Offline control-plane contracts: no account login or external message delivery."""

import asyncio
import base64
import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from ophira_backend.auth import build_gateway_assertion_headers
from ophira_backend.config import Settings
from ophira_backend.constants import PrincipalRole
from ophira_backend.main import create_app
from ophira_backend.models import ProjectCreate
from ophira_backend.project_deletion import ProjectDeletionService
from ophira_backend.project_deletion_models import ProjectDeletionRequest
from ophira_backend.weixin_transport import WeixinLoginStatus, WeixinQrCode, WeixinTransportError

PREFIX = "/api/channels/weixin"
SECRET = "weixin-gateway-test-signature-0123456789"


class OfflineRuntime:
    async def run(self, _run):
        return None

    async def cancel(self, _run_id):
        return None


@pytest.fixture
def app(tmp_path, monkeypatch):
    application = create_app(Settings(_env_file=None, data_dir=tmp_path), runtime=OfflineRuntime())

    async def start():
        return WeixinQrCode("private-qr-ticket", "https://weixin.qq.com/pair?ticket=private-qr")

    async def status(_qrcode, **_kwargs):
        return WeixinLoginStatus("confirmed", "private-bot-token", "bot@im.bot",
                                 "owner@im.wechat", "https://ilinkai.weixin.qq.com")

    monkeypatch.setattr(application.state.weixin_transport, "start_login", start)
    monkeypatch.setattr(application.state.weixin_transport, "login_status", status)
    return application


async def pair(client):
    response = await client.post(PREFIX + "/pairings", json={})
    assert response.status_code == 201, response.text
    pending = response.json()
    assert pending["status"] == "wait"
    raw_svg = base64.b64decode(pending["qr_svg_data_url"].split(",", 1)[1])
    assert b"<svg" in raw_svg and b"private-qr" not in raw_svg
    assert "private-qr-ticket" not in response.text
    assert response.headers["cache-control"] == "no-store"
    response = await client.post(PREFIX + f"/pairings/{pending['id']}/poll", json={})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "confirmed"
    assert "qr_svg_data_url" not in response.json()
    return response.json()["account_id"]


async def test_pair_bind_enable_disable_revoke_keeps_credentials_private(app, tmp_path):
    project = app.state.ledger.create_project(ProjectCreate(name="Weixin", objective="Research"))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1") as client:
        account_id = await pair(client)
        response = await client.get(PREFIX)
        overview = response.json()
        assert overview["direct_text"] and not overview["group_chat"]
        assert overview["projects"] == [{"id": project.id, "name": "Weixin"}]
        account = overview["accounts"][0]
        assert not account["enabled"] and account["project_id"] is None
        assert "principal_binding" not in account
        assert "private-bot-token" not in response.text
        path = f"{PREFIX}/accounts/{account_id}/binding"
        body = {"project_id": project.id, "enabled": True, "expected_revision": 1}
        response = await client.put(path, json=body)
        assert response.status_code == 200, response.text
        assert response.json()["enabled"]
        assert (await client.put(path, json=body)).status_code == 409
        store = app.state.weixin_store
        ref = store.save_context(account_id, "1", "private-context", expected_revision=2)
        assert store.reply_context(account_id, ref, 2) == "private-context"
        response = await client.delete(f"{PREFIX}/accounts/{account_id}?expected_revision=2")
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "revoked"
        assert store.vault.get(account_id) is None
        assert not app.state.policy.get().channel_delivery_enabled
    for path in (tmp_path / "channels").rglob("*.json"):
        text = path.read_text()
        assert "private-bot-token" not in text and "private-context" not in text


async def test_old_manual_connector_is_preserved_with_authoritative_secret_presence(
    app, monkeypatch,
):
    registry = app.state.connectors
    assert not any(item.type == "wechat" for item in registry.ensure_catalog())
    registry.upsert("wechat", type="wechat", display_name="WeChat",
                    secret_env="OPHIRA_CONNECTOR_WECHAT_KEY")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1") as client:
        async def old_card():
            response = await client.get("/api/connectors")
            assert response.status_code == 200, response.text
            assert "private-legacy-key" not in response.text
            return next(item for item in response.json() if item["id"] == "wechat")

        assert not (await old_card())["secret_configured"]
        monkeypatch.setenv("OPHIRA_CONNECTOR_WECHAT_KEY", "private-legacy-key")
        assert (await old_card())["secret_configured"]
        monkeypatch.delenv("OPHIRA_CONNECTOR_WECHAT_KEY")
        registry.upsert("wechat", type="wechat", display_name="WeChat",
                        secret="private-legacy-key")
        assert (await old_card())["secret_configured"]
        assert "secret_configured" not in registry.get("wechat").model_dump()
        registry.revoke("wechat")
        assert not (await old_card())["secret_configured"]


async def test_cancel_during_poll_cannot_reactivate_pairing(app, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed(*_args, **_kwargs):
        entered.set()
        await release.wait()
        return WeixinLoginStatus("confirmed", "late-private-token", "bot@im.bot",
                                 "owner@im.wechat", "https://ilinkai.weixin.qq.com")

    monkeypatch.setattr(app.state.weixin_transport, "login_status", delayed)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1") as client:
        pending = (await client.post(PREFIX + "/pairings", json={})).json()
        path = f"{PREFIX}/pairings/{pending['id']}"
        poll = asyncio.create_task(client.post(path + "/poll", json={}))
        await asyncio.wait_for(entered.wait(), 2)
        assert (await client.post(path + "/poll", json={})).status_code == 409
        assert (await client.delete(path)).json()["status"] == "cancelled"
        release.set()
        response = await poll
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "cancelled"
        assert not app.state.weixin_store.list_accounts()
        assert app.state.weixin_store.vault.get(pending["id"]) is None


@pytest.mark.parametrize("state", ["need_verifycode", "verify_code_blocked", "expired",
                                  "scaned_but_redirect", "binded_redirect"])
async def test_protocol_pairing_states_are_explicit(app, monkeypatch, state):
    received = []

    async def status(_qrcode, **kwargs):
        received.append(kwargs)
        return WeixinLoginStatus(state)

    monkeypatch.setattr(app.state.weixin_transport, "login_status", status)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1") as client:
        pending = (await client.post(PREFIX + "/pairings", json={})).json()
        response = await client.post(f"{PREFIX}/pairings/{pending['id']}/poll",
                                     json={"verify_code": "123456"})
        assert response.status_code == 200, response.text
        expected = "unsupported_redirect" if state == "scaned_but_redirect" else state
        assert response.json()["status"] == expected
        assert received == [{"verify_code": "123456"}]
        assert not app.state.weixin_store.list_accounts()


async def test_transport_failure_is_static_and_no_account_created(app, monkeypatch):
    async def fail():
        raise WeixinTransportError("NETWORK_ERROR")

    monkeypatch.setattr(app.state.weixin_transport, "start_login", fail)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1") as client:
        response = await client.post(PREFIX + "/pairings", json={})
        assert response.status_code == 502 and response.json()["detail"] == "NETWORK_ERROR"
        assert not app.state.weixin_store.list_accounts()


async def test_unknown_regional_redirect_closes_pairing_without_following(app, monkeypatch):
    calls = []

    async def rejected(*_args, **_kwargs):
        calls.append(True)
        raise WeixinTransportError("DESTINATION_REJECTED")

    monkeypatch.setattr(app.state.weixin_transport, "login_status", rejected)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1") as client:
        pairing = (await client.post(PREFIX + "/pairings", json={})).json()
        path = f"{PREFIX}/pairings/{pairing['id']}/poll"
        for _ in range(2):
            response = await client.post(path, json={})
            assert response.status_code == 200, response.text
            assert response.json()["status"] == "unsupported_redirect"
            assert "qr_svg_data_url" not in response.json()
        assert calls == [True]
        assert app.state.weixin_store.vault.get(pairing["id"]) is None


async def test_oversized_qr_encoding_fails_with_static_error(app, monkeypatch):
    async def huge():
        return WeixinQrCode("ticket", "x" * 16000)

    monkeypatch.setattr(app.state.weixin_transport, "start_login", huge)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1") as client:
        response = await client.post(PREFIX + "/pairings", json={})
        assert response.status_code == 502
        assert "xxxx" not in response.text and "ticket" not in response.text


async def test_project_erasure_detaches_account_and_purges_reply_contexts(app):
    project = app.state.ledger.create_project(ProjectCreate(name="Weixin erase", objective="Erase"))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1") as client:
        account_id = await pair(client)
        await client.put(f"{PREFIX}/accounts/{account_id}/binding", json={
            "project_id": project.id, "enabled": True, "expected_revision": 1,
        })
    store = app.state.weixin_store
    store.save_context(account_id, "1", "project-private-context", expected_revision=2)
    service = ProjectDeletionService(app.state.ledger, app.state.project_access)
    preview = service.preview(project.id, None)
    assert preview.can_delete
    assert any("Weixin" in item.message for item in preview.retained)
    request = ProjectDeletionRequest(
        operation_id=str(uuid4()), fingerprint=preview.fingerprint,
        confirmation_name=project.name, reason="Erase project and private replies",
    )
    result = service.start(project.id, request, None)
    assert result.status == "deleted", result
    account = store.get_account(account_id)
    assert not account.enabled and account.project_id is None
    bundle = store.vault.get(account_id)
    assert "project-private-context" not in bundle
    assert json.loads(bundle)["contexts"] == {}
    assert "private-bot-token" in bundle  # account retained as disclosed in the review
    assert service.start(project.id, request, None) == result


def gateway_headers(method, path, *, role=PrincipalRole.OWNER, principal="owner", session="1"):
    return build_gateway_assertion_headers(
        SECRET, method=method, path=path, tenant_id="tenant_weixin", principal_id=principal,
        role=role, session_id="weixin_gateway_session_" + session,
        issued_at=datetime.now(UTC), nonce="nonce_" + uuid4().hex,
    )


async def test_gateway_members_cannot_pair_and_other_admin_sessions_cannot_poll(
    tmp_path, monkeypatch,
):
    app = create_app(Settings(
        _env_file=None, data_dir=tmp_path, auth_mode="trusted_gateway",
        trusted_gateway_auth_secret=SECRET, trusted_gateway_tenant_id="tenant_weixin",
    ), runtime=OfflineRuntime())
    calls = []

    async def start():
        calls.append("start")
        return WeixinQrCode("secret-qr", "test-pairing")

    monkeypatch.setattr(app.state.weixin_transport, "start_login", start)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1") as client:
        response = await client.get(PREFIX, headers=gateway_headers(
            "GET", PREFIX, role=PrincipalRole.MEMBER, principal="member"))
        assert response.status_code == 200, response.text
        assert not response.json()["can_manage"] and response.json()["accounts"] == []
        path = PREFIX + "/pairings"
        response = await client.post(path, json={}, headers=gateway_headers(
            "POST", path, role=PrincipalRole.MEMBER, principal="member"))
        assert response.status_code == 403
        assert not calls
        response = await client.post(path, json={}, headers=gateway_headers("POST", path))
        assert response.status_code == 201, response.text
        pending = response.json()
        poll_path = path + f"/{pending['id']}/poll"
        response = await client.post(poll_path, json={}, headers=gateway_headers(
            "POST", poll_path, session="other"))
        assert response.status_code == 403, response.text
        assert "secret-qr" not in json.dumps(response.json())
