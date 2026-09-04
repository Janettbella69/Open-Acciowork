from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from ophira_backend.auth import build_gateway_assertion_headers
from ophira_backend.config import Settings
from ophira_backend.constants import PrincipalRole
from ophira_backend.main import create_app

SECRET = "test-memory-gateway-signing-secret-0000001"
TENANT_ID = "tenant_acme"


class FakeRuntime:
    async def run(self, _run) -> None:
        return None

    async def cancel(self, _run_id: str) -> None:
        return None


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        data_dir=tmp_path,
        auth_mode="trusted_gateway",
        trusted_gateway_auth_secret=SECRET,
        trusted_gateway_tenant_id=TENANT_ID,
    )


def _headers(
    method: str,
    path: str,
    principal_id: str,
    role: PrincipalRole,
    *,
    tenant_id: str = TENANT_ID,
) -> dict[str, str]:
    return build_gateway_assertion_headers(
        SECRET,
        method=method,
        path=urlsplit(path).path,
        tenant_id=tenant_id,
        principal_id=principal_id,
        role=role,
        session_id=f"gateway_session_{principal_id.replace('@', '_')}",
        issued_at=datetime.now(UTC),
        nonce=f"nonce_{uuid4().hex}",
    )


async def _request_as(
    client: AsyncClient,
    method: str,
    path: str,
    principal_id: str,
    role: PrincipalRole,
    **kwargs,
):
    return await client.request(
        method,
        path,
        headers=_headers(method, path, principal_id, role),
        **kwargs,
    )


@pytest.mark.asyncio
async def test_memory_ttl_retrieval_export_and_delete_api(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path), runtime=FakeRuntime())
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="https://gateway.test",
    ) as client:
        created = await _request_as(
            client,
            "POST",
            "/api/memory",
            "alice@example.com",
            PrincipalRole.MEMBER,
            json={
                "scope": "user",
                "scope_id": "default",
                "text": "Prefer EUR landed cost",
                "kind": "preference",
                "sensitivity": "sensitive",
                "ttl_seconds": 3_600,
            },
        )
        assert created.status_code == 201
        item = created.json()
        memory_id = item["id"]
        assert item["expires_at"] is not None

        retrieved = await _request_as(
            client,
            "POST",
            "/api/memory/retrieve",
            "alice@example.com",
            PrincipalRole.MEMBER,
            json={"query": "ＥＵＲ   LANDED", "limit": 8},
        )
        assert retrieved.status_code == 200
        body = retrieved.json()
        assert body["metadata"]["normalized_query"] == "eur landed"
        assert body["metadata"]["returned"] == 1
        assert body["hits"][0]["item"]["id"] == memory_id
        assert body["hits"][0]["item"]["sensitivity"] == "sensitive"
        assert body["hits"][0]["item"]["source"] == "user"
        assert body["hits"][0]["item"]["origin"] == "owner"
        assert "principal_session_id" not in body["hits"][0]["item"]

        exported = await _request_as(
            client,
            "GET",
            "/api/memory/export?scope=user&scope_id=default",
            "alice@example.com",
            PrincipalRole.MEMBER,
        )
        assert exported.status_code == 200
        assert exported.headers["content-disposition"] == (
            'attachment; filename="ophira-memory-export.json"'
        )
        export_body = exported.json()
        assert export_body["schema_version"] == "memory-export-v1"
        assert export_body["exported_by"] == "alice@example.com"
        assert export_body["count"] == 1
        assert export_body["items"][0]["source"] == "user"
        assert export_body["items"][0]["sensitivity"] == "sensitive"
        assert "principal_session_id" not in export_body["items"][0]

        changed_expiry = await _request_as(
            client,
            "PATCH",
            f"/api/memory/{memory_id}/expiry",
            "alice@example.com",
            PrincipalRole.MEMBER,
            json={"clear_expiry": True},
        )
        assert changed_expiry.status_code == 200
        assert changed_expiry.json()["expires_at"] is None

        deleted = await _request_as(
            client,
            "DELETE",
            f"/api/memory/{memory_id}",
            "alice@example.com",
            PrincipalRole.MEMBER,
            json={"reason": "Remove my stored preference"},
        )
        assert deleted.status_code == 200
        assert deleted.json()["memory_id"] == memory_id
        assert deleted.json()["deleted_by"] == "alice@example.com"
        assert "text" not in deleted.json()

        after_delete = await _request_as(
            client,
            "GET",
            "/api/memory/export?scope=user&scope_id=default",
            "alice@example.com",
            PrincipalRole.MEMBER,
        )
        assert after_delete.json()["items"] == []
        assert after_delete.json()["tombstone_count"] == 1
        assert after_delete.json()["tombstones"][0]["memory_id"] == memory_id
        assert "text" not in after_delete.json()["tombstones"][0]


