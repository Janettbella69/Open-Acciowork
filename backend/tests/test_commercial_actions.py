from __future__ import annotations

import multiprocessing
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from ophira_backend.commercial_actions import (
    CommercialActionConflictError,
    CommercialActionIntegrityError,
    CommercialActionPolicyError,
    CommercialActionScopeError,
    CommercialActionService,
)
from ophira_backend.config import Settings
from ophira_backend.constants import CommercialActionOperation, CommercialActionStatus
from ophira_backend.main import create_app
from ophira_backend.models import (
    CommercialActionApplyRequest,
    CommercialActionReviewRequest,
    CommercialActionStage,
    CommercialFieldChange,
    CommercialNumericLimit,
    OrgPolicy,
    ProjectCreate,
)
from ophira_backend.policy import OrgPolicyStore
from ophira_backend.storage import FileLedger


class FakeRuntime:
    async def run(self, run) -> None:
        return None

    async def cancel(self, run_id: str) -> None:
        return None


def local_settings(data_dir: Path) -> Settings:
    return Settings(data_dir=data_dir)


def apply_from_process(
    data_dir: str,
    project_id: str,
    action_id: str,
    run_id: str,
    session_id: str,
    actor_id: str,
    start_event,
    result_queue,
) -> None:
    """Exercise the same transaction from an independent process/store instance."""

    ledger = FileLedger(Path(data_dir))
    service = CommercialActionService(
        Path(data_dir) / "commercial-actions",
        ledger=ledger,
        policy=OrgPolicyStore(Path(data_dir) / "org" / "policy.json"),
    )
    start_event.wait(timeout=5)
    try:
        result = service.apply(
            project_id,
            action_id,
            CommercialActionApplyRequest(
                source_run_id=run_id,
                source_session_id=session_id,
                reason="Cross-process execution request",
            ),
            actor_id=actor_id,
        )
    except Exception as exc:  # pragma: no cover - surfaced to the parent assertion
        result_queue.put(("error", type(exc).__name__, str(exc)))
        return
    assert result.execution_request is not None
    result_queue.put(("ok", result.execution_request.id, len(result.history)))


def create_source(ledger: FileLedger, *, name: str = "Kettle") -> tuple[str, str, str]:
    project = ledger.create_project(ProjectCreate(name=name, objective="Validate demand"))
    run = ledger.create_run(project.id, "Draft a price change")
    session_id = f"sdk_session_{name.casefold()}"
    ledger.update_run(run.id, sdk_session_id=session_id)
    return project.id, run.id, session_id


def stage_json(run_id: str, session_id: str, *, after: float = 95) -> dict:
    return {
        "source_run_id": run_id,
        "source_session_id": session_id,
        "operation": "change_price",
        "target": {
            "connector_id": "shopify",
            "resource_type": "listing",
            "resource_key": "sku-100",
            "expected_version": "etag-7",
        },
        "observed_state": {
            "price": {"amount": 100, "currency": "USD"},
            "inventory": 20,
        },
        "changes": [{"field": "price.amount", "before": 100, "after": after}],
        "reason": "Apply the user-approved launch discount",
        "expires_in_seconds": 3600,
    }


def stage_action(app, project_id: str, run_id: str, session_id: str, *, after: float = 95):
    return app.state.commercial_actions.stage(
        project_id,
        CommercialActionStage.model_validate(stage_json(run_id, session_id, after=after)),
    )


def allow_price_actions(app, *, maximum: float = 150) -> None:
    app.state.policy.save(
        OrgPolicy(
            commercial_actions_enabled=True,
            allowed_commercial_operations=[CommercialActionOperation.CHANGE_PRICE],
            allowed_commercial_connector_ids=["shopify"],
            commercial_numeric_limits=[
                CommercialNumericLimit(field="price.amount", minimum=50, maximum=maximum)
            ],
        )
    )


