from __future__ import annotations

import hashlib
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from ophira_backend.auth import build_gateway_assertion_headers
from ophira_backend.commercial_adapters import CommercialTargetObservation
from ophira_backend.config import Settings
from ophira_backend.constants import CommercialActionOperation, PrincipalRole
from ophira_backend.main import create_app
from ophira_backend.models import (
    CommercialActionReceiptStage,
    OrgPolicy,
    PrincipalContext,
)

SECRET = "test-trusted-gateway-signing-secret-api-0001"
TENANT_ID = "tenant_acme"


class FakeRuntime:
    async def run(self, _run) -> None:
        return None

    async def cancel(self, _run_id: str) -> None:
        return None


class TrustedPriceReader:
    config_hash = "a" * 64

    def read_target(
        self,
        connector_id: str,
        resource_type: str,
        resource_key: str,
    ) -> CommercialTargetObservation:
        return CommercialTargetObservation(
            operation=CommercialActionOperation.CHANGE_PRICE,
            connector_id=connector_id,
            resource_type=resource_type,
            resource_key=resource_key,
            version="etag-7",
            state={"price": {"amount": 100, "currency": "USD"}},
            connector_config_hash=self.config_hash,
            canonical_source=(
                f"https://commerce.test/api/v1/targets/{resource_type}/{resource_key}"
            ),
        )

    def current_connector_config_hash(self, _connector_id: str) -> str:
        return self.config_hash


def trusted_settings(tmp_path: Path, **overrides) -> Settings:
    values = {
        "data_dir": tmp_path,
        "auth_mode": "trusted_gateway",
        "trusted_gateway_auth_secret": SECRET,
        "trusted_gateway_tenant_id": TENANT_ID,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def principal(
    principal_id: str,
    role: PrincipalRole,
    *,
    session_id: str | None = None,
) -> PrincipalContext:
    now = datetime.now(UTC)
    return PrincipalContext(
        tenant_id=TENANT_ID,
        principal_id=principal_id,
        role=role,
        session_id=session_id or f"gateway_session_{principal_id.replace('@', '_')}",
        assertion_issued_at=now,
        assertion_expires_at=now + timedelta(seconds=60),
        assertion_nonce=f"nonce_{uuid4().hex}",
    )


def signed_headers(
    method: str,
    path: str,
    principal_id: str,
    role: PrincipalRole,
    *,
    tenant_id: str = TENANT_ID,
    session_id: str | None = None,
) -> dict[str, str]:
    return build_gateway_assertion_headers(
        SECRET,
        method=method,
        path=path,
        tenant_id=tenant_id,
        principal_id=principal_id,
        role=role,
        session_id=session_id or f"gateway_session_{principal_id.replace('@', '_')}",
        issued_at=datetime.now(UTC),
        nonce=f"nonce_{uuid4().hex}",
    )


async def request_as(
    client: AsyncClient,
    method: str,
    path: str,
    principal_id: str,
    role: PrincipalRole,
    **kwargs,
):
    headers = signed_headers(method, urlsplit(path).path, principal_id, role)
    headers.update(kwargs.pop("headers", {}))
    return await client.request(method, path, headers=headers, **kwargs)


@pytest.mark.asyncio
async def test_trusted_api_rejects_unsigned_replayed_cross_tenant_and_forged_role(
    tmp_path: Path,
) -> None:
    app = create_app(trusted_settings(tmp_path), runtime=FakeRuntime())
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="https://gateway.test",
    ) as client:
        unsigned = await client.get(
            "/api/projects",
            headers={"X-Ophira-Authenticated-User": "owner@example.com"},
        )
        assert unsigned.status_code == 401

        headers = signed_headers(
            "GET",
            "/api/auth/status",
            "owner@example.com",
            PrincipalRole.OWNER,
        )
        status_response = await client.get("/api/auth/status", headers=headers)
        assert status_response.status_code == 200
        assert status_response.json() == {
            "mode": "trusted_gateway",
            "authenticated": True,
            "multi_tenant": False,
            "trusted_gateway_assertions_required": True,
        }
        replay = await client.get("/api/auth/status", headers=headers)
        assert replay.status_code == 401

        wrong_tenant = await client.get(
            "/api/projects",
            headers=signed_headers(
                "GET",
                "/api/projects",
                "owner@example.com",
                PrincipalRole.OWNER,
                tenant_id="tenant_other",
            ),
        )
        assert wrong_tenant.status_code == 401

        forged = signed_headers(
            "GET",
            "/api/projects",
            "member@example.com",
            PrincipalRole.MEMBER,
        )
        forged["X-Ophira-Auth-Role"] = "owner"
        forged_role = await client.get("/api/projects", headers=forged)
        assert forged_role.status_code == 401


