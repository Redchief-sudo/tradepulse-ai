"""Phase A0 verification tooling (Rev.139): the run-window check and the
order census."""
from __future__ import annotations

import importlib.util
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


window = _script("a0_verification_window")
census_script = _script("a0_order_census")


# --- the window -------------------------------------------------------------------------


@pytest.mark.parametrize(("start", "hours", "refused"), [
    ("2026-10-26T09:00-04:00", 31.5, []),  # the agreed run: Monday 09:00 ET to Tuesday 16:30 ET
    ("2026-10-26T09:30-04:00", 30.5, ["start"]),  # "open to close": starts inside the session
    ("2026-10-26T09:20-04:00", 31.0, ["start"]),  # within 15 minutes before the open
    ("2026-10-26T09:00-04:00", 24.5, ["end"]),  # ends 09:30 Tuesday, at the open
    ("2026-10-26T09:00-04:00", 31.0, []),  # ends exactly at Tuesday's 16:00 close: outside the session
    ("2026-11-26T08:00-05:00", 48.0, []),  # Thanksgiving to the early-close Friday's evening
    ("2026-11-27T12:30-05:00", 2.0, ["start"]),  # Friday's early session still open at 12:30
    ("2026-11-27T13:05-05:00", 3.0, []),  # after the 13:00 early close
])
def test_window_boundaries(start: str, hours: float, refused: list[str]) -> None:
    _, problems = window.check(datetime.fromisoformat(start), hours)
    assert [p.split()[0] for p in problems] == refused, problems


def test_window_command_prints_both_boundaries_and_exit_code(capsys: pytest.CaptureFixture[str]) -> None:
    assert window.main(["--start", "2026-10-26T09:00-04:00", "--hours", "31.5"]) == 0
    out = capsys.readouterr().out
    assert "2026-10-26 13:00 UTC  2026-10-26 09:00 EDT" in out and "2026-10-27 20:30 UTC  2026-10-27 16:30 EDT" in out
    assert window.main(["--start", "2026-10-26T09:30-04:00", "--hours", "30.5"]) == 1
    with pytest.raises(SystemExit):
        window.main(["--start", "2026-10-26T09:00", "--hours", "24"])  # no UTC offset


# --- the census -------------------------------------------------------------------------


def _intent(intent_id: str, status: str, reason: str | None = None) -> dict[str, object]:
    return {"trade_intent_id": intent_id, "status": status, "rejection_reason": reason,
            "created_at": "2026-10-26T14:00:00+00:00"}


def _order(order_id: str, client_order_id: str, at: str = "2026-10-26T14:00:01Z") -> dict[str, object]:
    return {"id": order_id, "client_order_id": client_order_id, "submitted_at": at}


def test_census_of_a_clean_run_passes() -> None:
    intents = [_intent("a", "filled"), _intent("b", "canceled"), _intent("c", "rejected", "BROKER_SUBMIT_ERROR: x"),
               _intent("d", "rejected", "EQUITY_MARKET_CLOSED"),  # never submitted
               _intent("e", "rejected", "ACCOUNT_LEASE_NOT_HELD")]  # refused before submission
    counts = census_script.census(intents, [_order("1", "a"), _order("2", "b")])
    assert counts == {"submitted_requests": 3, "accepted_orders": 2, "definitive_rejections": 1,
                      "unresolved_submissions": 0, "duplicate_orders": 0, "unauthorized_orders": 0}
    assert census_script.passed(counts)


@pytest.mark.parametrize(("intents", "orders", "failure"), [
    ([_intent("a", "filled")], [_order("1", "a"), _order("2", "a")], "duplicate_orders"),
    ([_intent("a", "filled")], [_order("1", "a"), _order("2", "by-hand")], "unauthorized_orders"),
    ([_intent("a", "submission_unknown")], [], "unresolved_submissions"),
    ([_intent("a", "filled")], [], None),  # a submitted request with no broker order
])
def test_census_failures(intents: list[dict[str, object]], orders: list[dict[str, object]], failure: str | None) -> None:
    counts = census_script.census(intents, orders)
    assert not census_script.passed(counts)
    if failure is not None:
        assert counts[failure] >= 1


RUN_START, RUN_END = datetime(2026, 10, 26, 13, tzinfo=UTC), datetime(2026, 10, 27, 20, 30, tzinfo=UTC)


def _population(*groups: tuple[int, int]) -> list[dict[str, object]]:
    """Orders in groups of (count, seconds after the run start); a group shares one submitted_at."""
    orders, n = [], 0
    for count, seconds in groups:
        stamp = (RUN_START + timedelta(hours=1, seconds=seconds)).isoformat().replace("+00:00", "Z")
        for _ in range(count):
            orders.append(_order(f"o{n:05d}", f"c{n}", stamp))
            n += 1
    return orders


