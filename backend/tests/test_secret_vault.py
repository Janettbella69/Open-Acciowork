from __future__ import annotations

import json
import multiprocessing
import os
from pathlib import Path

import pytest

from ophira_backend.config import Settings
from ophira_backend.providers import ProviderConfigurationError, ProviderRegistry
from ophira_backend.secret_vault import CredentialChangedError, FileSecretVault, VaultError


def test_legacy_migration_is_private_and_preserves_every_credential(tmp_path):
    path = tmp_path / "provider-secrets.json"
    path.write_text(json.dumps({"first": "original-A", "second": "original-B"}))
    vault = FileSecretVault(tmp_path)
    assert vault.get("first") == "original-A" and vault.get("second") == "original-B"
    assert (path.stat().st_mode & 0o777) == 0o600
    assert (tmp_path.stat().st_mode & 0o777) == 0o700
    view = vault.describe("first")
    assert "original-A" not in str(view) and view["source"] == "stored"
    assert FileSecretVault(tmp_path).describe("first") == view


@pytest.mark.parametrize("raw", [b"{broken", b"[]", b'{"key":"a","key":"b"}',
                                b'{"key":1}', b'{"key":"' + b"x" * 65537 + b'"}'])
def test_invalid_vault_is_not_treated_as_empty_or_overwritten(tmp_path, raw):
    path = tmp_path / "provider-secrets.json"
    path.write_bytes(raw)
    with pytest.raises(VaultError):
        FileSecretVault(tmp_path)
    assert path.read_bytes() == raw


@pytest.mark.parametrize("attack", ["symlink", "hardlink", "fifo", "parent", "lock",
                                    "lock_symlink", "lock_fifo"])
def test_vault_rejects_links_and_special_files(tmp_path, attack):
    root = tmp_path / "vault"
    root.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    source = other / "secret.json"
    source.write_text('{"key":"private-canary"}')
    path = root / "provider-secrets.json"
    if attack == "symlink":
        path.symlink_to(source)
    elif attack == "hardlink":
        os.link(source, path)
    elif attack == "fifo":
        os.mkfifo(path)
    elif attack == "lock":
        os.link(source, root / "provider-secrets.json.lock")
    elif attack == "lock_symlink":
        (root / "provider-secrets.json.lock").symlink_to(source)
    elif attack == "lock_fifo":
        os.mkfifo(root / "provider-secrets.json.lock")
    else:
        root.rmdir()
        root.symlink_to(other, target_is_directory=True)
    with pytest.raises((OSError, ValueError)):
        FileSecretVault(root)
    assert source.read_text() == '{"key":"private-canary"}'


def test_review_token_binds_credential_generation_and_replayed_delete_is_safe(tmp_path):
    vault = FileSecretVault(tmp_path)
    vault.set("key", "first")
    first = vault.describe("key")["revision"]
    vault.set("key", "second")
    with pytest.raises(CredentialChangedError):
        vault.delete("key", expected_revision=first)
    assert vault.get("key") == "second"
    second = vault.describe("key")["revision"]
    receipt = vault.delete("key", expected_revision=second, deleted_by="owner")
    assert receipt["source"] == "deleted" and receipt["deleted_by"] == "owner"
    assert vault.delete("key", expected_revision=second) == receipt
    assert "second" not in vault.path.read_text()
    assert FileSecretVault(tmp_path).resolve("key", fallback="environment-key") is None
    vault.set("key", "replacement")
    with pytest.raises(CredentialChangedError):
        vault.delete("key", expected_revision=second)
    assert vault.get("key") == "replacement"


def test_environment_rotation_and_explicit_reconfiguration(tmp_path):
    vault = FileSecretVault(tmp_path)
    previous = vault.describe("key", fallback="old-environment")["revision"]
    with pytest.raises(CredentialChangedError):
        vault.delete("key", expected_revision=previous, fallback="new-environment")
    current = vault.describe("key", fallback="new-environment")["revision"]
    assert vault.capture("key", current, fallback="new-environment") == "new-environment"
    vault.delete("key", expected_revision=current, fallback="new-environment")
    assert vault.resolve("key", fallback="new-environment") is None
    vault.set("key", "new-environment", only_if_absent=True)
    assert vault.get("key") is None
    vault.set("key", "saved-by-human")
    assert vault.resolve("key", fallback="new-environment") == "saved-by-human"


def test_environment_provider_stays_deleted_after_bootstrap(tmp_path):
    settings = Settings(data_dir=tmp_path, anthropic_api_key="bootstrap-key", claude_model="test")
    provider = ProviderRegistry(settings)
    provider.vault.delete("environment")
    restarted = ProviderRegistry(settings)
    assert not restarted.get("environment").secret_configured
    with pytest.raises(ProviderConfigurationError):
        restarted.build_worker_env("environment")
    assert "bootstrap-key" not in restarted.vault.path.read_text()


def _writer(root, identifier, gate):
    original_open = os.open
    first_lock = True

    def synchronized_open(path, flags, *args, **kwargs):
        nonlocal first_lock
        if path == "provider-secrets.json.lock" and first_lock:
            first_lock = False
            # Reproduce both processes creating the first lock at the same time.
            gate.wait(10)
        return original_open(path, flags, *args, **kwargs)

    os.open = synchronized_open
    try:
        vault = FileSecretVault(Path(root))
    finally:
        os.open = original_open
    for index in range(10):
        vault.set(identifier, f"{identifier}-{index}")


def test_concurrent_processes_preserve_unrelated_credential_updates(tmp_path):
    context = multiprocessing.get_context("spawn")
    gate = context.Barrier(2)
    processes = [context.Process(target=_writer, args=(str(tmp_path), name, gate))
                 for name in ("one", "two")]
    for process in processes:
        process.start()
    try:
        for process in processes:
            process.join(15)
        assert [process.exitcode for process in processes] == [0, 0]
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join(5)
    vault = FileSecretVault(tmp_path)
    assert vault.get("one") == "one-9" and vault.get("two") == "two-9"


def _crash_delete(root, token, after_replace):
    vault = FileSecretVault(Path(root))
    original = os.replace

    def crash(*args, **kwargs):
        if after_replace:
            original(*args, **kwargs)
        os._exit(71)

    os.replace = crash
    vault.delete("key", expected_revision=token)


@pytest.mark.parametrize("after_replace", [False, True])
def test_process_exit_during_delete_keeps_authoritative_state(tmp_path, after_replace):
    vault = FileSecretVault(tmp_path)
    vault.set("key", "private-key")
    token = vault.describe("key")["revision"]
    process = multiprocessing.get_context("spawn").Process(
        target=_crash_delete, args=(str(tmp_path), token, after_replace),
    )
    process.start()
    process.join(15)
    assert process.exitcode == 71
    restarted = FileSecretVault(tmp_path)
    assert restarted.get("key") == (None if after_replace else "private-key")
    assert not list(tmp_path.glob("*.tmp"))
    restarted.delete("key", expected_revision=token)
    assert restarted.get("key") is None


def test_authorization_rechecked_after_waiting_for_vault_transaction(tmp_path):
    vault = FileSecretVault(tmp_path)
    vault.set("key", "retained")

    def deny():
        raise PermissionError("revoked")

    with pytest.raises(PermissionError):
        vault.delete("key", authorize=deny)
    assert vault.get("key") == "retained"
