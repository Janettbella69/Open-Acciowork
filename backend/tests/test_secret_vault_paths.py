"""Private vault operations must not follow linked files or chmod their targets."""

import json
import os

import pytest

from ophira_backend.providers import FileSecretVault


@pytest.mark.parametrize("link", ["symlink", "hardlink"])
def test_existing_linked_vault_is_rejected_before_chmod(tmp_path, link):
    outside = tmp_path / "outside.json"
    outside.write_text('{"private":"do-not-read"}')
    outside.chmod(0o644)
    root = tmp_path / "secrets"
    root.mkdir()
    destination = root / "provider-secrets.json"
    if link == "symlink":
        destination.symlink_to(outside)
    else:
        os.link(outside, destination)
    with pytest.raises((OSError, ValueError)):
        FileSecretVault(root)
    assert outside.stat().st_mode & 0o777 == 0o644
    assert outside.read_text() == '{"private":"do-not-read"}'


def test_linked_vault_directory_rejected_before_read_or_chmod(tmp_path):
    outside = tmp_path / "external"
    outside.mkdir(mode=0o755)
    secret = outside / "provider-secrets.json"
    secret.write_text("{}")
    secret.chmod(0o644)
    root = tmp_path / "secrets"
    root.symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        FileSecretVault(root)
    assert secret.stat().st_mode & 0o777 == 0o644


@pytest.mark.parametrize("target", ["provider-secrets.json", "provider-secrets.json.lock",
                                    "provider-secrets.json.deletion-register.json"])
def test_link_substituted_after_construction_is_rejected(tmp_path, target):
    root = tmp_path / "secrets"
    vault = FileSecretVault(root)
    vault.set("account", "token")
    outside = tmp_path / "external.json"
    outside.write_text('{"account":"outside"}')
    selected = root / target
    selected.unlink(missing_ok=True)
    selected.symlink_to(outside)
    with pytest.raises((OSError, ValueError)):
        if target.endswith("deletion-register.json"):
            vault.delete("account")
        elif target.endswith(".lock"):
            vault.set("account", "replacement")
        else:
            vault.get("account")
    assert outside.read_text() == '{"account":"outside"}'


def test_duplicate_vault_keys_fail_closed(tmp_path):
    vault = FileSecretVault(tmp_path / "secrets")
    vault.path.write_text('{"account":"first","account":"second"}')
    with pytest.raises(ValueError, match="duplicate"):
        vault.get("account")


def test_private_replacement_preserves_deletion_register(tmp_path):
    vault = FileSecretVault(tmp_path / "secrets", "weixin-secrets.json")
    vault.set("account", "first")
    vault.set("account", "second")
    vault.delete("account")
    assert vault.get("account") is None
    assert json.loads(vault.deletion_register_path.read_text())["count"] == 2
    assert vault.path.stat().st_mode & 0o777 == 0o600
