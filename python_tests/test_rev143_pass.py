"""Rev.143 task 4: the attribution pass, escalation, run equity and recovery
(spec §3.4 step 7, §3.6, §3.7; T4, T13-T15, T18, T19, T21, T22, T24-T26)."""
from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tradepulse.alerts import TelegramAlerter
from tradepulse.calendar import BrokerSession, MarketCalendar, Venue, ny_time
from tradepulse.calendar.checks import run_check
from tradepulse.models import AssetClass, AssetIdentity, ExecutionMode, Fill, PositionLot, Side
from tradepulse.persistence import AsyncSQLiteDatabase, PersistenceRepositories, paginate_all_rows
from tradepulse.persistence.codec import encode_payload
from tradepulse.reconciliation import dividend_attribution as attribution
from tradepulse.reconciliation.dividend_attribution import (
    attribute_dividends,
    dividend_equity,
    strategy_dividend_income,
)
from tradepulse.reconciliation.generation_fees import persist_generation_fees

CAL = MarketCalendar.from_library()
GROSS = "gross_and_separate_withholding"
AAPL = AssetIdentity("AAPL", AssetClass.EQUITY, "alpaca:AAPL")
OBSERVED = datetime(2026, 11, 13, 15, tzinfo=UTC)


def div(identifier: str, symbol: str = "AAPL", amount: str = "2.60", qty: str = "10", rate: str = "0.26", **fields: Any) -> dict[str, Any]:
    return {"id": identifier, "activity_type": "DIV", "date": "2026-11-13", "created_at": "2026-11-13T09:00:00-05:00",
            "net_amount": amount, "per_share_amount": rate, "qty": qty, "symbol": symbol, "status": "executed", "currency": "USD", **fields}


def ann(identifier: str, symbol: str, cash: str, ex: str = "2026-11-10", payable: str = "2026-11-13") -> dict[str, Any]:
    return {"id": identifier, "ca_type": "dividend", "ca_sub_type": "cash", "initiating_symbol": symbol, "target_symbol": symbol,
            "ex_date": ex, "record_date": ex, "payable_date": payable, "cash": cash}


class Broker:
    venue = "alpaca"

    def __init__(self, announcements: list[dict[str, Any]] | None = None) -> None:
        self.announcements = announcements or []
        self.fetches = 0

    async def get_dividend_announcements(self, symbol: str, since: date, until: date) -> list[dict[str, Any]]:
        self.fetches += 1
        return [a for a in self.announcements if a["initiating_symbol"] == symbol]


@pytest.fixture
async def repositories(tmp_path: Path) -> PersistenceRepositories:
    database = AsyncSQLiteDatabase(f"sqlite:///{tmp_path}/div.db")
    await database.initialize()
    return PersistenceRepositories.create(database)


async def record(repositories: PersistenceRepositories, rows: list[dict[str, Any]], now: datetime = OBSERVED) -> None:
    membership = {"checkpoint": None, "classifications": {r["id"]: "in_generation" for r in rows}, "population_hash": "p"}
    await repositories.audit_events.database.run(lambda c: persist_generation_fees(c, rows, membership, now=now), write=True)


async def strategy_lot(repositories: PersistenceRepositories, lot_id: str, qty: str, opened: datetime, asset: AssetIdentity = AAPL) -> None:
    fill = Fill(f"fill-{lot_id}", f"intent-{lot_id}", f"order-{lot_id}", asset, Side.BUY, ExecutionMode.PAPER, Decimal(qty),
                Decimal(200), Decimal(0), Decimal(0), opened, broker_fill_id=f"act-{lot_id}")
    await repositories.fills.create_once(fill.fill_id, fill, unique_value=fill.broker_fill_id)
    lot = PositionLot(lot_id=lot_id, originating_fill_id=fill.fill_id, asset=asset, position_side="long", opened_quantity=Decimal(qty),
                      remaining_quantity=Decimal(qty), acquisition_price=Decimal(200), opened_at=opened)
    await repositories.position_lots.create_once(lot_id, lot, unique_value=fill.fill_id)


async def statuses(repositories: PersistenceRepositories) -> dict[str, Any]:
    return await repositories.audit_events.database.run(attribution.statuses)


