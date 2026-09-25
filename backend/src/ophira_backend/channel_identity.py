"""Durable, server-administered identity bindings for signed channel events.

The channel transport authenticates an event, but that event does not identify
an OPHIRA principal or project. An administrator binds the verified external
tenant/conversation/user tuple to a durable OPHIRA principal scope. Resolution
revalidates project membership so revocation or role changes take effect.
"""

from __future__ import annotations

import fcntl
import os
import re
import secrets
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .auth import AuthorizationError, ProjectAccessStore, ProjectPermission
from .constants import PrincipalRole
from .models import PrincipalBinding, PrincipalContext, new_id, utc_now
from .project_lifecycle import ProjectLifecycleBusyError, ProjectUnavailableError

_CHANNEL_ID_PATTERN: Final = r"^[A-Za-z0-9][A-Za-z0-9._:@+\-/=]{0,255}$"
_OPHIRA_ID_PATTERN: Final = r"^[A-Za-z0-9][A-Za-z0-9._:@+\-]{0,159}$"
_SESSION_ID_PATTERN: Final = r"^[A-Za-z0-9][A-Za-z0-9._:@+\-/]{15,499}$"
_LEDGER_ID_PATTERN: Final = r"^[A-Za-z0-9_-]{1,200}$"
_BINDING_ID = re.compile(r"^channel_binding_[a-f0-9]{32}$")


class ChannelIdentityError(RuntimeError):
    """Safe, fail-closed channel identity error."""


class ChannelIdentityNotFoundError(ChannelIdentityError):
    pass


class ChannelIdentityRevokedError(ChannelIdentityError):
    pass


class ChannelIdentityAmbiguousError(ChannelIdentityError):
    pass


class ChannelIdentityConflictError(ChannelIdentityError):
    pass


class ChannelIdentityIntegrityError(ChannelIdentityError):
    pass


class ChannelIdentityPayloadError(ChannelIdentityError):
    pass


