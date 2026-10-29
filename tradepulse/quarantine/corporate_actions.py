"""Corporate actions fail closed (Rev.140, spec §3.10, R5, R9).

No adjustment is automatic. Any activity naming an instrument with open
quarantined inventory that could change its quantity or identity refuses
every resolution for it; so does any activity type not known to be harmless.

Activity types from Alpaca's documentation,
https://docs.alpaca.markets/docs/account-activities (accessed 2026-10-29).
"""
from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime

from tradepulse.broker.port import BrokerActivity
from tradepulse.broker.symbols import broker_symbol_asset_key

# Quantity- or identity-changing: merger, name and symbol change, spinoff,
# split, reorg, security transfers and option lifecycle events.
CORPORATE_ACTION_ACTIVITY_TYPES = frozenset({
    "MA", "NC", "SC", "SSO", "SSP", "REORG", "ACATS", "JNLS", "FOPT", "OPASN", "OPEXP", "OPXRC",
})
# Executions, fees and account cash flows. A distribution naming the
# instrument (DIV, DIVFEE, DIVNRA, CGD) is not harmless: on a short liability
# it is a payment in lieu owed (§3.10), so it blocks like any unknown type.
HARMLESS_ACTIVITY_TYPES = frozenset({
    "FILL", "FEE", "CFEE", "PTC", "PTR", "JNLC", "ACATC", "CSD", "CSW", "TRANS", "INT", "INTNRA", "INTTW",
})


def _activity_date(activity: BrokerActivity) -> date | None:
    if activity.transaction_time is not None:
        return activity.transaction_time.date()
    raw = activity.raw.get("date") or activity.raw.get("created_at")
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None


def corporate_action_blocks(activities: Sequence[BrokerActivity], asset_key: str, since: datetime) -> str | None:
    """The first activity on ``asset_key`` on or after ``since``'s date that
    blocks resolution, as ``<type> <activity id>``; None when there is none.
    An undatable activity counts as on or after."""
    for activity in activities:
        if not activity.symbol or broker_symbol_asset_key(activity.symbol) != asset_key:
            continue
        if activity.activity_type in HARMLESS_ACTIVITY_TYPES:
            continue
        when = _activity_date(activity)
        if when is not None and when < since.date():
            continue
        return f"{activity.activity_type} {activity.activity_id}"
    return None
