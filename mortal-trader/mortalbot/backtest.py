"""Replay history through the exact same bot code that trades live.

Within each candle the simulated price path is: open -> stop (if the low
reached it) -> low -> close. Checking the low is deliberately pessimistic:
if the bot would have died at the candle's worst moment, it dies.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .bot import MortalBot
from .config import BotConfig
from .exchange.paper import PaperBroker, paper_rules
from .models import Candle, to_dec
from .state import BotState


@dataclass
class BacktestResult:
    bot: MortalBot
    candles_seen: int
    equity_curve: list[Decimal]
    buy_hold_return: float

    @property
    def state(self) -> BotState:
        return self.bot.state

    @property
    def final_equity(self) -> Decimal:
        return self.equity_curve[-1] if self.equity_curve else self.state.budget

    @property
    def total_return(self) -> float:
        return float(self.final_equity / self.state.budget - 1)

    @property
    def max_drawdown(self) -> float:
        peak, worst = Decimal("0"), 0.0
        for e in self.equity_curve:
            peak = max(peak, e)
            if peak > 0:
                worst = max(worst, float(1 - e / peak))
        return worst

    @property
    def wins(self) -> int:
        return sum(1 for t in self.state.trades if Decimal(t["pnl"]) > 0)


def run_backtest(candles: list[Candle], cfg: BotConfig) -> BacktestResult:
    if not candles:
        raise ValueError("no candles to backtest")
    ex = cfg.execution
    rules = paper_rules(cfg.symbol, cfg.base_asset, cfg.quote_asset, min_qty=ex.paper_min_qty,
                        step=ex.paper_step_size, min_notional=ex.paper_min_notional_php)
    broker = PaperBroker(ex.fee_rate, ex.slippage_bps)
    state = BotState.newborn(cfg.name, "backtest", cfg.symbol, cfg.life.budget_php, candles[0].open_time)
    bot = MortalBot(cfg, state, broker, rules, store=None)
    curve: list[Decimal] = []
    seen = 0

    def tick(price: float, ts: int) -> None:
        p = to_dec(price)
        broker.mark(p, ts)
        bot.on_price(p, ts)

    for i, c in enumerate(candles):
        seen = i + 1
        tick(c.open, c.open_time)
        stop = state.stop_price
        if bot.alive and bot.in_position and stop is not None and to_dec(c.low) <= stop < to_dec(c.open):
            tick(float(stop), c.open_time)
        if bot.alive:
            tick(c.low, c.open_time)
        if bot.alive:
            broker.mark(to_dec(c.close), c.close_time)
            bot.on_candle_close(candles[max(0, i - cfg.history + 1): i + 1])
        curve.append(bot.equity(to_dec(c.close)) if bot.alive else state.cash)
        if not bot.alive:
            break
    bh = candles[seen - 1].close / candles[0].open - 1
    return BacktestResult(bot, seen, curve, bh)
