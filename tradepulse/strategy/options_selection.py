"""Deterministic options-contract selection -- the AI only ever proposes a
directional view on an UNDERLYING (see scanner/coordinator.py's options
branch); this module turns that into a specific, tradeable contract using a
fixed, non-AI rule (expiry-window + OTM-pct-of-spot), then -- Rev.113 --
prefers the nearest of a few strikes whose quoted spread is executable. No
Greeks, IV or open interest, matching this codebase's existing "AI is a market-interpretation aid, never
a risk/selection authority" principle applied elsewhere (ATR stops,
confidence-scaled sizing).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal

OptionType = Literal["call", "put"]


@dataclass(frozen=True, slots=True)
class OptionContractSummary:
    """One eligible contract from an underlying's options chain -- the
    domain-level shape select_contract operates over. Broker-specific chain
    responses (see broker/alpaca_client.py::get_options_chain) are
    translated into these before being passed here."""

    occ_symbol: str
    underlying_symbol: str
    option_type: OptionType
    strike: Decimal
    expiry: date
    contract_multiplier: Decimal = Decimal("100")


def option_candidates(
    direction: OptionType,
    spot_price: Decimal,
    chain: list[OptionContractSummary],
    *,
    min_dte: int,
    max_dte: int,
    target_otm_pct: Decimal,
    now: date,
    count: int = 5,
) -> list[OptionContractSummary]:
    """The `count` strikes nearest the OTM target within one expiry.

    Selection: among contracts of the requested `direction` whose
    days-to-expiry falls in [min_dte, max_dte], take the expiry closest to
    the window's midpoint; within that expiry, order strikes by distance to
    spot_price * (1 + target_otm_pct/100) for a call, or spot_price *
    (1 - target_otm_pct/100) for a put. Ties keep chain order, so the first
    candidate is exactly select_contract's choice. Empty when nothing
    survives the DTE window.
    """
    eligible = [
        contract
        for contract in chain
        if contract.option_type == direction and min_dte <= (contract.expiry - now).days <= max_dte
    ]
    if not eligible:
        return []

    midpoint_dte = (min_dte + max_dte) / 2
    best_expiry = min(eligible, key=lambda c: abs((c.expiry - now).days - midpoint_dte)).expiry
    same_expiry = [c for c in eligible if c.expiry == best_expiry]

    otm_fraction = target_otm_pct / Decimal("100")
    target_strike = spot_price * (Decimal("1") + otm_fraction) if direction == "call" else spot_price * (Decimal("1") - otm_fraction)

    return sorted(same_expiry, key=lambda c: abs(c.strike - target_strike))[:count]


def select_contract(
    direction: OptionType,
    spot_price: Decimal,
    chain: list[OptionContractSummary],
    *,
    min_dte: int,
    max_dte: int,
    target_otm_pct: Decimal,
    now: date,
) -> OptionContractSummary | None:
    """The single contract nearest the OTM target in the midpoint expiry.

    Fail-closed (returns None) if nothing in the chain survives the DTE
    window -- same shape as every other rejection path in the scanner
    (QUOTE_FETCH_FAILED, CANDLE_FETCH_FAILED, etc.): reject this candidate,
    never fabricate a contract.
    """
    found = option_candidates(direction, spot_price, chain, min_dte=min_dte, max_dte=max_dte,
                              target_otm_pct=target_otm_pct, now=now, count=1)
    return found[0] if found else None


def spread_pct(bid: Decimal, ask: Decimal) -> Decimal:
    """Quoted spread as a percent of mid -- the risk engine's own formula."""
    mid = (bid + ask) / 2
    return ((ask - bid) / mid) * 100


def choose_liquid_contract(
    quoted: list[tuple[OptionContractSummary, Decimal, Decimal]], spread_limit_pct: Decimal,
) -> tuple[OptionContractSummary, Decimal, Decimal] | None:
    """Pick from (contract, bid, ask) rows ordered nearest-target first.

    The nearest contract whose spread is within the limit wins, keeping the
    strategy's strike intent. If none qualifies, the tightest is returned so
    the risk engine evaluates -- and records the rejection of -- a real
    contract rather than the candidate silently vanishing.
    """
    if not quoted:
        return None
    for row in quoted:
        if spread_pct(row[1], row[2]) <= spread_limit_pct:
            return row
    return min(quoted, key=lambda row: spread_pct(row[1], row[2]))
