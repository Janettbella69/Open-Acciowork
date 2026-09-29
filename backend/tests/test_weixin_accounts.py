"""Weixin account authorization, recovery and secret retention in temporary vaults."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from ophira_backend.auth import AuthorizationError, ProjectAccessStore
from ophira_backend.constants import PrincipalRole
from ophira_backend.models import PrincipalContext, ProjectCreate
from ophira_backend.providers import WEIXIN_VAULT_FILENAME, FileSecretVault
from ophira_backend.storage import FileLedger
from ophira_backend.weixin_accounts import (
    CONTEXT_TTL,
    DEFAULT_BASE_URL,
    MAX_BUNDLE_BYTES,
    WeixinStore,
    WeixinStoreError,
)

NOW = datetime(2026, 9, 29, 12, tzinfo=UTC)
BOT_TOKEN = "private-account-token"
CONTEXT_TOKEN = "private-reply-context"


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr("ophira_backend.weixin_accounts.utc_now", lambda: NOW)


def principal(tenant="tenant_a", user="owner", role=PrincipalRole.OWNER,
              session="session_1234567890"):
    return PrincipalContext(
        tenant_id=tenant, principal_id=user, role=role, session_id=session,
        assertion_issued_at=NOW - timedelta(minutes=1),
        assertion_expires_at=NOW + timedelta(hours=1), assertion_nonce="nonce_" + uuid4().hex,
    )


def make_store(tmp_path: Path, *, actor=None):
    ledger = FileLedger(tmp_path / "data")
    vault = FileSecretVault(ledger.root / "secrets", WEIXIN_VAULT_FILENAME)
    access = ProjectAccessStore(ledger.root)
    return WeixinStore(
        ledger, vault, access,
        identity_mode="trusted_gateway" if actor else "local_only",
        trusted_tenant_id=actor.tenant_id if actor else None,
    )


def connect(store, actor=None, *, bot_id="bot@im.bot", token=BOT_TOKEN):
    pairing = store.create_pairing("private-qr", "private-qr-payload", actor)
    pairing = store.confirm_pairing(
        pairing.id, actor, bot_id=bot_id, owner_user_id="owner@im.wechat",
        token=token, base_url=DEFAULT_BASE_URL,
    )
    return store.get_account(pairing.account_id), pairing


def enable(store, actor=None):
    account, _ = connect(store, actor)
    project = store.ledger.create_project(ProjectCreate(name="Project", objective="Research"),
                                          principal=actor)
    if actor:
        store.access.bind_project(project.id, actor)
    return store.bind(account.id, project_id=project.id, enabled=True,
                      expected_revision=account.revision, actor=actor)


def state_path(store):
    return store.ledger.root / "channels" / "weixin" / "state.json"


def test_qr_confirmation_is_disabled_and_keeps_credentials_out_of_ledger(tmp_path):
    store = make_store(tmp_path)
    account, pairing = connect(store)
    assert account.status == "connected"
    assert account.enabled is False
    assert account.enabled_at is None
    assert account.project_id is None
    assert store.active_accounts() == []
    assert not store.vault.has(pairing.id)
    assert BOT_TOKEN not in state_path(store).read_text()
    with pytest.raises(WeixinStoreError, match="ACCOUNT_DISABLED"):
        store.credentials(account.id, account.revision)


def test_enable_binds_owner_project_and_creates_fresh_admission_time(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    account = enable(store)
    assert account.enabled_at == NOW
    resolved, binding = store.resolve(account.id, "owner@im.wechat", account.revision)
    assert resolved.project_id == account.project_id
    assert binding is None
    assert store.credentials(account.id, account.revision) == BOT_TOKEN
    with pytest.raises(WeixinStoreError, match="SENDER_UNBOUND"):
        store.resolve(account.id, "someone@im.wechat", account.revision)
    monkeypatch.setattr("ophira_backend.weixin_accounts.utc_now",
                        lambda: NOW + timedelta(seconds=1))
    rebound = store.bind(account.id, project_id=account.project_id, enabled=True,
                         expected_revision=account.revision, actor=None)
    assert rebound.enabled_at == NOW + timedelta(seconds=1)
    assert rebound.revision == account.revision + 1
    with pytest.raises(WeixinStoreError, match="REVISION_CONFLICT"):
        store.resolve(account.id, "owner@im.wechat", account.revision)


def test_tenant_binding_rechecks_durable_membership_and_scanning_session(tmp_path):
    actor = principal()
    store = make_store(tmp_path, actor=actor)
    account = enable(store, actor)
    _, inherited = store.resolve(account.id, "owner@im.wechat", account.revision)
    assert inherited.principal_id == actor.principal_id
    record = store.access.get(account.project_id)
    store.access._write(record.model_copy(update={"memberships": {}}))
    with pytest.raises(AuthorizationError):
        store.resolve(account.id, "owner@im.wechat", account.revision)
    with pytest.raises(AuthorizationError):
        store.credentials(account.id, account.revision)


@pytest.mark.parametrize("actor", [
    principal(tenant="tenant_b"), principal(user="another"),
    principal(session="different_1234567890"), None,
])
def test_pairing_read_update_cancel_confirm_require_original_tenant_user_session(tmp_path, actor):
    owner = principal()
    store = make_store(tmp_path, actor=owner)
    pairing = store.create_pairing("qr", "content", owner)
    for call in (
        lambda: store.get_pairing(pairing.id, actor),
        lambda: store.pairing_secret(pairing.id, actor),
        lambda: store.update_pairing(pairing.id, actor, "cancelled"),
        lambda: store.confirm_pairing(pairing.id, actor, bot_id="bot@im.bot",
                                     owner_user_id="owner@im.wechat", token=BOT_TOKEN,
                                     base_url=DEFAULT_BASE_URL),
    ):
        with pytest.raises(AuthorizationError):
            call()
    assert store.get_pairing(pairing.id, owner).status == "wait"


@pytest.mark.parametrize("actor", [
    principal(role=PrincipalRole.MEMBER),
    principal().model_copy(update={"assertion_expires_at": NOW}),
])
def test_internal_management_also_rejects_nonadmin_and_expired_assertions(tmp_path, actor):
    owner = principal()
    store = make_store(tmp_path, actor=owner)
    account = enable(store, owner)
    with pytest.raises(AuthorizationError):
        store.create_pairing("qr", "content", actor)
    with pytest.raises(AuthorizationError):
        store.bind(account.id, project_id=account.project_id, enabled=False,
                   expected_revision=account.revision, actor=actor)
    with pytest.raises(AuthorizationError):
        store.revoke(account.id, account.revision, actor)


def test_cancel_is_terminal_and_cannot_be_reopened_or_confirmed(tmp_path):
    store = make_store(tmp_path)
    pairing = store.create_pairing("qr", "content", None)
    store.update_pairing(pairing.id, None, "cancelled")
    assert not store.vault.has(pairing.id)
    assert store.update_pairing(pairing.id, None, "cancelled").status == "cancelled"
    with pytest.raises(WeixinStoreError, match="PAIRING_CLOSED"):
        store.update_pairing(pairing.id, None, "wait")
    with pytest.raises(WeixinStoreError, match="PAIRING_CLOSED"):
        store.confirm_pairing(pairing.id, None, bot_id="bot@im.bot",
                              owner_user_id="owner@im.wechat", token=BOT_TOKEN,
                              base_url=DEFAULT_BASE_URL)
    assert store.list_accounts() == []


def test_confirmation_requires_full_method_not_state_only_update(tmp_path):
    store = make_store(tmp_path)
    pairing = store.create_pairing("qr", "content", None)
    with pytest.raises(WeixinStoreError, match="PAIRING_CLOSED"):
        store.update_pairing(pairing.id, None, "confirmed")


def test_expiry_preserves_terminal_pairings_and_drops_pending_secret(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    _, confirmed = connect(store)
    cancelled = store.create_pairing("qr2", "content2", None)
    store.update_pairing(cancelled.id, None, "cancelled")
    waiting = store.create_pairing("qr3", "content3", None)
    monkeypatch.setattr("ophira_backend.weixin_accounts.utc_now",
                        lambda: NOW + timedelta(minutes=6))
    assert store.get_pairing(confirmed.id, None).status == "confirmed"
    assert store.get_pairing(cancelled.id, None).status == "cancelled"
    assert store.get_pairing(waiting.id, None).status == "expired"
    assert not store.vault.has(waiting.id)


def test_reconnect_rotates_credentials_disables_binding_and_invalidates_old_context(tmp_path):
    store = make_store(tmp_path)
    account = enable(store)
    ref = store.save_context(account.id, "message1", CONTEXT_TOKEN, account.revision)
    store.save_poll_state(account.id, {"cursor": "old-cursor"}, account.revision)
    connected, _ = connect(store, token="replacement-token")
    assert connected.id == account.id
    assert connected.revision == account.revision + 1
    assert connected.credential_revision == account.credential_revision + 1
    assert not connected.enabled
    assert connected.enabled_at is None
    assert connected.project_id is None
    assert store.get_poll_state(account.id) == {"cursor": ""}
    assert store._bundle(account.id) == {"token": "replacement-token", "contexts": {}}
    with pytest.raises(WeixinStoreError, match="REVISION_CONFLICT"):
        store.reply_context(account.id, ref, account.revision)


def test_existing_bot_cannot_be_reassigned_to_another_tenant(tmp_path):
    owner = principal()
    store = make_store(tmp_path, actor=owner)
    account, _ = connect(store, owner)
    other = principal(tenant="tenant_b")
    second = WeixinStore(store.ledger, store.vault, store.access,
                         identity_mode="trusted_gateway", trusted_tenant_id=other.tenant_id)
    pairing = second.create_pairing("qr2", "content2", other)
    with pytest.raises(AuthorizationError):
        second.confirm_pairing(pairing.id, other, bot_id=account.bot_id,
                               owner_user_id=account.owner_user_id, token="other-token",
                               base_url=DEFAULT_BASE_URL)
    assert store._bundle(account.id)["token"] == BOT_TOKEN


def test_context_is_private_idempotent_revision_bound_and_has_fixed_expiry(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    account = enable(store)
    ref = store.save_context(account.id, "message1", CONTEXT_TOKEN, account.revision)
    original = store._bundle(account.id)["contexts"][ref]
    monkeypatch.setattr("ophira_backend.weixin_accounts.utc_now", lambda: NOW + timedelta(hours=1))
    assert store.save_context(account.id, "message1", CONTEXT_TOKEN, account.revision) == ref
    assert store._bundle(account.id)["contexts"][ref]["expires_at"] == original["expires_at"]
    assert store.reply_context(account.id, ref, account.revision) == CONTEXT_TOKEN
    assert CONTEXT_TOKEN not in state_path(store).read_text()
    with pytest.raises(WeixinStoreError, match="CONTEXT_CONFLICT"):
        store.save_context(account.id, "message1", "changed", account.revision)
    with pytest.raises(TypeError):
        store.save_context(account.id, "message2", CONTEXT_TOKEN)


def test_pause_prevents_credentials_context_and_cursor_writes(tmp_path):
    store = make_store(tmp_path)
    account = enable(store)
    ref = store.save_context(account.id, "m1", CONTEXT_TOKEN, account.revision)
    paused = store.bind(account.id, project_id=account.project_id, enabled=False,
                        expected_revision=account.revision, actor=None)
    assert paused.enabled_at is None
    for call in (
        lambda: store.credentials(account.id, paused.revision),
        lambda: store.reply_context(account.id, ref, paused.revision),
        lambda: store.save_context(account.id, "m2", CONTEXT_TOKEN, paused.revision),
        lambda: store.save_poll_state(account.id, {"cursor": "new"}, paused.revision),
    ):
        with pytest.raises(WeixinStoreError, match="ACCOUNT_DISABLED"):
            call()
    assert store._bundle(account.id)["contexts"] == {}


def test_cleanup_physically_expires_contexts_but_keeps_account_credentials(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    account = enable(store)
    ref = store.save_context(account.id, "m1", CONTEXT_TOKEN, account.revision)
    monkeypatch.setattr("ophira_backend.weixin_accounts.utc_now", lambda: NOW + CONTEXT_TTL)
    with pytest.raises(WeixinStoreError, match="CONTEXT_EXPIRED"):
        store.reply_context(account.id, ref, account.revision)
    store.cleanup()
    assert store._bundle(account.id) == {"token": BOT_TOKEN, "contexts": {}}
    assert CONTEXT_TOKEN not in store.vault.path.read_text()


def test_cleanup_without_changes_does_not_rewrite_files(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    account = enable(store)
    store.save_context(account.id, "m1", CONTEXT_TOKEN, account.revision)
    monkeypatch.setattr(store, "_write", lambda _state: pytest.fail("state was unchanged"))
    monkeypatch.setattr(store.vault, "set", lambda *_args: pytest.fail("vault was unchanged"))
    store.cleanup()


def test_revoke_fails_closed_and_cleanup_recovers_secret_deletion(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    account = enable(store)
    store.save_context(account.id, "m1", CONTEXT_TOKEN, account.revision)
    original_delete = store.vault.delete
    monkeypatch.setattr(store.vault, "delete", lambda _key: (_ for _ in ()).throw(OSError("crash")))
    with pytest.raises(OSError):
        store.revoke(account.id, account.revision, None)
    current = store.get_account(account.id)
    assert current.status == "revoked" and not current.enabled
    assert current.enabled_at is None
    with pytest.raises(WeixinStoreError, match="ACCOUNT_DISABLED"):
        store.credentials(account.id, current.revision)
    monkeypatch.setattr(store.vault, "delete", original_delete)
    store.cleanup()
    assert not store.vault.has(account.id)


def test_reconnect_crash_before_token_replace_leaves_old_account_revoked(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    account = enable(store)
    pairing = store.create_pairing("new-qr", "new-content", None)
    original_set = store.vault.set
    monkeypatch.setattr(store.vault, "set", lambda *_args: (_ for _ in ()).throw(OSError("crash")))
    with pytest.raises(OSError):
        store.confirm_pairing(pairing.id, None, bot_id=account.bot_id,
                              owner_user_id=account.owner_user_id, token="new-token",
                              base_url=DEFAULT_BASE_URL)
    assert store.get_account(account.id).status == "revoked"
    assert store.active_accounts() == []
    monkeypatch.setattr(store.vault, "set", original_set)
    store.cleanup()
    assert not store.vault.has(account.id)


def test_detach_deletes_project_contexts_cursor_and_binding_but_retains_account(tmp_path):
    store = make_store(tmp_path)
    account = enable(store)
    store.save_context(account.id, "m1", CONTEXT_TOKEN, account.revision)
    store.save_poll_state(account.id, {"cursor": "project-cursor"}, account.revision)
    with store.ledger.lifecycle.lease(account.project_id, exclusive=True):
        store.detach_project(account.project_id)
        store.detach_project(account.project_id)
    detached = store.get_account(account.id)
    assert detached.project_id is None
    assert detached.principal_binding is None
    assert detached.enabled_at is None
    assert not detached.enabled
    assert detached.revision == account.revision + 1
    assert store._bundle(account.id) == {"token": BOT_TOKEN, "contexts": {}}
    assert store.get_poll_state(account.id) == {"cursor": ""}


def test_detach_failure_does_not_hide_binding_and_is_retryable_after_crash(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    account = enable(store)
    store.save_context(account.id, "m1", CONTEXT_TOKEN, account.revision)
    original = store._write
    monkeypatch.setattr(store, "_write", lambda _state: (_ for _ in ()).throw(OSError("crash")))
    with pytest.raises(OSError):
        store.detach_project(account.project_id)
    assert store.get_account(account.id).project_id == account.project_id
    assert store._bundle(account.id)["contexts"] == {}
    monkeypatch.setattr(store, "_write", original)
    store.detach_project(account.project_id)
    assert store.get_account(account.id).project_id is None


@pytest.mark.parametrize("key,value", [
    ("enabled", "false"), ("enabled_at", None), ("revision", True),
    ("owner_user_id", "wrong@chatroom"), ("tenant_id", "alien"),
])
def test_incoherent_account_state_fails_closed(tmp_path, key, value):
    store = make_store(tmp_path)
    account = enable(store)
    raw = json.loads(state_path(store).read_text())
    raw["accounts"][account.id][key] = value
    state_path(store).write_text(json.dumps(raw))
    with pytest.raises(WeixinStoreError, match="STATE_INVALID"):
        store.get_account(account.id)


@pytest.mark.parametrize("body", [
    '{"accounts":{},"accounts":{}}',
    '{"accounts":[],"pairings":{},"polls":{}}',
    '[]', '{"pairings":',
])
def test_malformed_state_json_is_rejected_without_repair_or_overwrite(tmp_path, body):
    store = make_store(tmp_path)
    connect(store)
    state_path(store).write_text(body)
    with pytest.raises(WeixinStoreError, match="STATE_INVALID"):
        store.list_accounts()
    assert state_path(store).read_text() == body


@pytest.mark.parametrize("raw", [
    '[]', '{"token":"a","token":"b","contexts":{}}',
    '{"token":"private-account-token","contexts":{"x":{}}}',
    '{"token":"","contexts":{}}', 'x' * (MAX_BUNDLE_BYTES + 1),
])
def test_malformed_private_bundle_never_becomes_usable_credentials(tmp_path, raw):
    store = make_store(tmp_path)
    account = enable(store)
    store.vault.set(account.id, raw)
    with pytest.raises(WeixinStoreError, match="CREDENTIAL_INVALID") as error:
        store.credentials(account.id, account.revision)
    assert BOT_TOKEN not in str(error.value)


def test_naive_context_expiry_is_rejected_without_leaking_token(tmp_path):
    store = make_store(tmp_path)
    account = enable(store)
    ref = store.save_context(account.id, "m1", CONTEXT_TOKEN, account.revision)
    bundle = store._bundle(account.id)
    bundle["contexts"][ref]["expires_at"] = "2026-09-29T13:00:00"
    store.vault.set(account.id, json.dumps(bundle))
    with pytest.raises(WeixinStoreError, match="CREDENTIAL_INVALID"):
        store.reply_context(account.id, ref, account.revision)


@pytest.mark.parametrize("link_type", ["symbolic", "hard"])
def test_linked_ledger_and_poll_lock_files_are_rejected(tmp_path, link_type):
    store = make_store(tmp_path)
    account, _ = connect(store)
    path = state_path(store)
    external = tmp_path / "external-state"
    path.rename(external)
    if link_type == "symbolic":
        path.symlink_to(external)
    else:
        os.link(external, path)
    with pytest.raises((OSError, ValueError)):
        store.list_accounts()
    path.unlink()
    external.rename(path)
    slot = int(hashlib.sha256(account.id.encode()).hexdigest()[:8], 16) % 4096
    lock = path.parent / f"poll-{slot:04d}.lock"
    outside_lock = tmp_path / "outside-lock"
    outside_lock.write_text("keep")
    if link_type == "symbolic":
        lock.symlink_to(outside_lock)
    else:
        os.link(outside_lock, lock)
    with pytest.raises((OSError, ValueError)):
        with store.poll_lease(account.id):
            pytest.fail("linked lock acquired")
    assert outside_lock.read_text() == "keep"


def test_poll_lease_excludes_a_second_store_without_blocking(tmp_path):
    first = make_store(tmp_path)
    second = make_store(tmp_path)
    account, _ = connect(first)
    with first.poll_lease(account.id) as acquired:
        assert acquired
        with second.poll_lease(account.id) as acquired_again:
            assert not acquired_again
    with second.poll_lease(account.id) as acquired_after:
        assert acquired_after


@pytest.mark.parametrize("source_actor", [None, principal()])
def test_deployment_identity_mode_change_cannot_resume_old_accounts(tmp_path, source_actor):
    store = make_store(tmp_path, actor=source_actor)
    account = enable(store, source_actor)
    other = WeixinStore(
        store.ledger, store.vault, store.access,
        identity_mode="local_only" if source_actor else "trusted_gateway",
        trusted_tenant_id=None if source_actor else "tenant_a",
    )
    assert other.active_accounts() == []
    with pytest.raises(AuthorizationError):
        other.resolve(account.id, account.owner_user_id, account.revision)
    with pytest.raises(AuthorizationError):
        other.credentials(account.id, account.revision)


def test_account_context_storage_has_a_byte_limit_below_vault_limit(tmp_path):
    store = make_store(tmp_path)
    account = enable(store)
    # Construct a still valid near-limit bundle directly in the temporary vault.
    bundle = {"token": BOT_TOKEN, "contexts": {}}
    expiry = (NOW + CONTEXT_TTL).isoformat()
    for index in range(125):
        ref = "wxctx_" + hashlib.sha256(str(index).encode()).hexdigest()
        bundle["contexts"][ref] = {"token": "x" * 8192, "revision": account.revision,
                                   "expires_at": expiry}
    store._save_bundle(account.id, bundle)
    prior = store.vault.get(account.id)
    with pytest.raises(WeixinStoreError, match="CONTEXT_LIMIT"):
        for index in range(20):
            store.save_context(account.id, f"new-{index}", "x" * 8192, account.revision)
    assert len(store.vault.get(account.id).encode()) <= MAX_BUNDLE_BYTES
    assert prior is not None


def test_atomic_cancel_preserves_confirmation_if_poll_wins(tmp_path):
    store = make_store(tmp_path)
    account, confirmed = connect(store)
    cancelled = store.cancel_pairing(confirmed.id, None)
    assert cancelled == confirmed
    assert store.get_account(account.id).status == "connected"
    pending = store.create_pairing("pending", "content", None)
    cancelled = store.cancel_pairing(pending.id, None)
    assert cancelled.status == "cancelled"
    assert store.cancel_pairing(pending.id, None) == cancelled
    assert not store.vault.has(pending.id)