class FeishuIdentitySelector(BaseModel):
    """Identifiers read only from an already authenticated channel event."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    channel: Literal["feishu", "dingtalk"] = "feishu"
    tenant_key: str = Field(min_length=1, max_length=256, pattern=_CHANNEL_ID_PATTERN)
    chat_id: str = Field(min_length=1, max_length=256, pattern=_CHANNEL_ID_PATTERN)
    user_open_id: str = Field(min_length=1, max_length=256, pattern=_CHANNEL_ID_PATTERN)


class FeishuIdentityBindingCreate(BaseModel):
    """Trusted control-plane input; OPHIRA tenant and session are server-owned."""

    model_config = ConfigDict(extra="forbid")

    tenant_key: str = Field(min_length=1, max_length=256, pattern=_CHANNEL_ID_PATTERN)
    chat_id: str = Field(min_length=1, max_length=256, pattern=_CHANNEL_ID_PATTERN)
    user_open_id: str = Field(min_length=1, max_length=256, pattern=_CHANNEL_ID_PATTERN)
    project_id: str = Field(min_length=1, max_length=200, pattern=_LEDGER_ID_PATTERN)
    principal_id: str = Field(
        min_length=1,
        max_length=160,
        pattern=_OPHIRA_ID_PATTERN,
    )
    role: PrincipalRole

    @property
    def selector(self) -> FeishuIdentitySelector:
        return FeishuIdentitySelector(
            channel="feishu",
            tenant_key=self.tenant_key,
            chat_id=self.chat_id,
            user_open_id=self.user_open_id,
        )


class DingTalkIdentityBindingCreate(BaseModel):
    """Admin mapping for one authenticated DingTalk Stream conversation sender."""

    model_config = ConfigDict(extra="forbid")

    corp_id: str = Field(min_length=1, max_length=256, pattern=_CHANNEL_ID_PATTERN)
    conversation_id: str = Field(min_length=1, max_length=256, pattern=_CHANNEL_ID_PATTERN)
    sender_staff_id: str = Field(min_length=1, max_length=256, pattern=_CHANNEL_ID_PATTERN)
    project_id: str = Field(min_length=1, max_length=200, pattern=_LEDGER_ID_PATTERN)
    principal_id: str = Field(min_length=1, max_length=160, pattern=_OPHIRA_ID_PATTERN)
    role: PrincipalRole

    @property
    def selector(self) -> FeishuIdentitySelector:
        return FeishuIdentitySelector(
            channel="dingtalk",
            tenant_key=self.corp_id,
            chat_id=self.conversation_id,
            user_open_id=self.sender_staff_id,
        )


class FeishuIdentityBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(
        default_factory=lambda: new_id("channel_binding"),
        pattern=r"^channel_binding_[a-f0-9]{32}$",
    )
    channel: Literal["feishu", "dingtalk"] = "feishu"
    tenant_key: str = Field(min_length=1, max_length=256, pattern=_CHANNEL_ID_PATTERN)
    chat_id: str = Field(min_length=1, max_length=256, pattern=_CHANNEL_ID_PATTERN)
    user_open_id: str = Field(min_length=1, max_length=256, pattern=_CHANNEL_ID_PATTERN)
    tenant_id: str = Field(min_length=1, max_length=160, pattern=_OPHIRA_ID_PATTERN)
    principal_id: str = Field(min_length=1, max_length=160, pattern=_OPHIRA_ID_PATTERN)
    project_id: str = Field(min_length=1, max_length=200, pattern=_LEDGER_ID_PATTERN)
    role: PrincipalRole
    principal_session_id: str = Field(
        min_length=16,
        max_length=500,
        pattern=_SESSION_ID_PATTERN,
    )
    active: bool = True
    revision: int = Field(default=1, ge=1)
    created_by: str = Field(min_length=1, max_length=160, pattern=_OPHIRA_ID_PATTERN)
    created_at: datetime
    updated_at: datetime
    revoked_by: str | None = Field(
        default=None,
        min_length=1,
        max_length=160,
        pattern=_OPHIRA_ID_PATTERN,
    )
    revoked_at: datetime | None = None

    @model_validator(mode="after")
    def coherent_lifecycle(self) -> FeishuIdentityBinding:
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("binding timestamps must be timezone-aware")
        if self.updated_at.tzinfo is None or self.updated_at.utcoffset() is None:
            raise ValueError("binding timestamps must be timezone-aware")
        if self.active and (self.revoked_by is not None or self.revoked_at is not None):
            raise ValueError("active binding cannot carry revocation metadata")
        if not self.active and (self.revoked_by is None or self.revoked_at is None):
            raise ValueError("revoked binding requires complete revocation metadata")
        if self.revoked_at is not None:
            if self.revoked_at.tzinfo is None or self.revoked_at.utcoffset() is None:
                raise ValueError("binding timestamps must be timezone-aware")
            if self.revoked_at < self.created_at:
                raise ValueError("binding revocation cannot predate creation")
        if self.updated_at < self.created_at:
            raise ValueError("binding update cannot predate creation")
        return self

    @property
    def selector(self) -> FeishuIdentitySelector:
        return FeishuIdentitySelector(
            channel=self.channel,
            tenant_key=self.tenant_key,
            chat_id=self.chat_id,
            user_open_id=self.user_open_id,
        )

    @property
    def principal(self) -> PrincipalBinding:
        return PrincipalBinding(
            tenant_id=self.tenant_id,
            principal_id=self.principal_id,
            role=self.role,
            session_id=self.principal_session_id,
        )


class ResolvedFeishuIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    binding: FeishuIdentityBinding
    principal: PrincipalBinding


def feishu_identity_selector(payload: dict[str, Any]) -> FeishuIdentitySelector:
    """Extract the exact signed-body identity tuple; never inspect message text."""

    header = payload.get("header")
    event = payload.get("event")
    if (
        payload.get("schema") != "2.0"
        or not isinstance(header, dict)
        or header.get("event_type") != "im.message.receive_v1"
        or not isinstance(event, dict)
    ):
        raise ChannelIdentityPayloadError("Feishu event identity is missing")
    message = event.get("message")
    sender = event.get("sender")
    if not isinstance(message, dict) or not isinstance(sender, dict):
        raise ChannelIdentityPayloadError("Feishu event identity is missing")
    sender_id = sender.get("sender_id")
    if not isinstance(sender_id, dict):
        raise ChannelIdentityPayloadError("Feishu event identity is missing")
    try:
        return FeishuIdentitySelector(
            tenant_key=header.get("tenant_key"),
            chat_id=message.get("chat_id"),
            user_open_id=sender_id.get("open_id"),
        )
    except ValueError as exc:
        raise ChannelIdentityPayloadError("Feishu event identity is missing or invalid") from exc


class FeishuIdentityBindingStore:
    """Atomic binding history with one active target per channel identity tuple."""

    def __init__(
        self,
        root: Path,
        project_access: ProjectAccessStore,
        *,
        channel: Literal["feishu", "dingtalk"] = "feishu",
        clock=utc_now,
    ) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.root.chmod(0o700)
        except OSError:
            pass
        self.project_access = project_access
        self.channel = channel
        self._clock = clock
        self._process_lock = threading.RLock()

    @contextmanager
    def _locked(self) -> Iterator[None]:
        with self._process_lock:
            lock_path = self.root / ".bindings.lock"
            flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(lock_path, flags, 0o600)
                fcntl.flock(descriptor, fcntl.LOCK_EX)
            except OSError as exc:
                raise ChannelIdentityIntegrityError(
                    "channel identity binding store is unavailable"
                ) from exc
            try:
                yield
            finally:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(descriptor)

    def _path(self, binding_id: str) -> Path:
        if not _BINDING_ID.fullmatch(binding_id):
            raise ChannelIdentityNotFoundError("channel identity binding not found")
        return self.root / f"{binding_id}.json"

    @contextmanager
    def _project_lease(self, project_id: str) -> Iterator[None]:
        try:
            with self.project_access.lifecycle.lease(project_id):
                yield
        except ProjectUnavailableError as exc:
            raise ChannelIdentityNotFoundError("channel identity binding not found") from exc
        except ProjectLifecycleBusyError as exc:
            raise ChannelIdentityConflictError("channel identity project is busy") from exc

    def _read_path(self, path: Path) -> FeishuIdentityBinding:
        try:
            if path.is_symlink() or not path.is_file():
                raise OSError("invalid binding snapshot")
            record = FeishuIdentityBinding.model_validate_json(path.read_text(encoding="utf-8"))
            if record.channel != self.channel:
                raise ValueError("binding belongs to another channel")
            return record
        except (OSError, ValueError) as exc:
            raise ChannelIdentityIntegrityError(
                "channel identity binding failed integrity validation"
            ) from exc

    def _list_unlocked(self) -> list[FeishuIdentityBinding]:
        records: list[FeishuIdentityBinding] = []
        try:
            paths = sorted(self.root.glob("channel_binding_*.json"))
        except OSError as exc:
            raise ChannelIdentityIntegrityError(
                "channel identity binding store is unavailable"
            ) from exc
        for path in paths:
            record = self._read_path(path)
            if path.name != f"{record.id}.json":
                raise ChannelIdentityIntegrityError(
                    "channel identity binding failed integrity validation"
                )
            if not self.project_access.lifecycle.has_deletion(record.project_id):
                records.append(record)
        return records

    def _write(self, record: FeishuIdentityBinding, *, create: bool = False) -> None:
        with self._project_lease(record.project_id):
            self._write_available(record, create=create)

    def _write_available(self, record: FeishuIdentityBinding, *, create: bool = False) -> None:
        path = self._path(record.id)
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
                    raise ChannelIdentityConflictError(
                        "channel identity binding already exists"
                    ) from exc
            else:
                os.replace(temporary, path)
        except OSError as exc:
            raise ChannelIdentityIntegrityError(
                "channel identity binding could not be persisted"
            ) from exc
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _same_target(
        binding: FeishuIdentityBinding,
        payload: FeishuIdentityBindingCreate | DingTalkIdentityBindingCreate,
        tenant_id: str,
    ) -> bool:
        return (
            binding.tenant_id == tenant_id
            and binding.project_id == payload.project_id
            and binding.principal_id == payload.principal_id
            and binding.role == payload.role
        )

    def create(
        self,
        payload: FeishuIdentityBindingCreate | DingTalkIdentityBindingCreate,
        *,
        actor: PrincipalContext,
    ) -> FeishuIdentityBinding:
        """Create an idempotent binding after validating actor and target membership."""

        if payload.selector.channel != self.channel:
            raise ChannelIdentityPayloadError("binding payload belongs to another channel")
        with self._project_lease(payload.project_id):
            return self._create_available(payload, actor=actor)

    def _create_available(
        self,
        payload: FeishuIdentityBindingCreate | DingTalkIdentityBindingCreate,
        *,
        actor: PrincipalContext,
    ) -> FeishuIdentityBinding:

        if actor.role not in {PrincipalRole.OWNER, PrincipalRole.ADMIN}:
            raise AuthorizationError("tenant administrator authorization is required")
        self.project_access.authorize(
            payload.project_id,
            actor,
            ProjectPermission.PROJECT_ACCESS_MANAGE,
        )
        # The mapped role is never trusted from the request alone.  It must be
        # the current durable project role and capable of creating a Run.
        self.project_access.authorize_bound(
            payload.project_id,
            tenant_id=actor.tenant_id,
            principal_id=payload.principal_id,
            role=payload.role,
            permission=ProjectPermission.RUN_CREATE,
        )

        with self._locked():
            active = [
                item
                for item in self._list_unlocked()
                if item.active and item.selector == payload.selector
            ]
            if len(active) > 1:
                raise ChannelIdentityAmbiguousError("channel identity has multiple active bindings")
            if active:
                if self._same_target(active[0], payload, actor.tenant_id):
                    return active[0]
                raise ChannelIdentityConflictError(
                    "channel identity is already bound to a different target"
                )

            now = self._clock()
            binding_id = new_id("channel_binding")
            record = FeishuIdentityBinding(
                id=binding_id,
                channel=self.channel,
                tenant_key=payload.selector.tenant_key,
                chat_id=payload.selector.chat_id,
                user_open_id=payload.selector.user_open_id,
                tenant_id=actor.tenant_id,
                principal_id=payload.principal_id,
                project_id=payload.project_id,
                role=payload.role,
                principal_session_id=f"{self.channel}/{binding_id}",
                created_by=actor.principal_id,
                created_at=now,
                updated_at=now,
            )
            self._write(record, create=True)
            return record

    def list_for_tenant(
        self,
        tenant_id: str,
        *,
        include_revoked: bool = False,
    ) -> list[FeishuIdentityBinding]:
        with self._locked():
            records = [
                item
                for item in self._list_unlocked()
                if item.tenant_id == tenant_id and (include_revoked or item.active)
            ]
        return sorted(records, key=lambda item: (item.created_at, item.id))

    def get_for_tenant(
        self,
        binding_id: str,
        tenant_id: str,
    ) -> FeishuIdentityBinding:
        with self._locked():
            path = self._path(binding_id)
            if not path.exists():
                raise ChannelIdentityNotFoundError("channel identity binding not found")
            record = self._read_path(path)
        if record.tenant_id != tenant_id or self.project_access.lifecycle.has_deletion(
            record.project_id
        ):
            raise ChannelIdentityNotFoundError("channel identity binding not found")
        return record

    def revoke(
        self,
        binding_id: str,
        *,
        actor: PrincipalContext,
    ) -> FeishuIdentityBinding:
        if actor.role not in {PrincipalRole.OWNER, PrincipalRole.ADMIN}:
            raise AuthorizationError("tenant administrator authorization is required")
        with self._locked():
            path = self._path(binding_id)
            if not path.exists():
                raise ChannelIdentityNotFoundError("channel identity binding not found")
            current = self._read_path(path)
            if current.tenant_id != actor.tenant_id:
                raise ChannelIdentityNotFoundError("channel identity binding not found")
            self.project_access.authorize(
                current.project_id,
                actor,
                ProjectPermission.PROJECT_ACCESS_MANAGE,
            )
            if not current.active:
                return current
            now = self._clock()
            updated = current.model_copy(
                update={
                    "active": False,
                    "revision": current.revision + 1,
                    "updated_at": now,
                    "revoked_by": actor.principal_id,
                    "revoked_at": now,
                }
            )
            # model_copy does not rerun validators; validate before persistence.
            validated = FeishuIdentityBinding.model_validate(updated.model_dump())
            self._write(validated)
            return validated

    def resolve(self, selector: FeishuIdentitySelector) -> ResolvedFeishuIdentity:
        """Resolve one active mapping and revalidate its current project role."""

        if selector.channel != self.channel:
            raise ChannelIdentityPayloadError("selector belongs to another channel")
        with self._locked():
            matches = [item for item in self._list_unlocked() if item.selector == selector]
            active = [item for item in matches if item.active]
            if len(active) > 1:
                raise ChannelIdentityAmbiguousError("channel identity has multiple active bindings")
            if not active:
                if matches:
                    raise ChannelIdentityRevokedError("channel identity binding is revoked")
                raise ChannelIdentityNotFoundError("channel identity binding not found")
            binding = active[0]
            # Keep the binding lock through membership revalidation. A revoke
            # that has already returned can therefore never race with a stale
            # successful resolution.
            self.project_access.authorize_bound(
                binding.project_id,
                tenant_id=binding.tenant_id,
                principal_id=binding.principal_id,
                role=binding.role,
                permission=ProjectPermission.RUN_CREATE,
            )
            return ResolvedFeishuIdentity(binding=binding, principal=binding.principal)


# Existing Feishu names remain import-compatible with historical records and APIs.
ChannelIdentitySelector = FeishuIdentitySelector
ChannelIdentityBinding = FeishuIdentityBinding
ChannelIdentityBindingStore = FeishuIdentityBindingStore
ResolvedChannelIdentity = ResolvedFeishuIdentity
