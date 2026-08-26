"""The single-writer settlement processor -- port of
base44/functions/processSettlementQueue/entry.ts, simplified for this
system's architecture:

- No per-user processor lease, and no dependency on an external CLI-level
  lock for correctness: `process_pending` claims each event via
  `RecordRepository.claim_if_processable`, an atomic read-decide-write
  inside a single BEGIN IMMEDIATE transaction (see persistence/repositories.py).
  That gives a real, DB-enforced compare-and-swap -- a concurrent second
  caller (whether a second CLI invocation, in-process task, or, later, a
  separate process) genuinely cannot claim the same event twice. This
  replaces the source's elaborate lease-acquire/heartbeat/ownership-reverify
  dance, which existed only because Base44's BaaS platform had no way to
  enforce compare-and-swap at the database level -- SQLite's own
  transaction serialization gives that for free here.
- `cash_projected` is a structural no-op: this MVP's ExecutionGateway always
  submits through Alpaca (paper or live) -- there is no Base44-style
  internal_paper/shadow_live mode with a locally-simulated fill -- so
  Alpaca's account endpoint is the sole cash authority, exactly as the
  source system itself says for broker_paper/live mode. The stage stays in
  the pipeline for structural fidelity and in case a no-broker mode is added
  later.
- No separate "Trade" entity or "AITradeDecision" entity: TradeIntent already
  carries its own cumulative fill/realized-pnl summary directly, and no
  learning-sample entity exists in this system, so `trade_projected` updates
  TradeIntent's summary in place instead of writing a second denormalized
  record.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Mapping
from uuid import uuid4

from tradepulse.alerts import TelegramAlerter
from tradepulse.models import AssetIdentity, Holding, PositionLot, SettlementEvent, SettlementStatus, asset_identity_key
from tradepulse.persistence import PersistenceRepositories, hydrate
from tradepulse.risk import latch_financial_integrity_block

from .lots import IntegrityViolationError, plan_signed_lot_fill
from .stages import (
    StageHandler,
    classify_settlement_failure,
    is_settlement_processable,
    run_settlement_stages,
)


async def _project_lot(repositories: PersistenceRepositories, event: SettlementEvent) -> Mapping[str, Any]:
    lot_rows = await repositories.position_lots.list_all(limit=10000)
    lots = [hydrate("position_lots", row["payload"]) for row in lot_rows]
    symbol_lots = [lot for lot in lots if asset_identity_key(lot.asset) == asset_identity_key(event.asset)]
    plan = plan_signed_lot_fill(symbol_lots, event)

    for closure in plan.closures:
        updated_lot = replace(
            closure.lot,
            remaining_quantity=closure.lot.remaining_quantity - closure.quantity,
            closures={**closure.lot.closures, event.fill_id: closure.quantity},
            realized_pnl=closure.lot.realized_pnl + closure.pnl,
        )
        await repositories.position_lots.update(closure.lot.lot_id, updated_lot)

    if plan.opening_quantity > 0:
        new_lot = PositionLot(
            lot_id=str(uuid4()),
            originating_fill_id=event.fill_id,
            asset=event.asset,
            position_side=plan.opening_direction,
            opened_quantity=plan.opening_quantity,
            remaining_quantity=plan.opening_quantity,
            acquisition_price=event.price,
            opened_at=event.occurred_at,
        )
        # create_once is idempotent on originating_fill_id (a real DB UNIQUE
        # constraint) -- a resumed/retried run silently no-ops here instead
        # of needing the source's manual "does a lot for this event+direction
        # already exist" pre-check.
        await repositories.position_lots.create_once(new_lot.lot_id, new_lot, unique_value=event.fill_id)

    return {"realized_pnl": plan.realized_pnl}


def _holding_record_id(asset: AssetIdentity) -> str:
    return asset_identity_key(asset)


# Which fill's stop_loss/target_price govern an open position when it was
# built from multiple fills. FIRST_ENTRY is the only policy implemented --
# a future LATEST_ENTRY/WEIGHTED/TRAILING policy is a deliberate later choice,
# not a silent behavior change.
PROTECTIVE_THRESHOLD_POLICY = "first_entry"


async def _entry_protective_levels(
    repositories: PersistenceRepositories, lot: PositionLot
) -> tuple[Decimal | None, Decimal | None]:
    fill_row = await repositories.fills.get(lot.originating_fill_id)
    if fill_row is None:
        return None, None
    fill = hydrate("fills", fill_row["payload"])
    intent_row = await repositories.trade_intents.get(fill.trade_intent_id)
    if intent_row is None:
        return None, None
    intent = hydrate("trade_intents", intent_row["payload"])
    return intent.stop_loss, intent.target_price


async def _project_holding(repositories: PersistenceRepositories, event: SettlementEvent) -> None:
    lot_rows = await repositories.position_lots.list_all(limit=10000)
    lots = [hydrate("position_lots", row["payload"]) for row in lot_rows]
    open_lots = [
        lot for lot in lots
        if asset_identity_key(lot.asset) == asset_identity_key(event.asset) and lot.status in ("open", "partially_closed")
    ]

    record_id = _holding_record_id(event.asset)
    existing_row = await repositories.holdings.get(record_id)

    if not open_lots:
        if existing_row is not None:
            await repositories.holdings.delete(record_id)
        return None

    total_signed = sum((lot.signed_quantity for lot in open_lots), Decimal("0"))
    total_remaining = sum((lot.remaining_quantity for lot in open_lots), Decimal("0"))
    total_cost = sum((lot.remaining_quantity * lot.acquisition_price for lot in open_lots), Decimal("0"))
    avg_price = total_cost / total_remaining  # total_remaining > 0 guaranteed by open_lots' status filter

    oldest_lot = min(open_lots, key=lambda lot: lot.opened_at)  # PROTECTIVE_THRESHOLD_POLICY = "first_entry"
    stop_loss, target_price = await _entry_protective_levels(repositories, oldest_lot)

    holding = Holding(
        asset=event.asset, quantity=total_signed, average_price=avg_price,
        updated_at=event.occurred_at, sector=event.sector,
        stop_loss=stop_loss, target_price=target_price,
    )
    if existing_row is not None:
        await repositories.holdings.update(record_id, holding)
    else:
        await repositories.holdings.create_once(record_id, holding)
    return None


async def _project_trade(repositories: PersistenceRepositories, event: SettlementEvent) -> None:
    intent_row = await repositories.trade_intents.get(event.trade_intent_id)
    if intent_row is None:
        return None
    intent = hydrate("trade_intents", intent_row["payload"])

    fill_rows = await repositories.fills.list_all(limit=10000)
    fills = [
        hydrate("fills", row["payload"]) for row in fill_rows if row["payload"]["trade_intent_id"] == event.trade_intent_id
    ]
    cumulative_qty = sum((f.quantity for f in fills), Decimal("0"))
    total_notional = sum((f.quantity * f.price for f in fills), Decimal("0"))
    avg_price = total_notional / cumulative_qty if cumulative_qty > 0 else None

    # realized_pnl is RECOMPUTED from all SettlementEvents for this intent,
    # not accumulated onto the current TradeIntent value -- matches how
    # filled_quantity/filled_avg_price above are already recomputed, not
    # accumulated. Each event's own realized_pnl is stable/idempotent once
    # set (settlement/lots.py's closures-dict prevents a lot from being
    # double-closed by a replayed event), so summing them fresh every time
    # makes this safe to replay after a crash between this write and the
    # stage checkpoint, instead of double-counting on resume. (Fixes a
    # confirmed defect: the previous `intent.realized_pnl + event.realized_pnl`
    # form double-counted exactly that crash window.)
    settlement_rows = await repositories.settlements.list_all(limit=10000)
    own_events = [
        hydrate("settlements", row["payload"]) for row in settlement_rows
        if row["payload"]["trade_intent_id"] == event.trade_intent_id
    ]
    total_realized_pnl = sum((e.realized_pnl or Decimal("0") for e in own_events), Decimal("0"))

    patch: dict[str, Any] = {
        "filled_quantity": cumulative_qty, "filled_avg_price": avg_price, "realized_pnl": total_realized_pnl,
    }
    updated_intent = replace(intent, **patch)
    await repositories.trade_intents.update(event.trade_intent_id, updated_intent, status=updated_intent.status.value)
    return None


async def _verify_integrity(repositories: PersistenceRepositories, event: SettlementEvent) -> None:
    errors: list[str] = []

    lot_rows = await repositories.position_lots.list_all(limit=10000)
    lots = [hydrate("position_lots", row["payload"]) for row in lot_rows]
    open_lots = [
        lot for lot in lots
        if asset_identity_key(lot.asset) == asset_identity_key(event.asset) and lot.status in ("open", "partially_closed")
    ]
    lot_qty = sum((lot.signed_quantity for lot in open_lots), Decimal("0"))

    holding_row = await repositories.holdings.get(_holding_record_id(event.asset))
    holding_qty = hydrate("holdings", holding_row["payload"]).quantity if holding_row is not None else Decimal("0")

    if lot_qty != holding_qty:
        errors.append(f"HOLDING_LOT_MISMATCH: holding {holding_qty}, lots {lot_qty} for {event.asset.symbol}")

    if event.broker_fill_id:
        settlement_rows = await repositories.settlements.list_all(limit=10000)
        duplicates = [
            row
            for row in settlement_rows
            if row["payload"].get("broker_fill_id") == event.broker_fill_id
            and row["payload"]["settlement_event_id"] != event.settlement_event_id
            and row.get("status") == SettlementStatus.COMPLETED.value
        ]
        if duplicates:
            errors.append(f"DUPLICATE_BROKER_FILL: {event.broker_fill_id} already completed in {len(duplicates)} other event(s)")

    if errors:
        raise IntegrityViolationError("INTEGRITY_VIOLATION: " + "; ".join(errors))


@dataclass(frozen=True, slots=True)
class SettlementBatchSummary:
    ok: bool
    processed: int
    completed: int
    failed: int
    pending: int
    retryable_failed: int
    integrity_blocked: int
    terminal_failed: int


class SettlementProcessor:
    def __init__(
        self,
        repositories: PersistenceRepositories,
        alerts: TelegramAlerter,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._repositories = repositories
        self._alerts = alerts
        self._clock = clock

    def _handlers(self) -> Mapping[str, StageHandler]:
        async def project_lot(state: SettlementEvent) -> Mapping[str, Any]:
            return await _project_lot(self._repositories, state)

        async def project_cash(state: SettlementEvent) -> None:
            return None  # Alpaca is the cash authority in this MVP -- see module docstring.

        async def project_holding(state: SettlementEvent) -> None:
            await _project_holding(self._repositories, state)
            return None

        async def project_trade(state: SettlementEvent) -> None:
            await _project_trade(self._repositories, state)
            return None

        async def verify_integrity(state: SettlementEvent) -> None:
            await _verify_integrity(self._repositories, state)
            return None

        return {
            "project_lot": project_lot,
            "project_cash": project_cash,
            "project_holding": project_holding,
            "project_trade": project_trade,
            "verify_integrity": verify_integrity,
        }

    async def _checkpoint(self, event: SettlementEvent) -> None:
        await self._repositories.settlements.update(event.settlement_event_id, event, status=event.status.value)

    async def process_pending(
        self,
        limit: int = 100,
        stale_lease_seconds: int = 120,
        force_retry: bool = False,
        *,
        lease_lost: asyncio.Event | None = None,
    ) -> SettlementBatchSummary:
        now = self._clock()
        rows = await self._repositories.settlements.list_all(limit=1000)
        events = [hydrate("settlements", row["payload"]) for row in rows]
        due = sorted(
            (e for e in events if is_settlement_processable(e, now, stale_lease_seconds, force_retry)),
            key=lambda e: e.occurred_at,
        )[:limit]

        completed = failed = 0
        outcome_counts = {status: 0 for status in ("retryable_failed", "integrity_blocked", "terminal_failed")}

        for event in due:
            if lease_lost is not None and lease_lost.is_set():
                continue  # settle's own command lease may no longer be exclusive -- stop starting new work

            def decide(current: SettlementEvent) -> SettlementEvent | None:
                # Re-validated against the CURRENT row, inside the same
                # atomic transaction as the claim -- the `due` list above is
                # only a candidate snapshot that may already be stale by the
                # time we get here.
                if not is_settlement_processable(current, now, stale_lease_seconds, force_retry):
                    return None
                return replace(current, status=SettlementStatus.PROCESSING, processing_owner="settle", processing_started_at=now)

            claimed = await self._repositories.settlements.claim_if_processable(event.settlement_event_id, decide)
            if claimed is None:
                continue  # lost the race, or another caller already claimed/resolved it
            try:
                final_state = await run_settlement_stages(claimed, self._handlers(), self._checkpoint)
                done = replace(
                    final_state, status=SettlementStatus.COMPLETED, processing_owner=None,
                    processing_started_at=None, error_code=None, next_retry_at=None,
                )
                await self._checkpoint(done)
                completed += 1
            except Exception as exc:  # noqa: BLE001 - any stage failure must be classified and persisted, never crash the batch
                failure = classify_settlement_failure(event.attempt_count, exc, now)
                failed_state = replace(
                    claimed, status=failure.status, attempt_count=failure.attempt_count,
                    error_code=str(failure.error)[:200], next_retry_at=failure.next_retry_at,
                    processing_owner=None, processing_started_at=None,
                )
                await self._checkpoint(failed_state)
                failed += 1
                outcome_counts[failure.status.value] += 1
                # TERMINAL_FAILED also latches financial integrity, not just
                # INTEGRITY_BLOCKED: a real broker fill (this event originates
                # from one) whose accounting projection permanently fails --
                # for ANY reason, not just a detected INTEGRITY_VIOLATION --
                # leaves this system's local ledger permanently unresolved for
                # that fill. The session must stop taking new risk exactly as
                # it would for a detected integrity violation. This does NOT
                # change the settlement event's own persisted status --
                # failed_state.status above still records TERMINAL_FAILED,
                # distinct from a genuine INTEGRITY_BLOCKED -- only a separate,
                # global signal (TradingSession.state) is latched alongside it.
                if failure.status in (SettlementStatus.INTEGRITY_BLOCKED, SettlementStatus.TERMINAL_FAILED):
                    reason = (
                        f"Settlement integrity blocked: {failure.error}"
                        if failure.status == SettlementStatus.INTEGRITY_BLOCKED
                        else f"Settlement permanently failed after {failure.attempt_count} attempts for a real broker fill: {failure.error}"
                    )
                    await self._alerts.send(
                        "critical",
                        f"Settlement {failure.status.value}: {event.asset.symbol} {event.side.value} {event.quantity}",
                        {"error": failure.error, "settlement_event_id": event.settlement_event_id, "attempt_count": failure.attempt_count},
                    )
                    await latch_financial_integrity_block(self._repositories, reason, clock=self._clock)

        refreshed_rows = await self._repositories.settlements.list_all(limit=1000)
        unresolved = [row for row in refreshed_rows if row.get("status") != SettlementStatus.COMPLETED.value]
        pending = sum(1 for row in unresolved if row.get("status") in (SettlementStatus.PENDING.value, SettlementStatus.PROCESSING.value))

        return SettlementBatchSummary(
            ok=len(unresolved) == 0,
            processed=len(due),
            completed=completed,
            failed=failed,
            pending=pending,
            retryable_failed=outcome_counts["retryable_failed"],
            integrity_blocked=outcome_counts["integrity_blocked"],
            terminal_failed=outcome_counts["terminal_failed"],
        )
