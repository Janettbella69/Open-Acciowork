from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable
from datetime import datetime
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from .commercial_actions import CommercialActionError
from .constants import (
    PRESENTATION_FRAME_PROTOCOL_VERSION,
    PRESENTATION_MCP_SERVER_NAME,
    CommercialActionOperation,
    CommercialActionStatus,
    EvidenceStatus,
    ExternalExecutionStatus,
    PresentationFrameType,
)
from .models import (
    Artifact,
    CommercialActionRecord,
    Decision,
    Evidence,
    OpportunityProject,
    new_id,
    utc_now,
)
from .storage import FileLedger, LedgerNotFoundError, SourceReceiptError
from .untrusted import (
    UNTRUSTED_DATA_NOTICE,
    UntrustedSource,
    sanitize_untrusted_payload,
)

PRESENTATION_SCHEMA_VERSION = "1"
MAX_PRESENTATION_ITEMS = 12
_PRESENTATION_ID = re.compile(r"^view_[A-Za-z0-9_-]+$")
PRESENTATION_COMPONENTS = (
    "evidence_comparison",
    "supplier_comparison",
    "unit_economics",
    "decision_brief",
    "commercial_action_diff",
    "artifact_list",
)
_PRESENTATION_TOOL_COMPONENTS = {
    f"mcp__{PRESENTATION_MCP_SERVER_NAME}__present_evidence_comparison": (
        "evidence_comparison"
    ),
    f"mcp__{PRESENTATION_MCP_SERVER_NAME}__present_supplier_comparison": (
        "supplier_comparison"
    ),
    f"mcp__{PRESENTATION_MCP_SERVER_NAME}__present_unit_economics": "unit_economics",
    f"mcp__{PRESENTATION_MCP_SERVER_NAME}__present_decision_brief": "decision_brief",
    f"mcp__{PRESENTATION_MCP_SERVER_NAME}__present_commercial_action_diff": (
        "commercial_action_diff"
    ),
    f"mcp__{PRESENTATION_MCP_SERVER_NAME}__present_artifacts": "artifact_list",
}

if TYPE_CHECKING:
    from .commercial_actions import CommercialActionService


_UNIT_ECONOMICS_FIELDS = (
    "selling_price",
    "landed_unit_cost",
    "channel_fee_rate",
    "fulfillment_per_unit",
    "acquisition_per_unit",
    "refund_rate",
    "fixed_costs",
)
_RATE_FIELDS = frozenset({"channel_fee_rate", "refund_rate"})
_DECIMAL_SCALAR = re.compile(r"^(?:0|[1-9][0-9]*)(?:\.[0-9]{1,12})?$")
_NORMALIZED_DECIMAL = re.compile(r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]{1,12})?$")
_CURRENCY_CODE = re.compile(r"^[A-Za-z]{3,12}$")
_ACTIONABILITY = frozenset(
    {
        "analysis_only",
        "review_required",
        "apply_available",
        "restage_required",
        "awaiting_adapter",
        "retry_required",
        "none",
    }
)
_COMMERCIAL_OPERATIONS = frozenset(item.value for item in CommercialActionOperation)
_COMMERCIAL_STATUSES = frozenset(item.value for item in CommercialActionStatus)


class _PresentationSourceError(ValueError):
    def __init__(self, code: str, message: str, receipt_id: str) -> None:
        super().__init__(message)
        self.code = code
        self.receipt_id = receipt_id


def _text(value: Any, maximum: int, *, empty: bool = False) -> bool:
    return isinstance(value, str) and (empty or bool(value)) and len(value) <= maximum


def _identifier(value: Any) -> bool:
    return _text(value, 200) and bool(re.fullmatch(r"[A-Za-z0-9_-]+", value))


def _string_list(value: Any, maximum: int = 100) -> bool:
    return (
        isinstance(value, list)
        and len(value) <= maximum
        and all(_text(item, 8_000) for item in value)
    )


def _finite_number(value: Any, minimum: float, maximum: float) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and minimum <= value <= maximum
    )


def _valid_snapshot(
    value: Any,
    *,
    kind: str,
    actionability: frozenset[str],
) -> bool:
    return (
        isinstance(value, dict)
        and value.get("kind") == kind
        # This schema never claims that a project/action snapshot proves the
        # current state of a supplier, marketplace, or other external system.
        and value.get("current") == "unverified"
        and (value.get("expired") is None or isinstance(value.get("expired"), bool))
        and _text(value.get("actionable"), 80)
        and value.get("actionable") in actionability
        and _timestamp(value.get("captured_at"))
    )


def _timestamp(value: Any) -> bool:
    if not _text(value, 100):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _valid_normalized_decimal(value: Any) -> bool:
    return _text(value, 80) and bool(_NORMALIZED_DECIMAL.fullmatch(value))


def _valid_json_value(value: Any, *, depth: int = 0) -> bool:
    if depth > 10:
        return False
    if value is None or isinstance(value, bool):
        return True
    if isinstance(value, str):
        return len(value) <= 12_000
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return math.isfinite(value)
    if isinstance(value, list):
        return len(value) <= 200 and all(_valid_json_value(item, depth=depth + 1) for item in value)
    if isinstance(value, dict):
        return len(value) <= 200 and all(
            _text(key, 1_000) and _valid_json_value(item, depth=depth + 1)
            for key, item in value.items()
        )
    return False


def _valid_source(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and _text(value.get("title"), 4_000)
        and (value.get("url") is None or _text(value.get("url"), 2_000))
        and _text(value.get("type"), 80)
        and (value.get("observed_at") is None or _text(value.get("observed_at"), 100))
        and _text(value.get("retrieved_at"), 100)
    )


