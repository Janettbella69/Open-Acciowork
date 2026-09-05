from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from test_auth_api import principal, request_as, trusted_settings

from ophira_backend.config import Settings
from ophira_backend.constants import (
    EventType,
    EvidenceKind,
    EvidenceStatus,
    FeedbackKind,
    PrincipalRole,
    RunStatus,
)
from ophira_backend.events import EventPublisher
from ophira_backend.feedback import FeedbackConflictError, FeedbackNotFoundError, FeedbackStore
from ophira_backend.main import create_app
from ophira_backend.models import (
    Evidence,
    FeedbackCreate,
    FeedbackItem,
    FeedbackReview,
    ProjectCreate,
)
from ophira_backend.storage import FileLedger


class FailingRuntime:
    async def run(self, _run) -> None:
        raise RuntimeError("Synthetic failure api_key=do-not-persist")

    async def cancel(self, _run_id: str) -> None:
        return None


class WaitingRuntime:
    async def run(self, _run) -> None:
        await asyncio.Event().wait()

    async def cancel(self, _run_id: str) -> None:
        return None


async def anchored_message(ledger: FileLedger):
    project = ledger.create_project(ProjectCreate(name="Synthetic", objective="Check fixture"))
    run = ledger.create_run(project.id, "Synthetic review task")
    event = await EventPublisher(ledger).publish(
        run.id,
        EventType.ASSISTANT_MESSAGE_COMPLETED,
        {"text": "Synthetic price is 25 USD."},
    )
    payload = FeedbackCreate(
        run_id=run.id,
        message_id=event.id,
        kind=FeedbackKind.CONCLUSION_WRONG,
        user_note="The synthetic fixture lists 20 USD, not 25 USD.",
    )
    return project, run, event, payload


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "conclusion_wrong", "run_id": "run_1", "user_note": "Why"},
        {"kind": "conclusion_wrong", "message_id": "event_1", "user_note": "Why"},
        {"kind": "evidence_issue", "project_id": "project_1", "user_note": "Why"},
        {"kind": "run_failure", "user_note": "Why"},
        {"kind": "run_failure", "run_id": "run_1", "user_note": "  \n "},
        {"kind": "run_failure", "run_id": "run_1", "user_note": "Why", "source": "system"},
    ],
)
def test_feedback_requires_why_and_typed_anchor(payload: dict) -> None:
    with pytest.raises(ValidationError):
        FeedbackCreate.model_validate(payload)


@pytest.mark.asyncio
async def test_message_is_resolved_from_ledger_and_client_cannot_spoof_context(tmp_path: Path):
    ledger = FileLedger(tmp_path)
    project, run, event, payload = await anchored_message(ledger)
    store = FeedbackStore(tmp_path / "feedback")
    item = store.add(payload, ledger=ledger)
    assert item.project_id == project.id
    assert item.context.trace_id == run.trace_id
    assert item.context.event_id == event.id
    assert item.context.message_text == "Synthetic price is 25 USD."
    assert item.candidate_state == "unlabeled"
    assert "label" not in item.model_dump()
    assert FeedbackStore(store.root).get(item.id) == item
    assert (store.root / f"{item.id}.json").stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValidationError):
        FeedbackCreate.model_validate({**payload.model_dump(), "context": {"trace_id": "fake"}})


@pytest.mark.asyncio
async def test_feedback_redaction_expansion_keeps_valid_note_and_evidence_bounded(tmp_path):
    ledger = FileLedger(tmp_path)
    project, run, _, payload = await anchored_message(ledger)
    store = FeedbackStore(tmp_path / "feedback")
    note = "n" * (4_000 - len(" token=x")) + " token=x"
    item = store.add(payload.model_copy(update={"user_note": note}), ledger=ledger)
    assert len(item.user_note) <= 4_000 and item.user_note.endswith("[TRUNCATED]")
    assert "token=x" not in item.user_note
    evidence = Evidence(
        kind=EvidenceKind.USER_STATEMENT, claim="Synthetic quote", source_title="Fixture",
        confidence=1, recorded_by="fixture", source_run_id=run.id,
        excerpt="e" * (8_000 - len(" token=x")) + " token=x",
        location="l" * (1_000 - len(" token=x")) + " token=x",
        freshness_note="f" * (1_000 - len(" token=x")) + " token=x",
    )
    ledger.update_project(project.id, lambda saved: saved.evidence.append(evidence))
    captured = store.add(FeedbackCreate(
        kind="evidence_issue", project_id=project.id, evidence_id=evidence.id, user_note=note,
    ), ledger=ledger)
    snapshot = captured.context.evidence
    assert len(snapshot.excerpt) <= 8_000 and snapshot.excerpt.endswith("[TRUNCATED]")
    assert len(snapshot.location) <= 1_000 and snapshot.location.endswith("[TRUNCATED]")
    assert len(snapshot.freshness_note) <= 1_000
    assert "token=x" not in captured.model_dump_json()
    assert store.get(captured.id) == captured


