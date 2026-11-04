from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Literal


class Band(StrEnum):
    BELOW_MINIMUM = "below_minimum"
    MICRO = "micro"
    MICRO_PLUS = "micro_plus"
    FRACTIONAL = "fractional"
    STANDARD = "standard"
    FULL = "full"


# Inclusive lower edges, ascending (spec §4.1). Read at call time, never copied.
BAND_THRESHOLDS: tuple[tuple[Band, Decimal], ...] = (
    (Band.MICRO, Decimal(20)),
    (Band.MICRO_PLUS, Decimal(50)),
    (Band.FRACTIONAL, Decimal(100)),
    (Band.STANDARD, Decimal(500)),
    (Band.FULL, Decimal(2000)),
)


def band_for(equity: Decimal) -> Band:
    band = Band.BELOW_MINIMUM
    for candidate, lower_edge in BAND_THRESHOLDS:
        if equity >= lower_edge:
            band = candidate
    return band


@dataclass(frozen=True)
class BandUnlocks:
    equity: Literal["none", "fractionable_only", "fractionable_and_affordable_whole", "all"]
    crypto: bool
    options: bool


_UNLOCKS: dict[Band, BandUnlocks] = {
    Band.BELOW_MINIMUM: BandUnlocks("none", False, False),
    Band.MICRO: BandUnlocks("fractionable_only", False, False),
    Band.MICRO_PLUS: BandUnlocks("fractionable_only", False, False),
    Band.FRACTIONAL: BandUnlocks("fractionable_and_affordable_whole", True, False),
    Band.STANDARD: BandUnlocks("all", True, False),
    Band.FULL: BandUnlocks("all", True, True),
}


def unlocks(band: Band) -> BandUnlocks:
    return _UNLOCKS[band]


def floor_to_increment(qty: Decimal, increment: Decimal) -> Decimal:
    """Round qty down to a multiple of increment, in the increment's own exponent."""
    if not increment.is_finite() or increment <= 0:
        raise ValueError(f"increment must be a positive finite Decimal, got {increment}")
    return ((qty // increment) * increment).quantize(increment)
