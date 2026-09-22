import asyncio
import json
import multiprocessing
import time
from datetime import timedelta

import httpx
import pytest
from test_application_backup import source as application_source
from test_auth_api import FakeRuntime, principal, request_as, trusted_settings
from test_channel_delivery import adapter_settings, success_handler, target
from test_channels import signed_headers
from test_companion_push import (
    RecordingAdapter,
    b64,
    device_client,
    owner_client,
    paired,
    subscription,
)
from test_companion_push import settings as push_settings
from test_credential_api import PASSWORD, metadata

from ophira_backend import credential_api
from ophira_backend.application_backup import create_backup, restore_backup
from ophira_backend.backup_files import remove_staging
from ophira_backend.channel_delivery import ChannelDeliveryError, FeishuReplyAdapter
from ophira_backend.config import Settings
from ophira_backend.constants import PrincipalRole
from ophira_backend.credential_archive import decrypt_credential
from ophira_backend.credential_models import CredentialSave
from ophira_backend.main import create_app
from ophira_backend.models import utc_now
from ophira_backend.push_transport import WebPushAdapter
from ophira_backend.secret_vault import CredentialChangedError
from ophira_backend.service_credentials import ServiceCredentialId as ID
from ophira_backend.service_credentials import ServiceCredentialRegistry

VALUES = {
    ID.FEISHU_EVENTS: "event-private-canary",
    ID.FEISHU_APP: "app-private-canary",
    ID.WEB_PUSH: b64((98765).to_bytes(32, "big")),
}


def configured(tmp_path):
    settings = trusted_settings(
        tmp_path,
        feishu_encrypt_key=VALUES[ID.FEISHU_EVENTS],
        feishu_app_secret=VALUES[ID.FEISHU_APP],
        push_vapid_private_key=VALUES[ID.WEB_PUSH],
    )
    return settings, create_app(settings, runtime=FakeRuntime())


def erase(registry, identifier):
    view, _ = registry.snapshot(identifier)
    item = registry.get(identifier)
    return registry.vault.delete(
        identifier,
        expected_revision=view["revision"],
        fallback=registry.environment_credential(item),
    )


@pytest.mark.parametrize("identifier", list(ID))
async def test_service_export_delete_restart_and_reviewed_reconfiguration(tmp_path, identifier):
    settings, app = configured(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://gateway.test",
    ) as client:
        item = await metadata(client, "service", identifier)
        assert item["configured"] and item["source"] == "environment" and item["effect"]
        exported = await request_as(
            client,
            "POST",
            f"/api/credentials/service/{identifier}/export",
            "owner",
            PrincipalRole.OWNER,
            json={"expected_revision": item["revision"], "password": PASSWORD},
        )
        assert exported.status_code == 200 and VALUES[identifier] not in exported.text
        content = decrypt_credential(exported.content, PASSWORD)
        assert content["kind"] == "service" and content["credential"] == VALUES[identifier]
        receipts = []
        for _ in range(2):
            response = await request_as(
                client,
                "DELETE",
                f"/api/credentials/service/{identifier}",
                "owner",
                PrincipalRole.OWNER,
                json={"expected_revision": item["revision"], "confirm_delete": True},
            )
            assert response.status_code == 200
            receipts.append(response.json())
        assert receipts[0] == receipts[1] and receipts[0]["source"] == "deleted"
        assert ServiceCredentialRegistry(settings).resolve(identifier) is None
        assert VALUES[identifier] not in app.state.service_credentials.vault.path.read_text()
        stale = await request_as(
            client,
            "PUT",
            f"/api/credentials/service/{identifier}",
            "owner",
            PrincipalRole.OWNER,
            json={"expected_revision": item["revision"], "secret": VALUES[identifier]},
        )
        assert stale.status_code == 409 and VALUES[identifier] not in stale.text
        saved = await request_as(
            client,
            "PUT",
            f"/api/credentials/service/{identifier}",
            "owner",
            PrincipalRole.OWNER,
            json={"expected_revision": receipts[0]["revision"], "secret": VALUES[identifier]},
        )
        assert saved.status_code == 200 and saved.headers["cache-control"] == "no-store"
        assert VALUES[identifier] not in saved.text and saved.json()["source"] == "stored"
        assert ServiceCredentialRegistry(settings).resolve(identifier) == VALUES[identifier]
        old_delete = await request_as(
            client,
            "DELETE",
            f"/api/credentials/service/{identifier}",
            "owner",
            PrincipalRole.OWNER,
            json={"expected_revision": item["revision"], "confirm_delete": True},
        )
        assert old_delete.status_code == 409


