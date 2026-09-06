"""Deterministic cash timing and funding gaps from an explicit settlement schedule.

This module has no clock, network, ledger writes, market forecasts or financing
actions. Source references are caller-supplied labels, never verified Evidence.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, localcontext
from typing import Annotated, Any, Literal

from pydantic import BeforeValidator, Field, field_validator, model_validator

from .models import APIModel

CASHFLOW_MAX_MOVEMENTS = 500
CASHFLOW_MAX_HORIZON_DAYS = 3_660
CASHFLOW_MAX_TIMELINE_ROWS = CASHFLOW_MAX_MOVEMENTS + 2
CASHFLOW_DECIMAL_PLACES = 4
_MAX_AMOUNT = Decimal("1000000000000")
_QUANTUM = Decimal("0.0001")
_ZERO = Decimal("0.0000")
_ACTIVITIES = ("operating", "investing", "financing")


def _money_input(value: Any) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, str | int | float | Decimal):
        raise ValueError("amount must be a decimal string or number, never a boolean")
    if isinstance(value, str) and (
        len(value) > 40 or re.fullmatch(r"[+-]?[0-9]+(?:\.[0-9]+)?", value) is None
    ):
        raise ValueError("amount strings must use plain decimal notation")
    try:
        amount = Decimal(str(value))
    except (ValueError, InvalidOperation) as exc:
        raise ValueError("amount must be numeric") from exc
    if not amount.is_finite() or amount.copy_abs() > _MAX_AMOUNT:
        raise ValueError("amount must be finite and no greater than 1000000000000 in magnitude")
    with localcontext() as context:
        context.prec = 32
        normalized = amount.quantize(_QUANTUM)
    if normalized != amount:
        raise ValueError("amount requires rounding; provide at most four fractional decimal places")
    return normalized if normalized else _ZERO


def _date_input(value: Any) -> date | str:
    if isinstance(value, datetime):
        raise ValueError("settlement dates must be dates, without a time or timezone")
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value) is None:
        raise ValueError("settlement dates must use YYYY-MM-DD; timestamps are not accepted")
    return value


Money = Annotated[
    Decimal, BeforeValidator(_money_input),
    Field(ge=-_MAX_AMOUNT, le=_MAX_AMOUNT, max_digits=17, decimal_places=CASHFLOW_DECIMAL_PLACES),
]
SettlementDate = Annotated[date, BeforeValidator(_date_input)]


class CashMovement(APIModel):
    id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    date: SettlementDate
    direction: Literal["inflow", "outflow"]
    amount: Annotated[Money, Field(gt=0)]
    activity: Literal["operating", "investing", "financing"]
    description: str = Field(default="", max_length=200)
    source_ref: str | None = Field(default=None, min_length=1, max_length=300)

    @field_validator("source_ref")
    @classmethod
    def nonblank_reference(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("source_ref must not be blank")
        return value


class CashflowInput(APIModel):
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    start_date: SettlementDate
    end_date: SettlementDate
    opening_cash: Money
    minimum_reserve: Annotated[Money, Field(ge=0)] = _ZERO
    movements: list[CashMovement] = Field(default_factory=list, max_length=CASHFLOW_MAX_MOVEMENTS)

    @model_validator(mode="after")
    def coherent_schedule(self) -> CashflowInput:
        days = (self.end_date - self.start_date).days + 1
        if days < 1 or days > CASHFLOW_MAX_HORIZON_DAYS:
            raise ValueError(
                f"inclusive planning horizon must be 1..{CASHFLOW_MAX_HORIZON_DAYS} days"
            )
        ids = [movement.id for movement in self.movements]
        if len(ids) != len(set(ids)):
            raise ValueError("cash movement IDs must be unique; duplicate payments are not netted")
        if any(not self.start_date <= item.date <= self.end_date for item in self.movements):
            raise ValueError(
                "every cash movement must settle inside the inclusive planning horizon"
            )
        return self


def _amount(value: Decimal) -> str:
    return format(value if value else _ZERO, ".4f")


def calculate_cashflow(payload: CashflowInput | dict[str, Any]) -> dict[str, Any]:
    """Return exact four-decimal amounts; reserve is a threshold, never an expense."""
    request = (
        payload if isinstance(payload, CashflowInput) else CashflowInput.model_validate(payload)
    )
    with localcontext() as context:
        # 500 movements, each <= 10^12 with four fractional places, fit exactly.
        context.prec = 32
        return _calculate(request)


def _calculate(request: CashflowInput) -> dict[str, Any]:
    movements = sorted(request.movements, key=lambda item: (item.date, item.id))
    grouped: dict[date, list[CashMovement]] = defaultdict(list)
    for item in movements:
        grouped[item.date].append(item)
    dates = sorted({request.start_date, request.end_date, *grouped})
    totals = {activity: {"inflows": _ZERO, "outflows": _ZERO} for activity in _ACTIVITIES}
    balance = request.opening_cash
    reserve = request.minimum_reserve
    opening_gap = max(_ZERO, reserve - balance)
    close_gap = _ZERO
    conservative_gap = opening_gap
    conservative_date = request.start_date if opening_gap else None
    conservative_phase = "opening" if opening_gap else None
    first_gap_date = request.start_date if opening_gap else None
    first_gap_phase = "opening" if opening_gap else None
    minimum_close: Decimal | None = None
    minimum_close_date: date | None = None
    timeline = []
    for day in dates:
        day_items = grouped[day]
        inflows = sum((item.amount for item in day_items if item.direction == "inflow"), _ZERO)
        outflows = sum((item.amount for item in day_items if item.direction == "outflow"), _ZERO)
        after_outflows = balance - outflows
        closing = after_outflows + inflows
        day_close_gap = max(_ZERO, reserve - closing)
        day_conservative_gap = max(_ZERO, reserve - after_outflows)
        if minimum_close is None or closing < minimum_close:
            minimum_close, minimum_close_date = closing, day
        close_gap = max(close_gap, day_close_gap)
        if day_conservative_gap > conservative_gap:
            conservative_gap, conservative_date = day_conservative_gap, day
            conservative_phase = "outflows_before_inflows"
        if first_gap_date is None and day_conservative_gap:
            first_gap_date, first_gap_phase = day, "outflows_before_inflows"
        for item in day_items:
            totals[item.activity]["inflows" if item.direction == "inflow" else "outflows"] += (
                item.amount
            )
        timeline.append({
            "date": day.isoformat(), "movement_ids": [item.id for item in day_items],
            "opening_cash": _amount(balance), "inflows": _amount(inflows),
            "outflows": _amount(outflows), "net_change": _amount(inflows - outflows),
            "closing_cash": _amount(closing),
            "cash_if_outflows_settle_first": _amount(after_outflows),
            "closing_reserve_shortfall": _amount(day_close_gap),
            "outflows_first_reserve_shortfall": _amount(day_conservative_gap),
        })
        balance = closing
    total_inflows = sum((item["inflows"] for item in totals.values()), _ZERO)
    total_outflows = sum((item["outflows"] for item in totals.values()), _ZERO)
    normalized_inputs = {
        **request.model_dump(mode="json"),
        "movements": [item.model_dump(mode="json") for item in movements],
    }
    source_count = sum(item.source_ref is not None for item in movements)
    return {
        "schema_version": 1,
        "method": "explicit_dated_cash_movements",
        "currency": request.currency,
        "amount_format": {"unit": request.currency, "encoding": "decimal_string",
                          "fractional_places": CASHFLOW_DECIMAL_PLACES, "rounding": "none"},
        "input_sha256": hashlib.sha256(json.dumps(
            normalized_inputs, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest(),
        "source_coverage": {
            "input_origin": "caller_supplied", "references_verified": False,
            "movements_with_reference": source_count,
            "movements_without_reference": len(movements) - source_count,
            "note": "References are labels supplied by the caller; no Evidence was verified.",
        },
        "assumptions": [
            "All amounts use the one declared currency; "
            "no exchange rates or conversions are inferred.",
            "Dates are cash settlement dates, not invoice or revenue-recognition dates; "
            "no calendar adjustments.",
            "Only supplied movements are included; "
            "missing receipts, expenses, taxes, interest and fees are not estimated.",
            "Minimum reserve is a balance threshold and is never deducted as a cash expense.",
            "The intraday bound assumes all daily outflows settle before inflows; "
            "actual intraday order is unknown.",
            "Supplied financing is included in balances; "
            "additional funding is a calculated gap, not a loan or injection.",
            "Activity classifications and source references are caller-supplied and unverified.",
        ],
        "totals": {
            "opening_cash": _amount(request.opening_cash),
            "inflows": _amount(total_inflows), "outflows": _amount(total_outflows),
            "net_change": _amount(total_inflows - total_outflows),
            "closing_cash": _amount(balance),
            "reconciliation_difference": _amount(
                balance - request.opening_cash - total_inflows + total_outflows
            ),
            "by_activity": {key: {**{name: _amount(value) for name, value in item.items()},
                                  "net_change": _amount(item["inflows"] - item["outflows"])}
                            for key, item in totals.items()},
        },
        "funding": {
            "minimum_reserve": _amount(reserve), "opening_shortfall": _amount(opening_gap),
            "minimum_closing_cash": _amount(minimum_close),
            "minimum_closing_cash_date": minimum_close_date.isoformat(),
            "peak_closing_shortfall": _amount(close_gap),
            "funding_for_opening_and_daily_closes": _amount(max(opening_gap, close_gap)),
            "funding_if_outflows_settle_first": _amount(conservative_gap),
            "peak_outflows_first_shortfall_date": (
                conservative_date.isoformat() if conservative_date else None
            ),
            "peak_outflows_first_shortfall_phase": conservative_phase,
            "first_shortfall_date": first_gap_date.isoformat() if first_gap_date else None,
            "first_shortfall_phase": first_gap_phase,
            "ending_shortfall": _amount(max(_ZERO, reserve - balance)),
        },
        "timeline": timeline,
        "timeline_coverage": {
            "row_count": len(timeline), "max_rows": CASHFLOW_MAX_TIMELINE_ROWS,
            "inclusive_horizon_days": (request.end_date - request.start_date).days + 1,
            "unchanged_dates_omitted": True,
            "note": "Start/end dates and every settlement date are included; "
                    "balances carry forward between rows.",
        },
        # Summary first: bounded event previews retain the funding result even
        # when the full input/timeline is large. Full inputs permit recomputation.
        "inputs": normalized_inputs,
    }
