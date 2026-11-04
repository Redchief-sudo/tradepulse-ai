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
import logging
import time
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import uuid4

import httpx

from tradepulse.alerts import TelegramAlerter, alert_once_per_window
from tradepulse.broker import (
    AlpacaDataIntegrityError,
    AlpacaError,
    AlpacaOrderRequest,
    AlpacaPosition,
    BrokerPort,
    default_time_in_force,
    is_definitive_rejection,
)
from tradepulse.broker.capabilities import (
    AccountEvidence,
    AssetTradingCapabilities,
    DbaeCapabilities,
    evidence_fresh,
)
from tradepulse.broker.port import BrokerClock
from tradepulse.broker.symbols import broker_symbol_asset_key
from tradepulse.calendar import MarketCalendar
from tradepulse.calendar.checks import CalendarGate, EntryWindow
from tradepulse.dbae.bands import Band, unlocks
from tradepulse.dbae.baseline import (
    BASELINE_REASON,
    BaselineRefusal,
    SessionBaseline,
    band_equity,
    load_or_capture_baseline,
    session_date_for,
)
from tradepulse.dbae.funding import (
    PendingFunding,
    buy_fee_pct,
    fundable_notional,
    funding_available,
    funding_for,
    pending_buy_funding,
)
from tradepulse.dbae.policy import (
    EntryProposal,
    ExecutionPolicy,
    core_account_reasons,
    evaluate_entry_policy,
    exit_feasible,
    shape_quantity,
)
from tradepulse.models import (
    AssetClass,
    AssetIdentity,
    AuditEvent,
    ExecutionMode,
    PortfolioSnapshot,
    RiskLimits,
    SettlementStatus,
    Side,
    TradeIntent,
    TradeIntentStatus,
    asset_identity_key,
    asset_key_from_broker_symbol,
    contract_multiplier_of,
    is_continuous_market,
)
from tradepulse.persistence import (
    AsyncSQLiteDatabase,
    DatabaseError,
    PersistenceRepositories,
    RepositoryLeaseLostError,
    RepositoryPreconditionError,
    acquire_lock,
    hydrate,
    list_all_by_asset,
    list_all_by_json_field,
    paginate_all_rows,
    release_lock,
    renew_lock,
)
from tradepulse.providers import AlpacaMarketDataProvider, ProviderError
from tradepulse.risk import (
    RiskCheckInput,
    RiskDecision,
    RiskEvalOptions,
    build_portfolio_snapshot,
    check_max_drawdown,
    evaluate_risk,
    execution_session_decision,
    latch_risk_stop,
    load_session,
    open_portfolio_risk,
    symbol_entries_today,
)
from tradepulse.runtime_lease import AccountLease, AccountLeaseUnavailable
from tradepulse.settlement import SettlementProcessor
from tradepulse.time import aware_utc

from .fill_attribution import (
    TERMINAL_ORDER_STATUSES,
    TERMINAL_STATUSES,
    AttributedFills,
    attribute_order_fills,
    order_matches_intent,
    terminal_status_for_order,
)
from .idempotency import (
    IN_FLIGHT_STATUSES,
    PORTFOLIO_RISK_LOCK_KEY,
    PORTFOLIO_RISK_LOCK_TTL_SECONDS,
    SYMBOL_LOCK_TTL_SECONDS,
    derive_idempotency_key,
    execution_lock_key,
)
from .quotes import AuthoritativeQuote, fetch_authoritative_quote

logger = logging.getLogger(__name__)

# dbae_band_changed audit ids: this prefix, then a 12-digit sequence (M3).
BAND_CHANGE_ID_PREFIX = "dbae_band_changed:"


@asynccontextmanager
async def _portfolio_risk_lock(database: AsyncSQLiteDatabase, needed: bool) -> AsyncIterator[tuple[bool, tuple[str, str] | None]]:
    """Serializes the fetch-account/build-snapshot/evaluate_risk/persist-
    RISK_APPROVED segment of execute_intent across DIFFERENT symbols -- two
    concurrent calls for different symbols would otherwise both read the
    same starting exposure and both approve capital against it, since
    Alpaca's own buying-power check knows nothing about TradePulse's own
    max_total_exposure_pct/max_sector_pct/max_simultaneous_orders. Held only
    around that fast, local-only decision window -- never through broker
    submission/fill polling, which can take up to FILL_TIMEOUT_SECONDS.
    Non-blocking (fails fast if already held, same semantics as
    reserve_symbol_for_execution): a losing concurrent call gets `False` and
    must skip this candidate for now rather than wait, since there's no
    forward-progress benefit to blocking on a decision window this short.

    Yields (acquired, lease). The window makes broker calls, so it can
    outlive the TTL; the RISK_APPROVED write is fenced on ``lease`` so a
    holder whose lease lapsed never commits a decision made against a
    snapshot another caller may already have spent."""
    if not needed:
        yield True, None
        return
    owner_token = str(uuid4())
    acquired = await acquire_lock(database, PORTFOLIO_RISK_LOCK_KEY, owner_token, "execute_intent", PORTFOLIO_RISK_LOCK_TTL_SECONDS)
    try:
        yield acquired, ((PORTFOLIO_RISK_LOCK_KEY, owner_token) if acquired else None)
    finally:
        if acquired:
            await release_lock(database, PORTFOLIO_RISK_LOCK_KEY, owner_token)


@dataclass(frozen=True, slots=True)
class ExecutionRequest:
    asset: AssetIdentity
    side: Side
    requested_quantity: Decimal
    strategy: str
    decision_id: str | None = None
    # Why an exit was placed (monitor/coordinator.py: stop_loss,
    # trailing_stop, break_even_stop, target_price, option_expiry, time_stop,
    # scale_out, target_r, target_r_single_unit, stall). Recorded on the
    # closing intent's risk_snapshot as the authoritative exit reason;
    # attribution's after-the-fact inference cannot see ratcheted stops or
    # the exit ladder.
    exit_reason: str | None = None
    signal_timestamp: str | None = None
    confidence: Decimal | None = None
    stop_loss: Decimal | None = None
    target_price: Decimal | None = None
    sector: str | None = None
    order_type: Literal["market", "limit", "stop", "stop_limit"] = "market"
    idempotency_key: str | None = None
    # If the caller (scanner/monitor) holds a per-asset execution
    # reservation (see execution/idempotency.py), passing its owner_token
    # here lets execute_intent re-verify -- right before the irreversible
    # broker submission -- that the reservation is still valid. None (the
    # default) means the caller isn't participating in the scheme; no new
    # behavior for it.
    symbol_lock_owner_token: str | None = None
    # Market Regime Phase 2 -- kept deliberately dumb so this module never
    # depends on strategy.regime (matches risk/engine.py's own
    # "strategy-independent" principle, one layer up): a bare Decimal
    # threaded straight into RiskCheckInput.regime_multiplier unchanged,
    # and a plain, gateway-agnostic dict merged into risk_snapshot
    # verbatim -- this module never interprets what's inside either.
    regime_multiplier: Decimal | None = None
    regime_snapshot: Mapping[str, str | int | None] | None = None


ExecutionStatus = Literal["filled", "partially_filled", "rejected", "skipped", "pending"]


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    status: ExecutionStatus
    trade_intent_id: str | None
    reasons: list[str]
    filled_quantity: Decimal
    filled_avg_price: Decimal | None


DbaeSnapshotValue = str | bool | list[str] | None


@dataclass(frozen=True, slots=True)
class _EntryEvidence:
    """DBAE evidence read for one new buy (spec §3.3). None: the read failed,
    which is unknown evidence and never permission (§10)."""

    account: AccountEvidence | None
    asset: AssetTradingCapabilities | None


