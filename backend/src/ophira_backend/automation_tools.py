"""`ophira_automation` MCP server (PRD §4.6 / §4.16).

Governance split (§4.10 envelope principle): creating or pausing schedules and
standing intents are cross-envelope commitments -> they ASK for authorization.
Delivering into the inbox (`send_notification`) and firing an already-authorized
standing intent (`trigger_standing_intent`, budget-throttled) happen inside the
approved run envelope -> auto-approved, so proactive scheduled runs work with
nobody watching. Channel delivery (IM/mobile) stays out until FR-24/25.
"""

from __future__ import annotations

import json
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from .automation import IntentNotFoundError, IntentStore, NotificationStore
from .constants import AUTOMATION_MCP_SERVER_NAME
from .models import PrincipalBinding, ScheduleCreate, StandingIntentCreate
from .scheduler import ScheduleValidationError, new_schedule
from .storage import FileLedger, LedgerNotFoundError


def _result(value: Any, *, is_error: bool = False) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    elif isinstance(value, list):
        value = [
            item.model_dump(mode="json") if hasattr(item, "model_dump") else item
            for item in value
        ]
    return {
        "content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}],
        "is_error": is_error,
    }


def _source_principal_binding(
    ledger: FileLedger,
    project_id: str,
    run_id: str | None,
) -> PrincipalBinding | None:
    """Recover automation authority only from the durable source run."""

    try:
        project = ledger.get_project(project_id)
    except (LedgerNotFoundError, ValueError) as exc:
        raise ValueError("automation project is unavailable") from exc
    if run_id is None:
        if project.identity_binding == "authenticated_principal":
            raise ValueError(
                "authenticated automation creation requires a source run identity"
            )
        return None
    try:
        run = ledger.get_run(run_id)
    except (LedgerNotFoundError, ValueError) as exc:
        raise ValueError("automation source run is unavailable") from exc
    if run.project_id != project_id or run.identity_binding != project.identity_binding:
        raise ValueError("automation source run belongs to another identity scope")
    if project.identity_binding != "authenticated_principal":
        return None
    if (
        run.tenant_id != project.tenant_id
        or run.principal_id is None
        or run.principal_role is None
        or run.principal_session_id is None
    ):
        raise ValueError("automation source run principal scope is incomplete")
    return PrincipalBinding(
        tenant_id=run.tenant_id,
        principal_id=run.principal_id,
        role=run.principal_role,
        session_id=run.principal_session_id,
    )


