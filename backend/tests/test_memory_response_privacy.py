"""Private memory responses must not survive in intermediary/browser caches."""

import pytest
from httpx import ASGITransport, AsyncClient
from test_auth_api import FakeRuntime, request_as, trusted_settings

from ophira_backend.constants import PrincipalRole
from ophira_backend.main import create_app


@pytest.mark.asyncio
async def test_all_memory_success_and_error_responses_disable_caching(tmp_path):
    app = create_app(trusted_settings(tmp_path), runtime=FakeRuntime())
    async with AsyncClient(transport=ASGITransport(app), base_url="https://gateway.test") as client:
        responses = []
        created = await request_as(client, "POST", "/api/memory", "owner", PrincipalRole.OWNER,
                                   json={"scope": "user", "scope_id": "default",
                                         "kind": "fact", "text": "Private memory"})
        assert created.status_code == 201
        responses.append(created)
        identifier = created.json()["id"]
        for method, path, payload, status in (
            ("GET", "/api/memory", None, 200),
            ("GET", "/api/memory/export", None, 200),
            ("POST", "/api/memory/retrieve", {"query": "Private"}, 200),
            ("PATCH", f"/api/memory/{identifier}", {"text": "Corrected memory"}, 200),
            ("DELETE", f"/api/memory/{identifier}", {"reason": ""}, 422),
            ("DELETE", f"/api/memory/{identifier}", {"reason": "Forget this"}, 200),
            ("PATCH", f"/api/memory/{identifier}", {"text": "Cannot restore"}, 404),
        ):
            response = await request_as(
                client, method, path, "owner", PrincipalRole.OWNER,
                **({"json": payload} if payload is not None else {}),
            )
            assert response.status_code == status, response.text
            responses.append(response)
        unsigned = await client.get("/api/memory/export")
        assert unsigned.status_code == 401
        responses.append(unsigned)
        for response in responses:
            assert response.headers.get("cache-control") == "no-store"
            assert response.headers.get("x-content-type-options") == "nosniff"


@pytest.mark.asyncio
async def test_memory_rollback_capacity_error_does_not_claim_memory_is_missing(
    tmp_path, monkeypatch,
):
    from ophira_backend import constants

    monkeypatch.setattr(constants, "APPROVED_MEMORY_CAP", 1)
    app = create_app(trusted_settings(tmp_path), runtime=FakeRuntime())
    async with AsyncClient(transport=ASGITransport(app), base_url="https://gateway.test") as client:
        async def send(method, path, **kwargs):
            return await request_as(client, method, path, "owner", PrincipalRole.OWNER, **kwargs)

        first = await send("POST", "/api/memory", json={
            "scope": "user", "scope_id": "default", "kind": "fact", "text": "First memory",
        })
        assert first.status_code == 201
        identifier = first.json()["id"]
        disabled = await send("POST", f"/api/memory/{identifier}/decision",
                              json={"decision": "disable"})
        assert disabled.status_code == 200
        second = await send("POST", "/api/memory", json={
            "scope": "user", "scope_id": "default", "kind": "fact", "text": "Second memory",
        })
        assert second.status_code == 201
        blocked = await send("POST", f"/api/memory/{identifier}/rollback", json={"version": 1})
        assert blocked.status_code == 400
        assert "上限" in blocked.json()["detail"]
        assert blocked.headers["cache-control"] == "no-store"
        missing = await send("POST", f"/api/memory/{identifier}/rollback", json={"version": 99})
        assert missing.status_code == 404
