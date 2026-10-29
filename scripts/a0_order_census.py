#!/usr/bin/env python3
"""Order census for a Phase A0 verification run. Read-only.

Compares every order Alpaca holds for the run window (GET /v2/orders,
status=all, ascending; read-only HTTP, not on the broker port, since this is
verification tooling) with the trade intents in the run's database, opened
read-only. Every TradePulse order's client_order_id is its trade intent id.

Reported:
- submitted_requests: intents that reached SUBMITTED (anything past it, or
  rejected by the broker itself);
- accepted_orders: broker orders whose client_order_id is an intent;
- definitive_rejections: intents the broker definitively rejected;
- unresolved_submissions: intents still SUBMITTED or SUBMISSION_UNKNOWN;
- duplicate_orders: intents with more than one broker order;
- unauthorized_orders: broker orders whose client_order_id is no intent.

The run passes when duplicate_orders == unauthorized_orders ==
unresolved_submissions == 0 and accepted_orders + definitive_rejections ==
submitted_requests. An order placed by hand during the run is unauthorized.

    scripts/a0_order_census.py --database <run>.db --start <ISO> --end <ISO> [--json]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable, Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

PAGE_LIMIT = 500
SUBMITTED_OR_LATER = frozenset({"submitted", "accepted", "partially_filled", "filled", "canceled", "expired",
                                "submission_unknown"})
UNRESOLVED = frozenset({"submitted", "submission_unknown"})
BROKER_REJECTION_PREFIX = "BROKER_SUBMIT_ERROR"  # execution/gateway.py: a definitive broker rejection


def load_intents(database: Path, start: datetime, end: datetime) -> list[dict[str, Any]]:
    """Trade intents created in [start, end], read-only."""
    connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        rows = connection.execute("SELECT payload FROM trade_intents").fetchall()
    finally:
        connection.close()
    intents = [json.loads(row[0]) for row in rows]
    return [i for i in intents if start <= datetime.fromisoformat(i["created_at"]) <= end]


def census(intents: Iterable[Mapping[str, Any]], orders: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    intents, orders = list(intents), list(orders)
    by_id = {i["trade_intent_id"]: i for i in intents}
    rejected = {i["trade_intent_id"] for i in intents
                if i["status"] == "rejected" and str(i.get("rejection_reason") or "").startswith(BROKER_REJECTION_PREFIX)}
    submitted = {i["trade_intent_id"] for i in intents if i["status"] in SUBMITTED_OR_LATER} | rejected
    per_intent = Counter(o.get("client_order_id") for o in orders if o.get("client_order_id") in by_id)
    return {
        "submitted_requests": len(submitted),
        "accepted_orders": sum(per_intent.values()),
        "definitive_rejections": len(rejected),
        "unresolved_submissions": sum(1 for i in intents if i["status"] in UNRESOLVED),
        "duplicate_orders": sum(count - 1 for count in per_intent.values() if count > 1),
        "unauthorized_orders": sum(1 for o in orders if o.get("client_order_id") not in by_id),
    }


def passed(counts: Mapping[str, int]) -> bool:
    return (counts["duplicate_orders"] == counts["unauthorized_orders"] == counts["unresolved_submissions"] == 0
            and counts["accepted_orders"] + counts["definitive_rejections"] == counts["submitted_requests"])


class IncompleteOrderHistory(RuntimeError):
    """The order history could not be proven complete; the census fails."""


async def fetch_orders(get: Callable[[dict[str, str]], Awaitable[list[dict[str, Any]]]],
                       start: datetime, end: datetime) -> list[dict[str, Any]]:
    """Every order submitted in [start, end), or IncompleteOrderHistory.

    Alpaca's GET /v2/orders has no continuation token, only ``after``
    (inclusive), ``until`` (exclusive), both on submitted_at and measured on
    2026-10-26, and ``limit`` (at most 500).
    A page shorter than the limit is the whole remaining interval. After a
    full page, the next request starts one microsecond before that page's
    latest submitted_at -- Alpaca's timestamps carry microseconds -- so every
    order sharing that timestamp is fetched again and de-duplicated by id,
    never skipped. If a full page brings nothing new (PAGE_LIMIT or more
    orders share one timestamp), completeness cannot be established and the
    scan fails closed."""
    orders: dict[str, dict[str, Any]] = {}
    after = start
    while True:
        page = await get({"status": "all", "direction": "asc", "nested": "false", "limit": str(PAGE_LIMIT),
                          "after": after.isoformat(), "until": end.isoformat()})
        if not isinstance(page, list):
            raise IncompleteOrderHistory("orders response is not a list")
        if len(page) > PAGE_LIMIT:
            raise IncompleteOrderHistory(f"orders page of {len(page)} exceeds the limit of {PAGE_LIMIT}")
        stamps = []
        new = 0
        for order in page:
            if not isinstance(order, dict) or not isinstance(order.get("id"), str) or not order.get("submitted_at"):
                raise IncompleteOrderHistory(f"order without an id or submitted_at: {order!r}")
            stamp = datetime.fromisoformat(order["submitted_at"])
            if not after <= stamp < end:
                raise IncompleteOrderHistory(f"order {order['id']} at {stamp.isoformat()} is outside the request")
            stamps.append(stamp)
            new += order["id"] not in orders
            orders[order["id"]] = order
        if len(page) < PAGE_LIMIT:
            break
        if not new:
            raise IncompleteOrderHistory(
                f"{PAGE_LIMIT} or more orders share submitted_at {max(stamps).isoformat()}; the history cannot be paged")
        after = max(stamps) - timedelta(microseconds=1)
    return sorted(orders.values(), key=lambda o: (datetime.fromisoformat(o["submitted_at"]), o["id"]))


async def _alpaca_orders(start: datetime, end: datetime) -> list[dict[str, Any]]:
    import httpx

    from tradepulse.broker.alpaca_client import PAPER_TRADING_BASE
    from tradepulse.cli import _load_dotenv
    from tradepulse.config import Settings

    _load_dotenv(Path(".env"))
    settings = Settings.from_env()
    if settings.execution_mode != "paper" or settings.live_trading_enabled:
        raise SystemExit("the census runs only against paper with live trading disabled")
    headers = {"APCA-API-KEY-ID": settings.alpaca_api_key or "", "APCA-API-SECRET-KEY": settings.alpaca_api_secret or ""}
    async with httpx.AsyncClient(timeout=30) as client:
        async def get(params: dict[str, str]) -> list[dict[str, Any]]:
            response = await client.get(f"{PAPER_TRADING_BASE}/orders", params=params, headers=headers)
            response.raise_for_status()
            return response.json()
        return await fetch_orders(get, start, end)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--start", required=True, type=datetime.fromisoformat)
    parser.add_argument("--end", required=True, type=datetime.fromisoformat)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.start.tzinfo is None or args.end.tzinfo is None:
        parser.error("--start and --end need a UTC offset")
    intents = load_intents(args.database, args.start, args.end)
    try:
        orders = asyncio.run(_alpaca_orders(args.start, args.end))
    except IncompleteOrderHistory as exc:
        print(f"census FAILED: the broker order history is not provably complete: {exc}", file=sys.stderr)
        return 1
    counts = census(intents, orders)
    if args.json:
        print(json.dumps({**counts, "passed": passed(counts)}, sort_keys=True))
    else:
        for name, value in counts.items():
            print(f"{name}: {value}")
        print("census passed" if passed(counts) else "census FAILED")
    return 0 if passed(counts) else 1


if __name__ == "__main__":
    sys.exit(main())