async def stage_and_approve(
    app,
    client: AsyncClient,
    project_id: str,
    run_id: str,
    session_id: str,
) -> dict:
    action = stage_action(app, project_id, run_id, session_id)
    reviewed = await client.post(
        f"/api/projects/{project_id}/commercial-actions/{action.id}/review",
        json={
            "decision": "approve",
            "reason": "Discount is within the approved launch plan",
            "valid_for_seconds": 900,
        },
    )
    assert reviewed.status_code == 200
    return reviewed.json()


@pytest.mark.asyncio
async def test_full_lifecycle_is_human_gated_idempotent_and_truthful(tmp_path: Path) -> None:
    app = create_app(local_settings(tmp_path), runtime=FakeRuntime())
    project_id, run_id, session_id = create_source(app.state.ledger)
    allow_price_actions(app)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        injected = stage_json(run_id, session_id)
        injected["status"] = "approved"
        with pytest.raises(ValueError):
            CommercialActionStage.model_validate(injected)

        public_stage = await client.post(
            f"/api/projects/{project_id}/commercial-actions",
            json=stage_json(run_id, session_id),
        )
        assert public_stage.status_code == 405

        action = stage_action(app, project_id, run_id, session_id).model_dump(mode="json")
        assert action["id"].startswith("action_")
        assert len(action["id"]) > 40
        assert action["status"] == "staged"
        assert action["review"] is None
        assert action["execution_request"] is None

        approved = await client.post(
            f"/api/projects/{project_id}/commercial-actions/{action['id']}/review",
            json={
                "decision": "approve",
                "reason": "Launch discount confirmed in the control plane",
                "valid_for_seconds": 900,
            },
        )
        assert approved.status_code == 200
        assert approved.json()["status"] == "approved"
        # local_only is one loopback operator. Legacy unsigned identity/approval
        # headers are ignored and can never select a reviewer identity.
        assert approved.json()["review"]["actor_id"] == "local-operator"
        assert approved.json()["review"]["reason"]
        assert approved.json()["review"]["decided_at"]
        assert approved.json()["review"]["valid_until"]

        duplicate_review = await client.post(
            f"/api/projects/{project_id}/commercial-actions/{action['id']}/review",
            json={"decision": "approve", "reason": "Again", "valid_for_seconds": 900},
        )
        assert duplicate_review.status_code == 409

        apply_body = {
            "reason": "Queue the approved change for a future commerce adapter",
        }
        applied = await client.post(
            f"/api/projects/{project_id}/commercial-actions/{action['id']}/apply",
            json=apply_body,
        )
        assert applied.status_code == 202
        result = applied.json()
        assert result["status"] == "execution_requested"
        execution = result["execution_request"]
        assert execution["external_status"] == "not_executed"
        assert execution["adapter_required"] is True
        assert execution["projected_state"]["price"]["amount"] == 95
        assert execution["expected_target_version"] == "etag-7"
        assert "external system" in " ".join(execution["adapter_preconditions"])
        assert len(result["history"]) == 3

        repeated = await client.post(
            f"/api/projects/{project_id}/commercial-actions/{action['id']}/apply",
            json=apply_body,
        )
        assert repeated.status_code == 202
        assert repeated.json() == result

        listed = await client.get(f"/api/projects/{project_id}/commercial-actions")
        fetched = await client.get(
            f"/api/projects/{project_id}/commercial-actions/{action['id']}"
        )
        assert listed.status_code == fetched.status_code == 200
        assert [item["id"] for item in listed.json()] == [action["id"]]
        assert fetched.json() == result

    restarted = create_app(local_settings(tmp_path), runtime=FakeRuntime())
    persisted = restarted.state.commercial_actions.get(project_id, action["id"])
    assert persisted.execution_request is not None
    assert persisted.execution_request.external_status == "not_executed"


@pytest.mark.asyncio
async def test_local_only_review_uses_the_explicit_loopback_operator(tmp_path: Path) -> None:
    app = create_app(Settings(data_dir=tmp_path), runtime=FakeRuntime())
    project_id, run_id, session_id = create_source(app.state.ledger)
    allow_price_actions(app)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        staged = stage_action(app, project_id, run_id, session_id)
        response = await client.post(
            f"/api/projects/{project_id}/commercial-actions/{staged.id}/review",
            json={
                "decision": "approve",
                "reason": "No gateway is configured",
                "valid_for_seconds": 900,
            },
        )

    assert response.status_code == 200
    assert response.json()["review"]["actor_id"] == "local-operator"


