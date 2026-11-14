"""The mortal bot: it trades to survive, and it can die.

Two events drive it:
  on_price(price, ts)      -- any time a fresh price arrives: check the stop-loss and vital signs
  on_candle_close(candles) -- once per closed candle: run the strategy, trail the stop

Every state change is saved immediately, so a crash or restart never loses the ledger.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from decimal import Decimal

from .config import BotConfig
from .exchange import Broker
from .life import Health, Vitals, assess, obituary, size_multiplier
from .models import Candle, ExchangeError, OrderRejected, SymbolRules, peso, to_dec, ts_str
from .risk import entry_quote_amount
from .state import BotState, StateStore
from .strategy import Action, Decision, decide

log = logging.getLogger("mortalbot")


class MortalBot:
    def __init__(self, cfg: BotConfig, state: BotState, broker: Broker, rules: SymbolRules,
                 store: StateStore | None = None):
        self.cfg = cfg
        self.state = state
        self.broker = broker
        self.rules = rules
        self.store = store
        self.fee_rate = cfg.execution.fee_rate
        self.obituary: str | None = None

    # ---- vital signs ------------------------------------------------------

    @property
    def alive(self) -> bool:
        return self.state.alive

    @property
    def in_position(self) -> bool:
        return self.state.position_qty > 0

    def equity(self, price: Decimal) -> Decimal:
        """Cash plus what the coins would fetch if sold right now, after fees."""
        return self.state.cash + self.state.position_qty * price * (1 - self.fee_rate)

    # ---- events -----------------------------------------------------------

    def on_price(self, price: Decimal, ts: int) -> Vitals | None:
        if not self.alive:
            return None
        s = self.state
        if self.in_position and s.stop_price is not None and price <= s.stop_price:
            self._exit(price, ts, f"stop-loss (stop {peso(s.stop_price)})")
        return self._check_vitals(price, ts)

    def on_candle_close(self, candles: Sequence[Candle]) -> Vitals | None:
        if not self.alive or not candles:
            return None
        s = self.state
        last = candles[-1]
        if last.close_time <= s.last_candle_close_time:
            return None  # already handled this candle
        s.last_candle_close_time = last.close_time
        price, ts = to_dec(last.close), last.close_time
        decision = decide(candles, self.cfg.strategy, self.in_position)
        if self.in_position:
            if decision.action is Action.EXIT:
                self._exit(price, ts, decision.reason)
            else:
                self._trail_stop(price, decision)
        elif s.rest_candles_left > 0:
            s.rest_candles_left -= 1
            log.info("%s resting after a losing streak (%d candles left)", ts_str(ts), s.rest_candles_left)
        elif decision.action is Action.ENTER:
            self._enter(price, ts, decision)
        vitals = self._check_vitals(price, ts)
        self._save()
        return vitals

    # ---- actions ----------------------------------------------------------

    def _enter(self, price: Decimal, ts: int, decision: Decision) -> None:
        s = self.state
        eq = self.equity(price)
        mult = size_multiplier(Health(s.health), self.cfg.life)
        amount, why = entry_quote_amount(
            equity=eq, cash=s.cash, price=price, atr=to_dec(decision.atr), multiplier=mult,
            risk=self.cfg.risk, fee_rate=self.fee_rate, min_notional=self.rules.min_notional,
            quote_step=self.rules.quote_step,
        )
        if amount <= 0:
            log.info("%s skip entry: %s", ts_str(ts), why)
            return
        try:
            fill = self.broker.market_buy(self.rules, amount)
        except (OrderRejected, ExchangeError) as e:
            log.warning("%s buy failed, no position taken: %s", ts_str(ts), e)
            return
        if fill.base_qty <= 0:
            log.warning("%s buy order filled nothing", ts_str(ts))
            return
        stop_distance = to_dec(decision.atr) * to_dec(self.cfg.risk.atr_stop_mult)
        s.cash -= fill.quote_qty
        s.position_qty += fill.base_qty
        s.position_cost += fill.quote_qty
        s.entry_price = fill.avg_price
        s.entry_time = ts
        s.stop_price = fill.avg_price - stop_distance
        s.fees_paid += fill.fee_quote
        log.info("%s BUY  %s %s for %s at %s | stop %s | %s (%s, x%.2f size)",
                 ts_str(ts), fill.base_qty, self.rules.base_asset, peso(fill.quote_qty), peso(fill.avg_price),
                 peso(s.stop_price), decision.reason, why, mult)
        self._save()

    def _exit(self, price: Decimal, ts: int, reason: str) -> bool:
        s = self.state
        qty = self.rules.round_qty(s.position_qty)
        if qty <= 0 or qty < self.rules.min_qty or qty * price < self.rules.min_notional:
            log.warning("%s cannot sell %s %s: below the exchange minimum", ts_str(ts), s.position_qty, self.rules.base_asset)
            return False
        try:
            fill = self.broker.market_sell(self.rules, qty)
        except (OrderRejected, ExchangeError) as e:
            log.error("%s SELL FAILED (%s); still holding the position: %s", ts_str(ts), reason, e)
            return False
        before = s.position_qty
        remainder = before - fill.base_qty
        closed = remainder <= 0 or remainder < self.rules.min_qty or remainder * price < self.rules.min_notional
        cost = s.position_cost if closed else s.position_cost * fill.base_qty / before
        pnl = fill.quote_qty - cost
        s.cash += fill.quote_qty
        s.fees_paid += fill.fee_quote
        s.trades.append({
            "entry_time": s.entry_time, "exit_time": ts, "entry_price": str(s.entry_price),
            "exit_price": str(fill.avg_price), "qty": str(fill.base_qty), "cost": str(cost),
            "proceeds": str(fill.quote_qty), "pnl": str(pnl), "reason": reason,
        })
        if closed:
            s.dust_qty += max(remainder, Decimal("0"))
            s.position_qty = Decimal("0")
            s.position_cost = Decimal("0")
            s.entry_price = s.entry_time = s.stop_price = None
        else:
            s.position_qty = remainder
            s.position_cost -= cost
        log.info("%s SELL %s %s for %s at %s | P&L %s | %s",
                 ts_str(ts), fill.base_qty, self.rules.base_asset, peso(fill.quote_qty), peso(fill.avg_price),
                 peso(pnl), reason)
        self._after_trade(pnl, ts)
        self._save()
        return True

    def _after_trade(self, pnl: Decimal, ts: int) -> None:
        s, r = self.state, self.cfg.risk
        if pnl >= 0:
            s.consecutive_losses = 0
            return
        s.consecutive_losses += 1
        if r.max_consecutive_losses and s.consecutive_losses >= r.max_consecutive_losses and r.rest_candles:
            s.rest_candles_left = r.rest_candles
            s.consecutive_losses = 0
            log.warning("%s %d losses in a row: licking its wounds for %d candles",
                        ts_str(ts), r.max_consecutive_losses, r.rest_candles)

    def _trail_stop(self, price: Decimal, decision: Decision) -> None:
        s = self.state
        if not self.cfg.risk.trailing_stop or decision.atr <= 0 or s.stop_price is None:
            return
        candidate = price - to_dec(decision.atr) * to_dec(self.cfg.risk.atr_stop_mult)
        if candidate > s.stop_price:
            s.stop_price = candidate

    # ---- life and death ---------------------------------------------------

    def _check_vitals(self, price: Decimal, ts: int) -> Vitals:
        s = self.state
        v = assess(self.equity(price), self.cfg.life, self.rules.min_notional, previous=Health(s.health))
        s.last_equity = v.equity
        if v.equity > s.peak_equity:
            s.peak_equity = v.equity
        if v.health.value != s.health:
            level = logging.WARNING if v.health in (Health.WOUNDED, Health.CRITICAL, Health.DEAD) else logging.INFO
            log.log(level, "%s %s -> %s | equity %s (%.1f%% of life)",
                    ts_str(ts), s.health, v.health.value, peso(v.equity), v.life_left * 100)
            s.health = v.health.value
        if v.health is Health.DEAD:
            self._die(v.cause_of_death or "unknown", price, ts)
        return v

    def _die(self, cause: str, price: Decimal, ts: int) -> None:
        s = self.state
        if self.in_position:
            log.warning("%s last breath: selling everything", ts_str(ts))
            self._exit(price, ts, "last breath")
            if self.in_position:  # unsellable remainder
                s.dust_qty += s.position_qty
                s.position_qty = Decimal("0")
                s.position_cost = Decimal("0")
                s.entry_price = s.entry_time = s.stop_price = None
        s.alive = False
        s.died_at = ts
        s.cause_of_death = cause
        s.health = Health.DEAD.value
        s.last_equity = s.cash
        self.obituary = obituary(s)
        log.critical("%s %s HAS DIED. %s", ts_str(ts), s.name, cause)
        if self.store:
            self.store.save(s)
            self.store.write_obituary(self.obituary)

    def _save(self) -> None:
        if self.store:
            self.store.save(self.state)