@pytest.mark.asyncio
async def test_failed_project_write_compensates_the_initial_access_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(trusted_settings(tmp_path), runtime=FakeRuntime())

    def fail_project_write(*_args, **_kwargs):
        raise OSError("fixture project write failure")

    monkeypatch.setattr(app.state.ledger, "create_project", fail_project_write)
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="https://gateway.test",
    ) as client:
        response = await request_as(
            client,
            "POST",
            "/api/projects",
            "owner@example.com",
            PrincipalRole.OWNER,
            json={"name": "Kettle", "objective": "Validate demand"},
        )

    assert response.status_code == 500
    assert list((tmp_path / "auth" / "projects").glob("*/access.json")) == []


@pytest.mark.asyncio
async def test_project_access_api_enforces_membership_role_and_memory_scope(
    tmp_path: Path,
) -> None:
    app = create_app(trusted_settings(tmp_path), runtime=FakeRuntime())
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="https://gateway.test",
    ) as client:
        created = await request_as(
            client,
            "POST",
            "/api/projects",
            "owner@example.com",
            PrincipalRole.OWNER,
            json={"name": "Kettle", "objective": "Validate demand"},
        )
        assert created.status_code == 201
        project_id = created.json()["id"]

        for principal_id in ("member-a@example.com", "member-b@example.com"):
            granted = await request_as(
                client,
                "POST",
                f"/api/projects/{project_id}/access",
                "owner@example.com",
                PrincipalRole.OWNER,
                json={"principal_id": principal_id, "role": "member"},
            )
            assert granted.status_code == 200

        outsider_projects = await request_as(
            client,
            "GET",
            "/api/projects",
            "outsider@example.com",
            PrincipalRole.MEMBER,
        )
        assert outsider_projects.status_code == 200
        assert outsider_projects.json() == []
        outsider_read = await request_as(
            client,
            "GET",
            f"/api/projects/{project_id}",
            "outsider@example.com",
            PrincipalRole.MEMBER,
        )
        assert outsider_read.status_code == 403

        elevated_claim = await request_as(
            client,
            "GET",
            f"/api/projects/{project_id}",
            "member-a@example.com",
            PrincipalRole.OWNER,
        )
        assert elevated_claim.status_code == 403

        user_memory = await request_as(
            client,
            "POST",
            "/api/memory",
            "member-a@example.com",
            PrincipalRole.MEMBER,
            json={
                "scope": "user",
                "scope_id": "default",
                "text": "Member A private preference",
                "kind": "preference",
            },
        )
        assert user_memory.status_code == 201
        memory_id = user_memory.json()["id"]
        assert user_memory.json()["principal_id"] == "member-a@example.com"

        other_user_list = await request_as(
            client,
            "GET",
            "/api/memory?scope=user&scope_id=default",
            "member-b@example.com",
            PrincipalRole.MEMBER,
        )
        assert other_user_list.status_code == 200
        assert other_user_list.json() == []
        other_user_patch = await request_as(
            client,
            "PATCH",
            f"/api/memory/{memory_id}",
            "member-b@example.com",
            PrincipalRole.MEMBER,
            json={"text": "Overwrite another user's memory"},
        )
        assert other_user_patch.status_code == 403

        project_memory = await request_as(
            client,
            "POST",
            "/api/memory",
            "member-a@example.com",
            PrincipalRole.MEMBER,
            json={
                "scope": "project",
                "scope_id": project_id,
                "text": "Shared project fact",
                "kind": "fact",
            },
        )
        assert project_memory.status_code == 201
        shared = await request_as(
            client,
            "GET",
            f"/api/memory?scope=project&scope_id={project_id}",
            "member-b@example.com",
            PrincipalRole.MEMBER,
        )
        assert [item["id"] for item in shared.json()] == [project_memory.json()["id"]]
        outsider_shared = await request_as(
            client,
            "GET",
            f"/api/memory?scope=project&scope_id={project_id}",
            "outsider@example.com",
            PrincipalRole.MEMBER,
        )
        assert outsider_shared.status_code == 200
        assert outsider_shared.json() == []

        viewer_global_write = await request_as(
            client,
            "PUT",
            "/api/providers/provider_x",
            "viewer@example.com",
            PrincipalRole.VIEWER,
            json={},
        )
        assert viewer_global_write.status_code == 403


