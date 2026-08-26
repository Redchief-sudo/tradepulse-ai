"""Broker reconciliation: Alpaca is always the source of truth for facts
about the account. This is an after-the-fact audit pass (its own CLI
subcommand, its own cron cadence) -- not a live-protection concern like the
position monitor.

Position reconciliation is a THREE-way comparison per symbol -- broker
position, local `position_lots` (the accounting), and local `holdings` (a
materialized VIEW derived from those lots). Only when the lots themselves
already agree with the broker is it safe to auto-correct the Holding view
(a pure resync of a view that's documented as a cache of reality, never an
independent ledger). When the lots disagree with the broker, that's
accounting drift -- the Holding is deliberately left alone and NOT claimed
corrected, because fixing the view would hide a real problem in the
fill/lot history underneath it; a human is alerted instead.

Fill reconciliation first tries an exact match on Alpaca's real per-fill
activity ID (`Fill.broker_fill_id == activity.activity_id`) -- the
execution gateway (execution/fill_attribution.py::attribute_order_fills) has
carried that real ID since Fill records started being created from
validated Alpaca FILL activities rather than a locally-synthesized ID. Only
a local Fill with no activity-ID match at all (e.g. one predating that
change) falls back to the older symbol/qty/price/time-window heuristic,
which is never presented as an authoritative match.

An Alpaca activity with no local match at all is a candidate for late-fill
recovery, never silent fabrication: if its `order_id` (Alpaca's own field)
matches a known local TradeIntent, that's proof the fill genuinely belongs
to a trade this system placed -- reconciliation runs it through the exact
same accounting path the gateway uses (execution/fill_attribution.py::
resolve_order_from_broker: attribute_order_fills, then
SettlementProcessor.process_pending()), never a second, reconciliation-local
way of deriving lot/holding state. Only when no known TradeIntent's
broker_order_id matches at all is the activity a genuinely orphaned/missed
fill; that case is recorded and alerted, never auto-corrected.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal
from uuid import uuid4

from tradepulse.alerts import TelegramAlerter
from tradepulse.broker import AlpacaActivity, AlpacaClient
from tradepulse.models import (
    AssetIdentity,
    Fill,
    Holding,
    ReconciliationOutcome,
    ReconciliationRecord,
    TradeIntent,
    asset_identity_key,
    asset_key_from_broker_symbol,
)
from tradepulse.persistence import PersistenceRepositories, hydrate
from tradepulse.risk import latch_financial_integrity_block
from tradepulse.settlement import SettlementProcessor

from ..execution.fill_attribution import resolve_order_from_broker

_OPEN_LOT_STATUSES = ("open", "partially_closed")
_FILL_MATCH_WINDOW_SECONDS = 300

ReconciliationStatus = Literal["ok", "degraded"]


@dataclass(frozen=True, slots=True)
class ReconciliationSummary:
    status: ReconciliationStatus
    positions_checked: int = 0
    view_drift_corrected: int = 0
    accounting_drift_detected: int = 0
    fills_checked: int = 0
    missed_fills_detected: int = 0
    late_fills_recovered: int = 0
    error: str | None = None


async def _record(repositories: PersistenceRepositories, **kwargs) -> None:
    record = ReconciliationRecord(record_id=str(uuid4()), **kwargs)
    await repositories.reconciliation_records.create_once(record.record_id, record)


async def _rebuild_holding_from_lots(
    repositories: PersistenceRepositories, asset: AssetIdentity, now: datetime
) -> Holding | None:
    """Mirrors settlement/engine.py::_project_holding's own recompute (not
    imported directly -- that function is keyed off a SettlementEvent this
    caller doesn't have, and this codebase's convention is a small local
    duplicate over reaching into another module's private internals, same
    as execution/gateway.py's own `symbol.upper()` holding-key convention)."""
    lot_rows = await repositories.position_lots.list_all(limit=10000)
    lots = [hydrate("position_lots", row["payload"]) for row in lot_rows]
    target_key = asset_identity_key(asset)
    open_lots = [lot for lot in lots if asset_identity_key(lot.asset) == target_key and lot.status in _OPEN_LOT_STATUSES]
    if not open_lots:
        return None

    total_signed = sum((lot.signed_quantity for lot in open_lots), Decimal("0"))
    total_remaining = sum((lot.remaining_quantity for lot in open_lots), Decimal("0"))
    total_cost = sum((lot.remaining_quantity * lot.acquisition_price for lot in open_lots), Decimal("0"))
    avg_price = total_cost / total_remaining

    oldest_lot = min(open_lots, key=lambda lot: lot.opened_at)  # PROTECTIVE_THRESHOLD_POLICY = "first_entry"
    stop_loss = target_price = None
    fill_row = await repositories.fills.get(oldest_lot.originating_fill_id)
    if fill_row is not None:
        fill = hydrate("fills", fill_row["payload"])
        intent_row = await repositories.trade_intents.get(fill.trade_intent_id)
        if intent_row is not None:
            intent = hydrate("trade_intents", intent_row["payload"])
            stop_loss, target_price = intent.stop_loss, intent.target_price

    return Holding(
        asset=asset, quantity=total_signed, average_price=avg_price, updated_at=now,
        stop_loss=stop_loss, target_price=target_price,
    )


