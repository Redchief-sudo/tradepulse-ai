from datetime import UTC, datetime
from decimal import Decimal

from tradepulse.models import (
    AIResponse,
    AssetClass,
    AssetIdentity,
    AuditEvent,
    CashLedgerEntry,
    ExecutionMode,
    Fill,
    Holding,
    MarketQuote,
    Opportunity,
    Order,
    OrderStatus,
    PnlRecord,
    PortfolioSnapshot,
    PositionLot,
    ReconciliationOutcome,
    ReconciliationRecord,
    ScanRun,
    ScanRunStatus,
    ScanTrigger,
    SessionState,
    SettlementEvent,
    SettlementStatus,
    Side,
    TradeAttribution,
    TradeIntent,
    TradingSession,
)
from tradepulse.persistence import hydrate
from tradepulse.persistence.codec import decode_payload, encode_payload


NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)


def asset() -> AssetIdentity:
    return AssetIdentity("BTC/USD", AssetClass.CRYPTO, "alpaca:BTC/USD", "alpaca", {"quote": "USD"})


def quote() -> MarketQuote:
    return MarketQuote(asset(), Decimal("65000.50"), NOW, NOW, "alpaca", 7, bid=Decimal("65000"), ask=Decimal("65001"))


def roundtrip(table: str, instance: object) -> object:
    payload = decode_payload(encode_payload(instance))
    return hydrate(table, payload)


def test_opportunity_roundtrips() -> None:
    original = Opportunity("opp-1", "gen-1", asset(), quote(), "scanner", NOW, confidence=72.5, metadata={"note": "x"})
    assert roundtrip("opportunities", original) == original


def test_trade_intent_roundtrips() -> None:
    original = TradeIntent(
        "ti-1", "idem-1", "corr-1", asset(), Side.BUY, ExecutionMode.PAPER, "momentum", NOW,
        requested_quantity=Decimal("0.001"), reference_price=Decimal("65000"), confidence=80,
        risk_snapshot={"profile": "balanced"},
    )
    assert roundtrip("trade_intents", original) == original


def test_order_roundtrips() -> None:
    original = Order(
        "order-1", "ti-1", "idem-1", asset(), Side.BUY, ExecutionMode.PAPER, OrderStatus.PARTIALLY_FILLED,
        Decimal("0.010"), NOW, broker_order_id="broker-1", filled_quantity=Decimal("0.004"),
        average_fill_price=Decimal("65010"),
    )
    assert roundtrip("orders", original) == original


def test_fill_roundtrips() -> None:
    original = Fill(
        "fill-1", "ti-1", "order-1", asset(), Side.BUY, ExecutionMode.PAPER,
        Decimal("0.001"), Decimal("65010"), Decimal("0.05"), Decimal("0.01"), NOW, broker_fill_id="bf-1",
    )
    assert roundtrip("fills", original) == original


def test_settlement_roundtrips() -> None:
    original = SettlementEvent(
        "se-1", "fill-1", "ti-1", asset(), Side.BUY, ExecutionMode.PAPER, Decimal("0.001"), Decimal("65010"), NOW,
        status=SettlementStatus.COMPLETED, fees=Decimal("0.05"), broker_order_id="bo-1", broker_fill_id="bf-1",
        client_order_id="co-1", lot_projected=True, cash_projected=True, holding_projected=True,
        trade_projected=True, integrity_verified=True, realized_pnl=Decimal("1.25"), attempt_count=1,
    )
    assert roundtrip("settlements", original) == original


def test_holding_roundtrips() -> None:
    original = Holding(
        asset(), Decimal("0.5"), Decimal("64000"), NOW, sector="Technology",
        stop_loss=Decimal("60000"), target_price=Decimal("70000"),
    )
    assert roundtrip("holdings", original) == original


def test_position_lot_roundtrips() -> None:
    original = PositionLot(
        "lot-1", "fill-1", asset(), "long", Decimal("0.5"), Decimal("0.3"), Decimal("64000"), NOW,
        closures={"fill-2": Decimal("0.2")}, realized_pnl=Decimal("3.40"),
    )
    assert roundtrip("position_lots", original) == original


def test_position_lot_roundtrips_with_mfe_mae() -> None:
    original = PositionLot(
        "lot-2", "fill-3", asset(), "long", Decimal("0.5"), Decimal("0.5"), Decimal("64000"), NOW,
        mfe_price=Decimal("66500"), mae_price=Decimal("63200"),
    )
    assert roundtrip("position_lots", original) == original


