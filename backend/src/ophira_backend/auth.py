"""Trusted-gateway authentication and project authorization primitives.

Identity enters the control plane through one short-lived, request-bound HMAC
assertion.  Model/tool arguments and unsigned identity-shaped headers are never
accepted as a principal.  The gateway is an authentication adapter, while the
durable project membership ledger remains the authorization authority.
"""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import threading
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Final, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .config import Settings
from .constants import PrincipalRole
from .models import PrincipalContext, utc_now

AUTH_ASSERTION_VERSION: Final = "v1"
AUTH_HEADER_TENANT: Final = "x-ophira-auth-tenant-id"
AUTH_HEADER_PRINCIPAL: Final = "x-ophira-auth-principal-id"
AUTH_HEADER_ROLE: Final = "x-ophira-auth-role"
AUTH_HEADER_SESSION: Final = "x-ophira-auth-session-id"
AUTH_HEADER_ISSUED_AT: Final = "x-ophira-auth-issued-at"
AUTH_HEADER_NONCE: Final = "x-ophira-auth-nonce"
AUTH_HEADER_SIGNATURE: Final = "x-ophira-auth-signature"
AUTH_HEADERS: Final = frozenset(
    {
        AUTH_HEADER_TENANT,
        AUTH_HEADER_PRINCIPAL,
        AUTH_HEADER_ROLE,
        AUTH_HEADER_SESSION,
        AUTH_HEADER_ISSUED_AT,
        AUTH_HEADER_NONCE,
        AUTH_HEADER_SIGNATURE,
    }
)

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+\-]{0,159}$")
_SAFE_SESSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+\-/]{15,499}$")
_SAFE_NONCE = re.compile(r"^[A-Za-z0-9_-]{20,160}$")
_SAFE_METHOD = re.compile(r"^[A-Z][A-Z0-9_-]{0,31}$")
_SAFE_PATH = re.compile(r"^/[A-Za-z0-9._~!$&'()*+,;=:@/\-]*$")
_SAFE_LEDGER_ID = re.compile(r"^[A-Za-z0-9_-]{1,200}$")


class AuthenticationError(RuntimeError):
    """Safe, fail-closed request authentication error."""


class AuthConfigurationError(AuthenticationError):
    pass


class InvalidAuthAssertionError(AuthenticationError):
    pass


class AuthAssertionExpiredError(AuthenticationError):
    pass


class AuthAssertionReplayError(AuthenticationError):
    pass


class AuthorizationError(RuntimeError):
    """The authenticated principal is outside the resource/action scope."""


class ProjectAccessNotFoundError(AuthorizationError):
    pass


class ProjectAccessConflictError(AuthorizationError):
    pass


class ProjectPermission(StrEnum):
    PROJECT_READ = "project.read"
    PROJECT_WRITE = "project.write"
    PROJECT_ACCESS_MANAGE = "project.access.manage"
    RUN_CREATE = "run.create"
    RUN_READ = "run.read"
    TOOL_USE = "tool.use"
    MEMORY_READ = "memory.read"
    MEMORY_WRITE = "memory.write"
    COMMERCIAL_READ = "commercial.read"
    COMMERCIAL_STAGE = "commercial.stage"
    COMMERCIAL_REVIEW = "commercial.review"
    COMMERCIAL_APPLY = "commercial.apply"
    COMMERCIAL_CANCEL = "commercial.cancel"
    SOURCE_RECEIPT_READ = "source_receipt.read"


