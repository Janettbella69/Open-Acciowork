"""Bounded descriptor-based capture of untrusted workspace Skill packages."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from .skill_proposal_models import SkillProposalFile, SkillProposalManifest
from .skills import MAX_SKILL_ARCHIVE_ENTRIES, MAX_SKILL_FILE_BYTES, MAX_SKILL_UNPACKED_BYTES

MAX_MANIFEST_BYTES = 20_000


def relative_parts(value: str) -> tuple[str, ...]:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 1_000
        or "\\" in value
        or any(ord(char) < 32 for char in value)
    ):
        raise ValueError("invalid candidate relative path")
    path = PurePosixPath(value)
    if (
        not path.parts
        or path.is_absolute()
        or path.as_posix() != value
        or any(p in {".", ".."} for p in path.parts)
    ):
        raise ValueError("invalid candidate relative path")
    return path.parts


def is_skill_proposal_manifest(value: str) -> bool:
    try:
        parts = relative_parts(value)
    except ValueError:
        return False
    return len(parts) == 3 and parts[0] == "skill-proposals" and parts[2] == "manifest.json"


@contextmanager
def directory_fd(path: Path):
    """Resolve every component without following links, including ancestors."""
    absolute = path.absolute()
    fd = os.open(absolute.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for component in absolute.parts[1:]:
            child = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=fd,
            )
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


@contextmanager
def child_directory(fd: int, parts: tuple[str, ...]):
    current = os.dup(fd)
    try:
        for part in parts:
            child = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=current,
            )
            os.close(current)
            current = child
        yield current
    finally:
        os.close(current)


def _signature(info: os.stat_result) -> tuple[int, ...]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def read_at(fd: int, name: str, limit: int) -> bytes:
    descriptor = os.open(
        name,
        os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
        dir_fd=fd,
    )
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ValueError("candidate files must be regular files without hard links")
        if before.st_size > limit:
            raise ValueError("candidate file exceeds its byte limit")
        chunks = []
        size = 0
        while chunk := os.read(descriptor, min(64 * 1024, limit - size + 1)):
            size += len(chunk)
            if size > limit:
                raise ValueError("candidate file exceeds its byte limit")
            chunks.append(chunk)
        if _signature(before) != _signature(os.fstat(descriptor)):
            raise ValueError("candidate file changed during capture")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def read_relative(root: Path, relative: str, limit: int) -> bytes:
    parts = relative_parts(relative)
    with directory_fd(root) as root_fd, child_directory(root_fd, parts[:-1]) as parent_fd:
        return read_at(parent_fd, parts[-1], limit)


def capture_tree(root: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    entries = 0
    total = 0
    normalized: set[str] = set()

    def walk(fd: int, prefix: str = "") -> None:
        nonlocal entries, total
        with os.scandir(fd) as children:
            names = []
            for child in children:
                if len(names) + entries >= MAX_SKILL_ARCHIVE_ENTRIES:
                    raise ValueError("candidate package exceeds its entry limit")
                names.append(child.name)
        for name in sorted(names):
            relative = f"{prefix}{name}"
            relative_parts(relative)
            entries += 1
            if entries > MAX_SKILL_ARCHIVE_ENTRIES:
                raise ValueError("candidate package exceeds its entry limit")
            if relative.casefold() in normalized:
                raise ValueError("candidate package contains ambiguous duplicate paths")
            normalized.add(relative.casefold())
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                with child_directory(fd, (name,)) as child:
                    walk(child, relative + "/")
            elif stat.S_ISREG(info.st_mode):
                remaining = MAX_SKILL_UNPACKED_BYTES - total
                limit = (
                    min(remaining, MAX_SKILL_FILE_BYTES) if relative == "SKILL.md" else remaining
                )
                content = read_at(fd, name, limit)
                total += len(content)
                files[relative] = content
            else:
                raise ValueError("candidate package contains a link or special file")

    with directory_fd(root) as fd:
        walk(fd)
    if "SKILL.md" not in files:
        raise ValueError("candidate package requires a root SKILL.md")
    return files


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("candidate manifest contains duplicate fields")
        result[key] = value
    return result


def capture_submission(
    workspace: Path,
    manifest_path: str,
    expected_checksum: str,
) -> tuple[bytes, SkillProposalManifest, dict[str, bytes]]:
    if not is_skill_proposal_manifest(manifest_path):
        raise ValueError("proposal requires skill-proposals/<name>/manifest.json")
    raw = read_relative(workspace, manifest_path, MAX_MANIFEST_BYTES)
    if hashlib.sha256(raw).hexdigest() != expected_checksum:
        raise ValueError("registered manifest changed before submission")
    manifest = SkillProposalManifest.model_validate(
        json.loads(raw, object_pairs_hook=_unique_object)
    )
    parts = relative_parts(manifest_path)
    if manifest.name != parts[1]:
        raise ValueError("manifest name must match its proposal directory")
    package = workspace.joinpath(*parts[:-1], "package")
    files = capture_tree(package)
    # The registered manifest and all package bytes must stay stable while the
    # host takes its snapshot. Later workspace edits cannot touch the frozen copy.
    if read_relative(workspace, manifest_path, MAX_MANIFEST_BYTES) != raw:
        raise ValueError("manifest changed during capture")
    if capture_tree(package) != files:
        raise ValueError("candidate package changed during capture")
    return raw, manifest, files


def file_inventory(files: dict[str, bytes]) -> list[SkillProposalFile]:
    rows = []
    for path, content in sorted(files.items()):
        try:
            content.decode("utf-8")
            kind = "binary" if b"\x00" in content else "text"
        except UnicodeDecodeError:
            kind = "binary"
        rows.append(
            SkillProposalFile(
                path=path,
                content_hash=hashlib.sha256(content).hexdigest(),
                size_bytes=len(content),
                preview_kind=kind,
            )
        )
    return rows


def write_frozen(root: Path, raw_manifest: bytes, files: dict[str, bytes]) -> None:
    with (root / "manifest.json").open("xb") as handle:
        handle.write(raw_manifest)
        handle.flush()
        os.fsync(handle.fileno())
    (root / "manifest.json").chmod(0o444)
    write_package(root / "package", files)
    fsync_directory(root)


def write_package(package: Path, files: dict[str, bytes]) -> None:
    """Write one owned, private frozen copy for either human or Agent submission."""
    package.mkdir()
    for relative, content in files.items():
        relative_parts(relative)
        destination = package / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        destination.chmod(0o444)
    for directory in sorted(package.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if directory.is_dir():
            directory.chmod(0o555)
            fsync_directory(directory)
    package.chmod(0o555)
    fsync_directory(package)
    fsync_directory(package.parent)


def fsync_directory(path: Path) -> None:
    with directory_fd(path) as fd:
        os.fsync(fd)
