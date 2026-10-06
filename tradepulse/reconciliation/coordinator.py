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
import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal
from uuid import uuid4

from tradepulse.alerts import TelegramAlerter, alert_once
from tradepulse.broker import AlpacaActivity, AlpacaClient
from tradepulse.models import (
    AssetClass,
    AssetIdentity,
    AuditEvent,
    Fill,
    Holding,
    IntegrityHoldType,
    ReconciliationOutcome,
    ReconciliationRecord,
    TradeIntentStatus,
    asset_identity_key,
    asset_key_from_broker_symbol,
)
from tradepulse.persistence import (
    PersistenceRepositories,
    hydrate,
    list_all_by_asset,
    list_all_by_json_field,
    list_all_by_json_time_range,
    list_all_by_statuses,
    paginate_all_rows,
)
from tradepulse.risk import latch_financial_integrity_block
from tradepulse.settlement import SettlementProcessor
from tradepulse.settlement.stages import retry_delay_seconds
from tradepulse.time import aware_utc

from ..execution.fill_attribution import order_matches_intent, resolve_order_from_broker
from .asset_fees import reconcile_asset_fees

logger = logging.getLogger(__name__)

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
    verification_holds_reverified: int = 0
    error: str | None = None


async def _record(repositories: PersistenceRepositories, **kwargs) -> None:
    record = ReconciliationRecord(record_id=str(uuid4()), **kwargs)
    await repositories.reconciliation_records.create_once(record.record_id, record)


async def _alert_once(repositories: PersistenceRepositories, alerts: TelegramAlerter, *, event_type: str, subject: str,
                      window: str, severity: str, message: str, details: dict, now: datetime) -> bool:
    """Send an alert at most once per (event_type, subject, window).

    Reconciliation runs every minute, so a condition that persists until an
    operator acts would otherwise re-alert on every pass. The deterministic
    audit id records each window's evidence once and delivers its alert once
    (a failed send is retried next pass, see alert_once); ``window`` is a UTC
    date or date-hour string. Reconciliation records are still written on
    every pass."""
    event_id = f"{event_type}:{subject}:{window}"
    event = AuditEvent(event_id=event_id, event_type=event_type, severity=severity, message=message,
                       occurred_at=now, entity_type="reconciliation", entity_id=subject, details=details)
    return await alert_once(repositories.audit_events, alerts, event)


def _utc_day(now: datetime) -> str:
    return now.astimezone(UTC).date().isoformat()


def _utc_hour(now: datetime) -> str:
    return now.astimezone(UTC).strftime("%Y-%m-%dT%H")


