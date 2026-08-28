from decimal import Decimal

from tradepulse.config import Settings
from tradepulse.models import AssetClass, AssetIdentity, MarketQuote, Opportunity
from tradepulse.strategy.universe import filter_executable, is_executable, load_executable_universe
from datetime import UTC, datetime


NOW = datetime(2026, 8, 15, tzinfo=UTC)


def test_default_universe_covers_both_asset_classes() -> None:
    universe = load_executable_universe(Settings.from_env({}))
    aapl = AssetIdentity("AAPL", AssetClass.EQUITY, "alpaca:AAPL")
    btc = AssetIdentity("BTC/USD", AssetClass.CRYPTO, "alpaca:BTC/USD")
    unknown = AssetIdentity("ZZZZ", AssetClass.EQUITY, "alpaca:ZZZZ")
    unknown_crypto = AssetIdentity("DOGE/USD", AssetClass.CRYPTO, "alpaca:DOGE/USD")

    assert is_executable(aapl, universe)
    assert is_executable(btc, universe)
    assert not is_executable(unknown, universe)
    assert not is_executable(unknown_crypto, universe)  # crypto IS whitelisted here, unlike audited Base44


def test_options_universe_checks_underlying_not_contract_symbol() -> None:
    """By the time an OPTION AssetIdentity reaches is_executable, its
    .symbol is the resolved OCC contract, not the underlying the AI
    actually proposed -- membership must be checked against the underlying
    recorded in metadata, never the contract symbol itself."""
    universe = load_executable_universe(Settings.from_env({}))
    contract = AssetIdentity(
        "AAPL251219C00150000", AssetClass.OPTION, "alpaca:AAPL251219C00150000",
        metadata={"underlying_symbol": "AAPL"},
    )
    unknown_underlying = AssetIdentity(
        "ZZZZ251219C00150000", AssetClass.OPTION, "alpaca:ZZZZ251219C00150000",
        metadata={"underlying_symbol": "ZZZZ"},
    )
    no_metadata = AssetIdentity("AAPL251219C00150000", AssetClass.OPTION, "alpaca:AAPL251219C00150000")

    assert is_executable(contract, universe)
    assert not is_executable(unknown_underlying, universe)
    assert not is_executable(no_metadata, universe)  # fail-closed on missing underlying_symbol, not a silent pass


def test_options_universe_file_overrides_default(tmp_path) -> None:
    options_file = tmp_path / "options.txt"
    options_file.write_text("NFLX\nORCL\n")
    settings = Settings.from_env({"TRADEPULSE_OPTIONS_UNIVERSE_PATH": str(options_file)})
    universe = load_executable_universe(settings)
    assert universe.options_underlyings == frozenset({"NFLX", "ORCL"})


def test_crypto_universe_is_never_unrestricted() -> None:
    universe = load_executable_universe(Settings.from_env({}))
    assert len(universe.crypto) > 0
    assert len(universe.crypto) < 100  # a real, bounded whitelist -- not "anything goes"


def test_universe_file_overrides_default(tmp_path) -> None:
    equity_file = tmp_path / "equities.txt"
    equity_file.write_text("NFLX\n# comment\nORCL\n")
    settings = Settings.from_env({"TRADEPULSE_EQUITY_UNIVERSE_PATH": str(equity_file)})
    universe = load_executable_universe(settings)
    assert universe.equities == frozenset({"NFLX", "ORCL"})
    assert not is_executable(AssetIdentity("AAPL", AssetClass.EQUITY, "alpaca:AAPL"), universe)


def test_filter_executable_drops_non_whitelisted_candidates() -> None:
    universe = load_executable_universe(Settings.from_env({}))
    aapl = AssetIdentity("AAPL", AssetClass.EQUITY, "alpaca:AAPL")
    junk = AssetIdentity("ZZZZ", AssetClass.EQUITY, "alpaca:ZZZZ")
    quote_a = MarketQuote(aapl, Decimal("190"), NOW, NOW, "alpaca", 0)
    quote_j = MarketQuote(junk, Decimal("1"), NOW, NOW, "alpaca", 0)
    candidates = [
        Opportunity("opp-1", "gen-1", aapl, quote_a, "scanner", NOW),
        Opportunity("opp-2", "gen-1", junk, quote_j, "scanner", NOW),
    ]
    result = filter_executable(candidates, universe)
    assert [c.opportunity_id for c in result] == ["opp-1"]
