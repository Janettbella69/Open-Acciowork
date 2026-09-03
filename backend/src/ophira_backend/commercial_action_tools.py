"""Agent-facing MCP surface for staging, never approving or applying, commerce actions."""

from __future__ import annotations

import json
from typing import Any

from claude_agent_sdk import ToolAnnotations, create_sdk_mcp_server, tool
from pydantic import ValidationError

from .commercial_actions import CommercialActionError, CommercialActionService
from .constants import COMMERCIAL_ACTION_MCP_SERVER_NAME, CommercialActionOperation
from .models import CommercialActionStage
from .storage import FileLedger, LedgerNotFoundError

_FIELD_PATH_PATTERN = (
    r"^[A-Za-z][A-Za-z0-9_-]{0,63}(?:\.[A-Za-z][A-Za-z0-9_-]{0,63}){0,7}$"
)


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
        "stage_commercial_action",
        "Stage a structured high-consequence commerce change for later human review. "
        "This only creates a proposal in the OPHIRA ledger; it never approves, applies, "
        "sends, publishes, spends, or mutates an external system. Project, run, and SDK "
        "session identity are injected and verified by the server. After staging, tell "
        "the user that review and apply must happen in the authenticated control plane.",
        {
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": [item.value for item in CommercialActionOperation],
                },
                "target": {
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
                        },
                        "expected_version": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 500,
                            "description": (
                                "External version/ETag that a future adapter must match; "
                                "use an explicit 'absent' sentinel for a create."
                            ),
                        },
                    },
                    "required": [
                        "connector_id",
                        "resource_type",
                        "resource_key",
                        "expected_version",
                    ],
                    "additionalProperties": False,
                },
                "observed_state": {
                    "type": "object",
                    "maxProperties": 200,
                    "additionalProperties": True,
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
            "required": ["operation", "target", "observed_state", "changes", "reason"],
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
            payload = CommercialActionStage(
                source_run_id=run.id,
                source_session_id=run.sdk_session_id,
                operation=args.get("operation"),
                target=args.get("target"),
                observed_state=args.get("observed_state"),
                changes=args.get("changes"),
                reason=args.get("reason"),
                expires_in_seconds=args.get("expires_in_seconds", 86_400),
            )
            action = service.stage(project_id, payload)
        except (CommercialActionError, ValidationError, ValueError) as exc:
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

    return [stage_commercial_action]


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