@dataclass(frozen=True, slots=True)
class _AuthorizedEntry:
    """Spec §8.1 steps 1-4 passed: what steps 6-9 and the record need."""

    policy: ExecutionPolicy
    proposal: EntryProposal
    account: AccountEvidence
    asset: AssetTradingCapabilities
    baseline: SessionBaseline
    band_equity: Decimal
    pending: PendingFunding
    funding_available: Decimal
    decided_at: datetime


class ExecutionGateway:
    # Seconds, real time; a float so a caller may set a sub-second budget.
    FILL_TIMEOUT_SECONDS: float = 20
    POLL_INTERVAL_SECONDS: float = 1

    def __init__(
        self,
        repositories: PersistenceRepositories,
        broker: BrokerPort,
        market_data: AlpacaMarketDataProvider,
        settlement: SettlementProcessor,
        alerts: TelegramAlerter,
        risk_limits: RiskLimits,
        execution_mode: ExecutionMode,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        *,
        lease: AccountLease,
        capabilities: DbaeCapabilities,
        calendar: MarketCalendar | None = None,
    ) -> None:
        # Every submission is fenced by the account lease (runtime_lease.py);
        # a gateway can never exist without one, nor with one for another venue.
        if lease.ref.venue != broker.venue:
            raise ValueError(f"account lease is for venue {lease.ref.venue!r}, the broker is {broker.venue!r}")
        self._lease = lease
        # DBAE evidence (spec §3): read fresh inside the portfolio-risk lock
        # for every new buy; never read for a protective exit.
        self._capabilities = capabilities
        self._calendar = calendar  # None: the pinned process calendar for the current NY date (Rev.144)
        self._repositories = repositories
        self._broker = broker
        self._market_data = market_data
        self._settlement = settlement
        self._alerts = alerts
        self._risk_limits = risk_limits
        self._execution_mode = execution_mode
        self._clock = clock

    @property
    def alerts(self) -> TelegramAlerter:
        return self._alerts

    @property
    def capabilities(self) -> DbaeCapabilities:
        return self._capabilities

    @property
    def account_ref(self) -> str:
        return self._lease.ref.key

    def _calendar_gate(self, now: datetime) -> CalendarGate:
        from tradepulse.calendar import market_calendar, ny_date

        calendar = self._calendar or market_calendar(ny_date(now))
        return CalendarGate(self._repositories, calendar, (self._broker.venue,))

    async def _run_daily_checks_if_due(self, gate: CalendarGate, now: datetime) -> bool:
        """CAL-I1: the daily check, when none has completed today for every
        checked venue. True if it ran."""
        from tradepulse.calendar.checks import CHECKED_VENUES, checked_today, run_daily_checks

        if all([await checked_today(self._repositories, self._broker.venue, venue, now) for venue in CHECKED_VENUES]):
            return False
        await run_daily_checks(self._repositories, self._broker, self._broker.venue, gate.calendar, self._alerts, now)
        return True

    async def _entry_window(self, asset: AssetIdentity, market_clock: BrokerClock) -> EntryWindow:
        """CAL-I1. With no completed check for today yet (a one-shot command,
        or the first entry of a new NY date), the daily check runs here
        first, so an entry is never authorized by stale or absent evidence."""
        from tradepulse.calendar.checks import entry_window_open

        now = self._clock()
        gate = self._calendar_gate(now)
        window = await entry_window_open(gate, self._broker.venue, asset, now, market_clock)
        if window.reason == "CALENDAR_UNVERIFIED" and await self._run_daily_checks_if_due(gate, now):
            window = await entry_window_open(gate, self._broker.venue, asset, now, market_clock)
        return window

    async def _latch_kill_switch(self, risk: RiskDecision) -> None:
        """A genuine account-level kill-switch condition -- not an ordinary
        per-trade sizing/eligibility rejection (insufficient cash, confidence
        too low, sector/position caps, etc.) -- must latch the durable session
        state reset-risk exists to clear, not just reject this one order."""
        kill_switch_reason = next(
            (r for r in risk.reasons if r.startswith("MAX_DAILY_LOSS_EXCEEDED") or r == "MAX_DRAWDOWN_BREACHED"), None
        )
        if kill_switch_reason is not None:
            await latch_risk_stop(self._repositories, kill_switch_reason, clock=self._clock)

    async def _read_entry_evidence(self, asset: AssetIdentity) -> _EntryEvidence:
        """Spec §3.3: fresh account and asset evidence for a new buy. A failed
        read (timeout, HTTP failure, malformed data) is unknown evidence."""
        account: AccountEvidence | None
        try:
            account = await self._capabilities.get_account_evidence()
        except (AlpacaError, AlpacaDataIntegrityError, httpx.HTTPError, ValueError):
            account = None
        capabilities: AssetTradingCapabilities | None
        try:
            capabilities = await self._capabilities.get_asset_capabilities(asset)
        except (AlpacaError, AlpacaDataIntegrityError, httpx.HTTPError, ValueError):
            capabilities = None
        return _EntryEvidence(account, capabilities)

    async def _baseline_capture_due(self, gate: CalendarGate, now: datetime) -> bool:
        """Whether the current session's baseline still has to be captured,
        which needs a verified calendar date (§4.3). False once it is stored,
        and for a date the calendar cannot place, which
        load_or_capture_baseline refuses without any calendar check."""
        from tradepulse.calendar import CalendarHorizonExceeded

        try:
            session_date = session_date_for(gate.calendar, now)
        except (CalendarHorizonExceeded, ValueError):
            return False
        return await self._repositories.dbae_session_baselines.get(session_date.isoformat()) is None

    def _proposal(self, request: ExecutionRequest, quote: AuthoritativeQuote, sizing_stop: Decimal | None,
                  now: datetime) -> EntryProposal:
        """The entry as DBAE judges it. The stop is the one the entry is sized
        against, or the profile's stop_loss_pct below the price when there is
        no usable one, so exit feasibility is never skipped."""
        if sizing_stop is not None and 0 < sizing_stop < quote.price:
            stop_price = sizing_stop
        else:
            stop_price = quote.price * (1 - self._risk_limits.stop_loss_pct / 100)
        environment: Literal["alpaca-paper", "alpaca-live"] = (
            "alpaca-paper" if self._execution_mode == ExecutionMode.PAPER else "alpaca-live")
        return EntryProposal(request.asset, quote.price, stop_price, environment, now)

    def _evidence_still_fresh(self, entry: _AuthorizedEntry, trade_intent_id: str) -> Callable[[], Sequence[str]]:
        """DBAE-146-02: the RISK_APPROVED write's precondition, run inside its
        fenced transaction. It reads the gateway clock there and re-judges
        the account and asset evidence this entry was authorized on. Stale
        account evidence gives DBAE_ACCOUNT_STATE_UNVERIFIED; stale asset
        evidence is all fields unknown (record ruling 3), so the policy is
        re-run at that instant and its reasons (DBAE_ASSET_NOT_TRADABLE, and
        any other field's) are returned. Freshness is never extended: the
        evidence is not re-read.

        Re-review M1: an error while judging (the clock, or the policy re-run)
        means freshness at commit is unverified. It is logged and refused as
        DBAE_ACCOUNT_STATE_UNVERIFIED, so the transaction rolls back and the
        intent is closed unsubmitted with the refusal recorded. The second
        reason, DBAE_FRESHNESS_CHECK_ERROR, separates a fault in the check
        from evidence that is genuinely stale, for monitoring (Rev.146
        remediation review)."""
        def check() -> Sequence[str]:
            try:
                return judge()
            except Exception:
                logger.exception("DBAE_FRESHNESS_CHECK_ERROR: evidence freshness for %s could not be judged at "
                                 "commit; refusing", trade_intent_id)
                return ["DBAE_ACCOUNT_STATE_UNVERIFIED", "DBAE_FRESHNESS_CHECK_ERROR"]

        def judge() -> Sequence[str]:
            now = aware_utc(self._clock(), field_name="now")
            if not evidence_fresh(entry.account.received_at, now):
                return ["DBAE_ACCOUNT_STATE_UNVERIFIED"]
            if evidence_fresh(entry.asset.received_at, now):
                return []
            policy = evaluate_entry_policy(entry.account, entry.band_equity, entry.asset,
                                           replace(entry.proposal, now=now))
            # Stale asset evidence reads every field as None, so the policy
            # always refuses it, at least with DBAE_ASSET_NOT_TRADABLE.
            return list(policy.reasons) or ["DBAE_ASSET_NOT_TRADABLE"]

        return check

    async def _authorize_entry(self, request: ExecutionRequest, intent: TradeIntent, evidence: _EntryEvidence,
                               last_equity: Decimal, quote: AuthoritativeQuote,
                               sizing_stop: Decimal | None) -> _AuthorizedEntry | ExecutionResult:
        """Spec §8.1 steps 1-4, under the portfolio-risk lock: account
        evidence, the session baseline and band, asset eligibility, funding.
        The first failure refuses the entry with every reason of its step."""
        now = aware_utc(self._clock(), field_name="now")
        proposal = self._proposal(request, quote, sizing_stop, now)
        account = evidence.account
        if account is None:
            return await self._refuse_entry(intent, ["DBAE_ACCOUNT_STATE_UNVERIFIED"], evidence, None, None, now)
        asset = evidence.asset or AssetTradingCapabilities(None, None, None, None, None, None, now)
        # Stale or unusable account evidence is refused before the baseline
        # step reads history or writes anything.
        core = core_account_reasons(account, now)
        if core:
            return await self._refuse_entry(intent, core, evidence, None, None, now)

        gate = self._calendar_gate(now)
        baseline: SessionBaseline | BaselineRefusal
        # A store that cannot be read is unknown evidence (§10): the baseline
        # is unverified and the entry is refused, never raised.
        try:
            if await self._baseline_capture_due(gate, now):
                # Once the session's baseline is stored, no broker calendar call is made here.
                await self._run_daily_checks_if_due(gate, now)
            baseline = await load_or_capture_baseline(self._repositories, gate, self._capabilities, last_equity, now)
        except DatabaseError:
            baseline = BaselineRefusal(reason=BASELINE_REASON, check="baseline_store_unavailable")
        equity_for_band = band_equity(account.equity, baseline)

        policy = evaluate_entry_policy(account, equity_for_band, asset, proposal)
        if (equity_for_band is not None and account.equity is not None and isinstance(baseline, SessionBaseline)
                and evidence_fresh(account.received_at, now)):
            await self._record_band(intent, policy.band, equity_for_band, account.equity, baseline, now)
        if not policy.allowed:
            return await self._refuse_entry(intent, list(policy.reasons), evidence, policy.band, equity_for_band, now)
        if equity_for_band is None or isinstance(baseline, BaselineRefusal):
            # evaluate_entry_policy already refuses an unknown band equity; this only narrows the types.
            return await self._refuse_entry(intent, [BASELINE_REASON], evidence, policy.band, equity_for_band, now)

        reasons: list[str] = []
        try:
            pending = await pending_buy_funding(self._repositories, self._risk_limits)
        # PENDING_INTENT_QUANTITY_MISSING, a pending buy with no dollar notional,
        # or a store that cannot be read: P is unknown, so funding is unverified (§10).
        except (ValueError, DatabaseError):
            return await self._refuse_entry(intent, ["DBAE_FUNDING_UNVERIFIED"], evidence, policy.band,
                                            equity_for_band, now)
        for mismatched in pending.mismatches:
            await self._alert_quantity_identity_mismatch(mismatched, now)
        if pending.mismatches:
            reasons.append("DBAE_QUANTITY_IDENTITY_MISMATCH")
        available = None if pending.unpriced else funding_available(account, request.asset.asset_class, pending.total)
        if available is None:
            reasons.append("DBAE_FUNDING_UNVERIFIED")
        if reasons or available is None:
            return await self._refuse_entry(intent, reasons, evidence, policy.band, equity_for_band, now)
        return _AuthorizedEntry(policy, proposal, account, asset, baseline, equity_for_band, pending, available, now)

    async def _shape_entry(self, entry: _AuthorizedEntry, request: ExecutionRequest, intent: TradeIntent,
                           risk: RiskDecision, risk_input: RiskCheckInput, snapshot: PortfolioSnapshot,
                           risk_opts: RiskEvalOptions,
                           evidence: _EntryEvidence) -> tuple[Decimal, dict[str, DbaeSnapshotValue]] | ExecutionResult:
        """Spec §8.1 steps 6-8 on the first run's approved quantity: round
        down (§8.2), re-approve a changed quantity on the same quote,
        snapshot and options (§7.1), and check the protective exit (§8.3).
        Returns the submitted quantity and the risk_snapshot.dbae record."""
        async def refuse(reason: str) -> ExecutionResult:
            return await self._refuse_entry(intent, [reason], evidence, entry.policy.band, entry.band_equity,
                                            entry.decided_at)

        asset_class = request.asset.asset_class
        shaped = shape_quantity(entry.policy, entry.asset, asset_class, risk.approved_quantity)
        if shaped.refusal is not None:
            return await refuse(shaped.refusal)
        reapproved = True
        if shaped.quantity != risk.approved_quantity:
            again = evaluate_risk(replace(risk_input, requested_quantity=shaped.quantity), snapshot, self._risk_limits,
                                  risk_opts)
            reapproved = again.approved and again.approved_quantity == shaped.quantity
        if not reapproved:
            return await refuse("DBAE_SHAPED_QUANTITY_NOT_REAPPROVED")
        exit_refusal = exit_feasible(entry.policy, entry.asset, entry.proposal, shaped.quantity)
        if exit_refusal is not None:
            return await refuse(exit_refusal)

        account = entry.account
        power_name, power = (("non_marginable_buying_power", account.non_marginable_buying_power)
                             if asset_class == AssetClass.CRYPTO else ("buying_power", account.buying_power))
        # DBAE-146-01: max_funding is frozen with every input it is computed
        # from, so pending_buy_funding can recompute it exactly from the record.
        reference_price = entry.proposal.price
        multiplier = contract_multiplier_of(request.asset)
        slippage_pct = self._risk_limits.slippage_limit_for(asset_class)
        fee_pct = buy_fee_pct(asset_class)
        committed = funding_for(shaped.quantity, reference_price, multiplier, slippage_pct, fee_pct)
        session_date = entry.baseline.session_date.isoformat()
        record: dict[str, DbaeSnapshotValue] = {
            "original_requested_quantity": str(request.requested_quantity),
            "risk_approved_quantity": str(risk.approved_quantity),
            "shaped_quantity": str(shaped.quantity),
            "submitted_quantity": str(shaped.quantity),
            "band": entry.policy.band.value,
            "band_equity": str(entry.band_equity),
            "session_date": session_date,
            "baseline_id": session_date,  # dbae_session_baselines is keyed by session date
            "funding_available": str(entry.funding_available),
            "pending_buy_funding": str(entry.pending.total),
            "max_funding": str(committed),
            "reference_price": str(reference_price),
            "contract_multiplier": str(multiplier),
            "slippage_pct": str(slippage_pct),
            "fee_pct": str(fee_pct),
            "broker_funding_figures": [f"cash={account.cash}", f"{power_name}={power}",
                                       f"received_at={account.received_at.isoformat()}"],
            "shaping_rule": shaped.rule,
            "reapproved": reapproved,
            "exit_feasibility": "feasible",
            "evidence_received_at": [f"account={account.received_at.isoformat()}",
                                     f"asset={entry.asset.received_at.isoformat()}"],
        }
        return shaped.quantity, record

    async def _refuse_entry(self, intent: TradeIntent, reasons: list[str], evidence: _EntryEvidence, band: Band | None,
                            equity_for_band: Decimal | None, now: datetime) -> ExecutionResult:
        """Spec §10: the reasons, joined in check order, are the intent's
        rejection_reason, and ``dbae_entry_refused`` records them."""
        rejected = replace(intent, status=TradeIntentStatus.REJECTED, rejection_reason="; ".join(reasons))
        await self._repositories.trade_intents.update(intent.trade_intent_id, rejected, status=rejected.status.value)

        def age(received_at: datetime | None) -> str | None:
            return None if received_at is None else str((now - received_at).total_seconds())

        event_id = f"dbae_entry_refused:{intent.trade_intent_id}"
        await self._repositories.audit_events.create_once(event_id, AuditEvent(
            event_id=event_id, event_type="dbae_entry_refused", severity="warning",
            message=f"DBAE refused a {intent.asset.symbol} entry: {'; '.join(reasons)}", occurred_at=now,
            correlation_id=intent.correlation_id, entity_type="trade_intent", entity_id=intent.trade_intent_id,
            details={"reasons": reasons, "band": None if band is None else band.value,
                     "band_equity": None if equity_for_band is None else str(equity_for_band),
                     "asset_class": intent.asset.asset_class.value, "symbol": intent.asset.symbol,
                     "account_evidence_age_seconds": age(evidence.account.received_at if evidence.account else None),
                     "asset_evidence_age_seconds": age(evidence.asset.received_at if evidence.asset else None)}))
        return ExecutionResult("rejected", intent.trade_intent_id, reasons, Decimal(0), None)

    async def _record_band(self, intent: TradeIntent, band: Band, equity_for_band: Decimal, current_equity: Decimal,
                           baseline: SessionBaseline, now: datetime) -> None:
        """Spec §4.4: ``dbae_band_changed`` when this entry's band differs
        from the band of the latest such event (none yet: the first entry).
        Audit only, not a decision input: a store failure is logged and the
        entry goes on. Event ids carry a zero-padded sequence, so the latest
        is one primary-key read (M3) and two writers racing for the same
        sequence collide on create_once instead of both appending."""
        try:
            await self._write_band_change(intent, band, equity_for_band, current_equity, baseline, now)
        except DatabaseError:
            logger.exception("DBAE: dbae_band_changed not recorded for %s", intent.trade_intent_id)

    async def _write_band_change(self, intent: TradeIntent, band: Band, equity_for_band: Decimal,
                                 current_equity: Decimal, baseline: SessionBaseline, now: datetime) -> None:
        latest = await self._repositories.audit_events.last_by_id_prefix(BAND_CHANGE_ID_PREFIX)
        previous = latest["payload"]["details"]["new_band"] if latest else None
        if previous == band.value:
            return
        sequence = 1 if latest is None else int(latest["record_id"].removeprefix(BAND_CHANGE_ID_PREFIX)) + 1
        band_unlocks = unlocks(band)
        unlocked = [asset_class.value for asset_class, on in (
            (AssetClass.EQUITY, band_unlocks.equity != "none"), (AssetClass.CRYPTO, band_unlocks.crypto),
            (AssetClass.OPTION, band_unlocks.options)) if on]
        event_id = f"{BAND_CHANGE_ID_PREFIX}{sequence:012d}"
        await self._repositories.audit_events.create_once(event_id, AuditEvent(
            event_id=event_id, event_type="dbae_band_changed", severity="info",
            message=f"DBAE band {previous or 'none'} -> {band.value} at band equity {equity_for_band}.",
            occurred_at=now, correlation_id=intent.correlation_id, entity_type="dbae_band",
            entity_id=baseline.session_date.isoformat(),
            details={"old_band": previous, "new_band": band.value, "band_equity": str(equity_for_band),
                     "current_equity": str(current_equity),
                     "previous_close_equity": str(baseline.previous_close_equity),
                     "session_date": baseline.session_date.isoformat(), "unlocked_classes": unlocked,
                     "trade_intent_id": intent.trade_intent_id, "sequence": sequence}))

    async def _alert_quantity_identity_mismatch(self, trade_intent_id: str, now: datetime) -> None:
        """Spec §7.1: critical, once per intent. The entry is refused with
        DBAE_QUANTITY_IDENTITY_MISMATCH whether or not this is recorded, so a
        store failure is logged and retried on the next entry."""
        from tradepulse.alerts.once import alert_once

        event = AuditEvent(
            event_id=f"dbae_quantity_identity_mismatch:{trade_intent_id}",
            event_type="dbae_quantity_identity_mismatch", severity="critical",
            message=(f"DBAE_QUANTITY_IDENTITY_MISMATCH: pending buy {trade_intent_id} has a requested_quantity that "
                     "differs from its risk_snapshot.dbae.submitted_quantity. Funding reserves the larger; new entries "
                     "are refused until an operator resolves it."),
            occurred_at=now, entity_type="trade_intent", entity_id=trade_intent_id,
            details={"trade_intent_id": trade_intent_id})
        try:
            await alert_once(self._repositories.audit_events, self._alerts, event)
        except DatabaseError:
            logger.exception("DBAE: identity-mismatch alert for %s not recorded", trade_intent_id)

    async def execute_intent(self, request: ExecutionRequest) -> ExecutionResult:
        now = self._clock()
        idempotency_key = request.idempotency_key or derive_idempotency_key(
            request.strategy, request.decision_id, request.signal_timestamp, request.asset, request.side
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

        # Rev.140 §3.1, §3.1b: a sell, or any order on an asset holding
        # quarantined inventory, is authorized under the caller's execution
        # reservation by the conservative two-bound sellable quantity. Every
        # refusal here comes before any intent row is written.
        from tradepulse.quarantine import quarantined_positions

        quarantined = await quarantined_positions(self._repositories, asset_identity_key(request.asset))
        if request.side == Side.SELL or quarantined:
            if request.symbol_lock_owner_token is None:
                return ExecutionResult("rejected", None, ["EXECUTION_RESERVATION_REQUIRED"], Decimal("0"), None)
            if quarantined:
                return ExecutionResult("rejected", None, ["UNSUPPORTED_SHORT_POSITION"], Decimal("0"), None)
        if request.side == Side.BUY:
            # Rev.142 §3.9 Q2: no strategy re-entry into an adopted asset.
            from tradepulse.adoption import reentry_excluded

            key = asset_identity_key(request.asset)
            if await self._repositories.position_adoptions.database.run(lambda c: reentry_excluded(c, self._lease.ref.key, key)):
                return ExecutionResult("rejected", None, ["ADOPTED_ASSET_REENTRY_BLOCKED"], Decimal("0"), None)
        if request.side == Side.SELL:
            authorized = await self._authorize_sell(request, existing.trade_intent_id if existing else None)
            if isinstance(authorized, ExecutionResult):
                return authorized
            request = replace(request, requested_quantity=authorized)

        # Broker positions are the quantity AUTHORITY for protective-exit
        # classification -- the local Holding (see settlement/engine.py) can
        # be wrong during exactly the accounting-drift scenario that trips
        # FINANCIAL_INTEGRITY_BLOCKED, and a protective exit computed off a
        # wrong local quantity could itself be blocked by the very state it
        # exists to survive. A momentary fetch failure means "try again,"
        # never "assume non-protective" or "assume flat" -- same fail-closed
        # skip pattern as the quote/account fetches below.
        try:
            positions = await self._broker.get_positions()
        except (AlpacaError, AlpacaDataIntegrityError, httpx.HTTPError) as exc:
            return ExecutionResult("skipped", None, [f"BROKER_POSITIONS_UNAVAILABLE: {exc}"], Decimal("0"), None)

        # Canonically-keyed, not bare-symbol -- must agree with the same
        # identity scheme local financial state uses (Holdings, lots,
        # in-flight checks; see models/market.py::asset_identity_key). Two
        # broker positions should never legitimately collide on the same
        # canonical key; if they do, that's a broker-data anomaly worth
        # alerting on and refusing to guess through, not silently letting a
        # dict comprehension pick whichever came last.
        positions_by_key: dict[str, AlpacaPosition] = {}
        for p in positions:
            key = asset_key_from_broker_symbol(p.asset_class, p.symbol)
            if key in positions_by_key:
                await self._alerts.send(
                    "critical",
                    f"Duplicate broker position for canonical asset key {key} ({p.symbol}, {p.asset_class.value}) -- refusing to guess which is authoritative.",
                    {"asset_key": key, "symbol": p.symbol, "asset_class": p.asset_class.value},
                )
                return ExecutionResult("skipped", None, [f"DUPLICATE_BROKER_POSITION_KEY: {key}"], Decimal("0"), None)
            positions_by_key[key] = p

        held_position = positions_by_key.get(asset_identity_key(request.asset))
        held_quantity = held_position.qty if held_position is not None else Decimal("0")
        # Rev.140 removed the cover path: TradePulse never buys against a
        # short position, which exists only as quarantined broker inventory.
        if request.side == Side.BUY and held_quantity < 0:
            return ExecutionResult("rejected", None, ["UNSUPPORTED_SHORT_POSITION"], Decimal("0"), None)
        protective_exit = request.side == Side.SELL and held_quantity > 0 and request.requested_quantity <= held_quantity
        # Same broker truth feeds portfolio exposure -- current market value,
        # not stale local cost basis (see risk/engine.py::build_portfolio_snapshot).
        # Keyed canonically to match how build_portfolio_snapshot looks it up.
        mark_prices = {key: p.current_price for key, p in positions_by_key.items()}

        # Everything through the RISK_APPROVED persist below is the
        # capital-allocation DECISION -- serialized across different symbols
        # by the portfolio-risk lock so a concurrent evaluation (a different
        # asset-class lane, say) can't read the same stale exposure and
        # independently approve capital that's already spoken for. Only
        # needed when this can reach evaluate_risk's sizing block at all --
        # protective exits and every SELL only ever reduce exposure, so they
        # don't need cross-symbol serialization.
        database = self._repositories.trade_intents.database
        needs_portfolio_lock = request.side == Side.BUY and not protective_exit
        async with _portfolio_risk_lock(database, needed=needs_portfolio_lock) as (acquired, portfolio_lease):
            if not acquired:
                return ExecutionResult("skipped", None, ["PORTFOLIO_RISK_EVALUATION_LOCKED"], Decimal("0"), None)

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
            # DBAE (spec §3.3): every new buy reads fresh account and asset
            # evidence inside this lock. Protective exits and sells never do.
            entry_evidence = await self._read_entry_evidence(request.asset) if needs_portfolio_lock else None

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
                order_type=request.order_type,
                signal_timestamp=request.signal_timestamp,
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
                broker_prev_close_equity=account.last_equity, mark_prices=mark_prices, now=now,
                broker_positions=positions_by_key,
            )

            max_drawdown_breached = False
            if request.side == Side.BUY and not protective_exit and self._risk_limits.max_drawdown_pct > 0:
                drawdown = await check_max_drawdown(self._repositories, snapshot.total_equity, self._risk_limits)
                max_drawdown_breached = drawdown.breached

            # An add-on to a held long is protected by the holding's enforced
            # stop (the first entry's, PROTECTIVE_THRESHOLD_POLICY), not by
            # this request's own. Size against the looser of the two so the
            # add-on never risks more than its budget (Rev.126). The intent
            # still records its own stop.
            sizing_stop = request.stop_loss
            if request.side == Side.BUY and held_quantity > 0 and request.stop_loss is not None:
                holding_row = await self._repositories.holdings.get(asset_identity_key(request.asset))
                enforced_stop = hydrate("holdings", holding_row["payload"]).stop_loss if holding_row else None
                if enforced_stop is not None and enforced_stop < request.stop_loss:
                    sizing_stop = enforced_stop
            risk_input = RiskCheckInput(
                symbol=request.asset.symbol, asset_class=request.asset.asset_class, side=request.side,
                requested_quantity=request.requested_quantity, price=quote.price,
                confidence=request.confidence, stop_loss=sizing_stop, sector=request.sector or "Other",
                contract_multiplier=contract_multiplier_of(request.asset),
                regime_multiplier=request.regime_multiplier,
            )
            new_exposure = request.side == Side.BUY and not protective_exit
            risk_opts = RiskEvalOptions(
                open_risk=await open_portfolio_risk(self._repositories) if new_exposure else Decimal("0"),
                symbol_entries_today=(await symbol_entries_today(self._repositories, request.asset, now,
                                                                 exclude_intent_id=trade_intent_id)
                                      if new_exposure else 0),
                protective_exit=protective_exit, bid=quote.bid, ask=quote.ask,
                estimated_slippage_pct=quote.estimated_slippage_pct,
                held_quantity=abs(held_quantity), available_cash=account.cash,
                max_drawdown_breached=max_drawdown_breached,
                held_notional=abs(held_position.market_value) if held_position is not None and request.side == Side.BUY else Decimal("0"),
            )
            # DBAE (spec §8.1 steps 1-4): account evidence, the session
            # baseline and band, asset eligibility and funding, before the
            # unchanged risk engine, which then sizes against the cash not
            # already committed to a pending buy.
            authorized_entry: _AuthorizedEntry | None = None
            if entry_evidence is not None:
                decided = await self._authorize_entry(request, intent, entry_evidence, account.last_equity, quote,
                                                      sizing_stop)
                if isinstance(decided, ExecutionResult):
                    # §8.1 orders entry authorization, not the account-level
                    # kill switch: a daily-loss or drawdown breach still
                    # latches, judged by the engine on this same snapshot.
                    await self._latch_kill_switch(evaluate_risk(risk_input, snapshot, self._risk_limits, risk_opts))
                    return decided
                authorized_entry = decided
                # Sized so the reservation (notional plus slippage allowance
                # and fee, §7.2) never exceeds the funding available (§7.5).
                fundable = fundable_notional(decided.funding_available, request.asset.asset_class,
                                             self._risk_limits.slippage_limit_for(request.asset.asset_class))
                risk_opts = replace(risk_opts, available_cash=fundable)
            risk = evaluate_risk(risk_input, snapshot, self._risk_limits, risk_opts)
            if not risk.approved:
                await self._latch_kill_switch(risk)
                rejected = replace(intent, status=TradeIntentStatus.REJECTED, rejection_reason="; ".join(risk.reasons))
                await self._repositories.trade_intents.update(trade_intent_id, rejected, status=rejected.status.value)
                return ExecutionResult("rejected", trade_intent_id, risk.reasons, Decimal("0"), None)

            # DBAE (spec §8.1 steps 6-8): shape, re-approve, exit feasibility.
            # From RISK_APPROVED on, the intent's requested_quantity is the
            # submitted quantity (§7.1).
            submitted_quantity = risk.approved_quantity
            dbae_record: dict[str, DbaeSnapshotValue] | None = None
            if authorized_entry is not None and entry_evidence is not None:
                shaped = await self._shape_entry(authorized_entry, request, intent, risk, risk_input, snapshot, risk_opts,
                                                 entry_evidence)
                if isinstance(shaped, ExecutionResult):
                    return shaped
                submitted_quantity, dbae_record = shaped

            # Captures values already computed above (risk_input, risk,
            # quote, self._risk_limits) -- nothing here is recomputed or
            # newly derived. Without this, the sizing story (why this
            # quantity, which caps bound it) only ever existed as
            # RiskDecision.reasons, transient in-memory for the one process
            # that computed it -- gone the moment this function returns, and
            # unrecoverable for any later "why was this sized this way"
            # read (a dashboard, an audit, a human).
            risk_snapshot: dict[str, Any] = {
                "reasons": list(risk.reasons),
                "confidence": str(request.confidence) if request.confidence is not None else None,
                "entry_price": str(quote.price),
                "reference_bid": str(quote.bid),
                "reference_ask": str(quote.ask),
                "reference_observed_at": quote.observed_at.isoformat(),
                "estimated_slippage_pct": str(quote.estimated_slippage_pct),
                "order_type": request.order_type,
                "signal_timestamp": request.signal_timestamp,
                "stop_loss": str(request.stop_loss) if request.stop_loss is not None else None,
                "contract_multiplier": str(risk_input.contract_multiplier),
                "requested_quantity": str(request.requested_quantity),
                "approved_quantity": str(risk.approved_quantity),
                "risk_profile": self._risk_limits.profile_id,
                # Exit Intelligence -- frozen per-trade at entry time so
                # settlement/engine.py::_infer_exit_reason can classify a
                # time-stop exit post-hoc without depending on whatever the
                # CURRENT risk profile config says (profiles are manually
                # revisited over the life of a running trade -- a live
                # lookup would drift). Pure provenance, same shape as the
                # regime_snapshot merge below -- not a decision input.
                "max_hold_days": str(self._risk_limits.max_hold_days),
                # The account this intent was approved on. Stranded-intent
                # recovery may only close the intent after a definitive
                # not-found from this same account.
                "broker_account_number": account.account_number,
            }
            # Market Regime Phase 2 -- merged in verbatim, un-interpreted
            # (see ExecutionRequest.regime_snapshot's own docstring). Never
            # lets a regime_snapshot key override one of the established
            # keys above -- enforced here, not just by convention, so a
            # future regime_snapshot producer that happened to choose a
            # colliding key name (e.g. "risk_profile") cannot silently
            # corrupt this audit trail. Deliberately does not raise: this
            # runs inside a live scan cycle's execution path, where an
            # exception here would abort the whole cycle over what is only
            # a provenance-dict defect, not a sizing/safety one (sizing
            # already happened correctly via the separate, independently
            # validated regime_multiplier field).
            if request.exit_reason is not None:
                risk_snapshot["exit_reason"] = request.exit_reason
            if dbae_record is not None:
                risk_snapshot["dbae"] = dbae_record  # the four quantities and the decision record (§7.1, §11)
            for key, value in (request.regime_snapshot or {}).items():
                risk_snapshot.setdefault(key, value)
            approved = replace(
                intent, status=TradeIntentStatus.RISK_APPROVED, requested_quantity=submitted_quantity,
                risk_snapshot=risk_snapshot,
            )
            # DBAE-146-02: the evidence must still be fresh when the approval
            # commits, judged inside the same fenced transaction.
            fresh_at_commit = None if authorized_entry is None else self._evidence_still_fresh(authorized_entry, trade_intent_id)
            try:
                await self._repositories.trade_intents.update(trade_intent_id, approved, status=approved.status.value,
                                                              lease=portfolio_lease, precondition=fresh_at_commit)
            except RepositoryPreconditionError as stale:
                # Nothing was written. Closed unsubmitted, like the lease-lost
                # path, with the refusal recorded (§10).
                if authorized_entry is None or entry_evidence is None:
                    raise
                return await self._refuse_entry(intent, list(stale.reasons), entry_evidence,
                                                authorized_entry.policy.band, authorized_entry.band_equity,
                                                aware_utc(self._clock(), field_name="now"))
            except RepositoryLeaseLostError:
                # Another caller may have approved capital against the same
                # snapshot after this lease lapsed. Close the intent unsubmitted.
                reason = "PORTFOLIO_RISK_LOCK_LOST"
                rejected = replace(intent, status=TradeIntentStatus.REJECTED, rejection_reason=reason)
                await self._repositories.trade_intents.update(trade_intent_id, rejected, status=rejected.status.value)
                return ExecutionResult("skipped", trade_intent_id, [reason], Decimal("0"), None)

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
        # risk/session.py::sync_market_session) -- new exposure in any
        # non-continuous-market asset class (equity, options -- both trade
        # only during the regular exchange session) gets one more,
        # always-fresh check right at the irreversible submission boundary.
        # Crypto is exempt (continuous market, see is_continuous_market);
        # any protective exit is exempt regardless of asset class, matching
        # the session gate's own exemption -- a stop-loss must still be able
        # to fire.
        if not is_continuous_market(request.asset.asset_class) and not protective_exit:
            try:
                market_clock = await self._broker.get_clock()
            except (AlpacaError, httpx.HTTPError) as exc:
                reason = f"MARKET_CLOCK_UNAVAILABLE: {exc}"
                rejected = replace(approved, status=TradeIntentStatus.REJECTED, rejection_reason=reason)
                await self._repositories.trade_intents.update(trade_intent_id, rejected, status=rejected.status.value)
                return ExecutionResult("rejected", trade_intent_id, [reason], Decimal("0"), None)
            # Rev.144 §3.5: one decision, the broker clock plus the calendar
            # as a second witness. EQUITY_MARKET_CLOSED and
            # OPTION_MARKET_CLOSED keep their established strings.
            window = await self._entry_window(request.asset, market_clock)
            if not window.open:
                reason = window.reason or "CALENDAR_SESSION_CLOSED"
                rejected = replace(approved, status=TradeIntentStatus.REJECTED, rejection_reason=reason)
                await self._repositories.trade_intents.update(trade_intent_id, rejected, status=rejected.status.value)
                return ExecutionResult("rejected", trade_intent_id, [reason], Decimal("0"), None)

        # Account ownership fence: this process must still hold the account
        # lease, or another TradePulse runtime may be trading the same account.
        # No ambiguity to recover from on rejection -- place_order is never called.
        try:
            self._lease.verify()
        except AccountLeaseUnavailable as exc:
            await self._alerts.send(
                "critical",
                f"Account lease for {self._lease.ref.key} not held immediately before broker submission -- order NOT placed.",
                {"account": self._lease.ref.key, "symbol": request.asset.symbol, "trade_intent_id": trade_intent_id,
                 "cause": str(exc)},
            )
            return await self._reject_before_submission(approved, "ACCOUNT_LEASE_NOT_HELD")

        # Final ownership fence, right before the irreversible sequence
        # begins: if the caller holds a per-asset execution reservation
        # (see execution/idempotency.py), re-verify it's still valid.
        # renew_lock both checks AND extends it in one call, so the lease is
        # freshly renewed right as the genuinely long part (order placement
        # + fill polling) begins. No ambiguity to recover from on rejection
        # here -- place_order was never called.
        if request.symbol_lock_owner_token is not None:
            still_reserved = await renew_lock(
                self._repositories.trade_intents.database, execution_lock_key(request.asset),
                request.symbol_lock_owner_token, SYMBOL_LOCK_TTL_SECONDS,
            )
            if not still_reserved:
                await self._alerts.send(
                    "critical",
                    f"Execution reservation lost for {request.asset.symbol} immediately before broker submission -- order NOT placed.",
                    {"asset_class": request.asset.asset_class.value, "symbol": request.asset.symbol, "trade_intent_id": trade_intent_id},
                )
                rejected = replace(approved, status=TradeIntentStatus.REJECTED, rejection_reason="EXECUTION_RESERVATION_LOST")
                await self._repositories.trade_intents.update(trade_intent_id, rejected, status=rejected.status.value)
                return ExecutionResult("rejected", trade_intent_id, ["EXECUTION_RESERVATION_LOST"], Decimal("0"), None)

        if request.asset.asset_class == AssetClass.CRYPTO:
            from tradepulse.reconciliation.epochs import AccountingEpochPending, reserve_epoch
            # Re-read immediately before submitting a protective order. An
            # intervening fee must never leave an exit above broker inventory.
            if protective_exit:
                try:
                    fresh = await self._broker.get_positions()
                    matched = [p for p in fresh if asset_key_from_broker_symbol(p.asset_class, p.symbol)
                               == asset_identity_key(request.asset)]
                    if len(matched) != 1 or submitted_quantity > abs(matched[0].qty):
                        return await self._reject_before_submission(approved, "BROKER_EXIT_QUANTITY_CHANGED")
                    held_quantity = matched[0].qty
                except (AlpacaError, AlpacaDataIntegrityError, httpx.HTTPError) as exc:
                    return await self._reject_before_submission(approved, f"BROKER_POSITIONS_UNAVAILABLE: {exc}")
            try:
                await reserve_epoch(self._repositories, approved, held_quantity, protective=protective_exit, now=now)
            except AccountingEpochPending as exc:
                return await self._reject_before_submission(approved, str(exc))

        order_request = AlpacaOrderRequest(
            symbol=request.asset.symbol, qty=submitted_quantity, side=request.side,
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
                if protective_exit:
                    try:
                        await self._alert_protective_exit_rejected(rejected, reason)
                    except Exception:  # the rejection is already persisted; a failed alert must not abort the cycle
                        logger.exception("protective_exit_rejected_alert_failed trade_intent_id=%s", trade_intent_id)
                return ExecutionResult("rejected", trade_intent_id, [reason], Decimal("0"), None)
            # Ambiguous outcome (429, 5xx, network/timeout/DNS error, etc.) --
            # Alpaca's actual acceptance of the order cannot be established
            # from this error alone. Never assume rejection or resubmit.
            return await self._recover_unknown_submission(submitted, exc)

        if not placed.broker_order_id:
            # A success response without a usable order id proves nothing
            # about which order Alpaca holds (Rev.122). Recover by client
            # order id exactly as for an ambiguous submission; never resubmit.
            return await self._recover_unknown_submission(submitted, RuntimeError("BROKER_ORDER_ID_MISSING"))

        accepted = replace(
            submitted, status=TradeIntentStatus.ACCEPTED, broker_order_id=placed.broker_order_id,
            client_order_id=trade_intent_id, submitted_at=placed.submitted_at or self._clock(),
        )
        await self._repositories.trade_intents.update(trade_intent_id, accepted, status=accepted.status.value)

        return await self._poll_and_settle(accepted)

    async def _authorize_sell(self, request: ExecutionRequest, current_intent_id: str | None) -> Decimal | ExecutionResult:
        """The sell quantity the evidence supports, or the refusal (spec §3.1).
        Writes nothing and releases nothing; the caller's reservation is held
        throughout."""
        from tradepulse.execution.sellable import (
            SellableEvidenceError,
            SellableInputs,
            build_pending_set,
            compute_sellable,
            size_sell,
        )
        from tradepulse.reconciliation.completeness import history_unproven
        from tradepulse.reconciliation.unresolved import fill_cursor, open_unresolved

        def refuse(reason: str) -> ExecutionResult:
            return ExecutionResult("rejected", None, [reason], Decimal("0"), None)

        database = self._repositories.trade_intents.database
        assert request.symbol_lock_owner_token is not None
        if not await renew_lock(database, execution_lock_key(request.asset), request.symbol_lock_owner_token,
                                SYMBOL_LOCK_TTL_SECONDS):
            return refuse("EXECUTION_RESERVATION_LOST")
        key = asset_identity_key(request.asset)
        if await open_unresolved(self._repositories, key):
            return refuse("UNRESOLVED_BROKER_EXECUTION")
        if await history_unproven(self._repositories, key):
            return refuse("ACTIVITY_HISTORY_UNPROVEN")
        intents = [intent for intent in (hydrate("trade_intents", row["payload"])
                                         for row in await paginate_all_rows(self._repositories.trade_intents))
                   if intent.side == Side.SELL and asset_identity_key(intent.asset) == key
                   and intent.trade_intent_id != current_intent_id]
        if any(intent.status in (TradeIntentStatus.SUBMITTED, TradeIntentStatus.SUBMISSION_UNKNOWN) for intent in intents):
            return refuse("PRIOR_SUBMISSION_UNRESOLVED")  # S3: the monitor's in-flight rule, at the gateway
        cursor = await fill_cursor(self._repositories)
        if cursor is None:
            return refuse("FILL_CURSOR_NOT_BOOTSTRAPPED")
        settled = {}
        for intent in intents:
            rows = await list_all_by_json_field(self._repositories.settlements, "trade_intent_id", intent.trade_intent_id)
            settled[intent.trade_intent_id] = sum(
                (event.quantity for event in (hydrate("settlements", row["payload"]) for row in rows)
                 if event.status == SettlementStatus.COMPLETED), Decimal(0))
        local = sum((lot.remaining_quantity for lot in (hydrate("position_lots", row["payload"])
                                                         for row in await list_all_by_asset(self._repositories.position_lots, request.asset))
                     if lot.position_side == "long" and lot.status in ("open", "partially_closed")), Decimal(0))

        for attempt in range(3):  # R10: an unstable snapshot is retried at most three times, then refused
            try:
                first = await self._broker.get_open_orders()
            except (AlpacaError, AlpacaDataIntegrityError, httpx.HTTPError):
                return refuse("BROKER_OPEN_ORDERS_UNAVAILABLE")
            try:
                positions = await self._broker.get_positions()
            except (AlpacaError, AlpacaDataIntegrityError, httpx.HTTPError):
                return refuse("BROKER_POSITIONS_UNAVAILABLE")
            try:
                second = await self._broker.get_open_orders()
            except (AlpacaError, AlpacaDataIntegrityError, httpx.HTTPError):
                return refuse("BROKER_OPEN_ORDERS_UNAVAILABLE")
            pages: list[Any] = []
            try:
                activities = await self._broker.get_activities(activity_type="FILL", after_id=cursor, page_evidence=pages)
            except (AlpacaError, AlpacaDataIntegrityError, httpx.HTTPError):
                return refuse("BROKER_ACTIVITIES_UNAVAILABLE")
            if not pages or not pages[-1]["terminal"] or any(page["terminal"] for page in pages[:-1]):
                return refuse("ACTIVITY_PAGE_UNPROVEN")
            for activity in (a for a in activities if a.activity_id > cursor):  # never trust the broker's filter alone
                if broker_symbol_asset_key(activity.symbol) != key:
                    continue
                if await self._repositories.fills.get(activity.activity_id) is None \
                        and await self._repositories.fills.find_by_unique(activity.activity_id) is None \
                        and await self._repositories.quarantine_executions.get(activity.activity_id) is None:
                    return refuse("UNRECONCILED_BROKER_ACTIVITY")
            held = next((p.qty for p in positions if asset_key_from_broker_symbol(p.asset_class, p.symbol) == key), Decimal(0))
            if held < 0:
                return refuse("UNSUPPORTED_SHORT_POSITION")
            try:
                pending = build_pending_set(key, first, second, intents, settled)
                decision = compute_sellable(SellableInputs(held, local, pending))
            except SellableEvidenceError as exc:
                if exc.reason == "SELLABLE_SNAPSHOT_UNSTABLE" and attempt < 2:
                    continue
                return refuse(exc.reason)
            # Protective by purpose (the monitor names every exit's reason), or a
            # sell within the broker position: reduced to sellable, never refused.
            protective = held > 0 and (request.exit_reason is not None or request.requested_quantity <= held)
            quantity, note = size_sell(decision, request.requested_quantity, protective=protective)
            if quantity <= 0:
                return refuse(f"{note} (local {decision.local_bound}, broker {decision.broker_bound}, "
                              f"requested {request.requested_quantity})")
            if note == "SELLABLE_BELOW_REQUESTED":
                await self._alerts.send(
                    "warning",
                    f"SELLABLE_BELOW_REQUESTED: {request.asset.symbol} exit reduced from {request.requested_quantity} "
                    f"to {quantity} (local bound {decision.local_bound}, broker bound {decision.broker_bound}).",
                    {"symbol": request.asset.symbol, "requested": str(request.requested_quantity), "sellable": str(quantity),
                     "local_bound": str(decision.local_bound), "broker_bound": str(decision.broker_bound)})
            return quantity
        return refuse("SELLABLE_SNAPSHOT_UNSTABLE")  # pragma: no cover - the loop returns on its last attempt

    async def _reject_before_submission(self, intent: TradeIntent, reason: str) -> ExecutionResult:
        """No order was placed. Close the intent so has_in_flight_intent never
        treats it as in flight -- a stranded RISK_APPROVED intent would block
        every later order on the asset, protective exits included."""
        rejected = replace(intent, status=TradeIntentStatus.REJECTED, rejection_reason=reason)
        await self._repositories.trade_intents.update(intent.trade_intent_id, rejected, status=rejected.status.value)
        return ExecutionResult("rejected", intent.trade_intent_id, [reason], Decimal("0"), None)

    async def _alert_protective_exit_rejected(self, intent: TradeIntent, reason: str) -> None:
        """Spec §9: the holding and its stop obligation stay; the next monitor
        cycle may submit a new exit. Each retry is a new intent, so a broker
        that keeps rejecting would alert every cycle: one critical alert per
        asset per UTC hour instead. The audit event of the first rejection
        in the hour carries its intent; every rejection stays on its own
        REJECTED intent row."""
        now = aware_utc(self._clock(), field_name="now")
        key = asset_identity_key(intent.asset)
        await alert_once_per_window(
            self._repositories.audit_events, self._alerts, event_type="PROTECTIVE_EXIT_REJECTED", subject=key,
            window=now.astimezone(UTC).strftime("%Y-%m-%dT%H"), severity="critical",
            message=(f"PROTECTIVE_EXIT_REJECTED: {intent.asset.symbol} exit of {intent.requested_quantity} rejected by the "
                     f"broker ({reason}); the position stays held under its stop and the next monitor cycle retries. "
                     "Further rejections of this asset this hour are not re-alerted."),
            details={"trade_intent_id": intent.trade_intent_id, "reason": reason, "asset_key": key},
            now=now, entity_type="execution")

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

        if order is not None and not order_matches_intent(order, intent):
            # An order under this client id that is not the one this intent
            # would have submitted (Rev.120): never adopt it; a human decides.
            cause = RuntimeError(f"BROKER_ORDER_IDENTITY_MISMATCH: order {order.broker_order_id}")
            order = None

        if order is not None:
            accepted = replace(
                intent, status=TradeIntentStatus.ACCEPTED, broker_order_id=order.broker_order_id,
                client_order_id=intent.trade_intent_id,
            )
            await self._repositories.trade_intents.update(intent.trade_intent_id, accepted, status=accepted.status.value)
            return await self._poll_and_settle(accepted)

        unknown_reason = f"SUBMISSION_UNKNOWN: {cause}"
        unknown = replace(intent, status=TradeIntentStatus.SUBMISSION_UNKNOWN, rejection_reason=unknown_reason)
        await self._repositories.trade_intents.update(intent.trade_intent_id, unknown, status=unknown.status.value)
        await self._alerts.send(
            "critical",
            f"Broker submission outcome unknown for {intent.asset.symbol} {intent.side.value} {intent.requested_quantity} -- "
            "reconciliation retries the client-order-id lookup each pass; the asset stays blocked until Alpaca proves "
            "the order's outcome, and a mismatch needs manual review",
            {"trade_intent_id": intent.trade_intent_id, "cause": str(cause)},
        )
        return ExecutionResult("pending", intent.trade_intent_id, [unknown_reason], Decimal("0"), None)

    def _result_from_intent(self, intent: TradeIntent) -> ExecutionResult:
        status_map: dict[TradeIntentStatus, ExecutionStatus] = {
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

    async def _attribute_order_fills(self, current: TradeIntent, *, verify_order_filled_qty: bool = False) -> AttributedFills:
        """Thin pass-through to the shared fill_attribution module -- kept so
        existing/direct callers of this method (including tests) don't need
        to reach into fill_attribution themselves. See that module's
        docstring for why this logic is shared rather than gateway-private:
        reconciliation's late-fill recovery needs the exact same accounting
        path, not a second way to create a Fill/SettlementEvent.

        verify_order_filled_qty defaults False here so direct unit-level
        callers of this method (see test_execution_gateway.py) are
        unaffected -- _poll_and_settle's own call site below passes True,
        the only production path that needs FIN-094-01's order-level
        consistency check."""
        return await attribute_order_fills(
            self._repositories, self._broker, self._alerts, current, self._clock,
            verify_order_filled_qty=verify_order_filled_qty,
        )

    async def _poll_and_settle(self, intent: TradeIntent) -> ExecutionResult:
        broker_order_id = intent.broker_order_id  # an order's broker id never changes while it is polled
        if broker_order_id is None:
            raise ValueError("POLL_REQUIRES_BROKER_ORDER_ID")
        current = intent
        last_filled_qty = intent.filled_quantity
        deadline = time.monotonic() + self.FILL_TIMEOUT_SECONDS

        while time.monotonic() < deadline:
            try:
                order = await self._broker.get_order(broker_order_id)
            except AlpacaError:
                await asyncio.sleep(self.POLL_INTERVAL_SECONDS)
                continue

            cumulative_filled = order.filled_qty

            attributed_qty = last_filled_qty
            if cumulative_filled > 0:
                attributed = await self._attribute_order_fills(current, verify_order_filled_qty=True)
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
                    else "partially_filled" if attributed_qty > 0  # incl. a canceled order's filled part
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
