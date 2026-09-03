from tradepulse.config import sector_for_symbol
from tradepulse.models import AssetClass


def test_sector_for_symbol_resolves_known_equity() -> None:
    assert sector_for_symbol("AAPL", AssetClass.EQUITY) == "Technology"
    assert sector_for_symbol("JPM", AssetClass.EQUITY) == "Financials"
    assert sector_for_symbol("XLE", AssetClass.EQUITY) == "Energy"


def test_sector_for_symbol_is_case_insensitive() -> None:
    assert sector_for_symbol("aapl", AssetClass.EQUITY) == "Technology"


def test_sector_for_symbol_crypto_is_always_one_bucket() -> None:
    assert sector_for_symbol("BTC/USD", AssetClass.CRYPTO) == "Crypto"
    assert sector_for_symbol("ETH/USD", AssetClass.CRYPTO) == "Crypto"


def test_sector_for_symbol_unmapped_equity_falls_back_to_other() -> None:
    assert sector_for_symbol("ZZZZ", AssetClass.EQUITY) == "Other"
