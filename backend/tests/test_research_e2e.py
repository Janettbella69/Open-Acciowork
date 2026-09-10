"""A real-provider acceptance must reject incomplete or untrusted observations."""

import copy
import importlib.util
import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_telemetry import BASE, event, make_run

from ophira_backend.constants import (
    RESEARCH_UNITS,
    TOOL_RECORD_EVIDENCE_BATCH,
    EventType,
    RunMode,
)
from ophira_backend.models import EventActor

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/run_research_e2e.py"
SPEC = importlib.util.spec_from_file_location("ophira_research_e2e", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
acceptance = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(acceptance)


@pytest.fixture
def observed_run():
    run = make_run().model_copy(update={
        "mode": RunMode.DEEP_RESEARCH, "sdk_budget_usd": 2,
        "sdk_wall_clock_deadline": BASE + timedelta(minutes=10),
    })
    events = []

    def append(kind, data, *, agent_id=None):
        value = event(run, len(events) + 1, kind, len(events) * 100, data)
        if agent_id:
            value.actor = EventActor(type="subagent", name=agent_id, agent_id=agent_id)
        events.append(value)

    for unit in RESEARCH_UNITS:
        append(EventType.SUBAGENT_STARTED, {"agent_id": unit, "agent_type": unit})
    for unit in RESEARCH_UNITS:
        for filename in acceptance.FIXTURES:
            append(EventType.TOOL_COMPLETED, {
                "tool_name": "Read", "tool_input": {"file_path": f"uploads/{filename}"},
                "tool_response": "Synthetic quote",
            }, agent_id=unit)
        append(EventType.SUBAGENT_STOPPED, {"agent_id": unit, "agent_type": unit})
        packet = {"protocol": "ophira-research-v1", "unknowns": [], "claims": [{
            "claim": "Synthetic quote", "evidence_kind": "user_statement",
            "source_locator": "uploads/quote_en.md:3", "observed_at": None,
            "excerpt": "USD 6.80", "language": "en", "conflicts": ["New quote differs"],
            "limitations": ["Unverified"],
        }]}
        append(EventType.TOOL_COMPLETED, {
            "tool_name": "Agent", "tool_input": {"subagent_type": unit},
            "tool_response": {"content": [{"type": "text", "text": json.dumps(packet)}]},
        })
    for filename in acceptance.FIXTURES:
        append(EventType.TOOL_COMPLETED, {
            "tool_name": "Read", "tool_input": {"file_path": f"uploads/{filename}"},
            "tool_response": "Synthetic quote",
        })
    append(EventType.TOOL_COMPLETED, {
        "tool_name": TOOL_RECORD_EVIDENCE_BATCH,
        "tool_input": {"entries": [{"claim": "A"}, {"claim": "B"}, {"claim": "A"}]},
        "tool_response": {"content": [{"type": "text", "text": "[]"}]},
    })
    append(EventType.ASSISTANT_MESSAGE_COMPLETED, {"model": "deepseek-v4-pro"})
    append(EventType.RUN_RESULT, {"is_error": False, "total_cost_usd": 0.5})
    project = SimpleNamespace(id=run.project_id, evidence=[
        SimpleNamespace(id=f"ev_{index}", source_run_id=run.id, source_path=f"uploads/{name}",
                        claim=claim, kind="user_statement", source_type="file")
        for index, (name, claim) in enumerate((("quote_en.md", "USD 6.80"),
                                               ("quote_zh.md", "USD 7.20")))
    ], decisions=[SimpleNamespace(id="decision_1", source_run_id=run.id,
                                  evidence_ids=["ev_0", "ev_1"], decision_type="plan")])
    resources = {"observations_available": True, "run_id": run.id,
                 "project_id": project.id, "trace_id": run.trace_id, "status": run.status,
                 "agents": [{"agent_id": unit} for unit in RESEARCH_UNITS]}
    markdown = " ".join(["合成 fixture 未验证 6.80 7.20 500 300 冲突 去重 2026-09-01 2026-09-03",
                         "ev_0 ev_1 decision_1", *RESEARCH_UNITS])
    return [run, project, events, resources, markdown]


def test_complete_native_fixture_passes(observed_run):
    verdict = acceptance.verify_run(*observed_run, budget_usd=2)
    assert verdict["passed"], verdict["failed_checks"]


@pytest.mark.parametrize("tool", ["Glob", "Grep"])
def test_native_readonly_file_discovery_is_not_an_unrelated_external_tool(observed_run, tool):
    values = copy.deepcopy(observed_run)
    events = values[2]
    discovery = events[3].model_copy(deep=True)
    discovery.data = {"tool_name": tool, "tool_input": {"pattern": "**/uploads/*"},
                      "tool_response": "uploads/quote_en.md"}
    events.insert(3, discovery)
    for index, row in enumerate(events, 1):
        row.sequence, row.id = index, f"{values[0].id}:{index}"
    verdict = acceptance.verify_run(*values, budget_usd=2)
    assert verdict["passed"], verdict["failed_checks"]


@pytest.mark.parametrize("mutation,failed_check", [
    ("missing_unit", "exactly_three_native_research_units"),
    ("child_write", "children_remained_readonly"),
    ("wrong_trace", "event_identity_and_sequence"),
    ("duplicate_repost", "two_original_sources_only"),
    ("unlinked_decision", "decision_links_both_sources"),
    ("invented_report_id", "report_cites_ledger_and_scope"),
    ("unknown_cost", "sdk_budget_frozen_and_cost_reported"),
    ("stale_resource", "resources_match_native_observations"),
    ("failed_return", "source_packet:source-mapper"),
    ("no_source_packet", "source_packet:source-mapper"),
    ("unread_source", "child_read:source-mapper"),
    ("external_tool", "no_unrelated_completed_tools"),
])
def test_incomplete_or_forged_acceptance_fails(observed_run, mutation, failed_check):
    values = copy.deepcopy(observed_run)
    run, project, events, resources, _ = values
    if mutation == "missing_unit":
        events.pop(0)
    elif mutation == "child_write":
        events[3].data["tool_name"] = "Write"
    elif mutation == "wrong_trace":
        events[0].trace_id = "other"
    elif mutation == "duplicate_repost":
        project.evidence.append(copy.deepcopy(project.evidence[0]))
    elif mutation == "unlinked_decision":
        project.decisions[0].evidence_ids = ["ev_0"]
    elif mutation == "invented_report_id":
        values[-1] = values[-1].replace("ev_1", "invented")
    elif mutation == "unknown_cost":
        events[-1].data["total_cost_usd"] = None
    elif mutation == "stale_resource":
        resources["run_id"] = "other"
    elif mutation in {"failed_return", "no_source_packet"}:
        completion = next(event for event in events if event.data.get("tool_name") == "Agent")
        if mutation == "failed_return":
            completion.data["tool_response"]["is_error"] = True
        else:
            completion.data["tool_response"] = "Configuration includes all three agents."
    elif mutation == "unread_source":
        events[3].data["tool_input"]["file_path"] = "uploads/other.md"
    elif mutation == "external_tool":
        events[3].data["tool_name"] = "WebSearch"
    verdict = acceptance.verify_run(*values, budget_usd=2)
    assert not verdict["passed"]
    assert failed_check in verdict["failed_checks"]


def test_source_packet_does_not_accept_echoed_prompt():
    response = {"prompt": '{"protocol":"ophira-research-v1","claims":[]}',
                "content": [{"type": "text", "text": "Could not complete research."}]}
    assert acceptance.source_packet(response) is None


def test_acceptance_policy_only_approves_its_report_and_ledger_writes(tmp_path):
    policy = acceptance.ResearchPermissionPolicy(tmp_path)
    assert policy.decide("Write", {"file_path": acceptance.REPORT}) == "allow_once"
    assert policy.decide("Write", {"file_path": "../other.md"}) == "deny"
    assert policy.decide("Write", {"file_path": "uploads/quote_en.md"}) == "deny"
    assert policy.decide("Bash", {"command": "anything"}) == "deny"
    assert policy.decide("send_message", {"message": "anything"}) == "deny"
