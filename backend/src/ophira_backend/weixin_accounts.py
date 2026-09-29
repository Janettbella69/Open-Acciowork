"""Host-approved personal Weixin bindings; credentials never enter the ledger.

Pairing alone does not authorize a Run. The operator binds the scanning owner to
one project and explicitly enables it. Every admission and reply resolves that
binding again. Cross-process transactions are short; network leases are separate.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Literal
from uuid import uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from .auth import AuthorizationError, ProjectAccessStore, ProjectPermission
from .constants import PrincipalRole
from .models import PrincipalBinding, PrincipalContext, utc_now
from .providers import FileSecretVault
from .skill_proposal_files import child_directory, directory_fd, read_at
from .storage import FileLedger
from .weixin_transport import WEIXIN_API_ORIGIN

DEFAULT_BASE_URL = WEIXIN_API_ORIGIN
_ID = re.compile(r"weixin_[a-f0-9]{32}")
_CONTEXT_ID = re.compile(r"wxctx_[a-f0-9]{64}")
MAX_STATE_BYTES = 4 * 1024 * 1024
MAX_BUNDLE_BYTES = 1024 * 1024
CONTEXT_TTL = timedelta(hours=24)
PAIRING_TTL = timedelta(minutes=5)
ACTIVE_PAIRING_STATES = frozenset({"wait", "scaned", "need_verifycode", "verify_code_blocked"})


class WeixinStoreError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _wire_string(value, maximum: int, *, allow_empty: bool = False) -> bool:
    return (
        isinstance(value, str) and (allow_empty or bool(value)) and len(value) <= maximum
        and not any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in value)
    )


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _strict_json(raw: str | bytes, maximum: int, error: str):
    try:
        encoded = raw.encode() if isinstance(raw, str) else raw
        if len(encoded) > maximum:
            raise ValueError
        return json.loads(encoded, object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError, RecursionError, TypeError):
        raise WeixinStoreError(error) from None


class WeixinAccount(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^weixin_[a-f0-9]{32}$")
    bot_id: str = Field(min_length=1, max_length=256)
    owner_user_id: str = Field(min_length=1, max_length=256)
    base_url: Literal["https://ilinkai.weixin.qq.com"] = DEFAULT_BASE_URL
    tenant_id: str | None = None
    principal_binding: PrincipalBinding | None = None
    project_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,200}$")
    enabled: bool = Field(default=False, strict=True)
    enabled_at: AwareDatetime | None = None
    revision: int = Field(default=1, ge=1, strict=True)
    credential_revision: int = Field(default=1, ge=1, strict=True)
    status: Literal["connected", "revoked", "expired"] = "connected"
    last_error_code: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_]{1,100}$")

    @model_validator(mode="after")
    def coherent_scope(self):
        if (not _wire_string(self.bot_id, 256)
                or not _wire_string(self.owner_user_id, 256)
                or not self.owner_user_id.endswith("@im.wechat")):
            raise ValueError("invalid Weixin identity")
        if self.tenant_id is not None and not _wire_string(self.tenant_id, 160):
            raise ValueError("invalid tenant")
        if self.principal_binding is not None:
            if self.principal_binding.tenant_id != self.tenant_id:
                raise ValueError("inconsistent tenant binding")
        if self.project_id is not None and self.tenant_id is not None:
            if self.principal_binding is None:
                raise ValueError("bound tenant account requires principal")
        if self.enabled and (self.status != "connected" or self.project_id is None):
            raise ValueError("enabled account requires connected project")
        if self.enabled != (self.enabled_at is not None):
            raise ValueError("enabled account requires admission time boundary")
        return self


class WeixinPairing(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^weixin_[a-f0-9]{32}$")
    principal_binding: PrincipalBinding | None = None
    expires_at: AwareDatetime
    status: Literal[
        "wait", "scaned", "need_verifycode", "verify_code_blocked", "confirmed",
        "expired", "cancelled", "unsupported_redirect", "binded_redirect",
    ] = "wait"
    account_id: str | None = Field(default=None, pattern=r"^weixin_[a-f0-9]{32}$")

    @model_validator(mode="after")
    def coherent_result(self):
        if (self.status == "confirmed") != (self.account_id is not None):
            raise ValueError("pairing confirmation requires account")
        return self


class _State(BaseModel):
    model_config = ConfigDict(extra="forbid")
    accounts: dict[str, WeixinAccount] = Field(default_factory=dict)
    pairings: dict[str, WeixinPairing] = Field(default_factory=dict)
    polls: dict[str, dict] = Field(default_factory=dict)


class WeixinStore:
    def __init__(self, ledger: FileLedger, vault: FileSecretVault, access: ProjectAccessStore, *,
                 identity_mode: str = "local_only", trusted_tenant_id: str | None = None):
        if identity_mode not in {"local_only", "trusted_gateway"}:
            raise WeixinStoreError("IDENTITY_MODE_INVALID")
        if identity_mode == "trusted_gateway" and not _wire_string(trusted_tenant_id, 160):
            raise WeixinStoreError("IDENTITY_MODE_INVALID")
        self.ledger, self.vault, self.access = ledger, vault, access
        self.identity_mode = identity_mode
        self.trusted_tenant_id = trusted_tenant_id

    @contextmanager
    def _directory(self):
        with directory_fd(self.ledger.root) as root:
            current = os.dup(root)
            try:
                for name in ("channels", "weixin"):
                    try:
                        os.mkdir(name, 0o700, dir_fd=current)
                        os.fsync(current)
                    except FileExistsError:
                        pass
                    with child_directory(current, (name,)) as child:
                        next_fd = os.dup(child)
                    os.close(current)
                    current = next_fd
                yield current
            finally:
                os.close(current)

    def _read(self) -> _State:
        with self._directory() as parent:
            try:
                raw = read_at(parent, "state.json", MAX_STATE_BYTES)
            except FileNotFoundError:
                return _State()
        try:
            state = _State.model_validate(_strict_json(raw, MAX_STATE_BYTES, "STATE_INVALID"))
        except ValueError:
            raise WeixinStoreError("STATE_INVALID") from None
        self._validate_state(state)
        return state

    @staticmethod
    def _validate_state(state: _State):
        if (len(state.accounts) > 100 or len(state.pairings) > 100
                or set(state.polls) - set(state.accounts)
                or any(key != row.id for key, row in state.accounts.items())
                or any(key != row.id for key, row in state.pairings.items())
                or len({a.bot_id for a in state.accounts.values()}) != len(state.accounts)
                or any(p.account_id is not None and p.account_id not in state.accounts
                       for p in state.pairings.values())):
            raise WeixinStoreError("STATE_INVALID")
        for poll in state.polls.values():
            if (not isinstance(poll, dict) or set(poll) != {"cursor"}
                    or not _wire_string(poll["cursor"], 65536, allow_empty=True)):
                raise WeixinStoreError("STATE_INVALID")

    def _write(self, state: _State):
        # model_copy(update=...) deliberately skips Pydantic validation.
        try:
            state = _State.model_validate(state.model_dump())
        except ValueError:
            raise WeixinStoreError("STATE_INVALID") from None
        self._validate_state(state)
        raw = state.model_dump_json().encode()
        if len(raw) > MAX_STATE_BYTES:
            raise WeixinStoreError("STATE_LIMIT")
        with self._directory() as parent:
            temporary = uuid4().hex + ".tmp"
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=parent)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(raw)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, "state.json", src_dir_fd=parent, dst_dir_fd=parent)
                os.fsync(parent)
            finally:
                try:
                    os.unlink(temporary, dir_fd=parent)
                except FileNotFoundError:
                    pass

    def _lock(self):
        return self.ledger._lock_for("weixin:accounts")

    @contextmanager
    def poll_lease(self, account_id: str):
        """Nonblocking file lease; safe across await and shared by API workers."""
        if not _ID.fullmatch(account_id):
            raise WeixinStoreError("INVALID_ID")
        with self._directory() as parent:
            # Bounded stable lock inodes: expired login IDs must not leave an
            # unbounded directory, and deleting a live lock would break exclusion.
            slot = int(hashlib.sha256(account_id.encode()).hexdigest()[:8], 16) % 4096
            fd = os.open(f"poll-{slot:04d}.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                         0o600, dir_fd=parent)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise WeixinStoreError("LEASE_INVALID")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
            else:
                yield True
        finally:
            os.close(fd)

    @staticmethod
    def _account(state: _State, account_id: str, expected_revision: int | None = None):
        account = state.accounts.get(account_id)
        if account is None:
            raise WeixinStoreError("ACCOUNT_NOT_FOUND")
        if expected_revision is not None and (
            type(expected_revision) is not int or expected_revision < 1
            or account.revision != expected_revision
        ):
            raise WeixinStoreError("REVISION_CONFLICT")
        return account

    def get_account(self, account_id: str) -> WeixinAccount:
        with self._lock():
            return self._account(self._read(), account_id)

    def list_accounts(self) -> list[WeixinAccount]:
        with self._lock():
            return list(self._read().accounts.values())

    def active_accounts(self) -> list[WeixinAccount]:
        return [a for a in self.list_accounts() if a.enabled and a.status == "connected"
                and self._in_deployment(a)]

    def _in_deployment(self, account: WeixinAccount) -> bool:
        expected = self.trusted_tenant_id if self.identity_mode == "trusted_gateway" else None
        return account.tenant_id == expected

    def _deployment_scope(self, account: WeixinAccount):
        if not self._in_deployment(account):
            raise AuthorizationError("Weixin account is outside this deployment")

    @staticmethod
    def _management_actor(actor: PrincipalContext | None):
        if actor is not None and (
            actor.role not in {PrincipalRole.OWNER, PrincipalRole.ADMIN}
            or actor.assertion_expires_at <= utc_now()
        ):
            raise AuthorizationError("Weixin management requires a current administrator")

    def _actor_scope(self, actor: PrincipalContext | None):
        self._management_actor(actor)
        if self.identity_mode == "trusted_gateway":
            if actor is None or actor.tenant_id != self.trusted_tenant_id:
                raise AuthorizationError("Weixin management is outside this deployment")
        elif actor is not None:
            raise AuthorizationError("Weixin management is outside local deployment")

    def check_scope(self, account: WeixinAccount, actor: PrincipalContext | None):
        self._actor_scope(actor)
        self._deployment_scope(account)
        if account.tenant_id != (actor.tenant_id if actor else None):
            raise AuthorizationError("Weixin account is outside this scope")

    def _authorize(self, account: WeixinAccount):
        if not account.project_id:
            raise WeixinStoreError("PROJECT_REQUIRED")
        project = self.ledger.get_project(account.project_id)
        principal = account.principal_binding
        if principal is None:
            if account.tenant_id is not None or project.identity_binding != "local_only":
                raise AuthorizationError("Weixin project is outside local scope")
        else:
            if (project.identity_binding != "authenticated_principal"
                    or project.tenant_id != account.tenant_id
                    or principal.tenant_id != account.tenant_id):
                raise AuthorizationError("Weixin project is outside principal scope")
            self.access.authorize_bound(
                project.id, tenant_id=principal.tenant_id, principal_id=principal.principal_id,
                role=principal.role, permission=ProjectPermission.RUN_CREATE,
            )

    def bind(self, account_id: str, *, project_id: str | None, enabled: bool,
             expected_revision: int, actor: PrincipalContext | None) -> WeixinAccount:
        with self._lock():
            state = self._read()
            current = self._account(state, account_id)
            self.check_scope(current, actor)
            self._account(state, account_id, expected_revision)
            if current.status != "connected":
                raise WeixinStoreError("ACCOUNT_UNAVAILABLE")
            account = current.model_copy(update={
                "project_id": project_id, "enabled": enabled, "revision": current.revision + 1,
                "enabled_at": utc_now() if enabled else None,
                "principal_binding": PrincipalBinding.from_context(actor) if actor else None,
                "last_error_code": None,
            })
            if project_id is not None or enabled:
                self._authorize(account)
            # A failed context deletion must not publish the replacement binding.
            self._clear_contexts(account_id)
            state.accounts[account_id] = account
            # Tokens issued for the old binding cannot authorize the new one.
            self._write(state)
            return account

    def detach_project(self, project_id: str):
        """Deletion owns the exclusive lifecycle lease; retain only account login."""
        with self._lock():
            state = self._read()
            changed = False
            for account in state.accounts.values():
                if account.project_id != project_id:
                    continue
                # Clear first. If interrupted, metadata still points at this
                # project and the deletion retry can find and finish every row.
                self._clear_contexts(account.id, allow_missing=True)
                state.accounts[account.id] = account.model_copy(update={
                    "enabled": False, "project_id": None, "principal_binding": None,
                    "enabled_at": None,
                    "revision": account.revision + 1,
                })
                state.polls.pop(account.id, None)
                changed = True
            if changed:
                self._write(state)

    def resolve(self, account_id: str, sender_user_id: str, expected_revision: int | None = None):
        with self._lock():
            account = self._account(self._read(), account_id, expected_revision)
            self._deployment_scope(account)
            if not account.enabled or account.status != "connected":
                raise WeixinStoreError("ACCOUNT_DISABLED")
            if sender_user_id != account.owner_user_id:
                raise WeixinStoreError("SENDER_UNBOUND")
            self._authorize(account)
            return account, account.principal_binding

    def _bundle(self, account_id: str) -> dict:
        raw = self.vault.get(account_id)
        if not raw:
            raise WeixinStoreError("CREDENTIAL_MISSING")
        bundle = _strict_json(raw, MAX_BUNDLE_BYTES, "CREDENTIAL_INVALID")
        if (not isinstance(bundle, dict) or set(bundle) != {"token", "contexts"}
                or not _wire_string(bundle["token"], 8192)
                or not bundle["token"].isascii()
                or any(c.isspace() for c in bundle["token"])
                or not isinstance(bundle["contexts"], dict)
                or len(bundle["contexts"]) > 1000):
            raise WeixinStoreError("CREDENTIAL_INVALID")
        for key, value in bundle["contexts"].items():
            if (not _CONTEXT_ID.fullmatch(key) or not isinstance(value, dict)
                    or set(value) != {"token", "revision", "expires_at"}
                    or not _wire_string(value["token"], 8192)
                    or type(value["revision"]) is not int or value["revision"] < 1):
                raise WeixinStoreError("CREDENTIAL_INVALID")
            try:
                expires = datetime.fromisoformat(value["expires_at"])
                if expires.tzinfo is None or expires.utcoffset() is None:
                    raise ValueError
            except (ValueError, TypeError, OverflowError):
                raise WeixinStoreError("CREDENTIAL_INVALID") from None
        return bundle

    def _save_bundle(self, account_id: str, bundle: dict):
        raw = json.dumps(bundle, ensure_ascii=False, separators=(",", ":"))
        if len(raw.encode()) > MAX_BUNDLE_BYTES:
            raise WeixinStoreError("CONTEXT_LIMIT")
        self.vault.set(account_id, raw)

    def credentials(self, account_id: str, expected_revision: int) -> str:
        with self._lock():
            account = self._account(self._read(), account_id, expected_revision)
            self._deployment_scope(account)
            if not account.enabled or account.status != "connected":
                raise WeixinStoreError("ACCOUNT_DISABLED")
            self._authorize(account)
            return self._bundle(account_id)["token"]

    def _clear_contexts(self, account_id: str, *, allow_missing: bool = False):
        try:
            bundle = self._bundle(account_id)
        except WeixinStoreError as exc:
            if allow_missing and exc.code == "CREDENTIAL_MISSING":
                return
            raise
        if bundle["contexts"]:
            bundle["contexts"] = {}
            self._save_bundle(account_id, bundle)

    def save_context(self, account_id: str, event_id: str, context_token: str,
                     expected_revision: int) -> str:
        if (not _wire_string(context_token, 8192) or not _wire_string(event_id, 256)
                or type(expected_revision) is not int or expected_revision < 1):
            raise WeixinStoreError("CONTEXT_INVALID")
        with self._lock():
            account = self._account(self._read(), account_id, expected_revision)
            self._deployment_scope(account)
            if not account.enabled or account.status != "connected":
                raise WeixinStoreError("ACCOUNT_DISABLED")
            self._authorize(account)
            ref = "wxctx_" + hashlib.sha256(
                f"{account_id}:{account.revision}:{event_id}".encode(),
            ).hexdigest()
            bundle = self._bundle(account_id)
            contexts = bundle["contexts"]
            now = utc_now()
            contexts = {key: value for key, value in contexts.items()
                        if datetime.fromisoformat(value["expires_at"]) > now}
            existing = contexts.get(ref)
            if existing is not None and existing["token"] != context_token:
                raise WeixinStoreError("CONTEXT_CONFLICT")
            if ref not in contexts and len(contexts) >= 1000:
                raise WeixinStoreError("CONTEXT_LIMIT")
            contexts.setdefault(ref, {"token": context_token, "revision": account.revision,
                                      "expires_at": (now + CONTEXT_TTL).isoformat()})
            bundle["contexts"] = contexts
            self._save_bundle(account_id, bundle)
            return ref

    def reply_context(self, account_id: str, context_ref: str, expected_revision: int) -> str:
        with self._lock():
            account = self._account(self._read(), account_id, expected_revision)
            self._deployment_scope(account)
            if not account.enabled or account.status != "connected":
                raise WeixinStoreError("ACCOUNT_DISABLED")
            self._authorize(account)
            context = self._bundle(account_id)["contexts"].get(context_ref)
            if (context is None or context["revision"] != expected_revision
                    or datetime.fromisoformat(context["expires_at"]) <= utc_now()):
                raise WeixinStoreError("CONTEXT_EXPIRED")
            return context["token"]

    def revoke(self, account_id: str, expected_revision: int, actor: PrincipalContext | None):
        with self._lock():
            state = self._read()
            account = self._account(state, account_id)
            self.check_scope(account, actor)
            self._account(state, account_id, expected_revision)
            account = account.model_copy(update={"enabled": False, "enabled_at": None,
                                                 "status": "revoked",
                                                 "revision": account.revision + 1})
            state.accounts[account_id] = account
            state.polls.pop(account_id, None)
            self._write(state)  # fail closed before credential deletion, including crash recovery
            self.vault.delete(account_id)
            return account

    def set_error(self, account_id: str, code: str | None, expected_revision: int | None = None):
        if code is not None and re.fullmatch(r"[A-Za-z0-9_]{1,100}", code) is None:
            code = "CHANNEL_ERROR"
        with self._lock():
            state = self._read()
            current = self._account(state, account_id, expected_revision)
            changes = {"last_error_code": code}
            if code == "SESSION_EXPIRED":
                changes.update(enabled=False, enabled_at=None, status="expired",
                               revision=current.revision + 1)
            state.accounts[account_id] = current.model_copy(update=changes)
            self._write(state)

    def get_poll_state(self, account_id: str) -> dict:
        with self._lock():
            state = self._read()
            self._account(state, account_id)
            return state.polls.get(account_id, {"cursor": ""}).copy()

    def save_poll_state(self, account_id: str, value: dict, expected_revision: int):
        if not isinstance(value, dict) or set(value) != {"cursor"}:
            raise WeixinStoreError("CURSOR_INVALID")
        if not _wire_string(value["cursor"], 65536, allow_empty=True):
            raise WeixinStoreError("CURSOR_LIMIT")
        with self._lock():
            state = self._read()
            account = self._account(state, account_id, expected_revision)
            self._deployment_scope(account)
            if not account.enabled or account.status != "connected":
                raise WeixinStoreError("ACCOUNT_DISABLED")
            state.polls[account_id] = value.copy()
            self._write(state)

    def _cleanup(self, state: _State):
        now = utc_now()
        changed = False
        for key, pairing in list(state.pairings.items()):
            if pairing.expires_at <= now:
                self.vault.delete(key)
                state.pairings.pop(key)
                changed = True
        for account in state.accounts.values():
            if account.status == "revoked":
                self.vault.delete(account.id)
                continue
            try:
                bundle = self._bundle(account.id)
            except WeixinStoreError as exc:
                if exc.code == "CREDENTIAL_MISSING":
                    continue
                raise
            contexts = {
                key: value for key, value in bundle["contexts"].items()
                if datetime.fromisoformat(value["expires_at"]) > now
                and value["revision"] == account.revision
                and account.status == "connected" and account.enabled
            }
            if len(contexts) != len(bundle["contexts"]):
                bundle["contexts"] = contexts
                self._save_bundle(account.id, bundle)
        return changed

    def cleanup(self):
        with self._lock():
            state = self._read()
            if self._cleanup(state):
                self._write(state)

    def create_pairing(self, qrcode: str, content: str, actor: PrincipalContext | None):
        self._actor_scope(actor)
        if not _wire_string(qrcode, 8192) or not _wire_string(content, 16384):
            raise WeixinStoreError("PAIRING_INVALID")
        with self._lock():
            self._actor_scope(actor)
            state = self._read()
            self._cleanup(state)
            if len(state.pairings) >= 20:
                raise WeixinStoreError("PAIRING_LIMIT")
            pairing = WeixinPairing(id="weixin_" + uuid4().hex,
                principal_binding=PrincipalBinding.from_context(actor) if actor else None,
                expires_at=utc_now() + PAIRING_TTL)
            state.pairings[pairing.id] = pairing
            self._write(state)
            self.vault.set(pairing.id, json.dumps({"qrcode": qrcode, "content": content}))
            return pairing

    def _pairing_scope(self, pairing: WeixinPairing, actor: PrincipalContext | None):
        self._actor_scope(actor)
        binding = pairing.principal_binding
        if ((binding is None) != (actor is None)
                or (binding is not None and actor is not None and (
                    binding.tenant_id != actor.tenant_id
                    or binding.principal_id != actor.principal_id
                    or binding.session_id != actor.session_id))):
            raise AuthorizationError("Pairing is outside this session")

    def _pairing(self, state: _State, pairing_id: str, actor: PrincipalContext | None):
        pairing = state.pairings.get(pairing_id)
        if pairing is None:
            raise WeixinStoreError("PAIRING_NOT_FOUND")
        self._pairing_scope(pairing, actor)
        if pairing.status in ACTIVE_PAIRING_STATES and pairing.expires_at <= utc_now():
            pairing = pairing.model_copy(update={"status": "expired"})
            state.pairings[pairing.id] = pairing
            self._write(state)
            self.vault.delete(pairing.id)
        return pairing

    def get_pairing(self, pairing_id: str, actor: PrincipalContext | None):
        with self._lock():
            return self._pairing(self._read(), pairing_id, actor)

    def pairing_secret(self, pairing_id: str, actor: PrincipalContext | None) -> dict:
        with self._lock():
            pairing = self.get_pairing(pairing_id, actor)
            if pairing.status not in ACTIVE_PAIRING_STATES:
                raise WeixinStoreError("PAIRING_CLOSED")
            raw = self.vault.get(pairing_id)
            if raw is None:
                raise WeixinStoreError("PAIRING_CLOSED")
            secret = _strict_json(raw, 128 * 1024, "PAIRING_INVALID")
            if (not isinstance(secret, dict) or set(secret) != {"qrcode", "content"}
                    or not _wire_string(secret["qrcode"], 8192)
                    or not _wire_string(secret["content"], 16384)):
                raise WeixinStoreError("PAIRING_INVALID")
            return secret

    def update_pairing(self, pairing_id: str, actor: PrincipalContext | None, status: str):
        with self._lock():
            state = self._read()
            pairing = self._pairing(state, pairing_id, actor)
            if pairing.status == status:
                return pairing
            if pairing.status not in ACTIVE_PAIRING_STATES or status == "confirmed":
                raise WeixinStoreError("PAIRING_CLOSED")
            pairing = WeixinPairing.model_validate({**pairing.model_dump(), "status": status})
            state.pairings[pairing_id] = pairing
            self._write(state)
            if status in {"expired", "cancelled", "unsupported_redirect", "binded_redirect"}:
                self.vault.delete(pairing_id)
            return pairing

    def cancel_pairing(self, pairing_id: str, actor: PrincipalContext | None):
        """Cancellation and confirmation serialize; terminal outcomes remain true."""
        with self._lock():
            pairing = self._pairing(self._read(), pairing_id, actor)
            if pairing.status not in ACTIVE_PAIRING_STATES:
                return pairing
            return self.update_pairing(pairing_id, actor, "cancelled")

    def confirm_pairing(self, pairing_id: str, actor: PrincipalContext | None, *,
                        bot_id: str, owner_user_id: str, token: str, base_url: str):
        if (not _wire_string(token, 8192) or not token.isascii()
                or any(c.isspace() for c in token)):
            raise WeixinStoreError("CREDENTIAL_INVALID")
        with self._lock():
            pairing = self.get_pairing(pairing_id, actor)
            if pairing.status not in ACTIVE_PAIRING_STATES:
                raise WeixinStoreError("PAIRING_CLOSED")
            state = self._read()
            previous = next((a for a in state.accounts.values() if a.bot_id == bot_id), None)
            if previous:
                self.check_scope(previous, actor)
                if previous.owner_user_id != owner_user_id:
                    raise WeixinStoreError("OWNER_CONFLICT")
            elif len(state.accounts) >= 100:
                raise WeixinStoreError("ACCOUNT_LIMIT")
            account = WeixinAccount(
                id=previous.id if previous else "weixin_" + uuid4().hex,
                bot_id=bot_id, owner_user_id=owner_user_id, base_url=base_url,
                tenant_id=actor.tenant_id if actor else None,
                revision=previous.revision + 1 if previous else 1,
                credential_revision=previous.credential_revision + 1 if previous else 1,
                status="revoked",  # interrupted credential replacement always stays disabled
            )
            state.accounts[account.id] = account
            state.polls.pop(account.id, None)
            self._write(state)
            self._save_bundle(account.id, {"token": token, "contexts": {}})
            account = account.model_copy(update={"status": "connected"})
            state.accounts[account.id] = account
            pairing = pairing.model_copy(update={"status": "confirmed", "account_id": account.id})
            state.pairings[pairing.id] = pairing
            self._write(state)
            self.vault.delete(pairing.id)
            return pairing
