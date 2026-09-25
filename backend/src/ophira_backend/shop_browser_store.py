"""Project-owned shop identities and private browser session metadata."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .models import PrincipalContext, new_id, utc_now
from .storage import FileLedger

SHOP_ID = r"^shop_[a-f0-9]{32}$"
SESSION_ID = r"^shop_session_[a-f0-9]{32}$"
MAX_SHOPS = 100
MAX_SHOP_FILE_BYTES = 20 * 1024 * 1024
MAX_SHOP_RPC_BYTES = 128 * 1024


class ShopBrowserError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int = 409):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class ShopIdentityInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(min_length=1, max_length=160)
    platform: Literal["shein", "temu", "tiktok_shop", "aliexpress", "shopee", "shopify", "other"]
    seller_shop_id: str = Field(min_length=1, max_length=160)
    connector_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,160}$")
    entry_url: str = Field(min_length=8, max_length=2000)
    additional_domains: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("additional_domains")
    @classmethod
    def domain_names(cls, values):
        if any(
            not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?", item)
            or "." not in item
            or ".." in item
            for item in values
        ):
            raise ValueError("Enter plain lowercase domain names")
        return sorted(set(values))

    @model_validator(mode="after")
    def public_entry(self):
        parts = urlsplit(self.entry_url)
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.port not in {None, 443}
            or parts.fragment
            or parts.query
        ):
            raise ValueError(
                "Shop entry must be an HTTPS URL without credentials, query or fragment"
            )
        return self

    @property
    def domains(self) -> list[str]:
        return sorted({urlsplit(self.entry_url).hostname, *self.additional_domains})


class ShopIdentity(ShopIdentityInput):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(default_factory=lambda: new_id("shop"), pattern=SHOP_ID)
    project_id: str
    tenant_id: str | None
    identity_binding: Literal["local_only", "authenticated_principal"]
    revision: int = Field(default=1, ge=1)
    profile_generation: int = Field(default=1, ge=1)
    created_by: str
    created_at: datetime = Field(default_factory=utc_now)
    authorized_session_id: str | None = Field(default=None, pattern=SESSION_ID)
    grant_expires_at: datetime | None = None
    granted_by: str | None = None
    granted_at: datetime | None = None
    revoked_at: datetime | None = None
    revoked_by: str | None = None

    @model_validator(mode="after")
    def coherent_times(self):
        for instant in (
            self.created_at,
            self.granted_at,
            self.grant_expires_at,
            self.revoked_at,
        ):
            if instant is not None and (instant.tzinfo is None or instant.utcoffset() is None):
                raise ValueError("Shop timestamps require a timezone")
        if self.grant_expires_at and (
            not self.granted_at or self.grant_expires_at <= self.granted_at
        ):
            raise ValueError("Shop grant expiry must follow confirmation")
        return self

    def authorized(self, session_id: str) -> bool:
        return bool(
            self.authorized_session_id == session_id
            and self.grant_expires_at
            and self.grant_expires_at > utc_now()
            and self.revoked_at is None
        )


class ShopRevisionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_revision: int = Field(ge=1)


class ShopLoginDeleteInput(ShopRevisionInput):
    confirmation: Literal["delete"]


class ShopLoginExportInput(ShopRevisionInput):
    confirmation: Literal["export"]


class ShopGrantInput(ShopRevisionInput):
    session_id: str = Field(pattern=SESSION_ID)
    confirmed_identity: Literal[True]
    expires_in_seconds: int = Field(default=7 * 86400, ge=60, le=30 * 86400)


class ShopUpdateInput(ShopIdentityInput, ShopRevisionInput):
    pass


class ShopBrowserCaller(BaseModel):
    """Private server-bound context, never part of model tool arguments."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    project_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,160}$")
    shop_revision: int = Field(ge=1)
    run_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,160}$")
    sdk_session_id: str | None = Field(default=None, min_length=1, max_length=2000)
    principal: PrincipalContext | None = None

    @model_validator(mode="after")
    def bound_identity(self):
        if bool(self.run_id) != bool(self.sdk_session_id) or (self.run_id and self.principal):
            raise ValueError("Use either a bound Run or an administrator")
        return self


class ShopBrowserOperation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    operation: Literal["snapshot", "navigate", "click", "type", "submit", "download", "close"]
    url: str | None = Field(default=None, max_length=2000)
    observation_id: str | None = Field(default=None, pattern=r"^page_[a-f0-9]{32}$")
    target: str | None = Field(default=None, pattern=r"^element_[0-9]{1,3}$")
    value: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def arguments(self):
        if self.operation in {"click", "type", "submit", "download"} and (
            not self.target or not self.observation_id or self.url is not None
        ):
            raise ValueError("Actions require an observed element and page ID")
        if self.operation == "navigate" and not self.url:
            raise ValueError("Navigation requires a URL")
        if self.operation != "navigate" and self.url is not None:
            raise ValueError("Only navigation accepts a URL")
        if self.operation == "type" and self.value is None:
            raise ValueError("Typing requires an explicit value")
        if self.operation not in {"click", "type", "submit", "download"} and (
            self.target or self.observation_id or self.value is not None
        ):
            raise ValueError("This operation does not accept an element")
        if self.operation != "type" and self.value is not None:
            raise ValueError("Only typing accepts a value")
        return self