async def _rebuild_holding_from_lots(
    repositories: PersistenceRepositories, asset: AssetIdentity, now: datetime
) -> Holding | None:
    """Mirrors settlement/engine.py::_project_holding's own recompute (not
    imported directly -- that function is keyed off a SettlementEvent this
    caller doesn't have, and this codebase's convention is a small local
    duplicate over reaching into another module's private internals, same
    as execution/gateway.py's own `symbol.upper()` holding-key convention)."""
    # FIN-090-01: asset-scoped, unbounded pagination -- NOT list_all(limit=N).
    lot_rows = await list_all_by_asset(repositories.position_lots, asset)
    lots = [hydrate("position_lots", row["payload"]) for row in lot_rows]
    open_lots = [lot for lot in lots if lot.status in _OPEN_LOT_STATUSES]
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
    from tradepulse.verification.opening import load_bound_opening_checkpoint

    from .membership import opening_quantities
    checkpoint = await repositories.fills.database.run(load_bound_opening_checkpoint)
    opening_positions = opening_quantities(checkpoint)
    broker_positions = await broker.get_positions()
    broker_by_asset_key = {asset_key_from_broker_symbol(p.asset_class, p.symbol): p for p in broker_positions}

    # FIN-090-01: unbounded, whole-table pagination -- cross-asset by
    # design (every held asset compared at once). Open/closed determined
    # below via the payload's own Decimal-based lot.status property.
    lot_rows = await paginate_all_rows(repositories.position_lots)
    all_lots = [hydrate("position_lots", row["payload"]) for row in lot_rows]
    open_lots_by_asset_key: dict[str, Decimal] = {}
    asset_by_key: dict[str, AssetIdentity] = {}
    for lot in all_lots:
        key = asset_identity_key(lot.asset)
        asset_by_key.setdefault(key, lot.asset)
        if lot.status not in _OPEN_LOT_STATUSES:
            continue
        open_lots_by_asset_key[key] = open_lots_by_asset_key.get(key, Decimal("0")) + lot.signed_quantity
        asset_by_key.setdefault(key, lot.asset)

    # FIN-090-01: unbounded, whole-table pagination -- no filterable
    # dimension for "every currently-held asset."
    holding_rows = await paginate_all_rows(repositories.holdings)
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

    if checkpoint:
        for position in checkpoint['positions']:
            key = asset_key_from_broker_symbol(AssetClass(position['asset_class']), position['symbol'])
            asset_by_key.setdefault(key, AssetIdentity(symbol=position['symbol'],
                asset_class=AssetClass(position['asset_class']), native_asset_id='alpaca:' + position['symbol'].upper()))

    def _display_symbol(key: str) -> str:
        if key in broker_by_asset_key:
            return broker_by_asset_key[key].symbol
        if key in asset_by_key:
            return asset_by_key[key].symbol
        return holdings_by_asset_key[key].asset.symbol

    asset_keys = set(broker_by_asset_key) | set(asset_by_key) | set(holdings_by_asset_key) | set(opening_positions)

    positions_checked = 0
    view_drift_corrected = 0
    accounting_drift_detected = 0

    for key in asset_keys:
        if lease_lost is not None and lease_lost.is_set():
            continue  # reconcile's own command lease may no longer be exclusive -- stop starting new work
        positions_checked += 1
        display_symbol = _display_symbol(key)
        actual_broker_qty = broker_by_asset_key[key].qty if key in broker_by_asset_key else Decimal("0")
        broker_qty = actual_broker_qty - opening_positions.get(key, Decimal(0))
        lots_qty = open_lots_by_asset_key.get(key, Decimal("0"))
        holding = holdings_by_asset_key.get(key)
        holding_qty = holding.quantity if holding is not None else Decimal("0")

        if broker_qty == lots_qty == holding_qty:
            await _record(
                repositories, reconciliation_type="position", subject_id=key, outcome=ReconciliationOutcome.MATCHED,
                expected={"broker_qty": str(broker_qty)}, actual={"lots_qty": str(lots_qty), "holding_qty": str(holding_qty)},
                occurred_at=now,
            )
            continue

        if broker_qty == lots_qty:
            # VIEW_DRIFT: the accounting (lots) already agrees with Alpaca --
            # only the materialized Holding is stale. Safe to rebuild it.
            await _record(
                repositories, reconciliation_type="position_view", subject_id=key, outcome=ReconciliationOutcome.DRIFT_DETECTED,
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
                repositories, reconciliation_type="position_view", subject_id=key, outcome=ReconciliationOutcome.CORRECTED,
                expected={"holding_qty": str(holding_qty)}, actual={"broker_qty": str(broker_qty)}, occurred_at=now,
                corrective_action="rebuilt local Holding from position_lots to match Alpaca",
            )
        else:
            # ACCOUNTING_DRIFT: the lots themselves disagree with the broker.
            # Not auto-corrected -- the Holding is left untouched.
            accounting_drift_detected += 1
            await _record(
                repositories, reconciliation_type="position_accounting", subject_id=key, outcome=ReconciliationOutcome.DRIFT_DETECTED,
                expected={"lots_qty": str(lots_qty)}, actual={"broker_qty": str(broker_qty), "holding_qty": str(holding_qty)},
                occurred_at=now,
            )
            await _alert_once(
                repositories, alerts, event_type="accounting_drift", subject=key, window=_utc_day(now), severity="critical",
                message=(f"Reconciliation: ACCOUNTING DRIFT for {display_symbol} -- local position_lots disagree with Alpaca's real "
                         f"position (broker={broker_qty}, lots={lots_qty}). NOT auto-corrected -- investigate missing/duplicate fills."),
                details={"symbol": display_symbol, "broker_qty": str(broker_qty), "lots_qty": str(lots_qty)}, now=now,
            )
            if asset_by_key[key].asset_class == AssetClass.CRYPTO:
                from .epochs import block_asset
                await block_asset(repositories, asset_by_key[key],
                                  f"POSITION_QUANTITY_MISMATCH: broker={broker_qty}, lots={lots_qty}", now=now)
            else:
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
    generation_membership=None,
) -> tuple[int, int, int]:
    activities = await broker.get_activities(activity_type="FILL", since=None if generation_membership is not None else now - lookback)
    # FIN-090-01: scoped to the actual match window (_find_heuristic_match
    # never matches beyond +/-_FILL_MATCH_WINDOW_SECONDS of an activity's
    # transaction_time, and activities themselves are already bounded to
    # [now-lookback, now]) -- NOT list_all(limit=N), which would silently
    # drop this window's own fills once enough OTHER (older or newer)
    # fills existed first.
    match_window = timedelta(seconds=_FILL_MATCH_WINDOW_SECONDS)
    fill_rows = (await paginate_all_rows(repositories.fills) if generation_membership is not None else
                 await list_all_by_json_time_range(repositories.fills, "filled_at", now - lookback - match_window, now + match_window))
    local_fills = [hydrate("fills", row["payload"]) for row in fill_rows]

    fills_checked = 0
    missed_fills = 0
    late_fills_recovered = 0
    matched: set[str] = set()

    for activity in activities:
        if generation_membership is not None:
            from .membership import ELIGIBLE_MEMBERSHIPS
            membership_status = generation_membership['classifications'].get(activity.activity_id)
            if membership_status == 'pre_generation' or membership_status == 'post_generation':
                continue
            if membership_status not in ELIGIBLE_MEMBERSHIPS:
                raise ValueError('UNRESOLVED_GENERATION_MEMBERSHIP:' + activity.activity_id)
        if lease_lost is not None and lease_lost.is_set():
            continue  # reconcile's own command lease may no longer be exclusive -- stop starting new work
        fills_checked += 1
        match = _find_exact_id_match(activity, local_fills, matched)
        match_method = "exact_id"
        if match is None and generation_membership is None:
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
        candidate_intent = None
        if order_id:
            # FIN-090-01 refinement: a per-activity point lookup (scoped
            # to this order_id), not a whole-table prefetch -- and
            # deliberately NOT limit=1, since no DB-level uniqueness
            # constraint on broker_order_id was found. 0 matches = no
            # candidate (existing unmatched path, unchanged); exactly 1 =
            # the existing recovery path, unchanged; MORE THAN 1 is a
            # genuine financial-integrity ambiguity -- which local
            # TradeIntent this fill actually belongs to is unknown -- and
            # must be surfaced, never silently resolved by picking one.
            matching_rows = await list_all_by_json_field(repositories.trade_intents, "broker_order_id", order_id)
            candidate_intents = [hydrate("trade_intents", row["payload"]) for row in matching_rows]
            if len(candidate_intents) > 1:
                missed_fills += 1  # same "local financial truth uncertain" bucket reset-integrity's gate already checks
                await _record(
                    repositories, reconciliation_type="fill", subject_id=activity.activity_id, outcome=ReconciliationOutcome.DRIFT_DETECTED,
                    expected={}, actual={
                        "activity_id": activity.activity_id, "order_id": order_id,
                        "matching_trade_intent_ids": [i.trade_intent_id for i in candidate_intents],
                    },
                    occurred_at=now,
                )
                await _alert_once(
                    repositories, alerts, event_type="ambiguous_broker_order_id", subject=activity.activity_id,
                    window=_utc_day(now), severity="critical",
                    message=(f"Reconciliation: AMBIGUOUS BROKER_ORDER_ID -- {order_id} matches {len(candidate_intents)} local "
                             f"TradeIntents for activity {activity.activity_id} ({activity.symbol}); late-fill recovery skipped, "
                             "human investigation required."),
                    details={"activity_id": activity.activity_id, "order_id": order_id, "match_count": len(candidate_intents)},
                    now=now,
                )
                await latch_financial_integrity_block(
                    repositories, f"Ambiguous broker_order_id {order_id} matches multiple local TradeIntents", clock=lambda: now,
                )
                continue
            candidate_intent = candidate_intents[0] if candidate_intents else None
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
        await _alert_once(
            repositories, alerts, event_type="missed_fill", subject=activity.activity_id, window=_utc_day(now), severity="critical",
            message=f"Reconciliation: MISSED FILL -- Alpaca activity {activity.activity_id} ({activity.symbol}) has no matching local Fill record.",
            details={"activity_id": activity.activity_id, "symbol": activity.symbol, "qty": str(activity.qty), "price": str(activity.price)},
            now=now,
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


async def _reverify_pending_holds(
    repositories: PersistenceRepositories, broker: AlpacaClient, settlement: SettlementProcessor,
    alerts: TelegramAlerter, now: datetime, lease_lost: asyncio.Event | None = None,
) -> int:
    """FIN-095-01's actual retry mechanism for a verification_pending hold.
    Neither the gateway's poll loop (a single-tick-terminal order gets no
    "next tick") nor _reconcile_fills's activity-driven matching (which
    skips an activity forever once its Fill exists, _find_exact_id_match
    above) guarantees a later re-verification attempt -- this is scoped by
    the HOLD itself, independent of whether the order's activities already
    have local Fill matches. Backoff (mirroring settlement's own
    retry_delay_seconds, attempt_count/next_retry_at on the hold itself)
    avoids re-hitting a persistently-unavailable broker every
    reconciliation cycle."""
    hold_rows = await list_all_by_statuses(repositories.integrity_holds, [IntegrityHoldType.VERIFICATION_PENDING.value])
    reverified = 0
    for row in hold_rows:
        if lease_lost is not None and lease_lost.is_set():
            continue
        hold = hydrate("integrity_holds", row["payload"])
        if hold.next_retry_at is not None and hold.next_retry_at > now:
            continue
        intent_row = await repositories.trade_intents.get(hold.trade_intent_id)
        if intent_row is None:
            continue
        intent = hydrate("trade_intents", intent_row["payload"])
        # resolve_order_from_broker already calls attribute_order_fills
        # with verify_order_filled_qty=True internally -- this IS the
        # re-verification attempt, bypassing _reconcile_fills's
        # activity-matching entirely. Idempotent/safe regardless of the
        # intent's current status (documented behavior, unchanged).
        await resolve_order_from_broker(repositories, broker, settlement, alerts, intent, lambda: now)
        reverified += 1

        still_pending_row = await repositories.integrity_holds.get(hold.broker_order_id)
        if still_pending_row is not None and still_pending_row["status"] == IntegrityHoldType.VERIFICATION_PENDING.value:
            # Still unresolved -- another failed verification attempt (or
            # a genuinely still-inconclusive one). Advance backoff so the
            # NEXT reconciliation cycle doesn't immediately re-hit a
            # persistently-unavailable broker.
            still_pending = hydrate("integrity_holds", still_pending_row["payload"])
            next_attempt = still_pending.attempt_count + 1
            updated = replace(
                still_pending, attempt_count=next_attempt,
                next_retry_at=now + timedelta(seconds=retry_delay_seconds(next_attempt)),
            )
            await repositories.integrity_holds.update(hold.broker_order_id, updated, status=updated.hold_type.value)
        # else: cleared (resolved consistent) or upgraded to
        # fill_quantity_disputed -- either way, resolve_order_from_broker's
        # own call into attribute_order_fills already handled it; nothing
        # further to do here.
    return reverified


STRANDED_INTENT_GRACE_SECONDS = 120
_STRANDED_STATUSES = (TradeIntentStatus.RISK_APPROVED, TradeIntentStatus.SUBMITTED)
# Reasons no retry can clear without an operator; everything else is transient.
_UNPROVABLE_STRANDED_REASONS = frozenset({
    "ACCOUNT_IDENTITY_UNPROVEN", "STRANDED_ORDER_IDENTITY_MISMATCH", "STRANDED_INTENT_UNDER_INTEGRITY_HOLD",
})


async def _recover_stranded_intents(repositories, broker, alerts, now, lease_lost=None, *, lock_ttl_seconds=None,
                                    reconcile_lease=None) -> int:
    """Intents approved or marked submitted but never given a broker order id.

    A crash or early return between RISK_APPROVED and broker acceptance leaves
    an intent has_in_flight_intent treats as in flight forever, blocking every
    later order on its asset -- protective exits included. A missing broker id
    proves nothing about submission. Each candidate is re-read and resolved
    under the asset's execution reservation, held and renewed throughout. A
    lost lease stops further work, and the commit transaction re-verifies both
    the reservation and the parent reconciliation lease (owner token and
    expiry) before a conditional, full-payload-compared write. The grace
    period only keeps the sweep away from fresh intents -- it is not a
    finality proof.
    """
    from tradepulse.execution import (
        SYMBOL_LOCK_TTL_SECONDS, execution_lock_key, release_symbol_reservation, reserve_symbol_for_execution,
    )
    from tradepulse.persistence import run_with_lock_renewal

    ttl = lock_ttl_seconds or SYMBOL_LOCK_TTL_SECONDS
    database = repositories.trade_intents.database
    rows = await list_all_by_statuses(repositories.trade_intents, [status.value for status in _STRANDED_STATUSES])
    resolved = 0
    for row in rows:
        if lease_lost is not None and lease_lost.is_set():
            break
        subject_id = row["record_id"]
        reserved = None  # (asset, token) once the reservation is ours
        try:
            candidate = hydrate("trade_intents", row["payload"])
            if candidate.broker_order_id or (now - candidate.created_at).total_seconds() < STRANDED_INTENT_GRACE_SECONDS:
                continue
            token = str(uuid4())
            if not await reserve_symbol_for_execution(database, candidate.asset, token):
                continue  # a live execution owns this asset -- never race it
            reserved = (candidate.asset, token)
            fence = asyncio.Event()

            async def on_lost(fence=fence):
                fence.set()

            leases = [(execution_lock_key(candidate.asset), token), *([reconcile_lease] if reconcile_lease else [])]
            if await run_with_lock_renewal(
                database, execution_lock_key(candidate.asset), token, ttl,
                _resolve_stranded(repositories, broker, alerts, candidate.trade_intent_id, now, fence, leases),
                on_renewal_failed=on_lost,
            ):
                resolved += 1
        except Exception as exc:  # noqa: BLE001 - one poisoned candidate must not abort the pass or the protective lanes after it
            try:
                await _record(repositories, reconciliation_type="order", subject_id=subject_id,
                              outcome=ReconciliationOutcome.DRIFT_DETECTED,
                              expected={"stranded_intent_resolved": True},
                              actual={"error": f"STRANDED_SWEEP_FAILED: {exc}"}, occurred_at=now)
            except Exception:  # noqa: BLE001 - evidence is best effort; the sweep must still continue
                logger.exception("stranded_sweep_record_failed", extra={"trade_intent_id": subject_id})
        finally:
            if reserved is not None:
                try:
                    await release_symbol_reservation(database, reserved[0], reserved[1])
                except Exception:  # noqa: BLE001 - a failed release expires by TTL; never abort the pass
                    logger.exception("stranded_sweep_release_failed", extra={"trade_intent_id": subject_id})
    return resolved


async def _resolve_stranded(repositories, broker, alerts, trade_intent_id, now, fence, leases) -> bool:
    """Re-read under the reservation, prove account and order identity, then commit conditionally."""
    async def unresolved(intent, reason: str) -> bool:
        # The fence suppresses evidence once it has tripped. A commit refused
        # with STRANDED_RESERVATION_LOST before the fence trips still records drift.
        if fence.is_set():
            return False
        if reason in _UNPROVABLE_STRANDED_REASONS:
            # Permanent until an operator acts: one audit event per intent per
            # UTC day (deterministic id), delivered until Telegram accepts it,
            # and drift written only on that first sighting.
            event_id = f"stranded_intent_unresolved:{trade_intent_id}:{now.date().isoformat()}"
            event = AuditEvent(
                event_id=event_id, event_type="stranded_intent_unresolved", severity="critical",
                message=(f"STRANDED_INTENT_UNRESOLVED: {intent.asset.symbol} intent {trade_intent_id} ({reason}) cannot be "
                         "proven or closed automatically -- the asset is blocked for all orders including protective "
                         "exits and manual resolution is required."),
                occurred_at=now, entity_type="trade_intent", entity_id=trade_intent_id,
                details={"reason": reason, "status": intent.status.value, "symbol": intent.asset.symbol},
            )
            if await alert_once(repositories.audit_events, alerts, event):
                await _record(repositories, reconciliation_type="order", subject_id=trade_intent_id,
                              outcome=ReconciliationOutcome.DRIFT_DETECTED, expected={"stranded_intent_resolved": True},
                              actual={"error": reason, "status": intent.status.value}, occurred_at=now)
            return False
        await _record(repositories, reconciliation_type="order", subject_id=trade_intent_id,
                      outcome=ReconciliationOutcome.DRIFT_DETECTED, expected={"stranded_intent_resolved": True},
                      actual={"error": reason, "status": intent.status.value}, occurred_at=now)
        return False

    row = await repositories.trade_intents.get(trade_intent_id)
    if row is None:
        return False
    original_payload = row["payload"]
    intent = hydrate("trade_intents", original_payload)
    if (intent.status not in _STRANDED_STATUSES or intent.broker_order_id
            or (now - intent.created_at).total_seconds() < STRANDED_INTENT_GRACE_SECONDS):
        return False  # resolved or advanced by someone else since the scan -- nothing to do
    approved_on = intent.risk_snapshot.get("broker_account_number")
    try:
        account = await broker.get_account()
        # Blank on either side proves nothing: two empty strings are equal
        # without identifying any account.
        if not approved_on or not account.account_number or account.account_number != approved_on:
            return await unresolved(intent, "ACCOUNT_IDENTITY_UNPROVEN")
        order = await broker.get_order_by_client_order_id(intent.trade_intent_id)
    except Exception as exc:  # noqa: BLE001 - an unavailable lookup proves nothing; retry next pass
        return await unresolved(intent, str(exc))
    if order is not None and not order_matches_intent(order, intent):
        return await unresolved(intent, "STRANDED_ORDER_IDENTITY_MISMATCH")
    if fence.is_set():
        return False  # lease lost while waiting on the broker: ownership is unproven, write nothing
    if order is not None:
        updated = replace(intent, status=TradeIntentStatus.ACCEPTED, broker_order_id=order.broker_order_id,
                          client_order_id=intent.trade_intent_id)
        action = "adopted the broker order Alpaca holds for this client_order_id"
    else:
        updated = replace(intent, status=TradeIntentStatus.REJECTED, rejection_reason="STRANDED_BEFORE_SUBMISSION")
        action = "closed: Alpaca returned a definitive not-found for this client_order_id"
    record = ReconciliationRecord(
        str(uuid4()), "order", intent.trade_intent_id, ReconciliationOutcome.CORRECTED,
        expected={"stranded_intent_resolved": True},
        actual={"previous_status": intent.status.value, "status": updated.status.value,
                "broker_order_id": updated.broker_order_id, "account_number": approved_on},
        occurred_at=now, corrective_action=action,
    )
    outcome = await repositories.trade_intents.database.run(
        lambda connection: _commit_stranded(connection, original_payload, intent, updated, record, now, leases),
        write=True)
    if outcome != "committed":
        return await unresolved(intent, outcome)
    await alerts.send("warning", f"Stranded {intent.status.value} intent for {intent.asset.symbol} resolved: {action}",
                      {"trade_intent_id": intent.trade_intent_id})
    return True


def _commit_stranded(connection, original_payload, intent, updated, record, now, leases) -> str:
    """One BEGIN IMMEDIATE transaction. Every lease this decision relies on must
    still be ours and unexpired; the stored payload must equal the re-read one
    in full; and no integrity hold may reference the intent or the adopted
    order."""
    from datetime import UTC, datetime as _datetime

    from tradepulse.persistence.codec import decode_payload, encode_payload

    wall_clock = _datetime.now(UTC).isoformat()
    for lock_key, owner_token in leases:
        lock = connection.execute("SELECT owner_token, expires_at FROM locks WHERE lock_key=?", (lock_key,)).fetchone()
        if lock is None or lock["owner_token"] != owner_token or lock["expires_at"] <= wall_clock:
            return "STRANDED_RESERVATION_LOST"
    row = connection.execute("SELECT status, payload FROM trade_intents WHERE record_id=?",
                             (intent.trade_intent_id,)).fetchone()
    if row is None or row["status"] != intent.status.value or decode_payload(row["payload"]) != original_payload:
        return "STRANDED_INTENT_CHANGED_CONCURRENTLY"
    held = connection.execute(
        "SELECT 1 FROM integrity_holds WHERE json_extract(payload,'$.trade_intent_id')=? OR record_id=?",
        (intent.trade_intent_id, updated.broker_order_id or ""),
    ).fetchone()
    if held:
        return "STRANDED_INTENT_UNDER_INTEGRITY_HOLD"
    connection.execute("UPDATE trade_intents SET status=?, payload=?, updated_at=? WHERE record_id=?",
                       (updated.status.value, encode_payload(updated), now.isoformat(), intent.trade_intent_id))
    connection.execute("INSERT INTO reconciliation_records(record_id,payload,created_at) VALUES(?,?,?)",
                       (record.record_id, encode_payload(record), now.isoformat()))
    return "committed"



async def _recover_inflight_orders(repositories, broker, settlement, alerts, now, lease_lost=None):
    """Known unresolved orders do not expire out of the activity lookback.

    Fetch by the persisted broker ID, then use the sole fill/settlement path.
    Neither absence from current positions nor an old local status is a fill.
    """
    from hashlib import sha256

    from tradepulse.persistence.codec import encode_payload

    from .asset_fees import _record_feed_transition
    rows = await list_all_by_statuses(repositories.trade_intents,
                                     ['submitted', 'accepted', 'partially_filled', 'submission_unknown'])
    for row in rows:
        if lease_lost is not None and lease_lost.is_set():
            return
        intent = hydrate('trade_intents', row['payload'])
        if not intent.broker_order_id:
            continue
        try:
            owners = await list_all_by_json_field(repositories.trade_intents, 'broker_order_id', intent.broker_order_id)
            if len(owners) != 1:
                raise ValueError('AMBIGUOUS_BROKER_ORDER_ID')
            order = await broker.get_order(intent.broker_order_id)
            if (order.broker_order_id != intent.broker_order_id or order.symbol != intent.asset.symbol
                    or order.side != intent.side or order.raw.get('client_order_id') != intent.trade_intent_id):
                raise ValueError('RECOVERY_BROKER_ORDER_IDENTITY_MISMATCH')
            result = await resolve_order_from_broker(repositories, broker, settlement, alerts, intent, lambda: now)
            current = await repositories.trade_intents.get(intent.trade_intent_id)
            complete = current['status'] in {'filled', 'canceled', 'expired', 'rejected'} and result.quantity == order.filled_qty
            record = ReconciliationRecord(str(uuid4()), 'order', intent.broker_order_id,
                ReconciliationOutcome.MATCHED if complete else ReconciliationOutcome.DRIFT_DETECTED,
                expected={'broker_filled_quantity': str(order.filled_qty)},
                actual={'broker_order': order.raw, 'broker_order_sha256': sha256(encode_payload(order.raw).encode()).hexdigest(),
                        'attributed_quantity': str(result.quantity), 'trade_intent_id': intent.trade_intent_id,
                        'local_status': current['status']}, occurred_at=now)
        except Exception as exc:  # noqa: BLE001 - record one order's unresolved evidence; retain other lanes
            record = ReconciliationRecord(str(uuid4()), 'order', intent.broker_order_id,
                ReconciliationOutcome.DRIFT_DETECTED, expected={'authoritative_recovery': True},
                actual={'error': str(exc), 'trade_intent_id': intent.trade_intent_id}, occurred_at=now)
        await _record_feed_transition(repositories, record)


async def run_reconciliation(
    repositories: PersistenceRepositories,
    broker: AlpacaClient,
    settlement: SettlementProcessor,
    alerts: TelegramAlerter,
    *,
    fill_lookback: timedelta = timedelta(days=1),
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    lease_lost: asyncio.Event | None = None,
    reconcile_lease: tuple[str, str] | None = None,
) -> ReconciliationSummary:
    now = aware_utc(clock(), field_name='reconciliation_observed_at')
    generation_membership = None
    try:
        from tradepulse.verification.opening import load_bound_opening_checkpoint
        opening = await repositories.fills.database.run(load_bound_opening_checkpoint)
        if opening is not None:
            from .activity_cursor import activity_population
            from .generation_fees import persist_generation_fees
            from .membership import classify_population, require_resolved
            activities, pagination = await activity_population(broker, opening['opening_activity_cursor'])
            now = aware_utc(clock(), field_name='activity_population_observed_at')
            generation_membership = await repositories.fills.database.run(
                lambda connection: classify_population(connection, activities, pagination, now=now), write=True)
            # Membership incidents survive a later failed financial transaction.
            require_resolved(generation_membership)
            await repositories.fills.database.run(lambda connection: persist_generation_fees(
                connection, activities, generation_membership, now=now), write=True)
    except Exception as exc:
        reason = 'GENERATION_POPULATION_INVALID:' + str(exc)
        await latch_financial_integrity_block(repositories, reason, clock=lambda: now)
        await alerts.send('critical', reason, {})
        return ReconciliationSummary('degraded', error=reason)
    from tradepulse.settlement.accounting import accounting_issues, replay_accounting
    projection_error = None
    try:
        await replay_accounting(repositories)
    except Exception as exc:
        projection_error = 'CANONICAL_ACCOUNTING_REPLAY_FAILED:' + str(exc)
        await latch_financial_integrity_block(repositories, projection_error, clock=lambda: now)
    await _recover_stranded_intents(repositories, broker, alerts, now, lease_lost, reconcile_lease=reconcile_lease)
    await _recover_inflight_orders(repositories, broker, settlement, alerts, now, lease_lost)
    fee_error = None
    try:
        if not await reconcile_asset_fees(repositories, broker, now=now, lease_lost=lease_lost, clock=clock):
            fee_error = "ASSET_FEE_RECONCILIATION_FAILED"
    except Exception as exc:  # noqa: BLE001 - preserve other instruments and reconciliation work
        await _alert_once(repositories, alerts, event_type="reconciliation_degraded", subject="asset_fees", window=_utc_hour(now),
                          severity="critical", message=f"Asset-fee reconciliation unavailable: {exc}", details={"error": str(exc)}, now=now)
        fee_error = f"ASSET_FEE_RECONCILIATION_UNAVAILABLE: {exc}"
    try:
        positions_checked, view_drift_corrected, accounting_drift_detected = await _reconcile_positions(
            repositories, broker, alerts, now, lease_lost
        )
    except Exception as exc:  # noqa: BLE001 - a broker outage here must fail this pass cleanly, not crash the caller
        await _alert_once(repositories, alerts, event_type="reconciliation_degraded", subject="positions", window=_utc_hour(now),
                          severity="critical", message=f"Reconciliation degraded -- Alpaca positions unavailable: {exc}", details={"error": str(exc)}, now=now)
        return ReconciliationSummary("degraded", error=f"BROKER_POSITIONS_UNAVAILABLE: {exc}")

    try:
        fills_checked, missed_fills_detected, late_fills_recovered = await _reconcile_fills(
            repositories, broker, settlement, alerts, now, fill_lookback, lease_lost, generation_membership
        )
    except Exception as exc:  # noqa: BLE001 - same principle for the activities call
        await _alert_once(repositories, alerts, event_type="reconciliation_degraded", subject="activities", window=_utc_hour(now),
                          severity="critical", message=f"Reconciliation degraded -- Alpaca activities unavailable: {exc}", details={"error": str(exc)}, now=now)
        return ReconciliationSummary(
            "degraded", positions_checked, view_drift_corrected, accounting_drift_detected,
            error=f"BROKER_ACTIVITIES_UNAVAILABLE: {exc}",
        )

    try:
        verification_holds_reverified = await _reverify_pending_holds(repositories, broker, settlement, alerts, now, lease_lost)
    except Exception as exc:  # noqa: BLE001 - a broker outage here must fail this pass cleanly, not crash the caller
        await _alert_once(repositories, alerts, event_type="reconciliation_degraded", subject="verification_holds", window=_utc_hour(now),
                          severity="critical", message=f"Reconciliation degraded -- verification-hold re-check unavailable: {exc}", details={"error": str(exc)}, now=now)
        return ReconciliationSummary(
            "degraded", positions_checked, view_drift_corrected, accounting_drift_detected,
            fills_checked, missed_fills_detected, late_fills_recovered,
            error=f"BROKER_VERIFICATION_UNAVAILABLE: {exc}",
        )

    try:
        from .equity_epochs import reconcile_equity_epochs
        if not await reconcile_equity_epochs(repositories, broker, now=now, clock=clock):
            fee_error = fee_error or 'ACCOUNTING_EPOCH_INCOMPLETE'
        elif await list_all_by_json_field(repositories.reconciliation_records, 'subject_id', 'equity_checkpoint'):
            from .asset_fees import _record_feed_transition
            await _record_feed_transition(repositories, ReconciliationRecord(str(uuid4()), 'accounting_population',
                'equity_checkpoint', ReconciliationOutcome.MATCHED, expected={'complete_verified_population': True},
                actual={'population_reverified': True}, occurred_at=now))
    except Exception as exc:
        fee_error = fee_error or 'ACCOUNTING_EPOCH_FAILED:' + str(exc)
        from .asset_fees import _record_feed_transition
        await _record_feed_transition(repositories, ReconciliationRecord(str(uuid4()), 'accounting_population',
            'equity_checkpoint', ReconciliationOutcome.INCOMPLETE_EVIDENCE,
            expected={'complete_verified_population': True}, actual={'error': str(exc)}, occurred_at=now))
    projection_problems = await accounting_issues(repositories)
    if projection_problems:
        projection_error = 'CANONICAL_ACCOUNTING_INCOMPLETE'
        await latch_financial_integrity_block(repositories, projection_error, clock=lambda: now)
    active_holds = await paginate_all_rows(repositories.integrity_holds)
    if active_holds:
        projection_error = projection_error or 'ACTIVE_INTEGRITY_HOLD'
    return ReconciliationSummary(
        "degraded" if fee_error or projection_error or accounting_drift_detected or missed_fills_detected else "ok", positions_checked, view_drift_corrected, accounting_drift_detected,
        fills_checked, missed_fills_detected, late_fills_recovered, verification_holds_reverified, error=fee_error or projection_error,
    )
