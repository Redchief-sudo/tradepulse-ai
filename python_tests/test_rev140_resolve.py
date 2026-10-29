"""Rev.140 task 11: `quarantine resolve` (spec §3.7, §3.8, tests 12-14, 16, 17)."""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from rev140_support import AAPL, KEY, Rt

from tradepulse import cli
from tradepulse.broker import AlpacaActivity
from tradepulse.broker.port import AccountRef
from tradepulse.broker.simulated import Fault
from tradepulse.broker.types import AlpacaOrderRequest
from tradepulse.models import AssetClass, Side
from tradepulse.models.quarantine import ShortExposureLiability, UnresolvedBrokerExecution
from tradepulse.persistence import hydrate, paginate_all_rows
from tradepulse.persistence.codec import encode_payload
from tradepulse.quarantine import quarantined_positions
from tradepulse.quarantine.release import active_hold_count, released_short_exposure_holds, reset_gate_issues
from tradepulse.quarantine.resolve import ResolveRefused, resolve_from_activity
from tradepulse.reconciliation import run_reconciliation
from tradepulse.runtime_lease import account_lease, lease_directory
from tradepulse.settlement.accounting import accounting_issues, replay_accounting

TABLES = ("quarantine_executions", "quarantine_resolutions", "unmanaged_long_inventory", "quarantine_fee_recognitions",
          "cash_ledger", "audit_events", "unresolved_execution_outcomes")


async def _external(rt: Rt, side: Side, quantity: str, coid: str, symbol: str = "AAPL") -> str:
    """An order nobody in TradePulse placed; returns its FILL activity ID."""
    order = await rt.broker.place_order(AlpacaOrderRequest(symbol=symbol, qty=Decimal(quantity), side=side, client_order_id=coid))
    rt.clock.advance(timedelta(seconds=2))
    [activity] = [a for a in await rt.broker.get_activities(activity_type="FILL") if a.raw.get("order_id") == order.broker_order_id]
    return activity.activity_id


async def _resolve(rt: Rt, activity_id: str, reason: str = "operator cover") -> Any:
    return await resolve_from_activity(rt.repositories, rt.broker, rt.gateway._lease, activity_id, reason, rt.clock())


async def _liability(rt: Rt, identifier: str, quantity: str, price: str = "200", *, ago: int = 60) -> None:
    item = ShortExposureLiability(liability_id=f"liability:{identifier}", fill_id=identifier, broker_order_id=f"o-{identifier}",
                                  trade_intent_id=f"i-{identifier}", asset=AAPL, quantity=Decimal(quantity),
                                  sale_price=Decimal(price), multiplier=Decimal(1),
                                  proceeds=Decimal(quantity) * Decimal(price), opened_at=rt.clock() - timedelta(seconds=ago))
    await rt.repositories.short_exposure_liabilities.create_once(item.liability_id, item, unique_value=identifier)


async def _oversell(rt: Rt, bid: str = "100", ask: str = "100.10") -> None:
    """Buy 10, then a sell the broker overfills by 2: a 2-share liability."""
    await rt.buy("10")
    rt.quote(bid, ask)
    rt.broker.overfill_next(Decimal(2))
    await rt.sell("10", "exit", decision="oversell")
    rt.quote(bid, ask)
    await rt.settlement.process_pending(force_retry=True)


async def _counts(rt: Rt) -> dict[str, int]:
    return {table: len(await paginate_all_rows(getattr(rt.repositories, table))) for table in TABLES}


async def _cash(rt: Rt, activity_id: str) -> list[Decimal]:
    return [Decimal(r["payload"]["amount"]) for r in await paginate_all_rows(rt.repositories.cash_ledger)
            if r["record_id"] == "fill:cash:" + activity_id]


async def test_partial_covers_oldest_first(rt: Rt) -> None:
    """Spec test 12 (its numbers corrected, see the Rev record): liabilities
    of 2 (older) and 3; a cover of 4 resolves the 2, then 2 of the 3; a cover
    of 1 resolves the rest. The reset gates refuse until the end."""
    await _liability(rt, "older", "2", ago=120)
    await _liability(rt, "newer", "3", ago=60)
    rt.quote("189.90", "190")
    first = await _resolve(rt, await _external(rt, Side.BUY, "4", "cover-1"))
    assert [(r.record_id, r.quantity, r.realized_pnl) for r in first.resolved] == [
        ("liability:older", Decimal(2), Decimal(20)), ("liability:newer", Decimal(2), Decimal(20))]
    assert [(p.record_id, p.open_quantity) for p in await quarantined_positions(rt.repositories)] == [("liability:newer", Decimal(1))]
    assert await reset_gate_issues(rt.repositories) == ["QUARANTINE_OPEN: liability:newer"]
    second = await _resolve(rt, await _external(rt, Side.BUY, "1", "cover-2"))
    assert [(r.record_id, r.quantity) for r in second.resolved] == [("liability:newer", Decimal(1))]
    assert second.surplus is None
    assert await quarantined_positions(rt.repositories) == []
    assert await reset_gate_issues(rt.repositories) == []


