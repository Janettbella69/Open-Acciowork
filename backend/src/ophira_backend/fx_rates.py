"""Read-only ECB euro reference rates for traceable currency comparisons.

The ECB publishes observations for EUR against each currency. The latest
observation is per series, so older suspended series must be rejected before
they can be used as a current quote or as one side of a derived cross rate.
"""

from __future__ import annotations

import csv
import io
import json
import re
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from claude_agent_sdk import ToolAnnotations, create_sdk_mcp_server, tool

from .config import Settings
from .constants import FX_MCP_SERVER_NAME
from .web_tools import send_pinned_public_request

ECB_CSV_URL = (
    "https://data-api.ecb.europa.eu/service/data/EXR/D..EUR.SP00.A"
    "?lastNObservations=1&format=csvdata&detail=dataonly"
)
MAX_ECB_CSV_BYTES = 128_000
MAX_ECB_CSV_ROWS = 200
MAX_OBSERVATION_AGE_DAYS = 7
_CURRENCY_CODE = re.compile(r"^[A-Z]{3}$")
_OBSERVATION_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_REQUIRED_COLUMNS = frozenset(
    {"KEY", "FREQ", "CURRENCY", "CURRENCY_DENOM", "EXR_TYPE", "EXR_SUFFIX",
     "TIME_PERIOD", "OBS_VALUE"}
)


class InvalidECBFeed(ValueError):
    """The returned data cannot support a trustworthy reference rate."""


def _error(code: str, message: str) -> dict[str, str]:
    return {"error_code": code, "error": message}


def _parse_latest_observations(data: bytes) -> dict[str, tuple[date, Decimal]]:
    try:
        csv_text = data.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(csv_text, newline=""), strict=True)
        if not reader.fieldnames or not _REQUIRED_COLUMNS.issubset(reader.fieldnames):
            raise InvalidECBFeed("missing required CSV columns")
        observations: dict[str, tuple[date, Decimal]] = {}
        for row in reader:
            if len(observations) >= MAX_ECB_CSV_ROWS:
                raise InvalidECBFeed("too many CSV observations")
            currency = row["CURRENCY"]
            if (
                currency is None
                or not _CURRENCY_CODE.fullmatch(currency)
                or row["KEY"] != f"EXR.D.{currency}.EUR.SP00.A"
                or row["FREQ"] != "D"
                or row["CURRENCY_DENOM"] != "EUR"
                or row["EXR_TYPE"] != "SP00"
                or row["EXR_SUFFIX"] != "A"
                or row["TIME_PERIOD"] is None
                or not _OBSERVATION_DATE.fullmatch(row["TIME_PERIOD"])
                or currency in observations
            ):
                raise InvalidECBFeed("invalid or duplicate CSV series")
            observed_on = date.fromisoformat(row["TIME_PERIOD"])
            value = Decimal(row["OBS_VALUE"] or "")
            if not value.is_finite() or value <= 0:
                raise InvalidECBFeed("invalid CSV rate")
            observations[currency] = observed_on, value
        if not observations:
            raise InvalidECBFeed("empty CSV observations")
        return observations
    except (csv.Error, UnicodeError, InvalidOperation, OverflowError, ValueError) as exc:
        raise InvalidECBFeed("invalid ECB CSV response") from exc


