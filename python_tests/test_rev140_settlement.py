"""Rev.140 task 7: an actual oversell settles as a quarantined liability (spec §3.2, tests 5, 7, 20)."""
from __future__ import annotations

import sqlite3
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from rev140_support import AAPL, Rt

from tradepulse.models import (
    AssetClass,
    ExecutionMode,
    Fill,
    PositionLot,
    SessionState,
    SettlementEvent,
    SettlementStatus,
    Side,
)
from tradepulse.persistence import hydrate, paginate_all_rows
from tradepulse.persistence.codec import decode_payload, encode_payload
from tradepulse.risk import load_session
from tradepulse.settlement.accounting import pnl_evidence


async def _rows(rt: Rt, table: str) -> list[Any]:
    return [hydrate(table, row["payload"]) for row in await paginate_all_rows(getattr(rt.repositories, table))]


async def _oversell(rt: Rt, held: str = "10", extra: str = "2") -> Any:
    await rt.buy(held)
    rt.quote()
    rt.broker.overfill_next(Decimal(extra))
    result = await rt.sell(held, "exit", decision="oversell")
    rt.quote()
    await rt.settlement.process_pending(force_retry=True)
    return result


async def test_oversell_twelve_against_ten(rt: Rt) -> None:
    """Spec test 5: the broker executes 12 against 10 held."""
    await _oversell(rt)
    assert await rt.position() == Decimal(-2)
    sell_fills = [f for f in await _rows(rt, "fills") if f.side == Side.SELL]
    assert [f.quantity for f in sell_fills] == [Decimal(12)]
    fill = sell_fills[0]
    (liability,) = await _rows(rt, "short_exposure_liabilities")
    assert (liability.fill_id, liability.quantity, liability.multiplier) == (fill.fill_id, Decimal(2), Decimal(1))
    assert liability.proceeds == Decimal(2) * fill.price
    lots = await _rows(rt, "position_lots")
    assert not [lot for lot in lots if lot.position_side == "short"]
    cash = [e for e in await _rows(rt, "cash_ledger") if e.idempotency_key == "fill:cash:" + fill.fill_id]
    assert len(cash) == 1 and cash[0].amount == Decimal(12) * fill.price
    session = await load_session(rt.repositories)
    assert session.state == SessionState.FINANCIAL_INTEGRITY_BLOCKED
    assert f"UNSUPPORTED_SHORT_POSITION: {liability.liability_id}" in (session.financial_integrity_reason or "")
    holds = await _rows(rt, "integrity_holds")
    assert [h.hold_type.value for h in holds] == ["short_exposure"]
    assert [e.event_type for e in await _rows(rt, "audit_events")].count("unsupported_short_position") == 1  # alerted once
    (event,) = [e for e in await _rows(rt, "settlements") if e.fill_id == fill.fill_id]
    assert (event.status, event.liability_hold_projected) == (SettlementStatus.COMPLETED, True)


async def test_closure_conservation_includes_liability(rt: Rt) -> None:
    await _oversell(rt)
    (event,) = [e for e in await _rows(rt, "settlements") if e.side == Side.SELL]

    def evidence(connection: sqlite3.Connection) -> Any:
        connection.row_factory = sqlite3.Row
        return pnl_evidence(connection, event)

    await rt.database.run(evidence)
    await rt.database.run(lambda c: c.executescript(
        "DROP TRIGGER quarantine_no_delete_short_exposure_liabilities; DELETE FROM short_exposure_liabilities;"), write=True)
    with pytest.raises(Exception, match="ACCOUNTING_CLOSURE_CONSERVATION_FAILED"):
        await rt.database.run(evidence)


async def test_crash_after_project_lot_and_after_cash(rt: Rt, monkeypatch: pytest.MonkeyPatch) -> None:
    """Spec test 7: a crash between stages resumes to exactly one of everything."""
    from tradepulse.settlement import engine

    original = engine.project_accounting
    crashes = {"n": 0}

    async def crash_once(repositories: Any, state: Any, **kwargs: Any) -> Any:
        if kwargs.get("trade") and crashes["n"] == 0:
            crashes["n"] += 1
            raise RuntimeError("crash after cash")
        return await original(repositories, state, **kwargs)

    monkeypatch.setattr(engine, "project_accounting", crash_once)
    await _oversell(rt)
    rt.clock.advance(timedelta(minutes=10))
    await rt.settlement.process_pending(force_retry=True)
    assert crashes["n"] == 1
    assert len(await _rows(rt, "short_exposure_liabilities")) == 1
    (fill,) = [f for f in await _rows(rt, "fills") if f.side == Side.SELL]
    assert len([e for e in await _rows(rt, "cash_ledger") if e.idempotency_key == "fill:cash:" + fill.fill_id]) == 1
    assert len(await _rows(rt, "integrity_holds")) == 1
    assert [e.event_type for e in await _rows(rt, "audit_events")].count("unsupported_short_position") == 1  # alerted once


