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

from tradepulse.alerts import TelegramAlerter
from tradepulse.broker import AlpacaClient, AlpacaError, AlpacaOrderRequest, default_time_in_force, is_definitive_rejection
from tradepulse.models import (
    AssetIdentity,
    ExecutionMode,
    Fill,
    RiskLimits,
    SettlementEvent,
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
    load_session,
)
from tradepulse.settlement import SettlementProcessor

from .idempotency import IN_FLIGHT_STATUSES, derive_idempotency_key
from .quotes import fetch_authoritative_quote

TERMINAL_STATUSES = frozenset(
    {TradeIntentStatus.FILLED, TradeIntentStatus.REJECTED, TradeIntentStatus.CANCELED, TradeIntentStatus.EXPIRED, TradeIntentStatus.FAILED}
)
TERMINAL_FAILURE_ORDER_STATUSES = frozenset({"rejected", "canceled", "expired", "replaced"})
TERMINAL_ORDER_STATUSES = frozenset({"filled", "done_for_day"} | TERMINAL_FAILURE_ORDER_STATUSES)


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
            self._repositories, cash_balance=account.cash, account_equity=account.equity, now=now
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

    async def _poll_and_settle(self, intent: TradeIntent) -> ExecutionResult:
        assert intent.broker_order_id is not None
        current = intent
        last_filled_qty = intent.filled_quantity
        last_filled_price = intent.filled_avg_price or Decimal("0")
        deadline = time.monotonic() + self.FILL_TIMEOUT_SECONDS

        while time.monotonic() < deadline:
            try:
                order = await self._broker.get_order(current.broker_order_id)
            except AlpacaError:
                await asyncio.sleep(self.POLL_INTERVAL_SECONDS)
                continue

            cumulative_filled = order.filled_qty
            cumulative_avg_price = order.filled_avg_price or Decimal("0")

            if cumulative_filled > last_filled_qty:
                incremental_qty = cumulative_filled - last_filled_qty
                # incremental_price derived from cumulative VWAP deltas, NOT
                # naive filled_avg_price diffing -- see execution.ts's
                # comment: Alpaca's filled_avg_price is the VWAP of ALL
                # fills so far, so using it directly for each increment
                # corrupts the ledger on the 2nd+ partial fill.
                prev_notional = last_filled_qty * last_filled_price
                curr_notional = cumulative_filled * cumulative_avg_price
                incremental_notional = curr_notional - prev_notional
                incremental_price = (
                    incremental_notional / incremental_qty
                    if incremental_qty > 0 and incremental_notional > 0
                    else (cumulative_avg_price if cumulative_avg_price > 0 else (current.reference_price or Decimal("0")))
                )

                fill_id = f"{current.broker_order_id}:fill:{cumulative_filled}"
                fill = Fill(
                    fill_id=fill_id, trade_intent_id=current.trade_intent_id, order_id=current.broker_order_id,
                    asset=current.asset, side=current.side, execution_mode=current.execution_mode,
                    quantity=incremental_qty, price=incremental_price, fees=Decimal("0"), slippage=Decimal("0"),
                    filled_at=self._clock(), broker_fill_id=fill_id,
                )
                created = await self._repositories.fills.create_once(fill_id, fill, unique_value=fill_id)
                if created:
                    event = SettlementEvent(
                        settlement_event_id=fill_id, fill_id=fill_id, trade_intent_id=current.trade_intent_id,
                        asset=current.asset, side=current.side, execution_mode=current.execution_mode,
                        quantity=incremental_qty, price=incremental_price, occurred_at=self._clock(),
                        broker_order_id=current.broker_order_id, broker_fill_id=fill_id,
                        client_order_id=current.client_order_id, sector=current.sector,
                    )
                    await self._repositories.settlements.create_once(fill_id, event, status=event.status.value, unique_value=fill_id)

                last_filled_qty = cumulative_filled
                last_filled_price = cumulative_avg_price
                current = replace(current, filled_quantity=cumulative_filled, filled_avg_price=cumulative_avg_price)

            if order.status in TERMINAL_ORDER_STATUSES:
                if order.status in TERMINAL_FAILURE_ORDER_STATUSES:
                    terminal_status = TradeIntentStatus.PARTIALLY_FILLED if cumulative_filled > 0 else TradeIntentStatus.REJECTED
                else:
                    terminal_status = TradeIntentStatus.FILLED
                current = replace(current, status=terminal_status)
                await self._repositories.trade_intents.update(current.trade_intent_id, current, status=current.status.value)
                await self._settlement.process_pending()
                result_status: Literal["filled", "partially_filled", "rejected"] = (
                    "filled" if terminal_status == TradeIntentStatus.FILLED
                    else "partially_filled" if terminal_status == TradeIntentStatus.PARTIALLY_FILLED
                    else "rejected"
                )
                return ExecutionResult(result_status, current.trade_intent_id, [], cumulative_filled, cumulative_avg_price or None)

            if order.status == "partially_filled" and current.status != TradeIntentStatus.PARTIALLY_FILLED:
                current = replace(current, status=TradeIntentStatus.PARTIALLY_FILLED)
                await self._repositories.trade_intents.update(current.trade_intent_id, current, status=current.status.value)

            await asyncio.sleep(self.POLL_INTERVAL_SECONDS)

        # Timeout -- order still pending; a reconciliation pass picks up the
        # final state later.
        current = replace(current, status=TradeIntentStatus.PARTIALLY_FILLED if last_filled_qty > 0 else TradeIntentStatus.ACCEPTED)
        await self._repositories.trade_intents.update(current.trade_intent_id, current, status=current.status.value)
        return ExecutionResult("pending", current.trade_intent_id, [], last_filled_qty, last_filled_price or None)
