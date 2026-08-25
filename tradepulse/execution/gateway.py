"""The single coordinated execution entry point -- port of
base44/shared/execution.ts::executeIntent, simplified for this system's
single-mode-always-Alpaca architecture (see settlement/engine.py's module
docstring for why there's no internal_paper/shadow_live simulated path, and
no separate reservation/consumed-cash bookkeeping -- Alpaca's own account
endpoint is the buying-power authority, checked live via
risk.check_cash_sufficiency inside evaluate_risk).

Unlike the audited Base44 system, there is exactly ONE way into this
function -- every CLI subcommand calls the same ExecutionGateway.execute_intent,
under the same cli-level lock discipline (see cli/locking.py). There is no
second, browser-triggered authority.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal
from uuid import uuid4

import httpx

from tradepulse.alerts import TelegramAlerter
from tradepulse.broker import (
    AlpacaClient,
    AlpacaError,
    AlpacaOrderRequest,
    default_time_in_force,
    is_definitive_rejection,
)
from tradepulse.models import (
    AssetClass,
    AssetIdentity,
    ExecutionMode,
    RiskLimits,
    Side,
    TradeIntent,
    TradeIntentStatus,
)
from tradepulse.persistence import PersistenceRepositories, hydrate
from tradepulse.providers import AlpacaMarketDataProvider, ProviderError
from tradepulse.risk import (
    RiskCheckInput,
    RiskEvalOptions,
    build_portfolio_snapshot,
    check_max_drawdown,
    evaluate_risk,
    execution_session_decision,
    latch_risk_stop,
    load_session,
)
from tradepulse.settlement import SettlementProcessor

from .fill_attribution import (
    TERMINAL_ORDER_STATUSES,
    TERMINAL_STATUSES,
    AttributedFills,
    attribute_order_fills,
    terminal_status_for_order,
)
from .idempotency import IN_FLIGHT_STATUSES, derive_idempotency_key
from .quotes import fetch_authoritative_quote


@dataclass(frozen=True, slots=True)
class ExecutionRequest:
    asset: AssetIdentity
    side: Side
    requested_quantity: Decimal
    strategy: str
    decision_id: str | None = None
    signal_timestamp: str | None = None
    confidence: Decimal | None = None
    stop_loss: Decimal | None = None
    target_price: Decimal | None = None
    sector: str | None = None
    order_type: Literal["market", "limit", "stop", "stop_limit"] = "market"
    idempotency_key: str | None = None


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    status: Literal["filled", "partially_filled", "rejected", "skipped", "pending"]
    trade_intent_id: str | None
    reasons: list[str]
    filled_quantity: Decimal
    filled_avg_price: Decimal | None


class ExecutionGateway:
    FILL_TIMEOUT_SECONDS = 20
    POLL_INTERVAL_SECONDS = 1

    def __init__(
        self,
        repositories: PersistenceRepositories,
        broker: AlpacaClient,
        market_data: AlpacaMarketDataProvider,
        settlement: SettlementProcessor,
        alerts: TelegramAlerter,
        risk_limits: RiskLimits,
        execution_mode: ExecutionMode,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._repositories = repositories
        self._broker = broker
        self._market_data = market_data
        self._settlement = settlement
        self._alerts = alerts
        self._risk_limits = risk_limits
        self._execution_mode = execution_mode
        self._clock = clock

    async def execute_intent(self, request: ExecutionRequest) -> ExecutionResult:
        now = self._clock()
        idempotency_key = request.idempotency_key or derive_idempotency_key(
            request.strategy, request.decision_id, request.signal_timestamp, request.asset.symbol, request.side
        )

        existing: TradeIntent | None = None
        if idempotency_key:
            row = await self._repositories.trade_intents.find_by_unique(idempotency_key)
            if row is not None:
                existing = hydrate("trade_intents", row["payload"])
                if existing.status in TERMINAL_STATUSES:
                    return self._result_from_intent(existing)
                if existing.status in IN_FLIGHT_STATUSES and existing.broker_order_id:
                    return await self._poll_and_settle(existing)
                if existing.status == TradeIntentStatus.SUBMISSION_UNKNOWN:
                    # A prior call's broker outcome was never established.
                    # Re-attempt resolution -- must NEVER fall through to a
                    # fresh submission while the first attempt's outcome is
                    # still unresolved (that's exactly how a duplicate order
                    # would happen).
                    return await self._recover_unknown_submission(existing, RuntimeError("retry after prior SUBMISSION_UNKNOWN"))
                # RISK_APPROVED with no broker_order_id yet (crashed before
                # submission) or PROPOSED -- fall through and continue below,
                # reusing this intent's id instead of creating a new one.

        held_quantity = await self._held_quantity(request.asset.symbol)
        protective_exit = (
            request.side == Side.SELL and held_quantity > 0 and request.requested_quantity <= held_quantity
        ) or (request.side == Side.BUY and held_quantity < 0 and request.requested_quantity <= abs(held_quantity))

        # Session check UNCONDITIONALLY before anything else -- see
        # risk/session.py for the audited defect this ordering fixes.
        session = await load_session(self._repositories)
        decision = execution_session_decision(session, request.side, request.asset.asset_class, protective_exit)
        if not decision.allowed:
            return ExecutionResult("rejected", existing.trade_intent_id if existing else None, [decision.reason], Decimal("0"), None)

        try:
            quote = await fetch_authoritative_quote(self._market_data, request.asset, request.side, now)
        except ProviderError as exc:
            return ExecutionResult("skipped", None, [f"NO_EXECUTABLE_QUOTE: {exc}"], Decimal("0"), None)

        try:
            account = await self._broker.get_account()
        except AlpacaError as exc:
            return ExecutionResult("skipped", None, [f"BROKER_UNAVAILABLE: {exc.message}"], Decimal("0"), None)
        if account.equity <= 0:
            return ExecutionResult("skipped", None, ["BROKER_ACCOUNT_INVALID"], Decimal("0"), None)

        trade_intent_id = existing.trade_intent_id if existing else str(uuid4())
        intent = TradeIntent(
            trade_intent_id=trade_intent_id,
            idempotency_key=idempotency_key or str(uuid4()),
            correlation_id=request.decision_id or trade_intent_id,
            asset=request.asset,
            side=request.side,
            execution_mode=self._execution_mode,
            strategy=request.strategy,
            created_at=existing.created_at if existing else now,
            requested_quantity=request.requested_quantity,
            reference_price=quote.price,
            confidence=float(request.confidence) if request.confidence is not None else None,
            sector=request.sector,
            stop_loss=request.stop_loss,
            target_price=request.target_price,
            status=TradeIntentStatus.PROPOSED,
        )
        if existing is None:
            await self._repositories.trade_intents.create_once(
                trade_intent_id, intent, status=intent.status.value, unique_value=intent.idempotency_key
            )
        else:
            await self._repositories.trade_intents.update(trade_intent_id, intent, status=intent.status.value)

        snapshot = await build_portfolio_snapshot(
            self._repositories, cash_balance=account.cash, account_equity=account.equity,
            broker_prev_close_equity=account.last_equity, now=now,
        )

        max_drawdown_breached = False
        if request.side == Side.BUY and not protective_exit and self._risk_limits.max_drawdown_pct > 0:
            drawdown = await check_max_drawdown(self._repositories, snapshot.total_equity, self._risk_limits)
            max_drawdown_breached = drawdown.breached

        risk_input = RiskCheckInput(
            symbol=request.asset.symbol, asset_class=request.asset.asset_class, side=request.side,
            requested_quantity=request.requested_quantity, price=quote.price,
            confidence=request.confidence, stop_loss=request.stop_loss, sector=request.sector or "Other",
        )
        risk_opts = RiskEvalOptions(
            protective_exit=protective_exit, bid=quote.bid, ask=quote.ask,
            estimated_slippage_pct=quote.estimated_slippage_pct,
            held_quantity=abs(held_quantity), available_cash=account.cash,
            max_drawdown_breached=max_drawdown_breached,
        )
        risk = evaluate_risk(risk_input, snapshot, self._risk_limits, risk_opts)
        if not risk.approved:
            # A genuine account-level kill-switch condition -- not an
            # ordinary per-trade sizing/eligibility rejection (insufficient
            # cash, confidence too low, sector/position caps, etc.) -- must
            # latch the durable session state reset-risk exists to clear,
            # not just reject this one order.
            kill_switch_reason = next(
                (r for r in risk.reasons if r.startswith("MAX_DAILY_LOSS_EXCEEDED") or r == "MAX_DRAWDOWN_BREACHED"), None
            )
            if kill_switch_reason is not None:
                await latch_risk_stop(self._repositories, kill_switch_reason, clock=self._clock)
            rejected = replace(intent, status=TradeIntentStatus.REJECTED, rejection_reason="; ".join(risk.reasons))
            await self._repositories.trade_intents.update(trade_intent_id, rejected, status=rejected.status.value)
            return ExecutionResult("rejected", trade_intent_id, risk.reasons, Decimal("0"), None)

        approved = replace(intent, status=TradeIntentStatus.RISK_APPROVED, requested_quantity=risk.approved_quantity)
        await self._repositories.trade_intents.update(trade_intent_id, approved, status=approved.status.value)

        # External quote/account/risk calls may take long enough for an
        # operator to have stopped trading. Revalidate immediately before
        # the irreversible submission boundary.
        session_at_submit = await load_session(self._repositories)
        decision_at_submit = execution_session_decision(session_at_submit, request.side, request.asset.asset_class, protective_exit)
        if not decision_at_submit.allowed:
            rejected = replace(approved, status=TradeIntentStatus.REJECTED, rejection_reason=decision_at_submit.reason)
            await self._repositories.trade_intents.update(trade_intent_id, rejected, status=rejected.status.value)
            return ExecutionResult("rejected", trade_intent_id, [decision_at_submit.reason], Decimal("0"), None)

        # Independent of the session's own MARKET_CLOSED/ACTIVE label (which
        # a scan cycle only resyncs periodically, see
        # risk/session.py::sync_market_session) -- new equity exposure gets
        # one more, always-fresh check right at the irreversible submission
        # boundary. Crypto is exempt (continuous market); any protective
        # exit is exempt regardless of asset class, matching the session
        # gate's own exemption -- a stop-loss must still be able to fire.
        if request.asset.asset_class == AssetClass.EQUITY and not protective_exit:
            try:
                market_clock = await self._broker.get_clock()
            except (AlpacaError, httpx.HTTPError) as exc:
                rejected = replace(approved, status=TradeIntentStatus.REJECTED, rejection_reason=f"MARKET_CLOCK_UNAVAILABLE: {exc}")
                await self._repositories.trade_intents.update(trade_intent_id, rejected, status=rejected.status.value)
                return ExecutionResult("rejected", trade_intent_id, [rejected.rejection_reason], Decimal("0"), None)
            if not market_clock.is_open:
                rejected = replace(approved, status=TradeIntentStatus.REJECTED, rejection_reason="EQUITY_MARKET_CLOSED")
                await self._repositories.trade_intents.update(trade_intent_id, rejected, status=rejected.status.value)
                return ExecutionResult("rejected", trade_intent_id, ["EQUITY_MARKET_CLOSED"], Decimal("0"), None)

        order_request = AlpacaOrderRequest(
            symbol=request.asset.symbol, qty=risk.approved_quantity, side=request.side,
            order_type=request.order_type, time_in_force=default_time_in_force(request.asset.asset_class),
            client_order_id=trade_intent_id,
        )
        submitted = replace(approved, status=TradeIntentStatus.SUBMITTED)
        await self._repositories.trade_intents.update(trade_intent_id, submitted, status=submitted.status.value)
        try:
            placed = await self._broker.place_order(order_request)
        except Exception as exc:  # noqa: BLE001 - must classify every submission failure, never let one crash the cycle
            if is_definitive_rejection(exc):
                # A confirmed Alpaca business-logic rejection -- safe to
                # reject outright, no recovery needed.
                reason = f"BROKER_SUBMIT_ERROR: {exc.message}" if isinstance(exc, AlpacaError) else f"BROKER_SUBMIT_ERROR: {exc}"
                rejected = replace(submitted, status=TradeIntentStatus.REJECTED, rejection_reason=reason)
                await self._repositories.trade_intents.update(trade_intent_id, rejected, status=rejected.status.value)
                return ExecutionResult("rejected", trade_intent_id, [rejected.rejection_reason], Decimal("0"), None)
            # Ambiguous outcome (429, 5xx, network/timeout/DNS error, etc.) --
            # Alpaca's actual acceptance of the order cannot be established
            # from this error alone. Never assume rejection or resubmit.
            return await self._recover_unknown_submission(submitted, exc)

        accepted = replace(submitted, status=TradeIntentStatus.ACCEPTED, broker_order_id=placed.broker_order_id, client_order_id=trade_intent_id)
        await self._repositories.trade_intents.update(trade_intent_id, accepted, status=accepted.status.value)

        return await self._poll_and_settle(accepted)

    async def _recover_unknown_submission(self, intent: TradeIntent, cause: Exception) -> ExecutionResult:
        """Called whenever a broker submission's outcome is ambiguous (see
        is_definitive_rejection), or when resuming a prior SUBMISSION_UNKNOWN
        intent. Looks the order up by client_order_id (== trade_intent_id,
        set at submission time) before concluding anything -- if Alpaca DID
        receive it, resume as normal, never resubmit; if genuinely not
        found, mark SUBMISSION_UNKNOWN and alert a human rather than guess."""
        try:
            order = await self._broker.get_order_by_client_order_id(intent.trade_intent_id)
        except Exception:  # noqa: BLE001 - the recovery lookup itself failing is ALSO ambiguous, not "not found"
            order = None

        if order is not None:
            accepted = replace(
                intent, status=TradeIntentStatus.ACCEPTED, broker_order_id=order.broker_order_id,
                client_order_id=intent.trade_intent_id,
            )
            await self._repositories.trade_intents.update(intent.trade_intent_id, accepted, status=accepted.status.value)
            return await self._poll_and_settle(accepted)

        unknown = replace(intent, status=TradeIntentStatus.SUBMISSION_UNKNOWN, rejection_reason=f"SUBMISSION_UNKNOWN: {cause}")
        await self._repositories.trade_intents.update(intent.trade_intent_id, unknown, status=unknown.status.value)
        await self._alerts.send(
            "critical",
            f"Broker submission outcome unknown for {intent.asset.symbol} {intent.side.value} {intent.requested_quantity} -- requires manual review",
            {"trade_intent_id": intent.trade_intent_id, "cause": str(cause)},
        )
        return ExecutionResult("pending", intent.trade_intent_id, [unknown.rejection_reason], Decimal("0"), None)

    async def _held_quantity(self, symbol: str) -> Decimal:
        row = await self._repositories.holdings.get(symbol.upper())
        if row is None:
            return Decimal("0")
        return hydrate("holdings", row["payload"]).quantity

    def _result_from_intent(self, intent: TradeIntent) -> ExecutionResult:
        status_map: dict[TradeIntentStatus, str] = {
            TradeIntentStatus.FILLED: "filled",
            TradeIntentStatus.REJECTED: "rejected",
            TradeIntentStatus.CANCELED: "rejected",
            TradeIntentStatus.EXPIRED: "rejected",
            TradeIntentStatus.FAILED: "rejected",
        }
        reasons = [intent.rejection_reason] if intent.rejection_reason else []
        return ExecutionResult(
            status_map.get(intent.status, "rejected"), intent.trade_intent_id, reasons,
            intent.filled_quantity, intent.filled_avg_price,
        )

    async def _attribute_order_fills(self, current: TradeIntent) -> AttributedFills:
        """Thin pass-through to the shared fill_attribution module -- kept so
        existing/direct callers of this method (including tests) don't need
        to reach into fill_attribution themselves. See that module's
        docstring for why this logic is shared rather than gateway-private:
        reconciliation's late-fill recovery needs the exact same accounting
        path, not a second way to create a Fill/SettlementEvent."""
        return await attribute_order_fills(self._repositories, self._broker, self._alerts, current, self._clock)

    async def _poll_and_settle(self, intent: TradeIntent) -> ExecutionResult:
        assert intent.broker_order_id is not None
        current = intent
        last_filled_qty = intent.filled_quantity
        deadline = time.monotonic() + self.FILL_TIMEOUT_SECONDS

        while time.monotonic() < deadline:
            try:
                order = await self._broker.get_order(current.broker_order_id)
            except AlpacaError:
                await asyncio.sleep(self.POLL_INTERVAL_SECONDS)
                continue

            cumulative_filled = order.filled_qty

            attributed_qty = last_filled_qty
            if cumulative_filled > 0:
                attributed = await self._attribute_order_fills(current)
                attributed_qty = attributed.quantity
                if attributed_qty > cumulative_filled:
                    # A broker-side data anomaly, not eventual-consistency
                    # lag -- activities scoped to this order should never sum
                    # to more than the order itself reports filled. Fail
                    # closed rather than finalize on quantity that can't be
                    # trusted.
                    await self._alerts.send(
                        "critical",
                        f"BROKER_FILL_INTEGRITY_MISMATCH: attributed fill quantity ({attributed_qty}) exceeds "
                        f"order.filled_qty ({cumulative_filled}) for {current.asset.symbol} order "
                        f"{current.broker_order_id} -- halting for manual review.",
                        {"trade_intent_id": current.trade_intent_id, "broker_order_id": current.broker_order_id,
                         "attributed_qty": str(attributed_qty), "order_filled_qty": str(cumulative_filled)},
                    )
                    return ExecutionResult(
                        "pending", current.trade_intent_id, ["BROKER_FILL_INTEGRITY_MISMATCH"], attributed_qty, attributed.avg_price
                    )
                if attributed_qty != last_filled_qty:
                    # avg_price always derives from the SAME validated
                    # activities as attributed_qty -- never Alpaca's
                    # order-level VWAP, which can cover more (or different)
                    # fills than have actually been attributed yet.
                    current = replace(current, filled_quantity=attributed_qty, filled_avg_price=attributed.avg_price or current.filled_avg_price)
                    last_filled_qty = attributed_qty

            if order.status in TERMINAL_ORDER_STATUSES:
                if cumulative_filled > 0 and attributed_qty < cumulative_filled:
                    # /orders reports more filled quantity than the
                    # Activities API has surfaced yet (eventual-consistency
                    # lag). Do NOT finalize on unattributed quantity -- keep
                    # polling within the existing timeout budget; a gap that
                    # outlives the whole poll window falls through to the
                    # timeout path below, which already leaves the intent
                    # PARTIALLY_FILLED/pending for reconciliation to pick up
                    # rather than fabricating anything.
                    await asyncio.sleep(self.POLL_INTERVAL_SECONDS)
                    continue
                terminal_status = terminal_status_for_order(order.status, attributed_qty, current.requested_quantity)
                if terminal_status is None:
                    if order.status == "filled":
                        # A broker-side contradiction (status=filled but
                        # attributed quantity doesn't cover what was
                        # requested) -- never finalize FILLED without full
                        # evidence.
                        await self._alerts.send(
                            "critical",
                            f"BROKER_ORDER_INTEGRITY_MISMATCH: order {current.broker_order_id} for {current.asset.symbol} "
                            f"reports status=filled but attributed quantity ({attributed_qty}) is less than requested "
                            f"({current.requested_quantity}).",
                            {"trade_intent_id": current.trade_intent_id, "broker_order_id": current.broker_order_id,
                             "attributed_qty": str(attributed_qty), "requested_quantity": str(current.requested_quantity)},
                        )
                        return ExecutionResult(
                            "pending", current.trade_intent_id, ["BROKER_ORDER_INTEGRITY_MISMATCH"], attributed_qty, current.filled_avg_price
                        )
                    # done_for_day with zero fills today -- inconclusive, not
                    # an error; Alpaca may still send updates the next
                    # trading day. Exit the poll loop and fall through to the
                    # timeout path below, which leaves the intent non-terminal.
                    break
                current = replace(current, status=terminal_status)
                await self._repositories.trade_intents.update(current.trade_intent_id, current, status=current.status.value)
                await self._settlement.process_pending()
                result_status: Literal["filled", "partially_filled", "rejected"] = (
                    "filled" if terminal_status == TradeIntentStatus.FILLED
                    else "partially_filled" if terminal_status == TradeIntentStatus.PARTIALLY_FILLED
                    else "rejected"
                )
                return ExecutionResult(result_status, current.trade_intent_id, [], attributed_qty, current.filled_avg_price)

            if order.status == "partially_filled" and current.status != TradeIntentStatus.PARTIALLY_FILLED:
                current = replace(current, status=TradeIntentStatus.PARTIALLY_FILLED)
                await self._repositories.trade_intents.update(current.trade_intent_id, current, status=current.status.value)

            await asyncio.sleep(self.POLL_INTERVAL_SECONDS)

        # Timeout -- order still pending; a reconciliation pass picks up the
        # final state later.
        current = replace(current, status=TradeIntentStatus.PARTIALLY_FILLED if last_filled_qty > 0 else TradeIntentStatus.ACCEPTED)
        await self._repositories.trade_intents.update(current.trade_intent_id, current, status=current.status.value)
        return ExecutionResult("pending", current.trade_intent_id, [], last_filled_qty, current.filled_avg_price)
