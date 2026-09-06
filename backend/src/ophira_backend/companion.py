"""Project-scoped companion-device credentials (FR-25).

Only the authenticated desktop control plane can offer/approve a pairing. A
256-bit one-time fragment secret proves link possession, never approval. Device
credentials remain in a Secure HttpOnly cookie and are persisted as hashes only.
"""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import os
import re
import secrets
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path

from pydantic import Field, field_validator

from .models import APIModel, PrincipalBinding, PrincipalContext, new_id, utc_now

COMPANION_COOKIE = "__Host-ophira-companion"
COMPANION_HEADER = "x-ophira-companion"
PAIRING_TTL_SECONDS = 600
DEVICE_MAX_AGE_SECONDS = 30 * 24 * 60 * 60
MAX_ACTIVE_PAIRINGS = 25


class CompanionError(ValueError):
    """Safe error containing neither a credential nor a user-supplied value."""


class PairingStatus(StrEnum):
    OFFERED = "offered"
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    REVOKED = "revoked"
    EXPIRED = "expired"


class PairingCreate(APIModel):
    project_ids: list[str] = Field(min_length=1, max_length=20)
    allow_run_start: bool = False

    @field_validator("project_ids")
    @classmethod
    def bounded_projects(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value) or any(
            not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", item) for item in value
        ):
            raise ValueError("project IDs must be distinct ledger identifiers")
        return value