_OWNER_PERMISSIONS = frozenset(ProjectPermission)
ROLE_PERMISSIONS: Final[dict[PrincipalRole, frozenset[ProjectPermission]]] = {
    PrincipalRole.OWNER: _OWNER_PERMISSIONS,
    PrincipalRole.ADMIN: _OWNER_PERMISSIONS,
    PrincipalRole.MEMBER: frozenset(
        {
            ProjectPermission.PROJECT_READ,
            ProjectPermission.PROJECT_WRITE,
            ProjectPermission.RUN_CREATE,
            ProjectPermission.RUN_READ,
            ProjectPermission.TOOL_USE,
            ProjectPermission.MEMORY_READ,
            ProjectPermission.MEMORY_WRITE,
            ProjectPermission.COMMERCIAL_READ,
            ProjectPermission.COMMERCIAL_STAGE,
            ProjectPermission.COMMERCIAL_CANCEL,
            ProjectPermission.SOURCE_RECEIPT_READ,
        }
    ),
    PrincipalRole.REVIEWER: frozenset(
        {
            ProjectPermission.PROJECT_READ,
            ProjectPermission.RUN_READ,
            ProjectPermission.MEMORY_READ,
            ProjectPermission.COMMERCIAL_READ,
            ProjectPermission.COMMERCIAL_REVIEW,
            ProjectPermission.COMMERCIAL_APPLY,
            ProjectPermission.COMMERCIAL_CANCEL,
            ProjectPermission.SOURCE_RECEIPT_READ,
        }
    ),
    PrincipalRole.VIEWER: frozenset(
        {
            ProjectPermission.PROJECT_READ,
            ProjectPermission.RUN_READ,
            ProjectPermission.MEMORY_READ,
            ProjectPermission.COMMERCIAL_READ,
            ProjectPermission.SOURCE_RECEIPT_READ,
        }
    ),
}


@dataclass(frozen=True)
class AuthDeploymentStatus:
    mode: Literal["local_only", "trusted_gateway"]
    authenticated: bool
    multi_tenant: bool
    trusted_gateway_assertions_required: bool


def canonical_request_method(method: str) -> str:
    normalized = method.strip().upper()
    if not _SAFE_METHOD.fullmatch(normalized):
        raise InvalidAuthAssertionError("request method is not canonical")
    return normalized


def canonical_request_path(path: str) -> str:
    """Accept one exact ASCII routing path, excluding proxy-ambiguous encodings."""

    if (
        len(path) > 2_048
        or not _SAFE_PATH.fullmatch(path)
        or "//" in path
        or "\\" in path
    ):
        raise InvalidAuthAssertionError("request path is not canonical")
    if any(segment in {".", ".."} for segment in path.split("/")):
        raise InvalidAuthAssertionError("request path is not canonical")
    return path


def _canonical_assertion_bytes(
    *,
    method: str,
    path: str,
    tenant_id: str,
    principal_id: str,
    role: PrincipalRole,
    session_id: str,
    issued_at: int,
    nonce: str,
) -> bytes:
    value = {
        "issued_at": issued_at,
        "method": canonical_request_method(method),
        "nonce": nonce,
        "path": canonical_request_path(path),
        "principal_id": principal_id,
        "role": role.value,
        "session_id": session_id,
        "tenant_id": tenant_id,
        "version": AUTH_ASSERTION_VERSION,
    }
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def build_gateway_assertion_headers(
    secret: str,
    *,
    method: str,
    path: str,
    tenant_id: str,
    principal_id: str,
    role: PrincipalRole,
    session_id: str,
    issued_at: datetime,
    nonce: str | None = None,
) -> dict[str, str]:
    """Build the ingress contract; primarily useful to gateway adapters/tests."""

    secret_bytes = secret.encode("utf-8")
    if len(secret_bytes) < 32:
        raise AuthConfigurationError("gateway signing secret must contain at least 32 bytes")
    _validate_identity(tenant_id, principal_id, session_id)
    if issued_at.tzinfo is None or issued_at.utcoffset() is None:
        raise InvalidAuthAssertionError("assertion timestamp must be timezone-aware")
    issued_epoch = int(issued_at.timestamp())
    assertion_nonce = nonce or secrets.token_urlsafe(24)
    if not _SAFE_NONCE.fullmatch(assertion_nonce):
        raise InvalidAuthAssertionError("assertion nonce is invalid")
    canonical = _canonical_assertion_bytes(
        method=method,
        path=path,
        tenant_id=tenant_id,
        principal_id=principal_id,
        role=role,
        session_id=session_id,
        issued_at=issued_epoch,
        nonce=assertion_nonce,
    )
    signature = hmac.new(secret_bytes, canonical, hashlib.sha256).hexdigest()
    return {
        "X-Ophira-Auth-Tenant-Id": tenant_id,
        "X-Ophira-Auth-Principal-Id": principal_id,
        "X-Ophira-Auth-Role": role.value,
        "X-Ophira-Auth-Session-Id": session_id,
        "X-Ophira-Auth-Issued-At": str(issued_epoch),
        "X-Ophira-Auth-Nonce": assertion_nonce,
        "X-Ophira-Auth-Signature": f"{AUTH_ASSERTION_VERSION}={signature}",
    }


