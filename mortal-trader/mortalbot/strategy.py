"""Trend-following strategy: EMA crossover with a long-term trend filter.

Enter (long only; spot markets cannot short):
  the fast EMA crosses above the slow EMA AND price is above the trend EMA.
Exit:
  the fast EMA crosses below the slow EMA (the stop-loss in bot.py also exits).

It is deliberately simple and transparent. Swap in your own `decide`
function; the life, risk and execution layers do not care how signals are made.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum

from .config import StrategyConfig, warmup_candles
from .indicators import atr, ema
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


def decide(candles: Sequence[Candle], cfg: StrategyConfig, in_position: bool) -> Decision:
    if len(candles) < warmup_candles(cfg):
        return Decision(Action.HOLD, f"warming up ({len(candles)}/{warmup_candles(cfg)} candles)")
    closes = [c.close for c in candles]
    fast = ema(closes, cfg.fast_ema)
    slow = ema(closes, cfg.slow_ema)
    trend = ema(closes, cfg.trend_ema)
    vol = atr(candles, cfg.atr_period)[-1] or 0.0
    f0, f1, s0, s1, t1 = fast[-2], fast[-1], slow[-2], slow[-1], trend[-1]
    crossed_up = f0 <= s0 and f1 > s1
    crossed_down = f0 >= s0 and f1 < s1
    if not in_position and crossed_up:
        if closes[-1] > t1:
            return Decision(Action.ENTER, f"EMA{cfg.fast_ema} crossed above EMA{cfg.slow_ema}, price above EMA{cfg.trend_ema}", vol)
        return Decision(Action.HOLD, "crossover up, but price is below the long-term trend", vol)
    if in_position and crossed_down:
        return Decision(Action.EXIT, f"EMA{cfg.fast_ema} crossed below EMA{cfg.slow_ema}", vol)
    return Decision(Action.HOLD, "no signal", vol)
