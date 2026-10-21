"""Phase A0.1: BrokerPort, neutral aliases, AccountRef and static typing."""
from __future__ import annotations

import ast
import asyncio
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tradepulse.broker import AlpacaClient
from tradepulse.broker.port import AccountRef, BrokerPort, account_ref
from tradepulse.models import AssetClass
from tradepulse.models.market import VENUES, asset_key_from_broker_symbol, canonical_venue

ROOT = Path(__file__).resolve().parent.parent
PORT_MEMBERS = {
    "venue", "implementation", "get_account", "get_positions", "get_clock", "place_order", "get_order",
    "get_order_by_client_order_id", "get_open_orders", "cancel_order", "get_activities", "get_latest_quote",
    "get_latest_option_quotes", "get_options_chain", "get_bars", "set_market_data_feeds", "rate_limit_snapshot",
    "aclose",
}


def test_alpaca_client_satisfies_the_port() -> None:
    client = AlpacaClient("k", "s", "paper", 10)
    assert isinstance(client, BrokerPort)
    assert client.venue == "alpaca"
    assert client.implementation == "alpaca_http"


def _broker_attribute_names(path: Path, receivers: set[str]) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if not isinstance(node, ast.Attribute):
            continue
        base = node.value
        if isinstance(base, ast.Name):
            receiver = base.id
        elif isinstance(base, ast.Attribute) and isinstance(base.value, ast.Name):
            receiver = f"{base.value.id}.{base.attr}"
        else:
            continue
        if receiver in receivers:
            names.add(node.attr)
    return names


def test_every_broker_call_in_the_code_is_on_the_port() -> None:
    missing: dict[str, set[str]] = {}
    scanned = 0
    for path in sorted((ROOT / "tradepulse").rglob("*.py")):
        rel = path.relative_to(ROOT / "tradepulse")
        if rel.parts[0] == "broker":
            continue
        receivers = {"broker", "self._broker"}
        if rel.as_posix() == "providers/alpaca_market_data.py":
            receivers.add("self._client")
        names = _broker_attribute_names(path, receivers)
        scanned += bool(names)
        if names - PORT_MEMBERS:
            missing[rel.as_posix()] = names - PORT_MEMBERS
    assert scanned > 0, "scan found no broker usage at all; the scanner is broken"
    assert not missing, f"broker members used outside the port: {missing}"


def test_port_declares_exactly_the_expected_members() -> None:
    declared = set(BrokerPort.__protocol_attrs__)  # type: ignore[attr-defined]
    assert declared == PORT_MEMBERS


def test_account_ref_rejects_blank_none_and_path_characters() -> None:
    for bad in ("", None, "../x", "a/b", " ", "x" * 65):
        with pytest.raises(ValueError):
            AccountRef("alpaca", bad)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            AccountRef(bad, "PA123")  # type: ignore[arg-type]
    ref = AccountRef("alpaca", "PA-123_x")
    assert ref.key == "alpaca:PA-123_x"


class _FakeBroker:
    venue = "alpaca"

    def __init__(self, number: object) -> None:
        self._number = number

    async def get_account(self) -> object:
        return type("A", (), {"account_number": self._number})()


async def test_account_ref_reads_venue_and_account_number() -> None:
    ref = await account_ref(_FakeBroker("PA9"))  # type: ignore[arg-type]
    assert ref == AccountRef("alpaca", "PA9")


@pytest.mark.parametrize("number", [None, "", "  ", "../x"])
async def test_account_ref_raises_on_missing_or_invalid_account_number(number: object) -> None:
    with pytest.raises(ValueError):
        await account_ref(_FakeBroker(number))  # type: ignore[arg-type]


def test_mypy_accepts_the_port_assignments() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "mypy"],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_account_ref_rejects_non_string_account_number() -> None:
    class _Broker:
        venue = "alpaca"
        implementation = "alpaca_http"

        async def get_account(self) -> SimpleNamespace:
            return SimpleNamespace(account_number=12345)

    with pytest.raises(ValueError):
        asyncio.run(account_ref(_Broker()))  # type: ignore[arg-type]


def test_default_venue_reproduces_existing_keys() -> None:
    assert asset_key_from_broker_symbol(AssetClass.EQUITY, "AAPL") == "equity:default:alpaca:AAPL"
    assert asset_key_from_broker_symbol(AssetClass.CRYPTO, "BTC/USD") == "crypto:default:alpaca:BTC/USD"
    assert (
        asset_key_from_broker_symbol(AssetClass.OPTION, "AAPL261120C00200000")
        == "option:default:alpaca:AAPL261120C00200000"
    )


def test_other_venues_never_collide_with_alpaca() -> None:
    ibkr = asset_key_from_broker_symbol(AssetClass.EQUITY, "AAPL", venue="ibkr")
    assert ibkr == "equity:ibkr:ibkr:AAPL"
    assert ibkr != asset_key_from_broker_symbol(AssetClass.EQUITY, "AAPL")


def test_other_venue_keys_ignore_symbol_case() -> None:
    assert asset_key_from_broker_symbol(AssetClass.EQUITY, "aapl", venue="ibkr") == "equity:ibkr:ibkr:AAPL"


def test_venue_spelling_is_canonical_and_alpaca_keys_unchanged() -> None:
    assert asset_key_from_broker_symbol(AssetClass.EQUITY, "AAPL", venue="IBKR") == "equity:ibkr:ibkr:AAPL"
    assert asset_key_from_broker_symbol(AssetClass.EQUITY, "AAPL", venue=" Ibkr ") == "equity:ibkr:ibkr:AAPL"
    assert asset_key_from_broker_symbol(AssetClass.EQUITY, "AAPL", venue="ALPACA") == "equity:default:alpaca:AAPL"


@pytest.mark.parametrize("venue", ["", "   ", "ibkr2", "default", None, 7])
def test_empty_or_unknown_venue_is_rejected(venue: object) -> None:
    with pytest.raises(ValueError):
        asset_key_from_broker_symbol(AssetClass.EQUITY, "AAPL", venue=venue)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        AccountRef(venue, "PA123")  # type: ignore[arg-type]


def test_account_ref_venue_is_canonical() -> None:
    assert AccountRef("ALPACA", "PA9") == AccountRef("alpaca", "PA9")
    assert AccountRef(" Alpaca ", "PA9").key == "alpaca:PA9"


def test_every_venue_is_already_canonical() -> None:
    assert all(canonical_venue(v) == v for v in VENUES)
