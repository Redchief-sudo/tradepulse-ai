"""The scan cycle: AI-driven candidate discovery wired into Opportunity/
TradeIntent construction and the execution gateway.

Division of responsibility (matches the discovery-only contract shared by
every AI backend -- see tradepulse/providers/ai_provider.py): the AI
proposes SYMBOLS and a recommendation/confidence bucket only. It never
supplies a price, a quantity, a stop-loss, or a target -- this module
fetches its own reference quote per candidate
(never the AI's word for it), and the execution gateway fetches its OWN
fresh authoritative quote again before submitting, re-derives risk sizing
from scratch, and re-checks the trading session immediately before the
irreversible submission boundary. The AI's output is treated as an
untrusted, fallible hint that can only ever narrow what gets submitted, not
force it.

Only STRONG_BUY/BUY recommendations are acted on here -- opening a new long
position. SELL/STRONG_SELL exits require an existing holding and protective-
exit classification, which is a stop/target-monitor's job (explicitly
deferred), not the scanner's.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import ROUND_FLOOR, Decimal
from typing import Any
from uuid import uuid4

from tradepulse.broker import AlpacaClient
from tradepulse.config import default_strategy_weights
from tradepulse.execution import (
    SYMBOL_LOCK_TTL_SECONDS,
    ExecutionGateway,
    ExecutionRequest,
    ExecutionResult,
    execution_lock_key,
    has_in_flight_intent,
    release_symbol_reservation,
    reserve_symbol_for_execution,
)
from tradepulse.models import (
    AssetClass,
    AssetIdentity,
    Candle,
    Opportunity,
    RiskLimits,
    ScanRun,
    ScanRunStatus,
    ScanTrigger,
    SessionState,
    Side,
    StrategyWeights,
    contract_multiplier_of,
)
from tradepulse.persistence import PersistenceRepositories, hydrate, run_with_lock_renewal
from tradepulse.providers import (
    AIProvider,
    AlpacaMarketDataProvider,
    OpportunityCandidate,
    ProviderDataFailure,
    ProviderError,
    ProviderHttpFailure,
    build_scan_request,
)
from tradepulse.risk import build_portfolio_snapshot, load_session, sync_market_session
from tradepulse.strategy import (
    ExecutableUniverse,
    atr,
    compute_real_factors,
    is_executable,
    select_contract,
    signal_from_composite,
    weighted_composite,
)

logger = logging.getLogger(__name__)

_ACTIONABLE_RECOMMENDATIONS = frozenset({"STRONG_BUY", "BUY"})
_DETERMINISTIC_ACTIONABLE_SIGNALS = frozenset({"STRONG_BUY", "BUY"})

# Provenance for every Opportunity -- which Alpaca feed actually produced
# the quote a trade decision was based on, so paper-trading results can
# later be separated into consolidated (SIP/OPRA) vs. non-consolidated
# (IEX/indicative) evidence. Keys match RawQuote.source (see
# broker/alpaca_client.py). IEX is a real exchange feed, not "indicative" --
# these are kept as two distinct authority levels, never collapsed into one.
_MARKET_DATA_AUTHORITY = {
    "alpaca_sip": "consolidated",
    "alpaca_opra": "consolidated",
    "alpaca_iex": "exchange_limited",
    "alpaca_indicative": "indicative",
    "alpaca_crypto": "crypto",
}

# Generous vs cli.py's SCAN_LOCK_TTL_SECONDS=600 -- the lock already prevents
# real overlap; this only cleans up the audit trail after a crash left a
# ScanRun stuck at RUNNING forever.
STALE_SCAN_RUN_SECONDS = 900


@dataclass(frozen=True, slots=True)
class ScanCycleSummary:
    scan_run_id: str
    status: ScanRunStatus
    candidates_discovered: int
    candidates_approved: int
    orders_submitted: int
    execution_results: list[ExecutionResult]
    error: str | None = None


def _round_quantity(qty: Decimal, asset_class: AssetClass) -> Decimal:
    """Must match risk/engine.py's own precision policy (`_round_qty`) --
    this is only ever an UPPER BOUND the gateway's risk check will re-derive
    and can only shrink further, so a mismatch here can't create risk, but
    keeping it aligned avoids proposing quantities the risk engine would
    immediately re-floor for cosmetic reasons."""
    precision = Decimal("100000000") if asset_class == AssetClass.CRYPTO else Decimal("1") if asset_class == AssetClass.OPTION else Decimal("1000")
    floored = (max(qty, Decimal("0")) * precision).to_integral_value(rounding=ROUND_FLOOR)
    return floored / precision


def _round_price(value: Decimal, asset_class: AssetClass) -> Decimal:
    precision = Decimal("0.00000001") if asset_class == AssetClass.CRYPTO else Decimal("0.01")
    return value.quantize(precision)


def _stop_loss_price(reference_price: Decimal, stop_loss_pct: Decimal, asset_class: AssetClass) -> Decimal:
    """The scanner's fallback protective-stop source, used when an
    ATR-based stop (see _atr_stop_loss_price) can't be computed -- derived
    from RiskLimits.stop_loss_pct, the same per-profile value risk/engine.py's
    risk-per-share sizing already expects a caller to supply."""
    raw = reference_price * (Decimal("1") - stop_loss_pct / Decimal("100"))
    return _round_price(raw, asset_class)


def _atr_stop_loss_price(
    reference_price: Decimal, candles: list[Candle], atr_multiplier: Decimal, asset_class: AssetClass,
    min_stop_distance_pct: Decimal, max_stop_distance_pct: Decimal,
) -> Decimal | None:
    """Volatility-aware stop distance -- the scanner's PRIMARY source of a
    protective stop, replacing the fixed stop_loss_pct entirely so
    risk/engine.py's existing risk_per_share = price - stop_loss sizing
    formula automatically becomes volatility-aware too, without a second/
    independent sizing formula. Returns None (caller falls back to the
    fixed-pct stop) when ATR can't be computed OR when the resulting
    distance is outside the configured sanity band -- a near-zero distance
    would otherwise feed a pathologically large risk-based quantity into
    sizing; an oversized distance isn't a meaningful protective level at
    all."""
    atr_value = atr([float(c.high) for c in candles], [float(c.low) for c in candles], [float(c.close) for c in candles])
    if atr_value is None or atr_value <= 0 or reference_price <= 0:
        return None
    distance = Decimal(str(atr_value)) * atr_multiplier
    distance_pct = (distance / reference_price) * 100
    if distance_pct < min_stop_distance_pct or distance_pct > max_stop_distance_pct:
        return None
    raw = reference_price - distance
    if raw <= 0:
        return None
    return _round_price(raw, asset_class)


def _build_scan_prompt(symbols: list[str], asset_class: AssetClass) -> str:
    """One lane's symbols only -- the AI is never shown the other lane's
    universe. The AI still only ever IDENTIFIES candidates; it never
    decides position size (ATR, confidence-adjusted risk budget, and the
    deterministic allocator in risk/engine.py do that) -- these prompts add
    asset-aware market-interpretation context, not risk authority or
    increasingly aggressive instructions."""
    if asset_class == AssetClass.CRYPTO:
        return (
            "You are a crypto market-scanning analyst for an automated trading system. "
            "Crypto markets trade continuously (24/7, no session close) and typically show "
            "higher volatility and momentum persistence than equities. Review the following "
            "tradeable pairs and report any worth considering for a new long position right now. "
            "For each pair you report, give a recommendation (STRONG_BUY, BUY, HOLD, SELL, or "
            "STRONG_SELL), a confidence score from 0-100, and a one-sentence summary of your "
            "reasoning. Only report pairs from this exact list. It is fine to report zero "
            "candidates if nothing stands out.\n\nPairs: " + ", ".join(symbols)
        )
    if asset_class == AssetClass.OPTION:
        return (
            "You are an options market-scanning analyst for an automated trading system. "
            "You are evaluating the UNDERLYING stocks/ETFs below for a bullish directional view "
            "only -- you never choose a specific option contract, strike, or expiration; a "
            "separate deterministic process selects the actual contract once you identify a "
            "promising underlying. Review the following underlying symbols and report any worth "
            "a bullish view right now. For each symbol you report, give a recommendation "
            "(STRONG_BUY, BUY, HOLD, SELL, or STRONG_SELL), a confidence score from 0-100, and a "
            "one-sentence summary of your reasoning. Only report symbols from this exact list. It "
            "is fine to report zero candidates if nothing stands out.\n\nUnderlying symbols: " + ", ".join(symbols)
        )
    return (
        "You are an equity/ETF market-scanning analyst for an automated trading system. "
        "Equity markets trade during regular exchange sessions and are more influenced by "
        "fundamentals, sector rotation, and broader market regime than continuous crypto markets. "
        "Review the following tradeable symbols and report any worth considering for a new long "
        "position today. For each symbol you report, give a recommendation (STRONG_BUY, BUY, "
        "HOLD, SELL, or STRONG_SELL), a confidence score from 0-100, and a one-sentence summary "
        "of your reasoning. Only report symbols from this exact list. It is fine to report zero "
        "candidates if nothing stands out.\n\nSymbols: " + ", ".join(symbols)
    )


def _asset_from_candidate(candidate: OpportunityCandidate) -> AssetIdentity:
    asset_class = AssetClass.CRYPTO if "/" in candidate.symbol else AssetClass.EQUITY
    return AssetIdentity(symbol=candidate.symbol, asset_class=asset_class, native_asset_id=f"alpaca:{candidate.symbol.upper()}")


async def _reclaim_stale_scan_runs(repositories: PersistenceRepositories, now: datetime) -> None:
    """A crash mid-cycle leaves its ScanRun stuck at RUNNING forever -- not a
    safety issue (the `locks`-table lease, not this row, is what actually
    gates re-entry), but a permanently-stale audit record. Finalize any such
    row as FAILED before starting a new cycle, mirroring the stale-lease
    reclaim already used for settlement (is_settlement_processable) and the
    CLI scan lock (SCAN_LOCK_TTL_SECONDS)."""
    rows = await repositories.scan_runs.list_by_status(ScanRunStatus.RUNNING.value, limit=50)
    for row in rows:
        run = hydrate("scan_runs", row["payload"])
        if (now - run.started_at).total_seconds() > STALE_SCAN_RUN_SECONDS:
            finalized = replace(run, status=ScanRunStatus.FAILED, completed_at=now, error="CRASHED_STALE_SCAN_RUN")
            await repositories.scan_runs.update(run.scan_run_id, finalized, status=finalized.status.value)


async def run_scan_cycle(
    repositories: PersistenceRepositories,
    ai_provider: AIProvider,
    market_data: AlpacaMarketDataProvider,
    broker: AlpacaClient,
    gateway: ExecutionGateway,
    universe: ExecutableUniverse,
    risk_limits: RiskLimits,
    asset_class: AssetClass,
    *,
    trigger: ScanTrigger = ScanTrigger.SCHEDULED,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    strategy_weights: StrategyWeights | None = None,
    lease_lost: asyncio.Event | None = None,
) -> ScanCycleSummary:
    now = clock()
    strategy_weights = strategy_weights or default_strategy_weights(now)
    await _reclaim_stale_scan_runs(repositories, now)
    scan_run_id = str(uuid4())
    scan_generation = now.strftime("%Y%m%dT%H%M%SZ")
    scan_run = ScanRun(
        scan_run_id=scan_run_id, scan_generation=scan_generation, trigger=trigger, asset_class=asset_class,
        status=ScanRunStatus.RUNNING, started_at=now, lock_owner_token=str(uuid4()),
    )
    await repositories.scan_runs.create_once(scan_run_id, scan_run, status=scan_run.status.value)

    def _reject(symbol: str, reason: str, **context: Any) -> None:
        """Every candidate-filtering `continue` below logs through here
        first -- without this, a scan that approves zero candidates gives
        no clue which gate(s) it lost to. Relies on JsonFormatter forwarding
        every `extra=` field automatically (config/logging.py). A closure
        (not the former module-level function) so every rejection carries
        this cycle's lane without threading it through every call site."""
        logger.info(
            "candidate_rejected",
            extra={"event": "candidate_rejected", "symbol": symbol, "reason": reason, "asset_class": asset_class.value, **context},
        )

    async def _finish(status: ScanRunStatus, **fields: Any) -> None:
        finished = replace(scan_run, status=status, completed_at=clock(), **fields)
        await repositories.scan_runs.update(scan_run_id, finished, status=finished.status.value)

    session = await load_session(repositories)
    if session.state in (SessionState.ACTIVE, SessionState.MARKET_CLOSED):
        synced = await sync_market_session(repositories, broker, clock)
        if synced is not None:
            session = synced
    hard_blocked_states = (SessionState.FINANCIAL_INTEGRITY_BLOCKED, SessionState.RISK_STOPPED)
    if session.state in hard_blocked_states or session.kill_switch_reset_required or session.financial_integrity_manual_reenable_required:
        await _finish(ScanRunStatus.FAILED, error="SESSION_BLOCKED")
        return ScanCycleSummary(scan_run_id, ScanRunStatus.FAILED, 0, 0, 0, [], error="SESSION_BLOCKED")

    if asset_class == AssetClass.CRYPTO:
        lane_symbols = sorted(universe.crypto)
    elif asset_class == AssetClass.OPTION:
        lane_symbols = sorted(universe.options_underlyings)
    else:
        lane_symbols = sorted(universe.equities)
    request = build_scan_request(str(uuid4()), scan_run_id, _build_scan_prompt(lane_symbols, asset_class))
    try:
        ai_response, candidates = await ai_provider.scan_candidates(request)
    except (ProviderHttpFailure, ProviderDataFailure) as exc:
        await _finish(ScanRunStatus.FAILED, error=str(exc))
        return ScanCycleSummary(scan_run_id, ScanRunStatus.FAILED, 0, 0, 0, [], error=str(exc))

    await repositories.ai_responses.create_once(ai_response.request_id, ai_response)

    try:
        account = await broker.get_account()
    except Exception as exc:  # noqa: BLE001 - a broker outage must fail this scan cleanly, not crash the caller
        await _finish(ScanRunStatus.FAILED, candidates_discovered=len(candidates), error=f"BROKER_UNAVAILABLE: {exc}")
        return ScanCycleSummary(scan_run_id, ScanRunStatus.FAILED, len(candidates), 0, 0, [], error=f"BROKER_UNAVAILABLE: {exc}")

    # Persist one broker-truth equity snapshot per cycle -- the sole source
    # check_max_drawdown() has to search for a historical peak. Without this,
    # equity_snapshots stays permanently empty and drawdown protection can
    # never trip (drawdown against an empty history is always 0%).
    equity_snapshot = await build_portfolio_snapshot(
        repositories, cash_balance=account.cash, account_equity=account.equity,
        broker_prev_close_equity=account.last_equity, now=now,
    )
    await repositories.equity_snapshots.create_once(equity_snapshot.snapshot_id, equity_snapshot)

    notional_budget = (risk_limits.max_position_pct / 100) * account.equity

    execution_results: list[ExecutionResult] = []
    approved = 0
    submitted = 0
    for candidate in candidates:
        if lease_lost is not None and lease_lost.is_set():
            _reject(candidate.symbol, "COMMAND_LEASE_LOST")
            continue  # scan's own command lease may no longer be exclusive -- stop starting new work
        if candidate.recommendation not in _ACTIONABLE_RECOMMENDATIONS:
            _reject(candidate.symbol, "NOT_ACTIONABLE_RECOMMENDATION", recommendation=candidate.recommendation)
            continue
        if candidate.confidence < risk_limits.min_confidence:
            _reject(candidate.symbol, "CONFIDENCE_BELOW_MIN", confidence=candidate.confidence, min_confidence=str(risk_limits.min_confidence))
            continue
        # For the OPTIONS lane, `asset` is only ever the UNDERLYING's plain
        # equity identity -- the AI proposes a directional view on the
        # underlying, never a specific contract (see _build_scan_prompt's
        # options branch). The universe/lane checks below run against this
        # underlying; the resolved CONTRACT (built further down, after the
        # deterministic gate passes) is never re-checked against the
        # universe since it's produced by our own deterministic code from
        # the already-validated underlying's real chain data, not untrusted
        # AI output -- the underlying is the only untrusted-input boundary.
        asset = _asset_from_candidate(candidate)
        if asset_class == AssetClass.OPTION:
            if asset.asset_class != AssetClass.EQUITY:
                _reject(candidate.symbol, "OUTSIDE_SCAN_LANE")
                continue
            if asset.symbol not in universe.options_underlyings:
                _reject(candidate.symbol, "OUTSIDE_EXECUTABLE_UNIVERSE")
                continue
        else:
            if not is_executable(asset, universe):
                _reject(candidate.symbol, "OUTSIDE_EXECUTABLE_UNIVERSE")
                continue
            if asset.asset_class != asset_class:
                # Defense in depth -- the prompt only ever offers this
                # lane's symbols, but AI output is an untrusted hint (same
                # principle as OUTSIDE_EXECUTABLE_UNIVERSE above), so
                # nothing actually forces it to honor that.
                _reject(candidate.symbol, "OUTSIDE_SCAN_LANE")
                continue

        try:
            quote = await market_data.fetch_quote(asset)
        except ProviderError as exc:
            _reject(candidate.symbol, "QUOTE_FETCH_FAILED", error=str(exc))
            continue  # one bad quote must not abort the rest of the scan

        try:
            candles = await market_data.fetch_candles(asset)
        except ProviderError as exc:
            _reject(candidate.symbol, "CANDLE_FETCH_FAILED", error=str(exc))
            continue  # insufficient candle history or a data-fetch failure -- fail closed, same as every other provider boundary here
        scores = compute_real_factors(candles)
        if scores is None:
            _reject(candidate.symbol, "INSUFFICIENT_FACTOR_DATA")
            continue
        composite = weighted_composite(scores, strategy_weights)
        deterministic_signal = signal_from_composite(composite)
        if deterministic_signal not in _DETERMINISTIC_ACTIONABLE_SIGNALS:
            _reject(
                candidate.symbol, "DETERMINISTIC_SIGNAL_DISAGREED", ai_recommendation=candidate.recommendation,
                deterministic_signal=deterministic_signal, composite_score=str(composite),
            )
            continue  # AI proposed it, but the deterministic technical/momentum/risk read disagrees, on the UNDERLYING's own technicals

        # trade_asset/trade_quote are what actually gets sized, stopped, and
        # executed -- equal to the underlying's own asset/quote for every
        # lane except OPTIONS, where a deterministic (never AI-chosen)
        # contract-selection step turns the underlying's bullish signal into
        # a specific, tradeable OCC contract.
        if asset_class == AssetClass.OPTION:
            try:
                chain = await market_data.fetch_option_chain(
                    asset.symbol, risk_limits.options_expiry_min_days, risk_limits.options_expiry_max_days, now.date(),
                )
            except ProviderError as exc:
                _reject(candidate.symbol, "OPTION_CHAIN_FETCH_FAILED", error=str(exc))
                continue
            contract = select_contract(
                "call", quote.price, chain, min_dte=risk_limits.options_expiry_min_days,
                max_dte=risk_limits.options_expiry_max_days, target_otm_pct=risk_limits.options_target_otm_pct, now=now.date(),
            )
            if contract is None:
                _reject(candidate.symbol, "NO_ELIGIBLE_OPTION_CONTRACT")
                continue
            trade_asset = AssetIdentity(
                symbol=contract.occ_symbol, asset_class=AssetClass.OPTION, native_asset_id=f"alpaca:{contract.occ_symbol}",
                metadata={
                    "underlying_symbol": contract.underlying_symbol, "expiry": contract.expiry.isoformat(),
                    "strike": str(contract.strike), "option_type": contract.option_type,
                    "contract_multiplier": str(contract.contract_multiplier),
                },
            )
            try:
                trade_quote = await market_data.fetch_quote(trade_asset)
            except ProviderError as exc:
                _reject(candidate.symbol, "OPTION_QUOTE_FETCH_FAILED", error=str(exc))
                continue
        else:
            trade_asset = asset
            trade_quote = quote

        database = repositories.trade_intents.database
        owner_token = str(uuid4())
        if not await reserve_symbol_for_execution(database, trade_asset, owner_token):
            _reject(candidate.symbol, "SYMBOL_EXECUTION_LOCKED")
            continue  # another coordinator is already processing this asset -- don't race it
        try:
            if await has_in_flight_intent(repositories, trade_asset):
                _reject(candidate.symbol, "SYMBOL_HAS_IN_FLIGHT_INTENT")
                continue  # don't fight an order already in flight on this symbol (e.g. from the position monitor)

            multiplier = contract_multiplier_of(trade_asset)  # 1 for equity/crypto, ~100 for an options contract
            if notional_budget <= 0 or trade_quote.price <= 0:
                _reject(candidate.symbol, "NO_NOTIONAL_BUDGET_OR_INVALID_PRICE", notional_budget=str(notional_budget), price=str(trade_quote.price))
                continue
            quantity = _round_quantity(notional_budget / (trade_quote.price * multiplier), trade_asset.asset_class)
            if quantity <= 0:
                _reject(candidate.symbol, "QUANTITY_ROUNDED_TO_ZERO", notional_budget=str(notional_budget), price=str(trade_quote.price))
                continue

            if asset_class == AssetClass.OPTION:
                # A flat pct-of-entry-premium stop, never ATR -- ATR would
                # need the CONTRACT's own candle history, which this design
                # deliberately doesn't fetch (too short-lived/decay-driven
                # to be a meaningful momentum signal; see the deterministic
                # gate above, which correctly runs on the underlying
                # instead). Reuses the same helper the equity/crypto
                # fallback path already uses, just against the option's own
                # premium.
                stop_loss = (
                    _stop_loss_price(trade_quote.price, risk_limits.options_premium_stop_pct, AssetClass.OPTION)
                    if risk_limits.options_premium_stop_pct > 0 else None
                )
            else:
                stop_loss = (
                    (
                        _atr_stop_loss_price(
                            trade_quote.price, candles, risk_limits.atr_stop_multiplier, trade_asset.asset_class,
                            risk_limits.min_stop_distance_pct, risk_limits.max_stop_distance_pct,
                        )
                        if risk_limits.atr_stop_multiplier > 0 else None
                    )
                    or (_stop_loss_price(trade_quote.price, risk_limits.stop_loss_pct, trade_asset.asset_class) if risk_limits.stop_loss_pct > 0 else None)
                )

            opportunity = Opportunity(
                opportunity_id=str(uuid4()), scan_generation=scan_generation, asset=trade_asset, quote=trade_quote,
                source=ai_response.provider, created_at=clock(), confidence=candidate.confidence,
                metadata={
                    "ai_recommendation": candidate.recommendation, "ai_summary": candidate.summary,
                    "ai_request_id": ai_response.request_id,
                    "deterministic_signal": deterministic_signal, "composite_score": str(composite),
                    "technical_score": str(scores.technical_score), "momentum_score": str(scores.momentum_score),
                    "risk_score": str(scores.risk_score),
                    "stop_loss": str(stop_loss) if stop_loss is not None else None,
                    "market_data_provider": "alpaca",
                    "market_data_feed": trade_quote.provider.removeprefix("alpaca_"),
                    "market_data_authority": _MARKET_DATA_AUTHORITY.get(trade_quote.provider, "unknown"),
                },
            )
            await repositories.opportunities.create_once(opportunity.opportunity_id, opportunity)

            approved += 1
            exec_request = ExecutionRequest(
                asset=trade_asset, side=Side.BUY, requested_quantity=quantity, strategy="ai_scan",
                decision_id=opportunity.opportunity_id, confidence=Decimal(str(candidate.confidence)),
                stop_loss=stop_loss, symbol_lock_owner_token=owner_token,
            )
            result = await run_with_lock_renewal(
                database, execution_lock_key(trade_asset), owner_token, SYMBOL_LOCK_TTL_SECONDS, gateway.execute_intent(exec_request),
            )
            execution_results.append(result)
            if result.status not in ("rejected", "skipped"):
                submitted += 1
        finally:
            await release_symbol_reservation(database, trade_asset, owner_token)

    await _finish(
        ScanRunStatus.COMPLETED, candidates_discovered=len(candidates),
        candidates_approved=approved, orders_submitted=submitted,
    )
    return ScanCycleSummary(scan_run_id, ScanRunStatus.COMPLETED, len(candidates), approved, submitted, execution_results)
