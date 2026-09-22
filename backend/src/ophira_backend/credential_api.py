"""Administrator-only credential export and version-bound local deletion."""

from __future__ import annotations

import asyncio
from enum import StrEnum

from fastapi import HTTPException, Request, Response
from pydantic import Field, SecretStr, StrictBool, model_validator
from starlette.concurrency import run_in_threadpool

from .auth import AuthenticationError, AuthorizationError
from .backup_files import parse_json
from .connectors import ConnectorNotFoundError
from .constants import PrincipalRole
from .credential_archive import encrypt_credential
from .models import APIModel, utc_now
from .providers import ProviderNotFoundError
from .secret_vault import CredentialChangedError, VaultError
from .skill_proposal_files import directory_fd, read_relative


class CredentialKind(StrEnum):
    PROVIDER = "provider"
    CONNECTOR = "connector"


class CredentialReview(APIModel):
    expected_revision: str = Field(pattern=r"^[a-f0-9]{64}$")


class CredentialExport(CredentialReview):
    password: SecretStr = Field(min_length=12, max_length=1024)


class CredentialDelete(CredentialReview):
    confirm_delete: StrictBool

    @model_validator(mode="after")
    def confirmed(self):
        if not self.confirm_delete:
            raise ValueError("Explicit deletion confirmation is required")
        return self


class CredentialService:
    def __init__(self, settings, ledger, providers, connectors):
        self.settings, self.ledger = settings, ledger
        self.providers, self.connectors = providers, connectors
        self.root = ledger.root / "credential-management"
        self.root.mkdir(mode=0o700, exist_ok=True)
        self.scope = {
            "auth_mode": settings.auth_mode,
            "tenant_id": settings.trusted_gateway_tenant_id
            if settings.auth_mode == "trusted_gateway" else None,
        }
        with ledger._lock_for("credential-management"), directory_fd(self.root):
            try:
                read_relative(self.root, "scope.json", 2048)
            except FileNotFoundError:
                ledger._atomic_write(self.root / "scope.json", self.scope)

    def authorize(self, principal):
        if self.settings.auth_mode == "local_only":
            valid = principal is None
        else:
            valid = (
                principal is not None
                and principal.tenant_id == self.scope["tenant_id"]
                and principal.role in {PrincipalRole.OWNER, PrincipalRole.ADMIN}
                and utc_now() < principal.assertion_expires_at
            )
        if not valid or parse_json(read_relative(self.root, "scope.json", 2048)) != self.scope:
            raise AuthorizationError("Credential management requires the current administrator.")

    def target(self, kind: CredentialKind, identifier):
        if kind == CredentialKind.PROVIDER:
            item = self.providers.get(identifier)
            return item, self.providers.vault, None
        item = self.connectors.get(identifier)
        if self.connectors.vault is None:
            raise VaultError("Credential storage is unavailable")
        return item, self.connectors.vault, self.connectors.environment_credential(item)

    def describe(self, kind, identifier, principal):
        self.authorize(principal)
        item, vault, fallback = self.target(kind, identifier)
        value = {"kind": kind, "id": identifier, "display_name": item.display_name,
                 **vault.describe(identifier, fallback=fallback)}
        self.authorize(principal)
        return value

    def list(self, principal):
        self.authorize(principal)
        return [self.describe(kind, item.id, principal)
                for kind, items in (
                    (CredentialKind.PROVIDER, self.providers.list()),
                    (CredentialKind.CONNECTOR, self.connectors.list()),
                ) for item in items]

    def export(self, kind, identifier, data, principal):
        self.authorize(principal)
        item, vault, fallback = self.target(kind, identifier)
        secret = vault.capture(identifier, data.expected_revision, fallback=fallback)
        encrypted = encrypt_credential({
            "kind": kind, "id": identifier, "display_name": item.display_name,
            "credential": secret, "exported_at": utc_now().isoformat(),
            "exported_by": principal.principal_id if principal else "local-operator",
            "scope": self.scope,
        }, data.password.get_secret_value())
        # Encryption may outlast the assertion, a rotation or a revocation.
        current = self.describe(kind, identifier, principal)
        if current["revision"] != data.expected_revision:
            raise CredentialChangedError("Credential changed. Refresh and review it again.")
        return encrypted

    def delete(self, kind, identifier, data, principal):
        self.authorize(principal)
        item, vault, fallback = self.target(kind, identifier)
        receipt = vault.delete(
            identifier, expected_revision=data.expected_revision, fallback=fallback,
            deleted_by=principal.principal_id if principal else "local-operator",
            authorize=lambda: self.authorize(principal),
        )
        self.authorize(principal)
        return {"kind": kind, "id": identifier, "display_name": item.display_name, **receipt}


def register_credential_routes(app, settings, ledger, providers, connectors, read_body):
    service = CredentialService(settings, ledger, providers, connectors)
    app.state.credentials = service
    encryption_slots = asyncio.Semaphore(2)

    async def invoke(operation):
        try:
            return await run_in_threadpool(operation)
        except (AuthenticationError, AuthorizationError) as exc:
            raise HTTPException(
                403, "Credential management requires the current administrator.",
            ) from exc
        except (ProviderNotFoundError, ConnectorNotFoundError) as exc:
            raise HTTPException(404, "Credential profile not found.") from exc
        except (VaultError, ValueError, OSError) as exc:
            detail = (str(exc) if isinstance(exc, CredentialChangedError)
                      else "Credential operation could not be verified. Refresh before retrying.")
            raise HTTPException(409, detail) from exc

    async def payload(request, model):
        # Authenticate before consuming a body containing a password.
        await invoke(lambda: service.authorize(getattr(request.state, "principal", None)))
        raw = await read_body(
            request, max_bytes=8192, too_large_detail="Credential request too large.",
        )
        try:
            return model.model_validate_json(raw)
        except ValueError as exc:
            raise HTTPException(
                422, "Invalid credential request; check confirmation and password.",
            ) from exc

    @app.get("/api/credentials")
    async def credentials(request: Request, response: Response):
        response.headers["Cache-Control"] = "no-store"
        return await invoke(lambda: service.list(getattr(request.state, "principal", None)))

    @app.post("/api/credentials/{kind}/{identifier}/export")
    async def export(kind: CredentialKind, identifier: str, request: Request):
        data = await payload(request, CredentialExport)
        async with encryption_slots:
            result = await invoke(lambda: service.export(
                kind, identifier, data, getattr(request.state, "principal", None),
            ))
        return Response(result, media_type="application/json", headers={
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "Content-Disposition": 'attachment; filename="ophira-credential.encrypted.json"',
        })

    @app.delete("/api/credentials/{kind}/{identifier}")
    async def delete(kind: CredentialKind, identifier: str, request: Request, response: Response):
        data = await payload(request, CredentialDelete)
        response.headers["Cache-Control"] = "no-store"
        return await invoke(lambda: service.delete(
            kind, identifier, data, getattr(request.state, "principal", None),
        ))
