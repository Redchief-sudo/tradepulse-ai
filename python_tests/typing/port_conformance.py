"""Checked by mypy only (see test_broker_port.py); never imported or executed.

The file name does not match pytest's test_*.py pattern, so it is not collected."""
from __future__ import annotations

from datetime import UTC, datetime

from tradepulse.broker import AlpacaClient
from tradepulse.broker.port import BrokerPort
from tradepulse.broker.simulated import ManualClock, SimSession, SimulatedBroker

_a: BrokerPort = AlpacaClient("k", "s", "paper", 10)
_s: BrokerPort = SimulatedBroker(clock=ManualClock(datetime(2026, 1, 2, tzinfo=UTC)), session=SimSession.always_open())
