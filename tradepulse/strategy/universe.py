"""Executable universe -- the symbols the autonomous trader is allowed to
execute. Candidates outside this universe are research-only.

Unlike the audited Base44 system (which hardcoded an equity/ETF whitelist
but left crypto with NO whitelist at all -- finding H5 in the audit), BOTH
asset classes are config-file-driven here, defaulting to a conservative
built-in list when no file is configured.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from tradepulse.config import Settings
from tradepulse.models import AssetClass, AssetIdentity, Opportunity

DEFAULT_EQUITY_UNIVERSE = (
    # Mega-cap tech
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA",
    # Broad ETFs
    "SPY", "QQQ", "IWM", "VTI", "VOO", "DIA",
    # Sector ETFs
    "XLF", "XLE", "XLK", "XLV", "XLI", "XLU",
    # Blue chips
    "JPM", "V", "JNJ", "WMT", "PG", "UNH", "HD", "MA", "BAC", "KO",
    # Bonds
    "TLT", "IEF", "SHY", "BND",
    # Gold / commodities
    "GLD", "SLV",
)

# Conservative default crypto whitelist -- the audited system had none at
# all; this MVP always enforces one, defaulting to the most liquid pairs.
DEFAULT_CRYPTO_UNIVERSE = ("BTC/USD", "ETH/USD", "SOL/USD", "LTC/USD", "BCH/USD")


@dataclass(frozen=True, slots=True)
class ExecutableUniverse:
    equities: frozenset[str]
    crypto: frozenset[str]


def _load_symbols(path: str | None, default: tuple[str, ...]) -> frozenset[str]:
    if not path:
        return frozenset(s.upper() for s in default)
    text = Path(path).read_text(encoding="utf-8")
    symbols = {
        line.strip().upper() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")
    }
    return frozenset(symbols) if symbols else frozenset(s.upper() for s in default)


def load_executable_universe(settings: Settings) -> ExecutableUniverse:
    return ExecutableUniverse(
        equities=_load_symbols(settings.equity_universe_path, DEFAULT_EQUITY_UNIVERSE),
        crypto=_load_symbols(settings.crypto_universe_path, DEFAULT_CRYPTO_UNIVERSE),
    )


def is_executable(asset: AssetIdentity, universe: ExecutableUniverse) -> bool:
    symbol = asset.symbol.upper()
    if asset.asset_class == AssetClass.CRYPTO:
        return symbol in universe.crypto
    return symbol in universe.equities


def filter_executable(candidates: Iterable[Opportunity], universe: ExecutableUniverse) -> list[Opportunity]:
    return [c for c in candidates if is_executable(c.asset, universe)]
