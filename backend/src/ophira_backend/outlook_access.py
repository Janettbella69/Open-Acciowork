"""Human-owned Outlook mailbox grants and receipt-backed main Agent reads."""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .auth import AuthorizationError, ProjectAccessStore, ProjectPermission
from .config import Settings
from .connector_run_access import authorize_connector_run
from .connectors import ConnectorNotFoundError
from .constants import TOOL_READ_OUTLOOK, EvidenceSourceType, PrincipalRole
from .models import PrincipalContext, new_id, utc_now
from .outlook import OutlookError, OutlookService
from .storage import FileLedger
from .untrusted import UNTRUSTED_LIMITS, UntrustedSource, sanitize_untrusted_payload

ID_PATTERN = r"^[A-Za-z0-9_-]{1,160}$"
GRANT_PATTERN = r"^outlook_grant_[a-f0-9]{32}$"
MAX_GRANTS = 1000


class OutlookGrantRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    expected_revision: int = Field(ge=0)
    connector_id: str = Field(pattern=ID_PATTERN)
    expires_in_seconds: int = Field(default=7 * 86400, ge=60, le=30 * 86400)


class OutlookRevokeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    expected_revision: int = Field(ge=1)


class OutlookGrant(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: new_id("outlook_grant"), pattern=GRANT_PATTERN)
    connector_id: str = Field(pattern=ID_PATTERN)
    connector_revision: int = Field(ge=0)
    user_id: str = Field(min_length=1, max_length=255)
    mailbox: str = Field(min_length=3, max_length=255)
    created_by: str = Field(max_length=160)
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None
    revoked_by: str | None = Field(default=None, max_length=160)

    @model_validator(mode="after")
    def coherent(self):
        if any(instant is not None and instant.tzinfo is None for instant in (
            self.created_at, self.expires_at, self.revoked_at,
        )) or self.expires_at <= self.created_at:
            raise ValueError("Invalid Outlook grant time")
        if (self.revoked_at is None) != (self.revoked_by is None):
            raise ValueError("Invalid Outlook revocation")
        return self

    def active(self, now: datetime) -> bool:
        return self.revoked_at is None and self.expires_at > now


class OutlookAccessRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str = Field(pattern=ID_PATTERN)
    identity_binding: Literal["local_only", "authenticated_principal"]
    tenant_id: str | None
    revision: int = Field(default=0, ge=0)
    grants: list[OutlookGrant] = Field(default_factory=list, max_length=MAX_GRANTS)

    @model_validator(mode="after")
    def unique_grants(self):
        if len({item.id for item in self.grants}) != len(self.grants):
            raise ValueError("Duplicate Outlook grants")
        return self


class OutlookAgentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    operation: Literal["connections", "messages"]
    grant_id: str | None = Field(default=None, pattern=GRANT_PATTERN)
    page_size: int = Field(default=5, ge=1, le=5)
    offset: int = Field(default=0, ge=0, le=MAX_GRANTS)
    page_token: str | None = Field(default=None, min_length=1, max_length=4096)

    @model_validator(mode="after")
    def arguments(self):
        if self.operation == "connections" and (self.grant_id or self.page_token):
            raise ValueError("Connection discovery takes no message arguments")
        if self.operation == "messages" and (not self.grant_id or self.offset):
            raise ValueError("Message reads require a mailbox grant and no offset")
        return self


