import json
from pathlib import Path

import pytest

from ophira_backend.commercial_action_tools import build_commercial_action_tools
from ophira_backend.commercial_actions import CommercialActionService
from ophira_backend.constants import TOOL_STAGE_COMMERCIAL_ACTION
from ophira_backend.models import ProjectCreate
from ophira_backend.policy import OrgPolicyStore
from ophira_backend.storage import FileLedger


def tool_args() -> dict:
    return {
        "operation": "change_price",
        "target": {
            "connector_id": "shopify",
            "resource_type": "listing",
            "resource_key": "sku-100",
            "expected_version": "etag-7",
        },
        "observed_state": {"price": {"amount": 100, "currency": "USD"}},
        "changes": [{"field": "price.amount", "before": 100, "after": 95}],
        "reason": "Propose a launch discount for human review",
        "expires_in_seconds": 3600,
    }


def parse_result(result: dict) -> dict:
    return json.loads(result["content"][0]["text"])


@pytest.mark.asyncio
async def test_stage_tool_injects_server_run_and_initialized_sdk_session(
    tmp_path: Path,
) -> None:
    ledger = FileLedger(tmp_path)
    project = ledger.create_project(ProjectCreate(name="Kettle", objective="Validate"))
    run = ledger.create_run(project.id, "Propose a price change")
    service = CommercialActionService(
        tmp_path / "commercial-actions",
        ledger=ledger,
        policy=OrgPolicyStore(tmp_path / "org" / "policy.json"),
    )
    tool = build_commercial_action_tools(service, ledger, project.id, run.id)[0]

    assert tool.name == "stage_commercial_action"
    assert "source_run_id" not in tool.input_schema["properties"]
    assert "source_session_id" not in tool.input_schema["properties"]
    assert tool.input_schema["additionalProperties"] is False

    before_init = await tool.handler(tool_args())
    assert before_init["is_error"] is True
    assert "not initialized" in parse_result(before_init)["error"]
    assert service.list(project.id) == []

    session_id = "sdk_server_observed_session"
    ledger.update_run(run.id, sdk_session_id=session_id)
    staged_result = await tool.handler(tool_args())
    assert staged_result["is_error"] is False
    staged = parse_result(staged_result)["staged_action"]
    assert staged["status"] == "staged"
    assert staged["external_action_performed"] is False
    assert "authenticated control plane" in staged["next_step"]
    assert "source_session_id" not in staged

    stored = service.get(project.id, staged["id"])
    assert stored.source_run_id == run.id
    assert stored.source_session_id == session_id
    assert stored.execution_request is None


def test_agent_surface_contains_only_stage_not_review_or_apply(tmp_path: Path) -> None:
    ledger = FileLedger(tmp_path)
    project = ledger.create_project(ProjectCreate(name="Kettle", objective="Validate"))
    run = ledger.create_run(project.id, "Propose a price change")
    service = CommercialActionService(
        tmp_path / "commercial-actions",
        ledger=ledger,
        policy=OrgPolicyStore(tmp_path / "org" / "policy.json"),
    )

    tools = build_commercial_action_tools(service, ledger, project.id, run.id)
    assert [item.name for item in tools] == ["stage_commercial_action"]
    assert TOOL_STAGE_COMMERCIAL_ACTION.endswith("__stage_commercial_action")
    assert all("review" not in item.name and "apply" not in item.name for item in tools)
    assert tools[0].annotations is not None
    assert tools[0].annotations.readOnlyHint is False
    assert tools[0].annotations.destructiveHint is False
