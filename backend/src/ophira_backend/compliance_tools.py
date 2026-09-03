"""Public-source compliance lookup. Does not replace legal advice."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from .constants import COMPLIANCE_MCP_SERVER_NAME

CATALOG = [
    {
        "market": "EU",
        "topic": "CE / GPSR product safety",
        "summary": (
            "Consumer products sold in the EU generally need a responsible person, "
            "technical file, and GPSR-compliant labelling."
        ),
        "required_documents": ["Declaration of Conformity", "technical file", "instructions"],
        "source_url": "https://single-market-economy.ec.europa.eu/sectors/toys/toy-safety_en",
        "effective_date": "2024-12-13",
        "claim_language": "Do not claim CE as a quality mark; it is a conformity marking.",
    },
    {
        "market": "US",
        "topic": "CPSIA / FDA food contact",
        "summary": (
            "Children's products need CPSIA tracking; food-contact materials may "
            "need FDA 21 CFR compliance evidence."
        ),
        "required_documents": ["CPC", "test reports", "tracking labels"],
        "source_url": "https://www.cpsc.gov/Business--Manufacturing/Business-Education/childrens-products",
        "effective_date": "2008-08-14",
        "claim_language": "Do not claim FDA approved unless a specific FDA listing exists.",
    },
    {
        "market": "UK",
        "topic": "UKCA / labelling",
        "summary": (
            "Many goods still accept CE during transitional rules; UKCA may be "
            "required for GB-only placement."
        ),
        "required_documents": ["declaration", "technical documentation"],
        "source_url": "https://www.gov.uk/guidance/using-the-ukca-marking",
        "effective_date": "2023-01-01",
        "claim_language": "State the actual marking used; do not mix CE and UKCA claims.",
    },
    {
        "market": "CN-export",
        "topic": "commercial invoice / packing list / COO",
        "summary": (
            "Cross-border shipments typically need commercial invoice, packing list, "
            "and origin documentation matching the HS code."
        ),
        "required_documents": ["commercial invoice", "packing list", "certificate of origin"],
        "source_url": "https://www.customs.gov.cn/",
        "effective_date": "2021-01-01",
        "claim_language": "Origin claims must match the issued certificate.",
    },
]


def _result(value: Any, *, is_error: bool = False) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}],
        "is_error": is_error,
    }


def load_catalog() -> list[dict[str, Any]]:
    return CATALOG


def build_compliance_mcp_server():
    @tool(
        "lookup_requirements",
        "Look up bundled export/import document and claim-language requirements "
        "with source and effective date. Does not replace legal advice or a live "
        "customs ruling.",
        {
            "type": "object",
            "properties": {
                "market": {"type": "string", "minLength": 2, "maxLength": 80},
                "topic": {"type": "string", "minLength": 2, "maxLength": 120},
            },
            "required": ["market", "topic"],
            "additionalProperties": False,
        },
    )
    async def lookup_requirements(args: dict[str, Any]) -> dict[str, Any]:
        market = args["market"].casefold()
        topic = args["topic"].casefold()
        hits = []
        for row in load_catalog():
            haystack = " ".join(
                str(row.get(key, "")) for key in ("market", "topic", "summary")
            ).casefold()
            if market in haystack or topic in haystack:
                hits.append(row)
        return _result(
            {
                "retrieved_at": datetime.now(UTC).isoformat(),
                "matches": hits[:20],
                "disclaimer": "Bundled public-source notes only; not a legal opinion.",
            }
        )

    return create_sdk_mcp_server(
        name=COMPLIANCE_MCP_SERVER_NAME,
        version="1.0.0",
        tools=[lookup_requirements],
    )