def test_position_lot_hydrates_legacy_row_missing_mfe_mae() -> None:
    """A row persisted before Outcome Attribution shipped -- hydration must
    default mfe_price/mae_price to None, never raise."""
    legacy_payload = {
        "lot_id": "lot-legacy", "originating_fill_id": "fill-legacy", "asset": {
            "symbol": "BTC/USD", "asset_class": "crypto", "native_asset_id": "alpaca:BTC/USD",
            "venue": "alpaca", "metadata": {"quote": "USD"},
        },
        "position_side": "long", "opened_quantity": "0.5", "remaining_quantity": "0.5",
        "acquisition_price": "64000", "opened_at": NOW.isoformat(),
        # mfe_price / mae_price deliberately omitted -- the exact pre-Outcome-Attribution row shape
    }
    result = hydrate("position_lots", legacy_payload)
    assert result.mfe_price is None
    assert result.mae_price is None


def test_trade_attribution_roundtrips() -> None:
    original = TradeAttribution(
        attribution_id="lot-1:fill-2", asset=asset(), lot_id="lot-1", opening_trade_intent_id="ti-1",
        closing_trade_intent_id="ti-2", closing_fill_id="fill-2", quantity=Decimal("0.2"),
        entry_price=Decimal("64000"), entry_at=NOW, exit_price=Decimal("66000"), exit_at=NOW,
        realized_pnl=Decimal("400"), created_at=NOW, exit_reason="target_price",
        max_favorable_excursion=Decimal("66500"), max_adverse_excursion=Decimal("63200"),
        entry_context={"risk_snapshot": {"regime": "low_vol_bull"}, "opportunity_metadata": {"composite_score": "82"}},
    )
    assert roundtrip("trade_attributions", original) == original


def test_trade_attribution_roundtrips_with_none_optional_fields() -> None:
    original = TradeAttribution(
        attribution_id="lot-3:fill-4", asset=asset(), lot_id="lot-3", opening_trade_intent_id="ti-3",
        closing_trade_intent_id="ti-4", closing_fill_id="fill-4", quantity=Decimal("0.1"),
        entry_price=Decimal("64000"), entry_at=NOW, exit_price=Decimal("63500"), exit_at=NOW,
        realized_pnl=Decimal("-50"), created_at=NOW,
    )
    assert roundtrip("trade_attributions", original) == original


def test_cash_ledger_entry_roundtrips() -> None:
    original = CashLedgerEntry("entry-1", "idem-cash-1", Decimal("-65.01"), "USD", NOW, "buy settlement")
    assert roundtrip("cash_ledger", original) == original


def test_pnl_record_roundtrips() -> None:
    original = PnlRecord("pnl-1", asset(), Decimal("12.50"), Decimal("-3.20"), NOW)
    assert roundtrip("pnl_records", original) == original


def test_reconciliation_record_roundtrips() -> None:
    original = ReconciliationRecord(
        "rec-1", "position", "BTC/USD", ReconciliationOutcome.DRIFT_DETECTED,
        {"quantity": "0.5"}, {"quantity": "0.48"}, NOW,
    )
    assert roundtrip("reconciliation_records", original) == original


def test_trading_session_roundtrips() -> None:
    original = TradingSession(
        "session-1", SessionState.RISK_STOPPED, False, NOW,
        kill_switch_reason="daily loss limit breached", kill_switch_at=NOW, kill_switch_reset_required=True,
    )
    assert roundtrip("trading_sessions", original) == original


def test_audit_event_roundtrips() -> None:
    original = AuditEvent(
        "evt-1", "kill_switch_tripped", "critical", "daily loss limit breached", NOW,
        correlation_id="corr-1", entity_type="trading_session", entity_id="session-1", details={"pct": "-1.2"},
    )
    assert roundtrip("audit_events", original) == original


def test_scan_run_roundtrips() -> None:
    original = ScanRun(
        "scan-1", "gen-1", ScanTrigger.SCHEDULED, AssetClass.EQUITY, ScanRunStatus.COMPLETED, NOW, "owner-1",
        completed_at=NOW, candidates_discovered=5, candidates_approved=2, orders_submitted=1,
        universe_size=12, ai_response_request_id="ai-req-1",
    )
    assert roundtrip("scan_runs", original) == original


