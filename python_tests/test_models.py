from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tradepulse.models import (
    AssetClass, AssetIdentity, ExecutionMode, Fill, MarketQuote, Opportunity,
    SettlementEvent, SettlementStatus, Side, TradeIntent,
)


NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)


def crypto_asset() -> AssetIdentity:
    return AssetIdentity("BTC/USD", AssetClass.CRYPTO, "alpaca:BTC/USD", "alpaca", {"quote": "USD"})


def test_native_multi_asset_identity_is_preserved_and_immutable() -> None:
    asset = crypto_asset()
    assert asset.symbol == "BTC/USD"
    assert asset.native_asset_id == "alpaca:BTC/USD"
    assert asset.metadata["quote"] == "USD"
    with pytest.raises(TypeError):
        asset.metadata["quote"] = "USDT"


def test_asset_class_requires_canonical_enum() -> None:
    with pytest.raises(TypeError):
        AssetIdentity("BTC/USD", "crypto", "alpaca:BTC/USD")  # type: ignore[arg-type]


def test_market_quote_freshness_rejects_stale_or_future_data() -> None:
    quote = MarketQuote(crypto_asset(), Decimal("65000"), NOW, NOW, "alpaca", 7)
    assert quote.is_fresh(NOW + timedelta(seconds=30), 60)
    assert not quote.is_fresh(NOW + timedelta(seconds=61), 60)
    assert not quote.is_fresh(NOW - timedelta(seconds=1), 60)


def test_opportunity_cannot_attach_quote_for_another_asset() -> None:
    equity = AssetIdentity("MSFT", AssetClass.EQUITY, "alpaca:MSFT")
    quote = MarketQuote(crypto_asset(), Decimal("65000"), NOW, NOW, "alpaca", 1)
    with pytest.raises(ValueError, match="quote asset"):
        Opportunity("opp-1", "scan-1", equity, quote, "scanner", NOW)


def test_trade_intent_requires_stable_identity_and_size() -> None:
    with pytest.raises(ValueError, match="requested_quantity"):
        TradeIntent("ti-1", "idem-1", "corr-1", crypto_asset(), Side.BUY, ExecutionMode.PAPER, "momentum", NOW)
    intent = TradeIntent(
        "ti-1", "idem-1", "corr-1", crypto_asset(), Side.BUY, ExecutionMode.PAPER,
        "momentum", NOW, requested_quantity=Decimal("0.001"), reference_price=Decimal("65000"),
    )
    assert intent.requested_quantity == Decimal("0.001")


def test_fill_is_confirmed_fact_with_separate_costs() -> None:
    fill = Fill(
        "fill-1", "ti-1", "order-1", crypto_asset(), Side.BUY, ExecutionMode.PAPER,
        Decimal("0.001"), Decimal("65010"), Decimal("0.05"), Decimal("0.01"), NOW,
    )
    assert fill.notional == Decimal("65.010")
    assert fill.fees == Decimal("0.05")


def test_completed_settlement_requires_integrity_verification() -> None:
    with pytest.raises(ValueError, match="integrity_verified"):
        SettlementEvent(
            "se-1", "fill-1", "ti-1", crypto_asset(), Side.BUY, ExecutionMode.PAPER,
            Decimal("0.001"), Decimal("65010"), NOW, status=SettlementStatus.COMPLETED,
        )
    event = SettlementEvent(
        "se-1", "fill-1", "ti-1", crypto_asset(), Side.BUY, ExecutionMode.PAPER,
        Decimal("0.001"), Decimal("65010"), NOW, status=SettlementStatus.COMPLETED, integrity_verified=True,
    )
    assert event.integrity_verified