def _validate_identity(tenant_id: str, principal_id: str, session_id: str) -> None:
    if not _SAFE_ID.fullmatch(tenant_id):
        raise InvalidAuthAssertionError("tenant identity is invalid")
    if not _SAFE_ID.fullmatch(principal_id):
        raise InvalidAuthAssertionError("principal identity is invalid")
    if not _SAFE_SESSION.fullmatch(session_id):
        raise InvalidAuthAssertionError("principal session identity is invalid")


def _header_items(headers: Mapping[str, str] | Iterable[tuple[str, str]]):
    if hasattr(headers, "multi_items"):
        return list(headers.multi_items())  # type: ignore[attr-defined]
    if isinstance(headers, Mapping):
        return list(headers.items())
    return list(headers)


def _auth_header_values(
    headers: Mapping[str, str] | Iterable[tuple[str, str]],
) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw_name, raw_value in _header_items(headers):
        name = str(raw_name).casefold()
        if name not in AUTH_HEADERS:
            continue
        if name in result:
            raise InvalidAuthAssertionError("duplicate authentication header")
        value = str(raw_value)
        if not value or value != value.strip() or any(ord(char) < 32 for char in value):
            raise InvalidAuthAssertionError("authentication header is malformed")
        result[name] = value
    missing = AUTH_HEADERS - result.keys()
    if missing:
        raise InvalidAuthAssertionError("trusted gateway assertion is incomplete")
    return result