@pytest.mark.asyncio
async def test_forged_cross_project_and_cross_session_ids_fail_closed(tmp_path: Path) -> None:
    app = create_app(local_settings(tmp_path), runtime=FakeRuntime())
    project_id, run_id, session_id = create_source(app.state.ledger, name="Kettle")
    other_project, other_run, other_session = create_source(app.state.ledger, name="Lamp")
    allow_price_actions(app)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        action_id = stage_action(app, project_id, run_id, session_id).id

        forged = await client.get(
            f"/api/projects/{project_id}/commercial-actions/action_forged"
        )
        overlong = await client.get(
            f"/api/projects/{project_id}/commercial-actions/action_{'a' * 1_000}"
        )
        cross_project = await client.get(
            f"/api/projects/{other_project}/commercial-actions/{action_id}"
        )
        assert forged.status_code == overlong.status_code == cross_project.status_code == 404

        with pytest.raises(CommercialActionScopeError):
            stage_action(app, project_id, other_run, other_session)
        with pytest.raises(CommercialActionScopeError):
            stage_action(app, project_id, run_id, "sdk_session_forged")

        review = await client.post(
            f"/api/projects/{project_id}/commercial-actions/{action_id}/review",
            json={"decision": "approve", "reason": "Confirmed", "valid_for_seconds": 900},
        )
        assert review.status_code == 200
        cross_source_apply = await client.post(
            f"/api/projects/{project_id}/commercial-actions/{action_id}/apply",
            json={
                "source_run_id": other_run,
                "source_session_id": other_session,
                "reason": "Try the wrong source",
            },
        )
        assert cross_source_apply.status_code == 422


@pytest.mark.asyncio
async def test_rejected_action_can_never_be_applied(tmp_path: Path) -> None:
    app = create_app(local_settings(tmp_path), runtime=FakeRuntime())
    project_id, run_id, session_id = create_source(app.state.ledger)
    allow_price_actions(app)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        action_id = stage_action(app, project_id, run_id, session_id).id
        rejected = await client.post(
            f"/api/projects/{project_id}/commercial-actions/{action_id}/review",
            json={"decision": "reject", "reason": "Margin is too low"},
        )
        assert rejected.status_code == 200
        assert rejected.json()["status"] == "rejected"

        applied = await client.post(
            f"/api/projects/{project_id}/commercial-actions/{action_id}/apply",
            json={
                "reason": "This must be denied",
            },
        )
        assert applied.status_code == 409


@pytest.mark.asyncio
async def test_apply_rechecks_latest_policy_numeric_limits_and_project_revision(
    tmp_path: Path,
) -> None:
    app = create_app(local_settings(tmp_path), runtime=FakeRuntime())
    project_id, run_id, session_id = create_source(app.state.ledger)
    allow_price_actions(app)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        approved = await stage_and_approve(app, client, project_id, run_id, session_id)
        action_id = approved["id"]

        app.state.policy.save(OrgPolicy())
        denied_by_default = await client.post(
            f"/api/projects/{project_id}/commercial-actions/{action_id}/apply",
            json={
                "reason": "Request execution",
            },
        )
        assert denied_by_default.status_code == 403
        assert "disabled" in denied_by_default.text

        # A freshly staged action is checked against the narrowed policy at review.
        allow_price_actions(app, maximum=90)
        narrowed = stage_action(app, project_id, run_id, session_id)
        denied_by_latest_policy = await client.post(
            f"/api/projects/{project_id}/commercial-actions/{narrowed.id}/review",
            json={
                "decision": "approve",
                "reason": "Review under the narrower policy",
                "valid_for_seconds": 900,
            },
        )
        assert denied_by_latest_policy.status_code == 403
        assert "current maximum" in denied_by_latest_policy.text

        allow_price_actions(app)
        fresh = await stage_and_approve(
            app,
            client,
            project_id,
            run_id,
            session_id,
        )
        app.state.ledger.update_project(
            project_id,
            lambda project: project.constraints.append("Do not discount during the embargo"),
        )
        stale_project = await client.post(
            f"/api/projects/{project_id}/commercial-actions/{fresh['id']}/apply",
            json={
                "reason": "Request execution",
            },
        )
        assert stale_project.status_code == 409
        assert "project state changed" in stale_project.text


