from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ophira_backend.auth import ProjectAccessStore
from ophira_backend.commercial_actions import (
    CommercialActionScopeError,
    CommercialActionService,
)
from ophira_backend.commercial_adapters import CommercialTargetObservation
from ophira_backend.constants import CommercialActionOperation, PrincipalRole
from ophira_backend.models import (
    CommercialActionApplyRequest,
    CommercialActionReceiptStage,
    CommercialActionReviewRequest,
    OrgPolicy,
    PrincipalContext,
    ProjectCreate,
)
from ophira_backend.policy import OrgPolicyStore
from ophira_backend.storage import FileLedger

NOW = datetime(2026, 9, 3, 9, tzinfo=UTC)


class TrustedPriceReader:
    config_hash = "a" * 64

    def read_target(
        self,
        connector_id: str,
        resource_type: str,
        resource_key: str,
    ) -> CommercialTargetObservation:
        return CommercialTargetObservation(
            operation=CommercialActionOperation.CHANGE_PRICE,
            connector_id=connector_id,
            resource_type=resource_type,
            resource_key=resource_key,
            version="etag-7",
            state={"price": {"amount": 100, "currency": "USD"}},
            connector_config_hash=self.config_hash,
            canonical_source=(
                "https://commerce.example.test/api/v1/targets/"
                f"{resource_type}/{resource_key}"
            ),
        )

    def current_connector_config_hash(self, connector_id: str) -> str:
        return self.config_hash


def _principal(
    principal_id: str,
    role: PrincipalRole,
    *,
    tenant_id: str = "tenant_acme",
) -> PrincipalContext:
    return PrincipalContext(
        tenant_id=tenant_id,
        principal_id=principal_id,
        role=role,
        session_id=f"principal_session_{principal_id.replace('@', '_')}",
        assertion_issued_at=NOW,
        assertion_expires_at=NOW + timedelta(minutes=5),
        assertion_nonce=("nonce_" + principal_id.encode().hex() + "0" * 40)[:40],
    )


def _stage_authenticated(
    service: CommercialActionService,
    project_id: str,
    run_id: str,
    sdk_session_id: str,
):
    observed = service.observe_target(
        project_id,
        source_run_id=run_id,
        source_session_id=sdk_session_id,
        connector_id="shopify",
        resource_type="listing",
        resource_key="sku-100",
    )
    payload = CommercialActionReceiptStage.model_validate(
        {
            "source_run_id": run_id,
            "source_session_id": sdk_session_id,
            "receipt_id": observed.receipt.id,
            "changes": [{"field": "price.amount", "before": 100, "after": 95}],
            "reason": "Propose the reviewed launch discount",
            "expires_in_seconds": 3600,
        }
    )
    return service.stage_from_receipt(project_id, payload)


def _service(tmp_path: Path):
    ledger = FileLedger(tmp_path)
    access = ProjectAccessStore(tmp_path / "project-access", clock=lambda: NOW)
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
        target_reader=TrustedPriceReader(),
        access=access,
        clock=lambda: NOW,
    )
    return ledger, access, service