@pytest.mark.parametrize(
    "role", [PrincipalRole.MEMBER, PrincipalRole.REVIEWER, PrincipalRole.VIEWER]
)
async def test_service_actions_require_current_admin_before_reading_secrets(tmp_path, role):
    _, app = configured(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://gateway.test",
    ) as client:
        for method, path in (
            ("PUT", "/api/credentials/service/feishu_app"),
            ("DELETE", "/api/credentials/service/feishu_app"),
            ("POST", "/api/credentials/service/feishu_app/export"),
        ):
            response = await request_as(
                client, method, path, "member", role, json={"secret": "private-input"}
            )
            assert response.status_code == 403 and "private-input" not in response.text


async def test_bad_keys_and_non_service_identity_secrets_are_not_accepted(tmp_path):
    _, app = configured(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://gateway.test",
    ) as client:
        item = await metadata(client, "service", ID.WEB_PUSH)
        for value in ("", "invalid-private-key", "/private/key.pem", "a" * 2001):
            response = await request_as(
                client,
                "PUT",
                "/api/credentials/service/web_push",
                "owner",
                PrincipalRole.OWNER,
                json={"expected_revision": item["revision"], "secret": value},
            )
            assert response.status_code in {409, 422}
            if value:
                assert value not in response.text
        for name in ("trusted_gateway_auth_secret", "commercial_approval_secret", "search"):
            response = await request_as(
                client,
                "PUT",
                f"/api/credentials/service/{name}",
                "owner",
                PrincipalRole.OWNER,
                json={"expected_revision": item["revision"], "secret": "private-input"},
            )
            assert response.status_code == 404 and "private-input" not in response.text
    assert app.state.service_credentials.resolve(ID.WEB_PUSH) == VALUES[ID.WEB_PUSH]


def test_service_save_rechecks_authority_inside_vault_transaction(tmp_path, monkeypatch):
    _, app = configured(tmp_path)
    credentials = app.state.credentials
    actor = principal("owner", PrincipalRole.OWNER)
    view = credentials.describe("service", ID.FEISHU_APP, actor)
    original = credentials.services.vault._load

    def expire(fd):
        state = original(fd)
        monkeypatch.setattr(credential_api, "utc_now", lambda: utc_now() + timedelta(hours=1))
        return state

    monkeypatch.setattr(credentials.services.vault, "_load", expire)
    with pytest.raises(credential_api.AuthorizationError):
        credentials.save_service(
            ID.FEISHU_APP,
            CredentialSave(
                expected_revision=view["revision"],
                secret="new-private-key",
            ),
            actor,
        )
    assert credentials.services.resolve(ID.FEISHU_APP) == VALUES[ID.FEISHU_APP]


async def test_feishu_cached_token_is_revoked_and_rotation_observed_by_existing_adapter(tmp_path):
    settings = adapter_settings(tmp_path)
    calls = []
    adapter = FeishuReplyAdapter(settings, transport=success_handler(calls))
    registry = ServiceCredentialRegistry(settings)
    await adapter.reply(target(), "first", idempotency_key="first")
    assert len(calls) == 2
    erased = erase(registry, ID.FEISHU_APP)
    with pytest.raises(ChannelDeliveryError, match="not configured"):
        await adapter.reply(target(), "blocked", idempotency_key="blocked")
    assert len(calls) == 2 and adapter._token is None and not adapter.configured
    registry.save(ID.FEISHU_APP, "replacement-secret", erased["revision"])
    await adapter.reply(target(), "restored", idempotency_key="restored")
    assert len(calls) == 4 and json.loads(calls[2].content)["app_secret"] == "replacement-secret"
    registry.vault.path.write_text("corrupt")
    with pytest.raises(ChannelDeliveryError, match="could not be verified"):
        await adapter.reply(target(), "blocked again", idempotency_key="blocked-again")
    assert len(calls) == 4 and adapter._token is None