class PairingClaim(APIModel):
    pairing_id: str = Field(pattern=r"^pair_[a-f0-9]{32}$")
    secret: str = Field(min_length=43, max_length=43, repr=False)
    device_name: str = Field(min_length=1, max_length=120)

    @field_validator("device_name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        if not value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("device name must be visible text")
        return value.strip()


class PairingDecision(APIModel):
    decision: str = Field(pattern=r"^(approve|reject)$")
    verification_code: str | None = Field(default=None, pattern=r"^\d{6}$")


class PairingView(APIModel):
    id: str
    status: PairingStatus
    project_ids: list[str]
    allow_run_start: bool
    created_at: datetime
    expires_at: datetime
    device_name: str | None = None
    verification_code: str | None = None
    approved_at: datetime | None = None


class PairingRecord(PairingView):
    principal_binding: PrincipalBinding | None = None
    # Never returned by an HTTP response or included in Agent context.
    link_secret_hash: str | None = Field(default=None, repr=False)
    cookie_secret_hash: str | None = Field(default=None, repr=False)
    cookie_activated: bool = False
    revoked_at: datetime | None = None

    def view(self, *, now: datetime | None = None) -> PairingView:
        values = self.model_dump(include=PairingView.model_fields.keys())
        if self.status in {PairingStatus.OFFERED, PairingStatus.PENDING, PairingStatus.APPROVED}:
            if self.expires_at <= (now or utc_now()):
                values["status"] = PairingStatus.EXPIRED
        return PairingView.model_validate(values)


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _same_owner(record: PairingRecord, principal: PrincipalContext | None) -> bool:
    binding = record.principal_binding
    if binding is None or principal is None:
        return binding is None and principal is None
    return (binding.tenant_id, binding.principal_id) == (
        principal.tenant_id, principal.principal_id,
    )


class CompanionStore:
    def __init__(self, root: Path) -> None:
        if root.is_symlink():
            raise CompanionError("companion store must not be a symlink")
        self.root = root.resolve()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._lock = threading.RLock()

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self._lock:
            descriptor = os.open(
                self.root / ".transaction.lock",
                os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600,
            )
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                yield
            finally:
                os.close(descriptor)

    def _path(self, pairing_id: str) -> Path:
        if not re.fullmatch(r"pair_[a-f0-9]{32}", pairing_id):
            raise CompanionError("pairing is unavailable")
        return self.root / f"{pairing_id}.json"

    def _get(self, pairing_id: str) -> PairingRecord:
        path = self._path(pairing_id)
        if path.is_symlink():
            raise CompanionError("pairing is unavailable")
        try:
            return PairingRecord.model_validate_json(path.read_text("utf-8"))
        except (OSError, ValueError) as exc:
            raise CompanionError("pairing is unavailable") from exc

    def approved_record(self, pairing_id: str) -> PairingRecord:
        record = self._get(pairing_id)
        if record.view().status != PairingStatus.APPROVED or not record.cookie_activated:
            raise CompanionError("device is not approved or has expired")
        return record

    def _write(self, record: PairingRecord) -> None:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=self.root, prefix=".pair-", delete=False,
        ) as handle:
            temporary = Path(handle.name)
            try:
                handle.write(record.model_dump_json() + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise
        try:
            os.replace(temporary, self._path(record.id))
            descriptor = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        finally:
            temporary.unlink(missing_ok=True)

    def create(
        self, payload: PairingCreate, principal: PrincipalContext | None,
        *, now: datetime | None = None,
    ) -> tuple[PairingView, str]:
        moment = now or utc_now()
        with self._transaction():
            records = [self._get(path.stem) for path in self.root.glob("pair_*.json")]
            active = [record for record in records if _same_owner(record, principal)
                      and record.view(now=moment).status in {
                          PairingStatus.OFFERED, PairingStatus.PENDING, PairingStatus.APPROVED,
                      }]
            if len(active) >= MAX_ACTIVE_PAIRINGS:
                raise CompanionError("revoke an existing device before pairing another")
            # Authenticated offers are also rate limited, without storing IPs or secrets.
            if sum(record.created_at > moment - timedelta(minutes=1)
                   for record in records if _same_owner(record, principal)) >= 5:
                raise CompanionError("too many pairing offers; try again later")
            secret = secrets.token_urlsafe(32)
            record = PairingRecord(
                id=new_id("pair"), status=PairingStatus.OFFERED,
                **payload.model_dump(), created_at=moment,
                expires_at=moment + timedelta(seconds=PAIRING_TTL_SECONDS),
                principal_binding=PrincipalBinding.from_context(principal) if principal else None,
                link_secret_hash=_digest(secret),
            )
            self._write(record)
            return record.view(now=moment), secret

    def list(self, principal: PrincipalContext | None) -> list[PairingView]:
        with self._transaction():
            records = [self._get(path.stem) for path in self.root.glob("pair_*.json")]
            return [record.view() for record in sorted(
                records, key=lambda item: item.created_at, reverse=True,
            ) if _same_owner(record, principal)]

    def owner_record(self, pairing_id: str, principal: PrincipalContext | None) -> PairingRecord:
        with self._transaction():
            record = self._get(pairing_id)
            if not _same_owner(record, principal):
                raise CompanionError("pairing is unavailable")
            return record

    def claim(
        self, payload: PairingClaim, *, now: datetime | None = None,
    ) -> tuple[PairingView, str]:
        moment = now or utc_now()
        with self._transaction():
            record = self._get(payload.pairing_id)
            if (
                record.view(now=moment).status != PairingStatus.OFFERED
                or not record.link_secret_hash
                or not hmac.compare_digest(record.link_secret_hash, _digest(payload.secret))
            ):
                raise CompanionError("pairing link is invalid, expired or already used")
            secret = secrets.token_urlsafe(32)
            updated = record.model_copy(update={
                "status": PairingStatus.PENDING, "device_name": payload.device_name,
                "verification_code": f"{secrets.randbelow(1_000_000):06d}",
                "cookie_secret_hash": _digest(secret), "link_secret_hash": None,
            })
            self._write(updated)
            return updated.view(now=moment), f"{record.id}.{secret}"

    def decide(
        self, pairing_id: str, payload: PairingDecision,
        principal: PrincipalContext | None, *, now: datetime | None = None,
    ) -> PairingView:
        moment = now or utc_now()
        with self._transaction():
            record = self._get(pairing_id)
            if not _same_owner(record, principal):
                raise CompanionError("pairing is unavailable")
            if record.view(now=moment).status != PairingStatus.PENDING:
                raise CompanionError("pairing is not awaiting approval")
            if payload.decision == "approve":
                if not payload.verification_code or not hmac.compare_digest(
                    record.verification_code or "", payload.verification_code,
                ):
                    raise CompanionError("verify the six-digit code shown on the companion")
                updated = record.model_copy(update={
                    "status": PairingStatus.APPROVED, "approved_at": moment,
                    "expires_at": moment + timedelta(seconds=DEVICE_MAX_AGE_SECONDS),
                })
            else:
                updated = record.model_copy(update={"status": PairingStatus.REJECTED})
            self._write(updated)
            return updated.view(now=moment)

    def authenticate(self, cookie: str | None) -> PairingRecord:
        if not cookie or len(cookie) > 100 or cookie.count(".") != 1:
            raise CompanionError("companion authentication required")
        pairing_id, secret = cookie.split(".", 1)
        with self._transaction():
            record = self._get(pairing_id)
            if not record.cookie_secret_hash or not hmac.compare_digest(
                record.cookie_secret_hash, _digest(secret),
            ):
                raise CompanionError("companion authentication required")
            return record

    def activate(self, cookie: str) -> tuple[PairingRecord, str | None]:
        """Rotate the pending credential exactly once when desktop approval takes effect."""
        pairing_id, secret = cookie.split(".", 1)
        with self._transaction():
            record = self._get(pairing_id)
            if not record.cookie_secret_hash or not hmac.compare_digest(
                record.cookie_secret_hash, _digest(secret),
            ):
                raise CompanionError("companion authentication required")
            if record.view().status != PairingStatus.APPROVED or record.cookie_activated:
                return record, None
            renewed = secrets.token_urlsafe(32)
            updated = record.model_copy(update={
                "cookie_secret_hash": _digest(renewed), "cookie_activated": True,
            })
            self._write(updated)
            return updated, f"{record.id}.{renewed}"

    def revoke(self, pairing_id: str, principal: PrincipalContext | None) -> PairingView:
        with self._transaction():
            record = self._get(pairing_id)
            if not _same_owner(record, principal):
                raise CompanionError("pairing is unavailable")
            updated = record.model_copy(update={
                "status": PairingStatus.REVOKED, "revoked_at": utc_now(),
                "link_secret_hash": None,
            })
            self._write(updated)
            return updated.view()

    def revoke_device(self, cookie: str) -> None:
        # Authenticate again inside the same transaction; callers cannot revoke
        # another device by substituting an ID in an HTTP payload.
        pairing_id, secret = cookie.split(".", 1)
        with self._transaction():
            record = self._get(pairing_id)
            if not record.cookie_secret_hash or not hmac.compare_digest(
                record.cookie_secret_hash, _digest(secret),
            ):
                raise CompanionError("companion authentication required")
            self._write(record.model_copy(update={
                "status": PairingStatus.REVOKED, "revoked_at": utc_now(),
            }))
