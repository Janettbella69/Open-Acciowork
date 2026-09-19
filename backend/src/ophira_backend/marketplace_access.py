"""Human-owned project/shop grants and receipt-backed Agent reads.

The store is outside project workspaces. SDK tools can inspect and consume grants,
but cannot create, renew or revoke them. Locks cover local commits, never HTTP IO.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .auth import AuthorizationError, ProjectAccessStore, ProjectPermission
from .config import Settings
from .connectors import ConnectorNotFoundError, ConnectorRegistry, ConnectorUnavailableError
from .constants import TOOL_READ_MARKETPLACE, EvidenceSourceType, RunStatus
from .marketplaces import (
    SHOPIFY_TYPE,
    TIKTOK_SHOP_TYPE,
    MarketplaceError,
    MarketplaceReadRequest,
    ProductStatus,
    TikTokShopReader,
    default_marketplace_transport,
)
from .models import PrincipalContext, new_id, utc_now
from .shopify import ShopifyReader
from .storage import FileLedger
from .untrusted import UNTRUSTED_LIMITS, UntrustedSource, sanitize_untrusted_payload

ID_PATTERN = r"^[A-Za-z0-9_-]{1,160}$"
BINDING_PATTERN = r"^marketplace_binding_[a-f0-9]{32}$"
MAX_BINDINGS = 1000


def authorize_marketplace_run(ledger: FileLedger, access: ProjectAccessStore, project, run_id: str):
    """Shared API/browser check for a durable SDK Run and its current membership."""
    run = ledger.get_run(run_id)
    if (
        run.project_id != project.id or not run.sdk_session_id
        or run.status != RunStatus.RUNNING
        or run.identity_binding != project.identity_binding
        or run.tenant_id != project.tenant_id
    ):
        raise AuthorizationError("Marketplace read requires this project's running SDK session")
    if run.identity_binding == "authenticated_principal":
        if not run.principal_id or run.principal_role is None or not run.tenant_id:
            raise AuthorizationError("Run has no bound principal")
        access.authorize_bound(
            project.id, tenant_id=run.tenant_id, principal_id=run.principal_id,
            role=run.principal_role, permission=ProjectPermission.TOOL_USE,
        )
    return run


class MarketplaceGrantRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    expected_revision: int = Field(ge=0)
    connector_id: str = Field(pattern=ID_PATTERN)
    shop_id: str = Field(pattern=ID_PATTERN)
    expires_in_seconds: int = Field(default=7 * 86400, ge=60, le=30 * 86400)


class MarketplaceRevokeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_revision: int = Field(ge=1)


class MarketplaceBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: new_id("marketplace_binding"), pattern=BINDING_PATTERN)
    connector_id: str = Field(pattern=ID_PATTERN)
    connector_identity: str = Field(pattern=r"^[a-f0-9]{64}$")
    shop_id: str = Field(pattern=ID_PATTERN)
    shop_name: str = Field(max_length=1000)
    region: str = Field(max_length=80)
    created_by: str = Field(max_length=160)
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None
    revoked_by: str | None = Field(default=None, max_length=160)

    @model_validator(mode="after")
    def coherent_times(self):
        for instant in (self.created_at, self.expires_at, self.revoked_at):
            if instant is not None and (instant.tzinfo is None or instant.utcoffset() is None):
                raise ValueError("binding times require a timezone")
        if self.expires_at <= self.created_at:
            raise ValueError("binding expiry must follow creation")
        if (self.revoked_at is None) != (self.revoked_by is None):
            raise ValueError("revocation must have an actor and time")
        return self

    def active(self, now: datetime) -> bool:
        return self.revoked_at is None and self.expires_at > now


class MarketplaceAccessRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str = Field(pattern=ID_PATTERN)
    identity_binding: Literal["local_only", "authenticated_principal"]
    tenant_id: str | None
    revision: int = Field(default=0, ge=0)
    bindings: list[MarketplaceBinding] = Field(default_factory=list, max_length=MAX_BINDINGS)

    @model_validator(mode="after")
    def unique_bindings(self):
        if len({item.id for item in self.bindings}) != len(self.bindings):
            raise ValueError("binding ids must be unique")
        return self


class MarketplaceAgentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    operation: Literal["connections", "products", "orders"]
    binding_id: str | None = Field(default=None, pattern=BINDING_PATTERN)
    page_size: int = Field(default=10, ge=1, le=10)
    offset: int = Field(default=0, ge=0, le=MAX_BINDINGS)
    page_token: str | None = Field(default=None, min_length=1, max_length=2048)
    product_status: ProductStatus = "ALL"

    @model_validator(mode="after")
    def operation_arguments(self):
        if self.operation in {"products", "orders"} and (not self.binding_id or self.offset):
            raise ValueError("shop reads require a binding and use the upstream cursor")
        if self.operation == "orders" and self.product_status != "ALL":
            raise ValueError("orders do not accept product filters")
        if self.operation == "connections" and (
            self.binding_id or self.page_token or self.product_status != "ALL"
        ):
            raise ValueError("connections do not accept product filters")
        return self


class MarketplaceReader:
    """Route typed seller reads to the adapter for each connector's platform."""

    def __init__(
        self, connectors: ConnectorRegistry, *,
        transport_factory: Callable[[], httpx.AsyncBaseTransport] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.connectors = connectors
        self.transport_factory = transport_factory or default_marketplace_transport
        self.clock = clock
        # Adapters resolve these per request, so replacing them here reaches every platform.
        options: dict[str, Any] = {
            "transport_factory": lambda: self.transport_factory(),
            "clock": lambda: self.clock(),
        }
        self.adapters: dict[str, TikTokShopReader | ShopifyReader] = {
            TIKTOK_SHOP_TYPE: TikTokShopReader(connectors, **options),
            SHOPIFY_TYPE: ShopifyReader(connectors, **options),
        }

    def adapter(self, connector_id: str) -> TikTokShopReader | ShopifyReader:
        try:
            connector = self.connectors.get(connector_id)
        except (ConnectorNotFoundError, ValueError):
            raise ConnectorUnavailableError(
                "MARKETPLACE_NOT_CONFIGURED", "The marketplace connector is not configured.",
            ) from None
        adapter = self.adapters.get(connector.type)
        if adapter is None:
            raise ConnectorUnavailableError(
                "MARKETPLACE_UNSUPPORTED", "This connector has no supported marketplace reader.",
            )
        return adapter

    async def read(
        self, connector_id: str, request: MarketplaceReadRequest, *,
        authorize: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        return await self.adapter(connector_id).read(connector_id, request, authorize=authorize)

    def identity_parts(self, connector_id: str) -> list[str | None]:
        return self.adapter(connector_id).identity_parts(connector_id)

    def require_grantable(self, connector_id: str) -> None:
        self.adapter(connector_id).require_grantable(connector_id)


class MarketplaceAccessService:
    def __init__(
        self, ledger: FileLedger, connectors: ConnectorRegistry, *, settings: Settings,
        access: ProjectAccessStore, reader: MarketplaceReader | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.ledger = ledger
        self.connectors = connectors
        self.settings = settings
        self.access = access
        self.reader = reader or MarketplaceReader(connectors)
        self.clock = clock
        self.root = ledger.root / "auth" / "marketplaces"
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
            raise AuthorizationError("Marketplace access is outside deployment scope")
        return project

    def authorize_admin(self, project_id: str, principal: PrincipalContext | None) -> str:
        project = self._project(project_id)
        if project.identity_binding == "local_only":
            if principal is not None:
                raise AuthorizationError("Local access does not accept a principal")
            return "local_owner"
        if principal is None:
            raise AuthorizationError("Marketplace access requires an administrator")
        self.access.authorize(project_id, principal, ProjectPermission.PROJECT_ACCESS_MANAGE)
        return principal.principal_id

    def get(self, project_id: str) -> MarketplaceAccessRecord:
        with self.ledger.lifecycle.lease(project_id):
            project = self._project(project_id)
            # get_project has already validated the ledger id, before using it in a path.
            path = self.root / f"{project.id}.json"
            if not path.exists() and not path.is_symlink():
                return MarketplaceAccessRecord(
                    project_id=project.id, identity_binding=project.identity_binding,
                    tenant_id=project.tenant_id,
                )
            try:
                if path.is_symlink() or not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
                    raise ValueError("invalid access file")
                record = MarketplaceAccessRecord.model_validate_json(path.read_text("utf-8"))
                if (
                    record.project_id != project.id or record.tenant_id != project.tenant_id
                    or record.identity_binding != project.identity_binding
                ):
                    raise ValueError("invalid identity")
                return record
            except (OSError, ValueError):
                raise MarketplaceError(
                    "MARKETPLACE_ACCESS_INVALID",
                    "Project marketplace access failed validation.", 409,
                ) from None

    def view(self, project_id: str) -> dict:
        record = self.get(project_id)
        return {
            "project_id": record.project_id, "revision": record.revision,
            "bindings": [
                {**item.model_dump(mode="json", exclude={"connector_identity"}),
                 "active": item.active(self.clock())}
                for item in record.bindings
            ],
        }

    def _connector_identity(self, connector_id: str) -> str:
        # Refreshing a token or secret for the same app/shop does not widen the human
        # grant. Repointing the connector to a different app or store requires a new grant.
        value = self.reader.identity_parts(connector_id)
        return hashlib.sha256(json.dumps(value, separators=(",", ":")).encode()).hexdigest()

    def _revision(self, project_id: str, expected: int) -> MarketplaceAccessRecord:
        record = self.get(project_id)
        if record.revision != expected:
            raise MarketplaceError(
                "MARKETPLACE_ACCESS_CONFLICT",
                "Project access changed. Refresh and try again.", 409,
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

    async def grant(
        self, project_id: str, payload: MarketplaceGrantRequest,
        principal: PrincipalContext | None,
    ) -> dict:
        with self.ledger.lifecycle.lease(project_id):
            actor = self.authorize_admin(project_id, principal)
            self.reader.require_grantable(payload.connector_id)
            self._revision(project_id, payload.expected_revision)
            identity = self._connector_identity(payload.connector_id)

            def authorized():
                self.authorize_admin(project_id, principal)
                self._revision(project_id, payload.expected_revision)
                if self._connector_identity(payload.connector_id) != identity:
                    raise MarketplaceError(
                        "MARKETPLACE_CONFIGURATION_CHANGED", "The connector app changed.", 409,
                    )

            observed = await self.reader.read(
                payload.connector_id, MarketplaceReadRequest(operation="authorized_shops"),
                authorize=authorized,
            )
            shop = next((item for item in observed["shops"] if item["id"] == payload.shop_id), None)
            if shop is None:
                raise MarketplaceError(
                    "MARKETPLACE_SHOP_NOT_AUTHORIZED",
                    "The seller token does not authorize this shop.",
                    403,
                )
            with (
                self._identity_transaction(project_id),
                self.ledger._lock_for(f"marketplace-access:{project_id}"),
            ):
                authorized()
                record = self.get(project_id)
                if len(record.bindings) >= MAX_BINDINGS:
                    raise MarketplaceError(
                        "MARKETPLACE_ACCESS_LIMIT", "Project grant history is full."
                    )
                now = self.clock()
                # Renewal replaces the old authority with a fresh immutable binding id.
                bindings = [
                    item.model_copy(update={"revoked_at": now, "revoked_by": actor})
                    if item.active(now) and item.connector_id == payload.connector_id
                    and item.shop_id == payload.shop_id else item
                    for item in record.bindings
                ]
                bindings.append(MarketplaceBinding(
                    connector_id=payload.connector_id, connector_identity=identity,
                    shop_id=shop["id"], shop_name=shop["name"], region=shop["region"],
                    created_by=actor, created_at=now,
                    expires_at=now + timedelta(seconds=payload.expires_in_seconds),
                ))
                self.ledger._atomic_write(self.root / f"{project_id}.json", record.model_copy(
                    update={"revision": record.revision + 1, "bindings": bindings},
                ))
                return self.view(project_id)

    def revoke(
        self, project_id: str, binding_id: str, expected_revision: int,
        principal: PrincipalContext | None,
    ) -> dict:
        with (
            self._identity_transaction(project_id),
            self.ledger._lock_for(f"marketplace-access:{project_id}"),
        ):
            actor = self.authorize_admin(project_id, principal)
            record = self._revision(project_id, expected_revision)
            if not any(item.id == binding_id for item in record.bindings):
                raise MarketplaceError("MARKETPLACE_ACCESS_NOT_FOUND", "Shop grant not found.", 404)
            bindings = [
                item.model_copy(update={"revoked_at": self.clock(), "revoked_by": actor})
                if item.id == binding_id and item.revoked_at is None else item
                for item in record.bindings
            ]
            self.ledger._atomic_write(self.root / f"{project_id}.json", record.model_copy(
                update={"revision": record.revision + 1, "bindings": bindings},
            ))
            return self.view(project_id)

    def _run(self, project_id: str, run_id: str):
        return authorize_marketplace_run(
            self.ledger, self.access, self._project(project_id), run_id,
        )

    async def read_for_run(
        self, project_id: str, run_id: str, request: MarketplaceAgentRequest,
    ) -> dict:
        with self.ledger.lifecycle.lease(project_id):
            run = self._run(project_id, run_id)
            record = self.get(project_id)
            if request.operation == "connections":
                active = [item for item in record.bindings if item.active(self.clock())]
                page = active[request.offset:request.offset + request.page_size]
                self._run(project_id, run_id)
                return {"connections": [
                    {"binding_id": item.id, "shop_name": item.shop_name[:160],
                     "shop_id": item.shop_id, "connector_id": item.connector_id,
                     "region": item.region, "expires_at": item.expires_at.isoformat()}
                    for item in page
                ], "next_offset": (
                    request.offset + len(page) if request.offset + len(page) < len(active) else None
                )}
            binding = next(
                (item for item in record.bindings if item.id == request.binding_id), None,
            )
            if binding is None:
                raise AuthorizationError("This project has no matching shop grant")

            def authorized():
                current_run = self._run(project_id, run_id)
                if current_run.sdk_session_id != run.sdk_session_id:
                    raise AuthorizationError("The bound SDK session changed")
                current = next((item for item in self.get(project_id).bindings
                                if item.id == binding.id), None)
                if current != binding or not binding.active(self.clock()):
                    raise AuthorizationError("The project shop grant expired or was revoked")
                if self._connector_identity(binding.connector_id) != binding.connector_identity:
                    raise AuthorizationError("The connector app no longer matches the shop grant")

            authorized()
            observation = await self.reader.read(
                binding.connector_id,
                MarketplaceReadRequest(
                    operation=request.operation, shop_id=binding.shop_id,
                    page_size=request.page_size, page_token=request.page_token,
                    product_status=request.product_status,
                ),
                authorize=authorized,
            )
            data = sanitize_untrusted_payload({
                "binding_id": binding.id, "product_status": request.product_status,
                "page_size": request.page_size, "observation": observation,
            }, UntrustedSource.MARKETPLACE_PAGE)
            # Reserve space for the fixed notice and source-receipt metadata that the
            # SDK hook will see alongside this same observation. Include JSON framing
            # in this conservative size check, even though the sanitizer counts leaves.
            result_chars = len(json.dumps(data.value, ensure_ascii=False))
            if data.truncated or result_chars > (
                UNTRUSTED_LIMITS[UntrustedSource.MARKETPLACE_PAGE].total_chars - 1024
            ):
                raise MarketplaceError(
                    "MARKETPLACE_OBSERVATION_TOO_LARGE",
                    "Read fewer products to capture a full page.",
                )
            # Receipt issuance and revoke are serialized only after HTTP IO has ended.
            with (
                self._identity_transaction(project_id),
                self.ledger._lock_for(f"marketplace-access:{project_id}"),
                self.ledger._lock_for(run_id),
            ):
                authorized()
                now = self.clock()
                receipt = self.ledger.issue_tool_receipt(
                    project_id=project_id, run_id=run_id, tool=TOOL_READ_MARKETPLACE,
                    source_type=EvidenceSourceType.TOOL, canonical_source=observation["source_url"],
                    payload=data.value, object_type=(
                        "marketplace_product_page" if request.operation == "products"
                        else "marketplace_order_page"
                    ),
                    object_key=f"{binding.id}:{observation['request_id']}",
                    object_metadata={"binding_id": binding.id, "shop_id": binding.shop_id,
                                     "data_scope": "authorized_seller_data"},
                    ttl=min(timedelta(minutes=15), binding.expires_at - now), now=now,
                )
                authorized()
            return {**data.value, "source_receipt": {
                "id": receipt.id, "content_hash": receipt.content_hash,
                "retrieved_at": receipt.retrieved_at.isoformat(),
                "expires_at": receipt.expires_at.isoformat(),
            }}
