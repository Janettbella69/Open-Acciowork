"""Read-only Microsoft Graph Outlook OAuth and bounded Inbox observations."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import secrets
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .config import Settings

if TYPE_CHECKING:
    from .connectors import ConnectorRegistry

OUTLOOK_SCOPE = "offline_access User.Read Mail.Read"
OUTLOOK_CALLBACK_PATH = "/api/connectors/{connector_id}/outlook-oauth/callback"
OUTLOOK_CALLBACK_RE = re.compile(r"/api/connectors/[A-Za-z0-9_-]+/outlook-oauth/callback")
MICROSOFT_AUTH_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
MICROSOFT_TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
GRAPH_API = "https://graph.microsoft.com/v1.0"
INBOX_MESSAGES_URL = GRAPH_API + "/me/mailFolders/inbox/messages"
MAX_GRAPH_RESPONSE = 1_000_000
_CLIENT_ID = re.compile(r"^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")
_MESSAGE_ID = re.compile(r"^[A-Za-z0-9+/_=-]{1,512}$")
_EMAIL = re.compile(r"^[^\s@<>]{1,64}@[A-Za-z0-9.-]{1,190}$")
_PENDING = "outlook-oauth-pending:"
_TOKEN = "outlook-oauth-token:"


def _bounded_utf8(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return value[:limit].encode("utf-8", errors="replace").decode("utf-8")


class OutlookError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class OutlookCredentials(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    client_id: str = Field(min_length=36, max_length=36)
    client_secret: str = Field(min_length=16, max_length=512)


def parse_outlook_credentials(raw: str) -> OutlookCredentials:
    try:
        if len(raw) > 2_000:
            raise ValueError
        value = OutlookCredentials.model_validate_json(raw)
        if not _CLIENT_ID.fullmatch(value.client_id) or any(
            ord(char) < 33 or ord(char) > 126 for char in value.client_secret
        ):
            raise ValueError
        return value
    except (ValidationError, ValueError):
        raise ValueError("Outlook requires a Microsoft app client ID and secret") from None


def outlook_oauth_vault_keys(connector_id: str) -> tuple[str, ...]:
    return (
        _PENDING + connector_id, _TOKEN + connector_id,
        "outlook-subscription-state:" + connector_id,
    )


class OutlookToken(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    user_id: str = Field(min_length=1, max_length=255)
    email: str = Field(pattern=_EMAIL.pattern)
    tenant_id: str | None
    connector_revision: int
    refresh_token: str
    access_token: str
    expires_at: float


class OutlookService:
    def __init__(
        self, settings: Settings, connectors: ConnectorRegistry,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self.connectors = connectors
        self.client = client or httpx.AsyncClient(timeout=15, trust_env=False)
        self._owns_client = client is None
        self._locks: dict[str, asyncio.Lock] = {}

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    def _connector(self, connector_id: str) -> tuple[int, OutlookCredentials]:
        try:
            connector = self.connectors.get(connector_id)
            if connector.type != "outlook" or not connector.enabled:
                raise ValueError
            raw = self.connectors.vault.get(connector_id) if self.connectors.vault else None
            if raw is None and connector.secret_env:
                import os

                raw = os.environ.get(connector.secret_env)
            if raw is None:
                raise ValueError
            return connector.revision, parse_outlook_credentials(raw)
        except (KeyError, ValueError):
            raise OutlookError(
                "OUTLOOK_CONNECTOR_UNAVAILABLE", "Configure Outlook OAuth credentials first"
            ) from None

    def _token(self, connector_id: str, tenant_id: str | None = None) -> OutlookToken:
        revision, _ = self._connector(connector_id)
        vault = self.connectors.vault
        raw = vault.get(_TOKEN + connector_id) if vault else None
        try:
            token = OutlookToken.model_validate_json(raw or "")
            if token.connector_revision != revision or (
                tenant_id is not None and token.tenant_id != tenant_id
            ):
                raise ValueError
            return token
        except (ValidationError, ValueError):
            raise OutlookError(
                "OUTLOOK_AUTH_REQUIRED", "Authorize the Outlook account first"
            ) from None

    def token_owner(self, connector_id: str) -> tuple[bool, str | None]:
        vault = self.connectors.vault
        raw = vault.get(_TOKEN + connector_id) if vault else None
        try:
            token = OutlookToken.model_validate_json(raw or "")
            return True, token.tenant_id
        except ValidationError:
            return False, None

    def account(self, connector_id: str, tenant_id: str | None) -> tuple[str, str]:
        token = self._token(connector_id, tenant_id)
        if token.tenant_id != tenant_id:
            raise OutlookError(
                "OUTLOOK_TENANT_MISMATCH", "Outlook account is outside this tenant", 403
            )
        return token.user_id, token.email

    def oauth_status(self, connector_id: str, tenant_id: str | None) -> dict[str, Any]:
        callback = (
            self.settings.event_public_origin + OUTLOOK_CALLBACK_PATH.format(
                connector_id=connector_id,
            )
            if self.settings.auth_mode == "trusted_gateway"
            and self.settings.event_public_origin else None
        )
        try:
            token = self._token(connector_id, tenant_id)
            authorized = token.tenant_id == tenant_id
        except OutlookError:
            token = None
            authorized = False
        return {
            "callback_url": callback, "authorized": authorized,
            "email": token.email if authorized and token else None,
        }

    def begin_oauth(self, connector_id: str, tenant_id: str | None) -> dict[str, str]:
        origin = self.settings.event_public_origin
        if self.settings.auth_mode != "trusted_gateway" or origin is None:
            raise OutlookError("OUTLOOK_ORIGIN_REQUIRED", "Configure a public HTTPS event origin")
        revision, credentials = self._connector(connector_id)
        state = secrets.token_urlsafe(32)
        pending = {
            "state_hash": hashlib.sha256(state.encode()).hexdigest(),
            "revision": revision, "tenant_id": tenant_id,
            "expires_at": time.time() + 600,
        }
        assert self.connectors.vault is not None
        with self.connectors._transaction():
            owned, owner = self.token_owner(connector_id)
            if owned and owner != tenant_id:
                raise OutlookError(
                    "OUTLOOK_TENANT_MISMATCH", "Outlook account is outside this tenant", 403
                )
            if self._connector(connector_id)[0] != revision:
                raise OutlookError("OUTLOOK_CONNECTOR_CHANGED", "Outlook connector changed")
            self.connectors.vault.set(_PENDING + connector_id, json.dumps(pending))
        callback = origin + OUTLOOK_CALLBACK_PATH.format(connector_id=connector_id)
        query = urlencode({
            "client_id": credentials.client_id, "redirect_uri": callback,
            "response_type": "code", "response_mode": "query",
            "scope": OUTLOOK_SCOPE, "state": state,
        })
        return {"authorization_url": MICROSOFT_AUTH_URL + "?" + query,
                "callback_url": callback}

    async def complete_oauth(self, connector_id: str, raw_query: bytes) -> None:
        if len(raw_query) > 16_000:
            raise OutlookError("OUTLOOK_OAUTH_INVALID", "Invalid Outlook callback", 403)
        try:
            pairs = parse_qsl(
                raw_query.decode("ascii"), keep_blank_values=True, strict_parsing=True,
                max_num_fields=8, encoding="utf-8", errors="strict",
            )
            values = dict(pairs)
            if len(values) != len(pairs):
                raise ValueError
        except (UnicodeError, ValueError):
            raise OutlookError("OUTLOOK_OAUTH_INVALID", "Invalid Outlook callback", 403) from None
        revision, credentials = self._connector(connector_id)
        state = values.get("state", "")
        if not 32 <= len(state) <= 128 or not state.isascii():
            raise OutlookError("OUTLOOK_OAUTH_INVALID", "Invalid Outlook callback", 403)
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
                raise OutlookError(
                    "OUTLOOK_OAUTH_EXPIRED", "Outlook authorization expired", 403
                )
            self.connectors.vault.delete(_PENDING + connector_id)
        if values.get("error"):
            raise OutlookError("OUTLOOK_OAUTH_DENIED", "Outlook authorization was declined", 403)
        code = values.get("code", "")
        if not 1 <= len(code) <= 8_000 or any(ord(char) < 33 for char in code):
            raise OutlookError("OUTLOOK_OAUTH_INVALID", "Invalid Outlook code", 403)
        callback = self.settings.event_public_origin + OUTLOOK_CALLBACK_PATH.format(
            connector_id=connector_id,
        )
        response = await self._json("POST", MICROSOFT_TOKEN_URL, data={
            "client_id": credentials.client_id, "client_secret": credentials.client_secret,
            "code": code, "grant_type": "authorization_code", "redirect_uri": callback,
            "scope": OUTLOOK_SCOPE,
        })
        refresh = response.get("refresh_token")
        access = response.get("access_token")
        scope = response.get("scope")
        expires = response.get("expires_in")
        if (
            not isinstance(refresh, str) or not refresh
            or not isinstance(access, str) or not access
            or not isinstance(scope, str)
            or not {"user.read", "mail.read"}.issubset(set(scope.lower().split()))
            or type(expires) is not int or expires <= 60
            or not isinstance(response.get("token_type"), str)
            or response["token_type"].lower() != "bearer"
        ):
            raise OutlookError(
                "OUTLOOK_OAUTH_RESPONSE", "Outlook authorization did not grant read access", 502
            )
        profile = await self._json(
            "GET", GRAPH_API + "/me", headers={"Authorization": f"Bearer {access}"},
            params={"$select": "id,mail,userPrincipalName"},
        )
        user_id = profile.get("id")
        email = profile.get("mail") or profile.get("userPrincipalName")
        if (
            not isinstance(user_id, str) or not 1 <= len(user_id) <= 255
            or not isinstance(email, str) or not _EMAIL.fullmatch(email)
        ):
            raise OutlookError(
                "OUTLOOK_PROFILE_INVALID", "Outlook account identity is invalid", 502
            )
        with self.connectors._transaction():
            if self._connector(connector_id)[0] != revision:
                raise OutlookError("OUTLOOK_CONNECTOR_CHANGED", "Outlook connector changed")
            owned, owner = self.token_owner(connector_id)
            if owned and owner != pending.get("tenant_id"):
                raise OutlookError(
                    "OUTLOOK_TENANT_MISMATCH", "Outlook account is outside this tenant", 403
                )
            self.connectors.vault.set(
                _TOKEN + connector_id,
                OutlookToken(
                    user_id=user_id, email=email.lower(), tenant_id=pending.get("tenant_id"),
                    connector_revision=revision, refresh_token=refresh, access_token=access,
                    expires_at=time.time() + expires,
                ).model_dump_json(),
            )

    async def _json(
        self, method: str, url: str, *, headers: dict[str, str] | None = None,
        data: dict[str, str] | None = None, params: dict[str, str] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            async with self.client.stream(
                method, url, headers=headers, data=data, params=params, json=payload,
            ) as response:
                if response.status_code == 404:
                    raise OutlookError("OUTLOOK_NOT_FOUND", "Outlook item was not found", 404)
                if response.status_code >= 400:
                    raise OutlookError(
                        "OUTLOOK_UPSTREAM", "Microsoft rejected the request", 502
                    )
                chunks = bytearray()
                async for chunk in response.aiter_bytes():
                    chunks.extend(chunk)
                    if len(chunks) > MAX_GRAPH_RESPONSE:
                        raise OutlookError(
                            "OUTLOOK_RESPONSE_LARGE", "Microsoft response is too large", 502
                        )
            result = json.loads(chunks) if chunks else {}
            if not isinstance(result, dict):
                raise ValueError
            return result
        except (httpx.HTTPError, ValueError) as exc:
            raise OutlookError("OUTLOOK_UPSTREAM", "Outlook request failed", 502) from exc

    async def read_inbox_message(
        self, connector_id: str, tenant_id: str | None, message_id: str,
    ) -> dict[str, str] | None:
        """Fetch one immutable Graph ID; ignore mail moved out of the Inbox."""
        if not _MESSAGE_ID.fullmatch(message_id):
            raise OutlookError("OUTLOOK_MESSAGE_INVALID", "Outlook message ID is invalid", 400)
        token = await self._access(connector_id, tenant_id)
        headers = {
            "Authorization": f"Bearer {token.access_token}",
            "Prefer": 'outlook.body-content-type="text", IdType="ImmutableId"',
        }
        folder = await self._json(
            "GET", GRAPH_API + "/me/mailFolders/inbox", headers=headers,
            params={"$select": "id"},
        )
        folder_id = folder.get("id")
        if not isinstance(folder_id, str) or not 1 <= len(folder_id) <= 512:
            raise OutlookError("OUTLOOK_FOLDER_INVALID", "Outlook Inbox is invalid", 502)
        try:
            raw = await self._json(
                "GET", GRAPH_API + "/me/messages/" + quote(message_id, safe=""),
                headers=headers,
                params={"$select": "id,parentFolderId,isDraft,from,subject,body,receivedDateTime"},
            )
        except OutlookError as exc:
            if exc.code == "OUTLOOK_NOT_FOUND":
                return None
            raise
        if raw.get("id") != message_id:
            raise OutlookError("OUTLOOK_MESSAGE_INVALID", "Outlook message ID changed", 502)
        if raw.get("parentFolderId") != folder_id or raw.get("isDraft") is not False:
            return None
        body = raw.get("body")
        if (
            not isinstance(body, dict) or not isinstance(body.get("contentType"), str)
            or body["contentType"].lower() != "text" or not isinstance(body.get("content"), str)
        ):
            raise OutlookError("OUTLOOK_MESSAGE_INVALID", "Outlook did not return plain text", 502)
        sender = raw.get("from")
        address = (
            sender.get("emailAddress", {}).get("address")
            if isinstance(sender, dict) and isinstance(sender.get("emailAddress"), dict)
            else None
        )
        subject = raw.get("subject")
        return {
            "message_id": message_id,
            "from": _bounded_utf8(address, 255),
            "subject": _bounded_utf8(subject, 300),
            "body": _bounded_utf8(body["content"], 3500) or "[No plain-text body]",
            "recipient": token.email,
        }

    async def _access(self, connector_id: str, tenant_id: str | None) -> OutlookToken:
        token = self._token(connector_id, tenant_id)
        if token.expires_at > time.time() + 60:
            return token
        async with self._locks.setdefault(connector_id, asyncio.Lock()):
            token = self._token(connector_id, tenant_id)
            if token.expires_at > time.time() + 60:
                return token
            revision, credentials = self._connector(connector_id)
            result = await self._json("POST", MICROSOFT_TOKEN_URL, data={
                "client_id": credentials.client_id, "client_secret": credentials.client_secret,
                "refresh_token": token.refresh_token, "grant_type": "refresh_token",
                "scope": OUTLOOK_SCOPE,
            })
            access = result.get("access_token")
            refresh = result.get("refresh_token", token.refresh_token)
            scope = result.get("scope")
            expires = result.get("expires_in")
            if (
                not isinstance(access, str) or not access
                or not isinstance(refresh, str) or not refresh
                or not isinstance(scope, str)
                or not {"user.read", "mail.read"}.issubset(set(scope.lower().split()))
                or type(expires) is not int or expires <= 60
                or not isinstance(result.get("token_type"), str)
                or result["token_type"].lower() != "bearer"
            ):
                raise OutlookError(
                    "OUTLOOK_REFRESH_FAILED", "Outlook authorization needs renewal", 502
                )
            updated = token.model_copy(update={
                "access_token": access, "refresh_token": refresh,
                "expires_at": time.time() + expires,
            })
            with self.connectors._transaction():
                if self._connector(connector_id)[0] != revision:
                    raise OutlookError("OUTLOOK_CONNECTOR_CHANGED", "Outlook connector changed")
                assert self.connectors.vault is not None
                # Another worker may have stored a newer refresh token. Keep it.
                current = self._token(connector_id, tenant_id)
                if current.refresh_token != token.refresh_token:
                    return current
                self.connectors.vault.set(_TOKEN + connector_id, updated.model_dump_json())
            return updated

    @staticmethod
    def _next_link(raw: Any, page_size: int) -> str | None:
        if raw is None:
            return None
        if (
            not isinstance(raw, str) or not 1 <= len(raw) <= 4096
            or any(ord(char) < 33 or ord(char) == 127 for char in raw)
        ):
            raise OutlookError("OUTLOOK_PAGE_INVALID", "Outlook page cursor is invalid", 502)
        try:
            parts = urlsplit(raw)
            pairs = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=True)
            params = dict(pairs)
            if (
                parts.scheme != "https" or parts.netloc != "graph.microsoft.com"
                or parts.path != "/v1.0/me/mailFolders/inbox/messages"
                or parts.fragment or len(params) != len(pairs)
                or set(params) - {"$select", "$top", "$orderby", "$skip", "$skiptoken"}
                or params.get("$select") != "id"
                or params.get("$top") != str(page_size)
                or params.get("$orderby") != "receivedDateTime desc"
                or not ({"$skip", "$skiptoken"} & set(params))
                or ("$skip" in params and not re.fullmatch(r"[0-9]{1,12}", params["$skip"]))
                or ("$skiptoken" in params and not 1 <= len(params["$skiptoken"]) <= 4000)
            ):
                raise ValueError
        except ValueError:
            raise OutlookError(
                "OUTLOOK_PAGE_INVALID", "Outlook page cursor is invalid", 502
            ) from None
        return raw

    async def read_inbox_page(
        self, connector_id: str, tenant_id: str | None, *, page_size: int,
        page_token: str | None, authorize: Callable[[], None],
    ) -> dict[str, Any]:
        authorize()
        url = self._next_link(page_token, page_size) if page_token else INBOX_MESSAGES_URL
        token = await self._access(connector_id, tenant_id)
        authorize()
        headers = {"Authorization": f"Bearer {token.access_token}"}
        folder = await self._json(
            "GET", GRAPH_API + "/me/mailFolders/inbox", headers=headers,
            params={"$select": "id"},
        )
        authorize()
        folder_id = folder.get("id")
        if not isinstance(folder_id, str) or not 1 <= len(folder_id) <= 512:
            raise OutlookError("OUTLOOK_FOLDER_INVALID", "Outlook Inbox is invalid", 502)
        listed = await self._json(
            "GET", url, headers=headers,
            params=None if page_token else {
                "$select": "id", "$top": str(page_size),
                "$orderby": "receivedDateTime desc",
            },
        )
        authorize()
        items = listed.get("value")
        if not isinstance(items, list) or len(items) > page_size or any(
            not isinstance(item, dict) or not isinstance(item.get("id"), str)
            or not _MESSAGE_ID.fullmatch(item["id"])
            for item in items
        ):
            raise OutlookError("OUTLOOK_PAGE_INVALID", "Outlook Inbox page is invalid", 502)
        next_page = self._next_link(listed.get("@odata.nextLink"), page_size)
        messages = []
        for item in items:
            authorize()
            message_id = item["id"]
            raw = await self._json(
                "GET", GRAPH_API + "/me/messages/" + quote(message_id, safe=""),
                headers={**headers, "Prefer": 'outlook.body-content-type="text"'},
                params={"$select": "id,parentFolderId,isDraft,from,subject,body,receivedDateTime"},
            )
            authorize()
            if raw.get("id") != message_id:
                raise OutlookError("OUTLOOK_MESSAGE_INVALID", "Outlook message ID changed", 502)
            if raw.get("parentFolderId") != folder_id or raw.get("isDraft") is not False:
                continue
            body = raw.get("body")
            if (
                not isinstance(body, dict)
                or not isinstance(body.get("contentType"), str)
                or body["contentType"].lower() != "text"
            ):
                raise OutlookError(
                    "OUTLOOK_MESSAGE_INVALID", "Outlook did not return plain text", 502
                )
            content = body.get("content")
            if not isinstance(content, str):
                raise OutlookError("OUTLOOK_MESSAGE_INVALID", "Outlook body is invalid", 502)
            sender = raw.get("from")
            address = (
                sender.get("emailAddress", {}).get("address")
                if isinstance(sender, dict) and isinstance(sender.get("emailAddress"), dict)
                else None
            )
            subject = raw.get("subject")
            received = raw.get("receivedDateTime")
            messages.append({
                "message_id": message_id,
                "from": _bounded_utf8(address, 255),
                "subject": _bounded_utf8(subject, 300),
                "received_at": _bounded_utf8(received, 80) if isinstance(received, str) else None,
                "body": _bounded_utf8(content, 3500), "body_truncated": len(content) > 3500,
            })
        return {
            "mailbox": token.email, "page_size": page_size,
            "request_page_token": page_token, "messages": messages,
            "next_page_token": next_page, "source_url": INBOX_MESSAGES_URL,
        }