async def events(repositories: PersistenceRepositories, kind: str) -> list[Any]:
    return [r for r in await paginate_all_rows(repositories.audit_events) if r["payload"]["event_type"] == kind]


async def verified_calendar(repositories: PersistenceRepositories, at: datetime) -> None:
    class Calendar:
        async def get_calendar(self, start: date, end: date) -> list[BrokerSession]:
            return [BrokerSession(Venue.US_EQUITY, s.date, s.open, s.close, "t") for s in CAL.sessions_between(Venue.US_EQUITY, start, end)]

    await run_check(repositories, Calendar(), "alpaca", Venue.US_EQUITY, CAL, TelegramAlerter(None, None), at)


async def test_strategy_dividend_verified_and_counted(repositories: PersistenceRepositories) -> None:
    """T15, T18: pending from the first observation (in D_unresolved), then
    verified; strategy income kept apart from price P&L; info alert."""
    await strategy_lot(repositories, "L1", "10", datetime(2026, 11, 2, 15, tzinfo=UTC))
    await record(repositories, [div("d1")])
    pending = await repositories.audit_events.database.run(dividend_equity)
    assert (pending["d_unresolved"], pending["unresolved_activity_ids"]) == (Decimal("2.60"), ["d1"])
    results = await attribute_dividends(repositories, Broker([ann("a1", "AAPL", "0.26")]), TelegramAlerter(None, None), OBSERVED,
                                        representation=GROSS)
    assert results == {"d1": "verified"}
    equity = await repositories.audit_events.database.run(dividend_equity)
    assert (equity["d_excl"], equity["d_unresolved"]) == (Decimal(0), Decimal(0))
    assert await repositories.audit_events.database.run(strategy_dividend_income) == {"L1": Decimal("2.60")}
    assert [e["payload"]["message"] for e in await events(repositories, "dividend_attributed")] == ["Dividend AAPL $2.60 — strategy"]
    assert await attribute_dividends(repositories, Broker(), TelegramAlerter(None, None), OBSERVED, representation=GROSS) == {}


async def test_unknown_representation_records_cash_and_refuses(repositories: PersistenceRepositories) -> None:
    """T31 through the pass with the real constant: no attribution row, ever."""
    await strategy_lot(repositories, "L1", "10", datetime(2026, 11, 2, 15, tzinfo=UTC))
    await record(repositories, [div("d1")])
    broker = Broker([ann("a1", "AAPL", "0.26")])
    assert await attribute_dividends(repositories, broker, TelegramAlerter(None, None), OBSERVED) == {"d1": "unresolved"}
    assert broker.fetches == 0
    assert await repositories.audit_events.database.run(attribution.attribution_rows) == []
    assert (await statuses(repositories))["d1"]["reason"] == "DIVIDEND_REPRESENTATION_UNKNOWN"


async def test_unsupported_type_alerts_once(repositories: PersistenceRepositories) -> None:
    """T4 (the alert)."""
    await record(repositories, [div("roc", activity_type="DIVROC")])
    for hours in (0, 1):
        await attribute_dividends(repositories, Broker(), TelegramAlerter(None, None), OBSERVED + timedelta(hours=hours), representation=GROSS)
    assert len(await events(repositories, "dividend_type_unsupported")) == 1