def test_preview_review_and_apply_bind_current_policy_versions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ophira_backend.policy as policy_module

    policy_times = iter(
        [
            datetime(2026, 9, 3, 8, 0, tzinfo=UTC),
            datetime(2026, 9, 3, 8, 5, tzinfo=UTC),
            datetime(2026, 9, 3, 8, 10, tzinfo=UTC),
            datetime(2026, 9, 3, 8, 15, tzinfo=UTC),
        ]
    )
    monkeypatch.setattr(policy_module, "utc_now", lambda: next(policy_times))
    ledger = FileLedger(tmp_path)
    project_id, run_id, session_id = create_source(ledger)
    policy = OrgPolicyStore(tmp_path / "org" / "policy.json")
    allowed = OrgPolicy(
        commercial_actions_enabled=True,
        allowed_commercial_operations=[CommercialActionOperation.CHANGE_PRICE],
        allowed_commercial_connector_ids=["shopify"],
    )
    staged_policy = policy.save(allowed)
    service = CommercialActionService(
        tmp_path / "commercial-actions",
        ledger=ledger,
        policy=policy,
    )
    staged = service.stage(
        project_id,
        CommercialActionStage.model_validate(stage_json(run_id, session_id)),
    )
    assert staged.staged_policy_updated_at == staged_policy.updated_at

    policy.save(OrgPolicy())
    with pytest.raises(CommercialActionPolicyError, match="disabled"):
        service.review(
            project_id,
            staged.id,
            CommercialActionReviewRequest(
                decision="approve",
                reason="A disabled policy cannot authorize this preview",
                valid_for_seconds=900,
            ),
            actor_id="owner-1",
        )
    assert service.get(project_id, staged.id).status == CommercialActionStatus.STAGED

    review_policy = policy.save(allowed)
    approved = service.review(
        project_id,
        staged.id,
        CommercialActionReviewRequest(
            decision="approve",
            reason="Review against the restored current policy",
            valid_for_seconds=900,
        ),
        actor_id="owner-1",
    )
    assert approved.review is not None
    assert approved.review.policy_updated_at == review_policy.updated_at

    policy.save(allowed)
    with pytest.raises(CommercialActionConflictError, match="changed after approval"):
        service.apply(
            project_id,
            approved.id,
            CommercialActionApplyRequest(
                source_run_id=run_id,
                source_session_id=session_id,
                reason="Policy changed, so a new human review is required",
            ),
            actor_id="owner-1",
        )
    unchanged = service.get(project_id, approved.id)
    assert unchanged.status == CommercialActionStatus.APPROVED
    assert unchanged.execution_request is None


