from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Literal

from tradepulse.broker import capabilities
from tradepulse.dbae import bands
from tradepulse.models.enums import AssetClass

# DBAE_EVIDENCE_MAX_AGE_SECONDS is defined where it is enforced
# (broker/capabilities.py); the fingerprint below reads that one value.
DBAE_POLICY_VERSION = "dbae-1"
REQUIRED_OPTIONS_LEVEL = 2
ACCEPTED_ACCOUNT_MULTIPLIERS = frozenset({Decimal(2), Decimal(4)})


@dataclass(frozen=True)
class LiquidationScope:
    """Exact scope of a sub-minimum liquidation verdict (spec §8.3). No wildcards."""

    broker_environment: Literal["alpaca-paper", "alpaca-live"]
    account_type: Literal["margin"]
    asset_class: AssetClass
    quantity_kind: Literal["fractional", "whole"]
    order_type: Literal["market"]
    time_in_force: Literal["day", "gtc"]
    session: Literal["regular"]
    minimum_kind: Literal["notional_below_1_usd", "notional_below_1_usd_qty_at_or_above_min_order_size"]


@dataclass(frozen=True)
class LiquidationVerdict:
    verdict: Literal["verified", "refused"]
    evidence_path: str
    manifest_digest: str


# Starts empty; G2 may add at most one paper/equity/fractional row.
SUB_MINIMUM_LIQUIDATION_EVIDENCE: Mapping[LiquidationScope, LiquidationVerdict] = MappingProxyType({})


def dbae_policy_fingerprint() -> dict[str, Any]:
    """JSON-safe snapshot of the DBAE policy, read from the live module values on every call."""
    table = [
        {
            "broker_environment": scope.broker_environment,
            "account_type": scope.account_type,
            "asset_class": scope.asset_class.value,
            "quantity_kind": scope.quantity_kind,
            "order_type": scope.order_type,
            "time_in_force": scope.time_in_force,
            "session": scope.session,
            "minimum_kind": scope.minimum_kind,
            "verdict": row.verdict,
            "evidence_path": row.evidence_path,
            "manifest_digest": row.manifest_digest,
        }
        for scope, row in SUB_MINIMUM_LIQUIDATION_EVIDENCE.items()
    ]
    table.sort(key=lambda entry: [entry[key] for key in sorted(entry)])
    return {
        "dbae_policy_version": DBAE_POLICY_VERSION,
        "band_thresholds": [[band.value, str(edge)] for band, edge in bands.BAND_THRESHOLDS],
        "dbae_evidence_max_age_seconds": capabilities.DBAE_EVIDENCE_MAX_AGE_SECONDS,  # the enforced value, live
        "required_options_level": REQUIRED_OPTIONS_LEVEL,
        "accepted_account_multipliers": sorted(str(m) for m in ACCEPTED_ACCOUNT_MULTIPLIERS),
        "sub_minimum_liquidation_evidence": table,
    }
