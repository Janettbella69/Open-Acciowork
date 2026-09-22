import json
import sys

import pytest

from ophira_backend import credential_archive
from ophira_backend.credential_archive import (
    CredentialArchiveError,
    decrypt_credential,
    encrypt_credential,
)

PASSWORD = "a long backup password"
PAYLOAD = {"kind": "provider", "id": "test", "credential": "private-canary-credential"}


def test_encrypted_exports_round_trip_without_plaintext_and_are_randomized():
    first = encrypt_credential(PAYLOAD, PASSWORD)
    second = encrypt_credential(PAYLOAD, PASSWORD)
    assert first != second
    assert PAYLOAD["credential"].encode() not in first and PASSWORD.encode() not in first
    assert decrypt_credential(first, PASSWORD) == PAYLOAD


@pytest.mark.parametrize("change", ["password", "nonce", "salt", "ciphertext", "kdf", "format"])
def test_wrong_password_and_tampering_fail_before_releasing_plaintext(change):
    envelope = json.loads(encrypt_credential(PAYLOAD, PASSWORD))
    if change != "password":
        value = envelope[change]
        envelope[change] = ("A" if value[0] != "A" else "B") + value[1:]
    with pytest.raises(CredentialArchiveError, match="incorrect password"):
        decrypt_credential(json.dumps(envelope).encode(), "wrong password" if change == "password"
                           else PASSWORD)


def test_offline_decrypt_produces_private_file_and_never_overwrites(tmp_path, monkeypatch, capsys):
    archive, output = tmp_path / "encrypted.json", tmp_path / "decrypted.json"
    archive.write_bytes(encrypt_credential(PAYLOAD, PASSWORD))
    monkeypatch.setattr(credential_archive.getpass, "getpass", lambda _: PASSWORD)
    monkeypatch.setattr(sys, "argv", ["credential_archive", str(archive), str(output)])
    assert credential_archive.main() == 0
    assert json.loads(output.read_bytes()) == PAYLOAD
    assert (output.stat().st_mode & 0o777) == 0o600
    assert PAYLOAD["credential"] not in capsys.readouterr().out
    with pytest.raises(SystemExit) as exit_info:
        credential_archive.main()
    assert exit_info.value.code == 1 and json.loads(output.read_bytes()) == PAYLOAD


def test_imported_work_factors_are_not_executed(monkeypatch):
    envelope = json.loads(encrypt_credential(PAYLOAD, PASSWORD))
    envelope["kdf"] = "scrypt-2147483648-8-1"

    def reject(*args):
        pytest.fail("unapproved KDF parameters must be rejected before deriving a key")

    monkeypatch.setattr(credential_archive, "_key", reject)
    with pytest.raises(CredentialArchiveError):
        decrypt_credential(json.dumps(envelope).encode(), PASSWORD)
