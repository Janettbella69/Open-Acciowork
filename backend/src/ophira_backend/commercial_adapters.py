"""Ports and the first production-shaped Opportunity-to-Action adapter.

``HttpsJsonMessageAdapter`` implements a deliberately small, versioned HTTPS
contract owned by the configured connector rather than by the Agent:

* ``GET {base}/v1/targets/{resource_type}/{resource_key}`` returns
  ``{"version": "...", "state": {"recipient": "...", "content": "..."}}``.
* ``POST {base}/v1/messages:send`` accepts the reviewed recipient/content and
  must return ``status=succeeded`` while echoing the ``Idempotency-Key``.

The credential is resolved directly from ``ConnectorRegistry``'s server-side
vault (or its configured environment reference), placed only in the HTTPS
Authorization header, and never returned in models or error messages.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import socket
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import quote

import httpx

from .connectors import (
    CommercialConnectorRuntime,
    ConnectorRegistry,
    ConnectorUnavailableError,
    is_public_unicast_address,
)
from .constants import COMMERCIAL_HTTPS_JSON_CONNECTOR_TYPE, CommercialActionOperation
from .models import (
    CommercialActionRecord,
    CommercialExecutionRequest,
    JsonValue,
    json_value_is_finite,
)

MAX_EXTERNAL_RESPONSE_BYTES = 64 * 1024
MESSAGE_WRITE_SCOPES = frozenset({"messages:write", "message:send"})
MESSAGE_FIELDS = frozenset({"recipient", "content"})
RESOURCE_KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+\-]{0,499}$")


class CommercialAdapterError(RuntimeError):
    """Sanitized adapter failure safe to persist in the action ledger."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True)
class ExternalTargetSnapshot:
    version: str
    state: dict[str, JsonValue]
    connector_config_hash: str


@dataclass(frozen=True)
class CommercialTargetObservation:
    """Host-observed target data suitable for a server-issued source receipt."""

    operation: CommercialActionOperation
    connector_id: str
    resource_type: str
    resource_key: str
    version: str
    state: dict[str, JsonValue]
    connector_config_hash: str
    canonical_source: str


@dataclass(frozen=True)
class ExternalCommitResult:
    remote_action_id: str
    remote_version: str | None
    http_status: int
    response_hash: str
    adapter_attempts: int


class CommercialExecutionPort(Protocol):
    """External boundary consumed by ``CommercialActionService``."""

    adapter_type: str

    def supports(self, action: CommercialActionRecord) -> bool: ...

    def inspect_target(
        self,
        action: CommercialActionRecord,
        execution: CommercialExecutionRequest,
    ) -> ExternalTargetSnapshot: ...

    def commit(
        self,
        action: CommercialActionRecord,
        execution: CommercialExecutionRequest,
        snapshot: ExternalTargetSnapshot,
    ) -> ExternalCommitResult: ...


class CommercialTargetReadPort(Protocol):
    """Read-only host boundary used before a commercial action can be staged."""

    def read_target(
        self,
        connector_id: str,
        resource_type: str,
        resource_key: str,
    ) -> CommercialTargetObservation: ...

    def current_connector_config_hash(self, connector_id: str) -> str: ...


