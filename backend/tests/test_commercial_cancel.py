from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ophira_backend.commercial_actions import (
    CommercialActionConflictError,
    CommercialActionService,
)
from ophira_backend.constants import (
    CommercialActionOperation,
    CommercialActionStatus,
    ExternalExecutionStatus,
)
from ophira_backend.models import (
    CommercialActionApplyRequest,
    CommercialActionCancelRequest,
    CommercialActionReviewRequest,
    CommercialActionStage,
    OrgPolicy,
    ProjectCreate,
)
from ophira_backend.policy import OrgPolicyStore
from ophira_backend.storage import FileLedger

NOW = datetime(2026, 9, 3, 12, tzinfo=UTC)


def make_service(tmp_path: Path):
    ledger = FileLedger(tmp_path)
    project = ledger.create_project(ProjectCreate(name="Kettle", objective="Validate"))
    run = ledger.create_run(project.id, "Stage a price proposal")
    session_id = "sdk_commercial_cancel_session"
    ledger.update_run(run.id, sdk_session_id=session_id)
    policy = OrgPolicyStore(tmp_path / "org" / "policy.json")
    policy.save(
        OrgPolicy(
            commercial_actions_enabled=True,
            allowed_commercial_operations=[CommercialActionOperation.CHANGE_PRICE],
            allowed_commercial_connector_ids=["shopify"],
        )
    )
    service = CommercialActionService(
        tmp_path / "commercial-actions",
        ledger=ledger,
        policy=policy,
        clock=lambda: NOW,
    )
    return service, project.id, run.id, session_id


def stage(service, project_id: str, run_id: str, session_id: str):
    return service.stage(
        project_id,
        CommercialActionStage.model_validate(
            {
                "source_run_id": run_id,
                "source_session_id": session_id,
                "operation": "change_price",
                "target": {
                    "connector_id": "shopify",
                    "resource_type": "listing",
                    "resource_key": "sku-100",
                    "expected_version": "etag-7",
                },
                "observed_state": {"price": 100},
                "changes": [{"field": "price", "before": 100, "after": 95}],
                "reason": "Propose a discount",
            }
        ),
    )


def approve(service, project_id: str, action_id: str):
    return service.review(
        project_id,
        action_id,
        CommercialActionReviewRequest(
            decision="approve",
            reason="Discount checked",
            valid_for_seconds=900,
        ),
        actor_id="reviewer@example.test",
    )


@pytest.mark.parametrize("approve_first", [False, True])
def test_staged_or_approved_action_can_be_cancelled_idempotently(
    tmp_path: Path,
    approve_first: bool,
) -> None:
    service, project_id, run_id, session_id = make_service(tmp_path)
    action = stage(service, project_id, run_id, session_id)
    if approve_first:
        action = approve(service, project_id, action.id)
    before_history = len(action.history)
    request = CommercialActionCancelRequest(reason="Supplier outreach no longer needed")

    cancelled = service.cancel(
        project_id,
        action.id,
        request,
        actor_id="owner@example.test",
    )
    repeated = service.cancel(
        project_id,
        action.id,
        request,
        actor_id="owner@example.test",
    )

    assert cancelled.status == CommercialActionStatus.CANCELLED
    assert cancelled.history[-1].event == "cancelled"
    assert cancelled.history[-1].actor_id == "owner@example.test"
    assert cancelled.history[-1].reason == request.reason
    assert len(cancelled.history) == before_history + 1
    assert repeated.model_dump() == cancelled.model_dump()

    with pytest.raises(CommercialActionConflictError, match="cancelled"):
        service.apply(
            project_id,
            action.id,
            CommercialActionApplyRequest(
                source_run_id=run_id,
                source_session_id=session_id,
                reason="Must not execute",
            ),
            actor_id="owner@example.test",
        )


def test_not_executed_request_can_be_cancelled_but_attempting_cannot(
    tmp_path: Path,
) -> None:
    service, project_id, run_id, session_id = make_service(tmp_path)
    action = stage(service, project_id, run_id, session_id)
    approve(service, project_id, action.id)
    queued = service.apply(
        project_id,
        action.id,
        CommercialActionApplyRequest(
            source_run_id=run_id,
            source_session_id=session_id,
            reason="Queue until an adapter is available",
        ),
        actor_id="owner@example.test",
    )
    assert queued.execution_request is not None
    assert queued.execution_request.external_status == ExternalExecutionStatus.NOT_EXECUTED
    cancelled = service.cancel(
        project_id,
        action.id,
        CommercialActionCancelRequest(reason="Withdraw queued action"),
        actor_id="owner@example.test",
    )
    assert cancelled.status == CommercialActionStatus.CANCELLED
    assert cancelled.execution_request is not None

    other = stage(service, project_id, run_id, session_id)
    approve(service, project_id, other.id)
    queued = service.apply(
        project_id,
        other.id,
        CommercialActionApplyRequest(
            source_run_id=run_id,
            source_session_id=session_id,
            reason="Queue another action",
        ),
        actor_id="owner@example.test",
    )
    assert queued.execution_request is not None
    attempting_execution = queued.execution_request.model_copy(
        update={
            "external_status": ExternalExecutionStatus.ATTEMPTING,
            "attempt_count": 1,
            "attempt_started_at": NOW,
            "attempt_lease_until": NOW + timedelta(minutes=2),
        }
    )
    attempting = queued.model_copy(update={"execution_request": attempting_execution})
    with service.store.transaction(project_id):
        service.store.write_unlocked(attempting)

    with pytest.raises(CommercialActionConflictError, match="execution starts"):
        service.cancel(
            project_id,
            other.id,
            CommercialActionCancelRequest(reason="Too late to cancel safely"),
            actor_id="owner@example.test",
        )