@pytest.mark.asyncio
async def test_commercial_review_and_apply_use_request_principal_and_server_source_scope(
    tmp_path: Path,
) -> None:
    app = create_app(trusted_settings(tmp_path), runtime=FakeRuntime())
    app.state.commercial_actions.target_reader = TrustedPriceReader()
    app.state.commercial_actions.executor = None
    app.state.policy.save(
        OrgPolicy(
            commercial_actions_enabled=True,
            allowed_commercial_operations=[CommercialActionOperation.CHANGE_PRICE],
            allowed_commercial_connector_ids=["shopify"],
        )
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="https://gateway.test",
    ) as client:
        created = await request_as(
            client,
            "POST",
            "/api/projects",
            "owner@example.com",
            PrincipalRole.OWNER,
            json={"name": "Kettle", "objective": "Validate demand"},
        )
        project_id = created.json()["id"]
        for principal_id, role in (
            ("member@example.com", PrincipalRole.MEMBER),
            ("reviewer@example.com", PrincipalRole.REVIEWER),
            ("viewer@example.com", PrincipalRole.VIEWER),
        ):
            granted = await request_as(
                client,
                "POST",
                f"/api/projects/{project_id}/access",
                "owner@example.com",
                PrincipalRole.OWNER,
                json={"principal_id": principal_id, "role": role.value},
            )
            assert granted.status_code == 200

        member = principal("member@example.com", PrincipalRole.MEMBER)
        run = app.state.ledger.create_run(
            project_id,
            "Stage a reviewed price change",
            principal=member,
        )
        run = app.state.ledger.update_run(
            run.id,
            sdk_session_id="sdk_authenticated_commercial_api",
        )
        observed = app.state.commercial_actions.observe_target(
            project_id,
            source_run_id=run.id,
            source_session_id=run.sdk_session_id,
            connector_id="shopify",
            resource_type="listing",
            resource_key="sku-100",
        )
        staged = app.state.commercial_actions.stage_from_receipt(
            project_id,
            CommercialActionReceiptStage(
                source_run_id=run.id,
                source_session_id=run.sdk_session_id,
                receipt_id=observed.receipt.id,
                changes=[
                    {"field": "price.amount", "before": 100, "after": 95},
                ],
                reason="Stage the host-observed launch discount",
            ),
        )
        review_path = (
            f"/api/projects/{project_id}/commercial-actions/{staged.id}/review"
        )
        denied = await request_as(
            client,
            "POST",
            review_path,
            "viewer@example.com",
            PrincipalRole.VIEWER,
            json={
                "decision": "approve",
                "reason": "A viewer must not approve",
                "valid_for_seconds": 900,
            },
        )
        assert denied.status_code == 403
        approved = await request_as(
            client,
            "POST",
            review_path,
            "reviewer@example.com",
            PrincipalRole.REVIEWER,
            json={
                "decision": "approve",
                "reason": "Independent reviewer confirmed the discount",
                "valid_for_seconds": 900,
            },
        )
        assert approved.status_code == 200
        assert approved.json()["review"]["actor_id"] == "reviewer@example.com"

        apply_path = f"/api/projects/{project_id}/commercial-actions/{staged.id}/apply"
        client_supplied_scope = await request_as(
            client,
            "POST",
            apply_path,
            "reviewer@example.com",
            PrincipalRole.REVIEWER,
            json={
                "reason": "Attempt to supply internal source authority",
                "source_run_id": run.id,
                "source_session_id": run.sdk_session_id,
            },
        )
        assert client_supplied_scope.status_code == 422
        applied = await request_as(
            client,
            "POST",
            apply_path,
            "reviewer@example.com",
            PrincipalRole.REVIEWER,
            json={"reason": "Queue the independently approved change"},
        )
        assert applied.status_code == 202
        execution = applied.json()["execution_request"]
        assert execution["requested_by"] == "reviewer@example.com"
        assert execution["requested_tenant_id"] == TENANT_ID


