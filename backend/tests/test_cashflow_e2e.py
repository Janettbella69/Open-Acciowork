"""The live acceptance must reject incomplete, failed, or altered SDK evidence."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI

from ophira_backend.constants import (
    TOOL_CASHFLOW_PEAK,
    TOOL_REGISTER_ARTIFACT,
    EventType,
    RunStatus,
)
from ophira_backend.models import Artifact, EventActor, EventEnvelope, OpportunityProject, RunRecord
from ophira_backend.untrusted import UNTRUSTED_DATA_NOTICE

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_cashflow_e2e.py"
SPEC = importlib.util.spec_from_file_location("ophira_run_cashflow_e2e", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
acceptance = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(acceptance)


@dataclass
class AcceptanceLedger:
    """Only the three read methods used by the verifier; all records are real models."""

    workspace: Path
    project: OpportunityProject
    run: RunRecord
    events: list[EventEnvelope]
    skill_files: dict[str, Path]

    def list_events(self, run_id: str) -> list[EventEnvelope]:
        assert run_id == self.run.id
        return self.events

    def workspace_for_project(self, project_id: str) -> Path:
        assert project_id == self.project.id
        return self.workspace

    def get_project(self, project_id: str) -> OpportunityProject:
        assert project_id == self.project.id
        return self.project

    def list_runs(self, project_id: str) -> list[RunRecord]:
        assert project_id == self.project.id
        return [self.run]

    def event(self, event_type: EventType, data: dict[str, Any], tool_id=None) -> None:
        sequence = len(self.events) + 1
        self.events.append(EventEnvelope(
            id=f"{self.run.id}:{sequence}", sequence=sequence, type=event_type.value,
            run_id=self.run.id, project_id=self.project.id, trace_id=self.run.trace_id,
            sdk_session_id=self.run.sdk_session_id,
            actor=EventActor(type="agent", name="ophira"), tool_use_id=tool_id, data=data,
        ))

    def call(self, name: str, arguments: dict, response: Any) -> None:
        tool_id = f"tool_{len(self.events) + 1}"
        data = {"tool_name": name, "tool_input": copy.deepcopy(arguments)}
        self.event(EventType.TOOL_STARTED, data, tool_id)
        self.event(EventType.TOOL_COMPLETED, {**data, "tool_response": response}, tool_id)

    def tool_event(self, name: str, *, completed: bool = True) -> EventEnvelope:
        event_type = EventType.TOOL_COMPLETED if completed else EventType.TOOL_STARTED
        return next(event for event in self.events
                    if event.type == event_type.value and event.data.get("tool_name") == name)

    def reindex(self) -> None:
        for sequence, event in enumerate(self.events, 1):
            event.sequence = sequence
            event.id = f"{self.run.id}:{sequence}"

    def verify(self) -> dict:
        return acceptance.verify_run(self, self.run, self.skill_files, budget_usd=2)


@pytest.fixture
def ledger(tmp_path: Path) -> AcceptanceLedger:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    project = OpportunityProject(id="project_cashflow", name="Cashflow", objective="Acceptance")
    run = RunRecord(
        id="run_cashflow", trace_id="trace_cashflow", project_id=project.id,
        prompt=acceptance.PROMPT, status=RunStatus.COMPLETED,
        sdk_session_id="session_cashflow", sdk_budget_usd=2,
    )
    ledger = AcceptanceLedger(workspace, project, run, [], {})
    ledger.event(EventType.RUN_CREATED, {})
    ledger.event(EventType.RUN_STARTED, {})
    ledger.call("Skill", {"skill": acceptance.SKILL_ID}, [
        {"type": "text", "text": "Launching skill: " + acceptance.SKILL_ID},
    ])
    for relative in acceptance.RESOURCE_PATHS:
        path = tmp_path / "ophira-core/skills/landed-cost-analysis" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        contents = "# Cashflow\nUse cashflow_peak with explicit inputs.\nKeep reserves separate.\n"
        path.write_text(contents, encoding="utf-8")
        ledger.skill_files[relative] = path
        ledger.call("Read", {"file_path": str(path)}, {
            "type": "text", "file": {
                "filePath": str(path), "content": contents,
                "numLines": 3, "startLine": 1, "totalLines": 3,
            },
        })
    fixture_path = workspace / "uploads/cashflow-fixture.json"
    fixture_path.parent.mkdir()
    fixture_path.write_text(json.dumps(acceptance.FIXTURE), encoding="utf-8")
    ledger.call("Read", {"file_path": str(fixture_path)}, {
        "type": "text", "file": {"content": fixture_path.read_text(encoding="utf-8")},
    })
    payload = acceptance.calculate_cashflow(acceptance.FIXTURE)
    payload["note"] = UNTRUSTED_DATA_NOTICE
    payload["input_text_sanitized"] = False
    ledger.call(TOOL_CASHFLOW_PEAK, acceptance.FIXTURE, {
        "content": [{"type": "text", "text": json.dumps(payload)}], "isError": False,
    })
    markdown = (
        "# 合成 fixture\n\n来源未验证。reserve 不列支出；先付后收是保守假设。\n\n"
        "| 指标 | 工具值 |\n| --- | --- |\n"
        "| funding_for_opening_and_daily_closes | 400.0000 |\n"
        "| funding_if_outflows_settle_first | 600.0000 |\n"
        "| closing_cash | -200.0000 |\n"
        f"| input_sha256 | {payload['input_sha256']} |\n"
    )
    for filename, content, mime in (
        (acceptance.JSON_FILE, json.dumps(payload, ensure_ascii=False), "application/json"),
        (acceptance.MARKDOWN_FILE, markdown, "text/markdown"),
    ):
        path = workspace / filename
        path.write_text(content, encoding="utf-8")
        ledger.call("Write", {"file_path": str(path), "content": content}, {
            "type": "create", "filePath": str(path), "content": content,
        })
        artifact = Artifact(
            project_id=project.id, run_id=run.id, path=filename, name=filename, mime=mime,
            checksum=hashlib.sha256(path.read_bytes()).hexdigest(),
            size_bytes=path.stat().st_size, download_url=f"/artifacts/{filename}",
        )
        project.artifacts.append(artifact)
        ledger.call(TOOL_REGISTER_ARTIFACT, {"path": filename, "mime": mime, "source_ids": []}, {
            "content": [{"type": "text", "text": artifact.model_dump_json()}],
            "isError": False,
        })
    ledger.event(EventType.ASSISTANT_MESSAGE_COMPLETED, {
        "text": "已完成合成验收。", "model": "deepseek-v4-pro",
    })
    ledger.event(EventType.RUN_RESULT, {"is_error": False, "total_cost_usd": 0.123})
    ledger.event(EventType.RUN_COMPLETED, {})
    return ledger


def test_complete_sdk_evidence_and_editable_artifacts_pass(ledger: AcceptanceLedger) -> None:
    verdict = ledger.verify()
    assert verdict["passed"], verdict["failed_checks"]
    assert verdict["served_models"] == ["deepseek-v4-pro"]
    assert verdict["cost_usd_sdk_list_price"] == 0.123
    assert verdict["event_count"] == len(ledger.events)


@pytest.mark.parametrize("fault", [
    "planned_resource_read", "failed_resource_read", "failed_skill", "altered_tool_result",
    "altered_saved_json", "altered_tool_input", "altered_input_hash", "missing_registration",
    "failed_registration", "cashflow_wrong_session", "sdk_budget_exceeded", "different_model",
    "missing_event", "extra_skill_approval",
])
def test_incomplete_or_altered_evidence_cannot_claim_acceptance(
    ledger: AcceptanceLedger, fault: str,
) -> None:
    if fault == "planned_resource_read":
        ledger.events.remove(ledger.tool_event("Read"))
        ledger.reindex()  # Fail because Read never completed, even with a sound event sequence.
    elif fault in {"failed_resource_read", "failed_skill", "failed_registration"}:
        name = {"failed_resource_read": "Read", "failed_skill": "Skill",
                "failed_registration": TOOL_REGISTER_ARTIFACT}[fault]
        ledger.tool_event(name).data["tool_response"] = {
            "is_error": True, "content": [{"type": "text", "text": "cashflow_peak failed"}],
        }
    elif fault in {"altered_tool_result", "altered_input_hash"}:
        event = ledger.tool_event(TOOL_CASHFLOW_PEAK)
        payload = acceptance.tool_payload(event.data["tool_response"])
        assert payload is not None
        if fault == "altered_tool_result":
            payload["funding"]["funding_if_outflows_settle_first"] = "400.0000"
        else:
            payload["input_sha256"] = "0" * 64
        event.data["tool_response"] = payload
        # A fabricated result and matching on-disk JSON cannot validate each other.
        (ledger.workspace / acceptance.JSON_FILE).write_text(json.dumps(payload), encoding="utf-8")
        ledger.project.artifacts[0].checksum = hashlib.sha256(
            (ledger.workspace / acceptance.JSON_FILE).read_bytes(),
        ).hexdigest()
    elif fault == "altered_saved_json":
        path = ledger.workspace / acceptance.JSON_FILE
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload.pop("note")  # Even omitted server metadata makes this a non-exact copy.
        path.write_text(json.dumps(payload), encoding="utf-8")
        ledger.project.artifacts[0].checksum = hashlib.sha256(path.read_bytes()).hexdigest()
    elif fault == "altered_tool_input":
        ledger.tool_event(TOOL_CASHFLOW_PEAK, completed=False).data["tool_input"][
            "opening_cash"
        ] = "601"
    elif fault == "missing_registration":
        ledger.events.remove(ledger.tool_event(TOOL_REGISTER_ARTIFACT))
        ledger.reindex()  # A project artifact alone does not prove the Agent registered it.
    elif fault == "cashflow_wrong_session":
        ledger.tool_event(TOOL_CASHFLOW_PEAK, completed=False).sdk_session_id = "other-session"
    elif fault == "sdk_budget_exceeded":
        ledger.run.sdk_budget_usd = 2.01
    elif fault == "different_model":
        for event in ledger.events:
            if event.type == EventType.ASSISTANT_MESSAGE_COMPLETED.value:
                event.data["model"] = "claude-sonnet"
    elif fault == "missing_event":
        ledger.events.pop(0)
    elif fault == "extra_skill_approval":
        ledger.event(EventType.PERMISSION_REQUESTED, {"tool_name": "Skill"})
    else:
        raise AssertionError(f"Unknown fault: {fault}")
    verdict = ledger.verify()
    assert not verdict["passed"], f"False acceptance for {fault}"
    assert verdict["failed_checks"]


def test_permission_policy_limits_writes_to_two_exact_workspace_targets(tmp_path: Path) -> None:
    policy = acceptance.CashflowPermissionPolicy(tmp_path)
    for name in ("Write", "Edit", TOOL_REGISTER_ARTIFACT):
        key = "path" if name == TOOL_REGISTER_ARTIFACT else "file_path"
        for filename in (acceptance.JSON_FILE, acceptance.MARKDOWN_FILE):
            for raw in (filename, str(tmp_path / filename)):
                assert policy.decide(name, {key: raw}) == "allow_once"
        for raw in ("other.md", "../cashflow-result.json", "/tmp/cashflow-result.json", None):
            assert policy.decide(name, {key: raw}) == "deny"
    for name in ("Bash", "Read", "Skill", "WebSearch", TOOL_CASHFLOW_PEAK):
        assert policy.decide(name, {"file_path": acceptance.JSON_FILE}) == "deny"
    assert len(policy.allowed) == 12
    assert len(policy.denied) == 17


def test_permission_policy_denies_output_symlink_escape(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / acceptance.JSON_FILE).symlink_to(tmp_path / "outside.json")
    policy = acceptance.CashflowPermissionPolicy(workspace)
    assert policy.decide("Write", {"file_path": acceptance.JSON_FILE}) == "deny"
    assert policy.decide(TOOL_REGISTER_ARTIFACT, {"path": acceptance.JSON_FILE}) == "deny"


@pytest.mark.asyncio
async def test_run_failure_and_archive_error_still_preserve_independent_evidence(
    ledger: AcceptanceLedger, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = FastAPI()
    application.state.ledger = ledger
    application.state.providers = SimpleNamespace(get=lambda _: SimpleNamespace(budget_scale=1))
    plugin_root = next(iter(ledger.skill_files.values())).parents[3]
    application.state.skills = SimpleNamespace(runtime_snapshot_lease=lambda _: nullcontext(
        SimpleNamespace(plugin_paths=[{"path": str(plugin_root)}]),
    ))

    async def stop():
        raise RuntimeError("cleanup failed")

    application.state.runs = SimpleNamespace(stop=stop)

    @application.post("/api/projects")
    async def create_project():
        return ledger.project.model_dump(mode="json")

    @application.post("/api/projects/{project_id}/files")
    async def upload_file(project_id: str):
        assert project_id == ledger.project.id
        return {"filename": "cashflow-fixture.json"}

    async def fail_run(*args, **kwargs):
        raise TimeoutError("provider run deadline")

    def fail_copy(*args, **kwargs):
        raise OSError("workspace archive unavailable")

    monkeypatch.setattr(acceptance, "Settings", lambda **kwargs: SimpleNamespace(**kwargs))
    monkeypatch.setattr(acceptance, "create_app", lambda _: application)
    monkeypatch.setattr(acceptance, "provider_readiness_failures", lambda _: [])
    monkeypatch.setattr(acceptance, "drive_run", fail_run)
    monkeypatch.setattr(acceptance.shutil, "copytree", fail_copy)
    report_dir = tmp_path / "report"
    summary = await acceptance.run_acceptance(
        report_dir, provider_id="deepseek", budget_usd=2, timeout_s=900,
    )

    saved = json.loads((report_dir / "summary.json").read_text(encoding="utf-8"))
    assert saved == summary
    assert summary["passed"] is False
    assert "TimeoutError: provider run deadline" in summary["error"]
    assert "cleanup failed" in summary["cleanup_error"]
    assert any("copy_workspace" in error for error in summary["archive_errors"])
    archived_events = [json.loads(line) for line in
                       (report_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert archived_events == [event.model_dump(mode="json") for event in ledger.events]
    for filename in ("fixture.json", "prompt.md", "run.json", "project.json"):
        assert (report_dir / filename).is_file()
    for relative, original in ledger.skill_files.items():
        assert (report_dir / "skill-resources" / relative).read_bytes() == original.read_bytes()
