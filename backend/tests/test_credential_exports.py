"""Explicit connector and project MCP exports preserve tenant and vault boundaries."""

import json

import httpx
import pytest
from test_auth_api import FakeRuntime, request_as, trusted_settings

from ophira_backend.constants import PrincipalRole
from ophira_backend.main import create_app
from ophira_backend.user_mcp import UserMcpUpsert


@pytest.mark.asyncio
async def test_connector_export_requires_admin_and_revoke_removes_vault_keys(tmp_path):
    app = create_app(trusted_settings(tmp_path), runtime=FakeRuntime())
    app.state.connectors.upsert(
        "search_private", type="search", display_name="Search private",
        secret_env="OPHIRA_CONNECTOR_TEST_KEY", secret="connector-secret-123",
    )
    base = "/api/connectors/search_private"
    export = base + "/credential/export"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://gateway.test",
    ) as client:
        listed = await request_as(client, "GET", "/api/connectors", "member", PrincipalRole.MEMBER)
        assert "connector-secret-123" not in listed.text
        method_denied = await request_as(client, "GET", export, "admin", PrincipalRole.ADMIN)
        assert method_denied.status_code == 405
        assert (await request_as(
            client, "POST", export, "member", PrincipalRole.MEMBER,
            json={"confirmation": "export"},
        )).status_code == 403
        assert (await request_as(
            client, "POST", export, "admin", PrincipalRole.ADMIN,
            json={"confirmation": "revoke"},
        )).status_code == 400
        result = await request_as(
            client, "POST", export, "admin", PrincipalRole.ADMIN,
            json={"confirmation": "export"},
        )
        assert result.status_code == 200, result.text
        assert result.headers["cache-control"] == "no-store"
        assert result.headers["x-content-type-options"] == "nosniff"
        assert result.headers["content-disposition"].startswith("attachment;")
        assert result.json()["credentials"] == {"secret": "connector-secret-123"}
        revoked = await request_as(client, "POST", base + "/revoke", "admin", PrincipalRole.ADMIN)
        assert revoked.status_code == 200, revoked.text
        assert "connector-secret-123" not in revoked.text
        assert app.state.connectors.vault.get("search_private") is None
        assert app.state.connectors.vault._register()[0] == 1
        assert (await request_as(
            client, "POST", export, "admin", PrincipalRole.ADMIN,
            json={"confirmation": "export"},
        )).status_code == 409


@pytest.mark.asyncio
async def test_mailbox_token_export_rejects_other_tenant(tmp_path):
    app = create_app(trusted_settings(tmp_path), runtime=FakeRuntime())
    app.state.connectors.upsert("gmail_private", type="gmail", display_name="Gmail")
    # Use the registry's own key helper so this stays tied to mailbox storage.
    from ophira_backend.gmail import gmail_oauth_vault_keys

    token_key = gmail_oauth_vault_keys("gmail_private")[1]
    app.state.connectors.vault.set(token_key, json.dumps({
        "email": "owner@example.test", "tenant_id": "tenant_elsewhere",
        "connector_revision": 1, "refresh_token": "refresh-secret",
        "access_token": "access-secret", "expires_at": 9999999999.0,
    }))
    path = "/api/connectors/gmail_private/credential/export"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://gateway.test",
    ) as client:
        denied = await request_as(
            client, "POST", path, "admin", PrincipalRole.ADMIN,
            json={"confirmation": "export"},
        )
        assert denied.status_code == 403
        assert "refresh-secret" not in denied.text


