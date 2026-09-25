"""One scoped Eurostat Comext commodity trade observation through Web fetch.

The public statistics API is JSON-stat. Its dimensions must prove that the
returned cell matches the requested market, HS code, year and import measure.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .market_data import InvalidMarketObservation
from .untrusted import UNTRUSTED_DATA_NOTICE, UntrustedSource, sanitize_untrusted_payload

EU_REPORTERS = frozenset(
    "AT BE BG HR CY CZ DK EE FI FR DE EL HU IE IT LV LT LU MT NL PL PT RO SK SI ES SE".split()
)
EUROSTAT_TRADE_PATH = "/eurostat/api/comext/dissemination/statistics/1.0/data/DS-045409"
MAX_EUROSTAT_JSON_BYTES = 64_000
_HS6 = re.compile(r"^[0-9]{6}$")
_YEAR = re.compile(r"^[0-9]{4}$")


@dataclass(frozen=True)
class EurostatTradeQuery:
    reporter: str
    product: str
    year: str


def eurostat_trade_query(url: str) -> EurostatTradeQuery | None:
    """Recognize only a single annual HS6 import value for one EU member."""
    parts = urlsplit(url)
    if parts.hostname != "ec.europa.eu" or parts.path != EUROSTAT_TRADE_PATH:
        return None
    if (
        parts.scheme != "https" or parts.port is not None or parts.fragment
        or parts.username or parts.password
    ):
        raise InvalidMarketObservation("Use the canonical HTTPS Eurostat API URL.")
    params = parse_qs(parts.query, keep_blank_values=True)
    expected = {
        "format": "JSON",
        "lang": "EN",
        "freq": "A",
        "partner": "WORLD",
        "flow": "1",
        "indicators": "VALUE_IN_EUROS",
    }
    if set(params) != set(expected) | {"reporter", "product", "time"} or any(
        len(values) != 1 for values in params.values()
    ):
        raise InvalidMarketObservation("Request one Eurostat reporter, HS6 product and year.")
    if any(params[key] != [value] for key, value in expected.items()):
        raise InvalidMarketObservation("Use annual world-partner import value in euros.")
    reporter, product, year = (params[key][0] for key in ("reporter", "product", "time"))
    if reporter not in EU_REPORTERS or not _HS6.fullmatch(product) or product[:2] == "00":
        raise InvalidMarketObservation("Use an EU member reporter and a six-digit HS code.")
    if (
        not _YEAR.fullmatch(year) or not 2010 <= int(year) < date.today().year
    ):
        raise InvalidMarketObservation("Use a completed annual period from 2010 onward.")
    return EurostatTradeQuery(reporter, product, year)


def _single_cell(payload: dict[str, Any], query: EurostatTradeQuery) -> None:
    dimensions = payload.get("dimension")
    ids = payload.get("id")
    sizes = payload.get("size")
    expected = {
        "freq": "A",
        "reporter": query.reporter,
        "partner": "WORLD",
        "product": query.product,
        "flow": "1",
        "indicators": "VALUE_IN_EUROS",
        "time": query.year,
    }
    if isinstance(ids, list) and "time_period" in ids and "time" not in ids:
        expected["time_period"] = expected.pop("time")
    if (
        not isinstance(ids, list) or len(ids) != len(expected)
        or not all(isinstance(item, str) for item in ids)
        or set(ids) != set(expected) or not isinstance(sizes, list)
        or len(sizes) != len(ids) or any(type(size) is not int or size != 1 for size in sizes)
        or not isinstance(dimensions, dict)
    ):
        raise InvalidMarketObservation("Eurostat response is not one scoped cell.")
    for dimension, code in expected.items():
        entry = dimensions.get(dimension)
        category = entry.get("category") if isinstance(entry, dict) else None
        index = category.get("index") if isinstance(category, dict) else None
        if index != {code: 0} and index != [code]:
            raise InvalidMarketObservation("Eurostat response dimension differs from request.")


def _cell_value(raw: Any) -> str | None:
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float, Decimal)):
        raise InvalidMarketObservation("Eurostat trade value is invalid.")
    try:
        amount = Decimal(str(raw))
    except (InvalidOperation, ValueError) as exc:
        raise InvalidMarketObservation("Eurostat trade value is invalid.") from exc
    if not amount.is_finite() or amount < 0 or amount > Decimal("1e18"):
        raise InvalidMarketObservation("Eurostat trade value is invalid.")
    return str(amount)


def project_eurostat_trade_observation(
    payload: Any, *, query: EurostatTradeQuery, source_url: str, retrieved_at: str
) -> dict[str, Any]:
    """Fail closed on extra cells, mismatched dimensions or invalid values."""
    if (
        not isinstance(payload, dict) or payload.get("version") != "2.0"
        or payload.get("class") != "dataset" or payload.get("source") != "ESTAT"
    ):
        raise InvalidMarketObservation("Eurostat response is not a JSON-stat dataset.")
    _single_cell(payload, query)
    values = payload.get("value")
    if isinstance(values, dict) and set(values) <= {"0"}:
        value = _cell_value(values.get("0"))
    elif isinstance(values, list) and len(values) == 1:
        value = _cell_value(values[0])
    else:
        raise InvalidMarketObservation("Eurostat response has incomplete or excess values.")

    updated = payload.get("updated")
    if updated is not None:
        if not isinstance(updated, str) or len(updated) > 40:
            raise InvalidMarketObservation("Eurostat update time is invalid.")
        normalized = re.sub(r"([+-][0-9]{2})([0-9]{2})$", r"\1:\2", updated)
        try:
            timestamp = datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise InvalidMarketObservation("Eurostat update time is invalid.") from exc
        if timestamp.utcoffset() is None:
            raise InvalidMarketObservation("Eurostat update time lacks a timezone.")
        updated = timestamp.isoformat()

    status = payload.get("status")
    if status is None:
        quality_flag = None
    elif isinstance(status, dict) and set(status) <= {"0"}:
        quality_flag = status.get("0")
    elif isinstance(status, list) and len(status) == 1:
        quality_flag = status[0]
    else:
        raise InvalidMarketObservation("Eurostat status has an unexpected shape.")
    if quality_flag is not None and (
        not isinstance(quality_flag, str) or len(quality_flag) > 16
        or not re.fullmatch(r"[A-Za-z0-9 ]*", quality_flag)
    ):
        raise InvalidMarketObservation("Eurostat status is invalid.")

    result = {
        "note": UNTRUSTED_DATA_NOTICE,
        "source": "European Commission, Eurostat, Comext DS-045409",
        "source_url": source_url,
        "source_id": (
            f"EUROSTAT_COMEXT:DS-045409:{query.reporter}:WORLD:"
            f"{query.product}:{query.year}:IMPORT"
        ),
        "source_page": "https://ec.europa.eu/eurostat/databrowser/view/DS-045409/default/table",
        "retrieved_at": retrieved_at,
        "source_updated_at": updated,
        "applicable_market": query.reporter,
        "partner_scope": "WORLD (all origin countries, including intra-EU trade)",
        "product": {"classification": "HS6", "code": query.product},
        "observation": {
            "year": query.year,
            "measure": "reported merchandise imports",
            "value": value,
            "unit": "EUR",
            "quality_flag": quality_flag,
        },
        "coverage_note": (
            "This is a country-level trade value for an HS6 commodity, not retail demand, "
            "sales, a supplier quote or a forecast. A missing value is unknown, not zero. "
            "HS classification and published values may be revised; confirm product scope "
            "and Eurostat flags before commercial use. Cite Eurostat DS-045409 and access date."
        ),
    }
    sanitized = sanitize_untrusted_payload(result, UntrustedSource.WEB_PAGE)
    return {**sanitized.value, "truncated": sanitized.truncated}
