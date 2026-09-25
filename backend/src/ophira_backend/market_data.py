"""Bounded World Bank WDI observations surfaced through the existing web read tool.

This is a macro market signal, not product-level demand or a sales forecast. Only
three reviewed WDI indicators receive a structured projection; other web pages
continue through the ordinary snapshot path.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .untrusted import UNTRUSTED_DATA_NOTICE, UntrustedSource, sanitize_untrusted_payload

WORLD_BANK_INDICATORS = {
    "TM.VAL.MRCH.CD.WT": ("Merchandise imports", "current US$"),
    "NE.CON.PRVT.CD": ("Household final consumption expenditure", "current US$"),
    "SP.POP.TOTL": ("Population, total", "persons"),
}
MAX_WORLD_BANK_JSON_BYTES = 64_000
_WDI_PATH = re.compile(r"^/v2/country/([A-Za-z]{3})/indicator/([A-Za-z0-9.]+)$")
_YEAR = re.compile(r"^[0-9]{4}$")


class InvalidMarketObservation(ValueError):
    """A response cannot be represented as a trustworthy bounded observation."""


@dataclass(frozen=True)
class WorldBankQuery:
    country: str
    indicator: str
    periods: int


def world_bank_query(url: str) -> WorldBankQuery | None:
    """Identify the reviewed WDI URL shape, rejecting ambiguous query options."""
    parts = urlsplit(url)
    if parts.hostname != "api.worldbank.org":
        return None
    match = _WDI_PATH.fullmatch(parts.path)
    if match is None or match.group(2) not in WORLD_BANK_INDICATORS:
        return None
    if parts.scheme != "https" or parts.port is not None or parts.fragment:
        raise InvalidMarketObservation("Use the canonical HTTPS World Bank API URL.")
    params = parse_qs(parts.query, keep_blank_values=True)
    if set(params) != {"format", "source", "mrv", "per_page"} or any(
        len(values) != 1 for values in params.values()
    ):
        raise InvalidMarketObservation("Use format=json, source=2 and bounded mrv/per_page.")
    if params["format"] != ["json"] or params["source"] != ["2"]:
        raise InvalidMarketObservation("Use the World Development Indicators JSON source.")
    try:
        periods = int(params["mrv"][0])
        page_size = int(params["per_page"][0])
    except ValueError as exc:
        raise InvalidMarketObservation("mrv and per_page must be integers.") from exc
    if not 1 <= periods <= 5 or page_size != periods:
        raise InvalidMarketObservation("Request one to five recent periods on one page.")
    return WorldBankQuery(match.group(1).upper(), match.group(2), periods)


def project_world_bank_observations(
    payload: Any, *, query: WorldBankQuery, source_url: str, retrieved_at: str
) -> dict[str, Any]:
    """Validate country, series, years and numeric values before model exposure."""
    if not isinstance(payload, list) or len(payload) != 2:
        raise InvalidMarketObservation("World Bank response is not a two-part dataset.")
    metadata, rows = payload
    if not isinstance(metadata, dict) or not isinstance(rows, list):
        raise InvalidMarketObservation("World Bank response has invalid metadata or rows.")
    if (
        type(metadata.get("page")) is not int
        or metadata["page"] != 1
        or type(metadata.get("pages")) is not int
        or metadata["pages"] != 1
        or isinstance(metadata.get("total"), bool)
        or not isinstance(metadata.get("total"), int)
        or metadata["total"] < 1
        or metadata["total"] > query.periods
        or len(rows) != metadata["total"]
        or not rows
    ):
        raise InvalidMarketObservation("World Bank response has incomplete or excess coverage.")
    updated = metadata.get("lastupdated")
    if updated is not None:
        try:
            updated = date.fromisoformat(updated).isoformat()
        except (TypeError, ValueError) as exc:
            raise InvalidMarketObservation("World Bank update date is invalid.") from exc

    observations: list[dict[str, str | None]] = []
    years: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("indicator"), dict):
            raise InvalidMarketObservation("World Bank observation is malformed.")
        year = row.get("date")
        if (
            row.get("countryiso3code") != query.country
            or row["indicator"].get("id") != query.indicator
            or not isinstance(year, str)
            or not _YEAR.fullmatch(year)
            or not 1960 <= int(year) <= date.today().year
            or year in years
        ):
            raise InvalidMarketObservation("World Bank observation scope or year differs.")
        years.add(year)
        raw_value = row.get("value")
        if raw_value is None:
            value = None
        else:
            if isinstance(raw_value, bool) or not isinstance(
                raw_value, (int, float, Decimal, str)
            ):
                raise InvalidMarketObservation("World Bank observation value is invalid.")
            try:
                amount = Decimal(str(raw_value))
            except (InvalidOperation, ValueError) as exc:
                raise InvalidMarketObservation("World Bank observation value is invalid.") from exc
            if not amount.is_finite() or amount < 0 or amount > Decimal("1e18"):
                raise InvalidMarketObservation("World Bank observation value is invalid.")
            value = str(amount)
        observations.append({"year": year, "value": value})
    observations.sort(key=lambda item: item["year"], reverse=True)
    indicator_name, unit = WORLD_BANK_INDICATORS[query.indicator]
    result = {
        "note": UNTRUSTED_DATA_NOTICE,
        "source": "World Bank, World Development Indicators",
        "source_url": source_url,
        "source_id": f"WB_WDI:{query.country}:{query.indicator}",
        "source_page": f"https://data.worldbank.org/indicator/{query.indicator}",
        "retrieved_at": retrieved_at,
        "source_updated_at": updated,
        "applicable_market": query.country,
        "indicator": {"code": query.indicator, "name": indicator_name, "unit": unit},
        "observations": observations,
        "coverage_note": (
            "Country-level annual macro indicator. Missing values are unknown, not zero. "
            "This does not measure product-level demand, customer leads or sales. "
            "Check the indicator page for methodology, revisions and license before reuse."
        ),
    }
    sanitized = sanitize_untrusted_payload(result, UntrustedSource.WEB_PAGE)
    return {**sanitized.value, "truncated": sanitized.truncated}
