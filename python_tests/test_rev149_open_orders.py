"""Rev.149 F02 (forensic audit 2026-11-04): the open-order population must be
proven complete, not taken from one default page.

Before the fix, get_open_orders sent one GET with no limit and no paging.
Alpaca returns 50 orders by default and at most 500, so the 51st open order
(for example an older foreign SELL on a protected asset) was invisible to
the sell gate and would be invisible to Rev.147's census.

The mock below behaves like Alpaca's GET /v2/orders as measured on
2026-10-26 (~/tradepulse-evidence/2026-10-26-r7-api-semantics/): no
continuation token, ``after`` inclusive at microsecond precision, ``limit``
at most 500 with a default of 50, and ``direction``.
"""
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from tradepulse.broker import AlpacaClient
from tradepulse.broker.errors import AlpacaDataIntegrityError

ORDERS_URL = "https://paper-api.alpaca.markets/v2/orders"
BASE = datetime(2026, 11, 2, 14, 30, tzinfo=UTC)


def _order(index: int, stamp: datetime, *, side: str = "buy", symbol: str = "AAPL") -> dict:
    return {"id": f"order-{index:05d}", "status": "new", "symbol": symbol, "side": side,
            "filled_qty": "0", "filled_avg_price": None, "submitted_at": stamp.isoformat().replace("+00:00", "Z")}


class AlpacaOrdersModel:
    """Serves status=open pages the way Alpaca does. ``population`` may be
    changed between requests to model orders opening or closing mid-scan."""

    def __init__(self, population: list[dict]) -> None:
        self.population = population
        self.requests: list[dict[str, str]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        self.requests.append(params)
        assert params.get("status") == "open"
        limit = min(int(params.get("limit", "50")), 500)
        rows = sorted(self.population, key=lambda o: (o["submitted_at"], o["id"]),
                      reverse=params.get("direction", "desc") == "desc")
        if "after" in params:
            after = datetime.fromisoformat(params["after"])
            rows = [o for o in rows if datetime.fromisoformat(o["submitted_at"]) >= after]  # inclusive
        return httpx.Response(200, json=rows[:limit])


async def _read(population: list[dict]) -> tuple[list[str], AlpacaOrdersModel]:
    model = AlpacaOrdersModel(population)
    with respx.mock:
        respx.get(ORDERS_URL).mock(side_effect=model)
        broker = AlpacaClient("key", "secret", "paper", 10)
        try:
            orders = await broker.get_open_orders()
        finally:
            await broker.aclose()
    return [order.broker_order_id for order in orders], model


async def test_51_open_orders_are_all_returned() -> None:
    """The audit's case: an older foreign SELL is the 51st order."""
    population = [_order(0, BASE - timedelta(days=3), side="sell")]  # the oldest: a foreign sell
    population += [_order(i, BASE + timedelta(seconds=i)) for i in range(1, 51)]
    ids, model = await _read(population)

    assert sorted(ids) == sorted(o["id"] for o in population)
    assert "order-00000" in ids
    assert model.requests[0]["limit"] == "500"  # never the default page
    assert model.requests[0]["direction"] == "asc"


async def test_more_than_500_open_orders_are_paged_completely() -> None:
    population = [_order(i, BASE + timedelta(seconds=i)) for i in range(1203)]
    ids, model = await _read(population)

    assert len(ids) == 1203
    assert len(set(ids)) == 1203  # each order exactly once
    assert len(model.requests) == 3


async def test_equal_timestamps_at_a_page_boundary_are_neither_lost_nor_duplicated() -> None:
    """Orders 497-502 share one submitted_at, straddling the 500th row."""
    population = [_order(i, BASE + timedelta(seconds=i)) for i in range(497)]
    population += [_order(i, BASE + timedelta(seconds=497)) for i in range(497, 503)]
    population += [_order(i, BASE + timedelta(seconds=i)) for i in range(503, 520)]
    ids, _ = await _read(population)

    assert sorted(ids) == sorted(o["id"] for o in population)
    assert len(ids) == len(set(ids))


async def test_a_saturated_page_that_cannot_be_continued_fails_closed() -> None:
    """500 or more open orders on one submitted_at: the next page cannot move
    past them, so completeness cannot be proven."""
    population = [_order(i, BASE) for i in range(501)]
    with pytest.raises(AlpacaDataIntegrityError, match="cannot be proven complete"):
        await _read(population)


async def test_a_full_page_with_an_unstamped_order_fails_closed() -> None:
    population = [_order(i, BASE + timedelta(seconds=i)) for i in range(500)]
    population[-1] = {**population[-1], "submitted_at": None}
    model = AlpacaOrdersModel(population)
    model_rows = model.population

    def serve(request: httpx.Request) -> httpx.Response:
        model.requests.append(dict(request.url.params))
        return httpx.Response(200, json=model_rows[:500])

    with respx.mock:
        respx.get(ORDERS_URL).mock(side_effect=serve)
        broker = AlpacaClient("key", "secret", "paper", 10)
        try:
            with pytest.raises(AlpacaDataIntegrityError, match="cannot be proven complete"):
                await broker.get_open_orders()
        finally:
            await broker.aclose()


async def test_a_page_larger_than_the_limit_is_refused() -> None:
    rows = [_order(i, BASE + timedelta(seconds=i)) for i in range(501)]
    with respx.mock:
        respx.get(ORDERS_URL).mock(return_value=httpx.Response(200, json=rows))
        broker = AlpacaClient("key", "secret", "paper", 10)
        try:
            with pytest.raises(AlpacaDataIntegrityError, match="exceeds the limit"):
                await broker.get_open_orders()
        finally:
            await broker.aclose()


async def test_an_order_opened_or_closed_during_the_scan_is_handled() -> None:
    """The population changes between pages: an order that closes is simply
    absent, and one that opens (newest, so later in ascending order) is
    returned. The gateway's two bracketing reads then decide stability."""
    population = [_order(i, BASE + timedelta(seconds=i)) for i in range(600)]
    model = AlpacaOrdersModel(population)
    calls = 0

    def serve(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        response = model(request)
        calls += 1
        if calls == 1:
            model.population = [o for o in population if o["id"] != "order-00550"]  # 550 fills
            model.population.append(_order(9999, BASE + timedelta(seconds=700)))  # a new order opens
        return response

    with respx.mock:
        respx.get(ORDERS_URL).mock(side_effect=serve)
        broker = AlpacaClient("key", "secret", "paper", 10)
        try:
            ids = [order.broker_order_id for order in await broker.get_open_orders()]
        finally:
            await broker.aclose()

    assert "order-00550" not in ids
    assert "order-09999" in ids
    assert len(ids) == len(set(ids)) == 600


async def test_an_order_outside_the_requested_window_is_refused() -> None:
    """A continuation page must not contain an order older than ``after``."""
    population = [_order(i, BASE + timedelta(seconds=i)) for i in range(500)]
    stray = _order(7777, BASE - timedelta(days=1))
    served = 0

    def serve(request: httpx.Request) -> httpx.Response:
        nonlocal served
        served += 1
        rows = population if served == 1 else [stray]
        return httpx.Response(200, json=rows)

    with respx.mock:
        respx.get(ORDERS_URL).mock(side_effect=serve)
        broker = AlpacaClient("key", "secret", "paper", 10)
        try:
            with pytest.raises(AlpacaDataIntegrityError, match="outside the request"):
                await broker.get_open_orders()
        finally:
            await broker.aclose()