def test_expired_stage_and_approval_transition_fail_closed(tmp_path: Path) -> None:
    ledger = FileLedger(tmp_path)
    project_id, run_id, session_id = create_source(ledger)
    policy = OrgPolicyStore(tmp_path / "org" / "policy.json")
    policy.save(
        OrgPolicy(
            commercial_actions_enabled=True,
            allowed_commercial_operations=[CommercialActionOperation.CHANGE_PRICE],
            allowed_commercial_connector_ids=["shopify"],
        )
    )
    current = [datetime(2026, 9, 2, 12, tzinfo=UTC)]
    service = CommercialActionService(
        tmp_path / "commercial-actions",
        ledger=ledger,
        policy=policy,
        clock=lambda: current[0],
    )
    payload = CommercialActionStage.model_validate(stage_json(run_id, session_id))

    staged = service.stage(project_id, payload)
    current[0] += timedelta(hours=2)
    with pytest.raises(CommercialActionConflictError, match="expired"):
        service.review(
            project_id,
            staged.id,
            CommercialActionReviewRequest(
                decision="approve",
                reason="Too late",
                valid_for_seconds=60,
            ),
            actor_id="owner-1",
        )
    assert service.get(project_id, staged.id).status == CommercialActionStatus.EXPIRED

    current[0] = datetime(2026, 9, 2, 14, tzinfo=UTC)
    staged = service.stage(project_id, payload)
    approved = service.review(
        project_id,
        staged.id,
        CommercialActionReviewRequest(
            decision="approve",
            reason="Short approval",
            valid_for_seconds=60,
        ),
        actor_id="owner-1",
    )
    current[0] += timedelta(seconds=60)
    with pytest.raises(CommercialActionConflictError, match="approval has expired"):
        service.apply(
            project_id,
            approved.id,
            CommercialActionApplyRequest(
                source_run_id=run_id,
                source_session_id=session_id,
                reason="Too late",
            ),
            actor_id="owner-1",
        )
    assert service.get(project_id, approved.id).status == CommercialActionStatus.EXPIRED


def test_two_services_serialize_apply_and_preserve_audit(tmp_path: Path) -> None:
    first_ledger = FileLedger(tmp_path)
    project_id, run_id, session_id = create_source(first_ledger)
    first_policy = OrgPolicyStore(tmp_path / "org" / "policy.json")
    first_policy.save(
        OrgPolicy(
            commercial_actions_enabled=True,
            allowed_commercial_operations=[CommercialActionOperation.CHANGE_PRICE],
            allowed_commercial_connector_ids=["shopify"],
        )
    )
    first_service = CommercialActionService(
        tmp_path / "commercial-actions",
        ledger=first_ledger,
        policy=first_policy,
    )
    second_service = CommercialActionService(
        tmp_path / "commercial-actions",
        ledger=FileLedger(tmp_path),
        policy=OrgPolicyStore(tmp_path / "org" / "policy.json"),
    )
    staged = first_service.stage(
        project_id,
        CommercialActionStage.model_validate(stage_json(run_id, session_id)),
    )
    approved = first_service.review(
        project_id,
        staged.id,
        CommercialActionReviewRequest(
            decision="approve",
            reason="Approved",
            valid_for_seconds=900,
        ),
        actor_id="owner-1",
    )
    request = CommercialActionApplyRequest(
        source_run_id=run_id,
        source_session_id=session_id,
        reason="Queue once",
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                service.apply,
                project_id,
                approved.id,
                request,
                actor_id=f"owner-{index + 1}",
            )
            for index, service in enumerate((first_service, second_service))
        ]
        results = [future.result(timeout=2) for future in futures]

    execution_ids = {result.execution_request.id for result in results if result.execution_request}
    assert len(execution_ids) == 1
    assert results[0].model_dump() == results[1].model_dump()
    assert len(results[0].history) == 3
    persisted = second_service.get(project_id, approved.id)
    assert [entry.event for entry in persisted.history] == [
        "staged",
        "approved",
        "execution_requested",
    ]
    assert list((tmp_path / "commercial-actions").rglob("*.tmp")) == []