def _valid_evidence(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and _identifier(value.get("id"))
        and (value.get("dimension") is None or _text(value.get("dimension"), 80))
        and _string_list(value.get("tags"), 30)
        and _text(value.get("kind"), 80)
        and _text(value.get("claim"), 12_000)
        and _valid_source(value.get("source"))
        and _text(value.get("status"), 80)
        and _finite_number(value.get("confidence"), 0, 1)
        and _text(value.get("recorded_at"), 100)
    )


def _valid_evidence_list(value: Any, *, allow_empty: bool = False) -> bool:
    return (
        isinstance(value, list)
        and (allow_empty or bool(value))
        and len(value) <= MAX_PRESENTATION_ITEMS
        and all(_valid_evidence(item) for item in value)
    )


def _valid_commercial_review(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and _text(value.get("decision"), 80)
        and value.get("decision") in {"approve", "reject"}
        and _text(value.get("reason"), 4_000)
        and _text(value.get("decided_at"), 100)
        and (value.get("valid_until") is None or _text(value.get("valid_until"), 100))
    )


def _valid_commercial_execution(value: Any) -> bool:
    receipt = value.get("receipt") if isinstance(value, dict) else None
    if receipt is not None and not (
        isinstance(receipt, dict)
        and _identifier(receipt.get("id"))
        and _text(receipt.get("remote_action_id"), 500)
        and (receipt.get("remote_version") is None or _text(receipt.get("remote_version"), 500))
        and isinstance(receipt.get("http_status"), int)
        and not isinstance(receipt.get("http_status"), bool)
        and 200 <= receipt["http_status"] <= 299
        and _text(receipt.get("response_hash"), 64)
        and bool(re.fullmatch(r"[0-9a-f]{64}", receipt["response_hash"]))
        and isinstance(receipt.get("adapter_attempts"), int)
        and not isinstance(receipt.get("adapter_attempts"), bool)
        and receipt["adapter_attempts"] >= 1
        and _timestamp(receipt.get("issued_at"))
    ):
        return False
    valid_shape = (
        isinstance(value, dict)
        and _identifier(value.get("id"))
        and value.get("external_status") in {item.value for item in ExternalExecutionStatus}
        and value.get("adapter_required") is True
        and (value.get("adapter_type") is None or _text(value.get("adapter_type"), 80))
        and _timestamp(value.get("requested_at"))
        and (value.get("idempotency_key") is None or _text(value.get("idempotency_key"), 200))
        and isinstance(value.get("attempt_count"), int)
        and not isinstance(value.get("attempt_count"), bool)
        and value["attempt_count"] >= 0
        and (value.get("retry_not_before") is None or _timestamp(value.get("retry_not_before")))
        and (value.get("last_error_code") is None or _text(value.get("last_error_code"), 120))
        and (
            value.get("last_error_message") is None or _text(value.get("last_error_message"), 1_000)
        )
        and (receipt is None or value.get("external_status") == "succeeded")
    )
    if not valid_shape:
        return False
    external_status = value["external_status"]
    error_complete = bool(value.get("last_error_code")) and bool(value.get("last_error_message"))
    error_any = (
        value.get("last_error_code") is not None or value.get("last_error_message") is not None
    )
    if external_status == "succeeded":
        return receipt is not None and not error_any
    if receipt is not None:
        return False
    if external_status == "terminal_failed":
        return error_complete and value["attempt_count"] >= 1
    if external_status == "retryable_failed":
        return (
            error_complete
            and value["attempt_count"] >= 1
            and value.get("retry_not_before") is not None
        )
    if external_status == "attempting":
        return value["attempt_count"] >= 1
    return external_status == "not_executed" and value["attempt_count"] == 0


