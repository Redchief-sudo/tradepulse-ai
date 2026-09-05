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
import contextlib
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import ROUND_FLOOR, Decimal
from typing import Any
from uuid import uuid4

from tradepulse.broker import AlpacaClient
from tradepulse.config import default_strategy_weights, sector_for_symbol
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
    RejectedCandidate,
    RiskLimits,
    ScanRun,
    ScanRunStatus,
    ScanTrigger,
    SessionState,
    Side,
    StrategyWeights,
    asset_identity_key,
    contract_multiplier_of,
)
from tradepulse.persistence import PersistenceRepositories, hydrate, list_all_by_statuses, paginate_all_rows, run_with_lock_renewal
from tradepulse.providers import (
    AIProvider,
    AlpacaMarketDataProvider,
    MarketDataCapabilities,
    OpportunityCandidate,
    ProviderDataFailure,
    ProviderError,
    ProviderHttpFailure,
    build_scan_request,
)
from tradepulse.risk import build_portfolio_snapshot, load_session, sync_market_session
from tradepulse.strategy import (
    Calendar,
    ExecutableUniverse,
    FactorScores,
    Signal,
    atr,
    classify_regime,
    compute_real_factors,
    factor_breakdown,
    is_executable,
    pearson_correlation,
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

# Market Regime Phase 2 -- Architecture A: one broad-market benchmark
# regime per lane per cycle, applied to every candidate approved that
# cycle (never a per-candidate/per-instrument classifier). Options
# inherit the equity/broad-market regime rather than classify the option
# contract's own price history -- matches the existing deterministic-gate
# precedent (candles are always fetched for the underlying, never the
# contract; see test_deterministic_gate_fetches_candles_for_underlying_not_contract).
_BENCHMARK_ASSETS: dict[AssetClass, AssetIdentity] = {
    AssetClass.EQUITY: AssetIdentity("SPY", AssetClass.EQUITY, "alpaca:SPY"),
    AssetClass.OPTION: AssetIdentity("SPY", AssetClass.EQUITY, "alpaca:SPY"),
    AssetClass.CRYPTO: AssetIdentity("BTC/USD", AssetClass.CRYPTO, "alpaca:BTC/USD"),
}
_BENCHMARK_CALENDAR: dict[AssetClass, Calendar] = {
    AssetClass.EQUITY: "equity", AssetClass.OPTION: "equity", AssetClass.CRYPTO: "crypto",
}

# Applied whenever the benchmark fetch/classification fails -- deliberately
# NOT 1.0 (that would treat "we have no signal" as more permissive than "we
# have a confirmed elevated-risk signal", backwards for a fail-closed
# system) and NOT 0.0 (that would make an infrastructure hiccup as
# punishing as a confirmed liquidity crisis, and would amount to inventing
# a de facto lane-wide kill switch outside the session-state machinery).
# Matches high_vol_bear's own conservatism: absence of information is
# treated as AT LEAST as risky as a confirmed elevated-risk regime, never
# less.
REGIME_UNAVAILABLE_MULTIPLIER = Decimal("0.5")


@dataclass(frozen=True, slots=True)
class _LaneRegime:
    multiplier: Decimal  # always populated, always in [0, 1] -- never None
    snapshot: Mapping[str, str | int | None]  # plain, gateway-agnostic -- execution/gateway.py never interprets this, just copies it
    # Strategy Sophistication Phase 1 -- the SAME benchmark candle closes
    # already fetched to classify this lane's regime this cycle, oldest-
    # first, threaded out so compute_real_factors's relative-strength
    # factor can reuse them without a duplicate fetch. None on every
    # benchmark-unavailable path (by omission, matching multiplier/snapshot's
    # own fail-closed treatment).
    benchmark_closes: list[Decimal] | None = None


def _unavailable_lane_regime(reason: str) -> _LaneRegime:
    return _LaneRegime(
        multiplier=REGIME_UNAVAILABLE_MULTIPLIER,
        snapshot={
            "regime": "unavailable", "regime_reason": reason,
            "regime_position_multiplier": str(REGIME_UNAVAILABLE_MULTIPLIER),
        },
    )


async def _classify_lane_regime(market_data: AlpacaMarketDataProvider, asset_class: AssetClass) -> _LaneRegime:
    """One benchmark fetch per lane per cycle -- options fetches SPY
    independently too (not a read of equity's own last result): simpler,
    self-contained, no cross-lane coupling in the concurrent tradepulse-run
    supervisor, and matches the ~1-extra-request-per-lane-per-cycle cost
    already budgeted. Never raises and never blocks candidate evaluation --
    but a failure degrades to an explicit, conservative, PERSISTED
    "unavailable" state with a truthful, distinguishable reason, never a
    silent no-op and never a blanket `except Exception` around everything.

    Three narrow, specific exception surfaces, verified against source
    (not assumed):
      - ProviderError (ProviderHttpFailure/ProviderDataFailure) -- covers
        both a clean HTTP/transport failure AND "insufficient history"
        (fetch_candles's own MIN_CANDLES=30 gate already raises
        ProviderDataFailure for that case -- same except clause, same
        underlying cause class: the provider couldn't supply usable bars).
      - (ValueError, decimal.InvalidOperation) -- defense-in-depth, not the
        primary guard: providers/alpaca_market_data.py::fetch_candles now
        normalizes a malformed numeric bar field (get_bars's own bare
        Decimal(str(value)), raising decimal.InvalidOperation) or a
        semantically invalid bar (Candle.__post_init__, e.g. high < low)
        into ProviderDataFailure itself, so this branch should already be
        unreachable in practice -- the `except ProviderError` clause above
        catches it first, reported as "benchmark_fetch_failed" rather than
        the more specific "benchmark_data_invalid" this branch would give.
        Kept as a second layer rather than removed, matching this
        function's own `except Exception` precedent below -- this
        orchestration layer should not permanently depend on the provider
        boundary staying correct forever.
      - Exception, scoped ONLY around the classify_regime call itself
        (never around the fetch) -- unreachable today: classify_regime
        never raises for insufficient/non-finite/invalid closes (Phase 1
        already fails closed internally to "transition" for those), and
        never raises for timeframe/calendar here (always called with
        fixed, valid values). Kept as defense-in-depth only, since
        classify_regime is strategy-layer code this orchestration layer
        should not blindly assume will never change its contract -- not a
        real observed failure mode.
    """
    from decimal import InvalidOperation

    benchmark = _BENCHMARK_ASSETS[asset_class]
    calendar = _BENCHMARK_CALENDAR[asset_class]
    try:
        candles = await market_data.fetch_candles(benchmark)
    except ProviderError as exc:
        logger.warning("regime_benchmark_fetch_failed", extra={"event": "regime_benchmark_fetch_failed", "benchmark": benchmark.symbol, "error": str(exc)})
        return _unavailable_lane_regime("benchmark_fetch_failed")
    except (ValueError, InvalidOperation) as exc:
        logger.warning("regime_benchmark_data_invalid", extra={"event": "regime_benchmark_data_invalid", "benchmark": benchmark.symbol, "error": str(exc)})
        return _unavailable_lane_regime("benchmark_data_invalid")

    try:
        classification = classify_regime([c.close for c in candles], timeframe="1day", calendar=calendar)
    except Exception as exc:  # noqa: BLE001 -- see docstring: unreachable today, deliberate defense-in-depth against strategy-layer code this orchestration layer should not blindly trust
        logger.error("regime_classification_failed", extra={"event": "regime_classification_failed", "benchmark": benchmark.symbol, "error": str(exc)})
        return _unavailable_lane_regime("regime_classification_failed")

    return _LaneRegime(
        multiplier=classification.position_multiplier,
        snapshot={
            "regime": classification.regime, "regime_confidence": classification.confidence,
            "regime_position_multiplier": str(classification.position_multiplier),
            "regime_realized_vol": str(classification.realized_vol) if classification.realized_vol is not None else None,
            "regime_timeframe": classification.timeframe, "regime_calendar": classification.calendar,
        },
        benchmark_closes=[c.close for c in candles],
    )


def _scan_run_regime_fields(lane_regime: _LaneRegime, effective_weights: StrategyWeights) -> dict[str, Any]:
    """Translates a _LaneRegime's plain, gateway-agnostic snapshot dict
    into ScanRun's typed keyword fields -- the one place that knows
    ScanRun's specific field names/types, so the snapshot dict itself
    (also reused verbatim in TradeIntent.risk_snapshot, see execution/gateway.py)
    can stay generic."""
    snapshot = lane_regime.snapshot
    position_multiplier = snapshot.get("regime_position_multiplier")
    realized_vol = snapshot.get("regime_realized_vol")
    return {
        "regime": snapshot.get("regime"),
        "regime_reason": snapshot.get("regime_reason"),
        "regime_confidence": snapshot.get("regime_confidence"),
        "regime_position_multiplier": Decimal(str(position_multiplier)) if position_multiplier is not None else None,
        "regime_realized_vol": Decimal(str(realized_vol)) if realized_vol is not None else None,
        "regime_weight_profile": effective_weights.version,
    }


@dataclass(frozen=True, slots=True)
class ScanCycleSummary:
    scan_run_id: str
    status: ScanRunStatus
    candidates_discovered: int
    candidates_approved: int
    orders_submitted: int
    execution_results: list[ExecutionResult]
    error: str | None = None


@dataclass(frozen=True, slots=True)
class _ScoredCandidate:
    """Strategy Sophistication Phase 1 -- carries pass 1's (scoring/gating)
    results into pass 2 (ranking/execution), so pass 2 never re-fetches
    candles or re-derives scores for a candidate it already evaluated."""

    candidate: OpportunityCandidate
    asset: AssetIdentity
    candles: list[Candle]
    scores: FactorScores
    composite: Decimal
    deterministic_signal: Signal
    # Portfolio Optimization -- set by _correlation_adjusted_rank, None
    # until that step runs. max_correlation is the highest absolute Pearson
    # correlation found against anything already approved this cycle or
    # already held; correlation_penalty_applied records whether it crossed
    # risk_limits.max_correlation_threshold (a rank demotion, never a
    # rejection).
    max_correlation: Decimal | None = None
    correlation_penalty_applied: bool = False


async def _correlation_adjusted_rank(
    repositories: PersistenceRepositories, market_data: AlpacaMarketDataProvider,
    ranked: list[_ScoredCandidate], risk_limits: RiskLimits,
) -> list[_ScoredCandidate]:
    """Portfolio Optimization -- a stable partition (never a hard reject):
    candidates highly correlated (absolute Pearson, daily returns) with
    something already approved this cycle or already held are demoted below
    non-correlated peers, preserving each bucket's own relative order.
    Candidate-vs-candidate correlation is free (candles already fetched in
    pass 1); candidate-vs-holdings needs one new fetch per DISTINCT held
    asset not already among this cycle's candidates -- a data-fetch failure
    for one holding degrades to "no correlation signal for that holding,"
    never crashes the cycle.

    Correlation is only ever computed WITHIN an asset class (equity vs
    equity, crypto vs crypto) -- pearson_correlation tail-aligns purely by
    array position with no date awareness, and crypto trades 365 days/yr
    against equity's ~252, so "same index" across the two calendars never
    means "same calendar date." Comparing them would be economically
    meaningless, not just imprecise. The threshold itself is also
    asset-class-specific (see RiskLimits.max_correlation_threshold_crypto)."""
    candidate_keys = {asset_identity_key(sc.asset) for sc in ranked}
    selected_closes: dict[str, tuple[AssetClass, list[Decimal]]] = {}
    # FIN-090-01: unbounded, whole-table pagination -- NOT list_all(limit=1000),
    # which would silently drop a held asset from correlation demotion once
    # enough OTHER holdings existed first.
    holdings_rows = await paginate_all_rows(repositories.holdings)
    for row in holdings_rows:
        held_asset = hydrate("holdings", row["payload"]).asset
        key = asset_identity_key(held_asset)
        if key in candidate_keys:
            continue  # already have this cycle's own candle fetch for it, via a candidate sharing the same asset
        try:
            candles = await market_data.fetch_candles(held_asset)
            selected_closes[key] = (held_asset.asset_class, [c.close for c in candles])
        except ProviderError:
            continue  # no correlation signal available for this holding this cycle -- degrade gracefully, don't block ranking

    prioritized: list[_ScoredCandidate] = []
    penalized: list[_ScoredCandidate] = []
    for sc in ranked:
        own_closes = [float(c.close) for c in sc.candles]
        threshold = (
            risk_limits.max_correlation_threshold_crypto
            if sc.asset.asset_class == AssetClass.CRYPTO
            else risk_limits.max_correlation_threshold
        )
        correlations = (
            abs(v) for other_class, other_closes in selected_closes.values() if other_class == sc.asset.asset_class
            if (v := pearson_correlation(own_closes, [float(c) for c in other_closes])) is not None
        )
        max_corr = max(correlations, default=None)
        max_corr_decimal = Decimal(str(round(max_corr, 6))) if max_corr is not None else None
        penalize = max_corr is not None and max_corr >= threshold
        sc = replace(sc, max_correlation=max_corr_decimal, correlation_penalty_applied=penalize)
        if penalize:
            penalized.append(sc)
        else:
            prioritized.append(sc)
            selected_closes[asset_identity_key(sc.asset)] = (sc.asset.asset_class, [c.close for c in sc.candles])
    return prioritized + penalized


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
    # OBS-094-01: unbounded, not list_by_status(..., limit=50) -- this is
    # audit-record cleanup only (the `locks` table, not this row, is what
    # actually gates re-entry, see the docstring above), but a 50-row cap
    # meant repeated crashes leaving >50 stale RUNNING rows would gradually
    # (never fully) reclaim them across cycles rather than all at once.
    rows = await list_all_by_statuses(repositories.scan_runs, [ScanRunStatus.RUNNING.value])
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
    capabilities: MarketDataCapabilities | None = None,
) -> ScanCycleSummary:
    now = clock()
    strategy_weights = strategy_weights or default_strategy_weights(now)
    await _reclaim_stale_scan_runs(repositories, now)
    scan_run_id = str(uuid4())
    # UI-094-01: asset_class included -- the timestamp alone has only
    # one-second resolution, so parallel equity/crypto/options lanes
    # starting within the same second would otherwise share an identical
    # scan_generation. The dashboard's cross-lane funnel count
    # (frontend/src/useTradeLifecycleData.ts) is keyed purely by this
    # string, so a collision let a same-second fill from one lane inflate
    # another lane's displayed "Filled" count -- dashboard attribution
    # only, no effect on actual Opportunity/TradeIntent financial identity
    # (those are keyed by UUID, unaffected).
    scan_generation = f"{now.strftime('%Y%m%dT%H%M%SZ')}-{asset_class.value}"
    # Computed here (before session/AI calls) purely so universe_size can be
    # stamped on the ScanRun from its very first (RUNNING) persisted row --
    # a pure function of universe/asset_class, no decision logic moved.
    if asset_class == AssetClass.CRYPTO:
        lane_symbols = sorted(universe.crypto)
    elif asset_class == AssetClass.OPTION:
        lane_symbols = sorted(universe.options_underlyings)
    else:
        lane_symbols = sorted(universe.equities)
    scan_run = ScanRun(
        scan_run_id=scan_run_id, scan_generation=scan_generation, trigger=trigger, asset_class=asset_class,
        status=ScanRunStatus.RUNNING, started_at=now, lock_owner_token=str(uuid4()),
        market_data_tier=capabilities.tier_label if capabilities is not None else None,
        equity_feed=capabilities.equity_feed if capabilities is not None else None,
        option_feed=capabilities.option_feed if capabilities is not None else None,
        universe_size=len(lane_symbols),
    )
    await repositories.scan_runs.create_once(scan_run_id, scan_run, status=scan_run.status.value)

    async def _reject(symbol: str, reason: str, **context: Any) -> None:
        """Every candidate-filtering `continue` below routes through here
        first -- without this, a scan that approves zero candidates gives
        no clue which gate(s) it lost to. Relies on JsonFormatter forwarding
        every `extra=` field automatically (config/logging.py). A closure
        (not the former module-level function) so every rejection carries
        this cycle's lane without threading it through every call site.

        Also persists a RejectedCandidate row (durable counterpart to the
        log line, which only lives in whatever's currently capturing this
        process's stdout) so rejections can be reviewed after the fact --
        `context` is the exact same free-form diagnostic payload the log
        line already carries, just not lost the moment the process ends."""
        logger.info(
            "candidate_rejected",
            extra={"event": "candidate_rejected", "symbol": symbol, "reason": reason, "asset_class": asset_class.value, **context},
        )
        rejection = RejectedCandidate(
            rejection_id=str(uuid4()), scan_run_id=scan_run_id, scan_generation=scan_generation,
            symbol=symbol, asset_class=asset_class, reason=reason, occurred_at=clock(), context=context,
        )
        await repositories.rejected_candidates.create_once(rejection.rejection_id, rejection)

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

    # Regime benchmark fetch runs concurrently with the AI call -- the two
    # are independent, so this adds zero serial latency in the common
    # case. Started only after the session-block check above (no point
    # spending an API call on a cycle that's about to reject everything).
    regime_task = asyncio.create_task(_classify_lane_regime(market_data, asset_class))

    request = build_scan_request(str(uuid4()), scan_run_id, _build_scan_prompt(lane_symbols, asset_class))
    try:
        ai_response, candidates = await ai_provider.scan_candidates(request)
    except (ProviderHttpFailure, ProviderDataFailure) as exc:
        regime_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await regime_task
        await _finish(ScanRunStatus.FAILED, error=str(exc))
        return ScanCycleSummary(scan_run_id, ScanRunStatus.FAILED, 0, 0, 0, [], error=str(exc))

    lane_regime: _LaneRegime = await regime_task
    # Regime classification still feeds risk/engine.py's regime_multiplier
    # sizing gate below (that path IS empirically calibrated -- see
    # docs/regime-classifier-phase1-calibration.md) and is still persisted
    # for observability (regime/regime_confidence on ScanRun/Opportunity).
    #
    # It no longer conditions FACTOR WEIGHTS. Strategy Sophistication Phase
    # 1 originally wired regime_conditioned_weights() in here, but its
    # weight vectors (config/strategy_weights.py::_REGIME_WEIGHT_PROFILES)
    # were never validated against real trade outcomes -- unlike the sizing
    # multiplier, there's no historical-market-statistic proxy for "is 30%
    # momentum better than 20% in a bull regime," only a real backtest or
    # live outcome data could answer that, and neither exists yet. Reverted
    # to the fixed baseline composite (candidate scoring/ranking/capital
    # allocation must not depend on an unvalidated hypothesis) ahead of a
    # 60-day prove-edge baseline. regime_conditioned_weights() itself is
    # kept, tested, and importable for a future calibration pass -- just not
    # called from here until real evidence backs it.
    regime_label = str(lane_regime.snapshot.get("regime", "unavailable"))
    effective_weights = strategy_weights

    await repositories.ai_responses.create_once(ai_response.request_id, ai_response)

    try:
        account = await broker.get_account()
    except Exception as exc:  # noqa: BLE001 - a broker outage must fail this scan cleanly, not crash the caller
        await _finish(
            ScanRunStatus.FAILED, candidates_discovered=len(candidates), error=f"BROKER_UNAVAILABLE: {exc}",
            ai_response_request_id=ai_response.request_id, **_scan_run_regime_fields(lane_regime, effective_weights),
        )
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

    # Strategy Sophistication Phase 1 -- PASS 1: score and gate every
    # candidate (no execution, no locks, no writes beyond _reject logging).
    # Exactly today's pre-Phase-1 checks, in unchanged order, up through
    # the deterministic-signal gate, plus one new liquidity_crisis gate.
    # Deferred to pass 2 (unchanged in content/order/reject-reasons):
    # live quote fetch, options chain/contract selection, symbol lock/
    # in-flight checks, sizing, Opportunity creation, execution -- these
    # are execution-adjacent (a quote fetched here could go stale before a
    # lower-ranked candidate's turn; the gateway re-fetches its own
    # authoritative quote regardless) or naturally 1:1 with actually
    # attempting to spend the budget, so they shouldn't be paid for
    # candidates that never get a shot at it.
    scored: list[_ScoredCandidate] = []
    for candidate in candidates:
        if lease_lost is not None and lease_lost.is_set():
            await _reject(candidate.symbol, "COMMAND_LEASE_LOST")
            continue  # scan's own command lease may no longer be exclusive -- stop starting new work
        if candidate.recommendation not in _ACTIONABLE_RECOMMENDATIONS:
            await _reject(candidate.symbol, "NOT_ACTIONABLE_RECOMMENDATION", recommendation=candidate.recommendation)
            continue
        if candidate.confidence < risk_limits.min_confidence:
            await _reject(candidate.symbol, "CONFIDENCE_BELOW_MIN", confidence=candidate.confidence, min_confidence=str(risk_limits.min_confidence))
            continue
        if regime_label == "liquidity_crisis":
            # Signal-layer suppression -- explicit block, not a raised
            # composite threshold (more auditable than disguising
            # suppression as an unreachable bar). Belt-and-suspenders:
            # risk/engine.py's regime_multiplier=0 hard block (already
            # wired, untouched below) remains the actual sizing-layer
            # backstop even if this gate were ever bypassed or
            # misconfigured. Placed before any candle/quote fetch to avoid
            # wasted I/O during a crisis regime.
            await _reject(candidate.symbol, "LIQUIDITY_CRISIS_NEW_ENTRIES_SUPPRESSED", regime=regime_label)
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
                await _reject(candidate.symbol, "OUTSIDE_SCAN_LANE")
                continue
            if asset.symbol not in universe.options_underlyings:
                await _reject(candidate.symbol, "OUTSIDE_EXECUTABLE_UNIVERSE")
                continue
        else:
            if not is_executable(asset, universe):
                await _reject(candidate.symbol, "OUTSIDE_EXECUTABLE_UNIVERSE")
                continue
            if asset.asset_class != asset_class:
                # Defense in depth -- the prompt only ever offers this
                # lane's symbols, but AI output is an untrusted hint (same
                # principle as OUTSIDE_EXECUTABLE_UNIVERSE above), so
                # nothing actually forces it to honor that.
                await _reject(candidate.symbol, "OUTSIDE_SCAN_LANE")
                continue

        try:
            candles = await market_data.fetch_candles(asset)
        except ProviderError as exc:
            await _reject(candidate.symbol, "CANDLE_FETCH_FAILED", error=str(exc))
            continue  # insufficient candle history or a data-fetch failure -- fail closed, same as every other provider boundary here
        scores = compute_real_factors(candles, calendar=_BENCHMARK_CALENDAR[asset_class], benchmark_closes=lane_regime.benchmark_closes)
        if scores is None:
            await _reject(candidate.symbol, "INSUFFICIENT_FACTOR_DATA")
            continue
        composite = weighted_composite(scores, effective_weights)
        deterministic_signal = signal_from_composite(composite)
        if deterministic_signal not in _DETERMINISTIC_ACTIONABLE_SIGNALS:
            await _reject(
                candidate.symbol, "DETERMINISTIC_SIGNAL_DISAGREED", ai_recommendation=candidate.recommendation,
                deterministic_signal=deterministic_signal, composite_score=str(composite),
            )
            continue  # AI proposed it, but the deterministic technical/momentum/risk read disagrees, on the UNDERLYING's own technicals

        scored.append(_ScoredCandidate(candidate, asset, candles, scores, composite, deterministic_signal))

    # RANK: best composite first, deterministic tie-breaks (confidence,
    # then symbol) -- never incidental list/sort-stability order. This is
    # what makes ranking meaningful: the real cross-candidate capital
    # scarcity is evaluate_risk's fresh-broker-cash check inside
    # risk/engine.py (account.cash refetched on every execute_intent
    # below), now simply invoked in ranked-best-first order.
    ranked = sorted(scored, key=lambda s: (s.composite, Decimal(str(s.candidate.confidence)), s.candidate.symbol), reverse=True)
    ranked = await _correlation_adjusted_rank(repositories, market_data, ranked, risk_limits)

    # PASS 2: execute in ranked order. Everything below is today's
    # unchanged post-gate code, operating on the already-scored/fetched
    # values instead of re-deriving them.
    for rank, scored_candidate in enumerate(ranked, start=1):
        candidate, asset, candles, scores, composite, deterministic_signal = (
            scored_candidate.candidate, scored_candidate.asset, scored_candidate.candles,
            scored_candidate.scores, scored_candidate.composite, scored_candidate.deterministic_signal,
        )
        if lease_lost is not None and lease_lost.is_set():
            # Re-checked here (not just in pass 1) -- splitting scoring
            # from execution introduces a real time gap the old single-pass
            # loop didn't have to guard against (pass 1 may have taken
            # several awaited fetches, or pass 2 itself may be mid-way
            # through executing earlier-ranked candidates).
            await _reject(candidate.symbol, "COMMAND_LEASE_LOST")
            continue

        try:
            quote = await market_data.fetch_quote(asset)
        except ProviderError as exc:
            await _reject(candidate.symbol, "QUOTE_FETCH_FAILED", error=str(exc))
            continue  # one bad quote must not abort the rest of the scan

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
                await _reject(candidate.symbol, "OPTION_CHAIN_FETCH_FAILED", error=str(exc))
                continue
            contract = select_contract(
                "call", quote.price, chain, min_dte=risk_limits.options_expiry_min_days,
                max_dte=risk_limits.options_expiry_max_days, target_otm_pct=risk_limits.options_target_otm_pct, now=now.date(),
            )
            if contract is None:
                await _reject(candidate.symbol, "NO_ELIGIBLE_OPTION_CONTRACT")
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
                await _reject(candidate.symbol, "OPTION_QUOTE_FETCH_FAILED", error=str(exc))
                continue
        else:
            trade_asset = asset
            trade_quote = quote

        database = repositories.trade_intents.database
        owner_token = str(uuid4())
        if not await reserve_symbol_for_execution(database, trade_asset, owner_token):
            await _reject(candidate.symbol, "SYMBOL_EXECUTION_LOCKED")
            continue  # another coordinator is already processing this asset -- don't race it
        try:
            if await has_in_flight_intent(repositories, trade_asset):
                await _reject(candidate.symbol, "SYMBOL_HAS_IN_FLIGHT_INTENT")
                continue  # don't fight an order already in flight on this symbol (e.g. from the position monitor)

            multiplier = contract_multiplier_of(trade_asset)  # 1 for equity/crypto, ~100 for an options contract
            if notional_budget <= 0 or trade_quote.price <= 0:
                await _reject(candidate.symbol, "NO_NOTIONAL_BUDGET_OR_INVALID_PRICE", notional_budget=str(notional_budget), price=str(trade_quote.price))
                continue
            quantity = _round_quantity(notional_budget / (trade_quote.price * multiplier), trade_asset.asset_class)
            if quantity <= 0:
                await _reject(candidate.symbol, "QUANTITY_ROUNDED_TO_ZERO", notional_budget=str(notional_budget), price=str(trade_quote.price))
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
                    # Strategy Sophistication Phase 1 -- transparent factor
                    # breakdown and ranking provenance.
                    "liquidity_score": str(scores.liquidity_score), "risk_quality_score": str(scores.risk_quality_score),
                    "relative_strength_score": (
                        str(scores.relative_strength_score) if scores.relative_strength_score is not None else None
                    ),
                    "factor_breakdown": factor_breakdown(scores),
                    "regime_weight_profile": effective_weights.version,
                    "composite_rank": rank, "candidates_ranked_total": len(ranked),
                    # Portfolio Optimization -- sector (real, once
                    # sector_for_symbol resolves it -- see ExecutionRequest
                    # below) and correlation-aware ranking provenance.
                    "sector": sector_for_symbol(asset.symbol, asset.asset_class),
                    "max_correlation": str(scored_candidate.max_correlation) if scored_candidate.max_correlation is not None else None,
                    "correlation_penalty_applied": scored_candidate.correlation_penalty_applied,
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
                regime_multiplier=lane_regime.multiplier, regime_snapshot=lane_regime.snapshot,
                # Portfolio Optimization -- always the UNDERLYING's sector
                # (asset, not trade_asset -- for options, trade_asset is the
                # OCC contract symbol, never in the sector map), so options
                # correctly inherit their underlying's sector. Feeds
                # risk/engine.py's existing max_sector_pct cap, which has
                # always been wired correctly but never received a real value.
                sector=sector_for_symbol(asset.symbol, asset.asset_class),
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
        candidates_approved=approved, orders_submitted=submitted, ai_response_request_id=ai_response.request_id,
        **_scan_run_regime_fields(lane_regime, effective_weights),
    )
    return ScanCycleSummary(scan_run_id, ScanRunStatus.COMPLETED, len(candidates), approved, submitted, execution_results)