def test_cross_process_apply_is_atomic_and_idempotent(tmp_path: Path) -> None:
    try:
        context = multiprocessing.get_context("fork")
    except ValueError:  # pragma: no cover - project targets Unix for flock
        pytest.skip("cross-process flock test requires fork support")

    ledger = FileLedger(tmp_path)
    project_id, run_id, session_id = create_source(ledger)
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
    )
    staged = service.stage(
        project_id,
        CommercialActionStage.model_validate(stage_json(run_id, session_id)),
    )
    approved = service.review(
        project_id,
        staged.id,
        CommercialActionReviewRequest(
            decision="approve",
            reason="Approved before the process race",
            valid_for_seconds=900,
        ),
        actor_id="owner-1",
    )

    start_event = context.Event()
    result_queue = context.Queue()
    processes = [
        context.Process(
            target=apply_from_process,
            args=(
                str(tmp_path),
                project_id,
                approved.id,
                run_id,
                session_id,
                f"owner-{index + 1}",
                start_event,
                result_queue,
            ),
        )
        for index in range(2)
    ]
    for process in processes:
        process.start()
    start_event.set()
    for process in processes:
        process.join(timeout=10)
        if process.is_alive():
            process.terminate()
            process.join(timeout=2)
            pytest.fail("commercial action process deadlocked")
        assert process.exitcode == 0

    results = [result_queue.get(timeout=2) for _ in processes]
    assert all(result[0] == "ok" for result in results), results
    assert len({result[1] for result in results}) == 1
    assert {result[2] for result in results} == {3}

    persisted = service.get(project_id, approved.id)
    assert persisted.execution_request is not None
    assert persisted.execution_request.id == results[0][1]
    assert [entry.event for entry in persisted.history] == [
        "staged",
        "approved",
        "execution_requested",
    ]
    assert list((tmp_path / "commercial-actions").rglob("*.tmp")) == []


def test_stage_rejects_inconsistent_or_duplicate_structured_changes(tmp_path: Path) -> None:
    ledger = FileLedger(tmp_path)
    project_id, run_id, session_id = create_source(ledger)
    service = CommercialActionService(
        tmp_path / "commercial-actions",
        ledger=ledger,
        policy=OrgPolicyStore(tmp_path / "org" / "policy.json"),
    )
    payload = CommercialActionStage.model_validate(stage_json(run_id, session_id))
    inconsistent = payload.model_copy(
        update={
            "changes": [
                CommercialFieldChange(field="price.amount", before=101, after=95),
            ]
        }
    )
    with pytest.raises(CommercialActionConflictError, match="does not match before"):
        service.stage(project_id, inconsistent)

    duplicate = stage_json(run_id, session_id)
    duplicate["changes"].append(
        {"field": "price.amount", "before": 100, "after": 90}
    )
    with pytest.raises(ValueError, match="same field twice"):
        CommercialActionStage.model_validate(duplicate)


@pytest.mark.parametrize(
    ("blocked_field", "changed_field", "before", "after"),
    [
        (
            "pricing.cost",
            "pricing",
            {"cost": 10, "costBasis": 11, "retail": 20},
            {"cost": 10, "costBasis": 11, "retail": 21},
        ),
        ("pricing", "pricing.cost", 10, 9),
    ],
    ids=["blocked-child-denies-parent-replacement", "blocked-parent-denies-child"],
)
def test_forbidden_commercial_fields_block_both_hierarchy_directions(
    tmp_path: Path,
    blocked_field: str,
    changed_field: str,
    before: object,
    after: object,
) -> None:
    ledger = FileLedger(tmp_path)
    project_id, run_id, session_id = create_source(ledger)
    policy = OrgPolicyStore(tmp_path / "org" / "policy.json")
    policy.save(
        OrgPolicy(
            commercial_actions_enabled=True,
            allowed_commercial_operations=[CommercialActionOperation.CHANGE_PRICE],
            allowed_commercial_connector_ids=["shopify"],
            commercial_forbidden_fields=[blocked_field],
        )
    )
    service = CommercialActionService(
        tmp_path / "commercial-actions",
        ledger=ledger,
        policy=policy,
    )
    raw = stage_json(run_id, session_id)
    raw["observed_state"] = {
        "pricing": {"cost": 10, "costBasis": 11, "retail": 20}
    }
    raw["changes"] = [{"field": changed_field, "before": before, "after": after}]
    with pytest.raises(CommercialActionPolicyError, match="forbidden"):
        service.review(
            project_id,
            service.stage(
                project_id,
                CommercialActionStage.model_validate(raw),
            ).id,
            CommercialActionReviewRequest(
                decision="approve",
                reason="Policy must reject overlapping paths before approval",
                valid_for_seconds=900,
            ),
            actor_id="owner-1",
        )