async def test_worked_example_200_minus_180_plus_20(rt: Rt) -> None:
    """Spec test 13: short 2 at $100, cover 2 at $90. Cash +$200 and -$180,
    one entry each; realized +$20 before fees; open quantity 0."""
    await _oversell(rt)
    [liability] = await quarantined_positions(rt.repositories)
    assert (liability.open_quantity, liability.reference_price) == (Decimal(2), Decimal(100))
    assert await rt.database.run(active_hold_count) == 1
    rt.quote("89.90", "90")
    cover = await _external(rt, Side.BUY, "2", "operator-cover")
    result = await _resolve(rt, cover)
    assert [(r.quantity, r.realized_pnl) for r in result.resolved] == [(Decimal(2), Decimal(20))]
    assert result.surplus is None
    assert await _cash(rt, cover) == [Decimal(-180)]
    assert await quarantined_positions(rt.repositories) == []
    assert await accounting_issues(rt.repositories) == {}
    assert await reset_gate_issues(rt.repositories) == []
    [hold] = await paginate_all_rows(rt.repositories.integrity_holds)
    assert await rt.database.run(released_short_exposure_holds) == [hold["record_id"]]
    assert await rt.database.run(active_hold_count) == 0  # released at reset, atomically with the transition


@pytest.mark.parametrize("order", ["reconcile_before_and_after", "crash_then_retry", "twice", "replay_after"])
async def test_exactly_once_every_recovery_order(rt: Rt, order: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Spec test 14: one execution row and one cash entry, whatever the order;
    missed fills return to 0 only through the evidence."""
    await _oversell(rt)
    rt.quote("89.90", "90")
    cover = await _external(rt, Side.BUY, "2", "operator-cover")
    reconcile = lambda: run_reconciliation(rt.repositories, rt.broker, rt.settlement, rt.alerts, clock=lambda: rt.clock())
    if order == "reconcile_before_and_after":
        assert (await reconcile()).missed_fills_detected == 1
    if order == "crash_then_retry":
        import tradepulse.quarantine.resolve as resolve_module

        real = resolve_module.AuditEvent

        def crash(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("crash inside the transaction")

        monkeypatch.setattr(resolve_module, "AuditEvent", crash)
        before = await _counts(rt)
        with pytest.raises(RuntimeError):
            await _resolve(rt, cover)
        assert await _counts(rt) == before
        monkeypatch.setattr(resolve_module, "AuditEvent", real)
    await _resolve(rt, cover)
    if order == "twice":
        with pytest.raises(ResolveRefused, match="ACTIVITY_ALREADY_USED"):
            await _resolve(rt, cover)
    if order == "replay_after":
        assert await replay_accounting(rt.repositories) == 0
    summary = await reconcile()
    assert summary.missed_fills_detected == 0
    assert len(await paginate_all_rows(rt.repositories.quarantine_executions)) == 1
    assert await _cash(rt, cover) == [Decimal(-180)]


async def test_surplus_becomes_unmanaged_long(rt: Rt) -> None:
    """Spec test 16: a cover of 5 against a liability of 2. One cash entry for
    all 5; a 3-share unmanaged long from the same execution; the hold stays
    until a verified sale of 3 resolves it."""
    await _oversell(rt)
    rt.quote("89.90", "90")
    cover = await _external(rt, Side.BUY, "5", "big-cover")
    result = await _resolve(rt, cover)
    assert result.surplus is not None
    assert (result.surplus.quantity, result.surplus.cost_price, result.surplus.cover_activity_id) == (Decimal(3), Decimal(90), cover)
    assert await _cash(rt, cover) == [Decimal(-450)]
    assert await rt.database.run(released_short_exposure_holds) == []
    sale = await _external(rt, Side.SELL, "3", "surplus-sale")
    [resolution] = (await _resolve(rt, sale)).resolved
    assert (resolution.record_id, resolution.quantity, resolution.realized_pnl) == (result.surplus.record_id, Decimal(3),
                                                                                    Decimal(3) * (resolution.price - Decimal(90)))
    assert await quarantined_positions(rt.repositories) == []
    assert len(await rt.database.run(released_short_exposure_holds)) == 1


def _corporate_action(rt: Rt, monkeypatch: pytest.MonkeyPatch) -> None:
    real = rt.broker.get_activities

    async def with_split(*args: Any, **kwargs: Any) -> list[AlpacaActivity]:
        rows = await real(*args, **kwargs)
        if kwargs.get("activity_type", "FILL") is None:
            raw = {"id": "zz-split", "activity_type": "SSP", "symbol": "AAPL", "date": rt.clock().date().isoformat()}
            rows = [*rows, AlpacaActivity("zz-split", "SSP", "AAPL", None, Decimal(2), None, None, raw)]
        return rows

    monkeypatch.setattr(rt.broker, "get_activities", with_split)


@pytest.mark.parametrize("case", ["wrong_instrument", "wrong_side", "before_record", "reused", "has_intent", "missing",
                                  "changed_payload", "corporate_action"])
async def test_refusals_write_nothing(rt: Rt, case: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Spec test 17: each refused with nothing written."""
    rt.broker.set_quote("MSFT", AssetClass.EQUITY, Decimal("399.90"), Decimal(400), at=rt.clock())
    if case == "has_intent":  # a TradePulse buy, made before the gateway could see the liability
        await rt.buy("1")
        rt.clock.advance(timedelta(seconds=90))
        [activity] = [a.activity_id for a in await rt.broker.get_activities(activity_type="FILL")]
    await _liability(rt, "x", "2", ago={"before_record": -3600, "has_intent": 3600}.get(case, 60))
    rt.quote()
    if case == "wrong_instrument":
        activity = await _external(rt, Side.BUY, "2", "msft", symbol="MSFT")
    elif case == "wrong_side":
        rt.broker.inject(Fault.ACCEPT_OVERSELL)
        activity = await _external(rt, Side.SELL, "1", "a-sell")
    elif case in ("before_record", "changed_payload", "corporate_action"):
        activity = await _external(rt, Side.BUY, "2", "cover")
    elif case == "reused":
        activity = await _external(rt, Side.BUY, "1", "cover")
        await _resolve(rt, activity)
    elif case == "missing":
        activity = "no-such-activity"
    if case == "changed_payload":
        record = UnresolvedBrokerExecution(activity_id=activity, asset_key=KEY, side=Side.BUY, quantity=Decimal(2),
                                           price=Decimal(1), transaction_time=rt.clock(), raw_sha256="0" * 64,
                                           first_seen_at=rt.clock(), pass_id="p")
        await rt.database.run(lambda c: c.execute(
            "INSERT INTO unresolved_broker_executions (record_id,payload,created_at) VALUES (?,?,?)",
            (activity, encode_payload(record), rt.clock().isoformat())), write=True)
    if case == "corporate_action":
        _corporate_action(rt, monkeypatch)
    expected = {"wrong_instrument": "NO_OPEN_QUARANTINED_RECORD", "wrong_side": "WRONG_SIDE", "before_record": "ACTIVITY_BEFORE_RECORD",
                "reused": "ACTIVITY_ALREADY_USED", "has_intent": "ACTIVITY_HAS_TRADEPULSE_INTENT", "missing": "ACTIVITY_NOT_FOUND",
                "changed_payload": "BROKER_EVIDENCE_CHANGED", "corporate_action": "CORPORATE_ACTION: SSP zz-split"}[case]
    before = await _counts(rt)
    with pytest.raises(ResolveRefused) as refused:
        await _resolve(rt, activity)
    assert refused.value.reason == expected
    assert await _counts(rt) == before


async def test_resolve_refuses_while_runtime_leased(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                    caplog: pytest.LogCaptureFixture) -> None:
    """Review Focus 5: the command takes the account lease itself."""
    from rev140_support import T0
    from test_cli import _settings

    from tradepulse.broker.simulated import ManualClock, SimSession, SimulatedBroker

    monkeypatch.setattr(cli, "_build_broker", lambda settings: SimulatedBroker(clock=ManualClock(T0), session=SimSession.always_open()))
    called: list[str] = []

    async def reached(*args: Any, **kwargs: Any) -> Any:
        called.append("resolve")
        raise AssertionError("resolve reached while the lease is held")

    import tradepulse.quarantine.resolve as resolve_module
    monkeypatch.setattr(resolve_module, "resolve_from_activity", reached)
    settings = _settings(f"sqlite:///{tmp_path}/test.db")
    with account_lease(AccountRef("alpaca", "SIM0001"), holder="the runtime", directory=lease_directory()), caplog.at_level("ERROR"):
        assert await cli._run_quarantine_resolve(settings, "any", "cover") == 1
    assert called == []
    assert [r.reason for r in caplog.records if getattr(r, "event", None) == "quarantine_resolve_refused"] == ["ACCOUNT_LEASE_HELD"]


def test_parser_requires_activity_and_reason() -> None:
    args = cli._build_parser().parse_args(["quarantine", "resolve", "--activity", "a1", "--reason", "operator cover"])
    assert (args.command, args.action, args.activity, args.reason) == ("quarantine", "resolve", "a1", "operator cover")
    with pytest.raises(SystemExit):
        cli._build_parser().parse_args(["quarantine", "resolve", "--activity", "a1"])


async def test_cover_matches_after_resolution_and_not_heuristically(rt: Rt) -> None:
    """Reconciliation after the command: an exact-ID match against the
    quarantine execution, recorded as such; nothing new is written."""
    await _oversell(rt)
    rt.quote("89.90", "90")
    cover = await _external(rt, Side.BUY, "2", "operator-cover")
    await _resolve(rt, cover)
    before = len(await paginate_all_rows(rt.repositories.cash_ledger))
    await run_reconciliation(rt.repositories, rt.broker, rt.settlement, rt.alerts, clock=lambda: rt.clock())
    records = [hydrate("reconciliation_records", r["payload"]) for r in await paginate_all_rows(rt.repositories.reconciliation_records)]
    assert any(r.subject_id == cover and r.actual.get("match_method") == "quarantine_execution" for r in records)
    assert len(await paginate_all_rows(rt.repositories.cash_ledger)) == before

