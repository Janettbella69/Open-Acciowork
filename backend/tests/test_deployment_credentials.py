"""Root credential recovery stays local, version-bound and fail-closed."""

import json
import os
import subprocess
import sys

import httpx
import pytest
from test_application_backup import source as application_source
from test_auth import NOW, _headers, _settings
from test_auth_api import FakeRuntime, request_as, trusted_settings

from ophira_backend import deployment_credential_cli as cli
from ophira_backend.application_backup import create_backup, restore_backup
from ophira_backend.auth import (
    AuthConfigurationError,
    AuthNonceStore,
    InvalidAuthAssertionError,
    PrincipalAuthenticator,
    build_gateway_assertion_headers,
)
from ophira_backend.backup_files import remove_staging
from ophira_backend.config import Settings
from ophira_backend.constants import PrincipalRole
from ophira_backend.credential_archive import decrypt_credential, encrypt_credential
from ophira_backend.deployment_credentials import (
    DeploymentCredentialId as ID,
)
from ophira_backend.deployment_credentials import (
    DeploymentCredentialRegistry,
    DeploymentCredentialSettings,
)
from ophira_backend.main import create_app
from ophira_backend.maintenance import data_directory_lease
from ophira_backend.secret_vault import CredentialChangedError, VaultError

PASSWORD = "synthetic-root-backup-password"
REPLACEMENT = "synthetic-new-signing-key-00000000000001"


@pytest.fixture
def cold_source(tmp_path, monkeypatch):
    yield from application_source.__wrapped__(tmp_path, monkeypatch)


def authenticate(auth, headers=None):
    return auth.authenticate(
        headers or _headers(),
        method="GET",
        path="/api/projects/project_123",
        client_host="203.0.113.4",
        now=NOW,
    )


def test_live_authenticator_observes_delete_restore_and_rotation_without_env_fallback(tmp_path):
    settings = _settings(tmp_path)
    registry = DeploymentCredentialRegistry(settings)
    auth = PrincipalAuthenticator(settings, AuthNonceStore(tmp_path / "nonces"))
    initial = registry.describe(ID.TRUSTED_GATEWAY)
    archive = registry.export(ID.TRUSTED_GATEWAY, initial["revision"], PASSWORD)
    assert authenticate(auth).tenant_id == settings.trusted_gateway_tenant_id
    deleted = registry.delete(ID.TRUSTED_GATEWAY, initial["revision"])
    assert registry.delete(ID.TRUSTED_GATEWAY, initial["revision"]) == deleted
    assert (
        settings.trusted_gateway_auth_secret.get_secret_value()
        not in registry.vault.path.read_text()
    )
    with pytest.raises(AuthConfigurationError):
        authenticate(auth, _headers(nonce="nonce_deleted_0000000000000001"))
    with pytest.raises(AuthConfigurationError):
        PrincipalAuthenticator(settings, AuthNonceStore(tmp_path / "other-nonces"))
    restored = registry.restore(ID.TRUSTED_GATEWAY, deleted["revision"], archive, PASSWORD)
    assert authenticate(auth, _headers(nonce="nonce_restored_0000000000000001"))
    registry.save(ID.TRUSTED_GATEWAY, REPLACEMENT, restored["revision"])
    with pytest.raises(InvalidAuthAssertionError):
        authenticate(auth, _headers(nonce="nonce_old_key_0000000000000001"))
    headers = build_gateway_assertion_headers(
        REPLACEMENT,
        method="GET",
        path="/api/projects/project_123",
        tenant_id="tenant_acme",
        principal_id="owner",
        role=PrincipalRole.OWNER,
        session_id="gateway_session_synthetic",
        nonce="nonce_new_key_0000000000000001",
        issued_at=NOW,
    )
    assert authenticate(auth, headers)
    with pytest.raises(CredentialChangedError):
        registry.delete(ID.TRUSTED_GATEWAY, initial["revision"])
    registry.vault.path.write_text("broken")
    with pytest.raises(AuthConfigurationError, match="storage"):
        authenticate(auth, _headers(nonce="nonce_corrupt_0000000000000001"))


def test_vault_only_startup_needs_a_valid_saved_key_and_never_downgrades_auth(tmp_path):
    settings = _settings(
        tmp_path, trusted_gateway_auth_source="local_vault", trusted_gateway_auth_secret=None
    )
    registry = DeploymentCredentialRegistry(settings)
    with pytest.raises(AuthConfigurationError):
        PrincipalAuthenticator(settings, AuthNonceStore(tmp_path / "nonces"))
    item = registry.describe(ID.TRUSTED_GATEWAY)
    registry.save(ID.TRUSTED_GATEWAY, REPLACEMENT, item["revision"])
    auth = PrincipalAuthenticator(settings, AuthNonceStore(tmp_path / "nonces"))
    assert auth.status.authenticated and auth.status.mode == "trusted_gateway"
    assert not auth.status.multi_tenant
    assert not settings.trusted_gateway_auth_secret