async def _reconcile_positions(
    repositories: PersistenceRepositories, broker: AlpacaClient, alerts: TelegramAlerter, now: datetime,
    lease_lost: asyncio.Event | None = None,
) -> tuple[int, int, int]:
    broker_positions = await broker.get_positions()
    broker_by_asset_key = {asset_key_from_broker_symbol(p.asset_class, p.symbol): p for p in broker_positions}

    lot_rows = await repositories.position_lots.list_all(limit=10000)
    all_lots = [hydrate("position_lots", row["payload"]) for row in lot_rows]
    open_lots_by_asset_key: dict[str, Decimal] = {}
    asset_by_key: dict[str, AssetIdentity] = {}
    for lot in all_lots:
        if lot.status not in _OPEN_LOT_STATUSES:
            continue
        key = asset_identity_key(lot.asset)
        open_lots_by_asset_key[key] = open_lots_by_asset_key.get(key, Decimal("0")) + lot.signed_quantity
        asset_by_key.setdefault(key, lot.asset)

    holding_rows = await repositories.holdings.list_all(limit=10000)
    holdings_by_asset_key = {row["record_id"]: hydrate("holdings", row["payload"]) for row in holding_rows}
    for key, holding in holdings_by_asset_key.items():
        asset_by_key.setdefault(key, holding.asset)
    for key, position in broker_by_asset_key.items():
        # A position Alpaca reports with zero local record at all -- can only
        # reach the VIEW_DRIFT (rebuild) branch below if lots_qty == broker_qty,
        # which is impossible here (lots_qty is 0, broker_qty isn't, or the
        # position wouldn't be a broker position); this fallback exists for
        # completeness, not because it's exercised on the happy path.
        asset_by_key.setdefault(
            key, AssetIdentity(symbol=position.symbol, asset_class=position.asset_class, native_asset_id=f"alpaca:{position.symbol.upper()}")
        )

    def _display_symbol(key: str) -> str:
        if key in broker_by_asset_key:
            return broker_by_asset_key[key].symbol
        if key in asset_by_key:
            return asset_by_key[key].symbol
        return holdings_by_asset_key[key].asset.symbol

    asset_keys = set(broker_by_asset_key) | set(open_lots_by_asset_key) | set(holdings_by_asset_key)

    positions_checked = 0
    view_drift_corrected = 0
    accounting_drift_detected = 0

    for key in asset_keys:
        if lease_lost is not None and lease_lost.is_set():
            continue  # reconcile's own command lease may no longer be exclusive -- stop starting new work
        positions_checked += 1
        display_symbol = _display_symbol(key)
        broker_qty = broker_by_asset_key[key].qty if key in broker_by_asset_key else Decimal("0")
        lots_qty = open_lots_by_asset_key.get(key, Decimal("0"))
        holding = holdings_by_asset_key.get(key)
        holding_qty = holding.quantity if holding is not None else Decimal("0")

        if broker_qty == lots_qty == holding_qty:
            await _record(
                repositories, reconciliation_type="position", subject_id=display_symbol, outcome=ReconciliationOutcome.MATCHED,
                expected={"broker_qty": str(broker_qty)}, actual={"lots_qty": str(lots_qty), "holding_qty": str(holding_qty)},
                occurred_at=now,
            )
            continue

        if broker_qty == lots_qty:
            # VIEW_DRIFT: the accounting (lots) already agrees with Alpaca --
            # only the materialized Holding is stale. Safe to rebuild it.
            await _record(
                repositories, reconciliation_type="position_view", subject_id=display_symbol, outcome=ReconciliationOutcome.DRIFT_DETECTED,
                expected={"holding_qty": str(holding_qty)}, actual={"broker_qty": str(broker_qty), "lots_qty": str(lots_qty)},
                occurred_at=now,
            )
            await alerts.send(
                "warning", f"Reconciliation: local Holding view for {display_symbol} was stale, resyncing to lots/Alpaca",
                {"symbol": display_symbol, "broker_qty": str(broker_qty), "was_holding_qty": str(holding_qty)},
            )

            if lots_qty == 0:
                if holding is not None:
                    await repositories.holdings.delete(key)
            else:
                asset = asset_by_key.get(key)
                rebuilt = await _rebuild_holding_from_lots(repositories, asset, now) if asset is not None else None
                if rebuilt is not None:
                    if holding is not None:
                        await repositories.holdings.update(key, rebuilt)
                    else:
                        await repositories.holdings.create_once(key, rebuilt)

            view_drift_corrected += 1
            await _record(
                repositories, reconciliation_type="position_view", subject_id=display_symbol, outcome=ReconciliationOutcome.CORRECTED,
                expected={"holding_qty": str(holding_qty)}, actual={"broker_qty": str(broker_qty)}, occurred_at=now,
                corrective_action="rebuilt local Holding from position_lots to match Alpaca",
            )
        else:
            # ACCOUNTING_DRIFT: the lots themselves disagree with the broker.
            # Not auto-corrected -- the Holding is left untouched.
            accounting_drift_detected += 1
            await _record(
                repositories, reconciliation_type="position_accounting", subject_id=display_symbol, outcome=ReconciliationOutcome.DRIFT_DETECTED,
                expected={"lots_qty": str(lots_qty)}, actual={"broker_qty": str(broker_qty), "holding_qty": str(holding_qty)},
                occurred_at=now,
            )
            await alerts.send(
                "critical",
                f"Reconciliation: ACCOUNTING DRIFT for {display_symbol} -- local position_lots disagree with Alpaca's real "
                f"position (broker={broker_qty}, lots={lots_qty}). NOT auto-corrected -- investigate missing/duplicate fills.",
                {"symbol": display_symbol, "broker_qty": str(broker_qty), "lots_qty": str(lots_qty)},
            )
            await latch_financial_integrity_block(
                repositories, f"Accounting drift detected for {display_symbol}: broker={broker_qty}, lots={lots_qty}", clock=lambda: now
            )

    return positions_checked, view_drift_corrected, accounting_drift_detected


