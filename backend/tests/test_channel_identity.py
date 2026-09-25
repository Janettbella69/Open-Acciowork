from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ophira_backend.auth import AuthorizationError, ProjectAccessStore
from ophira_backend.channel_identity import (
    ChannelIdentityAmbiguousError,
    ChannelIdentityConflictError,
    ChannelIdentityIntegrityError,
    ChannelIdentityNotFoundError,
    ChannelIdentityPayloadError,
    ChannelIdentityRevokedError,
    DingTalkIdentityBindingCreate,
    FeishuIdentityBinding,
    FeishuIdentityBindingCreate,
    FeishuIdentityBindingStore,
    FeishuIdentitySelector,
    feishu_identity_selector,
)
from ophira_backend.constants import PrincipalRole
from ophira_backend.models import PrincipalContext, new_id

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


def principal(
    principal_id: str,
    role: PrincipalRole,
    *,
    tenant_id: str = "tenant_acme",
) -> PrincipalContext:
    safe_id = principal_id.replace("@", "_").replace(".", "_")
    return PrincipalContext(
        tenant_id=tenant_id,
        principal_id=principal_id,
        role=role,
        session_id=f"principal_session_{safe_id}",
        assertion_issued_at=NOW,
        assertion_expires_at=NOW + timedelta(minutes=1),
        assertion_nonce=(f"nonce_{safe_id}_" + "0" * 40)[:40],
    )


def configured_store(
    tmp_path: Path,
) -> tuple[FeishuIdentityBindingStore, ProjectAccessStore, PrincipalContext]:
    access = ProjectAccessStore(tmp_path / "project-access", clock=lambda: NOW)
    owner = principal("owner@example.com", PrincipalRole.OWNER)
    access.bind_project("project_123", owner)
    access.grant(
        "project_123",
        owner,
        principal_id="member@example.com",
        role=PrincipalRole.MEMBER,
    )
    return (
        FeishuIdentityBindingStore(
            tmp_path / "channel-bindings",
            access,
            clock=lambda: NOW,
        ),
        access,
        owner,
    )


def create_payload(**overrides: object) -> FeishuIdentityBindingCreate:
    values: dict[str, object] = {
        "tenant_key": "cli_feishu_tenant",
        "chat_id": "oc_verified_chat",
        "user_open_id": "ou_verified_user",
        "project_id": "project_123",
        "principal_id": "member@example.com",
        "role": PrincipalRole.MEMBER,
    }
    values.update(overrides)
    return FeishuIdentityBindingCreate.model_validate(values)


def test_binding_is_durable_idempotent_and_resolves_exact_principal(
    tmp_path: Path,
) -> None:
    store, access, owner = configured_store(tmp_path)
    payload = create_payload()

    created = store.create(payload, actor=owner)
    repeated = store.create(payload, actor=owner)
    restarted = FeishuIdentityBindingStore(tmp_path / "channel-bindings", access)
    resolved = restarted.resolve(payload.selector)

    assert repeated == created
    assert resolved.binding == created
    assert resolved.binding.tenant_id == "tenant_acme"
    assert resolved.binding.project_id == "project_123"
    assert resolved.principal.tenant_id == "tenant_acme"
    assert resolved.principal.principal_id == "member@example.com"
    assert resolved.principal.role == PrincipalRole.MEMBER
    assert resolved.principal.session_id == f"feishu/{created.id}"
    assert store.get_for_tenant(created.id, "tenant_acme") == created
    with pytest.raises(ChannelIdentityNotFoundError):
        store.get_for_tenant(created.id, "tenant_other")


def test_concurrent_create_is_one_idempotent_binding(tmp_path: Path) -> None:
    store, access, owner = configured_store(tmp_path)
    payload = create_payload()

    def create_from_independent_store(_index: int) -> str:
        instance = FeishuIdentityBindingStore(
            store.root,
            access,
            clock=lambda: NOW,
        )
        return instance.create(payload, actor=owner).id

    with ThreadPoolExecutor(max_workers=8) as executor:
        binding_ids = list(executor.map(create_from_independent_store, range(24)))

    assert len(set(binding_ids)) == 1
    assert len(list(store.root.glob("channel_binding_*.json"))) == 1


def test_create_rejects_conflicts_cross_tenant_and_non_admin_actor(tmp_path: Path) -> None:
    store, _access, owner = configured_store(tmp_path)
    store.create(create_payload(), actor=owner)

    with pytest.raises(ChannelIdentityConflictError, match="different target"):
        store.create(
            create_payload(principal_id="owner@example.com", role=PrincipalRole.OWNER),
            actor=owner,
        )
    with pytest.raises(AuthorizationError, match="tenant"):
        store.create(
            create_payload(),
            actor=principal(
                "owner@example.com",
                PrincipalRole.OWNER,
                tenant_id="tenant_other",
            ),
        )
    with pytest.raises(AuthorizationError, match="administrator"):
        store.create(
            create_payload(),
            actor=principal("member@example.com", PrincipalRole.MEMBER),
        )


