import json

import httpx
import pytest
from test_auth_api import FakeRuntime, principal, request_as, trusted_settings
from test_marketplace_api import configure
from test_marketplaces import PRODUCT, SHOP, response

from ophira_backend import web_tools
from ophira_backend.auth import AuthorizationError
from ophira_backend.constants import PrincipalRole, RunStatus
from ophira_backend.main import create_app
from ophira_backend.marketplace_access import MarketplaceAgentRequest
from ophira_backend.models import ProjectCreate


@pytest.fixture
def setup(tmp_path, monkeypatch):
    app = create_app(trusted_settings(tmp_path), runtime=FakeRuntime())
    configure(app)
    owner = principal("owner", PrincipalRole.OWNER)
    member = principal("member", PrincipalRole.MEMBER)
    project = app.state.ledger.create_project(ProjectCreate(name="Shop", objective="Review"),
                                              principal=owner)
    app.state.project_access.bind_project(project.id, owner)
    app.state.project_access.grant(project.id, owner, principal_id="member", role=member.role)
    monkeypatch.setattr(web_tools, "_resolve_ips", lambda _: ["93.184.216.34"])

    def handler(request):
        return response({"shops": [SHOP]} if request.method == "GET" else {"products": [PRODUCT]})

    app.state.marketplace_reader.transport_factory = lambda: httpx.MockTransport(handler)
    return app, owner, member, project


async def grant(client, project, revision=0):
    return await request_as(
        client, "POST", f"/api/projects/{project.id}/marketplace-access",
        "owner", PrincipalRole.OWNER,
        json={"expected_revision": revision, "connector_id": "tiktok_shop", "shop_id": "shop-1"},
    )


async def test_only_project_admin_can_grant_and_revoke_while_members_inspect(setup):
    app, owner, member, project = setup
    path = f"/api/projects/{project.id}/marketplace-access"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://gateway.test",
    ) as client:
        denied = await request_as(client, "POST", path, member.principal_id, member.role,
                                  json={"expected_revision": 0, "connector_id": "tiktok_shop",
                                        "shop_id": "shop-1"})
        assert denied.status_code == 403
        result = await grant(client, project)
        assert result.status_code == 200, result.text
        data = result.json()
        assert result.headers["cache-control"] == "no-store"
        assert data["revision"] == 1
        assert data["bindings"][0]["shop_name"] == SHOP["name"]
        assert data["bindings"][0]["created_by"] == owner.principal_id
        for secret in ("connector_identity", "app-key", "seller-token", "cipher-1"):
            assert secret not in result.text
        view = await request_as(client, "GET", path, member.principal_id, member.role)
        assert view.status_code == 200 and view.json() == data
        outsider = await request_as(client, "GET", path, "outsider", PrincipalRole.OWNER)
        assert outsider.status_code == 403 and SHOP["name"] not in outsider.text
        revoke_path = f"{path}/{data['bindings'][0]['id']}/revoke"
        denied = await request_as(client, "POST", revoke_path, "member", member.role,
                                  json={"expected_revision": 1})
        assert denied.status_code == 403
        revoked = await request_as(client, "POST", revoke_path, "owner", owner.role,
                                   json={"expected_revision": 1})
        assert revoked.status_code == 200
        assert revoked.json()["bindings"][0]["active"] is False
        stale = await grant(client, project, revision=1)
        assert stale.status_code == 409


async def test_authenticated_run_receipt_inherits_principal_and_rechecks_membership(setup):
    app, owner, member, project = setup
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://gateway.test",
    ) as client:
        result = await grant(client, project)
    binding_id = result.json()["bindings"][0]["id"]
    run = app.state.ledger.create_run(project.id, "Read products", principal=member)
    app.state.ledger.update_run(run.id, sdk_session_id="sdk-member", status=RunStatus.RUNNING)
    service = app.state.marketplace_access
    payload = MarketplaceAgentRequest(operation="products", binding_id=binding_id)
    observed = await service.read_for_run(project.id, run.id, payload)
    receipt = app.state.ledger.replay_tool_receipt(project.id, observed["source_receipt"]["id"])
    assert receipt.receipt.tenant_id == member.tenant_id
    assert receipt.receipt.principal_id == member.principal_id
    assert receipt.receipt.principal_role == member.role
    assert receipt.receipt.principal_session_id == member.session_id
    app.state.project_access.revoke(project.id, owner, principal_id=member.principal_id)
    with pytest.raises(AuthorizationError):
        await service.read_for_run(project.id, run.id, payload)
    assert len(app.state.ledger.list_tool_receipts(project.id)) == 1


async def test_membership_revoked_during_grant_prevents_persistence(setup):
    app, owner, _member, project = setup
    admin = principal("admin", PrincipalRole.ADMIN)
    app.state.project_access.grant(project.id, owner, principal_id="admin", role=admin.role)

    def handler(_request):
        app.state.project_access.revoke(project.id, owner, principal_id="admin")
        return response({"shops": [SHOP]})

    app.state.marketplace_reader.transport_factory = lambda: httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://gateway.test",
    ) as client:
        result = await request_as(
            client, "POST", f"/api/projects/{project.id}/marketplace-access", "admin", admin.role,
            json={"expected_revision": 0, "connector_id": "tiktok_shop", "shop_id": "shop-1"},
        )
    assert result.status_code == 403
    assert app.state.marketplace_access.get(project.id).revision == 0


async def test_corrupt_identity_record_fails_closed(setup):
    app, _owner, _member, project = setup
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://gateway.test",
    ) as client:
        assert (await grant(client, project)).status_code == 200
        path = app.state.marketplace_access.root / f"{project.id}.json"
        data = json.loads(path.read_text())
        data["tenant_id"] = "another-tenant"
        path.write_text(json.dumps(data))
        result = await request_as(
            client, "GET", f"/api/projects/{project.id}/marketplace-access",
            "owner", PrincipalRole.OWNER,
        )
        assert result.status_code == 409
        assert SHOP["name"] not in result.text


async def test_mistyped_or_extra_credentials_in_grant_are_not_reflected(setup):
    app, _owner, _member, project = setup
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://gateway.test",
    ) as client:
        result = await request_as(
            client, "POST", f"/api/projects/{project.id}/marketplace-access",
            "owner", PrincipalRole.OWNER,
            json={"expected_revision": 0, "connector_id": "tiktok_shop", "shop_id": "shop-1",
                  "app_secret": "never-reflect-this-secret"},
        )
    assert result.status_code == 422
    assert "never-reflect-this-secret" not in result.text