@pytest.mark.parametrize("change", ["delete", "rotate"])
async def test_pending_feishu_token_response_cannot_revive_revoked_generation(tmp_path, change):
    settings = adapter_settings(tmp_path)
    registry = ServiceCredentialRegistry(settings)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def handle(request):
        calls.append(request)
        entered.set()
        await release.wait()
        return httpx.Response(
            200, json={"code": 0, "tenant_access_token": "old-token", "expire": 7200}
        )

    adapter = FeishuReplyAdapter(settings, transport=httpx.MockTransport(handle))
    pending = asyncio.create_task(adapter.reply(target(), "never sent", idempotency_key="pending"))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        view, _ = registry.snapshot(ID.FEISHU_APP)
        if change == "delete":
            erase(registry, ID.FEISHU_APP)
        else:
            registry.save(ID.FEISHU_APP, "new-secret", view["revision"])
    finally:
        release.set()
    with pytest.raises(ChannelDeliveryError):
        await asyncio.wait_for(pending, 3)
    assert len(calls) == 1 and adapter._token is None


async def test_webhook_uses_current_signing_key_without_application_restart(tmp_path):
    _, app = configured(tmp_path)
    registry = app.state.service_credentials
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://gateway.test",
    ) as client:

        async def challenge(key, suffix):
            raw = json.dumps({"type": "url_verification", "challenge": suffix}).encode()
            return await client.post(
                "/api/channels/feishu",
                content=raw,
                headers=signed_headers(
                    raw,
                    key,
                    timestamp=str(int(time.time())),
                    nonce=suffix,
                ),
            )

        assert (await challenge(VALUES[ID.FEISHU_EVENTS], "first")).status_code == 200
        erased = erase(registry, ID.FEISHU_EVENTS)
        assert (await challenge(VALUES[ID.FEISHU_EVENTS], "deleted")).status_code == 503
        registry.save(ID.FEISHU_EVENTS, "new-signing-key", erased["revision"])
        assert (await challenge(VALUES[ID.FEISHU_EVENTS], "old")).status_code == 401
        assert (await challenge("new-signing-key", "new")).json() == {"challenge": "new"}


async def test_existing_push_adapter_drops_deleted_or_unverifiable_key_and_loads_replacement(
    tmp_path,
):
    settings = push_settings(tmp_path)
    registry = ServiceCredentialRegistry(settings)

    def forbidden(_):
        pytest.fail("Revoked push credentials must not reach any HTTP transport")

    adapter = WebPushAdapter(settings, transport=httpx.MockTransport(forbidden))
    previous = adapter.public_key
    assert adapter.enabled and previous
    erased = erase(registry, ID.WEB_PUSH)
    assert not adapter.enabled and adapter.public_key is None
    assert (await adapter.send(subscription(), "deleted")).status == "disabled"
    registry.save(ID.WEB_PUSH, b64((45678).to_bytes(32, "big")), erased["revision"])
    assert adapter.enabled and adapter.public_key != previous
    registry.vault.path.write_text("corrupt")
    assert (await adapter.send(subscription(), "corrupt")).status == "disabled"
    assert not adapter.enabled and adapter.public_key is None


def test_stale_service_save_cannot_overwrite_another_admin_change(tmp_path):
    settings = adapter_settings(tmp_path)
    first, second = ServiceCredentialRegistry(settings), ServiceCredentialRegistry(settings)
    view, _ = first.snapshot(ID.FEISHU_APP)
    second.save(ID.FEISHU_APP, "other-admin-key", view["revision"])
    with pytest.raises(CredentialChangedError):
        first.save(ID.FEISHU_APP, "stale-key", view["revision"])
    assert first.resolve(ID.FEISHU_APP) == "other-admin-key"


