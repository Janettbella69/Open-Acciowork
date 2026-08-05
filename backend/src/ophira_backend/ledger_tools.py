from __future__ import annotations

import json
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from .constants import (
    MCP_SERVER_NAME,
    Dimension,
    EvidenceKind,
    ProjectStage,
    StageDecisionValue,
    ValidationStatus,
)
from .models import DimensionAssessment, Evidence, Hypothesis, StageDecision
from .storage import FileLedger


def _tool_result(value: Any, *, is_error: bool = False) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(value, ensure_ascii=False, separators=(",", ":")),
            }
        ],
        "is_error": is_error,
    }


def build_ledger_mcp_server(ledger: FileLedger, project_id: str):
    @tool(
        "get_project_context",
        "Read the authoritative OPHIRA project, hypotheses, evidence, assessments, and decisions.",
        {},
    )
    async def get_project_context(_args: dict[str, Any]) -> dict[str, Any]:
        return _tool_result(ledger.get_project(project_id))

    @tool(
        "record_hypothesis",
        "Add a falsifiable hypothesis to the shared project ledger.",
        {
            "type": "object",
            "properties": {
                "dimension": {"type": "string", "enum": [item.value for item in Dimension]},
                "statement": {"type": "string", "minLength": 1},
                "validation_criteria": {"type": "array", "items": {"type": "string"}},
                "priority": {"type": "integer", "minimum": 1, "maximum": 5},
            },
            "required": ["dimension", "statement", "validation_criteria"],
            "additionalProperties": False,
        },
    )
    async def record_hypothesis(args: dict[str, Any]) -> dict[str, Any]:
        hypothesis = Hypothesis(
            dimension=Dimension(args["dimension"]),
            statement=args["statement"],
            validation_criteria=args["validation_criteria"],
            priority=args.get("priority", 3),
        )
        result: Hypothesis = hypothesis

        def mutate(project) -> None:
            nonlocal result
            duplicate = next(
                (
                    item
                    for item in project.hypotheses
                    if item.dimension == hypothesis.dimension
                    and item.statement.strip().casefold() == hypothesis.statement.strip().casefold()
                ),
                None,
            )
            if duplicate:
                result = duplicate
            else:
                project.hypotheses.append(hypothesis)

        ledger.update_project(project_id, mutate)
        return _tool_result(result)

    @tool(
        "record_evidence",
        "Record sourced evidence and label it as fact, user statement, "
        "community signal, or inference.",
        {
            "type": "object",
            "properties": {
                "dimension": {"type": "string", "enum": [item.value for item in Dimension]},
                "kind": {"type": "string", "enum": [item.value for item in EvidenceKind]},
                "claim": {"type": "string", "minLength": 1},
                "source_title": {"type": "string", "minLength": 1},
                "source_url": {"type": ["string", "null"]},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "supports_hypothesis_ids": {"type": "array", "items": {"type": "string"}},
                "recorded_by": {"type": "string", "minLength": 1},
            },
            "required": [
                "dimension",
                "kind",
                "claim",
                "source_title",
                "confidence",
                "supports_hypothesis_ids",
                "recorded_by",
            ],
            "additionalProperties": False,
        },
    )
    async def record_evidence(args: dict[str, Any]) -> dict[str, Any]:
        evidence = Evidence(
            dimension=Dimension(args["dimension"]),
            kind=EvidenceKind(args["kind"]),
            claim=args["claim"],
            source_title=args["source_title"],
            source_url=args.get("source_url") or None,
            confidence=args["confidence"],
            supports_hypothesis_ids=args["supports_hypothesis_ids"],
            recorded_by=args["recorded_by"],
        )
        result: Evidence = evidence

        def mutate(project) -> None:
            nonlocal result
            hypothesis_ids = {item.id for item in project.hypotheses}
            unknown = set(evidence.supports_hypothesis_ids) - hypothesis_ids
            if unknown:
                raise ValueError(f"unknown hypothesis ids: {sorted(unknown)}")
            duplicate = next(
                (
                    item
                    for item in project.evidence
                    if item.claim.strip().casefold() == evidence.claim.strip().casefold()
                    and str(item.source_url or "") == str(evidence.source_url or "")
                ),
                None,
            )
            if duplicate:
                result = duplicate
                return
            project.evidence.append(evidence)
            for hypothesis in project.hypotheses:
                if hypothesis.id in evidence.supports_hypothesis_ids:
                    hypothesis.evidence_ids.append(evidence.id)

        ledger.update_project(project_id, mutate)
        return _tool_result(result)

    @tool(
        "update_assessment",
        "Update one dimension's validation score, conclusion, evidence links, and unresolved gaps.",
        {
            "type": "object",
            "properties": {
                "dimension": {"type": "string", "enum": [item.value for item in Dimension]},
                "status": {"type": "string", "enum": [item.value for item in ValidationStatus]},
                "score": {"type": "integer", "minimum": 0, "maximum": 100},
                "summary": {"type": "string"},
                "gaps": {"type": "array", "items": {"type": "string"}},
                "evidence_ids": {"type": "array", "items": {"type": "string"}},
                "updated_by": {"type": "string", "minLength": 1},
            },
            "required": [
                "dimension",
                "status",
                "score",
                "summary",
                "gaps",
                "evidence_ids",
                "updated_by",
            ],
            "additionalProperties": False,
        },
    )
    async def update_assessment(args: dict[str, Any]) -> dict[str, Any]:
        dimension = Dimension(args["dimension"])
        assessment = DimensionAssessment(
            dimension=dimension,
            status=ValidationStatus(args["status"]),
            score=args["score"],
            summary=args["summary"],
            gaps=args["gaps"],
            evidence_ids=args["evidence_ids"],
            updated_by=args["updated_by"],
        )

        def mutate(project) -> None:
            evidence_ids = {item.id for item in project.evidence}
            unknown = set(assessment.evidence_ids) - evidence_ids
            if unknown:
                raise ValueError(f"unknown evidence ids: {sorted(unknown)}")
            project.assessments[dimension] = assessment

        ledger.update_project(project_id, mutate)
        return _tool_result(assessment)

    @tool(
        "record_stage_decision",
        "Record the Opportunity Lead's stage gate, weakest dimension, rationale, and next action.",
        {
            "type": "object",
            "properties": {
                "decision": {"type": "string", "enum": [item.value for item in StageDecisionValue]},
                "target_stage": {"type": "string", "enum": [item.value for item in ProjectStage]},
                "weakest_dimension": {"type": "string", "enum": [item.value for item in Dimension]},
                "rationale": {"type": "string", "minLength": 1},
                "next_actions": {"type": "array", "items": {"type": "string"}},
                "next_agent": {"type": ["string", "null"]},
                "made_by": {"type": "string", "minLength": 1},
            },
            "required": [
                "decision",
                "target_stage",
                "weakest_dimension",
                "rationale",
                "next_actions",
                "made_by",
            ],
            "additionalProperties": False,
        },
    )
    async def record_stage_decision(args: dict[str, Any]) -> dict[str, Any]:
        result: StageDecision | None = None

        def mutate(project) -> None:
            nonlocal result
            decision = StageDecision(
                decision=StageDecisionValue(args["decision"]),
                current_stage=project.stage,
                target_stage=ProjectStage(args["target_stage"]),
                weakest_dimension=Dimension(args["weakest_dimension"]),
                rationale=args["rationale"],
                next_actions=args["next_actions"],
                next_agent=args.get("next_agent"),
                made_by=args["made_by"],
            )
            project.stage_decisions.append(decision)
            if decision.decision == StageDecisionValue.ADVANCE:
                project.stage = decision.target_stage
            elif decision.decision == StageDecisionValue.STOP:
                project.stage = ProjectStage.STOPPED
            result = decision

        ledger.update_project(project_id, mutate)
        return _tool_result(result)

    return create_sdk_mcp_server(
        name=MCP_SERVER_NAME,
        version="0.1.0",
        tools=[
            get_project_context,
            record_hypothesis,
            record_evidence,
            update_assessment,
            record_stage_decision,
        ],
    )
