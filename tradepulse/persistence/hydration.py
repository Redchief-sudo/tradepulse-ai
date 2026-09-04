"""Typed-model <-> stored-payload round trip.

codec.py already handles the encode direction (model -> JSON via
encode_payload). Nothing previously decoded a stored payload dict back into a
typed, validated dataclass -- repositories.get()/find_by_unique()/
list_by_status() only ever returned raw dicts. That is the gap this module
closes: one decode_<table>() function per table, registered here, so callers
that need a real model call hydrate(table, row["payload"]) explicitly.

Kept in persistence/, not on the model classes, so tradepulse.models stays
free of any persistence-layer knowledge.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any

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
    RejectedCandidate,
    ScanRun,
    ScanRunStatus,
    ScanTrigger,
    SessionState,
    SettlementEvent,
    SettlementStatus,
    Side,
    TradeAttribution,
    TradeIntent,
    TradeIntentStatus,
    TradingSession,
)

HydrateFn = Callable[[Mapping[str, Any]], Any]
_HYDRATORS: dict[str, HydrateFn] = {}


def register(table: str, fn: HydrateFn) -> None:
    _HYDRATORS[table] = fn


def hydrate(table: str, payload: Mapping[str, Any]) -> Any:
    try:
        fn = _HYDRATORS[table]
    except KeyError as exc:
        raise ValueError(f"no hydrator registered for table: {table}") from exc
    return fn(payload)


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value))


def _decimal_or_none(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _datetime(value: Any) -> datetime:
    return datetime.fromisoformat(value)


def _datetime_or_none(value: Any) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


def _asset_identity(d: Mapping[str, Any]) -> AssetIdentity:
    return AssetIdentity(
        symbol=d["symbol"],
        asset_class=AssetClass(d["asset_class"]),
        native_asset_id=d["native_asset_id"],
        venue=d.get("venue"),
        metadata=d.get("metadata") or {},
    )


def _market_quote(d: Mapping[str, Any]) -> MarketQuote:
    return MarketQuote(
        asset=_asset_identity(d["asset"]),
        price=_decimal(d["price"]),
        observed_at=_datetime(d["observed_at"]),
        received_at=_datetime(d["received_at"]),
        provider=d["provider"],
        market_generation=d["market_generation"],
        bid=_decimal_or_none(d.get("bid")),
        ask=_decimal_or_none(d.get("ask")),
    )


def decode_opportunity(d: Mapping[str, Any]) -> Opportunity:
    return Opportunity(
        opportunity_id=d["opportunity_id"],
        scan_generation=d["scan_generation"],
        asset=_asset_identity(d["asset"]),
        quote=_market_quote(d["quote"]),
        source=d["source"],
        created_at=_datetime(d["created_at"]),
        confidence=d.get("confidence"),
        metadata=d.get("metadata") or {},
    )


def decode_trade_intent(d: Mapping[str, Any]) -> TradeIntent:
    return TradeIntent(
        trade_intent_id=d["trade_intent_id"],
        idempotency_key=d["idempotency_key"],
        correlation_id=d["correlation_id"],
        asset=_asset_identity(d["asset"]),
        side=Side(d["side"]),
        execution_mode=ExecutionMode(d["execution_mode"]),
        strategy=d["strategy"],
        created_at=_datetime(d["created_at"]),
        requested_quantity=_decimal_or_none(d.get("requested_quantity")),
        requested_notional=_decimal_or_none(d.get("requested_notional")),
        reference_price=_decimal_or_none(d.get("reference_price")),
        confidence=d.get("confidence"),
        risk_snapshot=d.get("risk_snapshot") or {},
        status=TradeIntentStatus(d.get("status", TradeIntentStatus.PROPOSED.value)),
        filled_quantity=_decimal(d.get("filled_quantity", "0")),
        filled_avg_price=_decimal_or_none(d.get("filled_avg_price")),
        broker_order_id=d.get("broker_order_id"),
        client_order_id=d.get("client_order_id"),
        reserved_cash=_decimal_or_none(d.get("reserved_cash")),
        consumed_cash=_decimal_or_none(d.get("consumed_cash")),
        realized_pnl=_decimal_or_none(d.get("realized_pnl")),
        rejection_reason=d.get("rejection_reason"),
        sector=d.get("sector"),
        stop_loss=_decimal_or_none(d.get("stop_loss")),
        target_price=_decimal_or_none(d.get("target_price")),
    )


def decode_order(d: Mapping[str, Any]) -> Order:
    return Order(
        order_id=d["order_id"],
        trade_intent_id=d["trade_intent_id"],
        idempotency_key=d["idempotency_key"],
        asset=_asset_identity(d["asset"]),
        side=Side(d["side"]),
        execution_mode=ExecutionMode(d["execution_mode"]),
        status=OrderStatus(d["status"]),
        requested_quantity=_decimal(d["requested_quantity"]),
        submitted_at=_datetime(d["submitted_at"]),
        broker_order_id=d.get("broker_order_id"),
        filled_quantity=_decimal(d.get("filled_quantity", "0")),
        average_fill_price=_decimal_or_none(d.get("average_fill_price")),
    )


def decode_fill(d: Mapping[str, Any]) -> Fill:
    return Fill(
        fill_id=d["fill_id"],
        trade_intent_id=d["trade_intent_id"],
        order_id=d["order_id"],
        asset=_asset_identity(d["asset"]),
        side=Side(d["side"]),
        execution_mode=ExecutionMode(d["execution_mode"]),
        quantity=_decimal(d["quantity"]),
        price=_decimal(d["price"]),
        fees=_decimal(d["fees"]),
        slippage=_decimal(d["slippage"]),
        filled_at=_datetime(d["filled_at"]),
        broker_fill_id=d.get("broker_fill_id"),
    )


def decode_settlement(d: Mapping[str, Any]) -> SettlementEvent:
    return SettlementEvent(
        settlement_event_id=d["settlement_event_id"],
        fill_id=d["fill_id"],
        trade_intent_id=d["trade_intent_id"],
        asset=_asset_identity(d["asset"]),
        side=Side(d["side"]),
        execution_mode=ExecutionMode(d["execution_mode"]),
        quantity=_decimal(d["quantity"]),
        price=_decimal(d["price"]),
        occurred_at=_datetime(d["occurred_at"]),
        status=SettlementStatus(d.get("status", SettlementStatus.PENDING.value)),
        fees=_decimal(d.get("fees", "0")),
        sector=d.get("sector"),
        broker_order_id=d.get("broker_order_id"),
        broker_fill_id=d.get("broker_fill_id"),
        client_order_id=d.get("client_order_id"),
        lot_projected=d.get("lot_projected", False),
        attribution_projected=d.get("attribution_projected", False),
        cash_projected=d.get("cash_projected", False),
        holding_projected=d.get("holding_projected", False),
        trade_projected=d.get("trade_projected", False),
        integrity_verified=d.get("integrity_verified", False),
        realized_pnl=_decimal_or_none(d.get("realized_pnl")),
        attempt_count=d.get("attempt_count", 0),
        error_code=d.get("error_code"),
        next_retry_at=_datetime_or_none(d.get("next_retry_at")),
        processing_owner=d.get("processing_owner"),
        processing_started_at=_datetime_or_none(d.get("processing_started_at")),
    )


def decode_holding(d: Mapping[str, Any]) -> Holding:
    return Holding(
        asset=_asset_identity(d["asset"]),
        quantity=_decimal(d["quantity"]),
        average_price=_decimal(d["average_price"]),
        updated_at=_datetime(d["updated_at"]),
        sector=d.get("sector"),
        stop_loss=_decimal_or_none(d.get("stop_loss")),
        target_price=_decimal_or_none(d.get("target_price")),
        current_stop=_decimal_or_none(d.get("current_stop")),
    )


def decode_position_lot(d: Mapping[str, Any]) -> PositionLot:
    return PositionLot(
        lot_id=d["lot_id"],
        originating_fill_id=d["originating_fill_id"],
        asset=_asset_identity(d["asset"]),
        position_side=d["position_side"],
        opened_quantity=_decimal(d["opened_quantity"]),
        remaining_quantity=_decimal(d["remaining_quantity"]),
        acquisition_price=_decimal(d["acquisition_price"]),
        opened_at=_datetime(d["opened_at"]),
        closures={k: _decimal(v) for k, v in (d.get("closures") or {}).items()},
        realized_pnl=_decimal(d.get("realized_pnl", "0")),
        mfe_price=_decimal_or_none(d.get("mfe_price")),
        mae_price=_decimal_or_none(d.get("mae_price")),
    )


def decode_trade_attribution(d: Mapping[str, Any]) -> TradeAttribution:
    return TradeAttribution(
        attribution_id=d["attribution_id"],
        asset=_asset_identity(d["asset"]),
        lot_id=d["lot_id"],
        opening_trade_intent_id=d["opening_trade_intent_id"],
        closing_trade_intent_id=d["closing_trade_intent_id"],
        closing_fill_id=d["closing_fill_id"],
        quantity=_decimal(d["quantity"]),
        entry_price=_decimal(d["entry_price"]),
        entry_at=_datetime(d["entry_at"]),
        exit_price=_decimal(d["exit_price"]),
        exit_at=_datetime(d["exit_at"]),
        realized_pnl=_decimal(d["realized_pnl"]),
        created_at=_datetime(d["created_at"]),
        exit_reason=d.get("exit_reason"),
        max_favorable_excursion=_decimal_or_none(d.get("max_favorable_excursion")),
        max_adverse_excursion=_decimal_or_none(d.get("max_adverse_excursion")),
        entry_context=d.get("entry_context") or {},
    )


def decode_cash_ledger_entry(d: Mapping[str, Any]) -> CashLedgerEntry:
    return CashLedgerEntry(
        entry_id=d["entry_id"],
        idempotency_key=d["idempotency_key"],
        amount=_decimal(d["amount"]),
        currency=d["currency"],
        occurred_at=_datetime(d["occurred_at"]),
        reason=d["reason"],
    )


def decode_pnl_record(d: Mapping[str, Any]) -> PnlRecord:
    return PnlRecord(
        record_id=d["record_id"],
        asset=_asset_identity(d["asset"]),
        realized=_decimal(d["realized"]),
        unrealized=_decimal(d["unrealized"]),
        as_of=_datetime(d["as_of"]),
    )


def decode_reconciliation_record(d: Mapping[str, Any]) -> ReconciliationRecord:
    return ReconciliationRecord(
        record_id=d["record_id"],
        reconciliation_type=d["reconciliation_type"],
        subject_id=d["subject_id"],
        outcome=ReconciliationOutcome(d["outcome"]),
        expected=d.get("expected") or {},
        actual=d.get("actual") or {},
        occurred_at=_datetime(d["occurred_at"]),
        corrective_action=d.get("corrective_action"),
    )


def decode_trading_session(d: Mapping[str, Any]) -> TradingSession:
    return TradingSession(
        session_id=d["session_id"],
        state=SessionState(d["state"]),
        trading_active=d["trading_active"],
        updated_at=_datetime(d["updated_at"]),
        kill_switch_reason=d.get("kill_switch_reason"),
        kill_switch_at=_datetime_or_none(d.get("kill_switch_at")),
        kill_switch_reset_required=d.get("kill_switch_reset_required", False),
        financial_integrity_reason=d.get("financial_integrity_reason"),
        financial_integrity_manual_reenable_required=d.get(
            "financial_integrity_manual_reenable_required", False
        ),
    )


def decode_audit_event(d: Mapping[str, Any]) -> AuditEvent:
    return AuditEvent(
        event_id=d["event_id"],
        event_type=d["event_type"],
        severity=d["severity"],
        message=d["message"],
        occurred_at=_datetime(d["occurred_at"]),
        correlation_id=d.get("correlation_id"),
        entity_type=d.get("entity_type"),
        entity_id=d.get("entity_id"),
        details=d.get("details") or {},
    )


def decode_scan_run(d: Mapping[str, Any]) -> ScanRun:
    return ScanRun(
        scan_run_id=d["scan_run_id"],
        scan_generation=d["scan_generation"],
        trigger=ScanTrigger(d["trigger"]),
        asset_class=AssetClass(d["asset_class"]),
        status=ScanRunStatus(d["status"]),
        started_at=_datetime(d["started_at"]),
        lock_owner_token=d["lock_owner_token"],
        completed_at=_datetime_or_none(d.get("completed_at")),
        candidates_discovered=d.get("candidates_discovered", 0),
        candidates_approved=d.get("candidates_approved", 0),
        orders_submitted=d.get("orders_submitted", 0),
        error=d.get("error"),
        market_data_tier=d.get("market_data_tier"),
        equity_feed=d.get("equity_feed"),
        option_feed=d.get("option_feed"),
        universe_size=d.get("universe_size", 0),
        ai_response_request_id=d.get("ai_response_request_id"),
        regime=d.get("regime"),
        regime_reason=d.get("regime_reason"),
        regime_confidence=d.get("regime_confidence"),
        regime_position_multiplier=_decimal_or_none(d.get("regime_position_multiplier")),
        regime_realized_vol=_decimal_or_none(d.get("regime_realized_vol")),
        regime_weight_profile=d.get("regime_weight_profile"),
    )


def decode_rejected_candidate(d: Mapping[str, Any]) -> RejectedCandidate:
    return RejectedCandidate(
        rejection_id=d["rejection_id"],
        scan_run_id=d["scan_run_id"],
        scan_generation=d["scan_generation"],
        symbol=d["symbol"],
        asset_class=AssetClass(d["asset_class"]),
        reason=d["reason"],
        occurred_at=_datetime(d["occurred_at"]),
        context=d.get("context") or {},
    )


def decode_equity_snapshot(d: Mapping[str, Any]) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        snapshot_id=d["snapshot_id"],
        as_of=_datetime(d["as_of"]),
        total_equity=_decimal(d["total_equity"]),
        cash_balance=_decimal(d["cash_balance"]),
        holdings_value=_decimal(d["holdings_value"]),
        sector_exposure={k: _decimal(v) for k, v in (d.get("sector_exposure") or {}).items()},
        open_positions=d["open_positions"],
        outstanding_orders=d["outstanding_orders"],
        trades_today=d["trades_today"],
        daily_pnl_pct=_decimal(d["daily_pnl_pct"]),
        source=d["source"],
    )


def decode_ai_response(d: Mapping[str, Any]) -> AIResponse:
    return AIResponse(
        request_id=d["request_id"],
        provider=d["provider"],
        model=d["model"],
        schema_version=d["schema_version"],
        completed_at=_datetime(d["completed_at"]),
        result=d.get("result") or {},
        latency_ms=d["latency_ms"],
    )


register("opportunities", decode_opportunity)
register("trade_intents", decode_trade_intent)
register("orders", decode_order)
register("fills", decode_fill)
register("settlements", decode_settlement)
register("holdings", decode_holding)
register("position_lots", decode_position_lot)
register("cash_ledger", decode_cash_ledger_entry)
register("pnl_records", decode_pnl_record)
register("reconciliation_records", decode_reconciliation_record)
register("trading_sessions", decode_trading_session)
register("audit_events", decode_audit_event)
register("scan_runs", decode_scan_run)
register("equity_snapshots", decode_equity_snapshot)
register("ai_responses", decode_ai_response)
register("trade_attributions", decode_trade_attribution)
register("rejected_candidates", decode_rejected_candidate)
