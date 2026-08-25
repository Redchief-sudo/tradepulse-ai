from datetime import UTC, datetime

from tradepulse.models import AssetClass, SessionState, Side, TradingSession
from tradepulse.risk.session import execution_session_decision


NOW = datetime(2026, 8, 15, tzinfo=UTC)


def _session(state: SessionState, trading_active: bool, **kwargs) -> TradingSession:
    return TradingSession("session-1", state, trading_active, NOW, **kwargs)


def test_kill_switch_checked_before_sell_bypass() -> None:
    """Regression test for the confirmed Base44 defect: sessionState.ts let
    ANY sell order bypass the kill-switch check because
    `protectiveExit || side === 'sell'` short-circuited before the
    kill-switch/integrity checks ran. A sell must be REJECTED while the kill
    switch is tripped and reset has not been acknowledged.
    """
    session = _session(
        SessionState.RISK_STOPPED, False,
        kill_switch_reason="daily loss limit breached", kill_switch_reset_required=True,
    )
    decision = execution_session_decision(session, Side.SELL, AssetClass.EQUITY, protective_exit=False)
    assert not decision.allowed
    assert decision.reason == "KILL_SWITCH_ACTIVE"


def test_financial_integrity_block_also_bypasses_sell_shortcut() -> None:
    session = _session(
        SessionState.FINANCIAL_INTEGRITY_BLOCKED, False,
        financial_integrity_reason="settlement mismatch", financial_integrity_manual_reenable_required=True,
    )
    decision = execution_session_decision(session, Side.SELL, AssetClass.EQUITY, protective_exit=False)
    assert not decision.allowed
    assert decision.reason == "FINANCIAL_INTEGRITY_BLOCKED"


def test_non_protective_sell_is_blocked_when_manually_stopped() -> None:
    """Regression test: a sell is not automatically protective just because
    it's a sell (e.g. opening/increasing a short). Only a caller-computed
    protective_exit=True (from actual held-quantity coverage) may bypass the
    plain 'session not active' check -- inferring it from side alone was the
    same class of defect already fixed once in the Base44 port.
    """
    session = _session(SessionState.MANUALLY_STOPPED, False)
    decision = execution_session_decision(session, Side.SELL, AssetClass.EQUITY, protective_exit=False)
    assert not decision.allowed
    assert "TRADING_SESSION_NOT_ACTIVE" in decision.reason


def test_genuinely_protective_sell_is_allowed_when_manually_stopped() -> None:
    session = _session(SessionState.MANUALLY_STOPPED, False)
    decision = execution_session_decision(session, Side.SELL, AssetClass.EQUITY, protective_exit=True)
    assert decision.allowed
    assert decision.reason == "PROTECTIVE_EXIT_ALLOWED"


def test_genuinely_protective_exit_bypasses_kill_switch() -> None:
    """A kill-switch must stop NEW risk-taking, not freeze an already-open
    position's own protective stop-loss/target exit -- a losing position
    should still be closeable while RISK_STOPPED, not left unprotected
    until an operator runs reset-risk."""
    session = _session(
        SessionState.RISK_STOPPED, False,
        kill_switch_reason="daily loss limit breached", kill_switch_reset_required=True,
    )
    decision = execution_session_decision(session, Side.SELL, AssetClass.EQUITY, protective_exit=True)
    assert decision.allowed
    assert decision.reason == "PROTECTIVE_EXIT_ALLOWED"


def test_genuinely_protective_exit_bypasses_financial_integrity_block() -> None:
    session = _session(
        SessionState.FINANCIAL_INTEGRITY_BLOCKED, False,
        financial_integrity_reason="settlement mismatch", financial_integrity_manual_reenable_required=True,
    )
    decision = execution_session_decision(session, Side.SELL, AssetClass.EQUITY, protective_exit=True)
    assert decision.allowed
    assert decision.reason == "PROTECTIVE_EXIT_ALLOWED"


def test_buy_blocked_when_session_not_active() -> None:
    session = _session(SessionState.MANUALLY_STOPPED, False)
    decision = execution_session_decision(session, Side.BUY, AssetClass.EQUITY, protective_exit=False)
    assert not decision.allowed
    assert "TRADING_SESSION_NOT_ACTIVE" in decision.reason


def test_active_session_allows_buy() -> None:
    session = _session(SessionState.ACTIVE, True)
    decision = execution_session_decision(session, Side.BUY, AssetClass.EQUITY, protective_exit=False)
    assert decision.allowed
    assert decision.reason == "ACTIVE"


def test_crypto_trades_continuously_through_market_closed_state() -> None:
    session = _session(SessionState.MARKET_CLOSED, True)
    decision = execution_session_decision(session, Side.BUY, AssetClass.CRYPTO, protective_exit=False)
    assert decision.allowed
    assert decision.reason == "CONTINUOUS_ASSET_SESSION"


def test_equity_buy_blocked_when_market_closed() -> None:
    session = _session(SessionState.MARKET_CLOSED, True)
    decision = execution_session_decision(session, Side.BUY, AssetClass.EQUITY, protective_exit=False)
    assert not decision.allowed
