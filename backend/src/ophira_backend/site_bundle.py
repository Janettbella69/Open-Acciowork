"""Bounded ZIP format for reviewed, immutable page assets."""

from __future__ import annotations

import io
import re
import stat
import zipfile

MAX_BUNDLE_BYTES = 12 * 1024 * 1024
MAX_EXPANDED_BYTES = 20 * 1024 * 1024
MAX_HTML_BYTES = 512 * 1024
MAX_ASSET_BYTES = 4 * 1024 * 1024
MAX_ASSETS = 32

_ASSET_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
ASSET_MEDIA_TYPES = {
    ".css": "text/css",
    ".js": "text/javascript",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
}


class InvalidSiteBundle(ValueError):
    pass


def asset_media_type(name: str) -> str | None:
    if not _ASSET_NAME.fullmatch(name):
        return None
    extension = "." + name.rsplit(".", 1)[-1].lower()
    return ASSET_MEDIA_TYPES.get(extension)


def _validate_asset(name: str, raw: bytes) -> None:
    media_type = asset_media_type(name)
    if media_type is None or not raw:
        raise InvalidSiteBundle("Site bundle has an unsupported asset")
    if media_type in {"text/css", "text/javascript"}:
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise InvalidSiteBundle("Site CSS and JavaScript must be UTF-8") from exc
        if b"\x00" in raw:
            raise InvalidSiteBundle("Site CSS or JavaScript contains NUL")
        return
    signatures = {
        "image/png": raw.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/jpeg": raw.startswith(b"\xff\xd8\xff"),
        "image/webp": raw.startswith(b"RIFF") and raw[8:12] == b"WEBP",
        "image/gif": raw.startswith((b"GIF87a", b"GIF89a")),
        "image/x-icon": raw.startswith(b"\x00\x00\x01\x00"),
        "font/woff": raw.startswith(b"wOFF"),
        "font/woff2": raw.startswith(b"wOF2"),
        "video/mp4": len(raw) >= 12 and raw[4:8] == b"ftyp",
        "video/webm": raw.startswith(b"\x1aE\xdf\xa3"),
    }
    if not signatures[media_type]:
        raise InvalidSiteBundle("Site asset does not match its file type")


def read_site_bundle(payload: bytes) -> dict[str, bytes]:
    if not payload or len(payload) > MAX_BUNDLE_BYTES:
        raise InvalidSiteBundle("Site bundle exceeds its compressed byte limit")
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
    except zipfile.BadZipFile as exc:
        raise InvalidSiteBundle("Site bundle is not a ZIP archive") from exc
    with archive:
        entries = archive.infolist()
        if len(entries) > MAX_ASSETS + 2:
            raise InvalidSiteBundle("Site bundle has too many entries")
        if sum(item.file_size for item in entries) > MAX_EXPANDED_BYTES:
            raise InvalidSiteBundle("Site bundle expands beyond its byte limit")
        files: dict[str, bytes] = {}
        seen: set[str] = set()
        total = 0
        for item in entries:
            name = item.filename
            folded = name.casefold()
            mode = stat.S_IFMT(item.external_attr >> 16)
            if item.is_dir() and name == "assets/":
                if folded in seen or (mode and not stat.S_ISDIR(mode)):
                    raise InvalidSiteBundle("Site bundle has an unsafe or duplicate entry")
                seen.add(folded)
                continue
            if name != "index.html":
                if not name.startswith("assets/") or asset_media_type(name[7:]) is None:
                    raise InvalidSiteBundle("Site bundle has an unsupported path")
            if (
                folded in seen or item.is_dir() or item.flag_bits & 1
                or item.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
                or (mode and not stat.S_ISREG(mode))
            ):
                raise InvalidSiteBundle("Site bundle has an unsafe or duplicate entry")
            seen.add(folded)
            limit = MAX_HTML_BYTES if name == "index.html" else MAX_ASSET_BYTES
            if item.file_size < 1 or item.file_size > limit:
                raise InvalidSiteBundle("Site bundle entry exceeds its byte limit")
            try:
                with archive.open(item) as source:
                    raw = source.read(limit + 1)
            except (zipfile.BadZipFile, RuntimeError, NotImplementedError, OSError) as exc:
                raise InvalidSiteBundle("Site bundle entry could not be read") from exc
            total += len(raw)
            if len(raw) != item.file_size or total > MAX_EXPANDED_BYTES:
                raise InvalidSiteBundle("Site bundle expanded size is invalid")
            if name == "index.html":
                try:
                    raw.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise InvalidSiteBundle("Site HTML must be UTF-8") from exc
                if b"\x00" in raw:
                    raise InvalidSiteBundle("Site HTML contains NUL")
            else:
                _validate_asset(name[7:], raw)
            files[name] = raw
        if "index.html" not in files or len(files) - 1 > MAX_ASSETS:
            raise InvalidSiteBundle("Site bundle needs index.html and at most 32 assets")
        return files


def read_site_part(payload: bytes, path: str) -> bytes:
    """Read one part of a ZIP that was fully validated when its version was staged."""
    if not payload or len(payload) > MAX_BUNDLE_BYTES:
        raise InvalidSiteBundle("Site bundle exceeds its compressed byte limit")
    if path != "index.html" and (
        not path.startswith("assets/") or asset_media_type(path[7:]) is None
    ):
        raise KeyError(path)
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
    except zipfile.BadZipFile as exc:
        raise InvalidSiteBundle("Site bundle is not a ZIP archive") from exc
    with archive:
        try:
            item = archive.getinfo(path)
        except KeyError:
            raise KeyError(path) from None
        limit = MAX_HTML_BYTES if path == "index.html" else MAX_ASSET_BYTES
        if item.file_size < 1 or item.file_size > limit:
            raise InvalidSiteBundle("Site bundle entry exceeds its byte limit")
        try:
            with archive.open(item) as source:
                raw = source.read(limit + 1)
        except (zipfile.BadZipFile, RuntimeError, NotImplementedError, OSError) as exc:
            raise InvalidSiteBundle("Site bundle entry could not be read") from exc
        if len(raw) != item.file_size:
            raise InvalidSiteBundle("Site bundle entry has an invalid size")
        if path == "index.html":
            try:
                raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise InvalidSiteBundle("Site HTML must be UTF-8") from exc
            if b"\x00" in raw:
                raise InvalidSiteBundle("Site HTML contains NUL")
        else:
            _validate_asset(path[7:], raw)
        return raw
