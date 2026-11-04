"""DBAE evidence for gateway tests written before DBAE (spec §3, §4, §5, §6).

FullBandCapabilities is a SimulatedCapabilities whose account mirrors the
test's own broker account (cash, equity, buying power and last_equity, read
through the broker the test already mocks), with every DBAE field verified,
received at the gateway's own clock. With a funded test account that is the
FULL band, every asset tradable and fractionable, and funding equal to the
broker's cash less pending buys: the pre-DBAE tests keep their behaviour.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import Decimal

from tradepulse.broker.capabilities import AccountEvidence, AssetTradingCapabilities, DailyEquityBar
from tradepulse.broker.port import BrokerPort
from tradepulse.broker.simulated import SimulatedCapabilities
from tradepulse.calendar import ny_date
from tradepulse.models import AssetClass
from tradepulse.models.market import AssetIdentity

CRYPTO_INCREMENT = Decimal("0.00000001")  # the risk engine's crypto precision: shaping never changes its quantity
BAR_DAYS = 14  # covers the previous trading date across any weekend or holiday run


class FullBandCapabilities(SimulatedCapabilities):
    def __init__(self, broker: BrokerPort, clock: Callable[[], datetime]) -> None:
        super().__init__()
        self._broker = broker
        self._clock = clock

    async def get_account_evidence(self) -> AccountEvidence:
        account = await self._broker.get_account()
        return AccountEvidence(
            status="ACTIVE", trading_blocked=False, account_blocked=False, trade_suspended_by_user=False,
            suspend_trade=False, closing_transactions_only=False, fractional_trading=True,
            equity=account.equity, cash=account.cash, buying_power=account.buying_power,
            non_marginable_buying_power=account.buying_power, account_multiplier=Decimal(4),
            crypto_status="ACTIVE", options_trading_level=3, received_at=self._clock())

    async def get_asset_capabilities(self, asset: AssetIdentity) -> AssetTradingCapabilities:
        if asset.asset_class is AssetClass.CRYPTO:
            return AssetTradingCapabilities(True, True, True, CRYPTO_INCREMENT, CRYPTO_INCREMENT, None, self._clock())
        if asset.asset_class is AssetClass.OPTION:
            return AssetTradingCapabilities(True, True, False, None, None, None, self._clock())
        return AssetTradingCapabilities(True, True, True, None, None, None, self._clock())

    async def get_daily_equity_bars(self) -> list[DailyEquityBar]:
        """One bar per day before today at the broker's last_equity, so the
        bar for the previous trading date agrees with it (spec §4.3)."""
        account = await self._broker.get_account()
        today = ny_date(self._clock())
        return [DailyEquityBar(today - timedelta(days=n), account.last_equity) for n in range(BAR_DAYS, 0, -1)]

