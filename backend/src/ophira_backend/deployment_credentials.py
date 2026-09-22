"""Deployment-owner secrets, deliberately absent from the application API."""

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .credential_archive import decrypt_credential, encrypt_credential
from .credential_models import CredentialKind
from .models import utc_now
from .secret_vault import FileSecretVault, VaultError


class DeploymentCredentialSettings(BaseSettings):
    """Shared configuration also loadable when unrelated application settings are invalid."""

    model_config = SettingsConfigDict(env_prefix="OPHIRA_", extra="ignore")
    data_dir: Path
    trusted_gateway_auth_source: Literal["environment", "local_vault"] = "environment"
    trusted_gateway_auth_secret: SecretStr | None = Field(
        default=None, min_length=32, max_length=512
    )
    trusted_gateway_tenant_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=160,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@+\-]{0,159}$",
    )
    # Compatibility setting only: no commercial action currently consumes it.
    commercial_approval_secret: SecretStr | None = Field(
        default=None, min_length=32, max_length=512
    )

    @field_validator(
        "trusted_gateway_auth_secret",
        "trusted_gateway_tenant_id",
        "commercial_approval_secret",
        mode="before",
    )
    @classmethod
    def empty_deployment_value_is_unset(cls, value):
        return value.strip() or None if isinstance(value, str) else value


class DeploymentCredentialId(StrEnum):
    TRUSTED_GATEWAY = "trusted_gateway"
    LEGACY_COMMERCIAL = "legacy_commercial_approval"


@dataclass(frozen=True)
class DeploymentCredential:
    id: DeploymentCredentialId
    setting: str
    effect: str


DEPLOYMENT_CREDENTIALS = {
    item.id: item
    for item in (
        DeploymentCredential(
            DeploymentCredentialId.TRUSTED_GATEWAY,
            "trusted_gateway_auth_secret",
            "Deletion disables gateway authentication and trusted-mode startup. "
            "Recovery requires local operator access. Coordinate the signing key with ingress.",
        ),
        DeploymentCredential(
            DeploymentCredentialId.LEGACY_COMMERCIAL,
            "commercial_approval_secret",
            "Legacy configuration only; no current commercial action consumes this secret.",
        ),
    )
}


class DeploymentCredentialRegistry:
    def __init__(self, settings: DeploymentCredentialSettings):
        self.settings = settings
        self.vault = FileSecretVault(
            settings.data_dir.resolve() / "secrets", "deployment-secrets.json"
        )

    def get(self, identifier):
        try:
            return DEPLOYMENT_CREDENTIALS[identifier]
        except KeyError as exc:
            raise VaultError("Unknown deployment credential") from exc

    def fallback(self, identifier):
        item = self.get(identifier)
        if (
            item.id == DeploymentCredentialId.TRUSTED_GATEWAY
            and self.settings.trusted_gateway_auth_source == "local_vault"
        ):
            return None
        value = getattr(self.settings, item.setting)
        return value.get_secret_value() if value else None

    def snapshot(self, identifier):
        self.get(identifier)
        return self.vault.resolve_versioned(identifier, fallback=self.fallback(identifier))

    def describe(self, identifier):
        item = self.get(identifier)
        view, _ = self.snapshot(identifier)
        return {"kind": CredentialKind.DEPLOYMENT, "id": item.id, "effect": item.effect, **view}

    def save(self, identifier, secret, revision):
        self.get(identifier)
        if not isinstance(secret, str) or secret != secret.strip() or not 32 <= len(secret) <= 512:
            raise VaultError(
                "Deployment credentials require 32 to 512 characters without edge spaces"
            )
        return self.vault.set(
            identifier, secret, expected_revision=revision, fallback=self.fallback(identifier)
        )

    def delete(self, identifier, revision):
        self.get(identifier)
        return self.vault.delete(
            identifier,
            expected_revision=revision,
            fallback=self.fallback(identifier),
            deleted_by="deployment-operator",
        )

    def export(self, identifier, revision, password):
        self.get(identifier)
        secret = self.vault.capture(identifier, revision, fallback=self.fallback(identifier))
        data = encrypt_credential(
            {
                "kind": CredentialKind.DEPLOYMENT,
                "id": identifier,
                "credential": secret,
                "exported_at": utc_now().isoformat(),
                "exported_by": "deployment-operator",
                "scope": {"tenant_id": self.settings.trusted_gateway_tenant_id},
            },
            password,
        )
        self.vault.capture(identifier, revision, fallback=self.fallback(identifier))
        return data

    def restore(self, identifier, revision, data, password):
        self.get(identifier)
        payload = decrypt_credential(data, password)
        if (
            payload.get("kind") != CredentialKind.DEPLOYMENT
            or payload.get("id") != identifier
            or payload.get("scope") != {"tenant_id": self.settings.trusted_gateway_tenant_id}
        ):
            raise VaultError("Archive does not match the reviewed deployment credential and tenant")
        return self.save(identifier, payload["credential"], revision)
