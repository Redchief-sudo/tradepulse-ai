"""Rev.143 task 2: dividend announcements on the broker port, stored by version (§3.4 step 1)."""
from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import respx

from tradepulse.broker import AlpacaClient
from tradepulse.broker.simulated import ManualClock, SimSession, SimulatedBroker
from tradepulse.persistence.database import SCHEMA
from tradepulse.reconciliation.announcements import store_announcements, stored_announcements

NOW = datetime(2026, 11, 13, 15, tzinfo=UTC)
AAPL = {"id": "56ec0951-6cc7-4d0d-8f7d-98a9676ff1e3", "corporate_action_id": "037833100_AC26", "ca_type": "dividend",
        "ca_sub_type": "cash", "initiating_symbol": "AAPL", "initiating_original_cusip": "037833100", "target_symbol": "AAPL",
        "target_original_cusip": "037833100", "declaration_date": "2026-08-06", "effective_date": "2026-08-10",
        "ex_date": "2026-08-10", "record_date": "2026-08-10", "payable_date": "2026-08-13", "cash": "0.27",
        "old_rate": "1", "new_rate": "1"}  # recorded at G0 (evidence 2026-10-29-rev143-g0)


@respx.mock
async def test_alpaca_announcements_request_and_shape() -> None:
    route = respx.get("https://paper-api.alpaca.markets/v2/corporate_actions/announcements").mock(
        return_value=httpx.Response(200, content=json.dumps([AAPL]).encode()))
    rows = await AlpacaClient("key", "secret", "paper", 10).get_dividend_announcements("AAPL", date(2026, 8, 15), date(2026, 11, 13))
    assert rows == [AAPL] and isinstance(rows[0]["cash"], str)
    params = route.calls.last.request.url.params
    assert (params["ca_types"], params["symbol"], params["since"], params["until"]) == ("dividend", "AAPL", "2026-08-15", "2026-11-13")


async def test_simulator_returns_the_symbol_s_scripted_announcements() -> None:
    broker = SimulatedBroker(clock=ManualClock(NOW), session=SimSession.always_open())
    broker.set_dividend_announcements([AAPL, {**AAPL, "id": "x", "initiating_symbol": "MSFT", "target_symbol": "MSFT"}])
    assert [row["id"] for row in await broker.get_dividend_announcements("aapl", NOW.date(), NOW.date())] == [AAPL["id"]]


def test_versions_are_kept_and_the_latest_is_used(tmp_path: Path) -> None:
    """An amended announcement is a new row; replaying a version adds nothing."""
    connection = sqlite3.connect(":memory:")
    connection.executescript(SCHEMA)
    assert store_announcements(connection, "AAPL", [AAPL], NOW) == 1
    assert store_announcements(connection, "AAPL", [AAPL], NOW + timedelta(hours=1)) == 0
    amended = {**AAPL, "payable_date": "2026-08-14"}
    assert store_announcements(connection, "AAPL", [amended], NOW + timedelta(hours=2)) == 1
    [latest] = stored_announcements(connection, "AAPL")
    assert latest["raw"]["payable_date"] == "2026-08-14"
    assert connection.execute("SELECT COUNT(*) FROM corporate_action_announcements").fetchone()[0] == 2
