"""Technical indicators. Pure functions over plain lists, no dependencies."""

from __future__ import annotations

from collections.abc import Sequence

from .models import Candle


def ema(values: Sequence[float], period: int) -> list[float | None]:
    """Exponential moving average, seeded with the simple average of the first `period` values."""
    if period < 1:
        raise ValueError("period must be >= 1")
    out: list[float | None] = [None] * len(values)
    if len(values) < period:
        return out
    prev = sum(values[:period]) / period
    out[period - 1] = prev
    k = 2.0 / (period + 1)
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def true_ranges(candles: Sequence[Candle]) -> list[float]:
    out: list[float] = []
    for i, c in enumerate(candles):
        if i == 0:
            out.append(c.high - c.low)
        else:
            pc = candles[i - 1].close
            out.append(max(c.high - c.low, abs(c.high - pc), abs(c.low - pc)))
    return out


def atr(candles: Sequence[Candle], period: int) -> list[float | None]:
    """Average True Range with Wilder's smoothing: how far price typically moves per candle."""
    if period < 1:
        raise ValueError("period must be >= 1")
    trs = true_ranges(candles)
    out: list[float | None] = [None] * len(trs)
    if len(trs) < period:
        return out
    prev = sum(trs[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(trs)):
        prev = (prev * (period - 1) + trs[i]) / period
        out[i] = prev
    return out