def test_authenticated_action_inherits_stager_and_requires_distinct_authorized_reviewer(
    tmp_path: Path,
) -> None:
    ledger, access, service = _service(tmp_path)
    owner = _principal("owner@example.com", PrincipalRole.OWNER)
    member = _principal("member@example.com", PrincipalRole.MEMBER)
    reviewer = _principal("reviewer@example.com", PrincipalRole.REVIEWER)
    project = ledger.create_project(
        ProjectCreate(name="Kettle", objective="Validate"),
        principal=owner,
    )
    access.bind_project(project.id, owner)
    access.grant(
        project.id,
        owner,
        principal_id=member.principal_id,
        role=PrincipalRole.MEMBER,
    )
    access.grant(
        project.id,
        owner,
        principal_id=reviewer.principal_id,
        role=PrincipalRole.REVIEWER,
    )
    run = ledger.create_run(project.id, "Stage a change", principal=member)
    run = ledger.update_run(run.id, sdk_session_id="sdk_authenticated_session")

    staged = _stage_authenticated(
        service,
        project.id,
        run.id,
        "sdk_authenticated_session",
    )
    assert staged.identity_binding == "authenticated_principal"
    assert staged.tenant_id == member.tenant_id
    assert staged.staged_by == member.principal_id
    assert staged.staged_role == PrincipalRole.MEMBER
    assert staged.staged_principal_session_id == member.session_id

    review_payload = CommercialActionReviewRequest(
        decision="approve",
        reason="Verified by a separate reviewer",
        valid_for_seconds=900,
    )
    with pytest.raises(CommercialActionScopeError, match="PrincipalContext"):
        service.review(
            project.id,
            staged.id,
            review_payload,
            actor_id="reviewer@example.com",
        )
    with pytest.raises(CommercialActionScopeError, match="role"):
        service.review(
            project.id,
            staged.id,
            review_payload,
            principal=_principal("reviewer@example.com", PrincipalRole.ADMIN),
        )
    with pytest.raises(CommercialActionScopeError, match="tenant"):
        service.review(
            project.id,
            staged.id,
            review_payload,
            principal=_principal(
                "reviewer@example.com",
                PrincipalRole.REVIEWER,
                tenant_id="tenant_other",
            ),
        )

    approved = service.review(
        project.id,
        staged.id,
        review_payload,
        principal=reviewer,
    )
    assert approved.review is not None
    assert approved.review.actor_id == reviewer.principal_id
    assert approved.review.tenant_id == reviewer.tenant_id
    assert approved.review.actor_role == PrincipalRole.REVIEWER
    assert approved.review.actor_session_id == reviewer.session_id

    applied = service.apply(
        project.id,
        staged.id,
        CommercialActionApplyRequest(
            source_run_id=run.id,
            source_session_id="sdk_authenticated_session",
            reason="Queue the separately reviewed change",
        ),
        principal=reviewer,
    )
    assert applied.execution_request is not None
    execution = applied.execution_request
    assert execution.requested_by == reviewer.principal_id
    assert execution.requested_tenant_id == reviewer.tenant_id
    assert execution.requested_role == PrincipalRole.REVIEWER
    assert execution.requested_session_id == reviewer.session_id


def test_stager_cannot_review_even_when_its_role_allows_both_actions(tmp_path: Path) -> None:
    ledger, access, service = _service(tmp_path)
    owner = _principal("owner@example.com", PrincipalRole.OWNER)
    project = ledger.create_project(
        ProjectCreate(name="Kettle", objective="Validate"),
        principal=owner,
    )
    access.bind_project(project.id, owner)
    run = ledger.create_run(project.id, "Stage a change", principal=owner)
    run = ledger.update_run(run.id, sdk_session_id="sdk_owner_session")
    staged = _stage_authenticated(
        service,
        project.id,
        run.id,
        "sdk_owner_session",
    )

    with pytest.raises(CommercialActionScopeError, match="staging identity"):
        service.review(
            project.id,
            staged.id,
            CommercialActionReviewRequest(
                decision="approve",
                reason="Self approval must fail",
                valid_for_seconds=900,
            ),
            principal=owner,
        )


def test_revoked_or_changed_stager_role_blocks_later_review(tmp_path: Path) -> None:
    ledger, access, service = _service(tmp_path)
    owner = _principal("owner@example.com", PrincipalRole.OWNER)
    member = _principal("member@example.com", PrincipalRole.MEMBER)
    reviewer = _principal("reviewer@example.com", PrincipalRole.REVIEWER)
    project = ledger.create_project(
        ProjectCreate(name="Kettle", objective="Validate"),
        principal=owner,
    )
    access.bind_project(project.id, owner)
    for principal in (member, reviewer):
        access.grant(
            project.id,
            owner,
            principal_id=principal.principal_id,
            role=principal.role,
        )
    run = ledger.create_run(project.id, "Stage a change", principal=member)
    run = ledger.update_run(run.id, sdk_session_id="sdk_member_session")
    staged = _stage_authenticated(
        service,
        project.id,
        run.id,
        "sdk_member_session",
    )
    access.revoke(project.id, owner, principal_id=member.principal_id)

    with pytest.raises(CommercialActionScopeError, match="not a project member"):
        service.review(
            project.id,
            staged.id,
            CommercialActionReviewRequest(
                decision="approve",
                reason="Must revalidate the staging principal",
                valid_for_seconds=900,
            ),
            principal=reviewer,
        )
