"""Bundled compliance lookups must be scoped and honest about freshness."""

import json

import pytest
from mcp_test_client import call_sdk_tool

from ophira_backend.compliance_tools import (
    build_compliance_mcp_server,
    lookup_requirements_impl,
)


def test_market_and_topic_must_both_match() -> None:
    eu = lookup_requirements_impl("EU", "CE / GPSR")
    assert {row["topic"] for row in eu["matches"]} == {
        "CE marking applicability",
        "General Product Safety Regulation (GPSR)",
    }
    assert all(row["market"] == "EU" for row in eu["matches"])
    assert eu["requires_live_verification"] is True
    assert eu["catalog_checked_at"] != eu["retrieved_at"]

    us = lookup_requirements_impl("United States", "FDA food contact")
    assert [row["topic"] for row in us["matches"]] == [
        "FDA food-contact material status"
    ]
    assert "effective_date" not in us["matches"][0]
    assert "required_documents" not in us["matches"][0]

    tracking = lookup_requirements_impl("US", "tracking label")
    assert [row["source_url"] for row in tracking["matches"]] == [
        "https://www.cpsc.gov/FAQ/Tracking-Label"
    ]


def test_uncovered_market_or_topic_does_not_return_unrelated_rules() -> None:
    assert lookup_requirements_impl("CN-export", "commercial invoice")["coverage"] == (
        "no_catalog_match"
    )
    assert lookup_requirements_impl("EU", "food contact")["matches"] == []
    assert lookup_requirements_impl("US", "device")["matches"] == []
    assert lookup_requirements_impl("US", "  ")["error_code"] == "INVALID_LOOKUP"


@pytest.mark.asyncio
async def test_mcp_exposes_source_locator_with_scope() -> None:
    server = build_compliance_mcp_server()
    response = await call_sdk_tool(
        server["instance"], "lookup_requirements", {"market": "GB", "topic": "UKCA"}
    )
    assert response.is_error is False
    payload = json.loads(response.content[0].text)
    assert payload["coverage"] == "source_locators"
    assert [row["market"] for row in payload["matches"]] == ["UK"]
    assert payload["requires_live_verification"] is True