def _find_exact_id_match(activity: AlpacaActivity, local_fills: list[Fill], already_matched: set[str]) -> Fill | None:
    for fill in local_fills:
        if fill.fill_id in already_matched:
            continue
        if fill.broker_fill_id is not None and fill.broker_fill_id == activity.activity_id:
            return fill
    return None


def _find_heuristic_match(activity: AlpacaActivity, local_fills: list[Fill], already_matched: set[str]) -> Fill | None:
    if activity.qty is None or activity.price is None or activity.transaction_time is None:
        return None
    if activity.side is None:
        return None  # can't confidently match a side-ambiguous activity -- fail closed, let it surface as a missed fill
    for fill in local_fills:
        if fill.fill_id in already_matched:
            continue
        if fill.asset.symbol != activity.symbol or fill.side != activity.side:
            continue
        if fill.quantity != activity.qty or fill.price != activity.price:
            continue
        if abs((fill.filled_at - activity.transaction_time).total_seconds()) > _FILL_MATCH_WINDOW_SECONDS:
            continue
        return fill
    return None


async def _reconcile_fills(
    repositories: PersistenceRepositories, broker: AlpacaClient, settlement: SettlementProcessor,
    alerts: TelegramAlerter, now: datetime, lookback: timedelta,
    lease_lost: asyncio.Event | None = None,
) -> tuple[int, int, int]:
    activities = await broker.get_activities(activity_type="FILL", since=now - lookback)
    fill_rows = await repositories.fills.list_all(limit=10000)
    local_fills = [hydrate("fills", row["payload"]) for row in fill_rows]

    intent_rows = await repositories.trade_intents.list_all(limit=10000)
    intents_by_order_id: dict[str, TradeIntent] = {}
    for row in intent_rows:
        intent = hydrate("trade_intents", row["payload"])
        if intent.broker_order_id:
            intents_by_order_id[intent.broker_order_id] = intent

    fills_checked = 0
    missed_fills = 0
    late_fills_recovered = 0
    matched: set[str] = set()

    for activity in activities:
        if lease_lost is not None and lease_lost.is_set():
            continue  # reconcile's own command lease may no longer be exclusive -- stop starting new work
        fills_checked += 1
        match = _find_exact_id_match(activity, local_fills, matched)
        match_method = "exact_id"
        if match is None:
            match = _find_heuristic_match(activity, local_fills, matched)
            match_method = "heuristic"
        if match is not None:
            matched.add(match.fill_id)
            await _record(
                repositories, reconciliation_type="fill", subject_id=activity.activity_id, outcome=ReconciliationOutcome.MATCHED,
                expected={"local_fill_id": match.fill_id}, actual={"activity_id": activity.activity_id, "match_method": match_method},
                occurred_at=now,
            )
            continue

        order_id = str(activity.raw.get("order_id") or "")
        candidate_intent = intents_by_order_id.get(order_id) if order_id else None
        if candidate_intent is not None:
            await resolve_order_from_broker(repositories, broker, settlement, alerts, candidate_intent, lambda: now)
            recovered_row = await repositories.fills.get(activity.activity_id)
            if recovered_row is not None:
                late_fills_recovered += 1
                await _record(
                    repositories, reconciliation_type="fill", subject_id=activity.activity_id, outcome=ReconciliationOutcome.CORRECTED,
                    expected={}, actual={
                        "activity_id": activity.activity_id, "trade_intent_id": candidate_intent.trade_intent_id, "symbol": activity.symbol,
                    },
                    occurred_at=now,
                    corrective_action="created local Fill/SettlementEvent from a late-recovered Alpaca activity via the order's known TradeIntent",
                )
                await alerts.send(
                    "warning",
                    f"Reconciliation: recovered a late fill for {activity.symbol} (activity {activity.activity_id}) into "
                    f"trade intent {candidate_intent.trade_intent_id} -- the gateway's live poll window had already expired.",
                    {"activity_id": activity.activity_id, "trade_intent_id": candidate_intent.trade_intent_id},
                )
                continue
            # A known order matched, but the activity still failed the same
            # validation attribute_order_fills always applies -- a genuine
            # anomaly, not a lag. Fall through to the missed-fill path below
            # rather than silently dropping it.

        missed_fills += 1
        await _record(
            repositories, reconciliation_type="fill", subject_id=activity.activity_id, outcome=ReconciliationOutcome.DRIFT_DETECTED,
            expected={}, actual={
                "activity_id": activity.activity_id, "symbol": activity.symbol,
                "qty": str(activity.qty), "price": str(activity.price),
            },
            occurred_at=now,
        )
        await alerts.send(
            "critical",
            f"Reconciliation: MISSED FILL -- Alpaca activity {activity.activity_id} ({activity.symbol}) has no matching local Fill record.",
            {"activity_id": activity.activity_id, "symbol": activity.symbol, "qty": str(activity.qty), "price": str(activity.price)},
        )
        # Unrecoverable, not merely late: a successfully recovered fill
        # (late_fills_recovered above) never reaches here at all. An
        # activity that does is a broker execution TradePulse cannot prove
        # or restore into its own ledger -- exactly the "local financial
        # truth is uncertain" condition FINANCIAL_INTEGRITY_BLOCKED exists
        # for, and exactly what reset-integrity's missed_fills_detected==0
        # gate checks before allowing that block to be cleared.
        await latch_financial_integrity_block(
            repositories,
            f"Unrecoverable broker fill: activity {activity.activity_id} ({activity.symbol}) has no matching or recoverable local Fill record.",
            clock=lambda: now,
        )

    return fills_checked, missed_fills, late_fills_recovered