@pytest.mark.asyncio
async def test_schedule_and_intent_keep_authority_and_fail_after_membership_revoke(
    tmp_path: Path,
) -> None:
    app = create_app(trusted_settings(tmp_path), runtime=FakeRuntime())
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="https://gateway.test",
    ) as client:
        project = await request_as(
            client,
            "POST",
            "/api/projects",
            "owner@example.com",
            PrincipalRole.OWNER,
            json={"name": "Watch", "objective": "Monitor demand"},
        )
        project_id = project.json()["id"]
        grant = await request_as(
            client,
            "POST",
            f"/api/projects/{project_id}/access",
            "owner@example.com",
            PrincipalRole.OWNER,
            json={"principal_id": "member@example.com", "role": "member"},
        )
        assert grant.status_code == 200

        scheduled_for = datetime.now(UTC) + timedelta(days=1)
        schedule = await request_as(
            client,
            "POST",
            "/api/schedules",
            "member@example.com",
            PrincipalRole.MEMBER,
            json={
                "project_id": project_id,
                "prompt": "Refresh demand evidence",
                "run_once_at": scheduled_for.isoformat(),
            },
        )
        assert schedule.status_code == 201
        assert schedule.json()["principal_binding"]["principal_id"] == (
            "member@example.com"
        )

        intent = await request_as(
            client,
            "POST",
            "/api/intents",
            "member@example.com",
            PrincipalRole.MEMBER,
            json={
                "project_id": project_id,
                "intent": "Tell me when verified demand exceeds the threshold",
            },
        )
        assert intent.status_code == 201
        assert intent.json()["principal_binding"]["principal_id"] == (
            "member@example.com"
        )

        revoked = await request_as(
            client,
            "DELETE",
            f"/api/projects/{project_id}/access/member@example.com",
            "owner@example.com",
            PrincipalRole.OWNER,
        )
        assert revoked.status_code == 200
        assert "member@example.com" not in revoked.json()["memberships"]

        run_schedule = await request_as(
            client,
            "POST",
            f"/api/schedules/{schedule.json()['id']}/run",
            "owner@example.com",
            PrincipalRole.OWNER,
        )
        assert run_schedule.status_code == 403
        assert app.state.ledger.get_schedule(schedule.json()["id"]).enabled is False

        app.state.intent_watcher._ready_provider_id = lambda _intent: "provider_ready"
        checked = await request_as(
            client,
            "POST",
            f"/api/intents/{intent.json()['id']}/check",
            "owner@example.com",
            PrincipalRole.OWNER,
        )
        assert checked.status_code == 202
        assert checked.json()["outcome"] == "backoff"
        assert app.state.ledger.list_runs(project_id) == []


@pytest.mark.asyncio
async def test_local_only_rejects_proxy_identity_and_ignores_legacy_headers(
    tmp_path: Path,
) -> None:
    app = create_app(Settings(_env_file=None, data_dir=tmp_path), runtime=FakeRuntime())
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://127.0.0.1",
    ) as client:
        local = await client.get(
            "/api/auth/status",
            headers={"X-Ophira-Authenticated-User": "attacker@example.com"},
        )
        assert local.status_code == 200
        assert local.json()["authenticated"] is False
        proxied = await client.get(
            "/api/projects",
            headers={"X-Forwarded-For": "127.0.0.1"},
        )
        assert proxied.status_code == 403


@pytest.mark.asyncio
async def test_trusted_feishu_event_uses_channel_signature_but_needs_durable_binding(
    tmp_path: Path,
) -> None:
    key = "feishu-encrypt-key"
    app = create_app(
        trusted_settings(
            tmp_path,
            feishu_encrypt_key=key,
            feishu_bot_open_id="ou_bot",
        ),
        runtime=FakeRuntime(),
    )
    payload = {
        "schema": "2.0",
        "header": {
            "event_id": "evt_trusted_binding",
            "event_type": "im.message.receive_v1",
        },
        "event": {
            "message": {
                "message_id": "om_trusted_binding",
                "chat_id": "oc_trusted_binding",
                "chat_type": "p2p",
                "content": json.dumps({"text": "Check supplier availability"}),
            },
            "sender": {"sender_id": {"open_id": "ou_sender"}},
        },
    }
    body = json.dumps(payload, separators=(",", ":")).encode()
    timestamp = str(int(time.time()))
    nonce = "feishu-binding-nonce"
    signature = hashlib.sha256(
        timestamp.encode() + nonce.encode() + key.encode() + body
    ).hexdigest()
    headers = {
        "content-type": "application/json",
        "x-lark-request-timestamp": timestamp,
        "x-lark-request-nonce": nonce,
        "x-lark-signature": signature,
    }

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="https://gateway.test",
    ) as client:
        response = await client.post(
            "/api/channels/feishu?project_id=attacker-selected",
            content=body,
            headers=headers,
        )

    assert response.status_code == 503
    assert "server-owned channel-to-principal/project binding" in response.text