@pytest.mark.asyncio
async def test_anchors_reject_wrong_message_type_project_and_nonterminal_failure(tmp_path: Path):
    ledger = FileLedger(tmp_path)
    project, run, event, payload = await anchored_message(ledger)
    other_project, other_run, other_event, _ = await anchored_message(ledger)
    store = FeedbackStore(tmp_path / "feedback")
    with pytest.raises(FeedbackNotFoundError):
        store.add(payload.model_copy(update={"message_id": other_event.id}), ledger=ledger)
    with pytest.raises(ValueError, match="outside feedback project"):
        store.add(payload.model_copy(update={"project_id": other_project.id}), ledger=ledger)
    tool = await EventPublisher(ledger).publish(run.id, EventType.TOOL_COMPLETED, {"text": "tool"})
    with pytest.raises(ValueError, match="completed visible assistant"):
        store.add(payload.model_copy(update={"message_id": tool.id}), ledger=ledger)
    with pytest.raises(ValueError, match="failed or cancelled"):
        store.add(
            FeedbackCreate(kind="run_failure", run_id=run.id, user_note="still running"),
            ledger=ledger,
        )
    assert store.list() == []


@pytest.mark.asyncio
async def test_evidence_anchor_infers_source_run_and_keeps_reported_snapshot(tmp_path: Path):
    ledger = FileLedger(tmp_path)
    project, run, _, _ = await anchored_message(ledger)
    evidence = Evidence(
        kind=EvidenceKind.USER_STATEMENT,
        claim="Synthetic quote is 20 USD",
        source_title="Fixture",
        source_type="user",
        confidence=1,
        recorded_by="fixture",
        source_run_id=run.id,
    )
    ledger.update_project(project.id, lambda saved: saved.evidence.append(evidence))
    store = FeedbackStore(tmp_path / "feedback")
    payload = FeedbackCreate(
        kind="evidence_issue",
        project_id=project.id,
        evidence_id=evidence.id,
        user_note="The fixture currency is EUR, not USD.",
    )
    item = store.add(payload, ledger=ledger)
    assert item.run_id == run.id and item.context.evidence.id == evidence.id
    ledger.update_project(
        project.id, lambda saved: setattr(saved.evidence[0], "status", EvidenceStatus.REMOVED)
    )
    assert store.get(item.id).context.evidence.status == "active"
    unrelated = ledger.create_run(project.id, "Unrelated run")
    with pytest.raises(ValueError, match="different source run"):
        store.add(payload.model_copy(update={"run_id": unrelated.id}), ledger=ledger)
    with pytest.raises(FeedbackNotFoundError):
        store.add(payload.model_copy(update={"evidence_id": "ev_missing"}), ledger=ledger)


@pytest.mark.asyncio
async def test_review_is_versioned_has_history_and_never_changes_project_or_skills(tmp_path: Path):
    ledger = FileLedger(tmp_path)
    project, _, _, payload = await anchored_message(ledger)
    store = FeedbackStore(tmp_path / "feedback")
    item = store.add(payload, ledger=ledger)
    before = ledger.get_project(project.id).model_dump_json()
    reviewed = store.review(
        item.id, FeedbackReview(expected_revision=1, status="reviewed", note="Checked fixture")
    )
    assert reviewed.status == "reviewed" and reviewed.revision == 2
    assert reviewed.reviews[0].from_status == "open"
    assert reviewed.reviews[0].note == "Checked fixture"
    with pytest.raises(FeedbackConflictError, match="reload"):
        store.review(
            item.id, FeedbackReview(expected_revision=1, status="rejected", note="Stale review")
        )
    rejected = store.review(
        item.id, FeedbackReview(expected_revision=2, status="rejected", note="Fixture expected")
    )
    reopened = store.review(
        item.id, FeedbackReview(expected_revision=3, status="open", note="New information")
    )
    assert rejected.status == "rejected" and len(reopened.reviews) == 3
    assert ledger.get_project(project.id).model_dump_json() == before
    assert not (tmp_path / "skills").exists()


