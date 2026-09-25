"""Explicit Provider key export and revocation keep tenant and vault boundaries."""

from multiprocessing import get_context
from pathlib import Path
from queue import Empty

import httpx
import pytest
from test_auth_api import FakeRuntime, request_as, trusted_settings

from ophira_backend.config import Settings
from ophira_backend.constants import PrincipalRole
from ophira_backend.main import create_app
from ophira_backend.models import ProviderProfileUpsert
from ophira_backend.providers import ProviderConfigurationError, ProviderRegistry


@pytest.mark.asyncio
async def test_admin_export_and_revoke_provider_key_without_normal_api_exposure(tmp_path):
    app = create_app(trusted_settings(tmp_path), runtime=FakeRuntime())
    base = "/api/providers/deepseek"
    credential = base + "/credential"
    secret = "private-provider-key-123"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://gateway.test",
    ) as client:
        saved = await request_as(
            client, "PUT", base, "admin", PrincipalRole.ADMIN,
            json={
                "display_name": "DeepSeek", "auth_env_name": "ANTHROPIC_AUTH_TOKEN",
                "model": "deepseek-v4-pro", "secret": secret,
            },
        )
        assert saved.status_code == 200, saved.text
        assert secret not in saved.text
        assert saved.json()["secret_configured"] is True
        listed = await request_as(
            client, "GET", "/api/providers", "member", PrincipalRole.MEMBER,
        )
        assert secret not in listed.text
        for action in ("export", "revoke"):
            denied = await request_as(
                client, "POST", credential + "/" + action,
                "member", PrincipalRole.MEMBER,
                json={"confirmation": action},
            )
            assert denied.status_code == 403
        assert (await request_as(
            client, "GET", credential + "/export", "admin", PrincipalRole.ADMIN,
        )).status_code == 405
        wrong = await request_as(
            client, "POST", credential + "/export", "admin", PrincipalRole.ADMIN,
            json={"confirmation": "revoke"},
        )
        assert wrong.status_code == 400
        exported = await request_as(
            client, "POST", credential + "/export", "admin", PrincipalRole.ADMIN,
            json={"confirmation": "export"},
        )
        assert exported.status_code == 200, exported.text
        assert exported.headers["cache-control"] == "no-store"
        assert exported.headers["content-disposition"].startswith("attachment;")
        assert exported.headers["x-content-type-options"] == "nosniff"
        assert exported.json()["secret"] == secret
        assert exported.json()["provider"]["id"] == "deepseek"
        assert "last_error_message" not in exported.json()["provider"]
        revoked = await request_as(
            client, "POST", credential + "/revoke", "admin", PrincipalRole.ADMIN,
            json={"confirmation": "revoke"},
        )
        assert revoked.status_code == 200, revoked.text
        assert revoked.json()["secret_configured"] is False
        assert revoked.json()["last_verified_at"] is None
        assert secret not in revoked.text
        assert app.state.providers.vault.get("deepseek") is None
        with pytest.raises(ProviderConfigurationError):
            app.state.providers.build_worker_env("deepseek")
        unavailable = await request_as(
            client, "POST", credential + "/export", "admin", PrincipalRole.ADMIN,
            json={"confirmation": "export"},
        )
        assert unavailable.status_code == 409
        replaced = await request_as(
            client, "PUT", base, "admin", PrincipalRole.ADMIN,
            json={
                "display_name": "DeepSeek", "auth_env_name": "ANTHROPIC_AUTH_TOKEN",
                "model": "deepseek-v4-pro", "secret": "new-private-key",
            },
        )
        assert replaced.status_code == 200
        assert app.state.providers.vault.get("deepseek") == "new-private-key"


@pytest.mark.asyncio
async def test_environment_managed_key_cannot_silently_reappear_after_revoke(tmp_path):
    settings = trusted_settings(
        tmp_path, claude_model="claude-sonnet-4-5", anthropic_auth_token="host-key",
    )
    app = create_app(settings, runtime=FakeRuntime())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://gateway.test",
    ) as client:
        denied = await request_as(
            client, "POST", "/api/providers/environment/credential/revoke",
            "admin", PrincipalRole.ADMIN, json={"confirmation": "revoke"},
        )
        assert denied.status_code == 409
        assert "Remove the host environment key" in denied.text
    assert app.state.providers.vault.get("environment") == "host-key"


def _replace_key(root, paused, release, results):
    registry = ProviderRegistry(Settings(_env_file=None, data_dir=Path(root)))
    original_set = registry.vault.set

    def delayed_set(key, value):
        paused.set()
        if not release.wait(10):
            raise RuntimeError("Provider key replacement was not released")
        original_set(key, value)

    registry.vault.set = delayed_set
    try:
        registry.upsert("deepseek", ProviderProfileUpsert(
            display_name="DeepSeek", auth_env_name="ANTHROPIC_AUTH_TOKEN",
            model="deepseek-v4-pro", secret="new-key",
        ))
        results.put(("replace", "ok"))
    except BaseException as exc:
        results.put(("replace", type(exc).__name__))


def _revoke_key(root, started, results):
    started.set()
    try:
        registry = ProviderRegistry(Settings(_env_file=None, data_dir=Path(root)))
        registry.revoke_credential("deepseek")
        results.put(("revoke", "ok"))
    except BaseException as exc:
        results.put(("revoke", type(exc).__name__))


def test_revoke_waits_for_inflight_provider_key_replacement(tmp_path):
    registry = ProviderRegistry(Settings(_env_file=None, data_dir=tmp_path))
    registry.upsert("deepseek", ProviderProfileUpsert(
        display_name="DeepSeek", auth_env_name="ANTHROPIC_AUTH_TOKEN",
        model="deepseek-v4-pro", secret="old-key",
    ))
    context = get_context("spawn")
    paused, release, started = context.Event(), context.Event(), context.Event()
    results = context.Queue()
    replacing = context.Process(target=_replace_key, args=(tmp_path, paused, release, results))
    revoking = context.Process(target=_revoke_key, args=(tmp_path, started, results))
    replacing.start()
    try:
        assert paused.wait(10)
        revoking.start()
        assert started.wait(10)
        with pytest.raises(Empty):
            results.get(timeout=0.25)
        release.set()
        assert sorted(results.get(timeout=10) for _ in range(2)) == [
            ("replace", "ok"), ("revoke", "ok"),
        ]
        for worker in (replacing, revoking):
            worker.join(timeout=10)
            assert worker.exitcode == 0
        assert registry.vault.get("deepseek") is None
        assert registry.get("deepseek").secret_configured is False
    finally:
        release.set()
        for worker in (replacing, revoking):
            if worker.pid is not None:
                if worker.is_alive():
                    worker.terminate()
                worker.join(timeout=10)
        results.close()