async def test_escalation_on_the_trading_calendar(repositories: PersistenceRepositories) -> None:
    """T24, T26: first seen Wednesday 11-25; Thanksgiving is not a session and
    11-27 counts, so the deadline is Tuesday 12-01's close. The critical
    alert fires once at the deadline; a later match verifies and sends an
    info alert, without repeating the critical one. Announcements are
    re-fetched at most once per trading day while pending."""
    first = datetime(2026, 11, 25, 15, tzinfo=UTC)
    await verified_calendar(repositories, first)
    await strategy_lot(repositories, "L1", "10", datetime(2026, 11, 2, 15, tzinfo=UTC))
    await record(repositories, [div("d1")], first)
    broker = Broker()
    alerts = TelegramAlerter(None, None)
    await attribute_dividends(repositories, broker, alerts, first, representation=GROSS)
    await attribute_dividends(repositories, broker, alerts, first + timedelta(hours=2), representation=GROSS)
    status = (await statuses(repositories))["d1"]
    assert (status["escalation_deadline"], status["deadline_source"]) == (ny_time(date(2026, 12, 1), time(16)).isoformat(), "market_calendar")
    assert broker.fetches == 1  # once that trading day
    await attribute_dividends(repositories, broker, alerts, ny_time(date(2026, 12, 1), time(15, 59)), representation=GROSS)
    assert await events(repositories, "dividend_attribution_unresolved") == []
    deadline = ny_time(date(2026, 12, 1), time(16))
    for minutes in (0, 30):
        await attribute_dividends(repositories, broker, alerts, deadline + timedelta(minutes=minutes), representation=GROSS)
    assert len(await events(repositories, "dividend_attribution_unresolved")) == 1
    assert (await statuses(repositories))["d1"]["attribution_status"] == "unresolved"
    broker.announcements = [ann("a1", "AAPL", "0.26")]
    later = deadline + timedelta(days=1)
    assert await attribute_dividends(repositories, broker, alerts, later, representation=GROSS) == {"d1": "verified"}
    assert len(await events(repositories, "dividend_attributed")) == 1
    assert len(await events(repositories, "dividend_attribution_unresolved")) == 1


async def test_deadline_is_a_weekday_bound_when_the_calendar_is_unverified(repositories: PersistenceRepositories) -> None:
    """T25: with no verified calendar, the earlier of calendar and three weekdays (here Monday 11-30)."""
    first = datetime(2026, 11, 25, 15, tzinfo=UTC)
    await record(repositories, [div("d1")], first)
    await attribute_dividends(repositories, Broker(), TelegramAlerter(None, None), first, representation=GROSS)
    status = (await statuses(repositories))["d1"]
    assert (status["escalation_deadline"], status["deadline_source"]) == (ny_time(date(2026, 11, 30), time(16)).isoformat(), "weekday_bound")


async def test_quarantined_instrument_keeps_its_hold(repositories: PersistenceRepositories) -> None:
    """T19: cash recorded, DIVIDEND_ON_QUARANTINED_INSTRUMENT."""
    from tradepulse.models.quarantine import ShortExposureLiability

    liability = ShortExposureLiability(liability_id="liability:x", fill_id="x", broker_order_id="o", trade_intent_id="i", asset=AAPL,
                                       quantity=Decimal(2), sale_price=Decimal(100), multiplier=Decimal(1), proceeds=Decimal(200),
                                       opened_at=OBSERVED - timedelta(days=5))
    await repositories.short_exposure_liabilities.create_once(liability.liability_id, liability, unique_value="x")
    await record(repositories, [div("d1")])
    await attribute_dividends(repositories, Broker([ann("a1", "AAPL", "0.26")]), TelegramAlerter(None, None), OBSERVED, representation=GROSS)
    assert (await statuses(repositories))["d1"]["reason"] == "DIVIDEND_ON_QUARANTINED_INSTRUMENT"
    assert len([r for r in await paginate_all_rows(repositories.cash_ledger) if r["record_id"] == "broker:dividend:d1"]) == 1


