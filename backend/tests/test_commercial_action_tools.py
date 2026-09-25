import json
from pathlib import Path

import pytest

from ophira_backend.commercial_action_tools import build_commercial_action_tools
from ophira_backend.commercial_actions import CommercialActionService
from ophira_backend.commercial_adapters import CommercialTargetObservation
from ophira_backend.constants import (
    TOOL_READ_COMMERCIAL_TARGET,
    TOOL_STAGE_COMMERCIAL_ACTION,
    CommercialActionOperation,
)
from ophira_backend.models import ProjectCreate
from ophira_backend.policy import OrgPolicyStore
from ophira_backend.storage import FileLedger

CONFIG_HASH = "a" * 64


class FakeTargetReader:
    def read_target(
        self,
        connector_id: str,
        resource_type: str,
        resource_key: str,
    ) -> CommercialTargetObservation:
        return CommercialTargetObservation(
            operation=CommercialActionOperation.SEND_MESSAGE,
            connector_id=connector_id,
            resource_type=resource_type,
            resource_key=resource_key,
            version="etag-7",
            state={
                "recipient": "supplier@example.test",
                "content": "Old reviewed draft",
            },
            connector_config_hash=CONFIG_HASH,
            canonical_source=(
                "https://commerce.example.test/api/v1/targets/"
                f"{resource_type}/{resource_key}"
            ),
        )

    def current_connector_config_hash(self, connector_id: str) -> str:
        assert connector_id == "supplier_outreach"
        return CONFIG_HASH


def read_args() -> dict:
    return {
        "connector_id": "supplier_outreach",
        "resource_type": "supplier_inquiry",
        "resource_key": "inquiry-100",
    }


def stage_args(receipt_id: str) -> dict:
    return {
        "receipt_id": receipt_id,
        "changes": [
            {
                "field": "content",
                "before": "Old reviewed draft",
                "after": "Could you quote 500 units FOB Ningbo?",
            }
        ],
        "reason": "Propose the supplier inquiry for human review",
        "expires_in_seconds": 3600,
    }


def parse_result(result: dict) -> dict:
    return json.loads(result["content"][0]["text"])


@pytest.mark.asyncio
async def test_read_then_stage_injects_server_scope_and_hydrates_receipt(
    tmp_path: Path,
) -> None:
    ledger = FileLedger(tmp_path)
    project = ledger.create_project(ProjectCreate(name="Kettle", objective="Validate"))
    run = ledger.create_run(project.id, "Propose a supplier inquiry")
    service = CommercialActionService(
        tmp_path / "commercial-actions",
        ledger=ledger,
        policy=OrgPolicyStore(tmp_path / "org" / "policy.json"),
        target_reader=FakeTargetReader(),
    )
    read_tool, stage_tool = build_commercial_action_tools(
        service,
        ledger,
        project.id,
        run.id,
    )

    assert read_tool.name == "read_commercial_target"
    assert stage_tool.name == "stage_commercial_action"
    assert "source_run_id" not in stage_tool.input_schema["properties"]
    assert "source_session_id" not in stage_tool.input_schema["properties"]
    assert "operation" not in stage_tool.input_schema["properties"]
    assert "target" not in stage_tool.input_schema["properties"]
    assert "observed_state" not in stage_tool.input_schema["properties"]
    assert stage_tool.input_schema["additionalProperties"] is False

    before_init = await read_tool.handler(read_args())
    assert before_init["is_error"] is True
    assert "not initialized" in parse_result(before_init)["error"]
    assert service.list(project.id) == []

    session_id = "sdk_server_observed_session"
    ledger.update_run(run.id, sdk_session_id=session_id)
    read_result = await read_tool.handler(read_args())
    assert read_result["is_error"] is False
    observation = parse_result(read_result)["target_observation"]
    assert observation["target"]["expected_version"] == "etag-7"
    receipt_id = observation["source_receipt"]["id"]

    staged_result = await stage_tool.handler(stage_args(receipt_id))
    assert staged_result["is_error"] is False
    staged = parse_result(staged_result)["staged_action"]
    assert staged["status"] == "staged"
    assert staged["source_receipt_id"] == receipt_id
    assert staged["external_action_performed"] is False
    assert "authenticated control plane" in staged["next_step"]
    assert "source_session_id" not in staged

    stored = service.get(project.id, staged["id"])
    assert stored.source_run_id == run.id
    assert stored.source_session_id == session_id
    assert stored.operation == CommercialActionOperation.SEND_MESSAGE
    assert stored.target.connector_id == "supplier_outreach"
    assert stored.observed_state == observation["observed_state"]
    assert stored.execution_request is None


@pytest.mark.asyncio
async def test_stage_tool_rejects_model_supplied_authority_fields(tmp_path: Path) -> None:
    ledger = FileLedger(tmp_path)
    project = ledger.create_project(ProjectCreate(name="Kettle", objective="Validate"))
    run = ledger.create_run(project.id, "Propose a supplier inquiry")
    ledger.update_run(run.id, sdk_session_id="sdk_server_observed_session")
    service = CommercialActionService(
        tmp_path / "commercial-actions",
        ledger=ledger,
        policy=OrgPolicyStore(tmp_path / "org" / "policy.json"),
        target_reader=FakeTargetReader(),
    )
    read_tool, stage_tool = build_commercial_action_tools(
        service,
        ledger,
        project.id,
        run.id,
    )
    read_result = await read_tool.handler(read_args())
    receipt_id = parse_result(read_result)["target_observation"]["source_receipt"]["id"]
    forged = {
        **stage_args(receipt_id),
        "operation": "change_price",
        "target": {
            "connector_id": "other",
            "resource_type": "listing",
            "resource_key": "sku-1",
            "expected_version": "invented",
        },
        "observed_state": {"price": 1},
    }

    result = await stage_tool.handler(forged)

    assert result["is_error"] is True
    assert "must come only from the server receipt" in parse_result(result)["error"]
    assert service.list(project.id) == []


def test_agent_surface_contains_read_and_stage_but_not_review_or_apply(
    tmp_path: Path,
) -> None:
    ledger = FileLedger(tmp_path)
    project = ledger.create_project(ProjectCreate(name="Kettle", objective="Validate"))
    run = ledger.create_run(project.id, "Propose a supplier inquiry")
    service = CommercialActionService(
        tmp_path / "commercial-actions",
        ledger=ledger,
        policy=OrgPolicyStore(tmp_path / "org" / "policy.json"),
        target_reader=FakeTargetReader(),
    )

    tools = build_commercial_action_tools(service, ledger, project.id, run.id)
    assert [item.name for item in tools] == [
        "read_commercial_target",
        "stage_commercial_action",
    ]
    assert TOOL_READ_COMMERCIAL_TARGET.endswith("__read_commercial_target")
    assert TOOL_STAGE_COMMERCIAL_ACTION.endswith("__stage_commercial_action")
    assert all("review" not in item.name and "apply" not in item.name for item in tools)
    assert tools[0].annotations is not None
    assert tools[0].annotations.read_only_hint is True
    assert tools[0].annotations.idempotent_hint is False
    assert tools[0].annotations.open_world_hint is True
    assert tools[1].annotations is not None
    assert tools[1].annotations.read_only_hint is False
    assert tools[1].annotations.destructive_hint is False