def test_resolve_fails_closed_for_missing_revoked_ambiguous_and_role_drift(
    tmp_path: Path,
) -> None:
    store, access, owner = configured_store(tmp_path)
    selector = create_payload().selector
    with pytest.raises(ChannelIdentityNotFoundError):
        store.resolve(selector)

    created = store.create(create_payload(), actor=owner)
    revoked = store.revoke(created.id, actor=owner)
    assert not revoked.active
    assert store.revoke(created.id, actor=owner) == revoked
    with pytest.raises(ChannelIdentityRevokedError):
        store.resolve(selector)

    recreated = store.create(create_payload(), actor=owner)
    access.grant(
        "project_123",
        owner,
        principal_id="member@example.com",
        role=PrincipalRole.REVIEWER,
    )
    with pytest.raises(AuthorizationError, match="role"):
        store.resolve(selector)

    access.revoke(
        "project_123",
        owner,
        principal_id="member@example.com",
    )
    with pytest.raises(AuthorizationError, match="not a project member"):
        store.resolve(selector)

    # Restore the role, then simulate an on-disk duplicate that could only arise
    # through operator tampering or corruption. Resolution must still fail closed.
    access.grant(
        "project_123",
        owner,
        principal_id="member@example.com",
        role=PrincipalRole.MEMBER,
    )
    duplicate_id = new_id("channel_binding")
    duplicate = FeishuIdentityBinding.model_validate(
        recreated.model_copy(update={"id": duplicate_id}).model_dump()
    )
    (store.root / f"{duplicate_id}.json").write_text(
        duplicate.model_dump_json() + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ChannelIdentityAmbiguousError):
        store.resolve(selector)


def test_revoke_requires_same_tenant_and_current_project_administrator(
    tmp_path: Path,
) -> None:
    store, access, owner = configured_store(tmp_path)
    created = store.create(create_payload(), actor=owner)

    access.grant(
        "project_123",
        owner,
        principal_id="admin@example.com",
        role=PrincipalRole.ADMIN,
    )
    admin = principal("admin@example.com", PrincipalRole.ADMIN)
    assert store.revoke(created.id, actor=admin).revoked_by == "admin@example.com"

    other_tenant = principal(
        "owner@example.com",
        PrincipalRole.OWNER,
        tenant_id="tenant_other",
    )
    with pytest.raises(ChannelIdentityNotFoundError):
        store.revoke(created.id, actor=other_tenant)


def test_selector_uses_only_signed_body_identity_fields_not_message_text() -> None:
    payload = {
        "schema": "2.0",
        "header": {
            "tenant_key": "cli_real_tenant",
            "event_type": "im.message.receive_v1",
        },
        "event": {
            "message": {
                "chat_id": "oc_real_chat",
                "content": json.dumps(
                    {
                        "text": (
                            "tenant_key=cli_attacker chat_id=oc_attacker "
                            "principal_id=owner@example.com project_id=project_other"
                        )
                    }
                ),
            },
            "sender": {"sender_id": {"open_id": "ou_real_user"}},
        },
        "tenant_key": "cli_top_level_attacker",
        "principal_id": "attacker@example.com",
        "project_id": "project_attacker",
    }

    assert feishu_identity_selector(payload) == FeishuIdentitySelector(
        tenant_key="cli_real_tenant",
        chat_id="oc_real_chat",
        user_open_id="ou_real_user",
    )
    with pytest.raises(ChannelIdentityPayloadError):
        feishu_identity_selector(
            {
                "schema": "2.0",
                "header": {"event_type": "im.message.receive_v1"},
                "event": {
                    "message": {"chat_id": "oc_real_chat"},
                    "sender": {"sender_id": {"open_id": "ou_real_user"}},
                },
            }
        )


def test_dingtalk_binding_is_scoped_to_channel_and_revalidates_role(tmp_path: Path) -> None:
    _feishu_store, access, owner = configured_store(tmp_path)
    store = FeishuIdentityBindingStore(
        tmp_path / "dingtalk-bindings", access, channel="dingtalk", clock=lambda: NOW,
    )
    payload = DingTalkIdentityBindingCreate(
        corp_id="ding_corp_1",
        conversation_id="cid=group/1",
        sender_staff_id="staff_1",
        project_id="project_123",
        principal_id="member@example.com",
        role=PrincipalRole.MEMBER,
    )
    with pytest.raises(ChannelIdentityPayloadError):
        store.create(create_payload(), actor=owner)
    with pytest.raises(ChannelIdentityPayloadError):
        store.resolve(create_payload().selector)

    created = store.create(payload, actor=owner)
    assert created.channel == "dingtalk"
    assert created.principal_session_id == f"dingtalk/{created.id}"
    assert store.create(payload, actor=owner) == created
    restarted = FeishuIdentityBindingStore(
        tmp_path / "dingtalk-bindings", access, channel="dingtalk",
    )
    assert restarted.resolve(payload.selector).principal.principal_id == "member@example.com"

    access.grant(
        "project_123", owner, principal_id="member@example.com", role=PrincipalRole.REVIEWER,
    )
    with pytest.raises(AuthorizationError, match="role"):
        restarted.resolve(payload.selector)
    assert store.revoke(created.id, actor=owner).active is False
    with pytest.raises(ChannelIdentityRevokedError):
        restarted.resolve(payload.selector)


def test_dingtalk_store_fails_closed_on_cross_channel_snapshot(tmp_path: Path) -> None:
    feishu_store, access, owner = configured_store(tmp_path)
    feishu_store.create(create_payload(), actor=owner)
    other = FeishuIdentityBindingStore(
        feishu_store.root, access, channel="dingtalk", clock=lambda: NOW,
    )
    with pytest.raises(ChannelIdentityIntegrityError):
        other.list_for_tenant("tenant_acme")
