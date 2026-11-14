"""Candle data: CSV load/save and a synthetic market generator.

Synthetic data exists to exercise the bot (does it die when it should, does
it survive when it should). It says nothing about real-world profitability;
for that, backtest on real Coins.ph candles (`mortalbot fetch`).
"""

from __future__ import annotations

import csv
import math
import random
from pathlib import Path

from .models import Candle

FIELDS = ["open_time", "open", "high", "low", "close", "volume", "close_time"]

# (drift per candle, volatility per candle) in log-return terms
REGIMES = {
    "bull": (0.0006, 0.006),
    "bear": (-0.0006, 0.008),
    "chop": (0.0, 0.006),
    "crash": (-0.004, 0.014),
}


def load_csv(path: str | Path) -> list[Candle]:
    with open(path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    missing = set(FIELDS) - set(rows[0].keys() if rows else FIELDS)
    if missing:
        raise ValueError(f"{path}: missing columns {sorted(missing)}")
    candles = [Candle(int(r["open_time"]), float(r["open"]), float(r["high"]), float(r["low"]),
                      float(r["close"]), float(r["volume"]), int(r["close_time"])) for r in rows]
    candles.sort(key=lambda c: c.open_time)
    return candles


def save_csv(candles: list[Candle], path: str | Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(FIELDS)
        for c in candles:
            w.writerow([c.open_time, c.open, c.high, c.low, c.close, c.volume, c.close_time])


def synthetic(n: int = 3000, *, regime: str = "mixed", seed: int = 7, start_price: float = 5_000_000.0,
              interval_ms: int = 3_600_000, start_ms: int = 1_767_225_600_000) -> list[Candle]:
    """Random-walk candles. regime: bull, bear, chop, crash, or mixed (random segments of each)."""
    if regime != "mixed" and regime not in REGIMES:
        raise ValueError(f"regime must be mixed or one of {sorted(REGIMES)}")
    rng = random.Random(seed)
    plan: list[str] = []
    if regime == "mixed":
        while len(plan) < n:
            plan += [rng.choice(["bull", "bear", "chop", "chop"])] * rng.randint(150, 450)
    else:
        plan = [regime] * n
    price, out = start_price, []
    for i in range(n):
        drift, vol = REGIMES[plan[i]]
        o = price
        hi = lo = o
        for _ in range(12):  # sub-steps give each candle a realistic high/low
            price *= math.exp(rng.gauss(drift / 12, vol / math.sqrt(12)))
            hi, lo = max(hi, price), min(lo, price)
        t = start_ms + i * interval_ms
        out.append(Candle(t, round(o, 2), round(hi, 2), round(lo, 2), round(price, 2),
                          round(rng.uniform(0.2, 4.0), 6), t + interval_ms - 1))
    return out
