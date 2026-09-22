"""Private credential storage with process transactions and durable revocation.

Public review tokens are keyed commitments, not hashes revealing key material.
The old string-map format migrates under the same lock without changing secrets.
"""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import re
import stat
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path

from .models import utc_now
from .skill_proposal_files import directory_fd, read_at

MAX_VAULT_BYTES = 16 * 1024 * 1024
MAX_SECRET_BYTES = 64 * 1024


class VaultError(ValueError):
    pass


class CredentialChangedError(VaultError):
    pass


class FileSecretVault:
    """The private vault file is never served; exports are separate encrypted copies."""

    def __init__(self, root: Path, filename: str = "provider-secrets.json") -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]+\.json", filename):
            raise VaultError("Invalid vault filename")
        self.root = root.absolute()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / filename
        self._lock = threading.RLock()
        with self._transaction() as fd:
            self._load(fd)

    @staticmethod
    def _id(identifier):
        if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", identifier):
            raise VaultError("Invalid credential identifier")
        return identifier

    @contextmanager
    def _transaction(self):
        with self._lock, directory_fd(self.root) as fd:
            os.fchmod(fd, 0o700)
            name = self.path.name + ".lock"
            flags = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
            try:
                # Concurrent nonexclusive creation can fail with ENOENT on macOS.
                # Only the creator makes the file; all other processes open it.
                lock = os.open(name, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=fd)
            except FileExistsError:
                lock = os.open(name, flags, dir_fd=fd)
            try:
                info = os.fstat(lock)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise VaultError("Invalid vault lock")
                fcntl.flock(lock, fcntl.LOCK_EX)
                # Remove only unpublished staging files owned by this vault.
                pattern = re.compile(rf"\.{re.escape(self.path.name)}\.[a-f0-9]{{32}}\.tmp")
                for name in os.listdir(fd):
                    if pattern.fullmatch(name) or name == self.path.with_suffix(".tmp").name:
                        os.unlink(name, dir_fd=fd)
                yield fd
            finally:
                os.close(lock)

    def _write(self, fd, value):
        raw = (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        if len(raw) > MAX_VAULT_BYTES:
            raise VaultError("Credential storage limit exceeded")
        temporary = f".{self.path.name}.{uuid.uuid4().hex}.tmp"
        output = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=fd)
        try:
            with os.fdopen(output, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path.name, src_dir_fd=fd, dst_dir_fd=fd)
            os.fsync(fd)
        finally:
            try:
                os.unlink(temporary, dir_fd=fd)
            except FileNotFoundError:
                pass

    def _load(self, fd):
        def unique(pairs):
            values = {}
            for key, value in pairs:
                if key in values:
                    raise VaultError("Credential storage is invalid")
                values[key] = value
            return values

        try:
            raw = read_at(fd, self.path.name, MAX_VAULT_BYTES)
        except FileNotFoundError:
            raw = b"{}"
        try:
            state = json.loads(raw, object_pairs_hook=unique)
            if not isinstance(state, dict):
                raise ValueError
            if all(isinstance(value, str) for value in state.values()):
                if len(state) > 5000 or any(len(value.encode()) > MAX_SECRET_BYTES
                                           for value in state.values()):
                    raise ValueError
                records = {self._id(key): {"secret": value, "generation": uuid.uuid4().hex}
                           for key, value in state.items()}
                state = {
                    "schema_version": 1, "review_key": os.urandom(32).hex(), "records": records,
                }
                self._write(fd, state)
            if set(state) != {"schema_version", "review_key", "records"}:
                raise ValueError
            if (state["schema_version"] != 1
                    or not re.fullmatch(r"[a-f0-9]{64}", state["review_key"])):
                raise ValueError
            if not isinstance(state["records"], dict) or len(state["records"]) > 5000:
                raise ValueError
            for key, record in state["records"].items():
                self._id(key)
                if not re.fullmatch(r"[a-f0-9]{32}", record["generation"]):
                    raise ValueError
                if "secret" in record:
                    if (set(record) != {"secret", "generation"}
                            or not isinstance(record["secret"], str)
                            or len(record["secret"].encode()) > MAX_SECRET_BYTES):
                        raise ValueError
                elif (set(record) != {"generation", "deleted_at", "deleted_by", "previous_token"}
                      or not isinstance(record["deleted_at"], str)
                      or not isinstance(record["deleted_by"], str)
                      or not isinstance(record["previous_token"], str)):
                    raise ValueError
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise VaultError("Credential storage could not be verified") from exc
        # read_at checked for links; chmod via an independently checked descriptor.
        secure = os.open(self.path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                         | os.O_CLOEXEC, dir_fd=fd)
        try:
            info = os.fstat(secure)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise VaultError("Invalid credential storage file")
            os.fchmod(secure, 0o600)
        finally:
            os.close(secure)
        return state

    @staticmethod
    def _view(state, identifier, fallback):
        record = state["records"].get(identifier, {})
        deleted = "deleted_at" in record
        secret = record.get("secret") if record else fallback
        source = ("deleted" if deleted else "stored" if record
                  else "environment" if secret else "missing")
        bound = json.dumps([identifier, record.get("generation"), source, secret],
                           ensure_ascii=False, separators=(",", ":")).encode()
        token = hmac.new(bytes.fromhex(state["review_key"]), bound, hashlib.sha256).hexdigest()
        return {
            "revision": token, "configured": bool(secret), "source": source,
            "deleted_at": record.get("deleted_at"), "deleted_by": record.get("deleted_by"),
        }, secret

    def describe(self, identifier: str, *, fallback=None) -> dict:
        self._id(identifier)
        with self._transaction() as fd:
            return self._view(self._load(fd), identifier, fallback)[0]

    def capture(self, identifier: str, revision: str, *, fallback=None) -> str:
        self._id(identifier)
        with self._transaction() as fd:
            view, secret = self._view(self._load(fd), identifier, fallback)
            if not hmac.compare_digest(view["revision"], revision):
                raise CredentialChangedError("Credential changed. Refresh and review it again.")
            if not secret:
                raise VaultError("No credential is available for export")
            return secret

    def resolve(self, identifier: str, *, fallback=None) -> str | None:
        return self.resolve_versioned(identifier, fallback=fallback)[1]

    def resolve_versioned(self, identifier: str, *, fallback=None) -> tuple[dict, str | None]:
        """Read a private value and its generation together for adapter cache invalidation."""
        self._id(identifier)
        with self._transaction() as fd:
            return self._view(self._load(fd), identifier, fallback)

    def get(self, identifier: str) -> str | None:
        return self.resolve(identifier)

    def has(self, identifier: str) -> bool:
        return self.get(identifier) is not None

    def was_deleted(self, identifier: str) -> bool:
        return self.describe(identifier)["source"] == "deleted"

    def set(self, identifier: str, secret: str, *, only_if_absent=False,
            expected_revision=None, fallback=None, authorize=lambda: None) -> dict:
        self._id(identifier)
        if not isinstance(secret, str) or len(secret.encode()) > MAX_SECRET_BYTES:
            raise VaultError("Credential exceeds its storage limit")
        with self._transaction() as fd:
            state = self._load(fd)
            view, _ = self._view(state, identifier, fallback)
            if expected_revision is not None and not hmac.compare_digest(
                view["revision"], expected_revision,
            ):
                raise CredentialChangedError("Credential changed. Refresh and review it again.")
            authorize()
            if only_if_absent and identifier in state["records"]:
                return view
            if identifier not in state["records"] and len(state["records"]) >= 5000:
                raise VaultError("Credential storage limit exceeded")
            state["records"][identifier] = {"secret": secret, "generation": uuid.uuid4().hex}
            self._write(fd, state)
            return self._view(state, identifier, fallback)[0]

    def delete(self, identifier: str, *, expected_revision=None, fallback=None,
               deleted_by="local-operator", authorize=lambda: None) -> dict:
        self._id(identifier)
        with self._transaction() as fd:
            state = self._load(fd)
            view, _secret = self._view(state, identifier, fallback)
            previous = state["records"].get(identifier, {})
            authorize()
            if expected_revision is not None and not hmac.compare_digest(
                view["revision"], expected_revision,
            ):
                if previous.get("previous_token") == expected_revision:
                    return view  # Durable acknowledgement after a lost response.
                raise CredentialChangedError("Credential changed. Refresh and review it again.")
            if view["source"] != "deleted":
                if identifier not in state["records"] and len(state["records"]) >= 5000:
                    raise VaultError("Credential storage limit exceeded")
                state["records"][identifier] = {
                    "generation": uuid.uuid4().hex, "deleted_at": utc_now().isoformat(),
                    "deleted_by": deleted_by, "previous_token": view["revision"],
                }
                self._write(fd, state)
            return self._view(state, identifier, fallback)[0]