@pytest.mark.parametrize("change", ["kind", "id", "scope", "password", "revision", "key"])
def test_restore_requires_exact_kind_id_scope_password_revision_and_key(tmp_path, change):
    registry = DeploymentCredentialRegistry(_settings(tmp_path))
    view, secret = registry.snapshot(ID.TRUSTED_GATEWAY)
    payload = decrypt_credential(
        registry.export(ID.TRUSTED_GATEWAY, view["revision"], PASSWORD), PASSWORD
    )
    if change == "kind":
        payload["kind"] = "service"
    elif change == "id":
        payload["id"] = ID.LEGACY_COMMERCIAL
    elif change == "scope":
        payload["scope"] = {"tenant_id": "another_tenant"}
    elif change == "key":
        payload["credential"] = "short-key"
    archive = encrypt_credential(payload, PASSWORD)
    with pytest.raises(ValueError):
        registry.restore(
            ID.TRUSTED_GATEWAY,
            "0" * 64 if change == "revision" else view["revision"],
            archive,
            "incorrect-password" if change == "password" else PASSWORD,
        )
    assert registry.snapshot(ID.TRUSTED_GATEWAY) == (view, secret)


async def test_root_credentials_are_absent_and_inaccessible_to_http_owner(tmp_path):
    config = trusted_settings(tmp_path)
    app = create_app(config, runtime=FakeRuntime())
    root = DeploymentCredentialRegistry(config)
    item = root.describe(ID.TRUSTED_GATEWAY)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://gateway.test"
    ) as client:
        listed = await request_as(client, "GET", "/api/credentials", "owner", PrincipalRole.OWNER)
        assert listed.status_code == 200 and all(
            row["kind"] != "deployment" for row in listed.json()
        )
        for method, suffix, body in (
            ("GET", "", None),
            ("POST", "/export", {"expected_revision": item["revision"], "password": PASSWORD}),
            ("DELETE", "", {"expected_revision": item["revision"], "confirm_delete": True}),
            ("PUT", "", {"expected_revision": item["revision"], "secret": REPLACEMENT}),
        ):
            response = await request_as(
                client,
                method,
                f"/api/credentials/deployment/trusted_gateway{suffix}",
                "owner",
                PrincipalRole.OWNER,
                **({"json": body} if body else {}),
            )
            assert response.status_code in {404, 405}
    assert root.describe(ID.TRUSTED_GATEWAY) == item


def cli_fixture(tmp_path):
    config = tmp_path / "synthetic.env"
    config.write_text(
        f"OPHIRA_TRUSTED_GATEWAY_AUTH_SECRET={REPLACEMENT}\n"
        "OPHIRA_TRUSTED_GATEWAY_TENANT_ID=tenant_acme\n"
        "OPHIRA_HOST=deliberately-invalid-unused-app-config\n"
    )
    args = ["--data-dir", str(tmp_path), "--env-file", str(config)]
    registry = DeploymentCredentialRegistry(
        DeploymentCredentialSettings(_env_file=config, data_dir=tmp_path),
    )
    return args, registry


def test_cli_encrypted_round_trip_review_and_private_output(tmp_path, monkeypatch, capsys):
    args, registry = cli_fixture(tmp_path)
    assert cli.main([*args, "list"]) == 0
    listed = json.loads(capsys.readouterr().out)
    item = listed["credentials"][0]
    assert item["source"] == "environment" and listed["tenant_id"] == "tenant_acme"
    output = tmp_path / "archive.json"
    monkeypatch.setattr(cli.getpass, "getpass", lambda _: PASSWORD)
    export = [
        *args,
        "export",
        "trusted_gateway",
        "--expected-revision",
        item["revision"],
        "--output",
        str(output),
    ]
    assert cli.main(export) == 0
    assert output.stat().st_mode & 0o777 == 0o600
    assert REPLACEMENT not in output.read_text() and PASSWORD not in output.read_text()
    before = output.read_bytes()
    with pytest.raises(SystemExit):
        cli.main(export)
    assert output.read_bytes() == before
    delete = [
        *args,
        "delete",
        "trusted_gateway",
        "--expected-revision",
        item["revision"],
        "--confirm-delete",
    ]
    assert cli.main(delete) == 0
    erased = registry.describe(ID.TRUSTED_GATEWAY)
    assert cli.main(delete) == 0  # Lost-response acknowledgement is durable.
    assert registry.describe(ID.TRUSTED_GATEWAY) == erased
    assert (
        cli.main(
            [
                *args,
                "restore",
                "trusted_gateway",
                "--expected-revision",
                erased["revision"],
                "--input",
                str(output),
                "--confirm-restore",
            ]
        )
        == 0
    )
    with pytest.raises(SystemExit):
        cli.main(delete)
    stdout, stderr = capsys.readouterr()
    assert REPLACEMENT not in stdout + stderr and PASSWORD not in stdout + stderr
    assert registry.snapshot(ID.TRUSTED_GATEWAY)[1] == REPLACEMENT