class AuthNonceStore:
    """Cross-thread/process replay guard using exclusive, mode-0600 snapshots."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.root.chmod(0o700)
        except OSError:
            pass
        self._process_lock = threading.RLock()

    @contextmanager
    def _locked(self) -> Iterator[None]:
        with self._process_lock:
            lock_path = self.root / ".nonce.lock"
            flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(lock_path, flags, 0o600)
                fcntl.flock(descriptor, fcntl.LOCK_EX)
            except OSError as exc:
                raise AuthenticationError("authentication replay guard is unavailable") from exc
            try:
                yield
            finally:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(descriptor)

    def consume(self, nonce: str, *, expires_at: datetime, now: datetime) -> None:
        digest = hashlib.sha256(nonce.encode("ascii")).hexdigest()
        path = self.root / f"nonce_{digest}.json"
        with self._locked():
            if path.exists():
                try:
                    if path.is_symlink() or not path.is_file():
                        raise OSError("invalid nonce snapshot")
                    stored = json.loads(path.read_text(encoding="utf-8"))
                    stored_expiry = datetime.fromisoformat(stored["expires_at"])
                except (KeyError, TypeError, ValueError, OSError) as exc:
                    raise AuthenticationError(
                        "authentication replay guard failed integrity validation"
                    ) from exc
                if stored_expiry.tzinfo is None or stored_expiry.utcoffset() is None:
                    raise AuthenticationError(
                        "authentication replay guard failed integrity validation"
                    )
                if now <= stored_expiry:
                    raise AuthAssertionReplayError("trusted gateway assertion was replayed")
                try:
                    path.unlink()
                except OSError as exc:
                    raise AuthenticationError(
                        "authentication replay guard could not retire an expired nonce"
                    ) from exc

            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(path, flags, 0o600)
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    handle.write(
                        json.dumps(
                            {"expires_at": expires_at.isoformat()},
                            separators=(",", ":"),
                        )
                        + "\n"
                    )
                    handle.flush()
                    os.fsync(handle.fileno())
            except FileExistsError as exc:  # pragma: no cover - flock defense in depth
                raise AuthAssertionReplayError(
                    "trusted gateway assertion was replayed"
                ) from exc
            except OSError as exc:
                raise AuthenticationError("authentication replay guard is unavailable") from exc


class PrincipalAuthenticator:
    """Resolve a trusted principal or explicitly report the local-only boundary."""

    def __init__(self, settings: Settings, nonce_store: AuthNonceStore) -> None:
        self.mode = settings.auth_mode
        self.max_age = timedelta(seconds=settings.auth_assertion_max_age_seconds)
        self.future_skew = timedelta(seconds=settings.auth_assertion_future_skew_seconds)
        configured = settings.trusted_gateway_auth_secret
        self.tenant_id = settings.trusted_gateway_tenant_id
        if self.mode == "trusted_gateway":
            if configured is None or self.tenant_id is None:
                raise AuthConfigurationError(
                    "trusted gateway signing secret or deployment tenant is not configured"
                )
            self._secret = configured.get_secret_value().encode("utf-8")
            if len(self._secret) < 32:
                raise AuthConfigurationError(
                    "gateway signing secret must contain at least 32 bytes"
                )
        else:
            self._secret = None
        self.nonce_store = nonce_store

    @property
    def status(self) -> AuthDeploymentStatus:
        trusted = self.mode == "trusted_gateway"
        return AuthDeploymentStatus(
            mode=self.mode,
            authenticated=trusted,
            multi_tenant=False,
            trusted_gateway_assertions_required=trusted,
        )

    def authenticate(
        self,
        headers: Mapping[str, str] | Iterable[tuple[str, str]],
        *,
        method: str,
        path: str,
        client_host: str | None,
        now: datetime | None = None,
    ) -> PrincipalContext | None:
        if self.mode == "local_only":
            if not _is_loopback(client_host):
                raise AuthenticationError(
                    "local-only control plane rejects non-loopback clients"
                )
            # Returning None is deliberate: local access must never masquerade as
            # an authenticated tenant/principal.
            return None

        assert self._secret is not None
        values = _auth_header_values(headers)
        tenant_id = values[AUTH_HEADER_TENANT]
        principal_id = values[AUTH_HEADER_PRINCIPAL]
        session_id = values[AUTH_HEADER_SESSION]
        _validate_identity(tenant_id, principal_id, session_id)
        if tenant_id != self.tenant_id:
            raise InvalidAuthAssertionError(
                "trusted gateway assertion is outside the deployment tenant"
            )
        try:
            role = PrincipalRole(values[AUTH_HEADER_ROLE])
        except ValueError as exc:
            raise InvalidAuthAssertionError("asserted principal role is invalid") from exc
        nonce = values[AUTH_HEADER_NONCE]
        if not _SAFE_NONCE.fullmatch(nonce):
            raise InvalidAuthAssertionError("assertion nonce is invalid")
        issued_text = values[AUTH_HEADER_ISSUED_AT]
        if not issued_text.isascii() or not issued_text.isdecimal() or len(issued_text) > 12:
            raise InvalidAuthAssertionError("assertion timestamp is invalid")
        issued_epoch = int(issued_text)
        try:
            issued_at = datetime.fromtimestamp(issued_epoch, UTC)
        except (OverflowError, OSError, ValueError) as exc:
            raise InvalidAuthAssertionError("assertion timestamp is invalid") from exc
        checked_at = now or utc_now()
        if checked_at.tzinfo is None or checked_at.utcoffset() is None:
            raise AuthConfigurationError("authentication clock must be timezone-aware")
        if issued_at > checked_at + self.future_skew:
            raise InvalidAuthAssertionError("trusted gateway assertion is from the future")
        expires_at = issued_at + self.max_age
        if checked_at >= expires_at:
            raise AuthAssertionExpiredError("trusted gateway assertion has expired")

        signature_value = values[AUTH_HEADER_SIGNATURE]
        prefix = f"{AUTH_ASSERTION_VERSION}="
        supplied = signature_value.removeprefix(prefix)
        if (
            not signature_value.startswith(prefix)
            or len(supplied) != 64
            or any(char not in "0123456789abcdef" for char in supplied)
        ):
            raise InvalidAuthAssertionError("trusted gateway signature is invalid")
        canonical = _canonical_assertion_bytes(
            method=method,
            path=path,
            tenant_id=tenant_id,
            principal_id=principal_id,
            role=role,
            session_id=session_id,
            issued_at=issued_epoch,
            nonce=nonce,
        )
        expected = hmac.new(self._secret, canonical, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(supplied.encode("ascii"), expected.encode("ascii")):
            raise InvalidAuthAssertionError("trusted gateway signature is invalid")

        # Consume only an authenticated nonce so arbitrary clients cannot fill
        # the replay store with unsigned input.
        self.nonce_store.consume(
            nonce,
            expires_at=expires_at + self.future_skew,
            now=checked_at,
        )
        return PrincipalContext(
            tenant_id=tenant_id,
            principal_id=principal_id,
            role=role,
            session_id=session_id,
            assertion_issued_at=issued_at,
            assertion_expires_at=expires_at,
            assertion_nonce=nonce,
        )


def _is_loopback(client_host: str | None) -> bool:
    if client_host is None:
        return False
    normalized = client_host.strip().casefold()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _http_origin(value: str, *, allow_path: bool = False) -> str:
    if not value.isascii() or any(char.isspace() for char in value) or "\\" in value:
        raise AuthenticationError("invalid browser origin")
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError as exc:
        raise AuthenticationError("invalid browser origin") from exc
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or "%" in parts.netloc
        or port == 0
        or (not allow_path and (parts.path or parts.query or parts.fragment))
    ):
        raise AuthenticationError("invalid browser origin")
    host = parts.hostname
    if ":" in host:
        host = f"[{host}]"
    default_port = 443 if parts.scheme == "https" else 80
    return f"{parts.scheme}://{host}" + (
        f":{port}" if port is not None and port != default_port else ""
    )


def validate_local_request_source(
    headers: Iterable[tuple[str, str]],
    *,
    scheme: str,
    allowed_origins: list[str],
) -> None:
    """Check browser authority before CORS or any local control-plane side effect."""

    values: dict[str, str] = {}
    for name, value in headers:
        name = name.casefold()
        if name not in {"host", "origin", "referer", "sec-fetch-site"}:
            continue
        if name in values:
            raise AuthenticationError("duplicate request source header")
        values[name] = value
    request_origin = _http_origin(f"{scheme}://{values.get('host', '')}")
    if not _is_loopback(urlsplit(request_origin).hostname):
        raise AuthenticationError("local-only control plane requires a loopback Host")
    trusted_origins = {request_origin}
    for origin in allowed_origins:
        # A CORS wildcard must never become a local control-plane trust grant.
        if origin != "*":
            trusted_origins.add(_http_origin(origin))
    if "origin" in values:
        source = _http_origin(values["origin"])
    elif "referer" in values:
        source = _http_origin(values["referer"], allow_path=True)
    elif values.get("sec-fetch-site") == "cross-site":
        raise AuthenticationError("local-only control plane rejects cross-site requests")
    else:
        return  # Direct local CLI clients have no browser source headers.
    if source not in trusted_origins:
        raise AuthenticationError("local-only control plane rejects untrusted origins")


class ProjectMembership(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    principal_id: str = Field(pattern=_SAFE_ID.pattern)
    role: PrincipalRole
    granted_by: str = Field(pattern=_SAFE_ID.pattern)
    granted_at: datetime


class ProjectAccessGrantRequest(BaseModel):
    """Product API input; identity and grantor always come from host auth."""

    model_config = ConfigDict(extra="forbid")

    principal_id: str = Field(pattern=_SAFE_ID.pattern)
    role: PrincipalRole


class ProjectAccessRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str = Field(pattern=_SAFE_LEDGER_ID.pattern)
    tenant_id: str = Field(pattern=_SAFE_ID.pattern)
    owner_principal_id: str = Field(pattern=_SAFE_ID.pattern)
    memberships: dict[str, ProjectMembership]
    revision: int = Field(default=1, ge=1)
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def coherent_memberships(self) -> ProjectAccessRecord:
        owner = self.memberships.get(self.owner_principal_id)
        if owner is None or owner.role != PrincipalRole.OWNER:
            raise ValueError("project owner membership is missing")
        for key, membership in self.memberships.items():
            if key != membership.principal_id:
                raise ValueError("project membership key does not match its principal")
            if membership.role == PrincipalRole.OWNER and key != self.owner_principal_id:
                raise ValueError("a project has exactly one owner")
        return self


class ProjectAccessStore:
    """Durable tenant/project membership and role authorization authority."""

    def __init__(self, root: Path, *, clock=utc_now) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._clock = clock
        self._locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()

    @staticmethod
    def _safe_project_id(project_id: str) -> str:
        if not _SAFE_LEDGER_ID.fullmatch(project_id):
            raise ProjectAccessNotFoundError("project access record not found")
        return project_id

    def _path(self, project_id: str) -> Path:
        return self.root / self._safe_project_id(project_id) / "access.json"

    @contextmanager
    def _transaction(self, project_id: str) -> Iterator[None]:
        project_id = self._safe_project_id(project_id)
        with self._locks_guard:
            lock = self._locks.setdefault(project_id, threading.RLock())
        with lock:
            directory = self.root / project_id
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            lock_path = directory / ".access.lock"
            flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(lock_path, flags, 0o600)
                fcntl.flock(descriptor, fcntl.LOCK_EX)
            except OSError as exc:
                raise AuthorizationError("project access store is unavailable") from exc
            try:
                yield
            finally:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(descriptor)

    def _write(self, record: ProjectAccessRecord, *, create: bool = False) -> None:
        path = self._path(record.project_id)
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(12)}.tmp")
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(temporary, flags, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(record.model_dump_json() + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            if create:
                try:
                    os.link(temporary, path)
                except FileExistsError as exc:
                    raise ProjectAccessConflictError(
                        "project access record already exists"
                    ) from exc
            else:
                os.replace(temporary, path)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def get(self, project_id: str) -> ProjectAccessRecord:
        path = self._path(project_id)
        try:
            if path.is_symlink() or not path.is_file():
                raise ProjectAccessNotFoundError("project access record not found")
            return ProjectAccessRecord.model_validate_json(path.read_text(encoding="utf-8"))
        except ProjectAccessNotFoundError:
            raise
        except (OSError, ValueError) as exc:
            raise AuthorizationError(
                "project access record failed integrity validation"
            ) from exc

    def bind_project(self, project_id: str, owner: PrincipalContext) -> ProjectAccessRecord:
        """Bind a new/migrated project only to an explicitly authenticated owner."""

        if owner.role != PrincipalRole.OWNER:
            raise AuthorizationError("only an asserted owner can bind a project")
        now = self._clock()
        membership = ProjectMembership(
            principal_id=owner.principal_id,
            role=PrincipalRole.OWNER,
            granted_by=owner.principal_id,
            granted_at=now,
        )
        record = ProjectAccessRecord(
            project_id=project_id,
            tenant_id=owner.tenant_id,
            owner_principal_id=owner.principal_id,
            memberships={owner.principal_id: membership},
            created_at=now,
            updated_at=now,
        )
        with self._transaction(project_id):
            try:
                existing = self.get(project_id)
            except ProjectAccessNotFoundError:
                self._write(record, create=True)
                return record
            if (
                existing.tenant_id == owner.tenant_id
                and existing.owner_principal_id == owner.principal_id
            ):
                return existing
            raise ProjectAccessConflictError("project is already bound to another identity")

    def remove_initial_binding(
        self,
        project_id: str,
        owner: PrincipalContext,
    ) -> None:
        """Compensate a failed project create without deleting an evolved record."""

        with self._transaction(project_id):
            current = self.get(project_id)
            if (
                current.tenant_id != owner.tenant_id
                or current.owner_principal_id != owner.principal_id
                or current.revision != 1
                or set(current.memberships) != {owner.principal_id}
            ):
                raise ProjectAccessConflictError(
                    "initial project binding changed before create rollback"
                )
            try:
                self._path(project_id).unlink()
            except OSError as exc:
                raise AuthorizationError(
                    "initial project binding could not be rolled back"
                ) from exc

    def authorize(
        self,
        project_id: str,
        principal: PrincipalContext,
        permission: ProjectPermission,
        *,
        now: datetime | None = None,
    ) -> ProjectMembership:
        checked_at = now or self._clock()
        if checked_at >= principal.assertion_expires_at:
            raise AuthAssertionExpiredError("principal assertion has expired")
        return self.authorize_bound(
            project_id,
            tenant_id=principal.tenant_id,
            principal_id=principal.principal_id,
            role=principal.role,
            permission=permission,
        )

    def authorize_bound(
        self,
        project_id: str,
        *,
        tenant_id: str,
        principal_id: str,
        role: PrincipalRole,
        permission: ProjectPermission,
    ) -> ProjectMembership:
        """Authorize identity already inherited from a durable host-bound object."""

        record = self.get(project_id)
        if record.tenant_id != tenant_id:
            raise AuthorizationError("principal is outside the project tenant")
        membership = record.memberships.get(principal_id)
        if membership is None:
            raise AuthorizationError("principal is not a project member")
        # The signed role and durable project role must agree. This prevents a
        # stale/elevated gateway claim from bypassing project authorization.
        if membership.role != role:
            raise AuthorizationError("asserted role does not match project membership")
        if permission not in ROLE_PERMISSIONS[membership.role]:
            raise AuthorizationError("principal role does not allow this project action")
        return membership

    def grant(
        self,
        project_id: str,
        actor: PrincipalContext,
        *,
        principal_id: str,
        role: PrincipalRole,
    ) -> ProjectAccessRecord:
        if not _SAFE_ID.fullmatch(principal_id) or role == PrincipalRole.OWNER:
            raise AuthorizationError("project membership grant is invalid")
        with self._transaction(project_id):
            self.authorize(project_id, actor, ProjectPermission.PROJECT_ACCESS_MANAGE)
            current = self.get(project_id)
            if principal_id == current.owner_principal_id:
                raise AuthorizationError("the project owner role cannot be changed")
            if actor.role == PrincipalRole.ADMIN and role == PrincipalRole.ADMIN:
                raise AuthorizationError("only the project owner can grant admin access")
            now = self._clock()
            memberships = dict(current.memberships)
            memberships[principal_id] = ProjectMembership(
                principal_id=principal_id,
                role=role,
                granted_by=actor.principal_id,
                granted_at=now,
            )
            updated = current.model_copy(
                update={
                    "memberships": memberships,
                    "revision": current.revision + 1,
                    "updated_at": now,
                }
            )
            self._write(updated)
            return updated

    def revoke(
        self,
        project_id: str,
        actor: PrincipalContext,
        *,
        principal_id: str,
    ) -> ProjectAccessRecord:
        with self._transaction(project_id):
            self.authorize(project_id, actor, ProjectPermission.PROJECT_ACCESS_MANAGE)
            current = self.get(project_id)
            if principal_id == current.owner_principal_id:
                raise AuthorizationError("the project owner cannot be revoked")
            memberships = dict(current.memberships)
            memberships.pop(principal_id, None)
            now = self._clock()
            updated = current.model_copy(
                update={
                    "memberships": memberships,
                    "revision": current.revision + 1,
                    "updated_at": now,
                }
            )
            self._write(updated)
            return updated
