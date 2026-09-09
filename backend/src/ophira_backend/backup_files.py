"""Shared no-replace publication for local backup and recovery tools."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import shutil
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

from .skill_proposal_files import child_directory, directory_fd, read_at, relative_parts


@dataclass(frozen=True)
class BackupLimits:
    files: int = 50_000
    entries: int = 100_000
    file_bytes: int = 64 * 1024 * 1024
    total_bytes: int = 2 * 1024 * 1024 * 1024
    manifest_bytes: int = 32 * 1024 * 1024


class BackupError(ValueError):
    """Stable failure reasons without arbitrary file contents or credentials."""


def parse_json(data: bytes):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise BackupError("duplicate_json_key")
            value[key] = item
        return value

    return json.loads(data, object_pairs_hook=unique)


def private_json(path: Path, value: object) -> None:
    data = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())


def tree_inventory(
    root: Path,
    limits: BackupLimits,
    *,
    omit_root: frozenset[str] = frozenset(),
) -> dict:
    files, directories, seen = [], [], set()
    total = 0

    def walk(fd: int, prefix: str = "", depth: int = 0):
        nonlocal total
        if depth > 32:
            raise BackupError("path_depth_limit")
        names = []
        with os.scandir(fd) as children:
            for entry in children:
                if not prefix and entry.name in omit_root:
                    continue
                if len(names) + len(seen) >= limits.entries:
                    raise BackupError("entry_count_limit")
                names.append(entry.name)
        for name in sorted(names):
            relative = prefix + name
            relative_parts(relative)
            if relative.casefold() in seen:
                raise BackupError("ambiguous_path")
            seen.add(relative.casefold())
            if len(seen) > limits.entries:
                raise BackupError("entry_count_limit")
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            mode = stat.S_IMODE(info.st_mode) & 0o700
            if stat.S_ISDIR(info.st_mode):
                directories.append({"path": relative, "mode": mode})
                with child_directory(fd, (name,)) as child:
                    walk(child, relative + "/", depth + 1)
            elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                if len(files) >= limits.files:
                    raise BackupError("file_count_limit")
                data = read_at(fd, name, min(limits.file_bytes, limits.total_bytes - total))
                total += len(data)
                files.append(
                    {
                        "path": relative,
                        "size": len(data),
                        "mode": mode,
                        "sha256": hashlib.sha256(data).hexdigest(),
                    }
                )
            else:
                raise BackupError("linked_or_special_file")

    with directory_fd(root) as descriptor:
        walk(descriptor)
    return {
        "files": sorted(files, key=lambda row: row["path"]),
        "directories": sorted(directories, key=lambda row: row["path"]),
    }


def copy_inventory(source: Path, target: Path, inventory: dict, limits: BackupLimits) -> None:
    """Copy into a task-owned empty staging tree, preserving owner-only modes."""
    target.mkdir(mode=0o700)
    with directory_fd(source) as source_fd:
        for row in inventory["directories"]:
            (target / row["path"]).mkdir(mode=0o700, parents=True, exist_ok=True)
        for row in inventory["files"]:
            parts = relative_parts(row["path"])
            with child_directory(source_fd, parts[:-1]) as parent:
                data = read_at(parent, parts[-1], limits.file_bytes)
            if len(data) != row["size"] or hashlib.sha256(data).hexdigest() != row["sha256"]:
                raise BackupError("source_changed")
            destination = target / row["path"]
            descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
                os.fchmod(output.fileno(), row["mode"])
    # Seal children before their read-only parent. Empty directories are retained.
    for row in reversed(inventory["directories"]):
        (target / row["path"]).chmod(row["mode"])


def remove_staging(root: Path) -> None:
    """Remove only a private staging tree owned by the current operation."""
    for directory, _children, _files in os.walk(root, followlinks=False):
        Path(directory).chmod(0o700)
    shutil.rmtree(root)


def sync_directories(root: Path) -> None:
    for directory, _children, _files in os.walk(root, topdown=False):
        with directory_fd(Path(directory)) as descriptor:
            os.fsync(descriptor)


def publish_exclusive(source: Path, target: Path) -> None:
    """Use native atomic no-replace rename; never replace a concurrent target."""
    library = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        rename = library.renamex_np
        rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        arguments = (os.fsencode(source), os.fsencode(target), 4)  # RENAME_EXCL
    elif sys.platform.startswith("linux") and hasattr(library, "renameat2"):
        rename = library.renameat2
        rename.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        arguments = (-100, os.fsencode(source), -100, os.fsencode(target), 1)  # NOREPLACE
    else:
        raise BackupError("atomic_publish_unsupported")
    rename.restype = ctypes.c_int
    if rename(*arguments) != 0:
        error = ctypes.get_errno()
        if error in {errno.EEXIST, errno.ENOTEMPTY}:
            raise BackupError("target_exists")
        raise OSError(error, os.strerror(error))
