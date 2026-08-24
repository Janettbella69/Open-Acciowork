from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from .constants import (
    COMMERCE_MATH_MCP_SERVER_NAME,
    CUSTOMER_MCP_SERVER_NAME,
    LEDGER_MCP_SERVER_NAME,
    MARKET_MCP_SERVER_NAME,
    SUPPLIER_MCP_SERVER_NAME,
    USER_SCOPE_ID,
    WEB_MCP_SERVER_NAME,
    DecisionStatus,
    Dimension,
    EvidenceKind,
    EvidenceSourceType,
    EvidenceStatus,
    MemoryKind,
    MemoryScope,
    MemorySensitivity,
    MemorySource,
)
from .memory import MemoryProvider, gather_approved
from .models import Decision, Evidence, Hypothesis
from .storage import FileLedger


def _tool_result(value: Any, *, is_error: bool = False) -> dict[str, Any]:
    if isinstance(value, list):
        value = [
            item.model_dump(mode="json") if hasattr(item, "model_dump") else item
            for item in value
        ]
    elif hasattr(value, "model_dump"):
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


def _decimal(value: Any, name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not result.is_finite():
        raise ValueError(f"{name} must be finite")
    return result


def _money(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.0001")))


def build_ledger_tools(
    ledger: FileLedger,
    project_id: str,
    memory: MemoryProvider | None = None,
    agent_id: str | None = None,
):
    """Tool list for the ledger MCP server; exposed separately so tests can call
    handlers directly without going through a live SDK session."""
    @tool(
        "read_project_context",
        "Read the authoritative OPHIRA project, evidence, decisions, artifacts, and legacy views.",
        {},
    )
    async def read_project_context(_args: dict[str, Any]) -> dict[str, Any]:
        return _tool_result(ledger.get_project(project_id))

    @tool(
        "search_evidence",
        "Search durable project evidence by text, kind, or tag without changing project truth.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "kind": {"type": ["string", "null"], "enum": [
                    *[item.value for item in EvidenceKind],
                    None,
                ]},
                "tag": {"type": ["string", "null"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            "required": [],
            "additionalProperties": False,
        },
    )
    async def search_evidence(args: dict[str, Any]) -> dict[str, Any]:
        kind = args.get("kind")
        matches = ledger.search_evidence(
            project_id,
            query=str(args.get("query") or ""),
            kind=EvidenceKind(kind) if kind else None,
            tag=args.get("tag"),
            limit=args.get("limit", 20),
        )
        return _tool_result(matches)

    @tool(
        "update_evidence_status",
        "Mark project evidence active, stale, or removed. Decisions citing stale or "
        "removed evidence are flagged needs_review automatically (FR-10).",
        {
            "type": "object",
            "properties": {
                "evidence_id": {"type": "string", "minLength": 1},
                "status": {
                    "type": "string",
                    "enum": [item.value for item in EvidenceStatus],
                },
            },
            "required": ["evidence_id", "status"],
            "additionalProperties": False,
        },
    )
    async def update_evidence_status(args: dict[str, Any]) -> dict[str, Any]:
        from .storage import LedgerNotFoundError

        try:
            evidence, affected = ledger.update_evidence_status(
                project_id, args["evidence_id"], EvidenceStatus(args["status"])
            )
        except LedgerNotFoundError:
            return _tool_result(
                {"error": f"unknown evidence id: {args['evidence_id']}"}, is_error=True
            )
        return _tool_result(
            {
                "evidence": evidence.model_dump(mode="json"),
                "affected_decision_ids": affected,
            }
        )

    @tool(
        "record_hypothesis",
        "Add a falsifiable hypothesis to project truth; dimensions are optional tags, not stages.",
        {
            "type": "object",
            "properties": {
                "statement": {"type": "string", "minLength": 1},
                "validation_criteria": {"type": "array", "items": {"type": "string"}},
                "priority": {"type": "integer", "minimum": 1, "maximum": 5},
                "dimension": {"type": ["string", "null"], "enum": [
                    *[item.value for item in Dimension],
                    None,
                ]},
                "tags": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["statement", "validation_criteria"],
            "additionalProperties": False,
        },
    )
    async def record_hypothesis(args: dict[str, Any]) -> dict[str, Any]:
        hypothesis = Hypothesis(
            dimension=Dimension(args["dimension"]) if args.get("dimension") else None,
            tags=args.get("tags", []),
            statement=args["statement"],
            validation_criteria=args["validation_criteria"],
            priority=args.get("priority", 3),
        )
        result = hypothesis

        def mutate(project) -> None:
            nonlocal result
            duplicate = next(
                (
                    item
                    for item in project.hypotheses
                    if item.statement.strip().casefold()
                    == hypothesis.statement.strip().casefold()
                ),
                None,
            )
            if duplicate:
                result = duplicate
            else:
                project.hypotheses.append(hypothesis)

        ledger.update_project(project_id, mutate)
        return _tool_result(result)

    evidence_schema = {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": [item.value for item in EvidenceKind]},
            "claim": {"type": "string", "minLength": 1},
            "source_title": {"type": "string", "minLength": 1},
            "source_url": {"type": ["string", "null"]},
            "source_type": {
                "type": "string",
                "enum": [item.value for item in EvidenceSourceType],
            },
            "source_path": {"type": ["string", "null"]},
            "observed_at": {"type": ["string", "null"], "format": "date-time"},
            "excerpt": {"type": ["string", "null"], "maxLength": 8000},
            "location": {"type": ["string", "null"]},
            "freshness_note": {"type": ["string", "null"]},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "supports_hypothesis_ids": {"type": "array", "items": {"type": "string"}},
            "recorded_by": {"type": "string", "minLength": 1},
            "dimension": {"type": ["string", "null"], "enum": [
                *[item.value for item in Dimension],
                None,
            ]},
            "tags": {"type": "array", "items": {"type": "string"}},
        },
        "required": [
            "kind",
            "claim",
            "source_title",
            "source_type",
            "confidence",
            "supports_hypothesis_ids",
            "recorded_by",
        ],
        "additionalProperties": False,
    }

    def record_one_evidence(args: dict[str, Any]) -> Evidence:
        observed_at = args.get("observed_at")
        evidence = Evidence(
            dimension=Dimension(args["dimension"]) if args.get("dimension") else None,
            tags=args.get("tags", []),
            kind=EvidenceKind(args["kind"]),
            claim=args["claim"],
            source_title=args["source_title"],
            source_url=args.get("source_url") or None,
            source_type=EvidenceSourceType(args["source_type"]),
            source_path=args.get("source_path"),
            observed_at=datetime.fromisoformat(observed_at) if observed_at else None,
            excerpt=args.get("excerpt"),
            location=args.get("location"),
            freshness_note=args.get("freshness_note"),
            confidence=args["confidence"],
            supports_hypothesis_ids=args["supports_hypothesis_ids"],
            recorded_by=args["recorded_by"],
        )
        result = evidence

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
                    and str(item.source_url or item.source_path or "")
                    == str(evidence.source_url or evidence.source_path or "")
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
        return result

    @tool(
        "record_evidence",
        "Record sourced evidence with retrieval time, source location, "
        "evidence type, and confidence.",
        evidence_schema,
    )
    async def record_evidence(args: dict[str, Any]) -> dict[str, Any]:
        return _tool_result(record_one_evidence(args))

    @tool(
        "record_evidence_batch",
        "Record up to 20 sourced evidence entries in one authorized call. Same "
        "per-entry rules as record_evidence; duplicates are merged idempotently.",
        {
            "type": "object",
            "properties": {
                "entries": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 20,
                    "items": evidence_schema,
                }
            },
            "required": ["entries"],
            "additionalProperties": False,
        },
    )
    async def record_evidence_batch(args: dict[str, Any]) -> dict[str, Any]:
        return _tool_result([record_one_evidence(entry) for entry in args["entries"]])

    @tool(
        "record_decision",
        "Record a traceable Go, Test, No-go, ranking, or plan decision linked to evidence.",
        {
            "type": "object",
            "properties": {
                "question": {"type": "string", "minLength": 1},
                "outcome": {"type": "string", "minLength": 1},
                "decision_type": {
                    "type": "string",
                    "enum": ["go", "test", "no_go", "ranking", "plan", "other"],
                },
                "rationale": {"type": "string"},
                "evidence_ids": {"type": "array", "items": {"type": "string"}},
                "hypothesis_ids": {"type": "array", "items": {"type": "string"}},
                "assumptions": {"type": "array", "items": {"type": "string"}},
                "conditions": {"type": "array", "items": {"type": "string"}},
                "next_actions": {"type": "array", "items": {"type": "string"}},
                "made_by": {"type": "string", "minLength": 1},
            },
            "required": [
                "question",
                "outcome",
                "decision_type",
                "rationale",
                "evidence_ids",
                "hypothesis_ids",
                "assumptions",
                "conditions",
                "next_actions",
                "made_by",
            ],
            "additionalProperties": False,
        },
    )
    async def record_decision(args: dict[str, Any]) -> dict[str, Any]:
        decision = Decision(**args, status=DecisionStatus.ACTIVE)

        def mutate(project) -> None:
            known_evidence = {item.id for item in project.evidence}
            known_hypotheses = {item.id for item in project.hypotheses}
            unknown_evidence = set(decision.evidence_ids) - known_evidence
            unknown_hypotheses = set(decision.hypothesis_ids) - known_hypotheses
            if unknown_evidence or unknown_hypotheses:
                raise ValueError(
                    f"unknown references: evidence={sorted(unknown_evidence)}, "
                    f"hypotheses={sorted(unknown_hypotheses)}"
                )
            project.decisions.append(decision)

        ledger.update_project(project_id, mutate)
        return _tool_result(decision)

    tools = [
        read_project_context,
        search_evidence,
        record_hypothesis,
        record_evidence,
        record_evidence_batch,
        record_decision,
        update_evidence_status,
    ]

    if memory is not None:

        @tool(
            "propose_memory",
            "Propose durable memory. It becomes active only after explicit user approval.",
            {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "minLength": 1, "maxLength": 4000},
                    "kind": {"type": "string", "enum": [item.value for item in MemoryKind]},
                    "sensitivity": {
                        "type": "string",
                        "enum": [item.value for item in MemorySensitivity],
                    },
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
                "required": ["text", "kind"],
                "additionalProperties": False,
            },
        )
        async def propose_memory(args: dict[str, Any]) -> dict[str, Any]:
            scope = MemoryScope.AGENT if agent_id else MemoryScope.PROJECT
            scope_id = agent_id or project_id
            item = memory.propose(
                scope=scope,
                scope_id=scope_id,
                text=args["text"],
                kind=MemoryKind(args["kind"]),
                source=MemorySource.AGENT_PROPOSAL,
                sensitivity=(
                    MemorySensitivity(args["sensitivity"])
                    if args.get("sensitivity")
                    else None
                ),
                confidence=args.get("confidence", 1.0),
                created_by=agent_id or "agent",
            )
            return _tool_result(item)

        @tool(
            "search_memory",
            "Search only user-approved memories for current scopes.",
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                },
                "required": [],
                "additionalProperties": False,
            },
        )
        async def search_memory(args: dict[str, Any]) -> dict[str, Any]:
            pairs = [(MemoryScope.USER, USER_SCOPE_ID), (MemoryScope.PROJECT, project_id)]
            if agent_id:
                pairs.append((MemoryScope.AGENT, agent_id))
            return _tool_result(
                gather_approved(
                    memory,
                    pairs,
                    query=args.get("query") or None,
                    limit=args.get("limit", 8),
                )
            )

        tools.extend([propose_memory, search_memory])

    return tools


