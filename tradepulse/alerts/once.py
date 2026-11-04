"""Record an alert's audit evidence once, and deliver it at most once per id."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from tradepulse.models import AuditEvent

from .telegram import TelegramAlerter

if TYPE_CHECKING:
    from tradepulse.persistence import RecordRepository

# Longer than one Telegram send (10 s timeout): only one caller attempts a
# given alert's delivery at a time.
ALERT_DELIVERY_LOCK_TTL_SECONDS = 60


def delivery_marker_id(event_id: str) -> str:
    return f"{event_id}:delivered"


async def alert_once(audit_events: RecordRepository, alerts: TelegramAlerter, event: AuditEvent) -> bool:
    """Record ``event`` locally once per event_id, then deliver it until delivered.

    The local audit event is the evidence and never depends on Telegram: it
    is written on first sighting whether or not the alert goes out. Delivery
    is tracked separately by a ``<event_id>:delivered`` marker written only
    after Telegram accepts the alert, so a failed send is retried on the next
    pass. A per-alert database lease makes concurrent callers (two lanes, two
    processes on one database) attempt delivery one at a time, so one alert
    is not sent twice. With alerting unconfigured nothing is sent.

    Returns True when this call recorded the evidence (the first sighting)."""
    from tradepulse.persistence import acquire_lock, release_lock

    first_sighting = await audit_events.create_once(event.event_id, event)
    if not alerts.configured:
        return first_sighting
    marker_id = delivery_marker_id(event.event_id)
    if await audit_events.get(marker_id) is not None:
        return first_sighting
    database = audit_events.database
    lock_key = f"alert_delivery:{event.event_id}"
    owner_token = str(uuid4())
    if not await acquire_lock(database, lock_key, owner_token, "alert_delivery", ALERT_DELIVERY_LOCK_TTL_SECONDS):
        return first_sighting  # another caller is delivering this alert right now
    try:
        if await audit_events.get(marker_id) is not None:
            return first_sighting  # delivered between our check and our lease
        severity: Literal["warning", "critical"] = "critical" if event.severity == "critical" else "warning"
        if await alerts.send(severity, event.message, dict(event.details)):
            marker = replace(event, event_id=marker_id, event_type="alert_delivered", severity="info",
                             message=f"Alert delivered: {event.event_id}", details={"alert_event_id": event.event_id})
            await audit_events.create_once(marker_id, marker)
    finally:
        await release_lock(database, lock_key, owner_token)
    return first_sighting


async def alert_once_per_window(audit_events: RecordRepository, alerts: TelegramAlerter, *, event_type: str, subject: str,
                                window: str, severity: Literal["info", "warning", "error", "critical"], message: str,
                                details: dict, now: datetime, entity_type: str) -> bool:
    """Send an alert at most once per (event_type, subject, window).

    A condition that persists until an operator acts would otherwise re-alert
    on every pass of a periodic loop. The deterministic audit id records each
    window's evidence once and delivers its alert once (a failed send is
    retried next pass, see alert_once); ``window`` is any stable string, such
    as a UTC date, a date-hour, or an id the condition is scoped to.
    ``entity_type`` names the subsystem that raised it."""
    event = AuditEvent(event_id=f"{event_type}:{subject}:{window}", event_type=event_type, severity=severity,
                       message=message, occurred_at=now, entity_type=entity_type, entity_id=subject, details=details)
    return await alert_once(audit_events, alerts, event)
