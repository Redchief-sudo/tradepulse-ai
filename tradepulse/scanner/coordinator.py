"""The scan cycle: AI-driven candidate discovery wired into Opportunity/
TradeIntent construction and the execution gateway.

Division of responsibility (matches the discovery-only contract documented
on AnthropicAIProvider): the AI proposes SYMBOLS and a recommendation/
confidence bucket only. It never supplies a price, a quantity, a stop-loss,
or a target -- this module fetches its own reference quote per candidate
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

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import ROUND_FLOOR, Decimal
from typing import Any
from uuid import uuid4

from tradepulse.broker import AlpacaClient
from tradepulse.execution import ExecutionGateway, ExecutionRequest, ExecutionResult
from tradepulse.models import (
    AssetClass,
    AssetIdentity,
    Opportunity,
    RiskLimits,
    ScanRun,
    ScanRunStatus,
    ScanTrigger,
    SessionState,
    Side,
)
from tradepulse.persistence import PersistenceRepositories
from tradepulse.providers import (
    AlpacaMarketDataProvider,
    AnthropicAIProvider,
    OpportunityCandidate,
    ProviderDataFailure,
    ProviderError,
    ProviderHttpFailure,
    build_scan_request,
)
from tradepulse.risk import load_session
from tradepulse.strategy import ExecutableUniverse, is_executable

_ACTIONABLE_RECOMMENDATIONS = frozenset({"STRONG_BUY", "BUY"})


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
    precision = Decimal("100000000") if asset_class == AssetClass.CRYPTO else Decimal("1000")
    floored = (max(qty, Decimal("0")) * precision).to_integral_value(rounding=ROUND_FLOOR)
    return floored / precision


def _build_scan_prompt(universe: ExecutableUniverse) -> str:
    symbols = sorted(universe.equities | universe.crypto)
    return (
        "You are a market-scanning analyst for an automated trading system. "
        "Review the following tradeable symbols and report any that are worth "
        "considering for a new long position today. For each symbol you report, give "
        "a recommendation (STRONG_BUY, BUY, HOLD, SELL, or STRONG_SELL), a confidence "
        "score from 0-100, and a one-sentence summary of your reasoning. Only report "
        "symbols from this exact list. It is fine to report zero candidates if nothing "
        "stands out.\n\nSymbols: " + ", ".join(symbols)
    )


def _asset_from_candidate(candidate: OpportunityCandidate) -> AssetIdentity:
    asset_class = AssetClass.CRYPTO if "/" in candidate.symbol else AssetClass.EQUITY
    return AssetIdentity(symbol=candidate.symbol, asset_class=asset_class, native_asset_id=f"alpaca:{candidate.symbol}")


async def run_scan_cycle(
    repositories: PersistenceRepositories,
    ai_provider: AnthropicAIProvider,
    market_data: AlpacaMarketDataProvider,
    broker: AlpacaClient,
    gateway: ExecutionGateway,
    universe: ExecutableUniverse,
    risk_limits: RiskLimits,
    *,
    trigger: ScanTrigger = ScanTrigger.SCHEDULED,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> ScanCycleSummary:
    now = clock()
    scan_run_id = str(uuid4())
    scan_generation = now.strftime("%Y%m%dT%H%M%SZ")
    scan_run = ScanRun(
        scan_run_id=scan_run_id, scan_generation=scan_generation, trigger=trigger,
        status=ScanRunStatus.RUNNING, started_at=now, lock_owner_token=str(uuid4()),
    )
    await repositories.scan_runs.create_once(scan_run_id, scan_run, status=scan_run.status.value)

    async def _finish(status: ScanRunStatus, **fields: Any) -> None:
        finished = replace(scan_run, status=status, completed_at=clock(), **fields)
        await repositories.scan_runs.update(scan_run_id, finished, status=finished.status.value)

    session = await load_session(repositories)
    hard_blocked_states = (SessionState.FINANCIAL_INTEGRITY_BLOCKED, SessionState.RISK_STOPPED)
    if session.state in hard_blocked_states or session.kill_switch_reset_required or session.financial_integrity_manual_reenable_required:
        await _finish(ScanRunStatus.FAILED, error="SESSION_BLOCKED")
        return ScanCycleSummary(scan_run_id, ScanRunStatus.FAILED, 0, 0, 0, [], error="SESSION_BLOCKED")

    request = build_scan_request(str(uuid4()), scan_run_id, _build_scan_prompt(universe))
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

    notional_budget = (risk_limits.max_position_pct / 100) * account.equity

    execution_results: list[ExecutionResult] = []
    approved = 0
    submitted = 0
    for candidate in candidates:
        if candidate.recommendation not in _ACTIONABLE_RECOMMENDATIONS:
            continue
        if candidate.confidence < risk_limits.min_confidence:
            continue
        asset = _asset_from_candidate(candidate)
        if not is_executable(asset, universe):
            continue

        try:
            quote = await market_data.fetch_quote(asset)
        except ProviderError:
            continue  # one bad quote must not abort the rest of the scan

        if notional_budget <= 0 or quote.price <= 0:
            continue
        quantity = _round_quantity(notional_budget / quote.price, asset.asset_class)
        if quantity <= 0:
            continue

        opportunity = Opportunity(
            opportunity_id=str(uuid4()), scan_generation=scan_generation, asset=asset, quote=quote,
            source="anthropic_ai", created_at=clock(), confidence=candidate.confidence,
            metadata={
                "recommendation": candidate.recommendation, "summary": candidate.summary,
                "ai_request_id": ai_response.request_id,
            },
        )
        await repositories.opportunities.create_once(opportunity.opportunity_id, opportunity)

        approved += 1
        exec_request = ExecutionRequest(
            asset=asset, side=Side.BUY, requested_quantity=quantity, strategy="ai_scan",
            decision_id=opportunity.opportunity_id, confidence=Decimal(str(candidate.confidence)),
        )
        result = await gateway.execute_intent(exec_request)
        execution_results.append(result)
        if result.status not in ("rejected", "skipped"):
            submitted += 1

    await _finish(
        ScanRunStatus.COMPLETED, candidates_discovered=len(candidates),
        candidates_approved=approved, orders_submitted=submitted,
    )
    return ScanCycleSummary(scan_run_id, ScanRunStatus.COMPLETED, len(candidates), approved, submitted, execution_results)
