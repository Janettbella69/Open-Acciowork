"""ECB observations must stay dated, same-series, and attributable to the source."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from ophira_backend.config import Settings
from ophira_backend.constants import (
    FX_MCP_SERVER_NAME,
    OPHIRA_TOOL_NAMES,
    READ_ONLY_MCP_TOOL_NAMES,
    TOOL_GET_ECB_FX_REFERENCE,
)
from ophira_backend.fx_rates import (
    ECB_CSV_URL,
    MAX_ECB_CSV_BYTES,
    build_fx_mcp_server,
    get_ecb_fx_reference_impl,
)
from ophira_backend.models import ProjectCreate
from ophira_backend.sdk_adapter import _post_tool_untrusted_source
from ophira_backend.source_receipts import attach_source_receipts
from ophira_backend.storage import FileLedger
from ophira_backend.untrusted import UntrustedSource

NOW = datetime(2026, 9, 24, 17, tzinfo=UTC)
HEADER = (
    "KEY,FREQ,CURRENCY,CURRENCY_DENOM,EXR_TYPE,EXR_SUFFIX,TIME_PERIOD,OBS_VALUE\n"
)


def row(currency: str, date: str, rate: str) -> str:
    return f"EXR.D.{currency}.EUR.SP00.A,D,{currency},EUR,SP00,A,{date},{rate}\n"


def feed(*rows: str) -> str:
    return HEADER + "".join(rows)


@pytest.fixture(autouse=True)
def public_ecb_dns(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "ophira_backend.web_tools._resolve_ips", lambda _host: ["93.184.216.34"]
    )


def transport_for(
    body: str,
    *,
    status: int = 200,
    content_type: str = "text/csv",
) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "93.184.216.34"
        assert request.headers["Host"] == "data-api.ecb.europa.eu"
        assert request.extensions["sni_hostname"] == "data-api.ecb.europa.eu"
        assert request.url.path == "/service/data/EXR/D..EUR.SP00.A"
        return httpx.Response(
            status, headers={"content-type": content_type}, text=body
        )

    return httpx.MockTransport(handle)


@pytest.mark.asyncio
async def test_cross_rate_uses_same_date_eur_observations_and_fixed_source(
    tmp_path: Path,
) -> None:
    result = await get_ecb_fx_reference_impl(
        Settings(data_dir=tmp_path),
        "usd",
        "CNY",
        transport=transport_for(
            feed(
                row("USD", "2026-09-24", "1.1367"),
                row("CNY", "2026-09-24", "7.6302"),
                # The all-series ECB endpoint also returns suspended currencies.
                row("RUB", "2022-03-01", "117.201"),
            )
        ),
        now=NOW,
    )

    assert result["url"] == ECB_CSV_URL
    assert result["observation_date"] == "2026-09-24"
    assert result["retrieved_at"] == NOW.isoformat()
    assert result["rate_type"] == "derived_cross_reference"
    assert result["eur_basis"] == {"USD": "1.1367", "CNY": "7.6302"}
    assert abs(
        Decimal(result["quote_per_base"])
        - Decimal("7.6302") / Decimal("1.1367")
    ) < Decimal("0.00000000001")
    assert "not transaction rates" in result["usage_note"]


@pytest.mark.asyncio
async def test_direct_eur_reference_is_reported_without_cross_label(
    tmp_path: Path,
) -> None:
    result = await get_ecb_fx_reference_impl(
        Settings(data_dir=tmp_path),
        "EUR",
        "USD",
        transport=transport_for(feed(row("USD", "2026-09-24", "1.1367"))),
        now=NOW,
    )

    assert result["quote_per_base"] == "1.1367"
    assert result["rate_type"] == "ecb_eur_reference"
    assert result["eur_basis"] == {"EUR": "1", "USD": "1.1367"}

    inverse = await get_ecb_fx_reference_impl(
        Settings(data_dir=tmp_path),
        "USD",
        "EUR",
        transport=transport_for(feed(row("USD", "2026-09-24", "1.1367"))),
        now=NOW,
    )
    assert inverse["rate_type"] == "derived_eur_reciprocal"
    assert abs(
        Decimal(inverse["quote_per_base"]) - Decimal(1) / Decimal("1.1367")
    ) < Decimal("0.000000000001")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("rows", "base", "quote", "expected"),
    [
        ((row("RUB", "2022-03-01", "117.201"),), "EUR", "RUB", "STALE_RATE"),
        (
            (row("USD", "2026-09-24", "1.1367"), row("CNY", "2026-09-23", "7.5")),
            "USD", "CNY", "RATE_DATE_MISMATCH",
        ),
        ((row("USD", "2026-09-24", "1.1367"),), "USD", "CNY", "RATE_UNAVAILABLE"),
        ((row("USD", "2026-09-25", "1.1367"),), "EUR", "USD", "STALE_RATE"),
        ((row("USD", "2026-09-24", "0"),), "EUR", "USD", "RATE_SOURCE_INVALID"),
        (
            (row("USD", "2026-09-24", "1.1367"), row("USD", "2026-09-24", "9")),
            "EUR", "USD", "RATE_SOURCE_INVALID",
        ),
    ],
)
async def test_unusable_observations_fail_closed(
    tmp_path: Path,
    rows: tuple[str, ...],
    base: str,
    quote: str,
    expected: str,
) -> None:
    result = await get_ecb_fx_reference_impl(
        Settings(data_dir=tmp_path),
        base,
        quote,
        transport=transport_for(feed(*rows)),
        now=NOW,
    )
    assert result["error_code"] == expected
    assert "quote_per_base" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "status", "content_type", "expected"),
    [
        ("", 302, "text/csv", "RATE_SOURCE_UNAVAILABLE"),
        ("", 503, "text/csv", "RATE_SOURCE_UNAVAILABLE"),
        ("{}", 200, "application/json", "RATE_SOURCE_INVALID"),
        ("bad header\n", 200, "text/csv", "RATE_SOURCE_INVALID"),
        ("x" * (MAX_ECB_CSV_BYTES + 1), 200, "text/csv", "RATE_SOURCE_INVALID"),
    ],
)
async def test_bad_http_or_feed_does_not_become_a_rate(
    tmp_path: Path,
    body: str,
    status: int,
    content_type: str,
    expected: str,
) -> None:
    result = await get_ecb_fx_reference_impl(
        Settings(data_dir=tmp_path),
        "EUR",
        "USD",
        transport=transport_for(body, status=status, content_type=content_type),
        now=NOW,
    )
    assert result["error_code"] == expected


def test_fx_tool_is_read_only_and_receipted_for_same_run_session(tmp_path: Path) -> None:
    server = build_fx_mcp_server(Settings(data_dir=tmp_path))
    assert server["type"] == "sdk"
    assert FX_MCP_SERVER_NAME == "ophira_fx"
    assert TOOL_GET_ECB_FX_REFERENCE in READ_ONLY_MCP_TOOL_NAMES
    assert OPHIRA_TOOL_NAMES.count(TOOL_GET_ECB_FX_REFERENCE) == 1
    assert _post_tool_untrusted_source(
        TOOL_GET_ECB_FX_REFERENCE, {}, tmp_path
    ) == UntrustedSource.WEB_PAGE

    ledger = FileLedger(tmp_path)
    project = ledger.create_project(ProjectCreate(name="FX project", objective="Compare"))
    run = ledger.create_run(project.id, "Compare USD and CNY")
    run = ledger.update_run(run.id, sdk_session_id="sdk_fx_test")
    payload = {
        "url": ECB_CSV_URL,
        "observation_date": "2026-09-24",
        "base_currency": "USD",
        "quote_currency": "CNY",
        "quote_per_base": "6.712593",
    }
    attached = attach_source_receipts(
        ledger,
        run.id,
        TOOL_GET_ECB_FX_REFERENCE,
        {"base_currency": "USD", "quote_currency": "CNY"},
        [{"type": "text", "text": json.dumps(payload)}],
        delegated=False,
    )

    assert len(attached.receipt_ids) == 1
    output = json.loads(attached.tool_response[0]["text"])
    assert output["source_receipt"]["id"] == attached.receipt_ids[0]
    replay = ledger.verify_tool_receipt(
        project.id,
        attached.receipt_ids[0],
        run_id=run.id,
        session_id=run.sdk_session_id or "",
    )
    assert replay.receipt.object_type == "fx_reference_observation"
    assert replay.receipt.canonical_source == ECB_CSV_URL
    assert replay.receipt.object_metadata["observation_date"] == "2026-09-24"
    assert replay.payload["quote_per_base"] == payload["quote_per_base"]

    failed = attach_source_receipts(
        ledger,
        run.id,
        TOOL_GET_ECB_FX_REFERENCE,
        {},
        [{"type": "text", "text": json.dumps({"error_code": "STALE_RATE", "url": ECB_CSV_URL})}],
        delegated=False,
    )
    assert failed.receipt_ids == ()