def _alpaca(population: list[dict[str, object]], *, ignore_after: bool = False):
    """GET /v2/orders as measured on 2026-10-26: ``after`` inclusive and
    ``until`` exclusive on submitted_at, ascending, at most `limit` rows."""
    requests: list[dict[str, str]] = []

    async def get(params: dict[str, str]) -> list[dict[str, object]]:
        requests.append(params)
        after, until = datetime.fromisoformat(params["after"]), datetime.fromisoformat(params["until"])
        rows = sorted((o for o in population
                       if (ignore_after or after <= datetime.fromisoformat(str(o["submitted_at"])))
                       and datetime.fromisoformat(str(o["submitted_at"])) < until),
                      key=lambda o: (datetime.fromisoformat(str(o["submitted_at"])), str(o["id"])))
        return rows[: int(params["limit"])]

    return get, requests


@pytest.mark.parametrize("groups", [
    [(2, i) for i in range(602)],  # pairs: page edges never split a timestamp
    [(499, 1), (5, 2), (300, 3)],  # the first page edge splits the 5 orders sharing second 2
    [(250, 1), (499, 2)],  # a 499-order group straddles the edge
    [(1, i) for i in range(1000)],  # exactly two full pages, then an empty one
])
async def test_order_paging_is_complete_across_page_edges(groups: list[tuple[int, int]]) -> None:
    population = _population(*groups)
    get, requests = _alpaca(population)
    fetched = await census_script.fetch_orders(get, RUN_START, RUN_END)
    assert [o["id"] for o in fetched] == [o["id"] for o in population]
    assert all(r["status"] == "all" and r["direction"] == "asc" and r["limit"] == "500" for r in requests)


async def test_an_order_submitted_exactly_at_the_run_start_is_counted() -> None:
    at_start = _order("o-start", "c-start", RUN_START.isoformat().replace("+00:00", "Z"))
    get, _ = _alpaca([at_start, *_population((1, 1))])
    fetched = await census_script.fetch_orders(get, RUN_START, RUN_END)
    assert [o["id"] for o in fetched] == ["o-start", "o00000"]


@pytest.mark.parametrize("groups", [
    [(501, 1)],  # 501 orders in one microsecond: more than a page can hold
    [(500, 1), (1, 2)],  # exactly a page's worth share one timestamp
])
async def test_a_timestamp_shared_by_a_full_page_fails_closed(groups: list[tuple[int, int]]) -> None:
    get, _ = _alpaca(_population(*groups))
    with pytest.raises(census_script.IncompleteOrderHistory, match="share submitted_at"):
        await census_script.fetch_orders(get, RUN_START, RUN_END)


async def test_a_cursor_that_does_not_progress_fails_closed() -> None:
    get, _ = _alpaca(_population(*[(1, i) for i in range(600)]), ignore_after=True)  # a server ignoring `after`
    with pytest.raises(census_script.IncompleteOrderHistory):
        await census_script.fetch_orders(get, RUN_START, RUN_END)


@pytest.mark.parametrize(("page", "reason"), [
    ({"orders": []}, "not a list"),
    ([{"client_order_id": "c"}], "without an id"),
    ([_order(f"o{i}", f"c{i}") for i in range(501)], "exceeds the limit"),
    ([_order("o1", "c1", "2026-10-28T00:00:00Z")], "outside the request"),
])
async def test_an_incomplete_or_malformed_response_fails_closed(page: object, reason: str) -> None:
    async def get(params: dict[str, str]) -> object:
        return page

    with pytest.raises(census_script.IncompleteOrderHistory, match=reason):
        await census_script.fetch_orders(get, RUN_START, RUN_END)  # type: ignore[arg-type]


def test_an_incomplete_history_never_produces_a_passing_census(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "run.db"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE trade_intents (payload TEXT)")
    connection.commit()
    connection.close()

    async def incomplete(start: datetime, end: datetime) -> list[dict[str, object]]:
        raise census_script.IncompleteOrderHistory("500 or more orders share submitted_at ...")

    monkeypatch.setattr(census_script, "_alpaca_orders", incomplete)
    code = census_script.main(["--database", str(database), "--start", RUN_START.isoformat(),
                               "--end", RUN_END.isoformat()])
    captured = capsys.readouterr()
    assert code == 1 and "census FAILED" in captured.err and "passed" not in captured.out


def test_intents_are_read_read_only_and_filtered_to_the_window(tmp_path: Path) -> None:
    database = tmp_path / "run.db"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE trade_intents (payload TEXT)")
    for intent_id, created in (("before", "2026-10-26T12:59:00+00:00"), ("inside", "2026-10-26T14:00:00+00:00")):
        connection.execute("INSERT INTO trade_intents VALUES (?)", (json.dumps(
            {"trade_intent_id": intent_id, "status": "filled", "created_at": created}),))
    connection.commit()
    connection.close()
    before = database.read_bytes()
    intents = census_script.load_intents(database, datetime(2026, 10, 26, 13, tzinfo=UTC),
                                         datetime(2026, 10, 27, 20, 30, tzinfo=UTC))
    assert [i["trade_intent_id"] for i in intents] == ["inside"]
    assert database.read_bytes() == before