def _valid_component_payload(
    component: str,
    payload: Any,
    project_id: str,
) -> bool:
    if not isinstance(payload, dict):
        return False
    if component == "evidence_comparison":
        return _text(payload.get("title"), 160) and _valid_evidence_list(payload.get("items"))
    if component == "supplier_comparison":
        return (
            _text(payload.get("title"), 160)
            and _valid_snapshot(
                payload.get("snapshot"),
                kind="project_ledger",
                actionability=frozenset({"analysis_only"}),
            )
            and _valid_evidence_list(payload.get("items"))
        )
    if component == "unit_economics":
        inputs = payload.get("inputs")
        currency = payload.get("currency")
        results = payload.get("results")
        if not (
            _text(payload.get("title"), 160)
            and _valid_snapshot(
                payload.get("snapshot"),
                kind="project_ledger",
                actionability=frozenset({"analysis_only"}),
            )
            and isinstance(inputs, list)
            and len(inputs) == len(_UNIT_ECONOMICS_FIELDS)
            and isinstance(currency, dict)
            and _text(currency.get("code"), 12)
            and bool(_CURRENCY_CODE.fullmatch(currency["code"]))
            and _valid_evidence(currency.get("evidence"))
            and isinstance(results, dict)
        ):
            return False
        metrics: list[Any] = []
        for item in inputs:
            if not isinstance(item, dict):
                return False
            metrics.append(item.get("metric"))
            if not (
                _text(item.get("metric"), 80)
                and item.get("metric") in _UNIT_ECONOMICS_FIELDS
                and item.get("format")
                == ("rate" if item.get("metric") in _RATE_FIELDS else "money")
                and _valid_normalized_decimal(item.get("value"))
                and _valid_evidence(item.get("evidence"))
            ):
                return False
        return (
            tuple(metrics) == _UNIT_ECONOMICS_FIELDS
            and _valid_normalized_decimal(results.get("net_revenue_per_unit"))
            and _valid_normalized_decimal(results.get("variable_cost_per_unit"))
            and _valid_normalized_decimal(results.get("contribution_per_unit"))
            and (
                results.get("contribution_margin_rate") is None
                or _valid_normalized_decimal(results.get("contribution_margin_rate"))
            )
            and (
                results.get("break_even_units") is None
                or (
                    isinstance(results.get("break_even_units"), int)
                    and not isinstance(results.get("break_even_units"), bool)
                    and results["break_even_units"] >= 0
                )
            )
        )
    if component == "decision_brief":
        decision = payload.get("decision")
        return (
            isinstance(decision, dict)
            and _identifier(decision.get("id"))
            and _text(decision.get("question"), 4_000)
            and _text(decision.get("outcome"), 8_000)
            and _text(decision.get("decision_type"), 80)
            and _text(decision.get("rationale"), 12_000, empty=True)
            and _text(decision.get("status"), 80)
            and _string_list(decision.get("assumptions"))
            and _string_list(decision.get("conditions"))
            and _string_list(decision.get("next_actions"))
            and (decision.get("review_at") is None or _text(decision.get("review_at"), 100))
            and _text(decision.get("created_at"), 100)
            and _valid_evidence_list(decision.get("evidence"), allow_empty=True)
        )
    if component == "commercial_action_diff":
        action = payload.get("action")
        if not (
            _valid_snapshot(
                payload.get("snapshot"),
                kind="commercial_action_ledger",
                actionability=_ACTIONABILITY - {"analysis_only"},
            )
            and isinstance(action, dict)
            and _identifier(action.get("id"))
            and _text(action.get("operation"), 80)
            and action.get("operation") in _COMMERCIAL_OPERATIONS
            and _text(action.get("stored_status"), 80)
            and action.get("stored_status") in _COMMERCIAL_STATUSES
            and _text(action.get("effective_status"), 80)
            and action.get("effective_status") in _COMMERCIAL_STATUSES
            and _text(action.get("reason"), 4_000)
            and isinstance(action.get("project_revision_at_stage"), int)
            and not isinstance(action.get("project_revision_at_stage"), bool)
            and action["project_revision_at_stage"] >= 1
            and isinstance(action.get("project_revision_changed"), bool)
            and isinstance(action.get("external_action_performed"), bool)
            and action["external_action_performed"]
            == (action.get("stored_status") == CommercialActionStatus.APPLIED.value)
            and _text(action.get("created_at"), 100)
            and _text(action.get("expires_at"), 100)
        ):
            return False
        target = action.get("target")
        review = action.get("review")
        execution = action.get("execution")
        changes = action.get("changes")
        if not (
            isinstance(target, dict)
            and _identifier(target.get("connector_id"))
            and _identifier(target.get("resource_type"))
            and _text(target.get("resource_key"), 500)
            and _text(target.get("expected_version"), 500)
            and (review is None or _valid_commercial_review(review))
            and (execution is None or _valid_commercial_execution(execution))
            and isinstance(changes, list)
            and 1 <= len(changes) <= MAX_PRESENTATION_ITEMS
        ):
            return False
        stored_status = action["stored_status"]
        execution_status = execution.get("external_status") if execution else None
        if stored_status == CommercialActionStatus.APPLIED.value:
            if execution_status != ExternalExecutionStatus.SUCCEEDED.value:
                return False
        elif stored_status == CommercialActionStatus.EXECUTION_FAILED.value:
            if execution_status != ExternalExecutionStatus.TERMINAL_FAILED.value:
                return False
        elif stored_status == CommercialActionStatus.EXECUTION_REQUESTED.value:
            if execution_status not in {
                ExternalExecutionStatus.NOT_EXECUTED.value,
                ExternalExecutionStatus.ATTEMPTING.value,
                ExternalExecutionStatus.RETRYABLE_FAILED.value,
            }:
                return False
        elif execution is not None:
            return False
        return all(
            isinstance(change, dict)
            and _text(change.get("field"), 519)
            and _valid_json_value(change.get("before"))
            and _valid_json_value(change.get("after"))
            for change in changes
        )
    if component != "artifact_list":
        return False
    items = payload.get("items")
    if not _text(payload.get("title"), 160) or not isinstance(items, list):
        return False
    if not 1 <= len(items) <= MAX_PRESENTATION_ITEMS:
        return False
    for item in items:
        if not isinstance(item, dict) or not _identifier(item.get("id")):
            return False
        expected_download = f"/api/projects/{project_id}/artifacts/{item['id']}/download"
        if not (
            _text(item.get("name"), 1_000)
            and _text(item.get("mime"), 200)
            and isinstance(item.get("version"), int)
            and not isinstance(item.get("version"), bool)
            and item["version"] >= 1
            and isinstance(item.get("size_bytes"), int)
            and not isinstance(item.get("size_bytes"), bool)
            and item["size_bytes"] >= 0
            and item.get("download_url") == expected_download
            and _string_list(item.get("source_ids"))
            and (item.get("run_id") is None or _identifier(item.get("run_id")))
            and _text(item.get("updated_at"), 100)
        ):
            return False
    return True


def _tool_result(value: Any, *, is_error: bool = False) -> dict[str, Any]:
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(value, ensure_ascii=False, separators=(",", ":")),
            }
        ],
        "is_error": is_error,
    }