class HttpsJsonMessageAdapter:
    """Execute one reviewed message against a strict HTTPS JSON connector."""

    adapter_type = COMMERCIAL_HTTPS_JSON_CONNECTOR_TYPE

    def __init__(
        self,
        connectors: ConnectorRegistry,
        *,
        timeout_seconds: float = 15,
        max_attempts: int = 3,
        retry_base_seconds: float = 0.25,
        retry_max_seconds: float = 2,
        environ: Mapping[str, str] | None = None,
        client: httpx.Client | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        dns_resolver: Callable[..., list[tuple]] = socket.getaddrinfo,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("commercial adapter timeout must be positive")
        if not 1 <= max_attempts <= 20:
            raise ValueError("commercial adapter attempts must be between 1 and 20")
        if retry_base_seconds < 0 or retry_max_seconds < retry_base_seconds:
            raise ValueError("commercial adapter retry bounds are invalid")
        self.connectors = connectors
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max_attempts
        self.retry_base_seconds = retry_base_seconds
        self.retry_max_seconds = retry_max_seconds
        self.environ = environ
        self._client = client
        self._sleeper = sleeper
        self._dns_resolver = dns_resolver

    def supports(self, action: CommercialActionRecord) -> bool:
        """Return true only for the vertical this adapter actually owns."""

        if action.operation != CommercialActionOperation.SEND_MESSAGE:
            return False
        execution = action.execution_request
        if execution is not None and execution.adapter_type == self.adapter_type:
            return True
        try:
            connector = self.connectors.get(action.target.connector_id)
        except (KeyError, ValueError):
            return False
        return connector.type == self.adapter_type

    def inspect_target(
        self,
        action: CommercialActionRecord,
        execution: CommercialExecutionRequest,
    ) -> ExternalTargetSnapshot:
        self._message_payload(action, execution)
        observation = self.read_target(
            action.target.connector_id,
            action.target.resource_type,
            action.target.resource_key,
        )
        return ExternalTargetSnapshot(
            version=observation.version,
            state=observation.state,
            connector_config_hash=observation.connector_config_hash,
        )

    def read_target(
        self,
        connector_id: str,
        resource_type: str,
        resource_key: str,
    ) -> CommercialTargetObservation:
        """Fetch one current message draft without accepting target state from Claude."""

        self._validate_target_selector(connector_id, resource_type, resource_key)
        runtime = self._runtime_by_id(connector_id)
        endpoint = runtime.connector.endpoint
        assert endpoint is not None
        encoded_type = quote(resource_type, safe="")
        encoded_key = quote(resource_key, safe="")
        canonical_source = (
            f"{endpoint.rstrip('/')}/v1/targets/{encoded_type}/{encoded_key}"
        )
        payload, _status, _attempts = self._request_json(
            "GET",
            canonical_source,
            runtime,
        )
        version = payload.get("version")
        state = payload.get("state")
        if not isinstance(version, str) or not version or len(version) > 500:
            raise CommercialAdapterError(
                "EXTERNAL_PROTOCOL_INVALID",
                "The connector target response did not contain a valid version.",
                retryable=False,
            )
        if (
            not isinstance(state, dict)
            or len(state) > 200
            or not json_value_is_finite(state)
        ):
            raise CommercialAdapterError(
                "EXTERNAL_PROTOCOL_INVALID",
                "The connector target response did not contain valid JSON state.",
                retryable=False,
            )
        return CommercialTargetObservation(
            operation=CommercialActionOperation.SEND_MESSAGE,
            connector_id=connector_id,
            resource_type=resource_type,
            resource_key=resource_key,
            version=version,
            state=state,
            connector_config_hash=self._connector_hash(runtime),
            canonical_source=canonical_source,
        )

    def current_connector_config_hash(self, connector_id: str) -> str:
        return self._connector_hash(self._runtime_by_id(connector_id))

    def commit(
        self,
        action: CommercialActionRecord,
        execution: CommercialExecutionRequest,
        snapshot: ExternalTargetSnapshot,
    ) -> ExternalCommitResult:
        message = self._message_payload(action, execution)
        runtime = self._runtime(action)
        if self._connector_hash(runtime) != snapshot.connector_config_hash:
            raise CommercialAdapterError(
                "CONNECTOR_CONFIG_CHANGED",
                "The connector configuration changed after target inspection.",
                retryable=False,
            )
        endpoint = runtime.connector.endpoint
        assert endpoint is not None
        idempotency_key = execution.idempotency_key or execution.id
        response, status, attempts = self._request_json(
            "POST",
            f"{endpoint.rstrip('/')}/v1/messages:send",
            runtime,
            extra_headers={"Idempotency-Key": idempotency_key},
            json_body={
                "resource_type": action.target.resource_type,
                "resource_key": action.target.resource_key,
                "expected_version": snapshot.version,
                "recipient": message["recipient"],
                "content": message["content"],
            },
        )
        echoed_key = response.get("idempotency_key")
        remote_status = response.get("status")
        remote_action_id = response.get("remote_action_id")
        remote_version = response.get("resource_version")
        if echoed_key != idempotency_key:
            raise CommercialAdapterError(
                "EXTERNAL_IDEMPOTENCY_UNCONFIRMED",
                "The connector did not confirm the server idempotency key.",
                retryable=False,
            )
        if remote_status != "succeeded":
            raise CommercialAdapterError(
                "EXTERNAL_SUCCESS_UNCONFIRMED",
                "The connector did not confirm a completed external action.",
                retryable=False,
            )
        if (
            not isinstance(remote_action_id, str)
            or not remote_action_id
            or len(remote_action_id) > 500
        ):
            raise CommercialAdapterError(
                "EXTERNAL_PROTOCOL_INVALID",
                "The connector success response did not contain a remote action ID.",
                retryable=False,
            )
        if remote_version is not None and (
            not isinstance(remote_version, str)
            or not remote_version
            or len(remote_version) > 500
        ):
            raise CommercialAdapterError(
                "EXTERNAL_PROTOCOL_INVALID",
                "The connector success response contained an invalid resource version.",
                retryable=False,
            )
        return ExternalCommitResult(
            remote_action_id=remote_action_id,
            remote_version=remote_version,
            http_status=status,
            response_hash=self._canonical_hash(response),
            adapter_attempts=attempts,
        )

    def _runtime(self, action: CommercialActionRecord) -> CommercialConnectorRuntime:
        return self._runtime_by_id(action.target.connector_id)

    def _runtime_by_id(self, connector_id: str) -> CommercialConnectorRuntime:
        try:
            runtime = self.connectors.require_commercial_action_runtime(
                connector_id,
                environ=self.environ,
            )
        except ConnectorUnavailableError as exc:
            raise CommercialAdapterError(
                exc.code,
                str(exc),
                retryable=False,
            ) from exc
        scopes = frozenset(runtime.connector.scope.split())
        if not scopes.intersection(MESSAGE_WRITE_SCOPES):
            raise CommercialAdapterError(
                "COMMERCIAL_CONNECTOR_SCOPE_DENIED",
                "The connector is missing a message-write scope.",
                retryable=False,
            )
        return runtime

    @staticmethod
    def _validate_target_selector(
        connector_id: str,
        resource_type: str,
        resource_key: str,
    ) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", connector_id):
            raise CommercialAdapterError(
                "COMMERCIAL_CONNECTOR_INVALID",
                "The commercial connector ID is invalid.",
                retryable=False,
            )
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", resource_type):
            raise CommercialAdapterError(
                "COMMERCIAL_RESOURCE_TYPE_INVALID",
                "The commercial resource type is invalid.",
                retryable=False,
            )
        if (
            not RESOURCE_KEY_PATTERN.fullmatch(resource_key)
            or resource_key in {".", ".."}
        ):
            raise CommercialAdapterError(
                "COMMERCIAL_RESOURCE_KEY_INVALID",
                "The commercial resource key is invalid.",
                retryable=False,
            )

    @staticmethod
    def _message_payload(
        action: CommercialActionRecord,
        execution: CommercialExecutionRequest,
    ) -> dict[str, str]:
        if action.operation != CommercialActionOperation.SEND_MESSAGE:
            raise CommercialAdapterError(
                "COMMERCIAL_OPERATION_UNSUPPORTED",
                "The HTTPS JSON adapter currently supports only send_message.",
                retryable=False,
            )
        if any(change.field not in MESSAGE_FIELDS for change in action.changes):
            raise CommercialAdapterError(
                "COMMERCIAL_MESSAGE_FIELD_DENIED",
                "Message actions may change only recipient or content.",
                retryable=False,
            )
        recipient = execution.projected_state.get("recipient")
        content = execution.projected_state.get("content")
        if not isinstance(recipient, str) or not recipient.strip() or len(recipient) > 500:
            raise CommercialAdapterError(
                "COMMERCIAL_RECIPIENT_INVALID",
                "The reviewed message recipient is invalid.",
                retryable=False,
            )
        if not isinstance(content, str) or not content.strip() or len(content) > 40_000:
            raise CommercialAdapterError(
                "COMMERCIAL_CONTENT_INVALID",
                "The reviewed message content is invalid.",
                retryable=False,
            )
        return {"recipient": recipient, "content": content}

    def _request_json(
        self,
        method: str,
        url: str,
        runtime: CommercialConnectorRuntime,
        *,
        extra_headers: Mapping[str, str] | None = None,
        json_body: dict[str, str] | None = None,
    ) -> tuple[dict[str, object], int, int]:
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {runtime.credential.get_secret_value()}",
        }
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        if extra_headers:
            headers.update(extra_headers)

        client = self._client or httpx.Client(
            timeout=self.timeout_seconds,
            follow_redirects=False,
            trust_env=False,
            limits=httpx.Limits(max_keepalive_connections=0),
        )
        should_close = self._client is None
        try:
            for attempt in range(1, self.max_attempts + 1):
                try:
                    # Resolve on every attempt before any credential-bearing
                    # request. All returned addresses must be globally routable;
                    # redirects remain disabled below.
                    pinned_url, host_header, sni_hostname = (
                        self._pinned_public_destination(url, attempt)
                    )
                    request_headers = {**headers, "Host": host_header}
                    extensions = {"sni_hostname": sni_hostname}
                    with client.stream(
                        method,
                        pinned_url,
                        headers=request_headers,
                        json=json_body,
                        timeout=self.timeout_seconds,
                        follow_redirects=False,
                        extensions=extensions,
                    ) as response:
                        if response.status_code == 429 or response.status_code >= 500:
                            retry_after = self._retry_after(response)
                            if attempt == self.max_attempts:
                                raise CommercialAdapterError(
                                    f"EXTERNAL_HTTP_{response.status_code}",
                                    "The connector returned a retryable HTTP response.",
                                    retryable=True,
                                    retry_after_seconds=self._backoff(
                                        attempt,
                                        retry_after,
                                    ),
                                )
                            self._sleeper(self._backoff(attempt, retry_after))
                            continue
                        if 300 <= response.status_code < 400:
                            raise CommercialAdapterError(
                                "EXTERNAL_REDIRECT_DENIED",
                                "The connector returned a redirect, which is not allowed.",
                                retryable=False,
                            )
                        if not 200 <= response.status_code < 300:
                            raise CommercialAdapterError(
                                f"EXTERNAL_HTTP_{response.status_code}",
                                "The connector rejected the external action request.",
                                retryable=False,
                            )

                        body = bytearray()
                        for chunk in response.iter_bytes():
                            if len(body) + len(chunk) > MAX_EXTERNAL_RESPONSE_BYTES:
                                raise CommercialAdapterError(
                                    "EXTERNAL_RESPONSE_TOO_LARGE",
                                    "The connector response exceeded the safety limit.",
                                    retryable=False,
                                )
                            body.extend(chunk)
                        response_status = response.status_code
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    if attempt == self.max_attempts:
                        raise CommercialAdapterError(
                            "EXTERNAL_TRANSPORT_RETRYABLE",
                            "The connector request failed after bounded retries.",
                            retryable=True,
                            retry_after_seconds=self.retry_max_seconds,
                        ) from exc
                    self._sleeper(self._backoff(attempt, None))
                    continue
                try:
                    payload = json.loads(body)
                except (TypeError, ValueError) as exc:
                    raise CommercialAdapterError(
                        "EXTERNAL_PROTOCOL_INVALID",
                        "The connector did not return valid JSON.",
                        retryable=False,
                    ) from exc
                if not isinstance(payload, dict):
                    raise CommercialAdapterError(
                        "EXTERNAL_PROTOCOL_INVALID",
                        "The connector did not return a JSON object.",
                        retryable=False,
                    )
                if len(payload) > 200 or not json_value_is_finite(payload):
                    raise CommercialAdapterError(
                        "EXTERNAL_PROTOCOL_INVALID",
                        "The connector returned invalid JSON values.",
                        retryable=False,
                    )
                return payload, response_status, attempt
        finally:
            if should_close:
                client.close()
        raise AssertionError("unreachable commercial adapter retry loop")

    def _pinned_public_destination(
        self,
        url: str,
        attempt: int,
    ) -> tuple[str, str, str]:
        """Resolve, validate, and pin one public address for this attempt.

        The request URL uses the selected numeric address so the transport
        cannot perform a second attacker-controlled DNS lookup. ``Host`` and
        httpcore's ``sni_hostname`` extension preserve HTTP virtual hosting and
        TLS certificate verification against the configured hostname.
        """

        try:
            parsed = httpx.URL(url)
            hostname = parsed.host
            port = parsed.port or 443
        except (TypeError, ValueError) as exc:
            raise CommercialAdapterError(
                "COMMERCIAL_ENDPOINT_INVALID",
                "The connector endpoint is invalid.",
                retryable=False,
            ) from exc
        if not hostname:
            raise CommercialAdapterError(
                "COMMERCIAL_ENDPOINT_INVALID",
                "The connector endpoint is invalid.",
                retryable=False,
            )
        try:
            addresses = self._dns_resolver(
                hostname,
                port,
                type=socket.SOCK_STREAM,
            )
        except (OSError, TypeError, ValueError) as exc:
            raise CommercialAdapterError(
                "COMMERCIAL_ENDPOINT_DNS_INVALID",
                "The connector endpoint could not be resolved to a public destination.",
                retryable=False,
            ) from exc
        if not addresses:
            raise CommercialAdapterError(
                "COMMERCIAL_ENDPOINT_DNS_INVALID",
                "The connector endpoint could not be resolved to a public destination.",
                retryable=False,
            )
        public_addresses: list[str] = []
        for result in addresses:
            try:
                raw_address = result[4][0]
                address = ipaddress.ip_address(raw_address)
            except (IndexError, TypeError, ValueError) as exc:
                raise CommercialAdapterError(
                    "COMMERCIAL_ENDPOINT_DNS_INVALID",
                    "The connector endpoint returned an invalid network destination.",
                    retryable=False,
                ) from exc
            if not is_public_unicast_address(address):
                raise CommercialAdapterError(
                    "COMMERCIAL_ENDPOINT_NOT_PUBLIC",
                    "The connector endpoint resolved outside the public network.",
                    retryable=False,
                )
            normalized = str(address)
            if normalized not in public_addresses:
                public_addresses.append(normalized)
        selected = public_addresses[(attempt - 1) % len(public_addresses)]
        try:
            pinned = parsed.copy_with(host=selected)
            host_header = parsed.netloc.decode("ascii")
            sni_hostname = parsed.raw_host.decode("ascii")
        except (UnicodeDecodeError, ValueError) as exc:
            raise CommercialAdapterError(
                "COMMERCIAL_ENDPOINT_INVALID",
                "The connector endpoint is invalid.",
                retryable=False,
            ) from exc
        return str(pinned), host_header, sni_hostname

    def _backoff(self, attempt: int, retry_after: float | None) -> float:
        if retry_after is not None:
            return min(max(retry_after, 0), self.retry_max_seconds)
        return min(self.retry_base_seconds * (2 ** (attempt - 1)), self.retry_max_seconds)

    def _retry_after(self, response: httpx.Response) -> float | None:
        value = response.headers.get("Retry-After")
        if not value:
            return None
        try:
            return float(value)
        except ValueError:
            return None

    @staticmethod
    def _connector_hash(runtime: CommercialConnectorRuntime) -> str:
        connector = runtime.connector
        payload = {
            "id": connector.id,
            "type": connector.type,
            "scope": sorted(connector.scope.split()),
            "endpoint": connector.endpoint,
            "enabled": connector.enabled,
            "health": connector.health,
        }
        return HttpsJsonMessageAdapter._canonical_hash(payload)

    @staticmethod
    def _canonical_hash(value: object) -> str:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()