def test_scan_run_roundtrips_with_regime_fields() -> None:
    original = ScanRun(
        "scan-2", "gen-2", ScanTrigger.SCHEDULED, AssetClass.EQUITY, ScanRunStatus.COMPLETED, NOW, "owner-2",
        completed_at=NOW, candidates_discovered=3, candidates_approved=1, orders_submitted=1,
        universe_size=8, ai_response_request_id="ai-req-2",
        regime="high_vol_bear", regime_reason=None, regime_confidence=62,
        regime_position_multiplier=Decimal("0.5"), regime_realized_vol=Decimal("0.21"),
    )
    assert roundtrip("scan_runs", original) == original


def test_scan_run_roundtrips_with_unavailable_regime_fallback() -> None:
    """The Market Regime Phase 2 fail-closed fallback path -- regime ==
    "unavailable" with a truthful regime_reason and no confidence/vol
    (the classifier was never reached)."""
    original = ScanRun(
        "scan-3", "gen-3", ScanTrigger.SCHEDULED, AssetClass.CRYPTO, ScanRunStatus.COMPLETED, NOW, "owner-3",
        completed_at=NOW, candidates_discovered=0, candidates_approved=0, orders_submitted=0,
        regime="unavailable", regime_reason="benchmark_fetch_failed",
        regime_position_multiplier=Decimal("0.5"),
    )
    assert roundtrip("scan_runs", original) == original


def test_scan_run_hydrates_legacy_row_missing_regime_fields() -> None:
    """A row persisted before Market Regime Phase 2 shipped -- hydration
    must default all five regime fields safely (all None), never raise.
    Mirrors test_scan_run_hydrates_legacy_row_missing_universe_size_and_ai_response_request_id."""
    legacy_payload = {
        "scan_run_id": "scan-legacy-regime", "scan_generation": "gen-0", "trigger": "scheduled",
        "asset_class": "equity", "status": "completed", "started_at": NOW.isoformat(),
        "lock_owner_token": "owner-0", "completed_at": NOW.isoformat(),
        "candidates_discovered": 2, "candidates_approved": 1, "orders_submitted": 1, "error": None,
        "market_data_tier": "basic", "equity_feed": "iex", "option_feed": "indicative",
        "universe_size": 5, "ai_response_request_id": "ai-req-legacy",
        # regime / regime_reason / regime_confidence / regime_position_multiplier /
        # regime_realized_vol deliberately omitted -- the exact pre-Phase-2 row shape
    }
    result = hydrate("scan_runs", legacy_payload)
    assert result.regime is None
    assert result.regime_reason is None
    assert result.regime_confidence is None
    assert result.regime_position_multiplier is None
    assert result.regime_realized_vol is None


def test_scan_run_hydrates_legacy_row_missing_universe_size_and_ai_response_request_id() -> None:
    """A row persisted before these two observability-only fields existed --
    hydration must default them safely (universe_size=0,
    ai_response_request_id=None), never raise. Mirrors the earlier
    asset_class-missing dashboard incident: an optional field added later
    must never break an old row."""
    legacy_payload = {
        "scan_run_id": "scan-legacy", "scan_generation": "gen-0", "trigger": "scheduled",
        "asset_class": "equity", "status": "completed", "started_at": NOW.isoformat(),
        "lock_owner_token": "owner-0", "completed_at": NOW.isoformat(),
        "candidates_discovered": 2, "candidates_approved": 1, "orders_submitted": 1, "error": None,
        "market_data_tier": "basic", "equity_feed": "iex", "option_feed": "indicative",
        # universe_size / ai_response_request_id deliberately omitted -- the exact legacy-row shape
    }
    result = hydrate("scan_runs", legacy_payload)
    assert result.universe_size == 0
    assert result.ai_response_request_id is None


def test_equity_snapshot_roundtrips() -> None:
    original = PortfolioSnapshot(
        "snap-1", NOW, Decimal("10500.25"), Decimal("4000"), Decimal("6500.25"),
        {"technology": Decimal("2500"), "crypto": Decimal("4000.25")}, 3, 1, 2, Decimal("0.75"), "broker",
    )
    assert roundtrip("equity_snapshots", original) == original


def test_ai_response_roundtrips() -> None:
    original = AIResponse(
        request_id="req-1", provider="anthropic", model="claude-haiku-4-5", schema_version="1.0",
        completed_at=NOW, result={"candidates": [{"symbol": "AAPL", "recommendation": "BUY", "confidence": 82.0, "summary": "ok"}]},
        latency_ms=420,
    )
    assert roundtrip("ai_responses", original) == original
