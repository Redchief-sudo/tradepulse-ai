"""alert_once records its dedupe id only after delivery (Rev.120)."""
from datetime import UTC, datetime

from tradepulse.alerts import TelegramAlerter, alert_once
from tradepulse.models import AuditEvent
from tradepulse.persistence import AsyncSQLiteDatabase, PersistenceRepositories

NOW = datetime(2026, 10, 5, 15, 0, tzinfo=UTC)


class _Alerter(TelegramAlerter):
    """Configured alerter whose deliveries succeed or fail on a script."""

    def __init__(self, outcomes: list[bool]) -> None:
        super().__init__("token", "chat")
        self.outcomes = outcomes
        self.attempts = 0

    async def send(self, severity, message, details=None) -> bool:
        self.attempts += 1
        return self.outcomes.pop(0)


async def _audit_events(tmp_path):
    database = AsyncSQLiteDatabase(f"sqlite:///{tmp_path}/test.db")
    await database.initialize()
    return PersistenceRepositories.create(database).audit_events


def _event() -> AuditEvent:
    return AuditEvent(event_id="accounting_drift:aapl:2026-10-05", event_type="accounting_drift", severity="critical",
                      message="ACCOUNTING DRIFT", occurred_at=NOW, details={"symbol": "AAPL"})


async def test_a_failed_delivery_is_retried_not_suppressed_for_the_window(tmp_path) -> None:
    audit_events = await _audit_events(tmp_path)
    alerts = _Alerter([False, True, True])
    assert await alert_once(audit_events, alerts, _event()) is False  # Telegram down: nothing recorded
    assert await audit_events.get(_event().event_id) is None
    assert await alert_once(audit_events, alerts, _event()) is True   # next pass delivers and records
    assert await alert_once(audit_events, alerts, _event()) is False  # then deduplicated
    assert alerts.attempts == 2


async def test_unconfigured_alerting_records_without_retrying(tmp_path) -> None:
    audit_events = await _audit_events(tmp_path)
    alerts = TelegramAlerter(None, None)
    assert not alerts.configured
    assert await alert_once(audit_events, alerts, _event()) is True
    assert await alert_once(audit_events, alerts, _event()) is False
