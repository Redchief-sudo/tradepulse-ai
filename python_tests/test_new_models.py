from datetime import UTC, datetime
from decimal import Decimal

import pytest

from tradepulse.models import (
    AssetClass,
    AuditEvent,
    PortfolioSnapshot,
    ReconciliationOutcome,
    ReconciliationRecord,
    RiskLimits,
    ScanRun,
    ScanRunStatus,
    ScanTrigger,
    SessionState,
    StrategyWeights,
    TradingSession,
)


NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)


def test_risk_stopped_session_requires_kill_switch_flags() -> None:
    with pytest.raises(ValueError, match="kill_switch_reset_required"):
        TradingSession("session-1", SessionState.RISK_STOPPED, False, NOW)
    session = TradingSession(
        "session-1", SessionState.RISK_STOPPED, False, NOW,
        kill_switch_reason="daily loss limit breached", kill_switch_reset_required=True,
    )
    assert not session.is_tradeable()


def test_active_session_cannot_carry_unresolved_kill_switch() -> None:
    with pytest.raises(ValueError, match="unresolved kill-switch"):
        TradingSession("session-1", SessionState.ACTIVE, True, NOW, kill_switch_reset_required=True)


def test_active_tradeable_session() -> None:
    session = TradingSession("session-1", SessionState.ACTIVE, True, NOW)
    assert session.is_tradeable()


def test_financial_integrity_blocked_requires_manual_reenable() -> None:
    with pytest.raises(ValueError, match="manual_reenable_required"):
        TradingSession("session-1", SessionState.FINANCIAL_INTEGRITY_BLOCKED, False, NOW)


def test_audit_event_requires_known_severity() -> None:
    with pytest.raises(ValueError, match="severity"):
        AuditEvent("evt-1", "kill_switch_tripped", "urgent", "daily loss limit breached", NOW)  # type: ignore[arg-type]
    event = AuditEvent("evt-1", "kill_switch_tripped", "critical", "daily loss limit breached", NOW)
    assert event.severity == "critical"


def test_failed_scan_run_requires_error_and_completed_at() -> None:
    with pytest.raises(ValueError, match="completed_at"):
        ScanRun("scan-1", "gen-1", ScanTrigger.SCHEDULED, AssetClass.EQUITY, ScanRunStatus.FAILED, NOW, "owner-1")
    with pytest.raises(ValueError, match="error"):
        ScanRun("scan-1", "gen-1", ScanTrigger.SCHEDULED, AssetClass.EQUITY, ScanRunStatus.FAILED, NOW, "owner-1", completed_at=NOW)
    run = ScanRun(
        "scan-1", "gen-1", ScanTrigger.SCHEDULED, AssetClass.EQUITY, ScanRunStatus.FAILED, NOW, "owner-1",
        completed_at=NOW, error="broker unreachable",
    )
    assert run.error == "broker unreachable"


def test_corrected_reconciliation_requires_corrective_action() -> None:
    with pytest.raises(ValueError, match="corrective_action"):
        ReconciliationRecord(
            "rec-1", "position", "AAPL", ReconciliationOutcome.CORRECTED,
            {"quantity": "10"}, {"quantity": "8"}, NOW,
        )
    record = ReconciliationRecord(
        "rec-1", "position", "AAPL", ReconciliationOutcome.CORRECTED,
        {"quantity": "10"}, {"quantity": "8"}, NOW, corrective_action="synced local ledger to broker",
    )
    assert record.outcome == ReconciliationOutcome.CORRECTED


def test_risk_limits_reject_out_of_range_confidence() -> None:
    with pytest.raises(ValueError, match="min_confidence"):
        RiskLimits(
            "balanced", Decimal("7"), Decimal("20"), Decimal("180"), 3, Decimal("8"), Decimal("15"),
            5, Decimal("1.0"), Decimal("1.5"), Decimal("1"), Decimal("0.30"), Decimal("40"), 2,
        )


def test_portfolio_snapshot_requires_known_source() -> None:
    with pytest.raises(ValueError, match="source"):
        PortfolioSnapshot(
            "snap-1", NOW, Decimal("10000"), Decimal("5000"), Decimal("5000"), {}, 2, 0, 1,
            Decimal("0.5"), "manual",  # type: ignore[arg-type]
        )
    snapshot = PortfolioSnapshot(
        "snap-1", NOW, Decimal("10000"), Decimal("5000"), Decimal("5000"), {}, 2, 0, 1,
        Decimal("0.5"), "broker",
    )
    assert snapshot.total_equity == Decimal("10000")


def test_strategy_weights_require_positive_total() -> None:
    with pytest.raises(ValueError, match="positive total"):
        StrategyWeights("v1", Decimal("0"), Decimal("0"), Decimal("0"), NOW)
    weights = StrategyWeights("v1", Decimal("0.40"), Decimal("0.35"), Decimal("0.25"), NOW)
    assert weights.technical_weight == Decimal("0.40")
