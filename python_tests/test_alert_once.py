"""alert_once: local audit evidence independent of delivery (Rev.121)."""
import asyncio
from datetime import UTC, datetime

from tradepulse.alerts import TelegramAlerter, alert_once
from tradepulse.alerts.once import delivery_marker_id
from tradepulse.models import AuditEvent
from tradepulse.persistence import AsyncSQLiteDatabase, PersistenceRepositories

NOW = datetime(2026, 10, 5, 15, 0, tzinfo=UTC)
EVENT_ID = "unmanaged_position:aapl:2026-10-05"


class _Alerter(TelegramAlerter):
    """Configured alerter whose deliveries succeed or fail on a script."""

    def __init__(self, outcomes: list[bool], delay: float = 0) -> None:
        super().__init__("token", "chat")
        self.outcomes = outcomes
        self.delay = delay
        self.attempts = 0

    async def send(self, severity, message, details=None) -> bool:
        self.attempts += 1
        await asyncio.sleep(self.delay)
        return self.outcomes.pop(0)


async def _audit_events(tmp_path):
    database = AsyncSQLiteDatabase(f"sqlite:///{tmp_path}/test.db")
    await database.initialize()
    return PersistenceRepositories.create(database).audit_events


def _event() -> AuditEvent:
    return AuditEvent(event_id=EVENT_ID, event_type="unmanaged_broker_position", severity="critical",
                      message="UNMANAGED_POSITION", occurred_at=NOW, details={"symbol": "AAPL"})


async def test_evidence_is_recorded_even_when_delivery_fails_and_delivery_is_retried(tmp_path) -> None:
    audit_events = await _audit_events(tmp_path)
    alerts = _Alerter([False, True])
    assert await alert_once(audit_events, alerts, _event()) is True    # first sighting: evidence written
    assert await audit_events.get(EVENT_ID) is not None                # despite Telegram being down
    assert await audit_events.get(delivery_marker_id(EVENT_ID)) is None
    assert await alert_once(audit_events, alerts, _event()) is False   # not a new sighting...
    assert await audit_events.get(delivery_marker_id(EVENT_ID)) is not None  # ...but now delivered
    assert await alert_once(audit_events, alerts, _event()) is False
    assert alerts.attempts == 2  # failed once, delivered once, never again


async def test_concurrent_callers_deliver_one_alert(tmp_path) -> None:
    audit_events = await _audit_events(tmp_path)
    alerts = _Alerter([True, True, True], delay=0.2)
    results = await asyncio.gather(*(alert_once(audit_events, alerts, _event()) for _ in range(3)))
    assert sorted(results) == [False, False, True]  # one first sighting
    assert alerts.attempts == 1


async def test_unconfigured_alerting_records_evidence_and_sends_nothing(tmp_path) -> None:
    audit_events = await _audit_events(tmp_path)
    alerts = TelegramAlerter(None, None)
    assert not alerts.configured
    assert await alert_once(audit_events, alerts, _event()) is True
    assert await alert_once(audit_events, alerts, _event()) is False
    assert [row["record_id"] for row in await audit_events.list_all()] == [EVENT_ID]
