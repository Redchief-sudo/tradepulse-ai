"""Portfolio Optimization -- a static symbol -> real-sector map.

Fixes an existing mechanism, not a new one: risk/engine.py's max_sector_pct
cap has always been wired correctly, but nothing has ever supplied it a real
sector value -- scanner/coordinator.py's ExecutionRequest construction never
passed `sector=`, so execution/gateway.py's `sector=request.sector or "Other"`
fallback made every scanner-originated position land in one "Other" bucket.

Derived from strategy/universe.py::DEFAULT_EQUITY_UNIVERSE's existing comment
groupings, but reclassified by REAL sector (not the loose comment buckets --
e.g. GOOGL/META are Communication Services, not "tech"; sector ETFs map to
the sector they represent). Covers only the default built-in universe -- a
custom equity_universe_path config still degrades to "Other" for unmapped
symbols, same graceful-degradation shape as today, not a regression.
"""

from __future__ import annotations

from tradepulse.models import AssetClass

EQUITY_SECTOR_MAP: dict[str, str] = {
    # Mega-cap tech
    "AAPL": "Technology", "MSFT": "Technology", "NVDA": "Technology",
    "GOOGL": "Communication Services", "META": "Communication Services",
    "AMZN": "Consumer Discretionary", "TSLA": "Consumer Discretionary",
    # Broad ETFs -- not sector-specific
    "SPY": "Broad Market", "QQQ": "Broad Market", "IWM": "Broad Market",
    "VTI": "Broad Market", "VOO": "Broad Market", "DIA": "Broad Market",
    # Sector ETFs -- map to the sector they represent
    "XLF": "Financials", "XLE": "Energy", "XLK": "Technology",
    "XLV": "Healthcare", "XLI": "Industrials", "XLU": "Utilities",
    # Blue chips
    "JPM": "Financials", "V": "Financials", "MA": "Financials", "BAC": "Financials",
    "JNJ": "Healthcare", "UNH": "Healthcare",
    "WMT": "Consumer Staples", "PG": "Consumer Staples", "KO": "Consumer Staples",
    "HD": "Consumer Discretionary",
    # Bonds
    "TLT": "Fixed Income", "IEF": "Fixed Income", "SHY": "Fixed Income", "BND": "Fixed Income",
    # Gold / commodities
    "GLD": "Commodities", "SLV": "Commodities",
}


def sector_for_symbol(symbol: str, asset_class: AssetClass) -> str:
    if asset_class == AssetClass.CRYPTO:
        return "Crypto"  # one bucket -- crypto pairs aren't meaningfully cross-classified by this map
    return EQUITY_SECTOR_MAP.get(symbol.upper(), "Other")