def build_ledger_mcp_server(
    ledger: FileLedger,
    project_id: str,
    memory: MemoryProvider | None = None,
    agent_id: str | None = None,
):
    return create_sdk_mcp_server(
        name=LEDGER_MCP_SERVER_NAME,
        version="1.0.0",
        tools=build_ledger_tools(ledger, project_id, memory=memory, agent_id=agent_id),
    )


def filter_market_signals(
    ledger: FileLedger,
    project_id: str,
    *,
    query: str = "",
    signal_type: str = "",
    limit: int = 20,
) -> list:
    """Read-only view over evidence tagged market-signal; limit applies post-filter."""
    candidates = ledger.search_evidence(
        project_id, query=query, tag="market-signal", limit=max(limit * 5, 100)
    )
    needle = signal_type.strip().casefold()
    if needle:
        candidates = [
            item
            for item in candidates
            if needle in {value.casefold() for value in item.tags}
        ]
    return candidates[:limit]


def build_market_mcp_server(ledger: FileLedger, project_id: str):
    @tool(
        "record_market_signal",
        "Record one public market signal with source and observation time; "
        "does not decide viability.",
        {
            "type": "object",
            "properties": {
                "claim": {"type": "string", "minLength": 1},
                "source_title": {"type": "string", "minLength": 1},
                "source_url": {"type": "string", "minLength": 1},
                "observed_at": {"type": ["string", "null"], "format": "date-time"},
                "excerpt": {"type": ["string", "null"]},
                "signal_type": {"type": "string", "minLength": 1},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            },
            "required": [
                "claim",
                "source_title",
                "source_url",
                "signal_type",
                "confidence",
            ],
            "additionalProperties": False,
        },
    )
    async def record_market_signal(args: dict[str, Any]) -> dict[str, Any]:
        evidence = Evidence(
            tags=["market-signal", args["signal_type"]],
            kind=EvidenceKind.COMMUNITY_SIGNAL,
            claim=args["claim"],
            source_title=args["source_title"],
            source_url=args["source_url"],
            source_type=EvidenceSourceType.WEB,
            observed_at=(
                datetime.fromisoformat(args["observed_at"]) if args.get("observed_at") else None
            ),
            excerpt=args.get("excerpt"),
            confidence=args["confidence"],
            recorded_by="ophira-market-mcp",
        )
        ledger.update_project(project_id, lambda project: project.evidence.append(evidence))
        return _tool_result(evidence)

    @tool(
        "search_market_signals",
        "Search market signals already recorded in this project's evidence ledger "
        "by text or signal type. Returns sourced records with retrieval times; "
        "never live platform data and never a viability judgment.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "signal_type": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            "required": [],
            "additionalProperties": False,
        },
    )
    async def search_market_signals(args: dict[str, Any]) -> dict[str, Any]:
        return _tool_result(
            filter_market_signals(
                ledger,
                project_id,
                query=str(args.get("query") or ""),
                signal_type=str(args.get("signal_type") or ""),
                limit=args.get("limit", 20),
            )
        )

    return create_sdk_mcp_server(
        name=MARKET_MCP_SERVER_NAME,
        version="1.0.0",
        tools=[record_market_signal, search_market_signals],
    )


