"""Bounded, non-following ZIP export of closed Chromium shop profiles."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import zipfile
from pathlib import Path

from .shop_browser_store import ShopBrowserError
from .skill_proposal_files import child_directory, directory_fd, relative_parts

MAX_FILES = 20_000
MAX_ENTRIES = 30_000
MAX_DEPTH = 32
MAX_FILE_BYTES = 256 * 1024 * 1024
MAX_TOTAL_BYTES = 512 * 1024 * 1024
_EPHEMERAL_ROOT_FILES = frozenset({"SingletonCookie", "SingletonLock", "SingletonSocket"})


def _changed(before: os.stat_result, after: os.stat_result) -> bool:
    return (
        before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns,
        before.st_ctime_ns, before.st_nlink,
    ) != (
        after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
        after.st_ctime_ns, after.st_nlink,
    )


def build_login_archive(
    directory: Path, profiles: list[Path], *, project_id: str, shop_id: str,
    revision: int,
):
    if not profiles:
        raise ShopBrowserError(
            "SHOP_PROFILE_MISSING", "This shop has no saved browser profile to export."
        )
    output = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b")
    files, entries, total = 0, 0, 0
    normalized = set()

    def walk(source: int, prefix: str, depth: int, archive: zipfile.ZipFile) -> None:
        nonlocal files, entries, total
        if depth > MAX_DEPTH:
            raise ShopBrowserError(
                "SHOP_PROFILE_TOO_LARGE", "Browser profile is too deep to export."
            )
        with os.scandir(source) as children:
            names = []
            for child in children:
                if entries + len(names) >= MAX_ENTRIES:
                    raise ShopBrowserError(
                        "SHOP_PROFILE_TOO_LARGE", "Browser profile has too many entries."
                    )
                names.append(child.name)
        names.sort()
        for name in names:
            if depth == 1 and name in _EPHEMERAL_ROOT_FILES:
                continue
            relative = prefix + name
            try:
                relative_parts(relative)
            except ValueError:
                raise ShopBrowserError(
                    "SHOP_PROFILE_INVALID", "Browser profile contains an invalid path."
                ) from None
            entries += 1
            if entries > MAX_ENTRIES or relative.casefold() in normalized:
                raise ShopBrowserError(
                    "SHOP_PROFILE_TOO_LARGE", "Browser profile has too many or ambiguous paths."
                )
            normalized.add(relative.casefold())
            info = os.stat(name, dir_fd=source, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                archive.writestr(relative + "/", b"")
                with child_directory(source, (name,)) as child:
                    walk(child, relative + "/", depth + 1, archive)
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ShopBrowserError(
                    "SHOP_PROFILE_INVALID", "Browser profile contains a link or special file."
                )
            if info.st_size > MAX_FILE_BYTES or total + info.st_size > MAX_TOTAL_BYTES:
                raise ShopBrowserError(
                    "SHOP_PROFILE_TOO_LARGE", "Browser profile exceeds its export byte limit."
                )
            descriptor = os.open(
                name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                dir_fd=source,
            )
            try:
                before = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                    or (info.st_dev, info.st_ino) != (before.st_dev, before.st_ino)
                ):
                    raise ShopBrowserError(
                        "SHOP_PROFILE_CHANGED", "Browser profile changed during export."
                    )
                size = 0
                with archive.open(relative, "w", force_zip64=True) as target:
                    while chunk := os.read(descriptor, 64 * 1024):
                        size += len(chunk)
                        if size > MAX_FILE_BYTES or total + size > MAX_TOTAL_BYTES:
                            raise ShopBrowserError(
                                "SHOP_PROFILE_TOO_LARGE",
                                "Browser profile exceeds its export byte limit.",
                            )
                        target.write(chunk)
                if size != before.st_size or _changed(before, os.fstat(descriptor)):
                    raise ShopBrowserError(
                        "SHOP_PROFILE_CHANGED", "Browser profile changed during export."
                    )
                files += 1
                if files > MAX_FILES:
                    raise ShopBrowserError(
                        "SHOP_PROFILE_TOO_LARGE", "Browser profile has too many files."
                    )
                total += size
            finally:
                os.close(descriptor)

    try:
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED, compresslevel=3) as archive:
            archive.writestr("browser-profile-manifest.json", json.dumps({
                "schema_version": 1, "project_id": project_id, "shop_id": shop_id,
                "shop_revision": revision,
                "profiles": [profile.name for profile in profiles],
                "excluded": sorted(_EPHEMERAL_ROOT_FILES),
            }, sort_keys=True))
            with directory_fd(directory) as root:
                for profile in profiles:
                    relative_parts(profile.name)
                    with child_directory(root, (profile.name,)) as source:
                        archive.writestr(profile.name + "/", b"")
                        walk(source, profile.name + "/", 1, archive)
        if files == 0:
            raise ShopBrowserError(
                "SHOP_PROFILE_MISSING", "This shop has no saved browser files to export."
            )
        output.seek(0)
        return output
    except BaseException:
        output.close()
        raise