@pytest.mark.parametrize("stage", ["completed", "lot_projected", "integrity_verified"])
async def test_events_checkpointed_before_rev140_resume(rt: Rt, stage: str) -> None:
    """Spec test 20, at payload level: a settlement written by the code
    before Rev.140 has no liability_hold_projected flag. A completed one
    stays complete; one stopped at any stage resumes through the new final
    stage and completes."""
    await rt.buy("5")
    (event,) = await _rows(rt, "settlements")
    payload = dict(decode_payload(encode_payload(event)))
    payload.pop("liability_hold_projected")
    if stage != "completed":
        flags = ["lot_projected", "attribution_projected", "cash_projected", "holding_projected", "trade_projected", "integrity_verified"]
        for flag in flags[flags.index(stage) + 1:]:
            payload[flag] = False
        payload["status"] = "pending"
    legacy = hydrate("settlements", payload)
    assert legacy.liability_hold_projected is (stage == "completed")
    if stage != "completed":
        await rt.database.run(lambda c: c.execute("UPDATE settlements SET payload=?, status='pending' WHERE record_id=?",
                                                  (encode_payload(payload).replace(',"liability_hold_projected":false', ''),
                                                   legacy.settlement_event_id)), write=True)
        await rt.settlement.process_pending(force_retry=True)
    (resumed,) = await _rows(rt, "settlements")
    assert (resumed.status, resumed.liability_hold_projected) == (SettlementStatus.COMPLETED, True)
    from tradepulse.settlement.accounting import accounting_issues
    assert await accounting_issues(rt.repositories) == {}


async def test_option_oversell_uses_multiplier(rt: Rt) -> None:
    """Review Focus 4: one contract oversold; proceeds carry the ×100."""
    from tradepulse.models import AssetIdentity

    occ = "AAPL261120C00200000"
    option = AssetIdentity(occ, AssetClass.OPTION, f"alpaca:{occ}", contract_multiplier=Decimal(100))
    rt.broker.seed_position(occ, AssetClass.OPTION, Decimal(1), Decimal("4.00"), multiplier=Decimal(100))
    lot = PositionLot(lot_id="option-lot", originating_fill_id="option-fill", asset=option, position_side="long",
                      opened_quantity=Decimal(1), remaining_quantity=Decimal(1), acquisition_price=Decimal("4.00"),
                      opened_at=rt.clock())
    opening = Fill(fill_id="option-fill", trade_intent_id="option-intent", order_id="option-order", asset=option, side=Side.BUY,
                   execution_mode=ExecutionMode.PAPER, quantity=Decimal(1), price=Decimal("4.00"), fees=Decimal(0),
                   slippage=Decimal(0), filled_at=rt.clock(), broker_fill_id="option-fill")
    await rt.repositories.fills.create_once(opening.fill_id, opening, unique_value=opening.broker_fill_id)
    await rt.repositories.position_lots.create_once(lot.lot_id, lot, unique_value=lot.originating_fill_id)
    event = SettlementEvent(settlement_event_id="settle-option", fill_id="option-sell", trade_intent_id="option-exit",
                            asset=option, side=Side.SELL, execution_mode=ExecutionMode.PAPER, quantity=Decimal(2),
                            price=Decimal("5.00"), occurred_at=rt.clock(), broker_order_id="option-exit-order")
    from tradepulse.settlement.engine import _project_lot

    await _project_lot(rt.repositories, event)
    (liability,) = await _rows(rt, "short_exposure_liabilities")
    assert (liability.quantity, liability.multiplier, liability.proceeds) == (Decimal(1), Decimal(100), Decimal(500))


async def test_buy_against_legacy_short_lot_blocked(rt: Rt) -> None:
    short = PositionLot(lot_id="legacy-short", originating_fill_id="legacy-fill", asset=AAPL, position_side="short",
                        opened_quantity=Decimal(2), remaining_quantity=Decimal(2), acquisition_price=Decimal(200),
                        opened_at=rt.clock())
    await rt.repositories.position_lots.create_once(short.lot_id, short, unique_value=short.originating_fill_id)
    event = SettlementEvent(settlement_event_id="settle-cover", fill_id="cover-fill", trade_intent_id="cover",
                            asset=AAPL, side=Side.BUY, execution_mode=ExecutionMode.PAPER, quantity=Decimal(2),
                            price=Decimal(199), occurred_at=rt.clock(), broker_order_id="cover-order")
    from tradepulse.settlement.engine import _project_lot

    with pytest.raises(Exception, match="INTEGRITY_VIOLATION: UNSUPPORTED_SHORT_LOT"):
        await _project_lot(rt.repositories, event)
    (unchanged,) = [lot for lot in await _rows(rt, "position_lots") if lot.lot_id == "legacy-short"]
    assert unchanged.remaining_quantity == Decimal(2)