@pytest.mark.asyncio
async def test_concurrent_review_keeps_exactly_one_revision(tmp_path: Path):
    ledger = FileLedger(tmp_path)
    _, _, _, payload = await anchored_message(ledger)
    root = tmp_path / "feedback"
    item = FeedbackStore(root).add(payload, ledger=ledger)

    def review(_):
        try:
            return (
                FeedbackStore(root)
                .review(
                    item.id,
                    FeedbackReview(
                        expected_revision=1,
                        status="reviewed",
                        note="Independent reviewer",
                    ),
                )
                .revision
            )
        except FeedbackConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(review, range(6)))
    assert results.count(2) == 1 and results.count("conflict") == 5
    assert len(FeedbackStore(root).get(item.id).reviews) == 1


def test_fold_requires_explicit_manual_skill_change_and_l2_attestations():
    with pytest.raises(ValidationError, match="attestations"):
        FeedbackReview(expected_revision=2, status="folded_into_skill", note="Fold it")


@pytest.mark.asyncio
async def test_automatic_collection_is_idempotent_redacted_and_crash_recoverable(tmp_path: Path):
    ledger = FileLedger(tmp_path)
    _, run, _, _ = await anchored_message(ledger)
    run = ledger.update_run(run.id, status=RunStatus.FAILED, error="api_key=synthetic-secret")
    store = FeedbackStore(tmp_path / "feedback")
    first = store.capture_terminal(run, ledger.list_events(run.id))
    assert first.source == "system" and first.kind == "run_failure"
    assert first.context.event_id is None
    reviewed = store.review(
        first.id, FeedbackReview(expected_revision=1, status="reviewed", note="Reviewed snapshot")
    )
    terminal = await EventPublisher(ledger).publish(
        run.id, EventType.RUN_FAILED, {"error": run.error}
    )
    store.reconcile_terminal(ledger)
    repaired = store.get(first.id)
    assert repaired.context.event_id == terminal.id
    assert repaired.status == reviewed.status and len(repaired.reviews) == 1
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(
            pool.map(
                lambda _: FeedbackStore(store.root).capture_terminal(
                    run, ledger.list_events(run.id)
                ),
                range(12),
            )
        )
    assert len(store.list()) == 1
    assert "synthetic-secret" not in (store.root / f"{first.id}.json").read_text()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "runtime,status", [(FailingRuntime, "failed"), (WaitingRuntime, "cancelled")]
)
async def test_actual_run_manager_collects_failure_and_manual_cancel(
    tmp_path: Path, runtime, status
):
    app = create_app(Settings(_env_file=None, data_dir=tmp_path), runtime=runtime())
    ledger = app.state.ledger
    project = ledger.create_project(ProjectCreate(name="Synthetic", objective="Feedback"))
    run = await app.state.runs.create_and_start(project.id, "Synthetic task")
    if status == "cancelled":
        await asyncio.sleep(0)
        await app.state.runs.cancel(run.id)
    else:
        await asyncio.gather(*list(app.state.runs._tasks.values()))
    rows = app.state.feedback.list()
    assert len(rows) == 1 and rows[0].run_id == run.id
    assert rows[0].context.run_status.value == status
    assert rows[0].context.event_id is not None
    assert rows[0].source == "system"
    assert "do-not-persist" not in rows[0].model_dump_json()
    if status == "cancelled":
        assert "User cancelled" in rows[0].user_note


