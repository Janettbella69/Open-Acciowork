"""Project archives reject mixed provenance before disclosing durable records."""

import io
import json
import zipfile
from datetime import timedelta

import pytest
from test_project_exports import unpack
from test_skill_proposals import approve, write_candidate

from ophira_backend.auth import ProjectAccessStore
from ophira_backend.commercial_actions import CommercialActionService
from ophira_backend.config import Settings
from ophira_backend.constants import EventType, EvidenceSourceType, PrincipalRole, RunStatus
from ophira_backend.models import (
    CommercialActionStage,
    EventActor,
    EventEnvelope,
    PrincipalContext,
    ProjectCreate,
    utc_now,
)
from ophira_backend.policy import OrgPolicyStore
from ophira_backend.project_exports import ProjectExports
from ophira_backend.skill_proposal_models import SkillProposalReviewRequest
from ophira_backend.skill_proposals import SkillProposalService
from ophira_backend.skills import SkillRegistry
from ophira_backend.storage import FileLedger


@pytest.fixture
def proposal(tmp_path):
    ledger = FileLedger(tmp_path / "data")
    access = ProjectAccessStore(ledger.root / "auth/projects")
    export = ProjectExports(ledger, access, Settings(_env_file=None, data_dir=ledger.root))
    project = ledger.create_project(ProjectCreate(name="Export", objective="Retain our work"))
    related = ledger.create_run(project.id, "Completed project-specific research")
    related = ledger.update_run(
        related.id, started_at=utc_now(), finished_at=utc_now(), status=RunStatus.COMPLETED,
        sdk_session_id="related-sdk-fixture",
    )
    run = ledger.create_run(project.id, "Create reusable workflow")
    run = ledger.update_run(run.id, started_at=utc_now(), sdk_session_id="source-sdk-fixture")
    plugin = tmp_path / "plugin"
    (plugin / ".claude-plugin").mkdir(parents=True)
    (plugin / ".claude-plugin/plugin.json").write_text('{"name":"ophira-core"}')
    registry = SkillRegistry(plugin, tmp_path / "registry")
    proposals = SkillProposalService(ledger, registry)
    artifact, _ = write_candidate(ledger, project, run, related_run_ids=[related.id])
    item = proposals.submit_artifact(run.id, artifact.id)
    ledger.update_run(run.id, status=RunStatus.COMPLETED, finished_at=utc_now())
    return export, project, item, related


def test_frozen_proposal_roundtrip_preserves_bytes_and_project_only_provenance(proposal):
    export, project, item, related = proposal
    _, files = unpack(export, project.id)
    prefix = f"skill-proposals/{item.id}"
    record = json.loads(files[prefix + "/record.json"])
    assert record["source_sdk_session_id"] == "[REDACTED]"
    assert record["manifest"]["related_run_ids"] == [related.id]
    assert record["source_runs"][0]["prompt_excerpt"] == related.prompt
    assert record["content_hash"] == item.content_hash
    frozen = export.ledger.root / prefix
    assert files[prefix + "/manifest.json"] == (frozen / "manifest.json").read_bytes()
    for entry in item.files:
        assert files[prefix + "/package/" + entry.path] == (
            frozen / "package" / entry.path
        ).read_bytes()