def _erase_feishu_in_child(root):
    registry = ServiceCredentialRegistry(adapter_settings(root))
    erase(registry, ID.FEISHU_APP)


async def test_existing_worker_observes_credential_deletion_from_another_process(tmp_path):
    adapter = FeishuReplyAdapter(adapter_settings(tmp_path), transport=success_handler([]))
    await adapter.reply(target(), "before", idempotency_key="before")
    process = multiprocessing.get_context("spawn").Process(
        target=_erase_feishu_in_child,
        args=(tmp_path,),
    )
    process.start()
    try:
        await asyncio.to_thread(process.join, 15)
        assert process.exitcode == 0
    finally:
        if process.is_alive():
            process.terminate()
        await asyncio.to_thread(process.join, 5)
    with pytest.raises(ChannelDeliveryError, match="not configured"):
        await adapter.reply(target(), "blocked", idempotency_key="blocked")
    assert adapter._token is None


@pytest.fixture
def backup_source(tmp_path, monkeypatch):
    yield from application_source.__wrapped__(tmp_path, monkeypatch)


def test_cold_backup_keeps_service_deletion_receipts_and_explicit_saved_key(
    backup_source, tmp_path
):
    data, plugin, *_ = backup_source
    settings = Settings(_env_file=None, data_dir=data, feishu_encrypt_key="environment-event-key")
    registry = ServiceCredentialRegistry(settings)
    erased = erase(registry, ID.FEISHU_EVENTS)
    view, _ = registry.snapshot(ID.FEISHU_APP)
    saved = registry.save(ID.FEISHU_APP, "saved-private-service-key", view["revision"])
    archive, output = tmp_path / "backup", tmp_path / "restored"
    create_backup(data, plugin, archive)
    remove_staging(data)
    remove_staging(plugin)
    restore_backup(archive, output)
    restored = ServiceCredentialRegistry(settings.model_copy(update={"data_dir": output / "data"}))
    assert restored.snapshot(ID.FEISHU_EVENTS) == (erased, None)
    assert restored.snapshot(ID.FEISHU_APP) == (saved, "saved-private-service-key")


async def test_push_worker_recovers_after_explicit_key_save_and_rejects_old_key_subscription(
    tmp_path, monkeypatch,
):
    config = push_settings(tmp_path, push_worker_interval_seconds=1)
    app = create_app(config, runtime=FakeRuntime(), push_adapter=RecordingAdapter(config))
    async with owner_client(app) as owner, device_client(app) as device:
        project, device_id = await paired(app, owner, device)
        push = app.state.companion_push
        delivered = asyncio.Event()
        original_send = push.adapter.send

        async def observe_send(*args):
            result = await original_send(*args)
            delivered.set()
            return result

        monkeypatch.setattr(push.adapter, "send", observe_send)
        registry = app.state.service_credentials
        erased = erase(registry, ID.WEB_PUSH)
        app.state.notifications.add(title="Synthetic update", project_id=project.id)
        await push.start()
        try:
            await asyncio.sleep(0)
            assert not push.adapter.calls
            registry.save(ID.WEB_PUSH, VALUES[ID.WEB_PUSH], erased["revision"])
            await asyncio.wait_for(delivered.wait(), 4)
            assert push.store.get(device_id).wake.state == "accepted"
        finally:
            await push.stop()
        view, _ = registry.snapshot(ID.WEB_PUSH)
        registry.save(ID.WEB_PUSH, b64((45678).to_bytes(32, "big")), view["revision"])
        status = push.status(device_id)
        assert not status["subscribed"] and status["reason"] == "application_key_changed"
        await push.tick()
        assert len(push.adapter.calls) == 1 and push.store.get(device_id).subscription is None
