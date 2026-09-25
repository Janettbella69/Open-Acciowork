"""Gmail OAuth, mailbox watches and authenticated Pub/Sub history delivery.

The mailbox cursor is durable. It advances only after every new message has
passed through the existing project-bound event receipt and Run dispatcher.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, urlencode

import httpx
import jwt
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .config import Settings
from .event_triggers import EventTrigger, EventTriggerStore
from .models import utc_now
from .storage import FileLedger

if TYPE_CHECKING:
    from .connectors import ConnectorRegistry

LOGGER = logging.getLogger(__name__)
GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GMAIL_CALLBACK_PATH = "/api/connectors/{connector_id}/gmail-oauth/callback"
GMAIL_CALLBACK_RE = re.compile(r"/api/connectors/[A-Za-z0-9_-]+/gmail-oauth/callback")
GMAIL_PUSH_PATH = "/api/connectors/{connector_id}/gmail/push"
GMAIL_PUSH_RE = re.compile(r"/api/connectors/[A-Za-z0-9_-]+/gmail/push")
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_CERTS_URL = "https://www.googleapis.com/oauth2/v3/certs"
GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"
MAX_GOOGLE_RESPONSE = 1_000_000
MAX_PUSH_BODY = 32_000
MAX_HISTORY_PAGES = 100
MAX_HISTORY_MESSAGES = 1_000
_HISTORY_ID = re.compile(r"^[1-9][0-9]{0,24}$")
_GMAIL_ID = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
_EMAIL = re.compile(r"^[^\s@<>]{1,64}@[A-Za-z0-9.-]{1,190}$")
_PENDING = "gmail-oauth-pending:"
_TOKEN = "gmail-oauth-token:"


class GmailError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class GmailCredentials(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    client_id: str = Field(min_length=20, max_length=255)
    client_secret: str = Field(min_length=16, max_length=512)


def parse_gmail_credentials(raw: str) -> GmailCredentials:
    try:
        if len(raw) > 2_000:
            raise ValueError
        value = GmailCredentials.model_validate_json(raw)
        if not value.client_id.endswith(".apps.googleusercontent.com"):
            raise ValueError
        if any(
            ord(char) < 33 or ord(char) > 126 for char in (value.client_id + value.client_secret)
        ):
            raise ValueError
        return value
    except (ValidationError, ValueError):
        # Validation errors may contain credential input.
        raise ValueError("Gmail requires a Google OAuth web client ID and secret") from None


def gmail_oauth_vault_keys(connector_id: str) -> tuple[str, str]:
    return _PENDING + connector_id, _TOKEN + connector_id


class GmailToken(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    email: str
    tenant_id: str | None
    connector_revision: int
    refresh_token: str
    access_token: str
    expires_at: float


class GmailWatch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    connector_id: str
    connector_revision: int
    tenant_id: str | None
    email: str = Field(pattern=_EMAIL.pattern)
    topic: str
    history_id: str = Field(pattern=_HISTORY_ID.pattern)
    expiration_ms: int
    stale: bool = False
    updated_at: str


class GmailEnrollment(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    project_id: str
    trigger_id: str
    connector_id: str
    connector_revision: int
    tenant_id: str | None
    email: str = Field(pattern=_EMAIL.pattern)
    start_history_id: str = Field(pattern=_HISTORY_ID.pattern)


class GmailService:
    def __init__(
        self,
        settings: Settings,
        connectors: ConnectorRegistry,
        ledger: FileLedger,
        triggers: EventTriggerStore,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self.connectors = connectors
        self.ledger = ledger
        self.triggers = triggers
        self.root = settings.data_dir / "gmail-watches"
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.client = client or httpx.AsyncClient(timeout=15, trust_env=False)
        self._owns_client = client is None
        self._locks: dict[str, asyncio.Lock] = {}
        self._certs: dict[str, Any] | None = None
        self._certs_expires = 0.0
        self._certs_fetched_at = 0.0
        self._renew_task: asyncio.Task[None] | None = None

    def _lock(self, connector_id: str) -> asyncio.Lock:
        return self._locks.setdefault(connector_id, asyncio.Lock())

    def _connector(self, connector_id: str) -> tuple[int, GmailCredentials]:
        try:
            connector = self.connectors.get(connector_id)
            if connector.type != "gmail" or not connector.enabled:
                raise ValueError
            raw = self.connectors.vault.get(connector_id) if self.connectors.vault else None
            if raw is None and connector.secret_env:
                import os

                raw = os.environ.get(connector.secret_env)
            if raw is None:
                raise ValueError
            return connector.revision, parse_gmail_credentials(raw)
        except (KeyError, ValueError):
            raise GmailError(
                "GMAIL_CONNECTOR_UNAVAILABLE", "Configure Gmail OAuth credentials first"
            ) from None

    def _token(self, connector_id: str, tenant_id: str | None = None) -> GmailToken:
        revision, _ = self._connector(connector_id)
        vault = self.connectors.vault
        raw = vault.get(_TOKEN + connector_id) if vault else None
        try:
            token = GmailToken.model_validate_json(raw or "")
            if token.connector_revision != revision or (
                tenant_id is not None and token.tenant_id != tenant_id
            ):
                raise ValueError
            if not _EMAIL.fullmatch(token.email):
                raise ValueError
            return token
        except (ValidationError, ValueError):
            raise GmailError("GMAIL_AUTH_REQUIRED", "Authorize the Gmail account first") from None

    def token_owner(self, connector_id: str) -> tuple[bool, str | None]:
        vault = self.connectors.vault
        raw = vault.get(_TOKEN + connector_id) if vault else None
        try:
            token = GmailToken.model_validate_json(raw or "")
            return True, token.tenant_id
        except ValidationError:
            return False, None

    def account(self, connector_id: str, tenant_id: str | None) -> str:
        token = self._token(connector_id, tenant_id)
        if token.tenant_id != tenant_id:
            raise GmailError("GMAIL_TENANT_MISMATCH", "Gmail account is outside this tenant", 403)
        return token.email

    def _watch_path(self, connector_id: str) -> Path:
        return self.root / f"{self.connectors._safe_id(connector_id)}.json"

    def _watch(self, connector_id: str) -> GmailWatch | None:
        path = self._watch_path(connector_id)
        if not path.is_file():
            return None
        return GmailWatch.model_validate_json(path.read_text("utf-8"))

    def _save_watch(self, watch: GmailWatch) -> None:
        self.ledger._atomic_write(self._watch_path(watch.connector_id), watch)

    def _enrollment_path(self, trigger: EventTrigger) -> Path:
        return (
            self.ledger.projects_dir / self.ledger._safe_id(trigger.project_id)
            / "event-triggers" / "gmail-enrollments"
            / f"{self.ledger._safe_id(trigger.id)}.json"
        )

    def _enrollment(self, trigger: EventTrigger) -> GmailEnrollment | None:
        path = self._enrollment_path(trigger)
        if not path.is_file():
            return None
        record = GmailEnrollment.model_validate_json(path.read_text("utf-8"))
        if record.project_id != trigger.project_id or record.trigger_id != trigger.id:
            raise GmailError("GMAIL_ENROLLMENT_INVALID", "Gmail trigger enrollment is invalid", 500)
        return record

    def _save_enrollment(self, trigger: EventTrigger, record: GmailEnrollment) -> None:
        self.ledger._atomic_write(self._enrollment_path(trigger), record)

    def oauth_status(self, connector_id: str, tenant_id: str | None) -> dict[str, Any]:
        callback = (
            self.settings.event_public_origin
            + GMAIL_CALLBACK_PATH.format(
                connector_id=connector_id,
            )
            if self.settings.event_public_origin
            else None
        )
        try:
            token = self._token(connector_id, tenant_id)
            authorized = token.tenant_id == tenant_id
        except GmailError:
            token = None
            authorized = False
        return {
            "callback_url": callback,
            "authorized": authorized,
            "email": token.email if authorized and token else None,
        }

    def begin_oauth(self, connector_id: str, tenant_id: str | None) -> dict[str, str]:
        origin = self.settings.event_public_origin
        if self.settings.auth_mode != "trusted_gateway" or origin is None:
            raise GmailError("GMAIL_ORIGIN_REQUIRED", "Configure a public HTTPS event origin")
        revision, credentials = self._connector(connector_id)
        state = secrets.token_urlsafe(32)
        pending = {
            "state_hash": hashlib.sha256(state.encode()).hexdigest(),
            "revision": revision,
            "tenant_id": tenant_id,
            "expires_at": time.time() + 600,
        }
        assert self.connectors.vault is not None
        with self.connectors._transaction():
            owned, owner = self.token_owner(connector_id)
            if owned and owner != tenant_id:
                raise GmailError(
                    "GMAIL_TENANT_MISMATCH", "Gmail account is outside this tenant", 403
                )
            if self._connector(connector_id)[0] != revision:
                raise GmailError("GMAIL_CONNECTOR_CHANGED", "Gmail connector changed")
            self.connectors.vault.set(_PENDING + connector_id, json.dumps(pending))
        callback = origin + GMAIL_CALLBACK_PATH.format(connector_id=connector_id)
        query = urlencode(
            {
                "client_id": credentials.client_id,
                "redirect_uri": callback,
                "response_type": "code",
                "scope": GMAIL_SCOPE,
                "access_type": "offline",
                "prompt": "consent",
                "state": state,
            }
        )
        return {"authorization_url": GOOGLE_AUTH_URL + "?" + query, "callback_url": callback}

    async def complete_oauth(self, connector_id: str, raw_query: bytes) -> None:
        if len(raw_query) > 16_000:
            raise GmailError("GMAIL_OAUTH_INVALID", "Invalid Gmail callback", 403)
        try:
            pairs = parse_qsl(
                raw_query.decode("ascii"),
                keep_blank_values=True,
                strict_parsing=True,
                max_num_fields=8,
                encoding="utf-8",
                errors="strict",
            )
            values = dict(pairs)
            if len(values) != len(pairs):
                raise ValueError
        except (UnicodeError, ValueError):
            raise GmailError("GMAIL_OAUTH_INVALID", "Invalid Gmail callback", 403) from None
        revision, credentials = self._connector(connector_id)
        state = values.get("state", "")
        if not 32 <= len(state) <= 128 or not state.isascii():
            raise GmailError("GMAIL_OAUTH_INVALID", "Invalid Gmail callback", 403)
        assert self.connectors.vault is not None
        with self.connectors._transaction():
            raw = self.connectors.vault.get(_PENDING + connector_id)
            try:
                pending = json.loads(raw or "")
                valid = (
                    isinstance(pending, dict)
                    and pending.get("revision") == revision
                    and type(pending.get("expires_at")) in (int, float)
                    and pending["expires_at"] > time.time()
                    and hmac.compare_digest(
                        str(pending.get("state_hash", "")),
                        hashlib.sha256(state.encode()).hexdigest(),
                    )
                    and self._connector(connector_id)[0] == revision
                )
            except (ValueError, TypeError):
                valid = False
            if not valid:
                raise GmailError("GMAIL_OAUTH_EXPIRED", "Gmail authorization expired", 403)
            self.connectors.vault.delete(_PENDING + connector_id)
        if values.get("error"):
            raise GmailError("GMAIL_OAUTH_DENIED", "Gmail authorization was declined", 403)
        code = values.get("code", "")
        if not 1 <= len(code) <= 8_000 or any(ord(char) < 33 for char in code):
            raise GmailError("GMAIL_OAUTH_INVALID", "Invalid Gmail authorization code", 403)
        callback = self.settings.event_public_origin + GMAIL_CALLBACK_PATH.format(
            connector_id=connector_id,
        )
        response = await self._json(
            "POST",
            GOOGLE_TOKEN_URL,
            data={
                "client_id": credentials.client_id,
                "client_secret": credentials.client_secret,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": callback,
            },
        )
        refresh = response.get("refresh_token")
        access = response.get("access_token")
        scope_value = response.get("scope", "")
        scopes = scope_value.split() if isinstance(scope_value, str) else []
        expires = response.get("expires_in")
        if (
            not isinstance(refresh, str)
            or not refresh
            or not isinstance(access, str)
            or not access
            or GMAIL_SCOPE not in scopes
            or type(expires) is not int
            or expires <= 60
        ):
            raise GmailError(
                "GMAIL_OAUTH_RESPONSE", "Gmail authorization did not grant read access", 502
            )
        profile = await self._json(
            "GET",
            GMAIL_API + "/profile",
            headers={"Authorization": f"Bearer {access}"},
        )
        email = profile.get("emailAddress")
        if not isinstance(email, str) or not _EMAIL.fullmatch(email):
            raise GmailError("GMAIL_PROFILE_INVALID", "Gmail account identity is invalid", 502)
        with self.connectors._transaction():
            if self._connector(connector_id)[0] != revision:
                raise GmailError("GMAIL_CONNECTOR_CHANGED", "Gmail connector changed")
            self.connectors.vault.set(
                _TOKEN + connector_id,
                GmailToken(
                    email=email.lower(),
                    tenant_id=pending.get("tenant_id"),
                    connector_revision=revision,
                    refresh_token=refresh,
                    access_token=access,
                    expires_at=time.time() + expires,
                ).model_dump_json(),
            )

    async def _json(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        data: dict[str, str] | None = None,
        payload: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        try:
            async with self.client.stream(
                method,
                url,
                headers=headers,
                data=data,
                json=payload,
                params=params,
            ) as response:
                if response.status_code == 404 and url.startswith(GMAIL_API + "/history"):
                    raise GmailError(
                        "GMAIL_HISTORY_STALE", "Gmail history expired; reset explicitly"
                    )
                if response.status_code >= 400:
                    raise GmailError("GMAIL_UPSTREAM", "Gmail or Google rejected the request", 502)
                chunks = bytearray()
                async for chunk in response.aiter_bytes():
                    chunks.extend(chunk)
                    if len(chunks) > MAX_GOOGLE_RESPONSE:
                        raise GmailError(
                            "GMAIL_RESPONSE_LARGE", "Google response is too large", 502
                        )
            result = json.loads(chunks) if chunks else {}
            if not isinstance(result, dict):
                raise ValueError
            return result
        except (httpx.HTTPError, ValueError) as exc:
            raise GmailError("GMAIL_UPSTREAM", "Gmail request failed", 502) from exc

    async def _access(self, connector_id: str, tenant_id: str | None) -> GmailToken:
        token = self._token(connector_id, tenant_id)
        if token.expires_at > time.time() + 60:
            return token
        async with self._lock(connector_id):
            token = self._token(connector_id, tenant_id)
            if token.expires_at > time.time() + 60:
                return token
            revision, credentials = self._connector(connector_id)
            result = await self._json(
                "POST",
                GOOGLE_TOKEN_URL,
                data={
                    "client_id": credentials.client_id,
                    "client_secret": credentials.client_secret,
                    "refresh_token": token.refresh_token,
                    "grant_type": "refresh_token",
                },
            )
            access = result.get("access_token")
            expires = result.get("expires_in")
            if (
                not isinstance(access, str)
                or not access
                or type(expires) is not int
                or expires <= 60
            ):
                raise GmailError("GMAIL_REFRESH_FAILED", "Gmail authorization needs renewal", 502)
            updated = token.model_copy(
                update={
                    "access_token": access,
                    "expires_at": time.time() + expires,
                }
            )
            with self.connectors._transaction():
                if self._connector(connector_id)[0] != revision:
                    raise GmailError("GMAIL_CONNECTOR_CHANGED", "Gmail connector changed")
                assert self.connectors.vault is not None
                self.connectors.vault.set(_TOKEN + connector_id, updated.model_dump_json())
            return updated

    async def read_inbox_page(
        self, connector_id: str, tenant_id: str | None, *, query: str,
        page_size: int, page_token: str | None,
        authorize: Callable[[], None],
    ) -> dict[str, Any]:
        """Read one bounded Inbox page after each project/run authorization check."""
        authorize()
        token = await self._access(connector_id, tenant_id)
        authorize()
        params = {"labelIds": "INBOX", "maxResults": str(page_size)}
        if query:
            params["q"] = query
        if page_token:
            params["pageToken"] = page_token
        listed = await self._json(
            "GET", GMAIL_API + "/messages",
            headers={"Authorization": f"Bearer {token.access_token}"}, params=params,
        )
        authorize()
        items = listed.get("messages", [])
        next_page = listed.get("nextPageToken")
        if not isinstance(items, list) or len(items) > page_size or any(
            not isinstance(item, dict)
            or not isinstance(item.get("id"), str)
            or not _GMAIL_ID.fullmatch(item["id"])
            for item in items
        ) or (next_page is not None and (
            not isinstance(next_page, str) or len(next_page) > 2048
        )):
            raise GmailError("GMAIL_MESSAGE_INVALID", "Gmail message page is invalid", 502)
        messages = []
        for item in items:
            authorize()
            raw = await self._json(
                "GET", GMAIL_API + "/messages/" + item["id"],
                headers={"Authorization": f"Bearer {token.access_token}"},
                params={"format": "full"},
            )
            authorize()
            if raw.get("id") != item["id"]:
                raise GmailError("GMAIL_MESSAGE_INVALID", "Gmail message ID changed", 502)
            payload = self._message_payload(raw, token.email)
            if payload is None:
                # A message can leave Inbox after the list call. Never expose a
                # later Sent/Draft/Trash body from a stale list response.
                continue
            body = payload["body"]
            messages.append({
                "message_id": item["id"], "from": payload["from"],
                "subject": payload["subject"], "body": body[:3500],
                "body_truncated": len(body) > 3500,
            })
        return {
            "mailbox": token.email, "query": query, "messages": messages,
            "next_page_token": next_page,
            "source_url": GMAIL_API + "/messages?" + urlencode(params),
        }

    def push_url(self, connector_id: str) -> str | None:
        origin = self.settings.event_public_origin
        if not origin or self.settings.auth_mode != "trusted_gateway":
            return None
        return origin + GMAIL_PUSH_PATH.format(connector_id=connector_id)

    def watch_status(
        self, connector_id: str, tenant_id: str | None,
        trigger: EventTrigger | None = None,
    ) -> dict[str, Any]:
        email = self.account(connector_id, tenant_id)
        record = self._watch(connector_id)
        token = self._token(connector_id, tenant_id)
        valid = bool(
            record
            and record.connector_revision == token.connector_revision
            and record.email == email
            and record.tenant_id == tenant_id
            and record.topic == self.settings.gmail_pubsub_topic
        )
        if trigger is not None:
            enrollment = self._enrollment(trigger)
            valid = bool(
                valid and enrollment
                and enrollment.connector_id == connector_id
                and enrollment.connector_revision == token.connector_revision
                and enrollment.email == email and enrollment.tenant_id == tenant_id
            )
        return {
            "email": email,
            "push_url": self.push_url(connector_id),
            "status": (
                "unconfigured"
                if not self.settings.gmail_pubsub_topic
                else "not_registered"
                if not valid
                else "stale"
                if record.stale
                else "expired"
                if record.expiration_ms <= int(time.time() * 1000)
                else "active"
            ),
            "expiration_ms": record.expiration_ms if valid else None,
            "history_id": record.history_id if valid else None,
        }

    async def register_watch(
        self,
        connector_id: str,
        tenant_id: str | None,
        *,
        reset_stale: bool = False,
        trigger: EventTrigger | None = None,
    ) -> dict[str, Any]:
        if not self.settings.gmail_pubsub_topic or not self.settings.gmail_push_service_account:
            raise GmailError(
                "GMAIL_PUSH_UNCONFIGURED", "Configure the Gmail Pub/Sub topic and push identity"
            )
        async with self._lock(connector_id):
            token = self._token(connector_id, tenant_id)
            if token.tenant_id != tenant_id:
                raise GmailError(
                    "GMAIL_TENANT_MISMATCH", "Gmail account is outside this tenant", 403
                )
            prior = self._watch(connector_id)
            same = bool(
                prior
                and prior.connector_revision == token.connector_revision
                and prior.email == token.email
                and prior.tenant_id == tenant_id
                and prior.topic == self.settings.gmail_pubsub_topic
            )
            if same and prior.stale and not reset_stale:
                raise GmailError(
                    "GMAIL_HISTORY_STALE", "Confirm a cursor reset to skip unavailable mail"
                )
            # _gmail may refresh a token, which acquires the same lock. Refresh first.
        token = await self._access(connector_id, tenant_id)
        async with self._lock(connector_id):
            result = await self._json(
                "POST",
                GMAIL_API + "/watch",
                headers={"Authorization": f"Bearer {token.access_token}"},
                payload={
                    "topicName": self.settings.gmail_pubsub_topic,
                    "labelIds": ["INBOX"],
                    "labelFilterBehavior": "INCLUDE",
                },
            )
            history_id = result.get("historyId")
            expiration = result.get("expiration")
            if (
                not isinstance(history_id, str)
                or not _HISTORY_ID.fullmatch(history_id)
                or not isinstance(expiration, str)
                or not expiration.isdecimal()
                or int(expiration) <= int(time.time() * 1000)
            ):
                raise GmailError("GMAIL_WATCH_INVALID", "Gmail returned an invalid watch", 502)
            current = self._token(connector_id, tenant_id)
            if (
                current.connector_revision != token.connector_revision
                or current.email != token.email
            ):
                raise GmailError("GMAIL_CONNECTOR_CHANGED", "Gmail connector changed")
            prior = self._watch(connector_id)
            same = bool(
                prior
                and prior.connector_revision == token.connector_revision
                and prior.email == token.email
                and prior.tenant_id == tenant_id
                and prior.topic == self.settings.gmail_pubsub_topic
            )
            self._save_watch(
                GmailWatch(
                    connector_id=connector_id,
                    connector_revision=token.connector_revision,
                    tenant_id=tenant_id,
                    email=token.email,
                    topic=self.settings.gmail_pubsub_topic,
                    history_id=(prior.history_id if same and not reset_stale else history_id),
                    expiration_ms=int(expiration),
                    stale=False,
                    updated_at=utc_now().isoformat(),
                )
            )
            if trigger is not None:
                if trigger.gmail_connector_id != connector_id:
                    raise GmailError("GMAIL_TRIGGER_INVALID", "Gmail trigger connector changed")
                previous = self._enrollment(trigger)
                enrollment = (
                    previous if previous and not reset_stale
                    and previous.connector_revision == token.connector_revision
                    and previous.email == token.email and previous.tenant_id == tenant_id
                    else GmailEnrollment(
                        project_id=trigger.project_id, trigger_id=trigger.id,
                        connector_id=connector_id, connector_revision=token.connector_revision,
                        tenant_id=tenant_id, email=token.email,
                        start_history_id=history_id,
                    )
                )
                self._save_enrollment(trigger, enrollment)
        return self.watch_status(connector_id, tenant_id, trigger)

    async def stop_watch(
        self, connector_id: str, tenant_id: str | None, trigger: EventTrigger,
    ) -> dict[str, Any]:
        if any(
            active.id != trigger.id
            for active, _ in self._triggers(connector_id, tenant_id)
        ):
            raise GmailError(
                "GMAIL_WATCH_SHARED", "Pause other Gmail triggers before stopping the watch"
            )
        token = await self._access(connector_id, tenant_id)
        async with self._lock(connector_id):
            record = self._watch(connector_id)
            if record and record.email == token.email and record.tenant_id == tenant_id:
                await self._json(
                    "POST",
                    GMAIL_API + "/stop",
                    headers={"Authorization": f"Bearer {token.access_token}"},
                )
                self._watch_path(connector_id).unlink(missing_ok=True)
                self._enrollment_path(trigger).unlink(missing_ok=True)
        return self.watch_status(connector_id, tenant_id, trigger)

    async def _cert_keys(self, *, unknown_kid: bool = False) -> dict[str, Any]:
        now = time.time()
        if (
            self._certs is None or self._certs_expires <= now
            or (unknown_kid and now - self._certs_fetched_at >= 60)
        ):
            result = await self._json("GET", GOOGLE_CERTS_URL)
            if not isinstance(result.get("keys"), list) or len(result["keys"]) > 20:
                raise GmailError("GMAIL_PUSH_CERTS", "Google signing keys are unavailable", 503)
            self._certs = result
            self._certs_fetched_at = now
            self._certs_expires = now + 3600
        return self._certs

    async def verify_push(self, connector_id: str, authorization: str) -> None:
        audience = self.push_url(connector_id)
        expected_email = self.settings.gmail_push_service_account
        if not audience or not expected_email or not authorization.startswith("Bearer "):
            raise GmailError("GMAIL_PUSH_UNAUTHORIZED", "Invalid Gmail push identity", 401)
        token = authorization[7:]
        if len(token) > 8_000:
            raise GmailError("GMAIL_PUSH_UNAUTHORIZED", "Invalid Gmail push identity", 401)
        try:
            header = jwt.get_unverified_header(token)
            if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
                raise ValueError
            jwks = jwt.PyJWKSet.from_dict(await self._cert_keys())
            key = next((key for key in jwks.keys if key.key_id == header["kid"]), None)
            if key is None:
                jwks = jwt.PyJWKSet.from_dict(await self._cert_keys(unknown_kid=True))
                key = next(key for key in jwks.keys if key.key_id == header["kid"])
            claims = jwt.decode(
                token,
                key.key,
                algorithms=["RS256"],
                audience=audience,
                issuer=["accounts.google.com", "https://accounts.google.com"],
                options={"require": ["exp", "iat", "aud", "iss", "email"]},
            )
            if claims.get("email") != expected_email or claims.get("email_verified") not in (
                True,
                "true",
            ):
                raise ValueError
        except (jwt.PyJWTError, StopIteration, ValueError, TypeError):
            raise GmailError(
                "GMAIL_PUSH_UNAUTHORIZED", "Invalid Gmail push identity", 401
            ) from None

    @staticmethod
    def notification(body: bytes) -> tuple[str, str]:
        try:
            envelope = json.loads(body)
            if not isinstance(envelope, dict) or not isinstance(envelope["message"], dict):
                raise ValueError
            encoded = envelope["message"]["data"]
            if not isinstance(encoded, str) or len(encoded) > 4_000:
                raise ValueError
            raw = base64.b64decode(encoded, validate=True)
            message = json.loads(raw)
            email = message["emailAddress"]
            history_id = message["historyId"]
            if (
                not isinstance(email, str)
                or not _EMAIL.fullmatch(email)
                or not isinstance(history_id, str)
                or not _HISTORY_ID.fullmatch(history_id)
            ):
                raise ValueError
            return email.lower(), history_id
        except (KeyError, TypeError, ValueError, binascii.Error):
            raise GmailError("GMAIL_PUSH_INVALID", "Invalid Gmail push message", 400) from None

    @staticmethod
    def _message_payload(raw: dict[str, Any], expected_email: str) -> dict[str, str] | None:
        labels = raw.get("labelIds", [])
        if (
            not isinstance(labels, list)
            or "INBOX" not in labels
            or any(label in labels for label in ("SENT", "DRAFT", "TRASH"))
        ):
            return None
        gmail_id = raw.get("id")
        if not isinstance(gmail_id, str) or not _GMAIL_ID.fullmatch(gmail_id):
            raise GmailError("GMAIL_MESSAGE_INVALID", "Gmail message ID is invalid", 502)
        payload = raw.get("payload")
        if not isinstance(payload, dict):
            raise GmailError("GMAIL_MESSAGE_INVALID", "Gmail message content is invalid", 502)
        headers = payload.get("headers", [])
        if not isinstance(headers, list) or len(headers) > 200:
            raise GmailError("GMAIL_MESSAGE_INVALID", "Gmail message headers are invalid", 502)
        values: dict[str, str] = {}
        for header in headers:
            if (
                isinstance(header, dict)
                and isinstance(header.get("name"), str)
                and isinstance(header.get("value"), str)
            ):
                key = header["name"].lower()
                if key in {"from", "subject", "to"} and key not in values:
                    values[key] = header["value"]
        sender = values.get("from", "")
        subject = values.get("subject", "") or "[No subject]"
        if not sender or len(sender.encode()) > 500 or len(subject.encode()) > 4_000:
            raise GmailError("GMAIL_MESSAGE_INVALID", "Gmail sender or subject is invalid", 502)

        def plain(part: dict[str, Any], depth: int = 0) -> str | None:
            if depth > 8:
                raise GmailError("GMAIL_MESSAGE_INVALID", "Gmail MIME nesting is too deep", 502)
            if part.get("mimeType") == "text/plain" and not part.get("filename"):
                body = part.get("body")
                if isinstance(body, dict) and isinstance(body.get("data"), str):
                    encoded = body["data"]
                    if len(encoded) > 70_000:
                        raise GmailError(
                            "GMAIL_MESSAGE_LARGE", "Gmail message body is too large", 502
                        )
                    try:
                        decoded = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
                        if len(decoded) > 48_000:
                            raise ValueError
                        return decoded.decode("utf-8")
                    except (UnicodeError, ValueError, binascii.Error):
                        raise GmailError(
                            "GMAIL_MESSAGE_INVALID",
                            "Gmail plain text is invalid",
                            502,
                        ) from None
            parts = part.get("parts", [])
            if not isinstance(parts, list) or len(parts) > 100:
                raise GmailError("GMAIL_MESSAGE_INVALID", "Gmail MIME parts are invalid", 502)
            for child in parts:
                if isinstance(child, dict):
                    found = plain(child, depth + 1)
                    if found is not None:
                        return found
            return None

        return {
            "message_id": gmail_id,
            "from": sender,
            "subject": subject,
            "body": plain(payload) or "[No plain-text body]",
            "recipient": expected_email,
        }

    def _triggers(
        self, connector_id: str, tenant_id: str | None,
    ) -> list[tuple[EventTrigger, GmailEnrollment]]:
        try:
            token = self._token(connector_id, tenant_id)
        except GmailError:
            return []
        result = []
        for path in self.ledger.projects_dir.glob("*/event-triggers/*.json"):
            try:
                trigger = self.triggers.get(path.parent.parent.name, path.stem)
                if (
                    trigger.gmail_connector_id == connector_id
                    and trigger.enabled
                    and (trigger.principal_binding.tenant_id if trigger.principal_binding else None)
                    == tenant_id
                ):
                    enrollment = self._enrollment(trigger)
                    if (
                        enrollment is not None
                        and enrollment.connector_id == connector_id
                        and enrollment.connector_revision == token.connector_revision
                        and enrollment.email == token.email
                        and enrollment.tenant_id == tenant_id
                    ):
                        result.append((trigger, enrollment))
            except (KeyError, ValueError):
                continue
        return result

    async def process_push(
        self,
        connector_id: str,
        body: bytes,
        dispatch: Callable[
            [EventTrigger, str, bytes, dict[str, str]], Awaitable[dict[str, object]]
        ],
    ) -> dict[str, object]:
        email, notified_id = self.notification(body)
        async with self._lock(connector_id):
            watch = self._watch(connector_id)
            if watch is None or watch.email != email:
                return {"status": "ignored"}
            if watch.stale:
                raise GmailError("GMAIL_HISTORY_STALE", "Gmail history expired; reset explicitly")
            try:
                token = self._token(connector_id, watch.tenant_id)
            except GmailError:
                return {"status": "ignored"}
            if token.email != watch.email or token.connector_revision != watch.connector_revision:
                return {"status": "ignored"}
            if int(notified_id) <= int(watch.history_id):
                return {"status": "duplicate"}
            # Refresh outside the mailbox lock to avoid a recursive lock acquisition.
        token = await self._access(connector_id, watch.tenant_id)
        async with self._lock(connector_id):
            watch = self._watch(connector_id)
            if watch is None or watch.email != email:
                return {"status": "ignored"}
            if watch.stale:
                raise GmailError("GMAIL_HISTORY_STALE", "Gmail history expired; reset explicitly")
            if int(notified_id) <= int(watch.history_id):
                return {"status": "duplicate"}
            page_token: str | None = None
            seen: set[str] = set()
            requested_start = watch.history_id
            latest = watch.history_id
            count = 0
            try:
                for _ in range(MAX_HISTORY_PAGES):
                    params = {
                        "startHistoryId": requested_start,
                        "historyTypes": "messageAdded",
                        "maxResults": "500",
                    }
                    if page_token:
                        params["pageToken"] = page_token
                    history = await self._json(
                        "GET",
                        GMAIL_API + "/history",
                        headers={"Authorization": f"Bearer {token.access_token}"},
                        params=params,
                    )
                    records = history.get("history", [])
                    if not isinstance(records, list):
                        raise GmailError("GMAIL_HISTORY_INVALID", "Gmail history is invalid", 502)
                    for record in records:
                        if not isinstance(record, dict):
                            raise GmailError(
                                "GMAIL_HISTORY_INVALID", "Gmail history is invalid", 502
                            )
                        history_record_id = record.get("id")
                        if (
                            not isinstance(history_record_id, str)
                            or not _HISTORY_ID.fullmatch(history_record_id)
                        ):
                            raise GmailError(
                                "GMAIL_HISTORY_INVALID", "Gmail history record is invalid", 502
                            )
                        added = record.get("messagesAdded", [])
                        if not isinstance(added, list):
                            raise GmailError(
                                "GMAIL_HISTORY_INVALID", "Gmail history is invalid", 502
                            )
                        for item in added:
                            message = item.get("message") if isinstance(item, dict) else None
                            message_id = message.get("id") if isinstance(message, dict) else None
                            if not isinstance(message_id, str) or not _GMAIL_ID.fullmatch(
                                message_id
                            ):
                                raise GmailError(
                                    "GMAIL_HISTORY_INVALID", "Gmail message ID is invalid", 502
                                )
                            if message_id in seen:
                                continue
                            seen.add(message_id)
                            count += 1
                            if count > MAX_HISTORY_MESSAGES:
                                raise GmailError(
                                    "GMAIL_HISTORY_LARGE", "Gmail backlog needs review", 503
                                )
                            raw = await self._json(
                                "GET",
                                GMAIL_API + "/messages/" + message_id,
                                headers={"Authorization": f"Bearer {token.access_token}"},
                                params={"format": "full"},
                            )
                            payload = self._message_payload(raw, email)
                            if payload is None:
                                continue
                            canonical = json.dumps(
                                payload,
                                ensure_ascii=False,
                                sort_keys=True,
                            ).encode("utf-8")
                            for trigger, enrollment in self._triggers(
                                connector_id, watch.tenant_id,
                            ):
                                if int(history_record_id) <= int(enrollment.start_history_id):
                                    continue
                                outcome = await dispatch(
                                    trigger,
                                    "gmail:" + message_id,
                                    canonical,
                                    payload,
                                )
                                if outcome.get("status") != "accepted":
                                    raise GmailError(
                                        "GMAIL_DISPATCH_UNCERTAIN",
                                        "Gmail dispatch is uncertain",
                                        503,
                                    )
                        if int(history_record_id) > int(watch.history_id):
                            watch = watch.model_copy(update={
                                "history_id": history_record_id,
                                "updated_at": utc_now().isoformat(),
                            })
                            self._save_watch(watch)
                    cursor = history.get("historyId")
                    if not isinstance(cursor, str) or not _HISTORY_ID.fullmatch(cursor):
                        raise GmailError(
                            "GMAIL_HISTORY_INVALID", "Gmail history cursor is invalid", 502
                        )
                    latest = cursor
                    page_token = history.get("nextPageToken")
                    if not page_token:
                        break
                    if not isinstance(page_token, str) or len(page_token) > 2_000:
                        raise GmailError(
                            "GMAIL_HISTORY_INVALID", "Gmail page token is invalid", 502
                        )
                else:
                    raise GmailError("GMAIL_HISTORY_LARGE", "Gmail backlog needs review", 503)
            except GmailError as exc:
                if exc.code == "GMAIL_HISTORY_STALE" and watch.history_id == requested_start:
                    self._save_watch(
                        watch.model_copy(
                            update={
                                "stale": True,
                                "updated_at": utc_now().isoformat(),
                            }
                        )
                    )
                raise
            current = self._watch(connector_id)
            if current != watch or self._token(connector_id, watch.tenant_id).email != email:
                raise GmailError("GMAIL_CONNECTOR_CHANGED", "Gmail connector changed")
            if int(latest) < int(watch.history_id):
                raise GmailError("GMAIL_HISTORY_INVALID", "Gmail cursor moved backward", 502)
            self._save_watch(
                watch.model_copy(
                    update={
                        "history_id": latest,
                        "updated_at": utc_now().isoformat(),
                    }
                )
            )
            return {"status": "accepted", "messages": count}

    async def renew_due(self) -> None:
        for path in self.root.glob("*.json"):
            try:
                watch = self._watch(path.stem)
                if (
                    watch
                    and not watch.stale
                    and watch.expiration_ms < int((time.time() + 2 * 86_400) * 1000)
                    and self._triggers(watch.connector_id, watch.tenant_id)
                ):
                    await self.register_watch(watch.connector_id, watch.tenant_id)
            except (GmailError, ValueError):
                LOGGER.exception("Gmail watch renewal failed for %s", path.stem)

    async def start(self) -> None:
        if self._renew_task is not None:
            return

        async def worker() -> None:
            while True:
                await self.renew_due()
                await asyncio.sleep(6 * 3600)

        self._renew_task = asyncio.create_task(worker(), name="gmail-watch-renewal")

    async def stop(self) -> None:
        if self._renew_task is not None:
            self._renew_task.cancel()
            try:
                await self._renew_task
            except asyncio.CancelledError:
                pass
            self._renew_task = None
        if self._owns_client:
            await self.client.aclose()