@pytest.mark.parametrize("change", [
    "foreign_source_run", "foreign_related_run", "missing_related_run", "related_start",
    "manifest_reason", "content_hash", "scan_hash", "duplicate_file",
])
def test_inconsistent_proposal_metadata_is_not_exportable(proposal, change):
    export, project, item, _related = proposal
    ledger = export.ledger
    path = ledger.root / "skill-proposals" / item.id / "record.json"
    value = json.loads(path.read_bytes())
    other = ledger.create_project(ProjectCreate(name="Private", objective="Unrelated project"))
    foreign = ledger.create_run(other.id, "foreign-project-prompt-canary")
    foreign = ledger.update_run(
        foreign.id, started_at=utc_now(), finished_at=utc_now(), status=RunStatus.COMPLETED,
        sdk_session_id="foreign-sdk-session",
    )
    if change == "foreign_source_run":
        value["source_run_id"] = foreign.id
    elif change == "foreign_related_run":
        value["source_runs"][0].update({
            "id": foreign.id, "prompt_excerpt": foreign.prompt,
            "started_at": foreign.started_at.isoformat(),
        })
    elif change == "missing_related_run":
        value["source_runs"] = []
    elif change == "related_start":
        value["source_runs"][0]["started_at"] = (utc_now() + timedelta(days=1)).isoformat()
    elif change == "manifest_reason":
        value["manifest"]["reason"] = "Different reason from the frozen manifest"
    elif change == "content_hash":
        value["content_hash"] = "0" * 64
    elif change == "scan_hash":
        value["scan_content_hash"] = "0" * 64
    else:
        value["files"].append(value["files"][0])
    ledger._atomic_write(path, value)
    with pytest.raises(ValueError):
        export.preview(project.id, None)


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_proposal_review_and_publication_audit_roundtrip(proposal, tmp_path, decision):
    export, project, item, _related = proposal
    registry = SkillRegistry(tmp_path / "plugin", tmp_path / "registry")
    proposals = SkillProposalService(export.ledger, registry)
    if decision == "approve":
        reviewed = approve(proposals, registry, item)
    else:
        reviewed = proposals.review(item.id, SkillProposalReviewRequest(
            decision="reject", revision=item.revision, content_hash=item.content_hash,
            note="Insufficient project evidence",
        ), None)
    _, files = unpack(export, project.id)
    record = json.loads(files[f"skill-proposals/{item.id}/record.json"])
    assert record["state"] == reviewed.state
    assert record["history"][-1]["decision"] == decision
    assert record["publication_receipt"] == (
        reviewed.publication_receipt.model_dump(mode="json")
        if reviewed.publication_receipt else None
    )
    # Shared installed Skills are not traversed to export a project proposal.
    assert not any(name.startswith(("skills/", "registry/")) for name in files)


def test_proposal_with_forged_human_review_binding_is_rejected(proposal, tmp_path):
    export, project, item, _related = proposal
    registry = SkillRegistry(tmp_path / "plugin", tmp_path / "registry")
    proposals = SkillProposalService(export.ledger, registry)
    approve(proposals, registry, item)
    path = export.ledger.root / "skill-proposals" / item.id / "record.json"
    record = json.loads(path.read_bytes())
    record["review"]["note"] = "Forged different reviewer decision"
    record["history"][-1]["note"] = record["review"]["note"]
    export.ledger._atomic_write(path, record)
    with pytest.raises(ValueError, match="review binding"):
        export.preview(project.id, None)


@pytest.fixture
def authenticated_receipt(tmp_path):
    now = utc_now()
    principal = PrincipalContext(
        tenant_id="export-tenant", principal_id="project-owner", role=PrincipalRole.OWNER,
        session_id="private-owner-login-fixture",
        assertion_issued_at=now, assertion_expires_at=now + timedelta(minutes=5),
        assertion_nonce="fixture_nonce_12345678901234567890",
    )
    ledger = FileLedger(tmp_path / "data")
    access = ProjectAccessStore(ledger.root / "auth/projects")
    project = ledger.create_project(
        ProjectCreate(name="Private archive", objective="Keep evidence"), principal=principal,
    )
    access.bind_project(project.id, principal)
    settings = Settings(
        _env_file=None, data_dir=ledger.root, auth_mode="trusted_gateway",
        trusted_gateway_auth_secret="synthetic-export-signing-key-for-tests-only",
        trusted_gateway_tenant_id=principal.tenant_id,
    )
    export = ProjectExports(ledger, access, settings)
    run = ledger.create_run(project.id, "Observe source", principal=principal)
    run = ledger.update_run(
        run.id, sdk_session_id="source-sdk-fixture", status=RunStatus.COMPLETED,
    )
    receipt = ledger.issue_tool_receipt(
        project.id, run.id, tool="WebFetch", source_type=EvidenceSourceType.WEB,
        canonical_source="https://example.com/report", payload={"fact": "Original evidence"},
        object_type="web_page_snapshot",
    )
    return export, project, principal, receipt


def test_authenticated_receipt_roundtrip_redacts_identity_handles(authenticated_receipt):
    export, project, principal, receipt = authenticated_receipt
    preview = export.preview(project.id, principal)
    archive = export.download(project.id, principal, preview.fingerprint)
    with zipfile.ZipFile(io.BytesIO(archive)) as z:
        record = json.loads(z.read(f"receipts/{receipt.id}/receipt.json"))
        assert record["principal_id"] == principal.principal_id
        assert record["principal_session_id"] == "[REDACTED]"
        assert record["session_id"] == "[REDACTED]"
        assert principal.session_id.encode() not in b"".join(z.read(name) for name in z.namelist())