def test_cli_mutations_require_exclusive_lease_and_confirmation(tmp_path, monkeypatch, capsys):
    args, registry = cli_fixture(tmp_path)
    initial = registry.describe(ID.TRUSTED_GATEWAY)
    delete = [*args, "delete", "trusted_gateway", "--expected-revision", initial["revision"]]
    with pytest.raises(SystemExit):
        cli.main(delete)
    with data_directory_lease(tmp_path):
        assert cli.main([*args, "list"]) == 0
        for command, options in (
            ("delete", ["--confirm-delete"]),
            ("set", []),
            ("restore", ["--input", "unused.json", "--confirm-restore"]),
        ):
            with pytest.raises(SystemExit):
                cli.main(
                    [
                        *args,
                        command,
                        "trusted_gateway",
                        "--expected-revision",
                        initial["revision"],
                        *options,
                    ]
                )
    assert registry.describe(ID.TRUSTED_GATEWAY) == initial
    assert "busy" in capsys.readouterr().err
    monkeypatch.setattr(cli.getpass, "getpass", lambda _: REPLACEMENT)
    assert (
        cli.main([*args, "set", "trusted_gateway", "--expected-revision", initial["revision"]]) == 0
    )
    assert registry.describe(ID.TRUSTED_GATEWAY)["source"] == "stored"


def test_independent_cli_process_delete_is_observed_and_never_exports_plaintext(tmp_path):
    settings = _settings(tmp_path)
    registry = DeploymentCredentialRegistry(settings)
    auth = PrincipalAuthenticator(settings, AuthNonceStore(tmp_path / "nonces"))
    initial = registry.describe(ID.TRUSTED_GATEWAY)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ophira_backend.deployment_credential_cli",
            "--data-dir",
            str(tmp_path),
            "delete",
            "trusted_gateway",
            "--expected-revision",
            initial["revision"],
            "--confirm-delete",
        ],
        env={
            "PATH": os.environ["PATH"],
            "PYTHONPATH": os.environ["PYTHONPATH"],
            "OPHIRA_TRUSTED_GATEWAY_AUTH_SECRET": (
                settings.trusted_gateway_auth_secret.get_secret_value()
            ),
            "OPHIRA_TRUSTED_GATEWAY_TENANT_ID": "tenant_acme",
        },
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    assert json.loads(result.stdout)["source"] == "deleted"
    assert (
        settings.trusted_gateway_auth_secret.get_secret_value() not in result.stdout + result.stderr
    )
    with pytest.raises(AuthConfigurationError):
        authenticate(auth)


def test_cold_recovery_preserves_saved_root_and_revoked_legacy_environment(cold_source, tmp_path):
    data, plugin, *_ = cold_source
    settings = Settings(
        _env_file=None,
        data_dir=data,
        commercial_approval_secret="legacy-synthetic-key-0000000000000001",
    )
    registry = DeploymentCredentialRegistry(settings)
    view = registry.describe(ID.TRUSTED_GATEWAY)
    saved = registry.save(ID.TRUSTED_GATEWAY, REPLACEMENT, view["revision"])
    view = registry.describe(ID.LEGACY_COMMERCIAL)
    erased = registry.delete(ID.LEGACY_COMMERCIAL, view["revision"])
    archive, output = tmp_path / "backup", tmp_path / "recovered"
    create_backup(data, plugin, archive)
    remove_staging(data)
    remove_staging(plugin)
    restore_backup(archive, output)
    restored = DeploymentCredentialRegistry(
        settings.model_copy(update={"data_dir": output / "data"})
    )
    assert restored.snapshot(ID.TRUSTED_GATEWAY) == (saved, REPLACEMENT)
    assert restored.snapshot(ID.LEGACY_COMMERCIAL) == (erased, None)


@pytest.mark.parametrize("bad", ["short", " x" * 32, "x" * 513])
def test_invalid_new_root_key_never_overwrites_reviewed_value(tmp_path, bad):
    registry = DeploymentCredentialRegistry(_settings(tmp_path))
    before = registry.snapshot(ID.TRUSTED_GATEWAY)
    with pytest.raises(VaultError):
        registry.save(ID.TRUSTED_GATEWAY, bad, before[0]["revision"])
    assert registry.snapshot(ID.TRUSTED_GATEWAY) == before
