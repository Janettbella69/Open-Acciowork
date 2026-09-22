"""Durable, bounded file erasure transactions for host-owned privacy operations.

Scope selection and human authorization belong to the calling service. This
module never infers ownership from file names and exposes no HTTP or model tool.
All cooperating readers/writers must hold operation_lease before online use.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
import unicodedata
from contextlib import contextmanager
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import Field, model_serializer, model_validator

from .backup_files import parse_json
from .maintenance import data_directory_lease
from .models import APIModel, utc_now
from .sdk_session_erasure import NativeSessionTarget, erase_native_sessions
from .skill_proposal_files import child_directory, directory_fd, read_at, relative_parts

JOURNAL = ".data-erasure"
OPERATION_LOCK = ".data-operation.lock"
MAX_ERASURE_FILES = 20_000
MAX_SOURCE_BYTES = 250_000_000
MAX_REPLACEMENT_BYTES = 16 * 1024 * 1024
MAX_JOURNAL_BYTES = 16 * 1024 * 1024
MAX_REPLACEMENTS_TOTAL = 64 * 1024 * 1024
MAX_SOURCES_TOTAL = 2 * 1024 * 1024 * 1024


class ErasureError(ValueError):
    """Stable errors containing no source contents or private file paths."""


class DataOperationBusy(ErasureError):
    pass


def encode(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _path(value):
    parts = relative_parts(value)
    if len(parts) > 32 or parts[0].casefold() in {
        JOURNAL,
        OPERATION_LOCK,
        ".application-maintenance.lock",
    }:
        raise ErasureError("Erasure contains an invalid target path.")
    return parts


@contextmanager
def operation_lease(root: Path, *, exclusive=False, allow_pending=False):
    """Nonblocking cross-process gate, separate from the app's cold-backup lease.

    Keep the descriptor alive through the entire operation, including async I/O.
    A pending committed erasure blocks ordinary access until recovery finishes.
    """
    with directory_fd(root) as parent:
        descriptor = os.open(
            OPERATION_LOCK,
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=parent,
        )
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ErasureError("Data operation lock is not a private regular file.")
            try:
                fcntl.flock(
                    descriptor, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB
                )
            except BlockingIOError as exc:
                raise DataOperationBusy("Data is in use. Retry the deletion later.") from exc
            if not allow_pending:
                # lstat deliberately treats a substituted link as pending too.
                try:
                    with child_directory(parent, (JOURNAL,)) as journal:
                        os.stat("pending", dir_fd=journal, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    raise DataOperationBusy("A committed deletion must finish before data access.")
            yield
        finally:
            os.close(descriptor)


class ErasureFile(APIModel):
    path: str
    before_size: int | None = Field(default=None, ge=0, le=MAX_SOURCE_BYTES)
    before_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    # None means unlink; otherwise this file is replaced by retained data only.
    after_size: int | None = Field(default=None, ge=0, le=MAX_REPLACEMENT_BYTES)
    after_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def valid(self):
        _path(self.path)
        if (self.before_size is None) != (self.before_sha256 is None):
            raise ValueError("incomplete source identity")
        if self.before_sha256 is None and self.after_sha256 is None:
            raise ValueError("erasure operation has neither source nor retained content")
        if (self.after_size is None) != (self.after_sha256 is None):
            raise ValueError("incomplete replacement")
        return self


class ErasurePlan(APIModel):
    schema_version: Literal[1, 2] = 1
    # Caller-created content-free scope/identity, validated by its domain model.
    scope: dict
    files: list[ErasureFile] = Field(max_length=MAX_ERASURE_FILES)
    directories: list[str] = Field(max_length=MAX_ERASURE_FILES)
    native_sessions: list[NativeSessionTarget] = Field(default_factory=list, max_length=1000)

    @model_serializer(mode="wrap")
    def serialized(self, handler):
        value = handler(self)
        if self.schema_version == 1:
            value.pop("native_sessions")
        return value

    @model_validator(mode="after")
    def valid(self):
        if self.schema_version == 1 and (self.native_sessions or any(
            item.before_sha256 is None for item in self.files
        )):
            raise ValueError("native operations and new files require erasure schema 2")
        seen: dict[str, tuple[str, bool]] = {}
        leaves = set()
        for path, directory in [
            *((item.path, False) for item in self.files),
            *((path, True) for path in self.directories),
        ]:
            parts = _path(path)
            if path in leaves:
                raise ValueError("duplicate erasure target")
            leaves.add(path)
            for index in range(1, len(parts) + 1):
                prefix = "/".join(parts[:index])
                key = unicodedata.normalize("NFC", prefix).casefold()
                value = prefix, directory or index < len(parts)
                if key in seen and seen[key] != value:
                    raise ValueError("ambiguous erasure target")
                seen[key] = value
        if sum(item.after_size or 0 for item in self.files) > MAX_REPLACEMENTS_TOTAL:
            raise ValueError("replacement byte limit exceeded")
        if sum(item.before_size or 0 for item in self.files) > MAX_SOURCES_TOTAL:
            raise ValueError("source byte limit exceeded")
        if any(
            item.after_sha256 is not None
            and any(item.path.startswith(directory + "/") for directory in self.directories)
            for item in self.files
        ):
            raise ValueError("retained files cannot be inside removed directories")
        if len(encode(self.model_dump(mode="json"))) > MAX_JOURNAL_BYTES:
            raise ValueError("erasure journal byte limit exceeded")
        return self

    def fingerprint(self):
        return digest(encode(self.model_dump(mode="json")))


def _read(root, path, limit, *, missing=False):
    parts = relative_parts(path)
    try:
        with directory_fd(root) as root_fd, child_directory(root_fd, parts[:-1]) as parent:
            return read_at(parent, parts[-1], limit)
    except FileNotFoundError:
        if missing:
            return None
        raise


def _matches(raw, size, sha):
    if size is None and sha is None:
        return raw is None
    return raw is not None and len(raw) == size and digest(raw) == sha


def _write_at(parent, name, raw, *, temporary=None):
    """Publish an atomic private replacement, including directory durability."""
    recoverable = temporary is not None
    temporary = temporary or f".erasure-{uuid4().hex}.tmp"
    if recoverable:
        try:
            partial = read_at(parent, temporary, len(raw))
        except FileNotFoundError:
            pass
        else:
            if not raw.startswith(partial):
                raise ErasureError("Committed replacement staging changed.")
            os.unlink(temporary, dir_fd=parent)
    fd = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent
    )
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
        os.fsync(parent)
    finally:
        try:
            os.unlink(temporary, dir_fd=parent)
        except FileNotFoundError:
            pass


class FileErasure:
    """A single committed transaction; recovery never copies deleted bytes back."""

    def __init__(self, root):
        self.root = Path(root).resolve()

    def _journal_fd(self, parent):
        try:
            os.mkdir(JOURNAL, mode=0o700, dir_fd=parent)
            os.fsync(parent)
        except FileExistsError:
            pass
        return child_directory(parent, (JOURNAL,))

    def _verify_sources(self, plan):
        for item in plan.files:
            raw = _read(self.root, item.path, MAX_SOURCE_BYTES, missing=True)
            if not _matches(raw, item.before_size, item.before_sha256):
                raise ErasureError("Data changed. Review the deletion again.")
            with directory_fd((self.root / item.path).parent):
                pass
        # Refuse nonempty unlisted directories before committing any deletion.
        expected = {item.path for item in plan.files} | set(plan.directories)
        for path in plan.directories:
            with directory_fd(self.root / path) as parent, os.scandir(parent) as entries:
                if any(f"{path}/{entry.name}" not in expected for entry in entries):
                    raise ErasureError("Deletion directory contains unreviewed data.")

    def _cleanup_orphans(self, journal):
        # No active transaction can be staging while the exclusive gate is held.
        # Finished directories are renamed only after the durable receipt exists.
        with os.scandir(journal) as entries:
            names = [entry.name for entry in entries]
        if len(names) > MAX_ERASURE_FILES:
            raise ErasureError("Erasure journal entry limit exceeded.")
        for name in names:
            if name.startswith(("staging-erase_", "finished-erase_")):
                with child_directory(journal, (name,)) as orphan:
                    with os.scandir(orphan) as entries:
                        children = [entry.name for entry in entries]
                    if len(children) > MAX_ERASURE_FILES + 2:
                        raise ErasureError("Erasure staging entry limit exceeded.")
                    for child in children:
                        info = os.stat(child, dir_fd=orphan, follow_symlinks=False)
                        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                            raise ErasureError("Erasure staging contains an unsafe entry.")
                    for child in children:
                        os.unlink(child, dir_fd=orphan)
                    os.fsync(orphan)
                os.rmdir(name, dir_fd=journal)
            elif name.startswith(".erasure-") and name.endswith(".tmp"):
                info = os.stat(name, dir_fd=journal, follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ErasureError("Erasure staging contains an unsafe entry.")
                os.unlink(name, dir_fd=journal)
        os.fsync(journal)

    def commit(self, plan: ErasurePlan, replacements: dict[str, bytes], *, authorize):
        """Recheck caller authority and sources before the irreversible commit point.

        Returns a receipt after cleanup, or leaves an authoritative pending journal
        on failure. Call recover after a committed failure; do not submit a new plan.
        """
        return self.commit_prepared(lambda: (plan, replacements), authorize=authorize)

    def commit_prepared(self, prepare, *, authorize):
        """Select and materialize the reviewed scope under the exclusive gate."""
        with data_directory_lease(self.root), operation_lease(self.root, exclusive=True):
            authorize()
            plan, replacements = prepare()
            return self._commit_locked(plan, replacements, authorize=authorize)

    def _commit_locked(self, plan, replacements, *, authorize):
        plan = ErasurePlan.model_validate(plan.model_dump())
        replacement_paths = {item.path for item in plan.files if item.after_sha256 is not None}
        if set(replacements) != replacement_paths:
            raise ErasureError("Erasure replacement selection is invalid.")
        for item in plan.files:
            if item.after_sha256 is not None and not _matches(
                replacements[item.path],
                item.after_size,
                item.after_sha256,
            ):
                raise ErasureError("Erasure replacement could not be verified.")
        self._verify_sources(plan)
        identifier = f"erase_{uuid4().hex}"
        record = {
            "id": identifier,
            "created_at": utc_now().isoformat(),
            "fingerprint": plan.fingerprint(),
            "plan": plan.model_dump(mode="json"),
        }
        record_bytes = encode(record)
        if len(record_bytes) > MAX_JOURNAL_BYTES:
            raise ErasureError("Erasure journal exceeds its byte limit.")
        with directory_fd(self.root) as root_fd, self._journal_fd(root_fd) as journal:
            self._cleanup_orphans(journal)
            stage = f"staging-{identifier}"
            os.mkdir(stage, mode=0o700, dir_fd=journal)
            with child_directory(journal, (stage,)) as staged:
                _write_at(staged, "record.json", record_bytes)
                for index, item in enumerate(plan.files):
                    if item.after_sha256 is not None:
                        _write_at(staged, f"{index}.bin", replacements[item.path])
            # Authorization and all source checks precede publication. The
            # committed journal is the authority after an interrupted response.
            authorize()
            self._verify_sources(plan)
            os.rename(stage, "pending", src_dir_fd=journal, dst_dir_fd=journal)
            os.fsync(journal)
        return self._recover_locked()

    def recover(self):
        """Complete a previously authorized commit before reopening data access."""
        with (
            data_directory_lease(self.root),
            operation_lease(self.root, exclusive=True, allow_pending=True),
        ):
            return self._recover_locked()

    def _apply_file(self, pending, identifier, index, item):
        parts = _path(item.path)
        try:
            with directory_fd(self.root) as root_fd, child_directory(root_fd, parts[:-1]) as parent:
                try:
                    raw = read_at(parent, parts[-1], MAX_SOURCE_BYTES)
                except FileNotFoundError:
                    raw = None
                if item.after_sha256 is None:
                    if raw is None:
                        return
                    if not _matches(raw, item.before_size, item.before_sha256):
                        raise ErasureError("Committed deletion encountered changed data.")
                    os.unlink(parts[-1], dir_fd=parent)
                    os.fsync(parent)
                else:
                    if _matches(raw, item.after_size, item.after_sha256):
                        return
                    if not _matches(raw, item.before_size, item.before_sha256):
                        raise ErasureError("Committed deletion encountered changed data.")
                    retained = read_at(pending, f"{index}.bin", MAX_REPLACEMENT_BYTES)
                    _write_at(
                        parent, parts[-1], retained, temporary=f".erasure-{identifier}-{index}.tmp"
                    )
        except FileNotFoundError:
            if item.after_sha256 is not None:
                raise ErasureError("Committed replacement target is missing.") from None

    def _recover_locked(self):
        raw = _read(self.root, f"{JOURNAL}/pending/record.json", MAX_JOURNAL_BYTES, missing=True)
        if raw is None:
            # A corrupt pending directory must not silently unlock the data.
            with directory_fd(self.root) as root_fd:
                try:
                    with child_directory(root_fd, (JOURNAL,)) as journal:
                        try:
                            os.stat("pending", dir_fd=journal, follow_symlinks=False)
                        except FileNotFoundError:
                            self._cleanup_orphans(journal)
                            return None
                except FileNotFoundError:
                    return None
            raise ErasureError("Committed deletion journal is incomplete.")
        record = parse_json(raw)
        if not isinstance(record, dict) or set(record) != {
            "id",
            "created_at",
            "fingerprint",
            "plan",
        }:
            raise ErasureError("Committed deletion journal is invalid.")
        identifier = record["id"]
        if (
            not isinstance(identifier, str)
            or not identifier.startswith("erase_")
            or len(identifier) != 38
            or any(c not in "0123456789abcdef" for c in identifier[6:])
        ):
            raise ErasureError("Committed deletion identity is invalid.")
        plan = ErasurePlan.model_validate(record["plan"])
        if plan.fingerprint() != record["fingerprint"]:
            raise ErasureError("Committed deletion fingerprint is invalid.")
        with directory_fd(self.root) as root_fd, self._journal_fd(root_fd) as journal:
            with child_directory(journal, ("pending",)) as pending:
                expected = {"record.json"}
                for index, item in enumerate(plan.files):
                    if item.after_sha256 is not None:
                        expected.add(f"{index}.bin")
                        if not _matches(
                            read_at(pending, f"{index}.bin", MAX_REPLACEMENT_BYTES),
                            item.after_size,
                            item.after_sha256,
                        ):
                            raise ErasureError("Committed replacement could not be verified.")
                with os.scandir(pending) as entries:
                    if {entry.name for entry in entries} != expected:
                        raise ErasureError("Committed deletion journal contains unexpected files.")
                erase_native_sessions(self.root, plan.native_sessions, plan.files, plan.directories)
                for index, item in enumerate(plan.files):
                    self._apply_file(pending, identifier, index, item)
                for path in sorted(plan.directories, key=lambda p: (-p.count("/"), p)):
                    parts = _path(path)
                    try:
                        with child_directory(root_fd, parts[:-1]) as parent:
                            os.rmdir(parts[-1], dir_fd=parent)
                            os.fsync(parent)
                    except FileNotFoundError:
                        pass
                receipt = {key: value for key, value in record.items() if key != "plan"}
                receipt.update(scope=plan.scope, files=len(plan.files), state="completed")
                # Content-free receipt precedes journal cleanup, so a lost HTTP
                # response never makes an already completed erasure unknowable.
                _write_at(journal, f"{identifier}.json", encode(receipt))
            # Preserve the complete recovery journal until this atomic rename.
            # A crash during cleanup cannot strand a half-removed pending record.
            os.rename("pending", f"finished-{identifier}", src_dir_fd=journal, dst_dir_fd=journal)
            os.fsync(journal)
            self._cleanup_orphans(journal)
            return receipt
