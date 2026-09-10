"""Content-addressed copies outside the Agent-writable workspace.

Callers hold the project ledger lock. Publish bytes before publishing the Artifact
record; an interrupted registration can leave an unreferenced copy, never a record
claiming a version whose bytes have not been persisted.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import stat
from pathlib import Path

from .skill_proposal_files import child_directory, directory_fd, read_at, read_relative

SNAPSHOT_DIRECTORY = "artifact-versions"
MAX_ARTIFACT_BYTES = 250_000_000
MAX_ARTIFACT_HISTORY_BYTES = 2_000_000_000
MAX_ARTIFACT_HISTORY_FILES = 10_000


def snapshot_relative(checksum: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{64}", checksum):
        raise ValueError("invalid artifact checksum")
    return f"{SNAPSHOT_DIRECTORY}/{checksum}"


def read_snapshot(project: Path, checksum: str, limit: int = MAX_ARTIFACT_BYTES) -> bytes:
    raw = read_relative(project, snapshot_relative(checksum), min(limit, MAX_ARTIFACT_BYTES))
    if hashlib.sha256(raw).hexdigest() != checksum:
        raise ValueError("artifact version failed its integrity check")
    return raw


def freeze_artifact(project: Path, raw: bytes) -> str:
    if len(raw) > MAX_ARTIFACT_BYTES:
        raise ValueError("artifact exceeds its version size limit")
    checksum = hashlib.sha256(raw).hexdigest()
    with directory_fd(project) as root_fd:
        try:
            os.mkdir(SNAPSHOT_DIRECTORY, mode=0o700, dir_fd=root_fd)
            os.fsync(root_fd)
        except FileExistsError:
            pass
        with child_directory(root_fd, (SNAPSHOT_DIRECTORY,)) as fd:
            try:
                existing = read_at(fd, checksum, MAX_ARTIFACT_BYTES)
            except FileNotFoundError:
                existing = None
            if existing is not None:
                if hashlib.sha256(existing).hexdigest() != checksum:
                    raise ValueError("artifact history is corrupt")
                return checksum
            total = 0
            count = 0
            with os.scandir(fd) as entries:
                for entry in entries:
                    info = entry.stat(follow_symlinks=False)
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                        raise ValueError("artifact history contains an unsafe entry")
                    count += 1
                    total += info.st_size
                    if count >= MAX_ARTIFACT_HISTORY_FILES:
                        raise ValueError("artifact history file limit reached")
                    if total + len(raw) > MAX_ARTIFACT_HISTORY_BYTES:
                        raise ValueError("artifact history byte limit reached")
            if total + len(raw) > MAX_ARTIFACT_HISTORY_BYTES:
                raise ValueError("artifact history byte limit reached")
            temporary = f".staging-{secrets.token_hex(16)}"
            descriptor = os.open(
                temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o400, dir_fd=fd,
            )
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(raw)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, checksum, src_dir_fd=fd, dst_dir_fd=fd)
                os.fsync(fd)
            finally:
                try:
                    os.unlink(temporary, dir_fd=fd)
                except FileNotFoundError:
                    pass
    return checksum
