"""Revocable local credentials for application-owned channel and push adapters."""

import re
from dataclasses import dataclass
from enum import StrEnum

from cryptography.hazmat.primitives.asymmetric import ec
from py_vapid import Vapid

from .secret_vault import FileSecretVault, VaultError


class ServiceCredentialId(StrEnum):
    FEISHU_EVENTS = "feishu_events"
    FEISHU_APP = "feishu_app"
    WEB_PUSH = "web_push"


@dataclass(frozen=True)
class ServiceCredential:
    id: ServiceCredentialId
    display_name: str
    setting: str
    max_bytes: int
    effect: str


SERVICES = {
    item.id: item
    for item in (
        ServiceCredential(
            ServiceCredentialId.FEISHU_EVENTS,
            "Feishu event signing key",
            "feishu_encrypt_key",
            2000,
            "Deleting this key blocks new Feishu webhook authentication. "
            "Existing conversations and outbound replies are separate.",
        ),
        ServiceCredential(
            ServiceCredentialId.FEISHU_APP,
            "Feishu application secret",
            "feishu_app_secret",
            512,
            "Deleting this secret blocks new Feishu API requests and invalidates "
            "cached tenant tokens. Requests already sent cannot be recalled.",
        ),
        ServiceCredential(
            ServiceCredentialId.WEB_PUSH,
            "Web Push application private key",
            "push_vapid_private_key",
            2000,
            "Deleting this key blocks new Web Push requests. Saving a different "
            "key requires devices to subscribe again.",
        ),
    )
}


class ServiceCredentialNotFoundError(KeyError):
    pass


def parse_vapid_key(key):
    # Accept the adapter's existing inline RAW/DER base64 contract, never key files.
    if not re.fullmatch(r"[A-Za-z0-9_+/=-]+", key):
        raise VaultError("Invalid Web Push private key")
    try:
        vapid = Vapid.from_string(key)
        if not isinstance(vapid.private_key.curve, ec.SECP256R1):
            raise ValueError
        return vapid
    except Exception as exc:
        raise VaultError("Invalid Web Push private key") from exc


class ServiceCredentialRegistry:
    def __init__(self, settings):
        self.settings = settings
        self.vault = FileSecretVault(
            settings.data_dir.resolve() / "secrets", "service-secrets.json"
        )

    def list(self):
        return list(SERVICES.values())

    def get(self, identifier):
        try:
            return SERVICES[identifier]
        except KeyError as exc:
            raise ServiceCredentialNotFoundError(identifier) from exc

    def environment_credential(self, item):
        value = getattr(self.settings, item.setting)
        return value.get_secret_value() if value else None

    def snapshot(self, identifier):
        item = self.get(identifier)
        return self.vault.resolve_versioned(
            item.id,
            fallback=self.environment_credential(item),
        )

    def resolve(self, identifier):
        return self.snapshot(identifier)[1]

    def save(self, identifier, secret, revision, *, authorize=lambda: None):
        item = self.get(identifier)
        if not secret.strip() or len(secret.encode()) > item.max_bytes:
            raise VaultError("Invalid service credential")
        if item.id == ServiceCredentialId.WEB_PUSH:
            parse_vapid_key(secret)
        return self.vault.set(
            item.id,
            secret,
            expected_revision=revision,
            fallback=self.environment_credential(item),
            authorize=authorize,
        )
