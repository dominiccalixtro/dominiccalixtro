"""The rules brain: one market, EMA crossover. No API key needed.

Used for backtests (an LLM cannot be honestly backtested: it may already
know what happened next) and as a simple alternative to the Claude brain.

Two events drive it:
  on_price(price, ts)      -- any fresh price: reflexes (stop-loss) and vital signs
  on_candle_close(candles) -- once per closed candle: run the strategy, trail the stop
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from decimal import Decimal

from .body import Body
from .config import BotConfig
from .life import Health, Vitals, size_multiplier
from .models import Candle, SymbolRules, to_dec, ts_str
from .risk import entry_quote_amount
from .state import BotState, StateStore
from .strategy import Action, Decision, decide

log = logging.getLogger("mortalbot")


class MortalBot:
    def __init__(self, cfg: BotConfig, state: BotState, broker, rules: SymbolRules,
                 store: StateStore | None = None):
        self.cfg = cfg
        self.symbol = rules.symbol
        self.rules = rules
        self.body = Body(cfg, state, broker, {rules.symbol: rules}, store)

    @property
    def state(self) -> BotState:
        return self.body.state

    @property
    def alive(self) -> bool:
        return self.body.alive

    @property
    def obituary(self) -> str | None:
        return self.body.obituary

    @property
    def in_position(self) -> bool:
        return self.symbol in self.state.positions

    def equity(self, price: Decimal | None = None) -> Decimal:
        if price is not None:
            self.body.mark({self.symbol: price})
        return self.body.equity()

    def on_price(self, price: Decimal, ts: int) -> Vitals | None:
        if not self.alive:
            return None
        self.body.mark({self.symbol: price})
        self.body.reflexes(ts)
        return self.body.check_vitals(ts)

    def on_candle_close(self, candles: Sequence[Candle]) -> Vitals | None:
        if not self.alive or not candles:
            return None
        s = self.state
        last = candles[-1]
        if last.close_time <= s.last_candle_close_time:
            return None  # already handled this candle
        s.last_candle_close_time = last.close_time
        price, ts = to_dec(last.close), last.close_time
        self.body.mark({self.symbol: price})
        decision = decide(candles, self.cfg.strategy, self.in_position)
        if self.in_position:
            if decision.action is Action.EXIT:
                self.body.sell(self.symbol, ts, decision.reason)
            else:
                self._trail_stop(price, decision)
        elif s.rest_candles_left > 0:
            s.rest_candles_left -= 1
            log.info("%s resting after a losing streak (%d candles left)", ts_str(ts), s.rest_candles_left)
        elif decision.action is Action.ENTER:
            self._enter(price, ts, decision)
        vitals = self.body.check_vitals(ts)
        self.body.save()
        return vitals

    def _enter(self, price: Decimal, ts: int, decision: Decision) -> None:
        s = self.state
        mult = size_multiplier(Health(s.health), self.cfg.life)
        amount, why = entry_quote_amount(
            equity=self.body.equity(), cash=s.cash, price=price, atr=to_dec(decision.atr), multiplier=mult,
            risk=self.cfg.risk, fee_rate=self.body.fee_rate, min_notional=self.rules.min_notional,
            quote_step=self.rules.quote_step,
        )
        if amount <= 0:
            log.info("%s skip entry: %s", ts_str(ts), why)
            return
        stop_distance = to_dec(decision.atr) * to_dec(self.cfg.risk.atr_stop_mult)
        self.body.buy(self.symbol, amount, ts, stop_distance=stop_distance,
                      reason=f"{decision.reason} ({why}, x{mult:.2f} size)")

    def _trail_stop(self, price: Decimal, decision: Decision) -> None:
        pos = self.state.positions[self.symbol]
        if not self.cfg.risk.trailing_stop or decision.atr <= 0 or pos.stop_price is None:
            return
        candidate = price - to_dec(decision.atr) * to_dec(self.cfg.risk.atr_stop_mult)
        if candidate > pos.stop_price:
            pos.stop_price = candidate
