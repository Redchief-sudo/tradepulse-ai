"""The account lease a test's directly built gateways carry (see conftest.py's
autouse ``held_lease`` fixture). Tests that run the real order-capable CLI
commands take their own lease for their broker's account instead."""
from __future__ import annotations

from tradepulse.broker.port import AccountRef
from tradepulse.runtime_lease import AccountLease

TEST_ACCOUNT = AccountRef("alpaca", "TESTACCOUNT")
HELD: AccountLease | None = None


def held_test_lease() -> AccountLease:
    if HELD is None:
        raise RuntimeError("no test lease is held (conftest.py's held_lease fixture)")
    return HELD
