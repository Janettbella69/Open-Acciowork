import json
from datetime import timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from mcp.types import CallToolRequest, CallToolRequestParams
from pydantic import SecretStr
from test_auth_api import FakeRuntime, principal, request_as, trusted_settings

from ophira_backend import credential_api
from ophira_backend.constants import MEDIA_MCP_SERVER_NAME, WEB_MCP_SERVER_NAME, PrincipalRole
from ophira_backend.credential_archive import decrypt_credential
from ophira_backend.ledger_tools import build_mcp_servers
from ophira_backend.main import create_app
from ophira_backend.models import ProjectCreate, ProviderProfileUpsert, utc_now

PASSWORD = "private archive password"
KEY = "test-credential-private-canary"


@pytest.mark.asyncio
@pytest.mark.parametrize("identifier", ["search", "image_gen"])
async def test_configured_service_credential_deletion_reaches_native_mcp(
    tmp_path,
    monkeypatch,
    identifier,
):
    # Config values may come from a dotenv file rather than process environment.
    monkeypatch.delenv("OPHIRA_SEARCH_API_KEY", raising=False)
    monkeypatch.delenv("OPHIRA_MEDIA_API_KEY", raising=False)
    settings = trusted_settings(tmp_path)
    settings.search_provider = "serper"
    settings.search_api_key = SecretStr("search-canary")
    settings.media_api_key = SecretStr("media-canary")
    settings.media_endpoint = "https://media.example.test/generate"
    app = create_app(settings, runtime=FakeRuntime())
    project = app.state.ledger.create_project(ProjectCreate(name="Test", objective="Test deletion"))
    servers = build_mcp_servers(
        app.state.ledger,
        project.id,
        settings=settings,
        connectors=app.state.connectors,
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="https://gateway.test",
    ) as client:
        item = await metadata(client, "connector", identifier)
        assert item["source"] == "environment" and item["configured"]
        response = await request_as(
            client,
            "POST",
            f"/api/credentials/connector/{identifier}/export",
            "owner",
            PrincipalRole.OWNER,
            json={"expected_revision": item["revision"], "password": PASSWORD},
        )
        assert decrypt_credential(response.content, PASSWORD)["credential"] == (
            "search-canary" if identifier == "search" else "media-canary"
        )
        response = await request_as(
            client,
            "DELETE",
            f"/api/credentials/connector/{identifier}",
            "owner",
            PrincipalRole.OWNER,
            json={"expected_revision": item["revision"], "confirm_delete": True},
        )
        assert response.status_code == 200
    if identifier == "search":
        server = servers[WEB_MCP_SERVER_NAME]["instance"]
        name, args, code = "search_public_web", {"query": "test"}, "SEARCH_API_NOT_CONFIGURED"
    else:
        server = servers[MEDIA_MCP_SERVER_NAME]["instance"]
        name, args, code = (
            "generate_media",
            {
                "kind": "image",
                "prompt": "test image",
                "filename": "test.png",
            },
            "MEDIA_API_NOT_CONFIGURED",
        )

    def forbid_network(*args, **kwargs):
        pytest.fail("Deleted credentials must never start a network request")

    monkeypatch.setattr("httpx.AsyncClient", forbid_network)
    result = await server.request_handlers[CallToolRequest](
        CallToolRequest(
            params=CallToolRequestParams(name=name, arguments=args),
        )
    )
    assert json.loads(result.root.content[0].text)["error_code"] == code
    restarted = create_app(settings, runtime=FakeRuntime())
    assert restarted.state.connectors.credential(restarted.state.connectors.get(identifier)) is None


def configured_app(tmp_path):
    app = create_app(trusted_settings(tmp_path), runtime=FakeRuntime())
    app.state.providers.upsert(
        "test",
        ProviderProfileUpsert(
            display_name="Test provider",
            model="fixture",
            secret=KEY,
            auth_env_name="ANTHROPIC_API_KEY",
        ),
    )
    return app


async def metadata(client, kind="provider", identifier="test"):
    response = await request_as(client, "GET", "/api/credentials", "owner", PrincipalRole.OWNER)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert KEY not in response.text
    return next(
        item for item in response.json() if item["kind"] == kind and item["id"] == identifier
    )


@pytest.mark.asyncio
async def test_admin_export_is_encrypted_version_bound_and_downloadable(tmp_path):
    app = configured_app(tmp_path)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://gateway.test"
    ) as client:
        item = await metadata(client)
        response = await request_as(
            client,
            "POST",
            "/api/credentials/provider/test/export",
            "owner",
            PrincipalRole.OWNER,
            json={"expected_revision": item["revision"], "password": PASSWORD},
        )
        assert response.status_code == 200
        assert KEY not in response.text and PASSWORD not in response.text
        assert response.headers["content-disposition"].startswith("attachment;")
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-content-type-options"] == "nosniff"
        payload = decrypt_credential(response.content, PASSWORD)
        assert payload["credential"] == KEY and payload["id"] == "test"
        assert payload["kind"] == "provider" and payload["exported_by"] == "owner"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "role", [PrincipalRole.MEMBER, PrincipalRole.REVIEWER, PrincipalRole.VIEWER]
)
async def test_non_admins_cannot_read_export_or_delete_credentials(tmp_path, role):
    app = configured_app(tmp_path)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://gateway.test"
    ) as client:
        for method, path in (
            ("GET", "/api/credentials"),
            ("POST", "/api/credentials/provider/test/export"),
            ("DELETE", "/api/credentials/provider/test"),
        ):
            response = await request_as(client, method, path, "member", role)
            assert response.status_code == 403 and KEY not in response.text
        assert (await client.get("/api/credentials")).status_code == 401
    assert app.state.providers.vault.get("test") == KEY


