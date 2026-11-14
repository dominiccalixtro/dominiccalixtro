"""The trend rule: own a coin only while it is in a daily uptrend.

Uptrend = the latest daily close is above the average close of the last
`trend_days` days (50 by default). Long only: spot markets cannot short, so
"downtrend" simply means "hold pesos".

Evidence (Coins.ph daily candles, Oct 2022 - Nov 2026, 0.4% cost per side):
  BTC  buy & hold +181%, worst drop 50%   |   trend rule +201%, worst drop 26%
  ETH  buy & hold  +19%, worst drop 65%   |   trend rule +131%, worst drop 34%
About 1 in 4 trend trades won, but winners averaged ~+25% and losers ~-3%.
One market cycle, in-sample, a handful of variants tried: treat it as
evidence for the idea, not a forecast. See research/trend_filters.py.

The rules brain trades this rule mechanically (and backtests use it). The
Claude brain trades inside it (agent.trend_rule = "enforce").
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum

from .config import StrategyConfig, warmup_candles
from .indicators import atr
from .models import Candle


class Action(str, Enum):
    ENTER = "ENTER"
    EXIT = "EXIT"
    HOLD = "HOLD"


@dataclass(frozen=True)
class Decision:
    action: Action
    reason: str
    atr: float = 0.0


@dataclass(frozen=True)
class Regime:
    """The daily trend of one coin, from closed daily candles."""

    symbol: str
    up: bool
    close: float
    average: float
    days: int  # length of the average
    close_time: int  # when the latest daily candle closed
    streak: int  # consecutive daily closes on the current side of the average
    chg_30d: float  # %
    atr_pct: float  # typical daily move, % of price

    @property
    def distance_pct(self) -> float:
        return (self.close / self.average - 1) * 100 if self.average else 0.0


def regime(symbol: str, daily: Sequence[Candle], days: int, atr_period: int = 14) -> Regime | None:
    """`daily` = closed daily candles, oldest first. None if there is not enough history."""
    if len(daily) < days + 1:
        return None
    closes = [c.close for c in daily]

    def above(i: int) -> bool:
        return closes[i] > sum(closes[i - days + 1:i + 1]) / days

    up = above(len(closes) - 1)
    streak = 0
    for i in range(len(closes) - 1, days - 2, -1):
        if above(i) != up:
            break
        streak += 1
    avg = sum(closes[-days:]) / days
    a = atr(daily, atr_period)[-1] or 0.0
    chg = (closes[-1] / closes[-31] - 1) * 100 if len(closes) > 30 else 0.0
    return Regime(symbol, up, closes[-1], avg, days, daily[-1].close_time, streak, chg,
                  a / closes[-1] * 100 if closes[-1] else 0.0)


def decide(candles: Sequence[Candle], cfg: StrategyConfig, in_position: bool) -> Decision:
    """Mechanical version of the trend rule, for the rules brain and backtests."""
    if len(candles) < warmup_candles(cfg):
        return Decision(Action.HOLD, f"warming up ({len(candles)}/{warmup_candles(cfg)} candles)")
    r = regime("", candles, cfg.trend_days, cfg.atr_period)
    vol = atr(candles, cfg.atr_period)[-1] or 0.0
    if r is None:
        return Decision(Action.HOLD, "not enough history", vol)
    if not in_position and r.up:
        return Decision(Action.ENTER, f"close above its {cfg.trend_days}-candle average", vol)
    if in_position and not r.up:
        return Decision(Action.EXIT, f"trend exit: close below its {cfg.trend_days}-candle average", vol)
    return Decision(Action.HOLD, "trend unchanged", vol)
