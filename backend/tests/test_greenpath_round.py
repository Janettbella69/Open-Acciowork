"""Offline round acceptance uses the final ledger and preserves failed SSE audits."""

import asyncio
import importlib.util
import json
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from test_greenpath_verifier import happy_path_events

from ophira_backend.constants import STREAM_END_EVENT, EventType, EvidenceSourceType, RunStatus
from ophira_backend.models import (
    Decision,
    EventActor,
    EventEnvelope,
    Evidence,
    ProjectCreate,
    utc_now,
)
from ophira_backend.storage import FileLedger

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_green_path.py"
SPEC = importlib.util.spec_from_file_location("ophira_greenpath_round", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


class OfflineRound:
    """Only the HTTP transport and model driver are replaced; verifiers stay real."""

    def __init__(self, root, control="valid"):
        self.ledger = FileLedger(root / "ledger")
        self.reports = root / "reports"
        self.control = control
        self.project = None
        self.run = None
        self.drive_calls = []
        self.sse_requests = 0
        self.initial_count = 0
        self.receipt = None

    async def drive(self, client, ledger, project_id, prompt, **kwargs):
        assert ledger is self.ledger
        self.drive_calls.append({"prompt": prompt, **kwargs})
        if kwargs.get("resume_session_id"):
            assert prompt == runner.RESUME_PROMPT
            assert self.sse_requests == 1
            resumed = ledger.create_run(project_id, "Synthetic offline resume")
            resumed = ledger.update_run(resumed.id, status=RunStatus.COMPLETED)
            report = ledger.workspace_for_project(project_id) / "greenpath-report.md"
            report.write_text(report.read_text() + "\n会话续接验证 OK\n", encoding="utf-8")
            return resumed.model_dump(mode="json")
        assert prompt == runner.GREEN_PATH_PROMPT
        self.run = ledger.create_run(project_id, "Synthetic offline main run")
        self.run = ledger.update_run(
            self.run.id, status=RunStatus.COMPLETED,
            sdk_session_id="offline-session", finished_at=utc_now(),
        )
        for event in happy_path_events():
            ledger.append_event(event.model_copy(update={
                "id": "pending", "run_id": self.run.id, "project_id": project_id,
                "trace_id": self.run.trace_id, "sdk_session_id": self.run.sdk_session_id,
            }))
        self.initial_count = len(ledger.list_events(self.run.id))
        return self.run.model_dump(mode="json")

    def complete_ledger_during_subscription(self):
        """Persist a complete synthetic project and project its late durable tail."""
        ledger, run = self.ledger, self.run
        workspace = ledger.workspace_for_project(run.project_id)
        (workspace / "greenpath-report.md").write_text("Synthetic offline report.\n")
        ledger.register_artifact(run.project_id, "greenpath-report.md", run_id=run.id)
        self.receipt = ledger.issue_tool_receipt(
            run.project_id, run.id, tool="WebSearch", source_type=EvidenceSourceType.WEB,
            canonical_source="https://example.invalid/synthetic-fixture",
            payload={"synthetic": True}, object_type="web_page",
        )
        web = Evidence(
            kind="observed_fact", claim="Synthetic web observation", source_title="Offline fixture",
            source_url="https://example.invalid/synthetic-fixture", source_type="web",
            receipt_id=self.receipt.id, confidence=1, recorded_by="offline-fixture",
            source_run_id=run.id,
        )
        local = Evidence(
            kind="observed_fact", claim="Synthetic quote observation", source_title="Quote fixture",
            source_type="file", source_path="uploads/quotes.csv", confidence=1,
            recorded_by="offline-fixture", source_run_id=run.id,
        )
        decision = Decision(
            question="Synthetic decision", outcome="Test the fixture", made_by="offline-fixture",
            evidence_ids=[web.id, local.id], source_run_id=run.id,
        )

        def persist(project):
            project.evidence.extend([web, local])
            project.decisions.append(decision)

        ledger.update_project(run.project_id, persist)
        for event_type, resource in [
            (EventType.EVIDENCE_ADDED, web), (EventType.EVIDENCE_ADDED, local),
            (EventType.DECISION_RECORDED, decision),
        ]:
            ledger.append_event(EventEnvelope(
                id="pending", sequence=1, type=event_type.value, run_id=run.id,
                project_id=run.project_id, trace_id=run.trace_id,
                actor=EventActor(type="system", name="offline-project-ledger"),
                data={"resource_id": resource.id},
            ))
        ledger.update_run(run.id, finished_at=run.finished_at + timedelta(seconds=1))

    def handle(self, request):
        if request.method == "POST" and request.url.path == "/api/projects":
            self.project = self.ledger.create_project(ProjectCreate.model_validate_json(
                request.content,
            ))
            return httpx.Response(201, json=self.project.model_dump(mode="json"))
        if (request.method == "POST"
                and request.url.path == f"/api/projects/{self.project.id}/files"):
            artifact = self.ledger.save_upload(
                self.project.id, request.url.params["filename"], request.content,
                mime=request.url.params["mime"],
            )
            return httpx.Response(201, json=artifact.model_dump(mode="json"))
        if request.method == "GET" and request.url.path == f"/api/runs/{self.run.id}/events":
            self.sse_requests += 1
            assert self.sse_requests == 1
            checkpoint = self.initial_count // 2
            assert request.headers["Last-Event-ID"] == f"{self.run.id}:{checkpoint}"
            self.complete_ledger_during_subscription()
            if self.control == "http_error":
                return httpx.Response(503, json={"detail": "Synthetic projection failure"})
            events = self.ledger.list_events(self.run.id, checkpoint)
            frames = [
                f"event: {event.type}\nid: {event.id}\ndata: {event.model_dump_json()}\n\n"
                for event in events
            ]
            watermark = {
                "run_id": self.run.id, "project_id": self.project.id,
                "last_sequence": events[-1].sequence,
            }
            if self.control == "wrong_watermark":
                watermark["last_sequence"] += 1
            if self.control != "missing":
                frames.append(f"event: {STREAM_END_EVENT}\ndata: {json.dumps(watermark)}\n\n")
            return httpx.Response(
                200, text="".join(frames), headers={"content-type": "text/event-stream"},
            )
        raise AssertionError(f"Unexpected offline request: {request.method} {request.url.path}")

    async def run_round(self, monkeypatch, *, timeout_s=3):
        monkeypatch.setattr(runner, "drive_run", self.drive)
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(self.handle), base_url="http://offline.invalid",
        ) as client:
            return await runner.run_round(
                client, self.ledger, "offline-provider", 1, self.reports, timeout_s,
            )

    def read_bundle(self):
        bundle = self.reports / "round-1"
        summary = json.loads((bundle / "summary.json").read_text())
        events = [json.loads(line) for line in (bundle / "events.jsonl").read_text().splitlines()]
        return summary, events, (bundle / "report.md").read_text()


