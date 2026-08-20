"""Offline tests for the Green Path verifier: the assertion logic must be trustworthy
before any live round is allowed to claim a pass."""

from ophira_backend.greenpath import GreenPathVerdict, verify_events, verify_project_state
from ophira_backend.models import (
    Artifact,
    Decision,
    EventActor,
    EventEnvelope,
    Evidence,
    OpportunityProject,
)

RUN = {"run_id": "run_x", "project_id": "project_x", "trace_id": "trace_x"}


def ev(sequence: int, type_: str, data: dict, actor_type: str = "agent") -> EventEnvelope:
    return EventEnvelope(
        id=f"run_x:{sequence}",
        sequence=sequence,
        type=type_,
        actor=EventActor(type=actor_type, name="ophira"),
        data=data,
        **RUN,
    )


def happy_path_events() -> list[EventEnvelope]:
    return [
        ev(1, "run.created", {}),
        ev(2, "run.started", {}),
        ev(3, "tool.started", {"tool_name": "Skill", "tool_input": {"skill": "ophira-core:landed-cost-analysis"}}),
        ev(4, "tool.started", {"tool_name": "Read", "tool_input": {"file_path": "uploads/quotes.csv"}}),
        ev(5, "tool.started", {"tool_name": "WebSearch", "tool_input": {"query": "blender"}}),
        ev(6, "tool.completed", {"tool_name": "WebSearch"}),
        ev(7, "tool.started", {"tool_name": "mcp__ophira_commerce_math__calculate_landed_cost", "tool_input": {}}),
        ev(8, "permission.requested", {"request_id": "p1", "tool_name": "Bash", "tool_input": {"command": "wc -l"}}),
        ev(9, "permission.resolved", {"request_id": "p1", "decision": "allow_once"}),
        ev(10, "tool.started", {"tool_name": "Bash", "tool_input": {"command": "wc -l"}}),
        ev(11, "tool.started", {"tool_name": "Write", "tool_input": {"file_path": "greenpath-report.md"}}),
        ev(12, "tool.started", {"tool_name": "Edit", "tool_input": {"file_path": "greenpath-report.md"}}),
        ev(13, "permission.requested", {"request_id": "p2", "tool_name": "Bash", "tool_input": {"command": "echo OPHIRA_DENY_MARKER"}}),
        ev(14, "permission.resolved", {"request_id": "p2", "decision": "deny"}),
        ev(15, "assistant.message.completed", {"text": "总结", "model": "deepseek-v4-pro"}),
        ev(16, "run.result", {"is_error": False, "total_cost_usd": 0.12, "num_turns": 14}),
        ev(17, "run.completed", {}),
    ]


def test_happy_path_passes_every_assertion() -> None:
    verdict = verify_events(happy_path_events())

    assert verdict.ok, verdict.missing
    assert all(verdict.tool_coverage.values())
    assert verdict.web_degraded is False
    assert verdict.subagent_events == 0
    assert verdict.permission_flow_ok
    assert verdict.deny_then_continue_ok
    assert verdict.sequence_contiguous
    assert verdict.served_models == ["deepseek-v4-pro"]
    assert verdict.total_cost_usd == 0.12
    assert verdict.num_turns == 14


def test_any_subagent_activity_is_a_violation() -> None:
    events = happy_path_events() + [ev(18, "subagent.started", {"agent_id": "a1"})]
    verdict = verify_events(events)

    assert "violation:subagent_events_present" in verdict.missing
    assert not verdict.ok


def test_missing_edit_tool_is_reported() -> None:
    events = [event for event in happy_path_events() if event.data.get("tool_name") != "Edit"]
    events = [
        event.model_copy(update={"sequence": index + 1, "id": f"run_x:{index + 1}"})
        for index, event in enumerate(events)
    ]
    verdict = verify_events(events)

    assert "tool:edit" in verdict.missing


def test_sequence_gap_is_detected() -> None:
    events = happy_path_events()
    events[5] = events[5].model_copy(update={"sequence": 99})
    verdict = verify_events(events)

    assert verdict.sequence_contiguous is False
    assert "events:sequence_gap" in verdict.missing


def test_web_tool_failure_counts_as_degraded_coverage() -> None:
    events = []
    for event in happy_path_events():
        if event.data.get("tool_name") == "WebSearch":
            continue
        events.append(event)
    events.append(ev(98, "tool.started", {"tool_name": "WebSearch", "tool_input": {}}))
    events = [
        event.model_copy(update={"sequence": index + 1, "id": f"run_x:{index + 1}"})
        for index, event in enumerate(events)
    ]
    # Replace the started event with a failure-only trace.
    events[-1] = ev(len(events), "tool.failed", {"tool_name": "WebSearch", "error": "not supported"})

    verdict = verify_events(events)

    assert verdict.tool_coverage["web"] is True
    assert verdict.web_degraded is True


def test_timed_out_permission_breaks_the_flow() -> None:
    events = happy_path_events() + [
        ev(18, "permission.requested", {"request_id": "p3", "tool_name": "Write", "tool_input": {}}),
        ev(19, "permission.timed_out", {"request_id": "p3"}),
    ]
    verdict = verify_events(events)

    assert verdict.permission_flow_ok is False
    assert "permission:flow_incomplete" in verdict.missing


def test_verdict_dataclass_defaults_fail_closed() -> None:
    assert GreenPathVerdict().ok is True  # empty missing means ok; verify_events always fills it
    assert verify_events([]).ok is False


def make_project(**overrides) -> OpportunityProject:
    project = OpportunityProject(id="project_x", name="GP", objective="verify")
    for key, value in overrides.items():
        setattr(project, key, value)
    return project


def test_project_state_requires_report_evidence_and_decision() -> None:
    empty = make_project()
    missing = verify_project_state(empty)
    assert "artifact:greenpath-report.md" in missing
    assert "evidence:with_source_url" in missing
    assert "decision:recorded" in missing

    evidence_url = Evidence(
        kind="community_signal",
        claim="EU stats exist",
        source_title="Eurostat",
        source_url="https://example.org/stats",
        source_type="web",
        confidence=0.4,
        recorded_by="greenpath-agent",
    )
    evidence_file = Evidence(
        kind="user_statement",
        claim="预算不超过 8000 USD",
        source_title="约束文件",
        source_type="file",
        source_path="uploads/constraints.md",
        confidence=0.9,
        recorded_by="greenpath-agent",
    )
    decision = Decision(
        question="值得测试吗",
        outcome="小规模测试",
        decision_type="test",
        evidence_ids=[evidence_url.id],
        made_by="greenpath-agent",
    )
    artifact = Artifact(
        project_id="project_x",
        path="greenpath-report.md",
        name="greenpath-report.md",
        checksum="0" * 64,
        size_bytes=10,
        download_url="/x",
    )
    full = make_project(
        evidence=[evidence_url, evidence_file],
        decisions=[decision],
        artifacts=[artifact],
    )
    assert verify_project_state(full) == []