async def test_worked_example_inherited_and_strategy(repositories: PersistenceRepositories, monkeypatch: pytest.MonkeyPatch) -> None:
    """T13, T14, T21: the run opened holding 13.252 MSFT (unadopted); the
    strategy bought 10 AAPL before AAPL's ex-date. MSFT $12.06 goes to
    opening_unadopted (D_excl); AAPL $2.60 is strategy; cash +$14.66. A
    pre-run ex-date has no E1: excluded_unallocated, reported, not unresolved."""
    checkpoint = {"opened_at": "2026-11-02T14:00:00+00:00", "verification_generation_id": "g", "checkpoint_id": "c",
                  "positions": [{"asset_class": "equity", "symbol": "MSFT", "qty": "13.252"}]}
    monkeypatch.setattr("tradepulse.verification.opening.load_bound_opening_checkpoint", lambda connection: checkpoint)
    await strategy_lot(repositories, "L1", "10", datetime(2026, 11, 3, 15, tzinfo=UTC))
    rows = [div("aapl"), div("msft", symbol="MSFT", amount="12.06", qty="13.252", rate="0.91"),
            div("old", symbol="JPM", amount="1.50", qty="1", rate="1.5")]
    await record(repositories, rows)
    population = {"record_id": "generation_membership:x", "reconciliation_type": "generation_membership", "subject_id": "x",
                  "outcome": "matched", "expected": {}, "occurred_at": OBSERVED.isoformat(),
                  "actual": {"activities": rows, "classifications": {r["id"]: "in_generation" for r in rows}}}
    await repositories.audit_events.database.run(lambda c: c.execute(
        "INSERT INTO reconciliation_records(record_id,payload,created_at) VALUES(?,?,?)",
        ("generation_membership:x", encode_payload(population), OBSERVED.isoformat())), write=True)
    broker = Broker([ann("a1", "AAPL", "0.26"), ann("m1", "MSFT", "0.91", ex="2026-11-12"),
                     ann("j1", "JPM", "1.5", ex="2026-10-06", payable="2026-11-13")])
    results = await attribute_dividends(repositories, broker, TelegramAlerter(None, None), OBSERVED, representation=GROSS)
    assert results == {"aapl": "verified", "msft": "verified", "old": "excluded_unallocated"}
    equity = await repositories.audit_events.database.run(dividend_equity)
    assert equity["d_excl"] == Decimal("13.56")  # MSFT 12.06 (opening_unadopted) + JPM 1.50 (excluded_unallocated)
    assert (equity["d_unresolved"], equity["entitlement_not_established"]) == (Decimal(0), ["old"])
    cash = sum(Decimal(r["payload"]["amount"]) for r in await paginate_all_rows(repositories.cash_ledger)
               if r["record_id"].startswith("broker:dividend:"))
    assert cash == Decimal("16.16")
    assert cash - equity["d_excl"] == Decimal("2.60")  # run equity includes the strategy's $2.60 only
    buckets = {(r["activity_id"], r["bucket"]) for r in await repositories.audit_events.database.run(attribution.attribution_rows)}
    assert buckets == {("aapl", "strategy"), ("msft", "opening_unadopted"), ("old", "excluded_unallocated")}


async def test_crash_after_recording_attributes_once(repositories: PersistenceRepositories, monkeypatch: pytest.MonkeyPatch) -> None:
    """T22: a crash inside the attribution commit writes nothing; the retry
    attributes exactly once."""
    await strategy_lot(repositories, "L1", "10", datetime(2026, 11, 2, 15, tzinfo=UTC))
    await record(repositories, [div("d1")])
    real = attribution.write_status

    def crash(*args: Any) -> None:
        raise RuntimeError("crash")

    monkeypatch.setattr(attribution, "write_status", crash)
    with pytest.raises(RuntimeError):
        await attribute_dividends(repositories, Broker([ann("a1", "AAPL", "0.26")]), TelegramAlerter(None, None), OBSERVED,
                                  representation=GROSS)
    assert await repositories.audit_events.database.run(attribution.attribution_rows) == []
    monkeypatch.setattr(attribution, "write_status", real)
    for _ in range(2):
        await attribute_dividends(repositories, Broker([ann("a1", "AAPL", "0.26")]), TelegramAlerter(None, None), OBSERVED,
                                  representation=GROSS)
    assert len(await repositories.audit_events.database.run(attribution.attribution_rows)) == 1


async def test_reversal_through_the_pass(repositories: PersistenceRepositories) -> None:
    """T17: a full reversal mirrors; a reissue after it is attributed as new."""
    await strategy_lot(repositories, "L1", "10", datetime(2026, 11, 2, 15, tzinfo=UTC))
    broker = Broker([ann("a1", "AAPL", "0.26")])
    await record(repositories, [div("d1")])
    await attribute_dividends(repositories, broker, TelegramAlerter(None, None), OBSERVED, representation=GROSS)
    await record(repositories, [div("r1", amount="-2.60", date="2026-11-14")])
    assert await attribute_dividends(repositories, broker, TelegramAlerter(None, None), OBSERVED, representation=GROSS) == {"r1": "verified"}
    total = sum(Decimal(r["amount"]) for r in await repositories.audit_events.database.run(attribution.attribution_rows))
    assert total == 0
    await record(repositories, [div("p1", amount="-1.30", date="2026-11-15")])  # partial
    assert (await attribute_dividends(repositories, broker, TelegramAlerter(None, None), OBSERVED, representation=GROSS))["p1"] == "unresolved"
