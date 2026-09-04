"""Agent-facing trusted read -> stage surface for commercial actions."""

from __future__ import annotations

import json
from typing import Any

from claude_agent_sdk import ToolAnnotations, create_sdk_mcp_server, tool
from pydantic import ValidationError

from .commercial_actions import CommercialActionError, CommercialActionService
from .commercial_adapters import CommercialAdapterError
from .constants import COMMERCIAL_ACTION_MCP_SERVER_NAME
from .models import CommercialActionReceiptStage
from .storage import FileLedger, LedgerNotFoundError, SourceReceiptError
from .untrusted import UNTRUSTED_DATA_NOTICE

_FIELD_PATH_PATTERN = (
    r"^[A-Za-z][A-Za-z0-9_-]{0,63}(?:\.[A-Za-z][A-Za-z0-9_-]{0,63}){0,7}$"
)
_RESOURCE_KEY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:@+\-]{0,499}$"


def _result(value: dict[str, Any], *, is_error: bool = False) -> dict[str, Any]:
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(value, ensure_ascii=False, separators=(",", ":")),
            }
        ],
        "is_error": is_error,
    }


def build_commercial_action_tools(
    service: CommercialActionService,
    ledger: FileLedger,
    project_id: str,
    run_id: str,
):
    @tool(
        "read_commercial_target",
        "Read one current commercial target through its server-configured connector. "
        "The response is external untrusted data plus a short-lived, server-issued "
        "receipt bound to this project, run, initialized SDK session, connector, "
        "resource, version, and exact sanitized state. Read before staging; never "
        "invent or copy a target/version/state into the stage call.",
        {
            "type": "object",
            "properties": {
                "connector_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 160,
                    "pattern": r"^[A-Za-z0-9_-]+$",
                },
                "resource_type": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 160,
                    "pattern": r"^[A-Za-z0-9_-]+$",
                },
                "resource_key": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 500,
                    "pattern": _RESOURCE_KEY_PATTERN,
                },
            },
            "required": ["connector_id", "resource_type", "resource_key"],
            "additionalProperties": False,
        },
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            # A repeat issues another durable audit receipt even when the
            # external state is unchanged, so this is not strictly idempotent.
            idempotentHint=False,
            openWorldHint=True,
        ),
    )
    async def read_commercial_target(args: dict[str, Any]) -> dict[str, Any]:
        unexpected = set(args) - {"connector_id", "resource_type", "resource_key"}
        if unexpected:
            return _result(
                {"error": "Commercial target read contained unsupported fields."},
                is_error=True,
            )
        try:
            run = ledger.get_run(run_id)
        except (LedgerNotFoundError, ValueError):
            return _result(
                {"error": "The server-bound source run no longer exists."},
                is_error=True,
            )
        if run.project_id != project_id or not run.sdk_session_id:
            return _result(
                {
                    "error": (
                        "The server-bound SDK session is not initialized for this project. "
                        "Wait for session initialization and retry; do not provide a session ID."
                    )
                },
                is_error=True,
            )
        try:
            verified = service.observe_target(
                project_id,
                source_run_id=run.id,
                source_session_id=run.sdk_session_id,
                connector_id=args.get("connector_id"),
                resource_type=args.get("resource_type"),
                resource_key=args.get("resource_key"),
            )
        except (
            CommercialActionError,
            CommercialAdapterError,
            SourceReceiptError,
            TypeError,
            ValueError,
        ) as exc:
            return _result(
                {"error": f"Commercial target was not read: {exc}"},
                is_error=True,
            )
        receipt = verified.receipt
        observed = verified.payload
        return _result(
            {
                "note": UNTRUSTED_DATA_NOTICE,
                "target_observation": {
                    "operation": observed.operation.value,
                    "target": observed.target.model_dump(mode="json"),
                    "observed_state": observed.observed_state,
                    "source_receipt": {
                        "id": receipt.id,
                        "content_hash": receipt.content_hash,
                        "retrieved_at": receipt.retrieved_at.isoformat(),
                        "expires_at": receipt.expires_at.isoformat(),
                    },
                    "external_action_performed": False,
                    "next_step": (
                        "Use only source_receipt.id, proposed changes, and a reason "
                        "with stage_commercial_action."
                    ),
                },
            }
        )

    @tool(
        "stage_commercial_action",
        "Stage a structured high-consequence commerce change for later human review. "
        "This only creates a proposal in the OPHIRA ledger; it never approves, applies, "
        "sends, publishes, spends, or mutates an external system. It accepts only the "
        "server receipt from read_commercial_target plus the proposed changes/reason; "
        "the host hydrates operation, target, version, and observed state. Project, run, "
        "and SDK session identity are injected and verified by the server. After staging, "
        "tell the user that review and apply must happen in the authenticated control plane.",
        {
            "type": "object",
            "properties": {
                "receipt_id": {
                    "type": "string",
                    "minLength": 20,
                    "maxLength": 200,
                    "pattern": r"^receipt_[A-Za-z0-9_-]{20,}$",
                    "description": (
                        "The source_receipt.id returned by read_commercial_target "
                        "in this exact run and initialized SDK session."
                    ),
                },
                "changes": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 100,
                    "items": {
                        "type": "object",
                        "properties": {
                            "field": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 519,
                                "pattern": _FIELD_PATH_PATTERN,
                            },
                            "before": {},
                            "after": {},
                        },
                        "required": ["field", "before", "after"],
                        "additionalProperties": False,
                    },
                },
                "reason": {"type": "string", "minLength": 1, "maxLength": 4000},
                "expires_in_seconds": {
                    "type": "integer",
                    "minimum": 60,
                    "maximum": 7 * 86_400,
                    "default": 86_400,
                },
            },
            "required": ["receipt_id", "changes", "reason"],
            "additionalProperties": False,
        },
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
    async def stage_commercial_action(args: dict[str, Any]) -> dict[str, Any]:
        unexpected = set(args) - {
            "receipt_id",
            "changes",
            "reason",
            "expires_in_seconds",
        }
        if unexpected:
            return _result(
                {
                    "error": (
                        "Commercial action was not staged: operation, target, version, "
                        "and observed state must come only from the server receipt."
                    )
                },
                is_error=True,
            )
        try:
            run = ledger.get_run(run_id)
        except (LedgerNotFoundError, ValueError):
            return _result(
                {"error": "The server-bound source run no longer exists."},
                is_error=True,
            )
        if run.project_id != project_id or not run.sdk_session_id:
            return _result(
                {
                    "error": (
                        "The server-bound SDK session is not initialized for this project. "
                        "Wait for session initialization and retry; do not provide a session ID."
                    )
                },
                is_error=True,
            )
        try:
            payload = CommercialActionReceiptStage(
                source_run_id=run.id,
                source_session_id=run.sdk_session_id,
                receipt_id=args.get("receipt_id"),
                changes=args.get("changes"),
                reason=args.get("reason"),
                expires_in_seconds=args.get("expires_in_seconds", 86_400),
            )
            action = service.stage_from_receipt(project_id, payload)
        except (
            CommercialActionError,
            SourceReceiptError,
            ValidationError,
            ValueError,
        ) as exc:
            return _result(
                {"error": f"Commercial action was not staged: {exc}"},
                is_error=True,
            )
        return _result(
            {
                "staged_action": {
                    "id": action.id,
                    "project_id": action.project_id,
                    "source_run_id": action.source_run_id,
                    "source_receipt_id": action.source_receipt_id,
                    "status": action.status.value,
                    "operation": action.operation.value,
                    "target": action.target.model_dump(mode="json"),
                    "change_count": len(action.changes),
                    "changed_fields": [change.field for change in action.changes[:20]],
                    "changed_fields_truncated": len(action.changes) > 20,
                    "reason": action.reason,
                    "expires_at": action.expires_at.isoformat(),
                    "external_action_performed": False,
                    "next_step": (
                        "A human must review this action through the authenticated control "
                        f"plane at /api/projects/{project_id}/commercial-actions/{action.id}."
                    ),
                }
            }
        )

    return [read_commercial_target, stage_commercial_action]


def build_commercial_action_mcp_server(
    service: CommercialActionService,
    ledger: FileLedger,
    project_id: str,
    run_id: str,
):
    return create_sdk_mcp_server(
        name=COMMERCIAL_ACTION_MCP_SERVER_NAME,
        version="1.0.0",
        tools=build_commercial_action_tools(service, ledger, project_id, run_id),
    )