@pytest.mark.asyncio
async def test_delete_requires_explicit_confirmation_and_replays_durable_receipt(tmp_path):
    app = configured_app(tmp_path)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://gateway.test"
    ) as client:
        item = await metadata(client)
        for confirm in (False, 1, "true", None):
            response = await request_as(
                client,
                "DELETE",
                "/api/credentials/provider/test",
                "owner",
                PrincipalRole.OWNER,
                json={"expected_revision": item["revision"], "confirm_delete": confirm},
            )
            assert response.status_code == 422
            assert app.state.providers.vault.get("test") == KEY
        receipts = []
        for _ in range(2):
            response = await request_as(
                client,
                "DELETE",
                "/api/credentials/provider/test",
                "owner",
                PrincipalRole.OWNER,
                json={"expected_revision": item["revision"], "confirm_delete": True},
            )
            assert response.status_code == 200
            receipts.append(response.json())
        assert receipts[0] == receipts[1]
        assert receipts[0]["source"] == "deleted"
        assert not app.state.providers.get("test").secret_configured
        assert KEY not in app.state.providers.vault.path.read_text()


@pytest.mark.asyncio
async def test_rotation_invalidates_prior_export_and_deletion_review(tmp_path):
    app = configured_app(tmp_path)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://gateway.test"
    ) as client:
        item = await metadata(client)
        app.state.providers.vault.set("test", "replacement-canary")
        for method, path, additional in (
            ("POST", "/api/credentials/provider/test/export", {"password": PASSWORD}),
            ("DELETE", "/api/credentials/provider/test", {"confirm_delete": True}),
        ):
            response = await request_as(
                client,
                method,
                path,
                "owner",
                PrincipalRole.OWNER,
                json={"expected_revision": item["revision"], **additional},
            )
            assert response.status_code == 409 and "replacement-canary" not in response.text
        assert app.state.providers.vault.get("test") == "replacement-canary"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["rotation", "expiry"])
async def test_export_rechecks_authority_and_key_after_encryption(tmp_path, monkeypatch, change):
    app = configured_app(tmp_path)
    encrypt = credential_api.encrypt_credential

    def changed(*args):
        result = encrypt(*args)
        if change == "rotation":
            app.state.providers.vault.set("test", "new-key")
        else:
            later = utc_now() + timedelta(hours=1)
            monkeypatch.setattr(credential_api, "utc_now", lambda: later)
        return result

    monkeypatch.setattr(credential_api, "encrypt_credential", changed)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://gateway.test"
    ) as client:
        item = await metadata(client)
        response = await request_as(
            client,
            "POST",
            "/api/credentials/provider/test/export",
            "owner",
            PrincipalRole.OWNER,
            json={"expected_revision": item["revision"], "password": PASSWORD},
        )
        assert response.status_code == (409 if change == "rotation" else 403)
        assert "ciphertext" not in response.text and KEY not in response.text


@pytest.mark.asyncio
async def test_environment_connector_can_export_and_remains_disabled_after_restart(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("OPHIRA_CONNECTOR_GMAIL_KEY", "external-canary")
    app = configured_app(tmp_path)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://gateway.test"
    ) as client:
        item = await metadata(client, "connector", "gmail")
        assert item["source"] == "environment" and item["configured"]
        exported = await request_as(
            client,
            "POST",
            "/api/credentials/connector/gmail/export",
            "owner",
            PrincipalRole.OWNER,
            json={"expected_revision": item["revision"], "password": PASSWORD},
        )
        assert exported.status_code == 200
        assert decrypt_credential(exported.content, PASSWORD)["credential"] == "external-canary"
        result = await request_as(
            client,
            "DELETE",
            "/api/credentials/connector/gmail",
            "owner",
            PrincipalRole.OWNER,
            json={"expected_revision": item["revision"], "confirm_delete": True},
        )
        assert result.status_code == 200
    restarted = create_app(trusted_settings(tmp_path), runtime=FakeRuntime())
    registry = restarted.state.connectors
    assert registry.credential(registry.get("gmail")) is None
    assert registry.health_check("gmail").health == "no_credential"
    registry.store_secret("gmail", "explicitly-restored")
    assert registry.credential(registry.get("gmail")) == "explicitly-restored"


@pytest.mark.asyncio
async def test_password_validation_never_echoes_sensitive_input(tmp_path):
    app = configured_app(tmp_path)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://gateway.test"
    ) as client:
        item = await metadata(client)
        response = await request_as(
            client,
            "POST",
            "/api/credentials/provider/test/export",
            "owner",
            PrincipalRole.OWNER,
            json={"expected_revision": item["revision"], "password": "tiny-key"},
        )
        assert response.status_code == 422
        assert "tiny-key" not in response.text and KEY not in response.text


def test_copying_data_into_a_different_deployment_does_not_rebind_exports(tmp_path):
    configured_app(tmp_path)
    app = create_app(
        trusted_settings(tmp_path, trusted_gateway_tenant_id="different-tenant"),
        runtime=FakeRuntime(),
    )
    owner = principal("owner", PrincipalRole.OWNER).model_copy(
        update={"tenant_id": "different-tenant"},
    )
    with pytest.raises(credential_api.AuthorizationError):
        app.state.credentials.list(owner)


def test_legacy_sdk_environment_path_cannot_resurrect_deleted_key(tmp_path):
    from test_sdk_adapter import build_runtime

    _, run, runtime = build_runtime(tmp_path)
    runtime.settings.anthropic_api_key = "unused"
    runtime.providers.vault.delete("environment")
    from ophira_backend.providers import ProviderConfigurationError

    with pytest.raises(ProviderConfigurationError):
        runtime._provider_env(run)