@pytest.mark.asyncio
async def test_feedback_http_create_lookup_review_and_invalid_inputs(tmp_path: Path):
    app = create_app(Settings(_env_file=None, data_dir=tmp_path), runtime=FailingRuntime())
    project, run, event, payload = await anchored_message(app.state.ledger)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        invalid = await client.post(
            "/api/feedback", json={"kind": "conclusion_wrong", "user_note": "Why"}
        )
        assert invalid.status_code == 422
        created = await client.post("/api/feedback", json=payload.model_dump(mode="json"))
        assert created.status_code == 201, created.text
        item = created.json()
        assert item["context"]["event_id"] == event.id and item["project_id"] == project.id
        path = f"/api/feedback/{item['id']}"
        assert (await client.get(path)).json()["run_id"] == run.id
        reviewed = await client.post(
            path + "/review", json={"expected_revision": 1, "status": "reviewed", "note": "Checked"}
        )
        assert reviewed.status_code == 200 and reviewed.json()["revision"] == 2
        conflict = await client.post(
            path + "/review",
            json={"expected_revision": 1, "status": "rejected", "note": "Old form"},
        )
        assert conflict.status_code == 409
        listed = await client.get(
            "/api/feedback", params={"project_id": project.id, "status": "reviewed"}
        )
        assert [row["id"] for row in listed.json()] == [item["id"]]
        assert (await client.get("/api/feedback?limit=0")).status_code == 422
        missing = await client.post(
            "/api/feedback", json={**payload.model_dump(mode="json"), "message_id": "missing"}
        )
        assert missing.status_code == 404


def test_legacy_feedback_is_preserved_but_cannot_gain_fake_review_trace(tmp_path: Path):
    store = FeedbackStore(tmp_path)
    legacy = FeedbackItem(kind="conclusion_wrong", user_note="Historical unanchored report")
    (tmp_path / f"{legacy.id}.json").write_text(legacy.model_dump_json())
    assert store.get(legacy.id).context is None
    with pytest.raises(FeedbackConflictError, match="no trace anchor"):
        store.review(
            legacy.id, FeedbackReview(expected_revision=1, status="reviewed", note="Cannot prove")
        )
    assert json.loads((tmp_path / f"{legacy.id}.json").read_text())["status"] == "open"


@pytest.mark.asyncio
async def test_feedback_auth_is_project_scoped_and_review_is_administrator_only(tmp_path: Path):
    app = create_app(trusted_settings(tmp_path), runtime=FailingRuntime())
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://gateway.test"
    ) as client:
        response = await request_as(
            client,
            "POST",
            "/api/projects",
            "owner@example.com",
            PrincipalRole.OWNER,
            json={"name": "Synthetic", "objective": "Review fixture"},
        )
        project_id = response.json()["id"]
        grant = await request_as(
            client,
            "POST",
            f"/api/projects/{project_id}/access",
            "owner@example.com",
            PrincipalRole.OWNER,
            json={"principal_id": "member@example.com", "role": "member"},
        )
        assert grant.status_code == 200
        run = app.state.ledger.create_run(
            project_id,
            "Synthetic",
            principal=principal("owner@example.com", PrincipalRole.OWNER),
        )
        event = await app.state.events.publish(
            run.id,
            EventType.ASSISTANT_MESSAGE_COMPLETED,
            {"text": "Synthetic quote is 25 USD."},
        )
        payload = {
            "run_id": run.id,
            "message_id": event.id,
            "kind": "conclusion_wrong",
            "user_note": "The synthetic quote says 20 USD.",
        }
        outsider = await request_as(
            client,
            "POST",
            "/api/feedback",
            "outsider@example.com",
            PrincipalRole.MEMBER,
            json=payload,
        )
        assert outsider.status_code == 403
        created = await request_as(
            client,
            "POST",
            "/api/feedback",
            "member@example.com",
            PrincipalRole.MEMBER,
            json=payload,
        )
        assert created.status_code == 201
        item = created.json()
        assert item["principal_id"] == "member@example.com"
        path = f"/api/feedback/{item['id']}"
        outsider_read = await request_as(
            client,
            "GET",
            path,
            "outsider@example.com",
            PrincipalRole.MEMBER,
        )
        assert outsider_read.status_code == 404
        outsider_list = await request_as(
            client,
            "GET",
            "/api/feedback",
            "outsider@example.com",
            PrincipalRole.MEMBER,
        )
        assert outsider_list.json() == []
        member_review = await request_as(
            client,
            "POST",
            path + "/review",
            "member@example.com",
            PrincipalRole.MEMBER,
            json={"expected_revision": 1, "status": "reviewed", "note": "Member cannot approve"},
        )
        assert member_review.status_code == 403
        owner_review = await request_as(
            client,
            "POST",
            path + "/review",
            "owner@example.com",
            PrincipalRole.OWNER,
            json={"expected_revision": 1, "status": "reviewed", "note": "Owner checked"},
        )
        assert owner_review.status_code == 200
        assert owner_review.json()["reviews"][0]["reviewer_principal_id"] == "owner@example.com"


