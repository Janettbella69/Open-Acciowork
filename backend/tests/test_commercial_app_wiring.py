from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from mcp_test_client import call_sdk_tool

from ophira_backend.auth import build_gateway_assertion_headers
from ophira_backend.commercial_adapters import HttpsJsonMessageAdapter
from ophira_backend.config import Settings
from ophira_backend.constants import (
    COMMERCIAL_ACTION_MCP_SERVER_NAME,
    COMMERCIAL_HTTPS_JSON_CONNECTOR_TYPE,
    CommercialActionOperation,
    PrincipalRole,
)
from ophira_backend.main import create_app
from ophira_backend.models import OrgPolicy, PrincipalContext, ProjectCreate
from ophira_backend.sdk_adapter import ClaudeAgentRuntime

GATEWAY_SECRET = "test-commerce-gateway-signing-secret-0001"
TENANT_ID = "tenant_commerce"
CONNECTOR_ID = "supplier_outreach"


def principal(principal_id: str, role: PrincipalRole) -> PrincipalContext:
    now = datetime.now(UTC)
    return PrincipalContext(
        tenant_id=TENANT_ID,
        principal_id=principal_id,
        role=role,
        session_id=f"principal_session_{principal_id.replace('@', '_')}",
        assertion_issued_at=now,
        assertion_expires_at=now + timedelta(minutes=5),
        assertion_nonce=("principal_nonce_" + principal_id.encode().hex())[:80],
    )


def gateway_headers(
    principal_context: PrincipalContext,
    *,
    method: str,
    path: str,
) -> dict[str, str]:
    return build_gateway_assertion_headers(
        GATEWAY_SECRET,
        method=method,
        path=path,
        tenant_id=principal_context.tenant_id,
        principal_id=principal_context.principal_id,
        role=principal_context.role,
        session_id=principal_context.session_id,
        issued_at=datetime.now(UTC),
    )


def mcp_payload(result) -> dict:
    assert result.is_error is False
    block = result.content[0]
    return json.loads(block.text)


@pytest.mark.asyncio
async def test_app_wires_receipt_first_mcp_and_authenticated_cancel(
    tmp_path: Path,
) -> None:
    app = create_app(
        Settings(
            _env_file=None,
            data_dir=tmp_path,
            auth_mode="trusted_gateway",
            trusted_gateway_auth_secret=GATEWAY_SECRET,
            trusted_gateway_tenant_id=TENANT_ID,
        )
    )
    assert isinstance(app.state.runtime, ClaudeAgentRuntime)
    adapter = app.state.commercial_action_adapter
    assert isinstance(adapter, HttpsJsonMessageAdapter)
    assert app.state.commercial_actions.executor is adapter
    assert app.state.commercial_actions.target_reader is adapter

    owner = principal("owner@example.test", PrincipalRole.OWNER)
    member = principal("member@example.test", PrincipalRole.MEMBER)
    viewer = principal("viewer@example.test", PrincipalRole.VIEWER)
    project = app.state.ledger.create_project(
        ProjectCreate(name="Supplier inquiry", objective="Request a reviewed quote"),
        principal=owner,
    )
    app.state.project_access.bind_project(project.id, owner)
    for collaborator in (member, viewer):
        app.state.project_access.grant(
            project.id,
            owner,
            principal_id=collaborator.principal_id,
            role=collaborator.role,
        )
    run = app.state.ledger.create_run(
        project.id,
        "Read and stage the supplier inquiry",
        principal=member,
    )
    run = app.state.ledger.update_run(
        run.id,
        sdk_session_id="sdk_session_commercial_app_wiring",
        skill_snapshot=app.state.runtime.skills.snapshot(),
    )
    app.state.policy.save(
        OrgPolicy(
            commercial_actions_enabled=True,
            allowed_commercial_operations=[CommercialActionOperation.SEND_MESSAGE],
            allowed_commercial_connector_ids=[CONNECTOR_ID],
        )
    )
    app.state.connectors.upsert(
        CONNECTOR_ID,
        type=COMMERCIAL_HTTPS_JSON_CONNECTOR_TYPE,
        display_name="Supplier messaging gateway",
        scope="messages:write",
        endpoint="https://commerce.example.test/api",
        secret="server-only-test-credential",
    )
    assert app.state.connectors.health_check(CONNECTOR_ID, environ={}).health == "ok"

    requests: list[httpx.Request] = []

    def external_handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["Authorization"] == "Bearer server-only-test-credential"
        assert request.headers["Host"] == "commerce.example.test"
        assert request.extensions["sni_hostname"] == "commerce.example.test"
        assert request.url.host == "93.184.216.34"
        assert request.method == "GET"
        return httpx.Response(
            200,
            json={
                "version": "etag-7",
                "state": {
                    "recipient": "supplier@example.test",
                    "content": "Old reviewed draft",
                },
            },
            request=request,
        )

    external_client = httpx.Client(transport=httpx.MockTransport(external_handler))
    adapter._client = external_client
    adapter._dns_resolver = lambda *_args, **_kwargs: [
        (2, 1, 6, "", ("93.184.216.34", 443))
    ]
    try:
        options = app.state.runtime._build_options(run)
        server_config = options.mcp_servers[COMMERCIAL_ACTION_MCP_SERVER_NAME]
        read_result = await call_sdk_tool(
            server_config["instance"], "read_commercial_target", {
                        "connector_id": CONNECTOR_ID,
                        "resource_type": "supplier_inquiry",
                        "resource_key": "inquiry-100",
            },
        )
        observation = mcp_payload(read_result)["target_observation"]
        receipt_id = observation["source_receipt"]["id"]
        assert observation["target"]["expected_version"] == "etag-7"

        stage_result = await call_sdk_tool(
            server_config["instance"], "stage_commercial_action", {
                        "receipt_id": receipt_id,
                        "changes": [
                            {
                                "field": "content",
                                "before": "Old reviewed draft",
                                "after": "Could you quote 500 units FOB Ningbo?",
                            }
                        ],
                        "reason": "Stage the host-observed supplier inquiry",
            },
        )
        staged = mcp_payload(stage_result)["staged_action"]
        action_id = staged["id"]
        persisted = app.state.commercial_actions.get(project.id, action_id)
        assert persisted.source_receipt_id == receipt_id
        assert persisted.staged_by == member.principal_id
        assert len(requests) == 1

        cancel_path = (
            f"/api/projects/{project.id}/commercial-actions/{action_id}/cancel"
        )
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://localhost",
        ) as client:
            denied = await client.post(
                cancel_path,
                headers=gateway_headers(viewer, method="POST", path=cancel_path),
                json={"reason": "A viewer must not cancel this action"},
            )
            assert denied.status_code == 403

            cancelled = await client.post(
                cancel_path,
                headers=gateway_headers(member, method="POST", path=cancel_path),
                json={"reason": "Supplier inquiry is no longer required"},
            )
            assert cancelled.status_code == 200
            body = cancelled.json()
            assert body["status"] == "cancelled"
            assert body["history"][-1]["event"] == "cancelled"
            assert body["history"][-1]["actor_id"] == member.principal_id

            repeated = await client.post(
                cancel_path,
                headers=gateway_headers(member, method="POST", path=cancel_path),
                json={"reason": "Duplicate click must stay idempotent"},
            )
            assert repeated.status_code == 200
            assert repeated.json() == body
    finally:
        await asyncio.to_thread(external_client.close)
