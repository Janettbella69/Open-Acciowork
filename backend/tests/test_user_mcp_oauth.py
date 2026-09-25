import asyncio
import json
import logging
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest
from httpx import ASGITransport, AsyncClient
from mcp.server.lowlevel import Server
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool
from test_auth_api import signed_headers, trusted_settings

from ophira_backend.config import Settings
from ophira_backend.constants import PrincipalRole
from ophira_backend.main import _OAuthCallbackAccessLogFilter, create_app
from ophira_backend.models import OrgPolicy
from ophira_backend.policy import OrgPolicyStore
from ophira_backend.providers import FileSecretVault
from ophira_backend.user_mcp import (
    HUBSPOT_MCP_URL,
    HUBSPOT_OAUTH_ISSUER,
    OAUTH_CALLBACK_PATH,
    OAUTH_METADATA_PATH,
    UserMcpConflictError,
    UserMcpRegistry,
    UserMcpUpsert,
    _PinnedMcpTransport,
)
from ophira_backend.web_tools import PinnedPublicDestination


def test_pre_registered_client_is_preserved_only_for_same_configuration(tmp_path: Path) -> None:
    service = UserMcpRegistry(
        tmp_path / "user-mcp",
        FileSecretVault(tmp_path / "secrets", filename="mcp-secrets.json"),
        OrgPolicyStore(tmp_path / "org" / "policy.json"),
        oauth_redirect_url="http://127.0.0.1:8000/api/mcp/oauth/callback",
    )
    service.policy.save(OrgPolicy(allowed_mcp_hosts=[
        "mcp.example.test", "other.example.test", "auth.example.test",
    ]))
    resource = "https://mcp.example.test/mcp"
    issuer = "https://auth.example.test"
    with pytest.raises(ValueError, match="requires an OAuth issuer"):
        service.upsert("project_one", "client_mcp", UserMcpUpsert(
            name="Client", url=resource, oauth_client_id="id",
        ))
    with pytest.raises(ValueError, match="requires a secret"):
        service.upsert("project_one", "client_mcp", UserMcpUpsert(
            name="Client", url=resource, oauth_issuer=issuer,
            oauth_client_id="id", oauth_client_auth_method="client_secret_basic",
        ))
    saved = service.upsert("project_one", "client_mcp", UserMcpUpsert(
        name="Client", url=resource, oauth_issuer=issuer,
        oauth_client_id="old-id", oauth_client_secret="old-secret",
    ))
    assert saved.oauth_client_configured
    assert "old-secret" not in saved.model_dump_json()
    token_key = service._oauth_vault_id("project_one", saved.id, "token")
    preset_key = service._oauth_vault_id("project_one", saved.id, "preset")
    service.vault.set(token_key, "held-token")
    renamed = service.upsert("project_one", saved.id, UserMcpUpsert(
        name="Renamed", url=resource,
    ))
    assert renamed.oauth_client_configured
    assert service.vault.has(token_key) and service.vault.has(preset_key)
    rotated = service.upsert("project_one", saved.id, UserMcpUpsert(
        name="Rotated", url=resource, oauth_issuer=issuer,
        oauth_client_id="new-id", oauth_client_secret="new-secret",
        oauth_client_auth_method="client_secret_post",
    ))
    assert rotated.oauth_client_auth_method == "client_secret_post"
    assert not service.vault.has(token_key)
    assert "new-secret" in (service.vault.get(preset_key) or "")
    assert "old-secret" not in (service.vault.get(preset_key) or "")
    moved = service.upsert("project_one", saved.id, UserMcpUpsert(
        name="Moved", url="https://other.example.test/mcp",
    ))
    assert not moved.oauth_client_configured
    assert not service.vault.has(preset_key)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "client_auth_method", [None, "cimd", "none", "client_secret_basic", "client_secret_post"],
)
async def test_oauth_authorization_review_refresh_and_revoke_are_bound_to_approved_hosts(
    tmp_path: Path, monkeypatch, client_auth_method: str | None,
) -> None:
    issuer = "https://auth.example.test"
    resource = "https://mcp.example.test/mcp"
    redirect_uri = (
        "https://gateway.example.test" + OAUTH_CALLBACK_PATH
        if client_auth_method == "cimd" else "http://127.0.0.1:8000" + OAUTH_CALLBACK_PATH
    )
    metadata_uri = "https://gateway.example.test" + OAUTH_METADATA_PATH
    requests: list[tuple[str, str, str | None]] = []
    token_count = 0

    async def list_tools(_context, _params):
        return ListToolsResult(tools=[Tool(name="echo", inputSchema={
            "type": "object", "properties": {"text": {"type": "string"}},
        })])

    async def call_tool(_context, params):
        return CallToolResult(content=[TextContent(
            type="text", text=params.arguments["text"],
        )])

    remote = Server("remote", on_list_tools=list_tools, on_call_tool=call_tool)
    mcp_app = remote.streamable_http_app(
        json_response=True, stateless_http=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    class OAuthTransport(httpx2.AsyncBaseTransport):
        def __init__(self):
            self.asgi = httpx2.ASGITransport(app=mcp_app)

        async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
            nonlocal token_count
            host = request.headers["Host"]
            path = request.url.path
            authorization = request.headers.get("Authorization")
            requests.append((host, path, authorization))
            if host == "mcp.example.test" and path == "/mcp":
                if authorization not in {"Bearer access-1", "Bearer access-2"}:
                    return httpx2.Response(401, headers={
                        "WWW-Authenticate": (
                            'Bearer resource_metadata="https://mcp.example.test/'
                            '.well-known/oauth-protected-resource"'
                        ),
                    }, request=request)
                return await self.asgi.handle_async_request(request)
            if host == "mcp.example.test" and path == "/.well-known/oauth-protected-resource":
                return httpx2.Response(200, json={
                    "resource": resource, "authorization_servers": [issuer],
                }, request=request)
            if host == "auth.example.test" and path == "/.well-known/oauth-authorization-server":
                metadata = {
                    "issuer": issuer,
                    "authorization_endpoint": issuer + "/authorize",
                    "token_endpoint": issuer + "/token",
                    "code_challenge_methods_supported": ["S256"],
                }
                if client_auth_method == "cimd":
                    metadata["client_id_metadata_document_supported"] = True
                else:
                    metadata["registration_endpoint"] = issuer + "/register"
                return httpx2.Response(200, json=metadata, request=request)
            if host == "auth.example.test" and path == "/register":
                assert client_auth_method is None, "pre-registered clients must skip DCR"
                return httpx2.Response(201, json={
                    "client_id": "ophira-client",
                    "redirect_uris": [redirect_uri],
                    "token_endpoint_auth_method": "none",
                }, request=request)
            if host == "auth.example.test" and path == "/token":
                form = parse_qs((await request.aread()).decode())
                assert form["grant_type"][0] in {"authorization_code", "refresh_token"}
                if client_auth_method == "client_secret_basic":
                    assert authorization is not None and authorization.startswith("Basic ")
                    assert "client_secret" not in form
                elif client_auth_method == "client_secret_post":
                    assert form["client_secret"] == ["registered-secret"]
                elif client_auth_method == "none":
                    assert form["client_id"] == ["registered-client"]
                    assert "client_secret" not in form
                elif client_auth_method == "cimd":
                    assert form["client_id"] == [metadata_uri]
                    assert "client_secret" not in form
                token_count += 1
                return httpx2.Response(200, json={
                    "access_token": f"access-{token_count}",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                    "refresh_token": "refresh-1",
                }, request=request)
            raise AssertionError(f"unexpected OAuth request to {host}{path}")

        async def aclose(self):
            await self.asgi.aclose()

    def pinned(url: str):
        parts = urlsplit(url)
        host = parts.hostname or ""
        address = "93.184.216.34" if host == "mcp.example.test" else "93.184.216.35"
        return PinnedPublicDestination(
            request_url=f"https://{address}{parts.path}" + (
                f"?{parts.query}" if parts.query else ""
            ),
            host_header=host,
            sni_hostname=host,
            resolved_ip=address,
        ), None

    monkeypatch.setattr("ophira_backend.user_mcp.pin_public_http_url", pinned)
    monkeypatch.setattr(
        "ophira_backend.user_mcp.httpx2.AsyncHTTPTransport", lambda **_kwargs: OAuthTransport(),
    )
    service = UserMcpRegistry(
        tmp_path / "user-mcp",
        FileSecretVault(tmp_path / "secrets", filename="mcp-secrets.json"),
        OrgPolicyStore(tmp_path / "org" / "policy.json"),
        oauth_redirect_url=redirect_uri,
    )
    service.policy.save(OrgPolicy(allowed_mcp_hosts=["mcp.example.test", "auth.example.test"]))
    with pytest.raises(ValueError, match="OAuth issuer host"):
        service.upsert("project_one", "unapproved_mcp", UserMcpUpsert(
            name="Unapproved", url=resource, oauth_issuer="https://other.example.test",
        ))
    registration = (
        {"oauth_client_id": "registered-client",
         "oauth_client_auth_method": client_auth_method,
         **({"oauth_client_secret": "registered-secret"}
            if client_auth_method in {"client_secret_basic", "client_secret_post"} else {})}
        if client_auth_method not in {None, "cimd"} else {}
    )
    saved = service.upsert("project_one", "oauth_mcp", UserMcpUpsert(
        name="OAuth MCP", url=resource, oauth_issuer=issuer, **registration,
    ))
    assert not saved.enabled and saved.auth_mode == "oauth"
    assert saved.oauth_client_configured == (client_auth_method not in {None, "cimd"})
    assert "registered-secret" not in saved.model_dump_json()
    async with mcp_app.router.lifespan_context(mcp_app):
        authorization_url = await asyncio.wait_for(
            service.begin_oauth("project_one", saved.id), 5,
        )
        auth_parts = urlsplit(authorization_url)
        auth_params = parse_qs(auth_parts.query)
        assert auth_parts.netloc == "auth.example.test"
        assert auth_params["code_challenge_method"] == ["S256"]
        assert auth_params["client_id"] == [
            "ophira-client" if client_auth_method is None else
            metadata_uri if client_auth_method == "cimd" else "registered-client"
        ]
        state = auth_params["state"][0]
        assert not service.complete_oauth_callback(
            state="wrong", code="accepted", issuer=issuer, error=None,
        )
        assert service.complete_oauth_callback(
            state=state, code="accepted", issuer=issuer, error=None,
        )
        for _ in range(100):
            reviewed = service.get("project_one", saved.id)
            if reviewed.enabled or reviewed.review_error:
                break
            await asyncio.sleep(0.01)
        assert reviewed.enabled, reviewed.review_error
        assert reviewed.credential_configured
        assert [tool.name for tool in reviewed.tools] == ["echo"]
        assert not service.complete_oauth_callback(
            state=state, code="replayed", issuer=issuer, error=None,
        )

        service.policy.save(OrgPolicy(allowed_mcp_hosts=["mcp.example.test"]))
        assert not service.active("project_one")
        with pytest.raises(UserMcpConflictError, match="no longer approved"):
            service.sdk_server(reviewed)
        service.policy.save(OrgPolicy(
            allowed_mcp_hosts=["mcp.example.test", "auth.example.test"],
        ))

        secret_id = service._oauth_vault_id("project_one", saved.id, "token")
        stored = json.loads(service.vault.get(secret_id) or "{}")
        stored["expires_at"] = 0
        service.vault.set(secret_id, json.dumps(stored))
        called = await service._invoke_remote(reviewed, reviewed.tools[0], {"text": "ready"})
        assert called.content[0].text == "ready"
        assert token_count == 2
        assert all(
            not authorization or not authorization.startswith("Bearer ")
            for host, path, authorization in requests
            if host != "mcp.example.test" or path != "/mcp"
        )
        revoked = service.revoke("project_one", saved.id)
        assert not revoked.credential_configured
        assert not revoked.oauth_client_configured
        assert not service.vault.has(secret_id)
        assert not service.vault.has(service._oauth_vault_id("project_one", saved.id, "preset"))
        assert not service.active("project_one")


@pytest.mark.asyncio
async def test_oauth_transport_never_sends_bearer_to_issuer_or_other_host() -> None:
    transport = _PinnedMcpTransport(
        "https://mcp.example.test/mcp", "https://auth.example.test",
    )
    with pytest.raises(UserMcpConflictError, match="token cannot leave"):
        await transport.handle_async_request(httpx2.Request(
            "POST", "https://auth.example.test/token",
            headers={"Authorization": "Bearer private"},
        ))
    with pytest.raises(UserMcpConflictError, match="approved endpoint"):
        await transport.handle_async_request(httpx2.Request(
            "GET", "https://other.example.test/.well-known/oauth-authorization-server",
        ))
    await transport.aclose()


@pytest.mark.asyncio
async def test_hubspot_catalog_oauth_reviews_only_documented_read_tools(
    tmp_path: Path, monkeypatch,
) -> None:
    redirect_uri = "https://gateway.example.test" + OAUTH_CALLBACK_PATH
    observed: list[tuple[str, str, str | None]] = []

    async def list_tools(_context, _params):
        return ListToolsResult(tools=[Tool(name=name, inputSchema={
            "type": "object", "properties": {},
        }) for name in (
            "get_user_details", "search_crm_objects", "manage_crm_objects",
        )])

    async def call_tool(_context, params):
        assert params.name == "search_crm_objects"
        return CallToolResult(content=[TextContent(type="text", text="scoped CRM result")])

    remote = Server("hubspot", on_list_tools=list_tools, on_call_tool=call_tool)
    mcp_app = remote.streamable_http_app(
        json_response=True, stateless_http=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    class HubSpotTransport(httpx2.AsyncBaseTransport):
        def __init__(self):
            self.asgi = httpx2.ASGITransport(app=mcp_app)

        async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
            host, path = request.headers["Host"], request.url.path
            authorization = request.headers.get("Authorization")
            observed.append((host, path, authorization))
            if host == "mcp.hubspot.com" and path == "/":
                if authorization != "Bearer hubspot-access":
                    return httpx2.Response(401, headers={
                        "WWW-Authenticate": (
                            'Bearer resource_metadata="https://mcp.hubspot.com/'
                            '.well-known/oauth-protected-resource"'
                        ),
                    }, request=request)
                return await self.asgi.handle_async_request(httpx2.Request(
                    request.method, "https://mcp.hubspot.com/mcp",
                    headers=request.headers, content=await request.aread(),
                ))
            if host == "mcp.hubspot.com" and path == "/.well-known/oauth-protected-resource":
                return httpx2.Response(200, json={
                    "resource": HUBSPOT_OAUTH_ISSUER,
                    "authorization_servers": [HUBSPOT_OAUTH_ISSUER],
                }, request=request)
            if host == "mcp.hubspot.com" and path == "/.well-known/oauth-authorization-server":
                return httpx2.Response(200, json={
                    "issuer": HUBSPOT_OAUTH_ISSUER,
                    "authorization_endpoint": "https://app.hubspot.com/oauth/authorize",
                    "token_endpoint": "https://api.hubspot.com/oauth/v3/token",
                    "code_challenge_methods_supported": ["S256"],
                    "token_endpoint_auth_methods_supported": ["client_secret_post"],
                }, request=request)
            if host == "api.hubspot.com" and path == "/oauth/v3/token":
                form = parse_qs((await request.aread()).decode())
                assert form["client_id"] == ["hubspot-client"]
                assert form["client_secret"] == ["hubspot-secret"]
                assert form["code_verifier"]
                return httpx2.Response(200, json={
                    "access_token": "hubspot-access", "token_type": "Bearer",
                    "expires_in": 3600, "refresh_token": "hubspot-refresh",
                }, request=request)
            raise AssertionError(f"unexpected HubSpot request: {host}{path}")

        async def aclose(self):
            await self.asgi.aclose()

    def pinned(url: str):
        parts = urlsplit(url)
        host = parts.hostname or ""
        return PinnedPublicDestination(
            request_url="https://93.184.216.34" + parts.path,
            host_header=host, sni_hostname=host, resolved_ip="93.184.216.34",
        ), None

    monkeypatch.setattr("ophira_backend.user_mcp.pin_public_http_url", pinned)
    monkeypatch.setattr(
        "ophira_backend.user_mcp.httpx2.AsyncHTTPTransport", lambda **_kwargs: HubSpotTransport(),
    )
    service = UserMcpRegistry(
        tmp_path / "user-mcp",
        FileSecretVault(tmp_path / "secrets", filename="mcp-secrets.json"),
        OrgPolicyStore(tmp_path / "org" / "policy.json"),
        oauth_redirect_url=redirect_uri,
    )
    added = service.add_catalog("project_one", "hubspot_crm")
    with pytest.raises(UserMcpConflictError, match="client ID and secret"):
        await service.begin_oauth("project_one", added.id)
    configured = service.upsert("project_one", added.id, UserMcpUpsert(
        name=added.name, url=HUBSPOT_MCP_URL, oauth_issuer=HUBSPOT_OAUTH_ISSUER,
        oauth_client_id="hubspot-client", oauth_client_secret="hubspot-secret",
        oauth_client_auth_method="client_secret_post",
    ))
    assert not configured.enabled
    async with mcp_app.router.lifespan_context(mcp_app):
        authorization_url = await asyncio.wait_for(
            service.begin_oauth("project_one", configured.id), 5,
        )
        parts = urlsplit(authorization_url)
        assert parts.hostname == "app.hubspot.com"
        parameters = parse_qs(parts.query)
        assert parameters["code_challenge_method"] == ["S256"]
        assert service.complete_oauth_callback(
            state=parameters["state"][0], code="approved", issuer=HUBSPOT_OAUTH_ISSUER,
            error=None,
        )
        for _ in range(100):
            reviewed = service.get("project_one", configured.id)
            if reviewed.enabled or reviewed.review_error:
                break
            await asyncio.sleep(0.01)
        assert reviewed.enabled, (reviewed.review_error, observed)
        assert [tool.name for tool in reviewed.tools] == [
            "get_user_details", "search_crm_objects",
        ]
        with pytest.raises(KeyError):
            service.set_tool_policy(
                "project_one", configured.id, "manage_crm_objects", "ask",
            )
        result = await service._invoke_remote(reviewed, reviewed.tools[1], {})
        assert result.content[0].text == "scoped CRM result"
        assert all(
            authorization is None or not authorization.startswith("Bearer ")
            for host, path, authorization in observed
            if host != "mcp.hubspot.com" or path != "/"
        )
        assert not service.vault.has("project_one:hubspot_crm")
    await service.aclose_oauth_flows()


@pytest.mark.asyncio
async def test_oauth_callback_accepts_only_live_state_and_clears_code_from_browser_url(
    tmp_path: Path, monkeypatch,
) -> None:
    class Runtime:
        async def run(self, _run):
            pass

        async def cancel(self, _run_id):
            pass

    app = create_app(Settings(data_dir=tmp_path), runtime=Runtime())
    accepted = []

    def complete(**kwargs):
        accepted.append(kwargs)
        return kwargs["state"] == "s" * 32

    monkeypatch.setattr(app.state.user_mcp, "complete_oauth_callback", complete)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        rejected = await client.get(
            "/api/mcp/oauth/callback", params={"state": "x" * 32, "code": "secret"},
            headers={"Sec-Fetch-Site": "cross-site"},
        )
        assert rejected.status_code == 400
        accepted_response = await client.get(
            "/api/mcp/oauth/callback", params={"state": "s" * 32, "code": "secret"},
            headers={"Sec-Fetch-Site": "cross-site"},
            follow_redirects=False,
        )
        assert accepted_response.status_code == 303
        assert accepted_response.headers["location"] == "/api/mcp/oauth/complete"
        assert accepted_response.headers["referrer-policy"] == "no-referrer"
        complete_response = await client.get("/api/mcp/oauth/complete")
        assert complete_response.status_code == 200
        assert "secret" not in complete_response.text
        assert complete_response.headers["cache-control"] == "no-store"
    assert accepted[-1]["code"] == "secret"


@pytest.mark.asyncio
async def test_oauth_start_requires_project_write_and_public_callback_host(
    tmp_path: Path, monkeypatch,
) -> None:
    class Runtime:
        async def run(self, _run):
            pass

        async def cancel(self, _run_id):
            pass

    app = create_app(trusted_settings(
        tmp_path, mcp_oauth_public_origin="https://gateway.example.com",
    ), runtime=Runtime())

    async def begin(_project_id, _connector_id):
        return "https://auth.example.test/authorize?state=fixed"

    monkeypatch.setattr(app.state.user_mcp, "begin_oauth", begin)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://gateway.example.com",
    ) as client:
        project = await client.post(
            "/api/projects", json={"name": "Kettle", "objective": "Validate"},
            headers=signed_headers("POST", "/api/projects", "owner", PrincipalRole.OWNER),
        )
        assert project.status_code == 201
        setup_path = f"/api/projects/{project.json()['id']}/mcp/oauth/setup"
        setup_outsider = await client.get(
            setup_path,
            headers=signed_headers("GET", setup_path, "outsider", PrincipalRole.MEMBER),
        )
        assert setup_outsider.status_code == 403
        setup = await client.get(
            setup_path,
            headers=signed_headers("GET", setup_path, "owner", PrincipalRole.OWNER),
        )
        assert setup.status_code == 200
        assert setup.json() == {
            "redirect_uri": "https://gateway.example.com" + OAUTH_CALLBACK_PATH,
            "client_metadata_url": "https://gateway.example.com" + OAUTH_METADATA_PATH,
        }
        metadata = await client.get(OAUTH_METADATA_PATH, headers={"Sec-Fetch-Site": "cross-site"})
        assert metadata.status_code == 200
        assert metadata.json()["client_id"] == setup.json()["client_metadata_url"]
        assert metadata.json()["redirect_uris"] == [setup.json()["redirect_uri"]]
        assert metadata.json()["token_endpoint_auth_method"] == "none"
        assert metadata.headers["cache-control"] == "no-store"
        assert "client_secret" not in metadata.text
        wrong_metadata_host = await client.get("https://other.test" + OAUTH_METADATA_PATH)
        assert wrong_metadata_host.status_code == 404
        path = f"/api/projects/{project.json()['id']}/mcp/sample_mcp/oauth/start"
        outsider = await client.post(
            path, headers=signed_headers("POST", path, "outsider", PrincipalRole.MEMBER),
        )
        assert outsider.status_code == 403
        owner = await client.post(
            path, headers=signed_headers("POST", path, "owner", PrincipalRole.OWNER),
        )
        assert owner.status_code == 200
        assert owner.json() == {"authorization_url": "https://auth.example.test/authorize?state=fixed"}
        wrong_host = await client.get(
            "https://other.test/api/mcp/oauth/callback",
            params={"state": "s" * 32, "code": "code"},
        )
        assert wrong_host.status_code == 404


def test_uvicorn_access_log_removes_oauth_authorization_code() -> None:
    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, "", 0,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1", "GET", OAUTH_CALLBACK_PATH + "?code=private-code&state=value", "1.1", 303),
        None,
    )
    assert _OAuthCallbackAccessLogFilter().filter(record)
    assert "private-code" not in record.getMessage()
    assert OAUTH_CALLBACK_PATH in record.getMessage()