def test_forbidden_commercial_field_does_not_match_adjacent_prefix(tmp_path: Path) -> None:
    ledger = FileLedger(tmp_path)
    project_id, run_id, session_id = create_source(ledger)
    policy = OrgPolicyStore(tmp_path / "org" / "policy.json")
    policy.save(
        OrgPolicy(
            commercial_actions_enabled=True,
            allowed_commercial_operations=[CommercialActionOperation.CHANGE_PRICE],
            allowed_commercial_connector_ids=["shopify"],
            commercial_forbidden_fields=["pricing.cost"],
        )
    )
    service = CommercialActionService(
        tmp_path / "commercial-actions",
        ledger=ledger,
        policy=policy,
    )
    raw = stage_json(run_id, session_id)
    raw["observed_state"] = {
        "pricing": {"cost": 10, "costBasis": 11, "retail": 20}
    }
    raw["changes"] = [
        {"field": "pricing.costBasis", "before": 11, "after": 12}
    ]
    staged = service.stage(project_id, CommercialActionStage.model_validate(raw))
    approved = service.review(
        project_id,
        staged.id,
        CommercialActionReviewRequest(
            decision="approve",
            reason="Adjacent field is allowed",
            valid_for_seconds=900,
        ),
        actor_id="owner-1",
    )
    applied = service.apply(
        project_id,
        approved.id,
        CommercialActionApplyRequest(
            source_run_id=run_id,
            source_session_id=session_id,
            reason="Queue the non-overlapping field",
        ),
        actor_id="owner-1",
    )

    assert applied.execution_request is not None
    assert applied.execution_request.projected_state["pricing"]["costBasis"] == 12


def test_org_policy_commercial_gate_is_explicit_and_default_deny() -> None:
    policy = OrgPolicy()
    assert policy.commercial_actions_enabled is False
    assert policy.allowed_commercial_operations == []
    assert policy.allowed_commercial_connector_ids == []

    with pytest.raises(ValueError, match="duplicate fields"):
        OrgPolicy(
            commercial_numeric_limits=[
                CommercialNumericLimit(field="price.amount", maximum=100),
                CommercialNumericLimit(field="price.amount", minimum=1),
            ]
        )

    with pytest.raises(ValueError):
        OrgPolicy(allowed_commercial_connector_ids=["x" * 161])
    with pytest.raises(ValueError):
        OrgPolicy(commercial_forbidden_fields=[f"field{index}" for index in range(101)])


def test_commercial_store_normalizes_directory_and_temporary_io_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = FileLedger(tmp_path)
    project_id, run_id, session_id = create_source(ledger)
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
    )
    original_mkdir = Path.mkdir

    def denied_project_directory(path: Path, *args, **kwargs) -> None:
        if path == service.store.root / project_id:
            raise PermissionError("fixture denied")
        original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", denied_project_directory)
    with pytest.raises(CommercialActionIntegrityError, match="directory is unavailable"):
        service.get(project_id, "action_missing")
    monkeypatch.setattr(Path, "mkdir", original_mkdir)

    import ophira_backend.commercial_actions as commercial_module

    staged = service.stage(
        project_id,
        CommercialActionStage.model_validate(stage_json(run_id, session_id)),
    )
    original_replace = commercial_module.os.replace

    def denied_replace(source, destination) -> None:
        raise PermissionError("fixture denied")

    monkeypatch.setattr(commercial_module.os, "replace", denied_replace)
    with pytest.raises(CommercialActionIntegrityError, match="could not be replaced"):
        service.review(
            project_id,
            staged.id,
            CommercialActionReviewRequest(
                decision="approve",
                reason="Exercise the replacement failure",
                valid_for_seconds=900,
            ),
            actor_id="owner-1",
        )
    monkeypatch.setattr(commercial_module.os, "replace", original_replace)

    original_open = commercial_module.os.open

    def denied_temporary(path, flags, mode=0o777, **kwargs):
        if str(path).endswith(".tmp"):
            raise PermissionError("fixture denied")
        return original_open(path, flags, mode, **kwargs)

    monkeypatch.setattr(commercial_module.os, "open", denied_temporary)
    with pytest.raises(CommercialActionIntegrityError, match="temporary file"):
        service.stage(
            project_id,
            CommercialActionStage.model_validate(stage_json(run_id, session_id)),
        )


