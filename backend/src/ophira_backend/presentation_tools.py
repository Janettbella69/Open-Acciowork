from __future__ import annotations

import json
import math
import re
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from .constants import PRESENTATION_MCP_SERVER_NAME
from .models import Artifact, Decision, Evidence, OpportunityProject, new_id
from .storage import FileLedger
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
    "decision_brief",
    "artifact_list",
)


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


def _valid_source(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and _text(value.get("title"), 4_000)
        and (value.get("url") is None or _text(value.get("url"), 2_000))
        and _text(value.get("type"), 80)
        and (
            value.get("observed_at") is None
            or _text(value.get("observed_at"), 100)
        )
        and _text(value.get("retrieved_at"), 100)
    )


def _valid_evidence(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and _identifier(value.get("id"))
        and (
            value.get("dimension") is None
            or _text(value.get("dimension"), 80)
        )
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


def _valid_component_payload(
    component: str,
    payload: Any,
    project_id: str,
) -> bool:
    if not isinstance(payload, dict):
        return False
    if component == "evidence_comparison":
        return _text(payload.get("title"), 160) and _valid_evidence_list(
            payload.get("items")
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
            and (
                decision.get("review_at") is None
                or _text(decision.get("review_at"), 100)
            )
            and _text(decision.get("created_at"), 100)
            and _valid_evidence_list(decision.get("evidence"), allow_empty=True)
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
        expected_download = (
            f"/api/projects/{project_id}/artifacts/{item['id']}/download"
        )
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


def _evidence_item(item: Evidence) -> dict[str, Any]:
    return {
        "id": item.id,
        "dimension": item.dimension.value if item.dimension else None,
        "tags": list(item.tags),
        "kind": item.kind.value,
        "claim": item.claim,
        "source": {
            "title": item.source_title,
            "url": str(item.source_url) if item.source_url else None,
            "type": item.source_type.value,
            "observed_at": item.observed_at.isoformat() if item.observed_at else None,
            "retrieved_at": item.retrieved_at.isoformat(),
        },
        "status": item.status.value,
        "confidence": item.confidence,
        "recorded_at": item.recorded_at.isoformat(),
    }


def _decision_item(item: Decision, evidence: list[Evidence]) -> dict[str, Any]:
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
        "evidence": [_evidence_item(evidence_item) for evidence_item in evidence],
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


def build_presentation_tools(ledger: FileLedger, project_id: str):
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
        items = [_evidence_item(evidence_by_id[item_id]) for item_id in evidence_ids]
        return _presentation(
            project,
            "evidence_comparison",
            {"title": title, "items": items},
        )

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
        return _presentation(
            project,
            "decision_brief",
            {
                "decision": _decision_item(
                    decision,
                    evidence[:MAX_PRESENTATION_ITEMS],
                )
            },
            source_truncated=evidence_truncated,
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

    return [present_evidence_comparison, present_decision_brief, present_artifacts]


def build_presentation_mcp_server(ledger: FileLedger, project_id: str):
    return create_sdk_mcp_server(
        name=PRESENTATION_MCP_SERVER_NAME,
        version="1.0.0",
        tools=build_presentation_tools(ledger, project_id),
    )
