"""Exact no-follow removal of reviewed, idle Shop browser Unix socket remnants."""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .project_deletion_capture import ProjectDeletionConflict
from .skill_proposal_files import directory_fd


class ShopSocketEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    shop_id: str = Field(pattern=r"^shop_[a-f0-9]{32}$")
    name: str
    kind: Literal["missing", "socket"]
    device: int | None = None
    inode: int | None = None
    changed_ns: int | None = None
    owner: int | None = None


class ShopSocketPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1
    data_root: str
    directory: str
    directory_device: int | None = None
    directory_inode: int | None = None
    entries: list[ShopSocketEntry] = Field(max_length=5000)


def _directory(root: Path) -> Path:
    # Keep this exact formula aligned with ShopBrowserStore.socket_path.
    key = hashlib.sha256(str((root / "shop-browsers").resolve()).encode()).hexdigest()[:12]
    return Path(tempfile.gettempdir()).resolve() / f"ophira-shops-{os.getuid()}-{key}"


@contextmanager
def _private_directory(path):
    with ExitStack() as stack:
        try:
            descriptor = stack.enter_context(directory_fd(path))
        except FileNotFoundError:
            yield None, None
            return
        info = os.fstat(descriptor)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ProjectDeletionConflict("Shop socket directory is not private")
        yield descriptor, info


def _entry(descriptor, shop_id):
    name = shop_id[-16:] + ".sock"
    try:
        if descriptor is None:
            raise FileNotFoundError
        info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return ShopSocketEntry(shop_id=shop_id, name=name, kind="missing")
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
        raise ProjectDeletionConflict("Reviewed Shop endpoint is not an owned Unix socket")
    return ShopSocketEntry(
        shop_id=shop_id,
        name=name,
        kind="socket",
        device=info.st_dev,
        inode=info.st_ino,
        changed_ns=info.st_ctime_ns,
        owner=info.st_uid,
    )


def preview(root: Path, shop_ids: list[str]) -> ShopSocketPlan:
    """Read metadata only; caller owns project exclusive and browser idle leases."""
    try:
        if len(shop_ids) > 5000 or len(set(shop_ids)) != len(shop_ids):
            raise ProjectDeletionConflict("Shop socket inventory is ambiguous or too large")
        # Validate all identities before deriving their truncated filename.
        for shop_id in shop_ids:
            ShopSocketEntry(shop_id=shop_id, name="", kind="missing")
        path = _directory(root)
        if not shop_ids:
            return ShopSocketPlan(data_root=str(root.resolve()), directory=str(path), entries=[])
        with _private_directory(path) as (descriptor, info):
            entries = [_entry(descriptor, shop_id) for shop_id in sorted(shop_ids)]
            if len({entry.name for entry in entries}) != len(entries):
                raise ProjectDeletionConflict("Shop socket filenames are ambiguous")
            return ShopSocketPlan(
                data_root=str(root.resolve()),
                directory=str(path),
                entries=entries,
                directory_device=info.st_dev if info else None,
                directory_inode=info.st_ino if info else None,
            )
    except ProjectDeletionConflict:
        raise
    except (OSError, ValueError) as exc:
        raise ProjectDeletionConflict("Shop socket ownership could not be verified") from exc


def erase(plan: ShopSocketPlan) -> int:
    """Unlink exact reviewed socket inodes; never connect, follow links or delete folders."""
    try:
        if not plan.entries:
            return 0
        path = _directory(Path(plan.data_root))
        if str(path) != plan.directory or len({entry.name for entry in plan.entries}) != len(
            plan.entries
        ):
            raise ProjectDeletionConflict("Shop socket cleanup plan is invalid")
        removed = 0
        with _private_directory(path) as (descriptor, info):
            if info is None:
                return removed
            if plan.directory_inode is not None and (
                plan.directory_device,
                plan.directory_inode,
            ) != (info.st_dev, info.st_ino):
                raise ProjectDeletionConflict("Shop socket directory changed after review")
            for entry in plan.entries:
                if entry.name != entry.shop_id[-16:] + ".sock":
                    raise ProjectDeletionConflict("Shop socket cleanup locator is invalid")
                current = _entry(descriptor, entry.shop_id)
                if current.kind == "missing":
                    continue
                if current != entry:
                    raise ProjectDeletionConflict("Shop socket changed after review")
                os.unlink(entry.name, dir_fd=descriptor)
                os.fsync(descriptor)
                removed += 1
            for entry in plan.entries:
                if _entry(descriptor, entry.shop_id).kind != "missing":
                    raise ProjectDeletionConflict("Shop socket appeared during cleanup")
        return removed
    except ProjectDeletionConflict:
        raise
    except (OSError, ValueError) as exc:
        raise ProjectDeletionConflict("Shop socket cleanup could not complete") from exc
