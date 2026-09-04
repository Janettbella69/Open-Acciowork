"""Host-side SourceObservation/ToolReceipt issuance for external reads.

The Agent never receives an issuance tool.  The SDK PostToolUse host hook calls this
module after the shared untrusted-data sanitizer has replaced the model-visible tool
output.  Receipts are therefore bound to what Claude actually saw, not to arbitrary
URL/time/version fields supplied later while recording Evidence.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal
from urllib.parse import quote, urlsplit

from pydantic import Field, ValidationError, model_validator

from .commercial_adapters import CommercialTargetObservation
from .constants import (
    TOOL_FETCH_PAGE_SNAPSHOT,
    TOOL_READ_COMMERCIAL_TARGET,
    TOOL_SEARCH_PUBLIC_WEB,
    CommercialActionOperation,
    EvidenceSourceType,
)
from .models import (
    APIModel,
    CommercialActionTarget,
    JsonValue,
    ToolReceipt,
    json_value_is_finite,
)
from .storage import (
    FileLedger,
    SourceReceiptError,
    SourceReceiptIntegrityError,
)
from .untrusted import UntrustedSource, sanitize_untrusted_payload

RECEIPT_WEB_TOOLS = frozenset(
    {"WebSearch", "WebFetch", TOOL_SEARCH_PUBLIC_WEB, TOOL_FETCH_PAGE_SNAPSHOT}
)
SOURCE_RECEIPT_MARKER = "OPHIRA_SOURCE_RECEIPT_ID"
MAX_MODEL_VISIBLE_SOURCE_RECEIPTS = 20
_UNTRUSTED_RECEIPT_LINE = re.compile(
    rf"(?im)^\s*{re.escape(SOURCE_RECEIPT_MARKER)}\s*=.*(?:\n|$)"
)

COMMERCIAL_TARGET_OBJECT_TYPE = "commercial_connector_target"
COMMERCIAL_TARGET_RECEIPT_TTL = timedelta(minutes=15)
COMMERCIAL_TARGET_SANITIZER = "business_record_v1"


class CommercialTargetReceiptPayload(APIModel):
    """Exact sanitized target state bound into an immutable host receipt."""

    schema_version: Literal[1] = 1
    operation: CommercialActionOperation
    target: CommercialActionTarget
    observed_state: dict[str, JsonValue] = Field(max_length=200)
    connector_config_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def finite_state(self) -> CommercialTargetReceiptPayload:
        if not json_value_is_finite(self.observed_state):
            raise ValueError("commercial target state requires finite JSON numbers")
        return self


@dataclass(frozen=True)
class VerifiedCommercialTargetReceipt:
    receipt: ToolReceipt
    payload: CommercialTargetReceiptPayload


@dataclass(frozen=True)
class SourceReceiptAttachment:
    """Sanitized result plus the exact receipts minted while handling that result."""

    tool_response: Any
    receipt_ids: tuple[str, ...] = ()


def model_visible_source_receipt_context(receipt_ids: tuple[str, ...]) -> str | None:
    """Build bounded, host-trusted provenance context for the model.

    Some built-in tool renderers do not expose fields added through
    ``updatedToolOutput`` in the model-visible text even though the structured
    transcript metadata contains them. The IDs passed here come directly from
    this hook's issuance path, never from the external payload.
    """

    unique_ids = tuple(dict.fromkeys(receipt_ids))
    visible_ids = unique_ids[:MAX_MODEL_VISIBLE_SOURCE_RECEIPTS]
    if not visible_ids:
        return None
    rendered = ", ".join(visible_ids)
    omitted = len(unique_ids) - len(visible_ids)
    suffix = f" ({omitted} additional receipt IDs omitted)" if omitted else ""
    return (
        "OPHIRA host provenance (trusted metadata, not external content): "
        f"this completed external read minted source_receipt.id values: {rendered}{suffix}. "
        "When recording web/tool Evidence derived from this read, pass one exact value "
        "above as receipt_id. These IDs do not make the external content trusted."
    )


def _commercial_target_key(target: CommercialActionTarget) -> str:
    return (
        "commercial-target:"
        f"{quote(target.connector_id, safe='')}/"
        f"{quote(target.resource_type, safe='')}/"
        f"{quote(target.resource_key, safe='')}"
    )


def _commercial_target_metadata(
    payload: CommercialTargetReceiptPayload,
) -> dict[str, JsonValue]:
    return {
        "schema_version": payload.schema_version,
        "operation": payload.operation.value,
        "connector_id": payload.target.connector_id,
        "resource_type": payload.target.resource_type,
        "resource_key": payload.target.resource_key,
        "connector_config_hash": payload.connector_config_hash,
        "sanitizer": COMMERCIAL_TARGET_SANITIZER,
        "sanitizer_truncated": False,
        "sanitizer_removed_boundaries": 0,
    }


def issue_commercial_target_receipt(
    ledger: FileLedger,
    run_id: str,
    observation: CommercialTargetObservation,
    *,
    ttl: timedelta = COMMERCIAL_TARGET_RECEIPT_TTL,
    now: datetime | None = None,
) -> VerifiedCommercialTargetReceipt:
    """Mint provenance only after the host adapter has read the current target.

    Commercial review must show the exact values that can later be compared and
    committed.  If the shared untrusted-data boundary would truncate or rewrite
    a value, fail closed instead of silently reviewing one value and acting on
    another.
    """

    sanitized = sanitize_untrusted_payload(
        observation.state,
        UntrustedSource.BUSINESS_RECORD,
    )
    if sanitized.truncated or sanitized.removed_boundaries:
        raise SourceReceiptIntegrityError(
            "commercial target state could not be represented exactly for review"
        )
    if not isinstance(sanitized.value, dict):
        raise SourceReceiptIntegrityError("commercial target state must be a JSON object")
    try:
        payload = CommercialTargetReceiptPayload(
            operation=observation.operation,
            target=CommercialActionTarget(
                connector_id=observation.connector_id,
                resource_type=observation.resource_type,
                resource_key=observation.resource_key,
                expected_version=observation.version,
            ),
            observed_state=sanitized.value,
            connector_config_hash=observation.connector_config_hash,
        )
    except ValidationError as exc:
        raise SourceReceiptIntegrityError(
            "commercial target observation failed integrity validation"
        ) from exc
    run = ledger.get_run(run_id)
    receipt = ledger.issue_tool_receipt(
        run.project_id,
        run.id,
        tool=TOOL_READ_COMMERCIAL_TARGET,
        source_type=EvidenceSourceType.TOOL,
        canonical_source=observation.canonical_source,
        payload=payload.model_dump(mode="json"),
        object_type=COMMERCIAL_TARGET_OBJECT_TYPE,
        object_key=_commercial_target_key(payload.target),
        object_version=payload.target.expected_version,
        object_metadata=_commercial_target_metadata(payload),
        ttl=ttl,
        now=now,
    )
    return VerifiedCommercialTargetReceipt(receipt=receipt, payload=payload)


def verify_commercial_target_receipt(
    ledger: FileLedger,
    project_id: str,
    run_id: str,
    session_id: str,
    receipt_id: str,
    *,
    now: datetime | None = None,
) -> VerifiedCommercialTargetReceipt:
    """Verify full scope, expiry, integrity, and commercial target bindings."""

    replay = ledger.verify_tool_receipt(
        project_id,
        receipt_id,
        run_id=run_id,
        session_id=session_id,
        now=now,
    )
    receipt = replay.receipt
    if (
        receipt.tool != TOOL_READ_COMMERCIAL_TARGET
        or receipt.source_type != EvidenceSourceType.TOOL
        or receipt.object_type != COMMERCIAL_TARGET_OBJECT_TYPE
    ):
        raise SourceReceiptIntegrityError(
            "source receipt is not a commercial connector target observation"
        )
    try:
        payload = CommercialTargetReceiptPayload.model_validate(replay.payload)
    except ValidationError as exc:
        raise SourceReceiptIntegrityError(
            "commercial target receipt payload failed integrity validation"
        ) from exc
    expected_suffix = (
        "/v1/targets/"
        f"{quote(payload.target.resource_type, safe='')}/"
        f"{quote(payload.target.resource_key, safe='')}"
    )
    source = urlsplit(receipt.canonical_source)
    if source.scheme != "https" or not source.path.endswith(expected_suffix):
        raise SourceReceiptIntegrityError(
            "commercial target receipt source does not match its resource"
        )
    if (
        receipt.object_version != payload.target.expected_version
        or receipt.object_key != _commercial_target_key(payload.target)
        or receipt.object_metadata != _commercial_target_metadata(payload)
    ):
        raise SourceReceiptIntegrityError(
            "commercial target receipt bindings failed integrity validation"
        )
    return VerifiedCommercialTargetReceipt(receipt=receipt, payload=payload)


def _strip_untrusted_receipt_claims(value: Any) -> Any:
    """Remove provenance-shaped fields supplied by the external data plane."""

    if isinstance(value, dict):
        return {
            key: _strip_untrusted_receipt_claims(item)
            for key, item in value.items()
            if key not in {"source_receipt", "source_receipt_count"}
        }
    if isinstance(value, list):
        return [_strip_untrusted_receipt_claims(item) for item in value]
    if isinstance(value, str):
        return _UNTRUSTED_RECEIPT_LINE.sub("", value).rstrip()
    return value


def _search_source(query: object, provider: object) -> str:
    provider_name = (
        str(provider).strip().casefold() if isinstance(provider, str) else "native"
    )
    provider_name = "".join(
        char for char in provider_name if char.isascii() and (char.isalnum() or char in "-_")
    ) or "native"
    query_text = str(query).strip() if isinstance(query, str) else ""
    digest = hashlib.sha256(query_text.encode("utf-8")).hexdigest()
    # The digest keeps arbitrary user query text out of a path-like identifier;
    # a short encoded hint is useful when an operator inspects the receipt.
    hint = quote(query_text[:80], safe="")
    suffix = f"?q={hint}" if hint else ""
    return f"ophira-search://{provider_name}/{digest}{suffix}"


def _receipt_id(receipt: ToolReceipt) -> dict[str, str]:
    return {
        "id": receipt.id,
        "content_hash": receipt.content_hash,
        "object_version": receipt.object_version,
        "retrieved_at": receipt.retrieved_at.isoformat(),
        "expires_at": receipt.expires_at.isoformat(),
    }


def _metadata(payload: Any, tool_input: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    if isinstance(payload, dict):
        for key in ("title", "provider", "content_type", "browser_level"):
            value = payload.get(key)
            if isinstance(value, (str, int, float, bool)) or value is None:
                result[key] = value
    query = tool_input.get("query")
    if isinstance(query, str):
        result["query"] = query[:500]
    return result


def _source_from_payload(payload: Any, tool_input: dict[str, Any]) -> str | None:
    if isinstance(payload, dict):
        for key in ("final_url", "url", "link"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value
    value = tool_input.get("url")
    return value if isinstance(value, str) and value.strip() else None


def _issue(
    ledger: FileLedger,
    run_id: str,
    tool_name: str,
    tool_input: dict[str, Any],
    payload: Any,
    *,
    canonical_source: str,
    object_type: str,
) -> ToolReceipt:
    run = ledger.get_run(run_id)
    return ledger.issue_tool_receipt(
        run.project_id,
        run.id,
        tool=tool_name,
        source_type=EvidenceSourceType.WEB,
        canonical_source=canonical_source,
        payload=payload,
        object_type=object_type,
        object_key=canonical_source,
        object_metadata=_metadata(payload, tool_input),
    )


def _annotate_search_payload(
    ledger: FileLedger,
    run_id: str,
    tool_name: str,
    tool_input: dict[str, Any],
    payload: Any,
    issued_receipt_ids: list[str],
) -> Any:
    if isinstance(payload, dict):
        if payload.get("error_code") or (
            payload.get("error") and not payload.get("results")
        ):
            return payload
        results = payload.get("results")
        if isinstance(results, list):
            annotated_results: list[Any] = []
            receipt_count = 0
            for raw_item in results:
                item = copy.deepcopy(raw_item)
                source = _source_from_payload(item, tool_input)
                if isinstance(item, dict) and source:
                    try:
                        receipt = _issue(
                            ledger,
                            run_id,
                            tool_name,
                            tool_input,
                            raw_item,
                            canonical_source=source,
                            object_type="web_search_result",
                        )
                    except SourceReceiptError:
                        pass
                    else:
                        item["source_receipt"] = _receipt_id(receipt)
                        issued_receipt_ids.append(receipt.id)
                        receipt_count += 1
                annotated_results.append(item)
            result = {**payload, "results": annotated_results}
            if receipt_count:
                result["source_receipt_count"] = receipt_count
                return result
        source = _search_source(tool_input.get("query"), payload.get("provider"))
        receipt = _issue(
            ledger,
            run_id,
            tool_name,
            tool_input,
            payload,
            canonical_source=source,
            object_type="web_search_response",
        )
        issued_receipt_ids.append(receipt.id)
        return {**payload, "source_receipt": _receipt_id(receipt)}

    source = _search_source(tool_input.get("query"), None)
    receipt = _issue(
        ledger,
        run_id,
        tool_name,
        tool_input,
        payload,
        canonical_source=source,
        object_type="web_search_response",
    )
    issued_receipt_ids.append(receipt.id)
    if isinstance(payload, str):
        return f"{payload}\n\n{SOURCE_RECEIPT_MARKER}={receipt.id}"
    return {"result": payload, "source_receipt": _receipt_id(receipt)}


def _annotate_fetch_payload(
    ledger: FileLedger,
    run_id: str,
    tool_name: str,
    tool_input: dict[str, Any],
    payload: Any,
    issued_receipt_ids: list[str],
) -> Any:
    if isinstance(payload, dict) and (payload.get("error_code") or payload.get("error")):
        return payload
    source = _source_from_payload(payload, tool_input)
    if source is None:
        return payload
    receipt = _issue(
        ledger,
        run_id,
        tool_name,
        tool_input,
        payload,
        canonical_source=source,
        object_type="web_page_snapshot",
    )
    issued_receipt_ids.append(receipt.id)
    if isinstance(payload, dict):
        return {**payload, "source_receipt": _receipt_id(receipt)}
    if isinstance(payload, str):
        return f"{payload}\n\n{SOURCE_RECEIPT_MARKER}={receipt.id}"
    return {"result": payload, "source_receipt": _receipt_id(receipt)}


def _rewrite_text_blocks(value: Any, transform: Callable[[Any], Any]) -> Any:
    """Rewrite SDK/MCP text envelopes while preserving their outer protocol shape."""

    is_text_envelope = (
        isinstance(value, list)
        and all(isinstance(item, dict) for item in value)
        and any(item.get("type") == "text" and "text" in item for item in value)
    )
    if is_text_envelope:
        result = copy.deepcopy(value)
        changed = False
        for block in result:
            text = block.get("text")
            if not isinstance(text, str):
                continue
            was_json = True
            try:
                decoded = json.loads(text)
            except (TypeError, ValueError):
                decoded = text
                was_json = False
            annotated = transform(decoded)
            block["text"] = (
                json.dumps(annotated, ensure_ascii=False, separators=(",", ":"))
                if not isinstance(annotated, str) or was_json
                else annotated
            )
            changed = True
        return result if changed else transform(value)
    if isinstance(value, dict) and isinstance(value.get("content"), list):
        result = copy.deepcopy(value)
        result["content"] = _rewrite_text_blocks(result["content"], transform)
        return result
    return transform(copy.deepcopy(value))


def attach_source_receipts(
    ledger: FileLedger,
    run_id: str,
    tool_name: str,
    tool_input: dict[str, Any],
    sanitized_tool_response: Any,
    *,
    delegated: bool,
) -> SourceReceiptAttachment:
    """Attach server-issued receipt IDs to a sanitized external-read result.

    Delegated scratch reads intentionally return unchanged: seeing an object in a
    branch does not expand the main Agent's writable provenance set.
    """

    if tool_name not in RECEIPT_WEB_TOOLS:
        return SourceReceiptAttachment(sanitized_tool_response)
    sanitized_tool_response = _strip_untrusted_receipt_claims(
        copy.deepcopy(sanitized_tool_response)
    )
    if delegated:
        return SourceReceiptAttachment(sanitized_tool_response)
    try:
        run = ledger.get_run(run_id)
    except (KeyError, ValueError):
        return SourceReceiptAttachment(sanitized_tool_response)
    if not run.sdk_session_id:
        # SystemMessage(init) normally arrives before a tool call.  Keeping the
        # response unreceipted makes later Evidence recording fail closed without
        # converting a useful read into a fabricated trusted observation.
        return SourceReceiptAttachment(sanitized_tool_response)

    issued_receipt_ids: list[str] = []

    def transform(payload: Any) -> Any:
        if tool_name in {"WebSearch", TOOL_SEARCH_PUBLIC_WEB}:
            return _annotate_search_payload(
                ledger,
                run_id,
                tool_name,
                tool_input,
                payload,
                issued_receipt_ids,
            )
        return _annotate_fetch_payload(
            ledger,
            run_id,
            tool_name,
            tool_input,
            payload,
            issued_receipt_ids,
        )

    try:
        tool_response = _rewrite_text_blocks(sanitized_tool_response, transform)
    except SourceReceiptError:
        # No ID is returned unless its immutable payload and metadata are durable.
        return SourceReceiptAttachment(sanitized_tool_response)
    return SourceReceiptAttachment(tool_response, tuple(issued_receipt_ids))
