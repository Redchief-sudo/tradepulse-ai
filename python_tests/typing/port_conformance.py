"""Checked by mypy only (see test_broker_port.py); never imported or executed.

The file name does not match pytest's test_*.py pattern, so it is not collected."""
from __future__ import annotations

from tradepulse.broker import AlpacaClient
from tradepulse.broker.port import BrokerPort

_a: BrokerPort = AlpacaClient("k", "s", "paper", 10)
