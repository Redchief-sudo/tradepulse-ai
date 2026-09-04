"""Fetches and caches full daily-bar history for the exit-parameter
calibration universe, via the account's actual, already-configured
AlpacaClient -- same IEX equity feed the live paper account is entitled to,
never a SIP/IEX mismatch between calibration and live behavior.

Read-only against Alpaca (historical bars only, no orders/positions/account
calls) and never touches `tradepulse/`. Caches to data/calibration/{equity,
crypto}/<symbol>.json (gitignored -- regeneratable, not source).

A single request per symbol is sufficient in practice (empirically verified:
limit=10000 against a 2015-2026 window returned next_page_token: None for
SPY/NVDA/BTC-USD alike -- 10,000 daily bars is ~40 years, far beyond any
realistic history), but this still loops on next_page_token defensively
rather than assuming that holds forever.

Usage: .venv/bin/python tools/historical_data/fetch_alpaca_history.py [--refresh]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from tradepulse.broker import AlpacaClient  # noqa: E402
from tradepulse.config import Settings  # noqa: E402
from tradepulse.strategy.universe import DEFAULT_CRYPTO_UNIVERSE, DEFAULT_EQUITY_UNIVERSE  # noqa: E402

CACHE_ROOT = REPO_ROOT / "data" / "calibration"
FETCH_START = datetime(2015, 1, 1, tzinfo=UTC)  # wider than any universe symbol's real history -- the fetch reports actual coverage, never assumes it
EQUITY_GAP_ALERT_DAYS = 5  # flags anything wider than an ordinary long weekend + one holiday
CRYPTO_GAP_ALERT_DAYS = 2  # crypto trades every calendar day -- any gap at all is notable


def _load_dotenv(path: Path = REPO_ROOT / ".env") -> None:
    from os import environ
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key:
            environ.setdefault(key, value.strip().strip('"').strip("'"))


async def _fetch_symbol_bars(client: AlpacaClient, symbol: str, is_crypto: bool) -> list[dict]:
    """Direct paginated fetch against Alpaca's bars endpoint (not
    AlpacaClient.get_bars, which doesn't expose next_page_token to the
    caller) -- reuses AlpacaClient's own request/auth/error-handling
    machinery (_request) rather than reimplementing it. Returns raw bar
    dicts, oldest-first."""
    from tradepulse.broker.alpaca_client import DATA_BASE, raise_alpaca_error

    normalized = symbol.upper().replace("-", "/")
    end = datetime.now(UTC)
    all_rows: list[dict] = []
    page_token: str | None = None
    while True:
        params = {
            "timeframe": "1Day", "start": FETCH_START.isoformat(), "end": end.isoformat(),
            "limit": "10000", "sort": "asc",
        }
        if page_token:
            params["page_token"] = page_token
        if is_crypto:
            url = f"{DATA_BASE}/v1beta3/crypto/us/bars"
            response = await client._request("GET", url, params={**params, "symbols": normalized})
        else:
            url = f"{DATA_BASE}/v2/stocks/{normalized}/bars"
            response = await client._request("GET", url, params={**params, "feed": client._equity_feed})
        if not response.is_success:
            raise_alpaca_error(response, "getBarsHistory")
        data = response.json()
        rows = (data.get("bars") or {}).get(normalized) if is_crypto else (data.get("bars") or [])
        all_rows.extend(rows or [])
        page_token = data.get("next_page_token")
        if not page_token:
            break
        print(f"    ...paginating {symbol} (next_page_token present -- unexpected for a full-history request, handling it anyway)")
    return all_rows


def _detect_gaps(dates: list[str], alert_days: int) -> list[dict]:
    gaps = []
    for i in range(1, len(dates)):
        prev = datetime.fromisoformat(dates[i - 1]).date()
        cur = datetime.fromisoformat(dates[i]).date()
        delta = (cur - prev).days
        if delta > alert_days:
            gaps.append({"from": dates[i - 1], "to": dates[i], "days": delta})
    return gaps


async def _fetch_and_cache(client: AlpacaClient, symbol: str, asset_class: str, refresh: bool) -> None:
    is_crypto = asset_class == "crypto"
    safe_name = symbol.replace("/", "-")
    out_path = CACHE_ROOT / asset_class / f"{safe_name}.json"
    if out_path.exists() and not refresh:
        print(f"  {symbol}: cached, skipping (--refresh to re-fetch)")
        return

    rows = await _fetch_symbol_bars(client, symbol, is_crypto)
    bars = []
    for row in rows:
        close = row.get("c")
        if close is None:
            continue
        ts = row.get("t")
        date = ts[:10] if ts else None
        if date is None:
            continue
        bars.append({
            "date": date, "open": str(row.get("o", "0")), "high": str(row.get("h", "0")),
            "low": str(row.get("l", "0")), "close": str(close), "volume": str(row.get("v", "0")),
        })

    dates = [b["date"] for b in bars]
    gap_alert = CRYPTO_GAP_ALERT_DAYS if is_crypto else EQUITY_GAP_ALERT_DAYS
    payload = {
        "symbol": symbol,
        "asset_class": asset_class,
        "fetched_at": datetime.now(UTC).isoformat(),
        "bar_count": len(bars),
        "first_bar_date": dates[0] if dates else None,
        "last_bar_date": dates[-1] if dates else None,
        "gaps": _detect_gaps(dates, gap_alert),
        "bars": bars,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"  {symbol}: {len(bars)} bars, {payload['first_bar_date']} -> {payload['last_bar_date']}, {len(payload['gaps'])} gap(s) > {gap_alert}d")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--refresh", action="store_true", help="re-fetch even if a cached file already exists")
    args = parser.parse_args()

    _load_dotenv()
    settings = Settings.from_env()
    client = AlpacaClient(settings.alpaca_api_key, settings.alpaca_api_secret, "paper", 30, equity_feed="iex")
    try:
        print("Equities (+ SPY benchmark):")
        for symbol in sorted({*DEFAULT_EQUITY_UNIVERSE, "SPY"}):
            await _fetch_and_cache(client, symbol, "equity", args.refresh)
        print("Crypto (+ BTC/USD benchmark):")
        for symbol in {*DEFAULT_CRYPTO_UNIVERSE, "BTC/USD"}:
            await _fetch_and_cache(client, symbol, "crypto", args.refresh)
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