@pytest.mark.asyncio
async def test_project_mcp_export_requires_project_export_permission_and_revoke(tmp_path):
    app = create_app(trusted_settings(tmp_path), runtime=FakeRuntime())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://gateway.test",
    ) as client:
        project = await request_as(
            client, "POST", "/api/projects", "owner", PrincipalRole.OWNER,
            json={"name": "Kettle", "objective": "Validate"},
        )
        assert project.status_code == 201, project.text
        project_id = project.json()["id"]
        access_path = f"/api/projects/{project_id}/access"
        access = await request_as(
            client, "GET", access_path, "owner", PrincipalRole.OWNER,
        )
        granted = await request_as(
            client, "POST", access_path, "owner", PrincipalRole.OWNER,
            json={
                "principal_id": "member", "role": "member",
                "expected_revision": access.json()["revision"],
            },
        )
        assert granted.status_code == 200, granted.text
        registry = app.state.user_mcp
        catalog = registry.add_catalog(project_id, "claude_code_docs")
        registry.upsert(project_id, catalog.id, UserMcpUpsert(
            name=catalog.name, url=catalog.url, bearer_token="mcp-private-token",
        ))
        base = f"/api/projects/{project_id}/mcp/{catalog.id}"
        export = base + "/credential/export"
        listed = await request_as(
            client, "GET", f"/api/projects/{project_id}/mcp", "owner", PrincipalRole.OWNER,
        )
        assert "mcp-private-token" not in listed.text
        denied = await request_as(
            client, "POST", export, "member", PrincipalRole.MEMBER,
            json={"confirmation": "export"},
        )
        assert denied.status_code == 403
        exported = await request_as(
            client, "POST", export, "owner", PrincipalRole.OWNER,
            json={"confirmation": "export"},
        )
        assert exported.status_code == 200, exported.text
        assert exported.headers["cache-control"] == "no-store"
        assert exported.json()["credentials"] == {"bearer_token": "mcp-private-token"}
        assert exported.json()["connector"]["project_id"] == project_id
        revoked = await request_as(client, "POST", base + "/revoke", "owner", PrincipalRole.OWNER)
        assert revoked.status_code == 200, revoked.text
        assert "mcp-private-token" not in revoked.text
        assert registry.vault._register()[0] == 1
        assert (await request_as(
            client, "POST", export, "owner", PrincipalRole.OWNER,
            json={"confirmation": "export"},
        )).status_code == 409


def test_connector_endpoint_change_drops_old_key_and_failed_write_stays_disabled(
    tmp_path, monkeypatch,
):
    from ophira_backend.connectors import ConnectorRegistry
    from ophira_backend.providers import FileSecretVault

    vault = FileSecretVault(tmp_path / "secrets", filename="connector-secrets.json")
    registry = ConnectorRegistry(tmp_path / "connectors", vault)
    registry.upsert(
        "commercial", type="commercial_https_json", display_name="Commercial",
        endpoint="https://old.example.test", secret="old-key",
    )
    changed = registry.upsert(
        "commercial", type="commercial_https_json", display_name="Commercial",
        endpoint="https://new.example.test",
    )
    assert changed.endpoint == "https://new.example.test"
    assert vault.get("commercial") is None

    def fail_set(_key, _value):
        raise OSError("vault unavailable")

    monkeypatch.setattr(vault, "set", fail_set)
    with pytest.raises(OSError, match="vault unavailable"):
        registry.upsert(
            "commercial", type="commercial_https_json", display_name="Commercial",
            endpoint="https://other.example.test", secret="new-key",
        )
    persisted = registry.get("commercial")
    assert persisted.enabled is False
    assert persisted.endpoint == "https://new.example.test"
    assert vault.get("commercial") is None


def test_connector_authority_change_cannot_reuse_environment_key(tmp_path):
    from ophira_backend.connectors import ConnectorRegistry
    from ophira_backend.providers import FileSecretVault

    registry = ConnectorRegistry(
        tmp_path / "connectors",
        FileSecretVault(tmp_path / "secrets", filename="connector-secrets.json"),
    )
    original = registry.upsert(
        "commercial", type="commercial_https_json", display_name="Commercial",
        endpoint="https://old.example.test", secret_env="OPHIRA_CONNECTOR_TEST_KEY",
    )
    with pytest.raises(ValueError, match="new credential or environment reference"):
        registry.upsert(
            "commercial", type="commercial_https_json", display_name="Commercial",
            endpoint="https://new.example.test", secret_env="OPHIRA_CONNECTOR_TEST_KEY",
        )
    assert registry.get("commercial") == original