class ShopBrowserStore:
    def __init__(self, ledger: FileLedger):
        self.ledger = ledger
        self.root = ledger.root / "shop-browsers"
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.root.is_symlink():
            raise ValueError("Shop browser directory cannot be a symlink")

    def path(self, shop_id: str) -> Path:
        if not re.fullmatch(SHOP_ID, shop_id):
            raise ShopBrowserError("SHOP_NOT_FOUND", "Shop not found.", 404)
        return self.root / f"{shop_id}.json"

    def directory(self, shop_id: str) -> Path:
        shop = self.get(shop_id)
        with self.ledger.lifecycle.lease(shop.project_id):
            path = self.root / shop_id
            path.mkdir(mode=0o700, exist_ok=True)
            if path.is_symlink():
                raise ValueError("Invalid shop directory")
            return path

    def profile_paths(self, shop_id: str) -> list[Path]:
        """List every saved Chromium generation without following profile links."""
        directory = self.directory(shop_id)
        profiles = []
        with os.scandir(directory) as entries:
            for entry in entries:
                if not entry.name.startswith("profile-"):
                    continue
                if (
                    not re.fullmatch(r"profile-[1-9][0-9]*", entry.name)
                    or not entry.is_dir(follow_symlinks=False)
                ):
                    raise ShopBrowserError(
                        "SHOP_PROFILE_INVALID", "Browser profile directory failed validation."
                    )
                profiles.append(directory / entry.name)
        return sorted(profiles)

    def has_login_files(self, shop_id: str) -> bool:
        directory = self.directory(shop_id)
        with os.scandir(directory) as entries:
            return any(
                entry.name == "session.json" or entry.name.startswith("profile-")
                for entry in entries
            )

    def has_profile_files(self, shop_id: str) -> bool:
        directory = self.directory(shop_id)
        with os.scandir(directory) as entries:
            return any(entry.name.startswith("profile-") for entry in entries)

    def get(self, shop_id: str) -> ShopIdentity:
        shop = self._read(shop_id)
        with self.ledger.lifecycle.lease(shop.project_id):
            return shop

    def _read(self, shop_id: str) -> ShopIdentity:
        path = self.path(shop_id)
        try:
            if path.is_symlink() or path.stat().st_size > 32_000:
                raise ValueError("Invalid shop")
            shop = ShopIdentity.model_validate_json(path.read_text("utf-8"))
            if shop.id != shop_id:
                raise ValueError("Invalid shop")
            return shop
        except FileNotFoundError:
            raise ShopBrowserError("SHOP_NOT_FOUND", "Shop not found.", 404) from None
        except (OSError, ValueError):
            raise ShopBrowserError(
                "SHOP_INVALID", "Shop configuration failed validation."
            ) from None

    def list(self, project_id: str) -> list[ShopIdentity]:
        with self.ledger.lifecycle.lease(project_id):
            result = []
            for path in self.root.glob("shop_*.json"):
                # Discover association without entering an unrelated project's
                # lease: deleting A must not break the shop list of B.
                shop = self._read(path.stem)
                if shop.project_id == project_id:
                    result.append(shop)
            return sorted(result, key=lambda item: item.created_at)

    def write(self, shop: ShopIdentity) -> None:
        with self.ledger.lifecycle.lease(shop.project_id):
            self.ledger._atomic_write(self.path(shop.id), shop)

    def socket_path(self, shop_id: str) -> Path:
        shop = self.get(shop_id)
        with self.ledger.lifecycle.lease(shop.project_id):
            self.path(shop_id)
            key = hashlib.sha256(str(self.root.resolve()).encode()).hexdigest()[:12]
            folder = Path(tempfile.gettempdir()) / f"ophira-shops-{os.getuid()}-{key}"
            folder.mkdir(mode=0o700, exist_ok=True)
            info = folder.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError("Invalid private browser socket directory")
            return folder / f"{shop_id[-16:]}.sock"

    def session(self, shop_id: str) -> dict | None:
        shop = self.get(shop_id)
        with self.ledger.lifecycle.lease(shop.project_id):
            path = self.directory(shop_id) / "session.json"
            if not path.exists():
                return None
            try:
                if path.is_symlink() or path.stat().st_size > 4096:
                    raise ValueError("invalid session")
                import json

                value = json.loads(path.read_text("utf-8"))
                if (
                    not isinstance(value, dict)
                    or value.get("shop_id") != shop_id
                    or not re.fullmatch(SESSION_ID, value.get("id", ""))
                    or value.get("state") not in {"starting", "open", "closed", "failed"}
                    or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", value.get("token", ""))
                ):
                    raise ValueError("invalid session")
                return value
            except (OSError, ValueError, TypeError):
                raise ShopBrowserError(
                    "SHOP_SESSION_INVALID", "Browser session failed validation."
                ) from None