@pytest.mark.asyncio
async def test_round_uses_post_subscription_ledger_for_replay_and_final_verdict(
    tmp_path, monkeypatch,
):
    fixture = OfflineRound(tmp_path)
    passed, result = await fixture.run_round(monkeypatch)
    assert passed is True, result
    assert result["sse_replay_ok"] is True
    assert result["sse_replay_error"] is None
    assert result["resume_ok"] is True
    assert result["verdict_missing"] == result["project_missing"] == []
    assert len(fixture.drive_calls) == 2
    assert fixture.drive_calls[1]["resume_session_id"] == "offline-session"
    final_events = fixture.ledger.list_events(fixture.run.id)
    assert len(final_events) == fixture.initial_count + 3
    summary, archived, report = fixture.read_bundle()
    assert summary["passed"] is True
    assert summary["event_count"] == len(final_events)
    assert summary["notes"]["sse_last_sequence"] == final_events[-1].sequence
    assert summary["notes"]["sse_replayed_count"] == len(final_events) - fixture.initial_count // 2
    assert summary["run"]["finished_at"] == fixture.ledger.get_run(fixture.run.id).model_dump(
        mode="json",
    )["finished_at"]
    assert archived == [event.model_dump(mode="json") for event in final_events]
    assert all(event["type"] != STREAM_END_EVENT for event in archived)
    assert "✅ PASS" in report


@pytest.mark.parametrize("control", ["missing", "wrong_watermark", "http_error"])
@pytest.mark.asyncio
async def test_round_preserves_full_audit_and_skips_resume_after_sse_failure(
    tmp_path, monkeypatch, control,
):
    fixture = OfflineRound(tmp_path, control)
    passed, result = await fixture.run_round(monkeypatch)
    assert passed is False
    assert result["passed"] is False
    assert result["sse_replay_ok"] is False
    assert result["sse_replay_error"]
    assert result["resume_ok"] is False
    assert "SSE replay verification failed" in result["resume_error"]
    assert len(fixture.drive_calls) == 1
    # Actual business verifiers pass, so only SSE failure can reject this round.
    assert result["verdict_missing"] == result["project_missing"] == []
    summary, archived, report = fixture.read_bundle()
    assert summary["passed"] is False
    assert summary["notes"]["sse_replay_error"] == result["sse_replay_error"]
    assert summary["notes"]["sse_replay_ok"] is False
    final_events = fixture.ledger.list_events(fixture.run.id)
    assert len(final_events) == fixture.initial_count + 3
    assert summary["event_count"] == len(final_events)
    assert archived == [event.model_dump(mode="json") for event in final_events]
    assert "❌ FAIL" in report and "✅ PASS" not in report


@pytest.mark.asyncio
async def test_round_deadline_cancels_unending_replay_and_archives_failure(tmp_path, monkeypatch):
    fixture = OfflineRound(tmp_path)
    cancelled = False

    async def never_finishes(_client, run_id, checkpoint, *, project_id):
        nonlocal cancelled
        assert run_id == fixture.run.id and project_id == fixture.project.id
        assert checkpoint == fixture.initial_count // 2
        fixture.complete_ledger_during_subscription()
        try:
            # Models a heartbeat connection that never sends its completion
            # watermark; an HTTP per-read timeout cannot bound this operation.
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled = True
            raise

    monkeypatch.setattr(runner, "sse_replay_sequences", never_finishes)
    passed, result = await asyncio.wait_for(
        fixture.run_round(monkeypatch, timeout_s=0.01), timeout=2,
    )
    assert cancelled is True
    assert passed is False and result["passed"] is False
    assert result["sse_replay_error"] == "SSE deadline exceeded after 0.01 seconds"
    assert result["sse_replay_ok"] is False and result["resume_ok"] is False
    assert len(fixture.drive_calls) == 1
    assert result["verdict_missing"] == result["project_missing"] == []
    summary, archived, report = fixture.read_bundle()
    assert summary["passed"] is False
    assert summary["notes"]["sse_replay_error"] == result["sse_replay_error"]
    assert archived == [event.model_dump(mode="json")
                        for event in fixture.ledger.list_events(fixture.run.id)]
    assert "❌ FAIL" in report and "✅ PASS" not in report
