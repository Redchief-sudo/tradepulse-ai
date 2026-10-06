"""Deliver an alert at most once per deterministic audit event id."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tradepulse.models import AuditEvent

from .telegram import TelegramAlerter

if TYPE_CHECKING:
    from tradepulse.persistence import RecordRepository


async def alert_once(audit_events: RecordRepository, alerts: TelegramAlerter, event: AuditEvent) -> bool:
    """Send ``event`` as an alert unless its event_id is already recorded.

    The id is recorded only after Telegram accepts the alert, or when
    alerting is not configured at all. A failed send is retried on the next
    pass rather than suppressed for the rest of the id's window. Returns True
    when this call recorded the id."""
    if await audit_events.get(event.event_id) is not None:
        return False
    severity = "critical" if event.severity == "critical" else "warning"
    if not await alerts.send(severity, event.message, dict(event.details)) and alerts.configured:
        return False
    return await audit_events.create_once(event.event_id, event)
