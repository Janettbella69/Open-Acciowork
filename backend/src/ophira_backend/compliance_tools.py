"""Official compliance source locators, not a current ruling for a product."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from .constants import COMPLIANCE_MCP_SERVER_NAME

CATALOG_CHECKED_AT = "2026-09-25"

# A catalog hit only points to a source. No product, use, destination, or rule
# version can be established from these bundled notes alone.
CATALOG = [
    {
        "market": "EU",
        "topic": "CE marking applicability",
        "search_terms": ("CE", "CE marking", "conformity marking"),
        "summary": (
            "CE marking is limited to product categories covered by EU rules that "
            "require it. Identify the product-specific law before listing documents "
            "or making a conformity claim."
        ),
        "source_url": (
            "https://europa.eu/youreurope/business/product-rules-compliance/"
            "general-product-compliance/ce-marking/index_en.htm"
        ),
        "check": "Which harmonised product rules, if any, apply to this exact product?",
    },
    {
        "market": "EU",
        "topic": "General Product Safety Regulation (GPSR)",
        "search_terms": ("GPSR", "general product safety", "consumer safety"),
        "summary": (
            "The GPSR applies from 13 December 2024. Check its scope and the "
            "obligations for the product and economic operator; CE status alone "
            "does not settle those questions."
        ),
        "source_url": "https://eur-lex.europa.eu/eli/reg/2023/988/oj/eng",
        "effective_date": "2024-12-13",
        "check": "Which GPSR provisions apply to the product and sales channel?",
    },
    {
        "market": "US",
        "topic": "Children's Product Certificate (CPC)",
        "search_terms": ("CPC", "CPSIA", "children's certificate", "toy certification"),
        "summary": (
            "CPSC testing and certification depend on the product's intended age "
            "group and applicable rules. Check the current certificate and "
            "import-filing guidance for the specific product."
        ),
        "source_url": "https://www.cpsc.gov/FAQ/CPC",
        "check": "Is this a children's product subject to a CPSC-enforced safety rule?",
    },
    {
        "market": "US",
        "topic": "Children's product tracking labels",
        "search_terms": ("tracking label", "tracking labels", "children's label"),
        "summary": (
            "CPSC tracking-label guidance addresses products designed or intended "
            "primarily for children 12 or younger. Verify the product's age group, "
            "label content and placement against the current guidance."
        ),
        "source_url": "https://www.cpsc.gov/FAQ/Tracking-Label",
        "check": "Is this a children's product within the tracking-label rule?",
    },
    {
        "market": "US",
        "topic": "FDA food-contact material status",
        "search_terms": ("food contact", "food-contact", "food packaging", "FCN"),
        "summary": (
            "Food-contact status depends on each component, its intended use and "
            "conditions of use. An FDA listing or notification is not a blanket "
            "approval of a finished article."
        ),
        "source_url": (
            "https://www.fda.gov/food/packaging-food-contact-substances-fcs/"
            "determining-regulatory-status-components-food-contact-material"
        ),
        "check": "Which materials, food types and use conditions need verification?",
    },
    {
        "market": "UK",
        "topic": "UKCA and CE product marking by sector",
        "search_terms": ("UKCA", "CE", "product marking", "conformity marking"),
        "summary": (
            "Marking rules differ by product sector and between Great Britain and "
            "Northern Ireland. Check the current government sector table before "
            "claiming that CE or UKCA is required or accepted."
        ),
        "source_url": (
            "https://www.gov.uk/government/publications/"
            "product-regulations-by-sector-and-current-approaches-to-product-marking-"
            "ukca-and-ce-regimes/product-regulations-by-sector-and-current-approaches-"
            "to-product-marking-ukca-and-ce-regimes"
        ),
        "check": "Which sector, territory (GB or NI) and placement date apply?",
    },
]

_MARKET_ALIASES = {
    "eu": "EU", "european union": "EU",
    "us": "US", "usa": "US", "united states": "US",
    "uk": "UK", "gb": "UK", "great britain": "UK",
    "ni": "UK", "northern ireland": "UK",
}


def load_catalog() -> list[dict[str, Any]]:
    return CATALOG


def _topic_matches(topic: str, term: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(term.casefold())}(?!\w)", topic) is not None


def lookup_requirements_impl(market: str, topic: str) -> dict[str, Any]:
    normalized_market = _MARKET_ALIASES.get(market.strip().casefold())
    normalized_topic = topic.strip().casefold()
    if not normalized_topic:
        return {"error_code": "INVALID_LOOKUP", "error": "Topic is required."}
    hits = []
    if normalized_market is not None:
        for row in load_catalog():
            if row["market"] != normalized_market or not any(
                _topic_matches(normalized_topic, term) for term in row["search_terms"]
            ):
                continue
            hits.append({
                key: value for key, value in row.items() if key != "search_terms"
            })
    return {
        "retrieved_at": datetime.now(UTC).isoformat(),
        "catalog_checked_at": CATALOG_CHECKED_AT,
        "market": normalized_market or market.strip(),
        "topic": topic.strip(),
        "matches": hits,
        "coverage": "source_locators" if hits else "no_catalog_match",
        "requires_live_verification": True,
        "note": (
            "Bundled official-source locators only. Retrieved_at is the local lookup "
            "time, not a regulatory update. Open the original source and verify "
            "product, territory, use, date and current text before any conclusion. "
            "No catalog match does not mean there is no requirement."
        ),
    }


def build_compliance_mcp_server():
    @tool(
        "lookup_requirements",
        "Locate bundled official sources for a market and topic. Search is scoped "
        "to both inputs; a match is not a current or product-specific ruling. "
        "Open the source and verify scope and date before any compliance claim. "
        "Not a substitute for legal judgement.",
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
        result = lookup_requirements_impl(args["market"], args["topic"])
        return {
            "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
            "is_error": "error_code" in result,
        }

    return create_sdk_mcp_server(
        name=COMPLIANCE_MCP_SERVER_NAME,
        version="1.0.0",
        tools=[lookup_requirements],
    )
