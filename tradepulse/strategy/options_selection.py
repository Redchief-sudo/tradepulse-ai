"""Deterministic options-contract selection -- the AI only ever proposes a
directional view on an UNDERLYING (see scanner/coordinator.py's options
branch); this module turns that into a specific, tradeable contract using a
fixed, non-AI rule. No Greeks, no IV, no liquidity/open-interest filtering
-- a first cut deliberately kept simple (expiry-window + OTM-pct-of-spot),
matching this codebase's existing "AI is a market-interpretation aid, never
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
    """Fail-closed (returns None) if nothing in the chain survives the DTE
    window -- same shape as every other rejection path in the scanner
    (QUOTE_FETCH_FAILED, CANDLE_FETCH_FAILED, etc.): reject this candidate,
    never fabricate a contract.

    Selection: among contracts of the requested `direction` whose
    days-to-expiry falls in [min_dte, max_dte], pick the expiry closest to
    the window's midpoint; within that expiry, pick the strike closest to
    spot_price * (1 + target_otm_pct/100) for a call, or spot_price *
    (1 - target_otm_pct/100) for a put.
    """
    eligible = [
        contract
        for contract in chain
        if contract.option_type == direction and min_dte <= (contract.expiry - now).days <= max_dte
    ]
    if not eligible:
        return None

    midpoint_dte = (min_dte + max_dte) / 2
    best_expiry = min(eligible, key=lambda c: abs((c.expiry - now).days - midpoint_dte)).expiry
    same_expiry = [c for c in eligible if c.expiry == best_expiry]

    otm_fraction = target_otm_pct / Decimal("100")
    target_strike = spot_price * (Decimal("1") + otm_fraction) if direction == "call" else spot_price * (Decimal("1") - otm_fraction)

    return min(same_expiry, key=lambda c: abs(c.strike - target_strike))