@pytest.mark.asyncio
async def test_fold_http_verifies_existing_skill_hash_without_changing_skill(tmp_path: Path):
    app = create_app(Settings(_env_file=None, data_dir=tmp_path), runtime=FailingRuntime())
    _, _, _, payload = await anchored_message(app.state.ledger)
    item = app.state.feedback.add(payload, ledger=app.state.ledger)
    app.state.feedback.review(
        item.id, FeedbackReview(expected_revision=1, status="reviewed", note="Reviewed fixture")
    )
    skill = app.state.skills.list()[0]
    app.state.skills.snapshot()
    versions = app.state.skills.versions(skill.id)
    payload = {
        "expected_revision": 2,
        "status": "folded_into_skill",
        "note": "Human change attestation fixture",
        "skill_id": skill.id,
        "skill_content_hash": "0" * 64,
        "regression_report": "synthetic-report-reference; not production evidence",
        "manual_skill_update_confirmed": True,
        "l2_regression_passed_confirmed": True,
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        path = f"/api/feedback/{item.id}/review"
        missing = await client.post(path, json=payload)
        assert missing.status_code == 400
        payload["skill_content_hash"] = versions[0].content_hash
        folded = await client.post(path, json=payload)
        assert folded.status_code == 200, folded.text
        assert folded.json()["status"] == "folded_into_skill"
        assert folded.json()["reviews"][-1]["manual_skill_update_confirmed"] is True
        assert app.state.skills.versions(skill.id) == versions


def test_feedback_store_never_reads_symlinked_records(tmp_path: Path):
    outside = tmp_path / "outside.json"
    outside.write_text(FeedbackItem(kind="run_failure", user_note="Outside data").model_dump_json())
    store = FeedbackStore(tmp_path / "feedback")
    (store.root / "feedback_link.json").symlink_to(outside)
    assert store.list() == []
    with pytest.raises(OSError):
        store.get("feedback_link")


@pytest.mark.asyncio
async def test_feedback_reconciliation_failure_does_not_skip_other_runs(
    tmp_path: Path, monkeypatch, caplog
):
    ledger = FileLedger(tmp_path)
    _, first, _, _ = await anchored_message(ledger)
    _, second, _, _ = await anchored_message(ledger)
    for run in (first, second):
        ledger.update_run(run.id, status=RunStatus.FAILED)
    store = FeedbackStore(tmp_path / "feedback")
    original = store.capture_terminal

    def capture(run, events):
        if run.id == first.id:
            raise OSError("Synthetic failure; secret must not reach logs")
        return original(run, events)

    monkeypatch.setattr(store, "capture_terminal", capture)
    assert store.reconcile_terminal(ledger) == 1
    assert store.list()[0].run_id == second.id
    assert "reconciliation failed" in caplog.text
    assert "secret must not reach logs" not in caplog.text


@pytest.mark.asyncio
async def test_application_startup_collects_orphaned_run_after_recovery(tmp_path: Path):
    app = create_app(Settings(_env_file=None, data_dir=tmp_path), runtime=FailingRuntime())
    project = app.state.ledger.create_project(ProjectCreate(name="Synthetic", objective="Recovery"))
    orphan = app.state.ledger.create_run(project.id, "Synthetic interrupted task")
    async with app.router.lifespan_context(app):
        rows = app.state.feedback.list()
        assert len(rows) == 1 and rows[0].run_id == orphan.id
        assert rows[0].context.run_status == RunStatus.FAILED
        assert rows[0].context.event_id is not None
    assert len(app.state.feedback.list()) == 1
