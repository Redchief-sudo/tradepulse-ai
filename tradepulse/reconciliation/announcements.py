"""Corporate-action announcements, stored raw with their SHA-256 (Rev.143 §3.4 step 1).

Append-only: an amended announcement is a new row under the same
announcement ID, and attribution records the exact version (SHA-256) it used.
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from hashlib import sha256
from typing import Any

from tradepulse.persistence.codec import decode_payload, encode_payload

WINDOW = timedelta(days=90)  # the measured maximum (evidence 2026-10-26-r7-api-semantics)


def announcement_sha256(raw: dict[str, Any]) -> str:
    return sha256(encode_payload(raw).encode()).hexdigest()


def store_announcements(connection: sqlite3.Connection, symbol: str, rows: list[dict[str, Any]], now: datetime) -> int:
    """Insert each version not already held. Returns how many were new."""
    added = 0
    for raw in rows:
        identifier = raw.get("id")
        if not isinstance(identifier, str) or not identifier:
            raise ValueError("DIVIDEND_ANNOUNCEMENT_INVALID: missing id")
        digest = announcement_sha256(raw)
        added += connection.execute(
            "INSERT OR IGNORE INTO corporate_action_announcements (record_id,announcement_id,sha256,symbol,payload,created_at) "
            "VALUES (?,?,?,?,?,?)",
            (f"{identifier}|{digest}", identifier, digest, symbol.upper(),
             encode_payload({"raw": raw, "fetched_at": now.isoformat()}), now.isoformat())).rowcount
    return added


def stored_announcements(connection: sqlite3.Connection, symbol: str) -> list[dict[str, Any]]:
    """The latest stored version of each announcement for ``symbol``, as
    ``{"raw", "sha256", "fetched_at"}``."""
    latest: dict[str, dict[str, Any]] = {}
    for identifier, digest, payload, created_at in connection.execute(
            "SELECT announcement_id, sha256, payload, created_at FROM corporate_action_announcements WHERE symbol=? "
            "ORDER BY created_at, rowid", (symbol.upper(),)):
        latest[identifier] = {"raw": decode_payload(payload)["raw"], "sha256": digest, "fetched_at": created_at}
    return list(latest.values())


async def refresh_announcements(repositories: Any, broker: Any, symbol: str, credit_date: date, now: datetime) -> int:
    """Fetch [credit date - 90 days, credit date] and store what is new."""
    rows = await broker.get_dividend_announcements(symbol, credit_date - WINDOW, credit_date)
    return await repositories.audit_events.database.run(lambda c: store_announcements(c, symbol, rows, now), write=True)