@pytest.mark.asyncio
async def test_memory_governance_fails_closed_across_principal_tenant_and_project(
    tmp_path: Path,
) -> None:
    app = create_app(_settings(tmp_path), runtime=FakeRuntime())
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="https://gateway.test",
    ) as client:
        private = await _request_as(
            client,
            "POST",
            "/api/memory",
            "alice@example.com",
            PrincipalRole.OWNER,
            json={
                "scope": "user",
                "scope_id": "default",
                "text": "Alice private memory signal",
                "kind": "fact",
            },
        )
        memory_id = private.json()["id"]

        bob_retrieval = await _request_as(
            client,
            "POST",
            "/api/memory/retrieve",
            "bob@example.com",
            PrincipalRole.OWNER,
            json={"query": "private memory"},
        )
        assert bob_retrieval.status_code == 200
        assert bob_retrieval.json()["metadata"]["authorized_approved_candidates"] == 0
        assert bob_retrieval.json()["hits"] == []

        bob_export = await _request_as(
            client,
            "GET",
            "/api/memory/export?scope=user&scope_id=default",
            "bob@example.com",
            PrincipalRole.OWNER,
        )
        assert bob_export.status_code == 200
        assert bob_export.json()["items"] == []

        bob_delete = await _request_as(
            client,
            "DELETE",
            f"/api/memory/{memory_id}",
            "bob@example.com",
            PrincipalRole.OWNER,
            json={"reason": "Attempt cross-principal delete"},
        )
        assert bob_delete.status_code == 403

        deleted = await _request_as(
            client,
            "DELETE",
            f"/api/memory/{memory_id}",
            "alice@example.com",
            PrincipalRole.OWNER,
            json={"reason": "Alice requested deletion"},
        )
        assert deleted.status_code == 200
        bob_after_delete = await _request_as(
            client,
            "GET",
            "/api/memory/export?scope=user&scope_id=default",
            "bob@example.com",
            PrincipalRole.OWNER,
        )
        assert bob_after_delete.status_code == 200
        assert bob_after_delete.json()["items"] == []
        assert bob_after_delete.json()["tombstones"] == []

        project = await _request_as(
            client,
            "POST",
            "/api/projects",
            "alice@example.com",
            PrincipalRole.OWNER,
            json={"name": "Kettle", "objective": "Validate demand"},
        )
        project_id = project.json()["id"]
        project_memory = await _request_as(
            client,
            "POST",
            "/api/memory",
            "alice@example.com",
            PrincipalRole.OWNER,
            json={
                "scope": "project",
                "scope_id": project_id,
                "text": "Project-only supplier fact",
                "kind": "fact",
            },
        )
        assert project_memory.status_code == 201

        outsider_project_retrieval = await _request_as(
            client,
            "POST",
            "/api/memory/retrieve",
            "bob@example.com",
            PrincipalRole.OWNER,
            json={
                "query": "supplier",
                "project_id": project_id,
                "include_user_scope": False,
            },
        )
        assert outsider_project_retrieval.status_code == 403

        cross_tenant = await client.post(
            "/api/memory/retrieve",
            headers=_headers(
                "POST",
                "/api/memory/retrieve",
                "mallory@example.com",
                PrincipalRole.OWNER,
                tenant_id="tenant_other",
            ),
            json={"query": "private"},
        )
        assert cross_tenant.status_code == 401
