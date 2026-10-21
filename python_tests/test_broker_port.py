"""Phase A0.1: BrokerPort, neutral aliases, AccountRef and static typing."""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

from tradepulse.broker import AlpacaClient
from tradepulse.broker.port import AccountRef, BrokerPort, account_ref

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
        [sys.executable, "-m", "mypy", "python_tests/typing/port_conformance.py"],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