def _title(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("title must be a non-empty string")
    title = value.strip()
    if len(title) > 160:
        raise ValueError("title must contain at most 160 characters")
    return title


def _ids(value: Any, *, field: str) -> list[str]:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_PRESENTATION_ITEMS:
        raise ValueError(f"{field} must contain 1 to {MAX_PRESENTATION_ITEMS} ids")
    if any(not isinstance(item, str) or not item for item in value):
        raise ValueError(f"{field} must contain non-empty string ids")
    if len(set(value)) != len(value):
        raise ValueError(f"{field} must not contain duplicate ids")
    return value


def _error(
    code: str,
    message: str,
    *,
    unknown_ids: list[str] | None = None,
) -> dict[str, Any]:
    return _tool_result(
        {
            "error": {
                "code": code,
                "message": message,
                "unknown_ids": unknown_ids or [],
            }
        },
        is_error=True,
    )


def _source_error(error: _PresentationSourceError) -> dict[str, Any]:
    return _error(
        error.code,
        str(error),
        unknown_ids=[error.receipt_id],
    )


def _evidence_item(
    item: Evidence,
    *,
    ledger: FileLedger | None = None,
    project_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    source_title = item.source_title
    source_url = str(item.source_url) if item.source_url else None
    source_type = item.source_type.value
    observed_at = item.observed_at.isoformat() if item.observed_at else None
    retrieved_at = item.retrieved_at
    if item.receipt_id:
        if ledger is None or project_id is None:
            raise _PresentationSourceError(
                "SOURCE_RECEIPT_UNAVAILABLE",
                "Receipt-backed evidence requires server ledger hydration.",
                item.receipt_id,
            )
        try:
            replay = ledger.replay_tool_receipt(
                project_id,
                item.receipt_id,
                now=now,
            )
        except SourceReceiptError as exc:
            raise _PresentationSourceError(
                "SOURCE_RECEIPT_INVALID",
                "A source receipt failed replay or integrity validation.",
                item.receipt_id,
            ) from exc
        if replay.expired:
            raise _PresentationSourceError(
                "SOURCE_RECEIPT_EXPIRED",
                "A source receipt expired; retrieve and record the source again.",
                item.receipt_id,
            )
        receipt = replay.receipt
        if receipt.source_type != item.source_type:
            raise _PresentationSourceError(
                "SOURCE_RECEIPT_INVALID",
                "Evidence source type no longer matches its signed receipt.",
                item.receipt_id,
            )
        metadata_title = receipt.object_metadata.get("title")
        source_title = (
            metadata_title.strip()
            if isinstance(metadata_title, str) and metadata_title.strip()
            else receipt.object_type.replace("_", " ")
        )
        # Keep a canonical non-web source URI in the typed payload for audit, but
        # the frontend only makes http(s) values clickable.
        source_url = receipt.canonical_source
        source_type = receipt.source_type.value
        metadata_observed_at = receipt.object_metadata.get("observed_at")
        observed_at = metadata_observed_at if isinstance(metadata_observed_at, str) else None
        retrieved_at = receipt.retrieved_at
    return {
        "id": item.id,
        "dimension": item.dimension.value if item.dimension else None,
        "tags": list(item.tags),
        "kind": item.kind.value,
        "claim": item.claim,
        "source": {
            "title": source_title,
            "url": source_url,
            "type": source_type,
            "observed_at": observed_at,
            "retrieved_at": retrieved_at.isoformat(),
        },
        "status": item.status.value,
        "confidence": item.confidence,
        "recorded_at": item.recorded_at.isoformat(),
    }


def _decision_item(item: Decision, evidence: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": item.id,
        "question": item.question,
        "outcome": item.outcome,
        "decision_type": item.decision_type,
        "rationale": item.rationale,
        "status": item.status.value,
        "assumptions": list(item.assumptions),
        "conditions": list(item.conditions),
        "next_actions": list(item.next_actions),
        "review_at": item.review_at.isoformat() if item.review_at else None,
        "created_at": item.created_at.isoformat(),
        "evidence": evidence,
    }


def _artifact_item(item: Artifact) -> dict[str, Any]:
    # Reconstruct the route from server-owned identifiers instead of trusting a
    # model-provided URL. Paths and checksums intentionally stay out of the UI payload.
    download_url = f"/api/projects/{item.project_id}/artifacts/{item.id}/download"
    return {
        "id": item.id,
        "name": item.name,
        "mime": item.mime,
        "version": item.version,
        "size_bytes": item.size_bytes,
        "download_url": download_url,
        "source_ids": list(item.source_ids),
        "run_id": item.run_id,
        "updated_at": item.updated_at.isoformat(),
    }


def _snapshot(
    kind: str,
    *,
    now: datetime,
    expired: bool | None,
    actionable: str,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "current": "unverified",
        "expired": expired,
        "actionable": actionable,
        "captured_at": now.isoformat(),
    }


def _evidence_snapshot(items: list[Evidence], now: datetime) -> dict[str, Any]:
    # ACTIVE means usable in the project ledger. It does not prove that an
    # external quote or price is still current, so unknown stays null rather
    # than being converted to a misleading ``expired: false``.
    expired = (
        True
        if any(item.status in {EvidenceStatus.STALE, EvidenceStatus.REMOVED} for item in items)
        else None
    )
    return _snapshot(
        "project_ledger",
        now=now,
        expired=expired,
        actionable="analysis_only",
    )


def _scalar_evidence(item: Evidence) -> str:
    value = item.excerpt if item.excerpt and item.excerpt.strip() else item.claim
    return value.strip()


def _decimal_from_evidence(item: Evidence, field: str) -> Decimal:
    raw = _scalar_evidence(item)
    if not _DECIMAL_SCALAR.fullmatch(raw):
        raise ValueError(
            f"{field} evidence excerpt or claim must be one non-negative decimal scalar"
        )
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError(f"{field} evidence is not a decimal scalar") from exc
    if not value.is_finite() or value > Decimal("1000000000000000"):
        raise ValueError(f"{field} evidence is outside the supported range")
    if field in _RATE_FIELDS and value > 1:
        raise ValueError(f"{field} evidence must be a decimal rate between 0 and 1")
    return value


def _decimal_text(value: Decimal, *, money: bool = False) -> str:
    normalized = value.quantize(Decimal("0.0001")) if money else value
    if normalized == 0:
        return "0"
    rendered = format(normalized, "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def _unit_economics_payload(
    title: str,
    evidence_by_field: dict[str, Evidence],
    currency_evidence: Evidence,
    *,
    ledger: FileLedger,
    project_id: str,
    now: datetime,
) -> dict[str, Any]:
    values = {
        field: _decimal_from_evidence(evidence_by_field[field], field)
        for field in _UNIT_ECONOMICS_FIELDS
    }
    currency = _scalar_evidence(currency_evidence)
    if not _CURRENCY_CODE.fullmatch(currency):
        raise ValueError(
            "currency evidence excerpt or claim must be a 3 to 12 letter currency code"
        )
    currency = currency.upper()
    price = values["selling_price"]
    net_revenue = price * (Decimal("1") - values["refund_rate"])
    variable = (
        values["landed_unit_cost"]
        + price * values["channel_fee_rate"]
        + values["fulfillment_per_unit"]
        + values["acquisition_per_unit"]
    )
    contribution = net_revenue - variable
    fixed = values["fixed_costs"]
    break_even = (
        None
        if contribution <= 0
        else int((fixed / contribution).to_integral_value(rounding=ROUND_CEILING))
    )
    all_evidence = [
        *(evidence_by_field[field] for field in _UNIT_ECONOMICS_FIELDS),
        currency_evidence,
    ]
    return {
        "title": title,
        "snapshot": _evidence_snapshot(all_evidence, now),
        "currency": {
            "code": currency,
            "evidence": _evidence_item(
                currency_evidence,
                ledger=ledger,
                project_id=project_id,
                now=now,
            ),
        },
        "inputs": [
            {
                "metric": field,
                "format": "rate" if field in _RATE_FIELDS else "money",
                "value": _decimal_text(values[field]),
                "evidence": _evidence_item(
                    evidence_by_field[field],
                    ledger=ledger,
                    project_id=project_id,
                    now=now,
                ),
            }
            for field in _UNIT_ECONOMICS_FIELDS
        ],
        "results": {
            "net_revenue_per_unit": _decimal_text(net_revenue, money=True),
            "variable_cost_per_unit": _decimal_text(variable, money=True),
            "contribution_per_unit": _decimal_text(contribution, money=True),
            "contribution_margin_rate": (
                _decimal_text(contribution / price, money=True) if price else None
            ),
            "break_even_units": break_even,
        },
    }


def _commercial_actionability(
    action: CommercialActionRecord,
    *,
    expired: bool,
    project_revision_changed: bool,
) -> str:
    if action.status == CommercialActionStatus.EXECUTION_REQUESTED:
        if (
            action.execution_request is not None
            and action.execution_request.external_status == ExternalExecutionStatus.RETRYABLE_FAILED
        ):
            return "retry_required"
        return "awaiting_adapter"
    if (
        action.status
        in {
            CommercialActionStatus.REJECTED,
            CommercialActionStatus.EXPIRED,
        }
        or expired
    ):
        return "none"
    if project_revision_changed:
        return "restage_required"
    if action.status == CommercialActionStatus.STAGED:
        return "review_required"
    if action.status == CommercialActionStatus.APPROVED:
        return "apply_available"
    return "none"


def _commercial_action_payload(
    action: CommercialActionRecord,
    project: OpportunityProject,
    *,
    now: datetime,
) -> tuple[dict[str, Any], bool]:
    approval_expired = bool(
        action.status == CommercialActionStatus.APPROVED
        and action.review
        and action.review.valid_until
        and now >= action.review.valid_until
    )
    expired = (
        action.status == CommercialActionStatus.EXPIRED
        or now >= action.expires_at
        or approval_expired
    )
    project_revision_changed = project.revision != action.project_revision_at_stage
    effective_status = (
        CommercialActionStatus.EXPIRED.value
        if expired
        and action.status
        in {
            CommercialActionStatus.STAGED,
            CommercialActionStatus.APPROVED,
        }
        else action.status.value
    )
    actionability = _commercial_actionability(
        action,
        expired=expired,
        project_revision_changed=project_revision_changed,
    )
    changes_truncated = len(action.changes) > MAX_PRESENTATION_ITEMS
    return (
        {
            "snapshot": _snapshot(
                "commercial_action_ledger",
                now=now,
                expired=expired,
                actionable=actionability,
            ),
            "action": {
                "id": action.id,
                "operation": action.operation.value,
                "stored_status": action.status.value,
                "effective_status": effective_status,
                "target": action.target.model_dump(mode="json"),
                "reason": action.reason,
                "project_revision_at_stage": action.project_revision_at_stage,
                "staged_policy_updated_at": (
                    action.staged_policy_updated_at.isoformat()
                    if action.staged_policy_updated_at
                    else None
                ),
                "project_revision_changed": project_revision_changed,
                "changes": [
                    change.model_dump(mode="json")
                    for change in action.changes[:MAX_PRESENTATION_ITEMS]
                ],
                "review": (
                    {
                        "decision": action.review.decision.value,
                        "reason": action.review.reason,
                        "decided_at": action.review.decided_at.isoformat(),
                        "valid_until": (
                            action.review.valid_until.isoformat()
                            if action.review.valid_until
                            else None
                        ),
                        "policy_updated_at": (
                            action.review.policy_updated_at.isoformat()
                            if action.review.policy_updated_at
                            else None
                        ),
                    }
                    if action.review
                    else None
                ),
                "execution": (
                    {
                        "id": action.execution_request.id,
                        "external_status": action.execution_request.external_status.value,
                        "adapter_required": action.execution_request.adapter_required,
                        "adapter_type": action.execution_request.adapter_type,
                        "requested_at": action.execution_request.requested_at.isoformat(),
                        "idempotency_key": action.execution_request.idempotency_key,
                        "attempt_count": action.execution_request.attempt_count,
                        "retry_not_before": (
                            action.execution_request.retry_not_before.isoformat()
                            if action.execution_request.retry_not_before
                            else None
                        ),
                        "last_error_code": action.execution_request.last_error_code,
                        "last_error_message": action.execution_request.last_error_message,
                        "receipt": (
                            {
                                "id": action.execution_request.receipt.id,
                                "remote_action_id": (
                                    action.execution_request.receipt.remote_action_id
                                ),
                                "remote_version": (action.execution_request.receipt.remote_version),
                                "http_status": action.execution_request.receipt.http_status,
                                "response_hash": (action.execution_request.receipt.response_hash),
                                "adapter_attempts": (
                                    action.execution_request.receipt.adapter_attempts
                                ),
                                "issued_at": (
                                    action.execution_request.receipt.issued_at.isoformat()
                                ),
                            }
                            if action.execution_request.receipt
                            else None
                        ),
                    }
                    if action.execution_request
                    else None
                ),
                "external_action_performed": (
                    action.status == CommercialActionStatus.APPLIED
                    and action.execution_request is not None
                    and action.execution_request.external_status
                    == ExternalExecutionStatus.SUCCEEDED
                    and action.execution_request.receipt is not None
                ),
                "created_at": action.created_at.isoformat(),
                "expires_at": action.expires_at.isoformat(),
            },
        },
        changes_truncated,
    )


def _presentation(
    project: OpportunityProject,
    component: str,
    payload: dict[str, Any],
    *,
    source_truncated: bool = False,
) -> dict[str, Any]:
    sanitized = sanitize_untrusted_payload(
        payload,
        UntrustedSource.PROJECT_PRESENTATION,
    )
    return _tool_result(
        {
            "presentation": {
                "external_data_notice": UNTRUSTED_DATA_NOTICE,
                "external_data_truncated": source_truncated or sanitized.truncated,
                "schema_version": PRESENTATION_SCHEMA_VERSION,
                "presentation_id": new_id("view"),
                "component": component,
                "project_id": project.id,
                "project_revision": project.revision,
                "origin": "project_ledger",
                "payload": sanitized.value,
            }
        }
    )


def presentation_frame_event(
    *,
    tool_name: str,
    tool_input: dict[str, Any],
    project_id: str,
    project_revision: int,
    tool_use_id: str | None,
    frame_type: PresentationFrameType,
    presentation_response: dict[str, Any] | None = None,
    clock: Callable[[], datetime] = utc_now,
) -> dict[str, Any] | None:
    """Build a bounded server-owned partial/final/discard event envelope.

    The model can suggest a title through tool args, but it cannot choose the
    slot, component, frame state, project scope, revision, or final facts.  A
    final frame is emitted only from a response that already passed the full
    product presentation validator.
    """

    component = _PRESENTATION_TOOL_COMPONENTS.get(tool_name)
    if component is None or not isinstance(tool_use_id, str) or not tool_use_id:
        return None
    slot_digest = hashlib.sha256(tool_use_id.encode("utf-8")).hexdigest()[:32]
    raw_title = tool_input.get("title")
    title = (
        raw_title.strip()
        if isinstance(raw_title, str) and raw_title.strip()
        else component.replace("_", " ")
    )
    sanitized_title = sanitize_untrusted_payload(
        title[:160],
        UntrustedSource.PROJECT_PRESENTATION,
    ).value
    if not isinstance(sanitized_title, str) or not sanitized_title:
        sanitized_title = component.replace("_", " ")
    frame = {
        "protocol_version": PRESENTATION_FRAME_PROTOCOL_VERSION,
        "slot_id": f"slot_{slot_digest}",
        "frame_type": frame_type.value,
        "schema_version": PRESENTATION_SCHEMA_VERSION,
        "component": component,
        "project_id": project_id,
        "project_revision": project_revision,
        "title": sanitized_title,
        "emitted_at": clock().isoformat(),
    }
    if frame_type == PresentationFrameType.FINAL:
        if presentation_response is None:
            return None
        presentation = presentation_response.get("presentation")
        if (
            not isinstance(presentation, dict)
            or presentation.get("component") != component
            or presentation.get("project_id") != project_id
            or presentation.get("project_revision") != project_revision
        ):
            return None
        return {"frame": frame, "presentation": presentation}
    if presentation_response is not None:
        return None
    return {"frame": frame}


def presentation_event_response(value: Any, project_id: str) -> dict[str, Any] | None:
    """Normalize our MCP text result into structured event data before redaction.

    EventPublisher intentionally caps individual strings at 20k. Keeping a whole
    presentation as JSON text would therefore make a large, otherwise valid card
    undecodable. Only envelopes stamped by this server and scoped to the current
    project are promoted into a structured event value.
    """

    def visit(item: Any) -> dict[str, Any] | None:
        if isinstance(item, str):
            if len(item) > 128_000:
                return None
            try:
                return visit(json.loads(item))
            except (TypeError, ValueError):
                return None
        if isinstance(item, list):
            for child in item:
                found = visit(child)
                if found is not None:
                    return found
            return None
        if not isinstance(item, dict):
            return None
        presentation = item.get("presentation")
        if isinstance(presentation, dict):
            presentation_id = presentation.get("presentation_id")
            component = presentation.get("component")
            revision = presentation.get("project_revision")
            if (
                presentation.get("schema_version") == PRESENTATION_SCHEMA_VERSION
                and presentation.get("origin") == "project_ledger"
                and presentation.get("project_id") == project_id
                and component in PRESENTATION_COMPONENTS
                and isinstance(presentation_id, str)
                and _PRESENTATION_ID.fullmatch(presentation_id)
                and len(presentation_id) <= 200
                and isinstance(revision, int)
                and not isinstance(revision, bool)
                and revision >= 1
                and presentation.get("external_data_notice") == UNTRUSTED_DATA_NOTICE
                and isinstance(presentation.get("external_data_truncated"), bool)
                and _valid_component_payload(
                    str(component), presentation.get("payload"), project_id
                )
            ):
                return {"presentation": presentation}
            return None
        content = item.get("content")
        if isinstance(content, list):
            return visit(content)
        block_text = item.get("text")
        return visit(block_text) if isinstance(block_text, str) else None

    return visit(value)


def build_presentation_tools(
    ledger: FileLedger,
    project_id: str,
    *,
    commercial_actions: CommercialActionService | None = None,
    clock: Callable[[], datetime] = utc_now,
):
    """Build typed UI tools whose factual fields come only from project truth.

    The model chooses a component, a title, and existing record ids. The server
    validates those ids and enriches the response from the current ledger snapshot.
    """

    @tool(
        "present_evidence_comparison",
        "Render a comparison of existing project evidence. Supply only a display title "
        "and ids returned by read_project_context/search_evidence; all facts are joined "
        "from the current server ledger. This does not modify project truth.",
        {
            "type": "object",
            "properties": {
                "title": {"type": "string", "minLength": 1, "maxLength": 160},
                "evidence_ids": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": MAX_PRESENTATION_ITEMS,
                    "uniqueItems": True,
                    "items": {"type": "string", "minLength": 1},
                },
            },
            "required": ["title", "evidence_ids"],
            "additionalProperties": False,
        },
    )
    async def present_evidence_comparison(args: dict[str, Any]) -> dict[str, Any]:
        title = _title(args.get("title"))
        evidence_ids = _ids(args.get("evidence_ids"), field="evidence_ids")
        project = ledger.get_project(project_id)
        evidence_by_id = {item.id: item for item in project.evidence}
        unknown = [item_id for item_id in evidence_ids if item_id not in evidence_by_id]
        if unknown:
            return _error(
                "UNKNOWN_EVIDENCE_IDS",
                "Call search_evidence or read_project_context, then retry with current ids.",
                unknown_ids=unknown,
            )
        now = clock()
        try:
            items = [
                _evidence_item(
                    evidence_by_id[item_id],
                    ledger=ledger,
                    project_id=project_id,
                    now=now,
                )
                for item_id in evidence_ids
            ]
        except _PresentationSourceError as exc:
            return _source_error(exc)
        return _presentation(
            project,
            "evidence_comparison",
            {"title": title, "items": items},
        )

    @tool(
        "present_supplier_comparison",
        "Render supplier or quote evidence from the current project ledger. Supply "
        "only a display title and evidence ids; supplier names, claims, sources, "
        "freshness, and confidence are joined by the server. The card is an analysis "
        "snapshot, not proof of current external terms and not an executable order.",
        {
            "type": "object",
            "properties": {
                "title": {"type": "string", "minLength": 1, "maxLength": 160},
                "evidence_ids": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": MAX_PRESENTATION_ITEMS,
                    "uniqueItems": True,
                    "items": {"type": "string", "minLength": 1},
                },
            },
            "required": ["title", "evidence_ids"],
            "additionalProperties": False,
        },
    )
    async def present_supplier_comparison(args: dict[str, Any]) -> dict[str, Any]:
        title = _title(args.get("title"))
        evidence_ids = _ids(args.get("evidence_ids"), field="evidence_ids")
        project = ledger.get_project(project_id)
        evidence_by_id = {item.id: item for item in project.evidence}
        unknown = [item_id for item_id in evidence_ids if item_id not in evidence_by_id]
        if unknown:
            return _error(
                "UNKNOWN_SUPPLIER_EVIDENCE_IDS",
                "Call search_evidence or read_project_context, then retry with current ids.",
                unknown_ids=unknown,
            )
        evidence = [evidence_by_id[item_id] for item_id in evidence_ids]
        now = clock()
        try:
            items = [
                _evidence_item(
                    item,
                    ledger=ledger,
                    project_id=project_id,
                    now=now,
                )
                for item in evidence
            ]
        except _PresentationSourceError as exc:
            return _source_error(exc)
        return _presentation(
            project,
            "supplier_comparison",
            {
                "title": title,
                "snapshot": _evidence_snapshot(evidence, now),
                "items": items,
            },
        )

    unit_economics_id_properties = {
        field: {
            "type": "string",
            "minLength": 1,
            "description": (
                "An evidence id whose excerpt (preferred) or claim is the exact "
                f"non-negative decimal scalar for {field}."
            ),
        }
        for field in _UNIT_ECONOMICS_FIELDS
    }
    unit_economics_id_properties["currency"] = {
        "type": "string",
        "minLength": 1,
        "description": (
            "An evidence id whose excerpt (preferred) or claim is a 3 to 12 letter currency code."
        ),
    }

    @tool(
        "present_unit_economics",
        "Render deterministic unit economics from scalar facts already recorded in "
        "the project ledger. Supply a display title and evidence ids only. The server "
        "hydrates every input and recomputes every output; it never trusts model-authored "
        "display values. This is an analysis snapshot, not a current external quote.",
        {
            "type": "object",
            "properties": {
                "title": {"type": "string", "minLength": 1, "maxLength": 160},
                "input_evidence_ids": {
                    "type": "object",
                    "properties": unit_economics_id_properties,
                    "required": [*_UNIT_ECONOMICS_FIELDS, "currency"],
                    "additionalProperties": False,
                },
            },
            "required": ["title", "input_evidence_ids"],
            "additionalProperties": False,
        },
    )
    async def present_unit_economics(args: dict[str, Any]) -> dict[str, Any]:
        title = _title(args.get("title"))
        input_ids = args.get("input_evidence_ids")
        if not isinstance(input_ids, dict):
            raise ValueError("input_evidence_ids must be an object of ledger ids")
        expected_fields = (*_UNIT_ECONOMICS_FIELDS, "currency")
        if set(input_ids) != set(expected_fields):
            raise ValueError("input_evidence_ids must contain exactly the documented fields")
        evidence_ids = [input_ids[field] for field in expected_fields]
        if any(not isinstance(item, str) or not item for item in evidence_ids):
            raise ValueError("input_evidence_ids must contain non-empty string ids")
        if len(set(evidence_ids)) != len(evidence_ids):
            raise ValueError("unit economics inputs must reference distinct evidence ids")
        project = ledger.get_project(project_id)
        evidence_by_id = {item.id: item for item in project.evidence}
        unknown = [item_id for item_id in evidence_ids if item_id not in evidence_by_id]
        if unknown:
            return _error(
                "UNKNOWN_UNIT_ECONOMICS_EVIDENCE_IDS",
                "Call search_evidence or read_project_context, then retry with current ids.",
                unknown_ids=unknown,
            )
        now = clock()
        try:
            payload = _unit_economics_payload(
                title,
                {field: evidence_by_id[input_ids[field]] for field in _UNIT_ECONOMICS_FIELDS},
                evidence_by_id[input_ids["currency"]],
                ledger=ledger,
                project_id=project_id,
                now=now,
            )
        except _PresentationSourceError as exc:
            return _source_error(exc)
        except ValueError as exc:
            return _error("INVALID_UNIT_ECONOMICS_EVIDENCE", str(exc))
        return _presentation(project, "unit_economics", payload)

    @tool(
        "present_decision_brief",
        "Render one existing project decision and its linked evidence. Supply only a "
        "decision id from read_project_context; all displayed facts come from the "
        "current server ledger. This does not modify project truth.",
        {
            "type": "object",
            "properties": {
                "decision_id": {"type": "string", "minLength": 1},
            },
            "required": ["decision_id"],
            "additionalProperties": False,
        },
    )
    async def present_decision_brief(args: dict[str, Any]) -> dict[str, Any]:
        decision_id = args.get("decision_id")
        if not isinstance(decision_id, str) or not decision_id:
            raise ValueError("decision_id must be a non-empty string")
        project = ledger.get_project(project_id)
        decision = next((item for item in project.decisions if item.id == decision_id), None)
        if decision is None:
            return _error(
                "UNKNOWN_DECISION_ID",
                "Call read_project_context, then retry with a current decision id.",
                unknown_ids=[decision_id],
            )
        evidence_by_id = {item.id: item for item in project.evidence}
        evidence = [
            evidence_by_id[item_id]
            for item_id in decision.evidence_ids
            if item_id in evidence_by_id
        ]
        evidence_truncated = len(evidence) > MAX_PRESENTATION_ITEMS
        now = clock()
        try:
            evidence_payload = [
                _evidence_item(
                    item,
                    ledger=ledger,
                    project_id=project_id,
                    now=now,
                )
                for item in evidence[:MAX_PRESENTATION_ITEMS]
            ]
        except _PresentationSourceError as exc:
            return _source_error(exc)
        return _presentation(
            project,
            "decision_brief",
            {
                "decision": _decision_item(
                    decision,
                    evidence_payload,
                )
            },
            source_truncated=evidence_truncated,
        )

    @tool(
        "present_commercial_action_diff",
        "Render one server-staged commercial action by id. All target, status, review, "
        "execution-request, and before/after fields are joined from the commercial action "
        "ledger. The card never approves or applies the action and explicitly marks "
        "external current-state verification and control-plane actionability.",
        {
            "type": "object",
            "properties": {
                "action_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 200,
                    "pattern": r"^[A-Za-z0-9_-]+$",
                },
            },
            "required": ["action_id"],
            "additionalProperties": False,
        },
    )
    async def present_commercial_action_diff(args: dict[str, Any]) -> dict[str, Any]:
        action_id = args.get("action_id")
        if not _identifier(action_id):
            raise ValueError("action_id must be a valid non-empty ledger id")
        if commercial_actions is None:
            return _error(
                "COMMERCIAL_ACTIONS_UNAVAILABLE",
                "Commercial action presentation is not connected on this server.",
            )
        try:
            project = ledger.get_project(project_id)
            action = commercial_actions.get(project_id, action_id)
        except (CommercialActionError, LedgerNotFoundError, ValueError):
            return _error(
                "UNKNOWN_COMMERCIAL_ACTION_ID",
                "List commercial actions in the authenticated control plane, then retry.",
                unknown_ids=[action_id],
            )
        payload, changes_truncated = _commercial_action_payload(
            action,
            project,
            now=clock(),
        )
        return _presentation(
            project,
            "commercial_action_diff",
            payload,
            source_truncated=changes_truncated,
        )

    @tool(
        "present_artifacts",
        "Render download cards for existing project artifacts. Supply only a display "
        "title and artifact ids from read_project_context; names, metadata, and download "
        "routes are joined by the server. This does not modify project truth.",
        {
            "type": "object",
            "properties": {
                "title": {"type": "string", "minLength": 1, "maxLength": 160},
                "artifact_ids": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": MAX_PRESENTATION_ITEMS,
                    "uniqueItems": True,
                    "items": {"type": "string", "minLength": 1},
                },
            },
            "required": ["title", "artifact_ids"],
            "additionalProperties": False,
        },
    )
    async def present_artifacts(args: dict[str, Any]) -> dict[str, Any]:
        title = _title(args.get("title"))
        artifact_ids = _ids(args.get("artifact_ids"), field="artifact_ids")
        project = ledger.get_project(project_id)
        artifacts_by_id = {item.id: item for item in project.artifacts}
        unknown = [item_id for item_id in artifact_ids if item_id not in artifacts_by_id]
        if unknown:
            return _error(
                "UNKNOWN_ARTIFACT_IDS",
                "Call read_project_context, then retry with current artifact ids.",
                unknown_ids=unknown,
            )
        items = [_artifact_item(artifacts_by_id[item_id]) for item_id in artifact_ids]
        return _presentation(project, "artifact_list", {"title": title, "items": items})

    return [
        present_evidence_comparison,
        present_supplier_comparison,
        present_unit_economics,
        present_decision_brief,
        present_commercial_action_diff,
        present_artifacts,
    ]


def build_presentation_mcp_server(
    ledger: FileLedger,
    project_id: str,
    *,
    commercial_actions: CommercialActionService | None = None,
):
    return create_sdk_mcp_server(
        name=PRESENTATION_MCP_SERVER_NAME,
        version="1.0.0",
        tools=build_presentation_tools(
            ledger,
            project_id,
            commercial_actions=commercial_actions,
        ),
    )