async def get_ecb_fx_reference_impl(
    settings: Settings,
    base_currency: str,
    quote_currency: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return quote units per one base unit, or a safe structured error."""
    base = base_currency.strip().upper() if isinstance(base_currency, str) else ""
    quote = quote_currency.strip().upper() if isinstance(quote_currency, str) else ""
    if not _CURRENCY_CODE.fullmatch(base) or not _CURRENCY_CODE.fullmatch(quote):
        return _error("INVALID_CURRENCY", "Currency codes must be three ASCII letters.")
    if base == quote:
        return _error("INVALID_PAIR", "Choose two different currencies.")

    retrieved_at = now.astimezone(UTC) if now is not None else datetime.now(UTC)
    async with httpx.AsyncClient(
        transport=transport,
        follow_redirects=False,
        trust_env=False,
        limits=httpx.Limits(max_keepalive_connections=0),
        timeout=settings.web_timeout_seconds,
        headers={"Accept": "text/csv", "User-Agent": "OPHIRA-ECB-FX/1.0"},
    ) as client:
        try:
            response, reason = await send_pinned_public_request(
                client, "GET", ECB_CSV_URL, stream=True
            )
            if response is None:
                return _error("RATE_SOURCE_UNAVAILABLE", reason or "ECB source unavailable")
            try:
                if response.status_code != 200:
                    return _error(
                        "RATE_SOURCE_UNAVAILABLE",
                        f"ECB data request returned HTTP {response.status_code}.",
                    )
                content_type = response.headers.get("content-type", "").casefold()
                if not any(
                    accepted in content_type
                    for accepted in ("text/csv", "application/vnd.sdmx.data+csv")
                ):
                    return _error("RATE_SOURCE_INVALID", "ECB response is not CSV data.")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > MAX_ECB_CSV_BYTES:
                        return _error("RATE_SOURCE_INVALID", "ECB CSV response is too large.")
                    body.extend(chunk)
            finally:
                await response.aclose()
        except httpx.HTTPError:
            return _error("RATE_SOURCE_UNAVAILABLE", "ECB data request failed.")

    try:
        observations = _parse_latest_observations(bytes(body))
    except InvalidECBFeed:
        return _error("RATE_SOURCE_INVALID", "ECB CSV response is malformed.")

    required = {base, quote} - {"EUR"}
    if required - observations.keys():
        return _error("RATE_UNAVAILABLE", "ECB has no reference rate for this pair.")
    dates = {observations[currency][0] for currency in required}
    if len(dates) != 1:
        return _error(
            "RATE_DATE_MISMATCH",
            "ECB currency observations have different dates; no cross rate was derived.",
        )
    observed_on = next(iter(dates))
    age_days = (retrieved_at.date() - observed_on).days
    if age_days < 0 or age_days > MAX_OBSERVATION_AGE_DAYS:
        return _error(
            "STALE_RATE",
            "ECB's latest observation for this pair is outside the seven-day freshness window.",
        )

    base_per_eur = observations[base][1] if base != "EUR" else Decimal(1)
    quote_per_eur = observations[quote][1] if quote != "EUR" else Decimal(1)
    rate = quote_per_eur / base_per_eur
    significant_digits = min(
        12, *(len(observations[currency][1].as_tuple().digits) for currency in required)
    )
    return {
        "source": "European Central Bank",
        "url": ECB_CSV_URL,
        "retrieved_at": retrieved_at.isoformat(),
        "observation_date": observed_on.isoformat(),
        "base_currency": base,
        "quote_currency": quote,
        "quote_per_base": format(rate, f".{significant_digits}g"),
        "significant_digits": significant_digits,
        "eur_basis": {
            base: str(base_per_eur),
            quote: str(quote_per_eur),
        },
        "rate_type": (
            "ecb_eur_reference"
            if base == "EUR"
            else "derived_eur_reciprocal"
            if quote == "EUR"
            else "derived_cross_reference"
        ),
        "usage_note": (
            "ECB reference rates are for information only and are not transaction rates. "
            "Rates with a non-EUR base are calculated from same-date EUR references."
        ),
    }


def build_fx_mcp_server(settings: Settings):
    @tool(
        "get_ecb_fx_reference",
        "Get the latest same-date ECB euro reference rate or a cross rate derived "
        "from it, with source and observation date. Informational only; never use "
        "as a transaction quote. Stale, missing, or date-mismatched rates fail closed.",
        {
            "type": "object",
            "properties": {
                "base_currency": {"type": "string", "pattern": "^[A-Za-z]{3}$"},
                "quote_currency": {"type": "string", "pattern": "^[A-Za-z]{3}$"},
            },
            "required": ["base_currency", "quote_currency"],
            "additionalProperties": False,
        },
        annotations=ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, openWorldHint=True
        ),
    )
    async def get_ecb_fx_reference(args: dict[str, Any]) -> dict[str, Any]:
        result = await get_ecb_fx_reference_impl(
            settings, args["base_currency"], args["quote_currency"]
        )
        return {
            "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
            "is_error": "error_code" in result,
        }

    return create_sdk_mcp_server(
        name=FX_MCP_SERVER_NAME, version="1.0.0", tools=[get_ecb_fx_reference]
    )