@pytest.mark.parametrize("field,value", [
    ("principal_id", "another-principal"),
    ("principal_role", "admin"),
    ("principal_session_id", "another-principal-login-fixture"),
    ("tenant_id", "another-tenant"),
    ("session_id", "another-sdk-session"),
])
def test_receipt_must_match_full_source_run_identity(authenticated_receipt, field, value):
    export, project, principal, receipt = authenticated_receipt
    path = export.ledger.root / "receipts" / project.id / receipt.id / "receipt.json"
    record = json.loads(path.read_bytes())
    record[field] = value
    export.ledger._atomic_write(path, record)
    with pytest.raises(ValueError):
        export.preview(project.id, principal)


@pytest.fixture
def commercial_action(tmp_path):
    ledger = FileLedger(tmp_path / "data")
    access = ProjectAccessStore(ledger.root / "auth/projects")
    export = ProjectExports(ledger, access, Settings(_env_file=None, data_dir=ledger.root))
    project = ledger.create_project(ProjectCreate(name="Review", objective="Retain action history"))
    run = ledger.create_run(project.id, "Stage a price change for review")
    run = ledger.update_run(run.id, sdk_session_id="commercial-sdk-fixture")
    commercial = CommercialActionService(
        ledger.root / "commercial-actions", ledger=ledger,
        policy=OrgPolicyStore(ledger.root / "org/policy.json"),
    )
    record = commercial.stage(project.id, CommercialActionStage.model_validate({
        "source_run_id": run.id, "source_session_id": run.sdk_session_id,
        "operation": "change_price",
        "target": {
            "connector_id": "shopify", "resource_type": "listing",
            "resource_key": "sku-fixture", "expected_version": "etag-fixture",
        },
        "observed_state": {"price": 100},
        "changes": [{"field": "price", "before": 100, "after": 95}],
        "reason": "Reviewed discount proposal", "expires_in_seconds": 3600,
    }))
    ledger.update_run(run.id, status=RunStatus.COMPLETED)
    return export, project, record


def test_commercial_action_exports_reviewable_state_without_sdk_handles(commercial_action):
    export, project, record = commercial_action
    _, files = unpack(export, project.id)
    saved = json.loads(files[f"commercial-actions/{record.id}.json"])
    assert saved["observed_state"] == {"price": 100}
    assert saved["changes"] == [{"field": "price", "before": 100, "after": 95}]
    assert saved["source_run_id"] == record.source_run_id
    assert saved["source_session_id"] == "[REDACTED]"


@pytest.mark.parametrize("field", ["source_run_id", "source_session_id"])
def test_commercial_action_cannot_export_other_project_source(commercial_action, field):
    export, project, record = commercial_action
    ledger = export.ledger
    other = ledger.create_project(ProjectCreate(name="Other", objective="Private source"))
    run = ledger.create_run(other.id, "private-source-canary")
    ledger.update_run(run.id, sdk_session_id="another-sdk-fixture", status=RunStatus.COMPLETED)
    path = ledger.root / "commercial-actions" / project.id / "actions" / (record.id + ".json")
    value = json.loads(path.read_bytes())
    value[field] = run.id if field == "source_run_id" else "another-sdk-fixture"
    ledger._atomic_write(path, value)
    with pytest.raises(ValueError):
        export.preview(project.id, None)


@pytest.mark.parametrize("row", [{}, [], {"item": "not a memory record"}])
def test_malformed_memory_history_has_a_safe_validation_failure(commercial_action, row):
    export, project, _record = commercial_action
    path = export.ledger.root / "memory/project" / project.id / "history.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(row) + "\n")
    with pytest.raises(ValueError):
        export.preview(project.id, None)


def test_historical_run_summary_can_omit_default_sequence(commercial_action):
    export, project, record = commercial_action
    path = export.ledger.root / "runs" / record.source_run_id / "summary.json"
    value = json.loads(path.read_bytes())
    value.pop("last_sequence", None)
    export.ledger._atomic_write(path, value)
    assert export.preview(project.id, None).project_id == project.id


@pytest.mark.parametrize("event_count", [0, 1])
def test_incomplete_run_conversation_is_not_exported_as_complete(commercial_action, event_count):
    export, project, record = commercial_action
    ledger = export.ledger
    run = ledger.get_run(record.source_run_id)
    for _ in range(event_count):
        ledger.append_event(EventEnvelope(
            id="pending", sequence=1, type=EventType.ASSISTANT_MESSAGE_COMPLETED,
            project_id=project.id, run_id=run.id, trace_id=run.trace_id,
            actor=EventActor(type="assistant", name="Main"), data={"text": "Retained message"},
        ))
    path = ledger.root / "runs" / run.id / "summary.json"
    summary = json.loads(path.read_bytes())
    summary["last_sequence"] = event_count + 1
    ledger._atomic_write(path, summary)
    with pytest.raises(ValueError, match="history is incomplete"):
        export.preview(project.id, None)
