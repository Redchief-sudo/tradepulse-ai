"""Sanitized receipt-bearing broker fixture; never a live-account adapter."""
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256

from tradepulse.broker.types import AlpacaAccount, AlpacaActivity
from tradepulse.persistence.codec import encode_payload


class OpeningBroker:
    def __init__(self, activities=(), *, now=None, positions=()):
        self.now = now or datetime.now(UTC)
        self.activities = list(activities)
        self.positions = list(positions)
        self.last_positions_received_at = self.now
        self.account = AlpacaAccount(
            equity=Decimal("100000"), last_equity=Decimal("100000"), cash=Decimal("100000"),
            buying_power=Decimal("100000"), portfolio_value=Decimal("100000"),
            received_at=self.now, account_id="sanitized-account-id", account_number="sanitized-account-number",
            raw={"id": "sanitized-account-id", "cash": "100000", "equity": "100000"},
        )

    async def get_account(self):
        return self.account

    async def get_positions(self):
        return self.positions

    async def get_activities(self, activity_type=None, *, page_evidence=None, after_id=None):
        rows = self.activities
        if after_id is not None:
            offset = next(i for i, row in enumerate(rows) if row["id"] == after_id)
            rows = rows[offset + 1:]
        boundary = after_id
        for index in range(0, len(rows) + 1, 100):
            chunk = rows[index:index + 100]
            request = {"page_size": "100", "direction": "asc"}
            if boundary is not None:
                request["page_token"] = boundary
            if page_evidence is not None:
                page_evidence.append({"request": request, "activity_ids": [row["id"] for row in chunk],
                                      "response_hash": sha256(encode_payload(chunk).encode()).hexdigest(),
                                      "terminal": len(chunk) < 100, "received_at": self.now.isoformat()})
            if len(chunk) < 100:
                break
            boundary = chunk[-1]["id"]
        return [AlpacaActivity(activity_id=row["id"], activity_type=row["activity_type"], symbol=row.get("symbol", ""),
                               side=None, qty=None, price=None, transaction_time=None, raw=row) for row in rows]