class OutlookAccessService:
    def __init__(
        self, ledger: FileLedger, outlook: OutlookService, *, settings: Settings,
        access: ProjectAccessStore, clock=utc_now,
    ) -> None:
        self.ledger = ledger
        self.outlook = outlook
        self.settings = settings
        self.access = access
        self.clock = clock
        self.root = ledger.root / "auth" / "outlook"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _project(self, project_id: str):
        project = self.ledger.get_project(project_id)
        if self.settings.auth_mode == "local_only":
            valid = project.identity_binding == "local_only"
        else:
            valid = (
                project.identity_binding == "authenticated_principal"
                and project.tenant_id == self.settings.trusted_gateway_tenant_id
            )
        if not valid:
            raise AuthorizationError("Outlook access is outside deployment scope")
        return project

    def authorize_admin(self, project_id: str, principal: PrincipalContext | None) -> str:
        project = self._project(project_id)
        if project.identity_binding == "local_only":
            if principal is not None:
                raise AuthorizationError("Local access does not accept a principal")
            return "local_owner"
        if principal is None:
            raise AuthorizationError("Outlook access requires a project administrator")
        if principal.role not in {PrincipalRole.OWNER, PrincipalRole.ADMIN}:
            raise AuthorizationError("Outlook access requires a tenant administrator")
        self.access.authorize(project_id, principal, ProjectPermission.PROJECT_ACCESS_MANAGE)
        return principal.principal_id

    def get(self, project_id: str) -> OutlookAccessRecord:
        with self.ledger.lifecycle.lease(project_id):
            project = self._project(project_id)
            path = self.root / f"{project.id}.json"
            if not path.exists() and not path.is_symlink():
                return OutlookAccessRecord(
                    project_id=project.id, identity_binding=project.identity_binding,
                    tenant_id=project.tenant_id,
                )
            try:
                if path.is_symlink() or not path.is_file() or path.stat().st_size > 2_000_000:
                    raise ValueError
                record = OutlookAccessRecord.model_validate_json(path.read_text("utf-8"))
                if (record.project_id, record.identity_binding, record.tenant_id) != (
                    project.id, project.identity_binding, project.tenant_id,
                ):
                    raise ValueError
                return record
            except (OSError, ValueError):
                raise OutlookError(
                    "OUTLOOK_ACCESS_INVALID", "Project Outlook access is invalid"
                ) from None

    def view(self, project_id: str) -> dict:
        record = self.get(project_id)
        return {
            "project_id": record.project_id, "revision": record.revision,
            "grants": [
                {**item.model_dump(mode="json"), "active": item.active(self.clock())}
                for item in record.grants
            ],
        }

    def _revision(self, project_id: str, expected: int) -> OutlookAccessRecord:
        record = self.get(project_id)
        if record.revision != expected:
            raise OutlookError(
                "OUTLOOK_ACCESS_CONFLICT", "Project access changed; refresh and retry"
            )
        return record

    @contextmanager
    def _identity_transaction(self, project_id: str):
        with (
            self.ledger.lifecycle.lease(project_id),
            self.access._transaction(project_id)
            if self.settings.auth_mode == "trusted_gateway" else nullcontext(),
        ):
            yield

    def _mailbox(self, connector_id: str, tenant_id: str | None) -> tuple[int, str, str]:
        try:
            connector = self.outlook.connectors.get(connector_id)
        except (ConnectorNotFoundError, ValueError):
            raise OutlookError(
                "OUTLOOK_CONNECTOR_UNAVAILABLE", "Outlook read-only connector is unavailable"
            ) from None
        if (
            connector.type != "outlook" or not connector.enabled
            or not {"User.Read", "Mail.Read"}.issubset(set(connector.scope.split()))
        ):
            raise OutlookError(
                "OUTLOOK_CONNECTOR_UNAVAILABLE", "Outlook read-only connector is unavailable"
            )
        user_id, email = self.outlook.account(connector_id, tenant_id)
        return connector.revision, user_id, email

    def grant(
        self, project_id: str, payload: OutlookGrantRequest,
        principal: PrincipalContext | None,
    ) -> dict:
        with (
            self._identity_transaction(project_id),
            self.ledger._lock_for(f"outlook-access:{project_id}"),
        ):
            actor = self.authorize_admin(project_id, principal)
            record = self._revision(project_id, payload.expected_revision)
            revision, user_id, mailbox = self._mailbox(payload.connector_id, record.tenant_id)
            if len(record.grants) >= MAX_GRANTS:
                raise OutlookError("OUTLOOK_ACCESS_LIMIT", "Project Outlook grant history is full")
            now = self.clock()
            grants = [
                item.model_copy(update={"revoked_at": now, "revoked_by": actor})
                if item.active(now) and item.connector_id == payload.connector_id else item
                for item in record.grants
            ]
            grants.append(OutlookGrant(
                connector_id=payload.connector_id, connector_revision=revision,
                user_id=user_id,
                mailbox=mailbox, created_by=actor, created_at=now,
                expires_at=now + timedelta(seconds=payload.expires_in_seconds),
            ))
            self.ledger._atomic_write(self.root / f"{project_id}.json", record.model_copy(
                update={"revision": record.revision + 1, "grants": grants},
            ))
            return self.view(project_id)

    def revoke(
        self, project_id: str, grant_id: str, expected_revision: int,
        principal: PrincipalContext | None,
    ) -> dict:
        with (
            self._identity_transaction(project_id),
            self.ledger._lock_for(f"outlook-access:{project_id}"),
        ):
            actor = self.authorize_admin(project_id, principal)
            record = self._revision(project_id, expected_revision)
            if not any(item.id == grant_id for item in record.grants):
                raise OutlookError("OUTLOOK_ACCESS_NOT_FOUND", "Mailbox grant not found", 404)
            now = self.clock()
            grants = [
                item.model_copy(update={"revoked_at": now, "revoked_by": actor})
                if item.id == grant_id and item.revoked_at is None else item
                for item in record.grants
            ]
            self.ledger._atomic_write(self.root / f"{project_id}.json", record.model_copy(
                update={"revision": record.revision + 1, "grants": grants},
            ))
            return self.view(project_id)

    async def read_for_run(
        self, project_id: str, run_id: str, request: OutlookAgentRequest,
    ) -> dict:
        with self.ledger.lifecycle.lease(project_id):
            project = self._project(project_id)
            run = authorize_connector_run(self.ledger, self.access, project, run_id)
            if request.operation == "connections":
                grants = [item for item in self.get(project_id).grants
                          if item.active(self.clock())]
                page = grants[request.offset:request.offset + request.page_size]
                authorize_connector_run(self.ledger, self.access, project, run_id)
                return {"connections": [{
                    "grant_id": item.id, "mailbox": item.mailbox,
                    "expires_at": item.expires_at.isoformat(),
                } for item in page], "next_offset": (
                    request.offset + len(page)
                    if request.offset + len(page) < len(grants) else None
                )}
            grant = next((item for item in self.get(project_id).grants
                          if item.id == request.grant_id), None)
            if grant is None:
                raise AuthorizationError("This project has no matching Outlook grant")

            def authorized():
                current_run = authorize_connector_run(
                    self.ledger, self.access, self._project(project_id), run_id,
                )
                if current_run.sdk_session_id != run.sdk_session_id:
                    raise AuthorizationError("The bound SDK session changed")
                current = next((item for item in self.get(project_id).grants
                                if item.id == grant.id), None)
                if current != grant or not grant.active(self.clock()):
                    raise AuthorizationError("The Outlook grant expired or was revoked")
                if self._mailbox(grant.connector_id, project.tenant_id) != (
                    grant.connector_revision, grant.user_id, grant.mailbox,
                ):
                    raise AuthorizationError("The Outlook mailbox changed")

            observation = await self.outlook.read_inbox_page(
                grant.connector_id, project.tenant_id,
                page_size=request.page_size, page_token=request.page_token,
                authorize=authorized,
            )
            data = sanitize_untrusted_payload({
                "grant_id": grant.id, "observation": observation,
            }, UntrustedSource.OUTLOOK_PAGE)
            if data.truncated or len(json.dumps(data.value, ensure_ascii=False)) > (
                UNTRUSTED_LIMITS[UntrustedSource.OUTLOOK_PAGE].total_chars - 1024
            ):
                raise OutlookError("OUTLOOK_PAGE_LARGE", "Read fewer Outlook messages")
            with (
                self._identity_transaction(project_id),
                self.ledger._lock_for(f"outlook-access:{project_id}"),
                self.ledger._lock_for(run_id),
            ):
                authorized()
                now = self.clock()
                key = hashlib.sha256(json.dumps([
                    grant.id, request.page_token,
                    [item["message_id"] for item in observation["messages"]],
                ], separators=(",", ":")).encode()).hexdigest()
                receipt = self.ledger.issue_tool_receipt(
                    project_id=project_id, run_id=run_id, tool=TOOL_READ_OUTLOOK,
                    source_type=EvidenceSourceType.TOOL,
                    canonical_source=observation["source_url"], payload=data.value,
                    object_type="outlook_inbox_page", object_key=f"{grant.id}:{key}",
                    object_metadata={"grant_id": grant.id, "mailbox": grant.mailbox,
                                     "user_id": grant.user_id,
                                     "data_scope": "authorized_inbox"},
                    ttl=min(timedelta(minutes=15), grant.expires_at - now), now=now,
                )
                authorized()
            return {**data.value, "source_receipt": {
                "id": receipt.id, "content_hash": receipt.content_hash,
                "retrieved_at": receipt.retrieved_at.isoformat(),
                "expires_at": receipt.expires_at.isoformat(),
            }}
