import json
from decimal import Decimal
from types import MappingProxyType

import pytest

from tradepulse.dbae import (
    SUB_MINIMUM_LIQUIDATION_EVIDENCE,
    Band,
    BandUnlocks,
    band_for,
    dbae_policy_fingerprint,
    floor_to_increment,
    unlocks,
)


@pytest.mark.parametrize(
    ("equity", "band"),
    [
        ("19.99", Band.BELOW_MINIMUM),
        ("20", Band.MICRO),
        ("49.99", Band.MICRO),
        ("50", Band.MICRO_PLUS),
        ("99.99", Band.MICRO_PLUS),
        ("100", Band.FRACTIONAL),
        ("499.99", Band.FRACTIONAL),
        ("500", Band.STANDARD),
        ("1999.99", Band.STANDARD),
        ("2000", Band.FULL),
        ("0", Band.BELOW_MINIMUM),
        ("-5", Band.BELOW_MINIMUM),
    ],
)
def test_band_edges(equity: str, band: Band) -> None:
    assert band_for(Decimal(equity)) is band


def test_unlocks_table() -> None:
    assert unlocks(Band.BELOW_MINIMUM) == BandUnlocks("none", False, False)
    assert unlocks(Band.MICRO) == BandUnlocks("fractionable_only", False, False)
    assert unlocks(Band.MICRO_PLUS) == BandUnlocks("fractionable_only", False, False)
    assert unlocks(Band.FRACTIONAL) == BandUnlocks("fractionable_and_affordable_whole", True, False)
    assert unlocks(Band.STANDARD) == BandUnlocks("all", True, False)
    assert unlocks(Band.FULL) == BandUnlocks("all", True, True)


def test_floor_to_increment_scientific_notation() -> None:
    qty = Decimal("0.0000131234567891")
    expected = Decimal("0.000013123")
    assert floor_to_increment(qty, Decimal("1e-9")) == expected
    assert floor_to_increment(qty, Decimal("0.000000001")) == expected
    assert str(floor_to_increment(qty, Decimal("1e-9"))) == str(floor_to_increment(qty, Decimal("0.000000001")))


@pytest.mark.parametrize("increment", [Decimal(0), Decimal(-1)])
def test_floor_to_increment_refuses_nonpositive_increment(increment: Decimal) -> None:
    with pytest.raises(ValueError, match="increment"):
        floor_to_increment(Decimal(1), increment)


def test_evidence_table_starts_empty() -> None:
    assert len(SUB_MINIMUM_LIQUIDATION_EVIDENCE) == 0
    assert isinstance(SUB_MINIMUM_LIQUIDATION_EVIDENCE, MappingProxyType)


def test_policy_fingerprint_is_stable_and_json_safe() -> None:
    first = json.dumps(dbae_policy_fingerprint(), sort_keys=True)
    second = json.dumps(dbae_policy_fingerprint(), sort_keys=True)
    assert first == second
    assert "dbae-1" in first


def test_policy_fingerprint_reads_live_values(monkeypatch: pytest.MonkeyPatch) -> None:
    from tradepulse.dbae import bands, evidence
    from tradepulse.models.enums import AssetClass

    before = json.dumps(dbae_policy_fingerprint(), sort_keys=True)
    monkeypatch.setattr(bands, "BAND_THRESHOLDS", ((Band.MICRO, Decimal(25)),) + bands.BAND_THRESHOLDS[1:])
    assert json.dumps(dbae_policy_fingerprint(), sort_keys=True) != before
    monkeypatch.undo()
    assert json.dumps(dbae_policy_fingerprint(), sort_keys=True) == before

    scope = evidence.LiquidationScope(
        "alpaca-paper", "margin", AssetClass.EQUITY, "fractional", "market", "day", "regular", "notional_below_1_usd"
    )
    row = evidence.LiquidationVerdict("verified", "evidence/x.json", "abc")
    monkeypatch.setattr(evidence, "SUB_MINIMUM_LIQUIDATION_EVIDENCE", {scope: row})
    after = dbae_policy_fingerprint()
    assert after["sub_minimum_liquidation_evidence"] == [
        {
            "broker_environment": "alpaca-paper",
            "account_type": "margin",
            "asset_class": "equity",
            "quantity_kind": "fractional",
            "order_type": "market",
            "time_in_force": "day",
            "session": "regular",
            "minimum_kind": "notional_below_1_usd",
            "verdict": "verified",
            "evidence_path": "evidence/x.json",
            "manifest_digest": "abc",
        }
    ]
    json.dumps(after)