@pytest.mark.parametrize(
    ("observed", "declared_before"),
    [
        (1, True),
        ({"available": 1}, {"available": True}),
        ([{"available": 0}], [{"available": False}]),
    ],
    ids=["scalar", "nested-object", "nested-list"],
)
def test_commercial_before_state_uses_strict_json_type_equality(
    tmp_path: Path,
    observed: object,
    declared_before: object,
) -> None:
    ledger = FileLedger(tmp_path)
    project_id, run_id, session_id = create_source(ledger)
    service = CommercialActionService(
        tmp_path / "commercial-actions",
        ledger=ledger,
        policy=OrgPolicyStore(tmp_path / "org" / "policy.json"),
    )
    raw = stage_json(run_id, session_id)
    raw["observed_state"] = {"inventory": observed}
    raw["changes"] = [
        {"field": "inventory", "before": declared_before, "after": "reviewed"}
    ]

    with pytest.raises(CommercialActionConflictError, match="does not match before"):
        service.stage(project_id, CommercialActionStage.model_validate(raw))


def test_commercial_lock_release_failure_still_closes_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ophira_backend.commercial_actions as commercial_module

    ledger = FileLedger(tmp_path)
    project_id, _run_id, _session_id = create_source(ledger)
    service = CommercialActionService(
        tmp_path / "commercial-actions",
        ledger=ledger,
        policy=OrgPolicyStore(tmp_path / "org" / "policy.json"),
    )
    original_flock = commercial_module.fcntl.flock
    released_descriptors: list[int] = []

    def fail_unlock(descriptor: int, operation: int) -> None:
        if operation == commercial_module.fcntl.LOCK_UN:
            released_descriptors.append(descriptor)
            raise PermissionError("fixture denied")
        original_flock(descriptor, operation)

    monkeypatch.setattr(commercial_module.fcntl, "flock", fail_unlock)
    assert service.list(project_id) == []

    assert len(released_descriptors) == 1
    with pytest.raises(OSError):
        commercial_module.os.fstat(released_descriptors[0])


@pytest.mark.parametrize(
    "invalid_value",
    [float("inf"), float("-inf"), float("nan"), {"nested": [float("inf")]}],
    ids=["positive-infinity", "negative-infinity", "nan", "nested"],
)
def test_commercial_models_reject_non_finite_json_numbers(
    invalid_value: object,
) -> None:
    raw = stage_json("run_1", "session_1")
    raw["observed_state"] = {"price": invalid_value}
    with pytest.raises(ValueError, match="finite JSON numbers"):
        CommercialActionStage.model_validate(raw)

    raw = stage_json("run_1", "session_1")
    raw["changes"] = [
        {"field": "price.amount", "before": invalid_value, "after": 1}
    ]
    with pytest.raises(ValueError, match="finite JSON numbers"):
        CommercialActionStage.model_validate(raw)


def test_commercial_store_rejects_non_finite_numbers_in_persisted_json(
    tmp_path: Path,
) -> None:
    ledger = FileLedger(tmp_path)
    project_id, run_id, session_id = create_source(ledger)
    service = CommercialActionService(
        tmp_path / "commercial-actions",
        ledger=ledger,
        policy=OrgPolicyStore(tmp_path / "org" / "policy.json"),
    )
    staged = service.stage(
        project_id,
        CommercialActionStage.model_validate(stage_json(run_id, session_id)),
    )
    path = service.store._path(project_id, staged.id)
    payload = path.read_text(encoding="utf-8")
    assert '"inventory": 20' in payload
    path.write_text(
        payload.replace('"inventory": 20', '"inventory": NaN'),
        encoding="utf-8",
    )

    with pytest.raises(CommercialActionIntegrityError, match="integrity validation"):
        service.get(project_id, staged.id)