def build_supplier_mcp_server():
    @tool(
        "compare_supplier_quotes",
        "Normalize supplier quotes and compute comparable landed unit cost; "
        "does not choose a winner.",
        {
            "type": "object",
            "properties": {
                "quotes": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 100,
                    "items": {
                        "type": "object",
                        "properties": {
                            "supplier": {"type": "string", "minLength": 1},
                            "unit_price": {"type": "number", "minimum": 0},
                            "quantity": {"type": "integer", "minimum": 1},
                            "shipping_total": {"type": "number", "minimum": 0},
                            "duty_total": {"type": "number", "minimum": 0},
                            "other_total": {"type": "number", "minimum": 0},
                            "currency": {"type": "string", "minLength": 3},
                            "lead_days": {"type": ["integer", "null"], "minimum": 0},
                            "source": {"type": "string", "minLength": 1},
                        },
                        "required": [
                            "supplier",
                            "unit_price",
                            "quantity",
                            "shipping_total",
                            "duty_total",
                            "other_total",
                            "currency",
                            "source",
                        ],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["quotes"],
            "additionalProperties": False,
        },
    )
    async def compare_supplier_quotes(args: dict[str, Any]) -> dict[str, Any]:
        normalized = []
        for quote in args["quotes"]:
            quantity = _decimal(quote["quantity"], "quantity")
            landed_total = (
                _decimal(quote["unit_price"], "unit_price") * quantity
                + _decimal(quote["shipping_total"], "shipping_total")
                + _decimal(quote["duty_total"], "duty_total")
                + _decimal(quote["other_total"], "other_total")
            )
            normalized.append(
                {
                    **quote,
                    "landed_total": _money(landed_total),
                    "landed_unit_cost": _money(landed_total / quantity),
                    "warnings": [] if quote.get("lead_days") is not None else ["lead_days_missing"],
                }
            )
        return _tool_result({"quotes": normalized, "assumptions": ["No FX conversion applied"]})

    return create_sdk_mcp_server(
        name=SUPPLIER_MCP_SERVER_NAME,
        version="1.0.0",
        tools=[compare_supplier_quotes],
    )


def build_customer_mcp_server():
    @tool(
        "rank_customer_candidates",
        "Apply explicit weights to sourced customer candidates; "
        "returns scores, not a sales conclusion.",
        {
            "type": "object",
            "properties": {
                "fit_weight": {"type": "number", "minimum": 0},
                "access_weight": {"type": "number", "minimum": 0},
                "candidates": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 200,
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "minLength": 1},
                            "fit_score": {"type": "number", "minimum": 0, "maximum": 100},
                            "access_score": {"type": "number", "minimum": 0, "maximum": 100},
                            "source": {"type": "string", "minLength": 1},
                        },
                        "required": ["name", "fit_score", "access_score", "source"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["fit_weight", "access_weight", "candidates"],
            "additionalProperties": False,
        },
    )
    async def rank_customer_candidates(args: dict[str, Any]) -> dict[str, Any]:
        fit_weight = _decimal(args["fit_weight"], "fit_weight")
        access_weight = _decimal(args["access_weight"], "access_weight")
        total_weight = fit_weight + access_weight
        if total_weight <= 0:
            raise ValueError("at least one weight must be positive")
        ranked = []
        for candidate in args["candidates"]:
            score = (
                _decimal(candidate["fit_score"], "fit_score") * fit_weight
                + _decimal(candidate["access_score"], "access_score") * access_weight
            ) / total_weight
            ranked.append({**candidate, "weighted_score": _money(score)})
        ranked.sort(key=lambda item: item["weighted_score"], reverse=True)
        return _tool_result({"candidates": ranked, "method": "explicit weighted score"})

    return create_sdk_mcp_server(
        name=CUSTOMER_MCP_SERVER_NAME,
        version="1.0.0",
        tools=[rank_customer_candidates],
    )


def build_commerce_math_mcp_server():
    @tool(
        "calculate_landed_cost",
        "Calculate deterministic total and per-unit landed cost from explicit inputs.",
        {
            "type": "object",
            "properties": {
                "quantity": {"type": "integer", "minimum": 1},
                "unit_price": {"type": "number", "minimum": 0},
                "freight": {"type": "number", "minimum": 0},
                "insurance": {"type": "number", "minimum": 0},
                "duty_rate": {"type": "number", "minimum": 0},
                "tax_rate": {"type": "number", "minimum": 0},
                "other_costs": {"type": "number", "minimum": 0},
                "currency": {"type": "string", "minLength": 3},
            },
            "required": [
                "quantity",
                "unit_price",
                "freight",
                "insurance",
                "duty_rate",
                "tax_rate",
                "other_costs",
                "currency",
            ],
            "additionalProperties": False,
        },
    )
    async def calculate_landed_cost(args: dict[str, Any]) -> dict[str, Any]:
        quantity = _decimal(args["quantity"], "quantity")
        goods = quantity * _decimal(args["unit_price"], "unit_price")
        freight = _decimal(args["freight"], "freight")
        insurance = _decimal(args["insurance"], "insurance")
        dutiable = goods + freight + insurance
        duty = dutiable * _decimal(args["duty_rate"], "duty_rate")
        tax = (dutiable + duty) * _decimal(args["tax_rate"], "tax_rate")
        total = duty + tax + dutiable + _decimal(args["other_costs"], "other_costs")
        return _tool_result(
            {
                "currency": args["currency"],
                "goods_cost": _money(goods),
                "duty": _money(duty),
                "tax": _money(tax),
                "landed_total": _money(total),
                "landed_unit_cost": _money(total / quantity),
                "inputs": args,
            }
        )

    @tool(
        "calculate_unit_economics",
        "Calculate deterministic contribution margin and break-even units from explicit inputs.",
        {
            "type": "object",
            "properties": {
                "selling_price": {"type": "number", "minimum": 0},
                "landed_unit_cost": {"type": "number", "minimum": 0},
                "channel_fee_rate": {"type": "number", "minimum": 0},
                "fulfillment_per_unit": {"type": "number", "minimum": 0},
                "acquisition_per_unit": {"type": "number", "minimum": 0},
                "refund_rate": {"type": "number", "minimum": 0, "maximum": 1},
                "fixed_costs": {"type": "number", "minimum": 0},
                "currency": {"type": "string", "minLength": 3},
            },
            "required": [
                "selling_price",
                "landed_unit_cost",
                "channel_fee_rate",
                "fulfillment_per_unit",
                "acquisition_per_unit",
                "refund_rate",
                "fixed_costs",
                "currency",
            ],
            "additionalProperties": False,
        },
    )
    async def calculate_unit_economics(args: dict[str, Any]) -> dict[str, Any]:
        price = _decimal(args["selling_price"], "selling_price")
        net_revenue = price * (Decimal("1") - _decimal(args["refund_rate"], "refund_rate"))
        variable = (
            _decimal(args["landed_unit_cost"], "landed_unit_cost")
            + price * _decimal(args["channel_fee_rate"], "channel_fee_rate")
            + _decimal(args["fulfillment_per_unit"], "fulfillment_per_unit")
            + _decimal(args["acquisition_per_unit"], "acquisition_per_unit")
        )
        contribution = net_revenue - variable
        fixed = _decimal(args["fixed_costs"], "fixed_costs")
        break_even = (
            None
            if contribution <= 0
            else int(
                (fixed / contribution).to_integral_value(rounding="ROUND_CEILING")
            )
        )
        return _tool_result(
            {
                "currency": args["currency"],
                "net_revenue_per_unit": _money(net_revenue),
                "variable_cost_per_unit": _money(variable),
                "contribution_per_unit": _money(contribution),
                "contribution_margin_rate": _money(contribution / price) if price else None,
                "break_even_units": break_even,
                "inputs": args,
            }
        )

    return create_sdk_mcp_server(
        name=COMMERCE_MATH_MCP_SERVER_NAME,
        version="1.0.0",
        tools=[calculate_landed_cost, calculate_unit_economics],
    )


def build_mcp_servers(
    ledger: FileLedger,
    project_id: str,
    memory: MemoryProvider | None = None,
    agent_id: str | None = None,
    settings: Any | None = None,
) -> dict[str, Any]:
    servers = {
        LEDGER_MCP_SERVER_NAME: build_ledger_mcp_server(
            ledger,
            project_id,
            memory=memory,
            agent_id=agent_id,
        ),
        MARKET_MCP_SERVER_NAME: build_market_mcp_server(ledger, project_id),
        SUPPLIER_MCP_SERVER_NAME: build_supplier_mcp_server(),
        CUSTOMER_MCP_SERVER_NAME: build_customer_mcp_server(),
        COMMERCE_MATH_MCP_SERVER_NAME: build_commerce_math_mcp_server(),
    }
    if settings is not None:
        from .web_tools import build_web_mcp_server

        servers[WEB_MCP_SERVER_NAME] = build_web_mcp_server(settings)
    return servers