async def run_reconciliation(
    repositories: PersistenceRepositories,
    broker: AlpacaClient,
    settlement: SettlementProcessor,
    alerts: TelegramAlerter,
    *,
    fill_lookback: timedelta = timedelta(days=1),
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    lease_lost: asyncio.Event | None = None,
) -> ReconciliationSummary:
    now = clock()
    try:
        positions_checked, view_drift_corrected, accounting_drift_detected = await _reconcile_positions(
            repositories, broker, alerts, now, lease_lost
        )
    except Exception as exc:  # noqa: BLE001 - a broker outage here must fail this pass cleanly, not crash the caller
        await alerts.send("critical", f"Reconciliation degraded -- Alpaca positions unavailable: {exc}", {})
        return ReconciliationSummary("degraded", error=f"BROKER_POSITIONS_UNAVAILABLE: {exc}")

    try:
        fills_checked, missed_fills_detected, late_fills_recovered = await _reconcile_fills(
            repositories, broker, settlement, alerts, now, fill_lookback, lease_lost
        )
    except Exception as exc:  # noqa: BLE001 - same principle for the activities call
        await alerts.send("critical", f"Reconciliation degraded -- Alpaca activities unavailable: {exc}", {})
        return ReconciliationSummary(
            "degraded", positions_checked, view_drift_corrected, accounting_drift_detected,
            error=f"BROKER_ACTIVITIES_UNAVAILABLE: {exc}",
        )

    return ReconciliationSummary(
        "ok", positions_checked, view_drift_corrected, accounting_drift_detected,
        fills_checked, missed_fills_detected, late_fills_recovered,
    )
