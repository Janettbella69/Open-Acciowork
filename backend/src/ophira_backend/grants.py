"""Persistent permission grants with scope, expiry, and revoke (FR-13)."""

from __future__ import annotations

import os
import posixpath
import re
import threading
from contextlib import nullcontext
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

from .constants import (
    CLARIFYING_TOOL,
    DESKTOP_TOOL_NAMES,
    PERSISTENT_GRANT_DAYS,
    SHELL_BUILTIN_TOOLS,
    TOOL_ACT_ON_PAGE,
    TOOL_SNAPSHOT_PAGE,
)
from .destructive import is_destructive_delete
from .models import PermissionGrant, utc_now
from .project_lifecycle import ProjectLifecycle

_MAX_PERMISSION_SCOPE_LENGTH = 1_000


class GrantNotFoundError(KeyError):
    pass


def _tool_leaf(tool_name: str) -> str:
    return (tool_name or "").rsplit("__", 1)[-1]


def _url_origin(value: str) -> str:
    if any(ord(character) < 32 for character in value):
        return ""
    try:
        parsed = urlsplit(value.strip())
        if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
            return ""
        if parsed.username is not None or parsed.password is not None:
            return ""
        hostname = parsed.hostname.rstrip(".").casefold()
        if ":" not in hostname:
            hostname = hostname.encode("idna").decode("ascii")
        else:
            hostname = f"[{hostname}]"
        port = parsed.port
    except (UnicodeError, ValueError):
        return ""
    scheme = parsed.scheme.casefold()
    default_port = 443 if scheme == "https" else 80
    port_suffix = f":{port}" if port is not None and port != default_port else ""
    return f"{scheme}://{hostname}{port_suffix}"


def normalized_permission_scope(tool_input: dict | None) -> str:
    """Return a stable, user-displayable scope suitable for repeat grants."""
    payload = tool_input or {}
    url = payload.get("url")
    if isinstance(url, str):
        origin = _url_origin(url)
        if origin and len(origin) <= _MAX_PERMISSION_SCOPE_LENGTH:
            return f"url:{origin}"
    for key in ("file_path", "path"):
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        if "\x00" in value or any(ord(character) < 32 for character in value):
            return ""
        normalized = posixpath.normpath(value.strip().replace("\\", "/"))
        if normalized not in {"", "."} and len(normalized) <= _MAX_PERMISSION_SCOPE_LENGTH:
            return f"path:{normalized}"
    return ""


def permission_scope_covers(granted_scope: str, requested_scope: str) -> bool:
    """Boundary-aware scope matching; URL origins are always exact."""
    if not granted_scope or not requested_scope:
        return False
    if granted_scope.startswith("url:") or requested_scope.startswith("url:"):
        return granted_scope == requested_scope
    if not granted_scope.startswith("path:") or not requested_scope.startswith("path:"):
        return False
    granted_path = granted_scope.removeprefix("path:").rstrip("/") or "/"
    requested_path = requested_scope.removeprefix("path:").rstrip("/") or "/"
    return requested_path == granted_path or requested_path.startswith(f"{granted_path}/")


def is_persistable_permission(tool_name: str, tool_input: dict | None) -> bool:
    """Whether a decision can safely outlive exactly one invocation."""
    if tool_name.startswith("mcp__user_"):
        return False
    leaf = _tool_leaf(tool_name)
    if leaf == CLARIFYING_TOOL or leaf in SHELL_BUILTIN_TOOLS:
        return False
    if is_destructive_delete(tool_name, tool_input):
        return False
    if tool_name in DESKTOP_TOOL_NAMES or leaf in {
        _tool_leaf(item) for item in DESKTOP_TOOL_NAMES
    }:
        return False
    if tool_name == TOOL_ACT_ON_PAGE or leaf == _tool_leaf(TOOL_ACT_ON_PAGE):
        return False
    if (
        (tool_name == TOOL_SNAPSHOT_PAGE or leaf == _tool_leaf(TOOL_SNAPSHOT_PAGE))
        and (tool_input or {}).get("use_connected_browser") is True
    ):
        return False
    return bool(normalized_permission_scope(tool_input))


class PermissionGrantStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    @staticmethod
    def _safe_id(value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", value) or value in {".", ".."}:
            raise ValueError("invalid grant id")
        return value

    def _path(self, grant_id: str) -> Path:
        return self.root / f"{self._safe_id(grant_id)}.json"

    def _write(self, grant: PermissionGrant) -> None:
        with (ProjectLifecycle(self.root.parent).lease(grant.project_id)
              if grant.project_id else nullcontext()):
            path = self._path(grant.id)
            temporary = path.with_suffix(".tmp")
            temporary.write_text(grant.model_dump_json(indent=2) + "\n", encoding="utf-8")
            os.replace(temporary, path)

    def record(
        self,
        *,
        tool_name: str,
        tool_input: dict,
        project_id: str | None,
        days: int = PERSISTENT_GRANT_DAYS,
    ) -> PermissionGrant:
        scope = normalized_permission_scope(tool_input)
        if not is_persistable_permission(tool_name, tool_input) or not scope:
            raise ValueError("permission cannot be persisted without a safe scope")
        grant = PermissionGrant(
            project_id=project_id,
            tool_name=tool_name,
            path_prefix=scope,
            expires_at=utc_now() + timedelta(days=days),
        )
        with self._lock:
            self._write(grant)
        return grant

    def get(self, grant_id: str) -> PermissionGrant:
        path = self._path(grant_id)
        if not path.exists():
            raise GrantNotFoundError(grant_id)
        return PermissionGrant.model_validate_json(path.read_text("utf-8"))

    def list(self, *, include_revoked: bool = False) -> list[PermissionGrant]:
        items: list[PermissionGrant] = []
        for path in self.root.glob("*.json"):
            try:
                items.append(PermissionGrant.model_validate_json(path.read_text("utf-8")))
            except (OSError, ValueError):
                continue
        if not include_revoked:
            items = [item for item in items if not item.revoked]
        return sorted(items, key=lambda item: item.created_at, reverse=True)

    def revoke(self, grant_id: str) -> PermissionGrant:
        with self._lock:
            grant = self.get(grant_id).model_copy(update={"revoked": True})
            self._write(grant)
        return grant

    def match(
        self,
        tool_name: str,
        tool_input: dict,
        *,
        project_id: str | None,
    ) -> PermissionGrant | None:
        if not is_persistable_permission(tool_name, tool_input):
            return None
        now = utc_now()
        scope = normalized_permission_scope(tool_input)
        for grant in self.list():
            if grant.expires_at <= now:
                continue
            if grant.tool_name != tool_name:
                continue
            if grant.project_id is not None and grant.project_id != project_id:
                continue
            if not permission_scope_covers(grant.path_prefix, scope):
                continue
            return grant
        return None
