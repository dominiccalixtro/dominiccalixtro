"""Market snapshot: turn raw candles into a compact table Claude can read cheaply.

Code does the arithmetic (changes, trend, volatility); Claude does the judgement.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .indicators import atr, ema
from .models import Candle


@dataclass(frozen=True)
class CoinSnapshot:
    symbol: str
    price: float
    chg_1h: float
    chg_24h: float
    chg_7d: float
    trend: str  # "up", "down" or "mixed"
    atr_pct: float  # typical hourly move, % of price
    high_24h: float
    low_24h: float


def _chg(closes: Sequence[float], hours: int) -> float:
    if len(closes) <= hours or closes[-1 - hours] == 0:
        return 0.0
    return (closes[-1] / closes[-1 - hours] - 1) * 100


def snapshot(symbol: str, candles: Sequence[Candle]) -> CoinSnapshot | None:
    """`candles` are 1h candles, oldest first; 200 gives a full picture."""
    if len(candles) < 30:
        return None
    closes = [c.close for c in candles]
    e20, e50 = ema(closes, 20)[-1], ema(closes, min(50, len(closes)))[-1]
    p = closes[-1]
    if e20 and e50 and p > e20 > e50:
        trend = "up"
    elif e20 and e50 and p < e20 < e50:
        trend = "down"
    else:
        trend = "mixed"
    a = atr(candles, 14)[-1] or 0.0
    day = candles[-24:]
    return CoinSnapshot(symbol, p, _chg(closes, 1), _chg(closes, 24), _chg(closes, 168), trend,
                        a / p * 100 if p else 0.0, max(c.high for c in day), min(c.low for c in day))


def render(snaps: Sequence[CoinSnapshot]) -> str:
    rows = ["symbol   | price (PHP)      | 1h     | 24h    | 7d     | trend | hourly move | 24h range"]
    for s in snaps:
        rows.append(f"{s.symbol:<8} | {s.price:>16,.2f} | {s.chg_1h:+5.1f}% | {s.chg_24h:+5.1f}% | "
                    f"{s.chg_7d:+5.1f}% | {s.trend:<5} | {s.atr_pct:5.2f}%      | "
                    f"{s.low_24h:,.2f} - {s.high_24h:,.2f}")
    return "\n".join(rows)
