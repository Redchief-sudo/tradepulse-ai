"""Real technical indicator formulas -- direct port of base44/shared/indicators.ts.
Computed from actual OHLCV price series. No LLM, no estimation.

Operates on plain floats (matching the TS/IEEE-754 numeric semantics being
ported) rather than Decimal -- these are dimensionless statistical scores,
not money, so float precision is the correct and faithful choice here.
"""

from __future__ import annotations

from dataclasses import dataclass


def sma(values: list[float], period: int) -> float | None:
    if not values or len(values) < period:
        return None
    return sum(values[-period:]) / period


def ema_series(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    k = 2 / (period + 1)
    out = [values[0]]
    for value in values[1:]:
        out.append(value * k + out[-1] * (1 - k))
    return out


def ema(values: list[float], period: int) -> float | None:
    series = ema_series(values, period)
    return series[-1] if series else None


def rsi(closes: list[float], period: int = 14) -> float | None:
    if not closes or len(closes) < period + 1:
        return None
    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        diff = closes[i] - closes[i - 1]
        if diff >= 0:
            gains += diff
        else:
            losses -= diff
    avg_gain = gains / period
    avg_loss = losses / period
    for i in range(period + 1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gain = diff if diff > 0 else 0.0
        loss = -diff if diff < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


@dataclass(frozen=True, slots=True)
class MacdResult:
    macd: float
    signal: float
    histogram: float


def macd(closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9) -> MacdResult | None:
    if not closes or len(closes) < slow + signal:
        return None
    fast_ema = ema_series(closes, fast)
    slow_ema = ema_series(closes, slow)
    macd_line = [f - s for f, s in zip(fast_ema, slow_ema)]
    signal_line = ema_series(macd_line, signal)
    i = len(macd_line) - 1
    return MacdResult(macd=macd_line[i], signal=signal_line[i], histogram=macd_line[i] - signal_line[i])


@dataclass(frozen=True, slots=True)
class BollingerResult:
    upper: float
    middle: float
    lower: float
    percent_b: float


def bollinger(closes: list[float], period: int = 20, mult: float = 2) -> BollingerResult | None:
    if not closes or len(closes) < period:
        return None
    window = closes[-period:]
    mean = sum(window) / period
    variance = sum((c - mean) ** 2 for c in window) / period
    sd = variance**0.5
    last = closes[-1]
    width = mult * sd
    percent_b = ((last - (mean - width)) / (2 * width)) * 100 if width > 0 else 50.0
    return BollingerResult(upper=mean + width, middle=mean, lower=mean - width, percent_b=percent_b)


def atr(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> float | None:
    if not closes or len(closes) < period + 1:
        return None
    trs = []
    for i in range(1, len(closes)):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        trs.append(tr)
    value = sum(trs[:period]) / period
    for i in range(period, len(trs)):
        value = (value * (period - 1) + trs[i]) / period
    return value


def momentum(closes: list[float], period: int = 14) -> float | None:
    if not closes or len(closes) < period + 1:
        return None
    past = closes[-1 - period]
    if past == 0:
        return None
    return ((closes[-1] - past) / past) * 100


def volatility(closes: list[float], period: int = 20) -> float | None:
    if not closes or len(closes) < period + 1:
        return None
    n = len(closes)
    returns = [(closes[i] - closes[i - 1]) / closes[i - 1] for i in range(n - period, n)]
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / len(returns)
    return (variance**0.5) * (365**0.5) * 100
