"""Password-encrypted credential portability and explicit offline decryption.

This module does not load Settings, initialize a vault, or contact a provider.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from .backup_files import parse_json
from .skill_proposal_files import directory_fd, read_relative

FORMAT = "ophira-encrypted-credential"
MAX_ARCHIVE_BYTES = 1024 * 1024
_AAD = f"{FORMAT}:1:scrypt-32768-8-1:aes-256-gcm".encode()


class CredentialArchiveError(ValueError):
    pass


def _key(password: str, salt: bytes) -> bytes:
    if not isinstance(password, str) or not 12 <= len(password) <= 1024:
        raise CredentialArchiveError("Use an archive password of 12 to 1024 characters.")
    return Scrypt(salt=salt, length=32, n=32768, r=8, p=1).derive(password.encode())


def encrypt_credential(payload: dict, password: str) -> bytes:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    if len(raw) > 512 * 1024:
        raise CredentialArchiveError("Credential archive exceeds its size limit.")
    salt, nonce = os.urandom(16), os.urandom(12)
    encrypted = AESGCM(_key(password, salt)).encrypt(nonce, raw, _AAD)
    return (json.dumps({
        "format": FORMAT, "version": 1, "kdf": "scrypt-32768-8-1", "cipher": "aes-256-gcm",
        "salt": base64.b64encode(salt).decode(), "nonce": base64.b64encode(nonce).decode(),
        "ciphertext": base64.b64encode(encrypted).decode(),
    }, indent=2) + "\n").encode()


def decrypt_credential(raw: bytes, password: str) -> dict:
    try:
        if len(raw) > MAX_ARCHIVE_BYTES:
            raise ValueError
        envelope = parse_json(raw)
        if (set(envelope) != {"format", "version", "kdf", "cipher", "salt", "nonce", "ciphertext"}
                or envelope["format"] != FORMAT or type(envelope["version"]) is not int
                or envelope["version"] != 1 or envelope["kdf"] != "scrypt-32768-8-1"
                or envelope["cipher"] != "aes-256-gcm"):
            raise ValueError
        salt, nonce, encrypted = (
            base64.b64decode(envelope[name], validate=True)
            for name in ("salt", "nonce", "ciphertext")
        )
        if len(salt) != 16 or len(nonce) != 12 or len(encrypted) < 16:
            raise ValueError
        payload = parse_json(AESGCM(_key(password, salt)).decrypt(nonce, encrypted, _AAD))
        if (not isinstance(payload, dict) or payload.get("kind") not in {"provider", "connector"}
                or not isinstance(payload.get("credential"), str)
                or not isinstance(payload.get("id"), str)):
            raise ValueError
        return payload
    except (ValueError, TypeError, KeyError, InvalidTag) as exc:
        raise CredentialArchiveError("Invalid credential archive or incorrect password.") from exc


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Decrypt one exported credential into a private file",
    )
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    try:
        raw = read_relative(args.input.absolute().parent, args.input.name, MAX_ARCHIVE_BYTES)
        payload = decrypt_credential(raw, getpass.getpass("Archive password: "))
        with directory_fd(args.output.absolute().parent) as parent:
            descriptor = os.open(args.output.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                                 | os.O_NOFOLLOW, 0o600, dir_fd=parent)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.fsync(parent)
    except (ValueError, OSError, EOFError):
        parser.exit(1, "Credential decryption failed; check the password, paths and archive.\n")
    print("Credential decrypted to the requested private file.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