def build_automation_tools(
    ledger: FileLedger,
    intents: IntentStore,
    notifications: NotificationStore,
    project_id: str,
    *,
    run_id: str | None = None,
    schedule_id: str | None = None,
    bound_intent_id: str | None = None,
):
    @tool(
        "create_schedule",
        "Create a recurring or one-off backend schedule for this project after the "
        "user approves. Runs server-side; identical active schedules are reused "
        "(idempotent).",
        {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "minLength": 1, "maxLength": 40000},
                "cron": {"type": ["string", "null"], "minLength": 9, "maxLength": 120},
                "run_once_at": {"type": ["string", "null"], "format": "date-time"},
                "timezone": {"type": "string", "minLength": 1, "maxLength": 64},
                "missed_run_policy": {"type": "string", "enum": ["catch_up", "skip"]},
            },
            "required": ["prompt", "timezone"],
            "additionalProperties": False,
        },
    )
    async def create_schedule(args: dict[str, Any]) -> dict[str, Any]:
        try:
            principal_binding = _source_principal_binding(
                ledger,
                project_id,
                run_id,
            )
        except ValueError as exc:
            return _result({"error": str(exc)}, is_error=True)
        for existing in ledger.list_schedules():
            if (
                existing.enabled
                and existing.project_id == project_id
                and existing.principal_binding == principal_binding
                and existing.prompt == args["prompt"]
                and existing.cron == args.get("cron")
                and (
                    existing.run_once_at.isoformat()
                    if existing.run_once_at
                    else None
                )
                == args.get("run_once_at")
            ):
                return _result({"schedule": existing.model_dump(mode="json"),
                                "reused_existing": True})
        try:
            schedule = new_schedule(
                ScheduleCreate(
                    project_id=project_id,
                    prompt=args["prompt"],
                    cron=args.get("cron"),
                    run_once_at=args.get("run_once_at"),
                    timezone=args["timezone"],
                    missed_run_policy=args.get("missed_run_policy", "skip"),
                ),
                principal=principal_binding,
            )
        except (ScheduleValidationError, ValueError) as exc:
            return _result({"error": str(exc)}, is_error=True)
        created = ledger.create_schedule(schedule)
        return _result({"schedule": created.model_dump(mode="json"), "reused_existing": False})

    @tool(
        "pause_schedule",
        "Pause one of this project's schedules (requires user approval).",
        {
            "type": "object",
            "properties": {"schedule_id": {"type": "string", "minLength": 1}},
            "required": ["schedule_id"],
            "additionalProperties": False,
        },
    )
    async def pause_schedule(args: dict[str, Any]) -> dict[str, Any]:
        try:
            schedule = ledger.get_schedule(args["schedule_id"])
        except KeyError:
            return _result({"error": f"unknown schedule: {args['schedule_id']}"}, is_error=True)
        if schedule.project_id != project_id:
            return _result({"error": "schedule belongs to another project"}, is_error=True)
        return _result(ledger.update_schedule(schedule.id, enabled=False))

    @tool(
        "create_standing_intent",
        "Register a user-approved standing intent ('tell me when X') with an anti-nag "
        "budget (cooldown/max triggers/expiry). Identical active intents are reused.",
        {
            "type": "object",
            "properties": {
                "intent": {"type": "string", "minLength": 1, "maxLength": 2000},
                "cooldown_hours": {"type": "number", "minimum": 0, "maximum": 720},
                "max_triggers": {"type": "integer", "minimum": 1, "maximum": 100},
                "expires_days": {"type": "integer", "minimum": 1, "maximum": 365},
                "check_interval_minutes": {
                    "type": "integer",
                    "minimum": 15,
                    "maximum": 10080,
                },
                "check_timeout_seconds": {
                    "type": "integer",
                    "minimum": 60,
                    "maximum": 7200,
                },
                "per_check_budget_usd": {
                    "type": "number",
                    "exclusiveMinimum": 0,
                    "maximum": 100,
                },
                "total_budget_usd": {
                    "type": "number",
                    "exclusiveMinimum": 0,
                    "maximum": 10000,
                },
            },
            "required": ["intent"],
            "additionalProperties": False,
        },
    )
    async def create_standing_intent(args: dict[str, Any]) -> dict[str, Any]:
        if run_id is None:
            return _result(
                {"error": "standing intent creation requires a user-approved source run"},
                is_error=True,
            )
        try:
            source_run = ledger.get_run(run_id)
        except (KeyError, ValueError):
            return _result(
                {"error": "standing intent source run is unavailable"},
                is_error=True,
            )
        if source_run.project_id != project_id:
            return _result(
                {"error": "standing intent source run belongs to another project"},
                is_error=True,
            )
        try:
            principal_binding = _source_principal_binding(
                ledger,
                project_id,
                run_id,
            )
        except ValueError as exc:
            return _result({"error": str(exc)}, is_error=True)
        agent_id = source_run.agent_id
        provider_profile_id = source_run.provider_profile_id
        for existing in intents.list(project_id=project_id, enabled_only=True):
            if (
                existing.intent.strip() == args["intent"].strip()
                and existing.principal_binding == principal_binding
                and existing.agent_id == agent_id
                and existing.provider_profile_id == provider_profile_id
                and existing.cooldown_hours == args.get("cooldown_hours", 24)
                and existing.max_triggers == args.get("max_triggers", 3)
                and existing.check_interval_minutes
                == args.get("check_interval_minutes", 1_440)
                and existing.check_timeout_seconds
                == args.get("check_timeout_seconds", 1_800)
                and existing.per_check_budget_usd
                == args.get("per_check_budget_usd", 0.5)
                and existing.total_budget_usd == args.get("total_budget_usd", 15)
            ):
                return _result({"intent": existing.model_dump(mode="json"),
                                "reused_existing": True})
        created = intents.create(
            StandingIntentCreate(
                project_id=project_id,
                agent_id=agent_id,
                provider_profile_id=provider_profile_id,
                intent=args["intent"],
                cooldown_hours=args.get("cooldown_hours", 24),
                max_triggers=args.get("max_triggers", 3),
                expires_days=args.get("expires_days", 90),
                check_interval_minutes=args.get("check_interval_minutes", 1_440),
                check_timeout_seconds=args.get("check_timeout_seconds", 1_800),
                per_check_budget_usd=args.get("per_check_budget_usd", 0.5),
                total_budget_usd=args.get("total_budget_usd", 15),
            ),
            authorization_source="user_approved_agent",
            principal=principal_binding,
        )
        return _result({"intent": created.model_dump(mode="json"), "reused_existing": False})

    @tool(
        "list_standing_intents",
        "List this project's standing intents with their remaining anti-nag budget.",
        {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
    )
    async def list_standing_intents(_args: dict[str, Any]) -> dict[str, Any]:
        return _result(intents.list(project_id=project_id))

    @tool(
        "trigger_standing_intent",
        "Fire a standing intent because its condition was observed. The anti-nag "
        "budget decides whether the inbox notification is actually delivered; "
        "respect a suppressed outcome instead of retrying.",
        {
            "type": "object",
            "properties": {
                "intent_id": {"type": "string", "minLength": 1},
                "summary": {"type": "string", "minLength": 1, "maxLength": 2000},
            },
            "required": ["intent_id", "summary"],
            "additionalProperties": False,
        },
    )
    async def trigger_standing_intent(args: dict[str, Any]) -> dict[str, Any]:
        try:
            intent = intents.get(args["intent_id"])
        except IntentNotFoundError:
            return _result({"error": f"unknown intent: {args['intent_id']}"}, is_error=True)
        if bound_intent_id is not None and intent.id != bound_intent_id:
            return _result(
                {"error": "evaluation run may trigger only its bound intent"},
                is_error=True,
            )
        if bound_intent_id is not None and run_id is None:
            return _result(
                {"error": "evaluation trigger is missing its bound run"},
                is_error=True,
            )
        if intent.project_id != project_id:
            return _result({"error": "intent belongs to another project"}, is_error=True)
        outcome, intent = intents.try_trigger(
            intent.id,
            expected_check_run_id=(run_id if bound_intent_id is not None else None),
        )
        delivered = None
        if outcome == "triggered":
            delivered = notifications.add(
                title=f"前瞻意图命中：{intent.intent[:80]}",
                body=args["summary"],
                severity="info",
                project_id=project_id,
                run_id=run_id,
                schedule_id=schedule_id,
                intent_id=intent.id,
            )
        return _result(
            {
                "outcome": outcome,
                "delivered_notification_id": delivered.id if delivered else None,
                "budget": {
                    "triggered_count": intent.triggered_count,
                    "max_triggers": intent.max_triggers,
                    "cooldown_hours": intent.cooldown_hours,
                    "expires_at": intent.expires_at.isoformat(),
                },
            }
        )

    @tool(
        "send_notification",
        "Deliver one result into the user's notification inbox. Use only when "
        "something actually changed or needs attention; when nothing happened, "
        "stay silent (HEARTBEAT_OK semantics) instead of sending.",
        {
            "type": "object",
            "properties": {
                "title": {"type": "string", "minLength": 1, "maxLength": 200},
                "body": {"type": "string", "maxLength": 4000},
                "severity": {"type": "string", "enum": ["info", "warning", "error"]},
            },
            "required": ["title"],
            "additionalProperties": False,
        },
    )
    async def send_notification(args: dict[str, Any]) -> dict[str, Any]:
        notification = notifications.add(
            title=args["title"],
            body=args.get("body", ""),
            severity=args.get("severity", "info"),
            project_id=project_id,
            run_id=run_id,
            schedule_id=schedule_id,
        )
        return _result(notification)

    return [
        create_schedule,
        pause_schedule,
        create_standing_intent,
        list_standing_intents,
        trigger_standing_intent,
        send_notification,
    ]


def build_automation_mcp_server(
    ledger: FileLedger,
    intents: IntentStore,
    notifications: NotificationStore,
    project_id: str,
    *,
    run_id: str | None = None,
    schedule_id: str | None = None,
    bound_intent_id: str | None = None,
):
    return create_sdk_mcp_server(
        name=AUTOMATION_MCP_SERVER_NAME,
        version="1.0.0",
        tools=build_automation_tools(
            ledger, intents, notifications, project_id,
            run_id=run_id,
            schedule_id=schedule_id,
            bound_intent_id=bound_intent_id,
        ),
    )
