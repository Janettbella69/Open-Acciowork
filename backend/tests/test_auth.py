from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from ophira_backend.auth import (
    AuthAssertionExpiredError,
    AuthAssertionReplayError,
    AuthenticationError,
    AuthNonceStore,
    AuthorizationError,
    InvalidAuthAssertionError,
    PrincipalAuthenticator,
    ProjectAccessStore,
    ProjectPermission,
    build_gateway_assertion_headers,
)
from ophira_backend.config import Settings
from ophira_backend.constants import (
    EvidenceSourceType,
    MemoryKind,
    MemoryScope,
    MemorySource,
    PrincipalRole,
)
from ophira_backend.memory import FileMemoryProvider
from ophira_backend.models import PrincipalContext, ProjectCreate
from ophira_backend.storage import (
    FileLedger,
    LedgerIdentityScopeError,
    SourceReceiptScopeError,
)

SECRET = "test-trusted-gateway-signing-secret-0001"
NOW = datetime(2026, 9, 3, 8, 30, tzinfo=UTC)


def _settings(tmp_path: Path, **overrides) -> Settings:
    values = {
        "data_dir": tmp_path,
        "auth_mode": "trusted_gateway",
        "trusted_gateway_auth_secret": SECRET,
        "trusted_gateway_tenant_id": "tenant_acme",
        "auth_assertion_max_age_seconds": 60,
        "auth_assertion_future_skew_seconds": 5,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _headers(
    *,
    role: PrincipalRole = PrincipalRole.OWNER,
    tenant_id: str = "tenant_acme",
    principal_id: str = "owner@example.com",
    session_id: str = "principal_session_0001",
    path: str = "/api/projects/project_123",
    issued_at: datetime = NOW,
    nonce: str = "nonce_00000000000000000001",
) -> dict[str, str]:
    return build_gateway_assertion_headers(
        SECRET,
        method="GET",
        path=path,
        tenant_id=tenant_id,
        principal_id=principal_id,
        role=role,
        session_id=session_id,
        issued_at=issued_at,
        nonce=nonce,
    )


def _principal(
    principal_id: str,
    role: PrincipalRole,
    *,
    tenant_id: str = "tenant_acme",
    session_id: str | None = None,
) -> PrincipalContext:
    return PrincipalContext(
        tenant_id=tenant_id,
        principal_id=principal_id,
        role=role,
        session_id=session_id or f"principal_session_{principal_id.replace('@', '_')}",
        assertion_issued_at=NOW,
        assertion_expires_at=NOW + timedelta(minutes=1),
        assertion_nonce=("nonce_" + principal_id.encode().hex() + "0" * 40)[:40],
    )


def test_auth_configuration_is_explicit_about_local_only_and_trusted_modes(
    tmp_path: Path,
) -> None:
    local = Settings(_env_file=None, data_dir=tmp_path)
    assert local.auth_status() == {
        "mode": "local_only",
        "authenticated": False,
        "multi_tenant": False,
        "trusted_gateway_assertions_required": False,
    }
    with pytest.raises(ValidationError, match="requires OPHIRA_TRUSTED_GATEWAY_AUTH_SECRET"):
        Settings(_env_file=None, data_dir=tmp_path, auth_mode="trusted_gateway")
    with pytest.raises(ValidationError, match="OPHIRA_TRUSTED_GATEWAY_TENANT_ID"):
        Settings(
            _env_file=None,
            data_dir=tmp_path,
            auth_mode="trusted_gateway",
            trusted_gateway_auth_secret=SECRET,
        )
    with pytest.raises(ValidationError, match="requires a loopback"):
        Settings(_env_file=None, data_dir=tmp_path, host="0.0.0.0")

    trusted = _settings(tmp_path)
    assert trusted.auth_status() == {
        "mode": "trusted_gateway",
        "authenticated": True,
        "multi_tenant": False,
        "trusted_gateway_assertions_required": True,
    }


def test_gateway_assertion_binds_all_identity_method_path_and_nonce(tmp_path: Path) -> None:
    auth = PrincipalAuthenticator(
        _settings(tmp_path),
        AuthNonceStore(tmp_path / "auth-nonces"),
    )
    headers = _headers()
    principal = auth.authenticate(
        headers,
        method="GET",
        path="/api/projects/project_123",
        client_host="203.0.113.4",
        now=NOW,
    )

    assert principal is not None
    assert principal.tenant_id == "tenant_acme"
    assert principal.principal_id == "owner@example.com"
    assert principal.role == PrincipalRole.OWNER
    assert principal.session_id == "principal_session_0001"
    assert auth.status.multi_tenant is False

    with pytest.raises(AuthAssertionReplayError):
        auth.authenticate(
            headers,
            method="GET",
            path="/api/projects/project_123",
            client_host="203.0.113.4",
            now=NOW,
        )


@pytest.mark.parametrize(
    ("mutation", "method", "path"),
    [
        ({"X-Ophira-Auth-Role": "admin"}, "GET", "/api/projects/project_123"),
        (
            {"X-Ophira-Auth-Principal-Id": "attacker@example.com"},
            "GET",
            "/api/projects/project_123",
        ),
        ({}, "POST", "/api/projects/project_123"),
        ({}, "GET", "/api/projects/project_other"),
    ],
)
def test_modified_identity_method_or_path_fails_signature(
    tmp_path: Path,
    mutation: dict[str, str],
    method: str,
    path: str,
) -> None:
    auth = PrincipalAuthenticator(
        _settings(tmp_path),
        AuthNonceStore(tmp_path / f"nonces-{method}-{path.rsplit('/', 1)[-1]}"),
    )
    headers = {**_headers(), **mutation}
    with pytest.raises(InvalidAuthAssertionError, match="signature"):
        auth.authenticate(
            headers,
            method=method,
            path=path,
            client_host="203.0.113.4",
            now=NOW,
        )


def test_unsigned_expired_future_duplicate_and_noncanonical_assertions_fail_closed(
    tmp_path: Path,
) -> None:
    auth = PrincipalAuthenticator(
        _settings(tmp_path),
        AuthNonceStore(tmp_path / "nonces"),
    )
    unsigned = _headers()
    unsigned.pop("X-Ophira-Auth-Signature")
    with pytest.raises(InvalidAuthAssertionError, match="incomplete"):
        auth.authenticate(
            unsigned,
            method="GET",
            path="/api/projects/project_123",
            client_host="203.0.113.4",
            now=NOW,
        )

    with pytest.raises(AuthAssertionExpiredError):
        auth.authenticate(
            _headers(issued_at=NOW - timedelta(seconds=60), nonce="nonce_expired_00000000001"),
            method="GET",
            path="/api/projects/project_123",
            client_host="203.0.113.4",
            now=NOW,
        )
    with pytest.raises(InvalidAuthAssertionError, match="future"):
        auth.authenticate(
            _headers(issued_at=NOW + timedelta(seconds=6), nonce="nonce_future_000000000001"),
            method="GET",
            path="/api/projects/project_123",
            client_host="203.0.113.4",
            now=NOW,
        )
    with pytest.raises(InvalidAuthAssertionError, match="duplicate"):
        auth.authenticate(
            [*_headers(nonce="nonce_duplicate_000000001").items(), ("x-ophira-auth-role", "owner")],
            method="GET",
            path="/api/projects/project_123",
            client_host="203.0.113.4",
            now=NOW,
        )
    with pytest.raises(InvalidAuthAssertionError, match="path"):
        auth.authenticate(
            _headers(nonce="nonce_path_000000000000001"),
            method="GET",
            path="/api/projects/%2e%2e/secrets",
            client_host="203.0.113.4",
            now=NOW,
        )


def test_local_only_mode_never_invents_a_principal_and_rejects_remote_clients(
    tmp_path: Path,
) -> None:
    auth = PrincipalAuthenticator(
        Settings(_env_file=None, data_dir=tmp_path),
        AuthNonceStore(tmp_path / "nonces"),
    )
    assert (
        auth.authenticate(
            {},
            method="GET",
            path="/api/projects",
            client_host="127.0.0.1",
            now=NOW,
        )
        is None
    )
    with pytest.raises(AuthenticationError, match="non-loopback"):
        auth.authenticate(
            {},
            method="GET",
            path="/api/projects",
            client_host="198.51.100.8",
            now=NOW,
        )


def test_project_access_rejects_cross_tenant_unknown_principal_and_role_escalation(
    tmp_path: Path,
) -> None:
    store = ProjectAccessStore(tmp_path / "project-access", clock=lambda: NOW)
    owner = _principal("owner@example.com", PrincipalRole.OWNER)
    record = store.bind_project("project_123", owner)
    assert record.tenant_id == "tenant_acme"
    assert store.authorize(
        "project_123", owner, ProjectPermission.PROJECT_ACCESS_MANAGE, now=NOW
    ).role == PrincipalRole.OWNER
    with pytest.raises(AuthorizationError, match="owner role cannot be changed"):
        store.grant(
            "project_123",
            owner,
            principal_id=owner.principal_id,
            role=PrincipalRole.MEMBER,
        )
    assert store.get("project_123").memberships[owner.principal_id].role == PrincipalRole.OWNER

    record = store.grant(
        "project_123",
        owner,
        principal_id="reviewer@example.com",
        role=PrincipalRole.REVIEWER,
    )
    assert record.memberships["reviewer@example.com"].role == PrincipalRole.REVIEWER
    reviewer = _principal("reviewer@example.com", PrincipalRole.REVIEWER)
    store.authorize(
        "project_123", reviewer, ProjectPermission.COMMERCIAL_REVIEW, now=NOW
    )
    with pytest.raises(AuthorizationError, match="does not allow"):
        store.authorize(
            "project_123", reviewer, ProjectPermission.PROJECT_WRITE, now=NOW
        )
    with pytest.raises(AuthorizationError, match="tenant"):
        store.authorize(
            "project_123",
            _principal(
                "reviewer@example.com",
                PrincipalRole.REVIEWER,
                tenant_id="tenant_other",
            ),
            ProjectPermission.PROJECT_READ,
            now=NOW,
        )
    with pytest.raises(AuthorizationError, match="not a project member"):
        store.authorize(
            "project_123",
            _principal("unknown@example.com", PrincipalRole.VIEWER),
            ProjectPermission.PROJECT_READ,
            now=NOW,
        )
    with pytest.raises(AuthorizationError, match="role"):
        store.authorize(
            "project_123",
            _principal("reviewer@example.com", PrincipalRole.ADMIN),
            ProjectPermission.PROJECT_WRITE,
            now=NOW,
        )


def test_ledger_run_receipt_and_memory_inherit_authenticated_principal_scope(
    tmp_path: Path,
) -> None:
    ledger = FileLedger(tmp_path)
    owner = _principal("owner@example.com", PrincipalRole.OWNER)
    member = _principal("member@example.com", PrincipalRole.MEMBER)
    project = ledger.create_project(
        ProjectCreate(name="Kettle", objective="Validate"),
        principal=owner,
    )
    assert project.identity_binding == "authenticated_principal"
    assert project.owner_principal_id == owner.principal_id

    with pytest.raises(LedgerIdentityScopeError, match="trusted principal"):
        ledger.create_run(project.id, "Missing principal")
    with pytest.raises(LedgerIdentityScopeError, match="outside"):
        ledger.create_run(
            project.id,
            "Cross tenant",
            principal=_principal(
                "member@example.com",
                PrincipalRole.MEMBER,
                tenant_id="tenant_other",
            ),
        )

    run = ledger.create_run(project.id, "Read supplier", principal=member)
    run = ledger.update_run(run.id, sdk_session_id="sdk_session_supplier")
    assert run.tenant_id == member.tenant_id
    assert run.principal_id == member.principal_id
    assert run.principal_role == PrincipalRole.MEMBER

    receipt = ledger.issue_tool_receipt(
        project.id,
        run.id,
        tool="WebFetch",
        source_type=EvidenceSourceType.WEB,
        canonical_source="https://example.com/supplier",
        payload={"price": 12.5},
        object_type="supplier_quote",
    )
    assert receipt.identity_binding == "authenticated_principal"
    assert receipt.tenant_id == member.tenant_id
    assert receipt.principal_id == member.principal_id
    assert receipt.principal_role == PrincipalRole.MEMBER
    assert receipt.principal_session_id == member.session_id
    assert ledger.verify_tool_receipt(
        project.id,
        receipt.id,
        run_id=run.id,
        session_id="sdk_session_supplier",
    ).receipt == receipt
    with pytest.raises(SourceReceiptScopeError, match="principal"):
        ledger.verify_tool_receipt(
            project.id,
            receipt.id,
            run_id=run.id,
            session_id="sdk_session_supplier",
            principal_id="attacker@example.com",
        )

    memory = FileMemoryProvider(tmp_path / "memory")
    item = memory.propose(
        scope=MemoryScope.PROJECT,
        scope_id=project.id,
        text="Use USD for supplier comparisons.",
        kind=MemoryKind.FACT,
        source=MemorySource.USER,
        principal=member,
    )
    assert item.identity_binding == "authenticated_principal"
    assert item.tenant_id == member.tenant_id
    assert item.principal_id == member.principal_id
    assert item.created_by == member.principal_id


def test_legacy_project_stays_local_only_until_explicit_owner_migration(
    tmp_path: Path,
) -> None:
    ledger = FileLedger(tmp_path)
    project = ledger.create_project(ProjectCreate(name="Legacy", objective="Migrate safely"))
    owner = _principal("owner@example.com", PrincipalRole.OWNER)
    with pytest.raises(LedgerIdentityScopeError, match="bind the local project"):
        ledger.create_run(project.id, "Do not auto-claim", principal=owner)

    migrated = ledger.bind_project_identity(project.id, owner)
    assert migrated.identity_binding == "authenticated_principal"
    assert migrated.owner_principal_id == owner.principal_id
    assert ledger.create_run(project.id, "Now bound", principal=owner).principal_id == (
        owner.principal_id
    )
