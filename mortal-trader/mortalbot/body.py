"""The body: ledger, execution, reflexes, metabolism and death.

Every brain (the Claude agent or the simple rules bot) acts through a Body.
The body enforces what no brain can override: it only spends its own money,
it sells at the stop-loss without asking, and it dies when its money is gone.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from decimal import Decimal

from .config import BotConfig
from .life import Health, Vitals, assess, obituary
from .models import ExchangeError, Fill, OrderRejected, SymbolRules, peso, ts_str
from .state import BotState, Position, StateStore

log = logging.getLogger("mortalbot")
DAY_MS = 86_400_000
INTERVAL_MS = {"1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000, "1h": 3_600_000,
               "2h": 7_200_000, "4h": 14_400_000, "6h": 21_600_000, "8h": 28_800_000, "12h": 43_200_000,
               "1d": DAY_MS, "3d": 3 * DAY_MS, "1w": 7 * DAY_MS}


class Body:
    def __init__(self, cfg: BotConfig, state: BotState, broker, rules: Mapping[str, SymbolRules],
                 store: StateStore | None = None):
        if not rules:
            raise ValueError("the body needs at least one tradable market")
        self.cfg = cfg
        self.state = state
        self.broker = broker
        self.rules = dict(rules)
        self.store = store
        self.fee_rate = cfg.execution.fee_rate
        self.obituary: str | None = None

    # ---- senses -----------------------------------------------------------

    @property
    def alive(self) -> bool:
        return self.state.alive

    @property
    def min_order(self) -> Decimal:
        """The cheapest order any of its markets accepts; below this it can never trade again."""
        return min(r.min_notional for r in self.rules.values())

    def mark(self, prices: Mapping[str, Decimal]) -> None:
        for sym, p in prices.items():
            if p and p > 0:
                self.state.last_prices[sym] = p

    def price(self, symbol: str) -> Decimal | None:
        return self.state.last_prices.get(symbol)

    def position_value(self, pos: Position) -> Decimal:
        """What the coins would fetch if sold right now, after fees."""
        p = self.price(pos.symbol) or pos.entry_price
        return pos.qty * p * (1 - self.fee_rate)

    def invested(self) -> Decimal:
        return sum((self.position_value(p) for p in self.state.positions.values()), Decimal("0"))

    def equity(self) -> Decimal:
        return self.state.cash + self.invested()

    # ---- actions ----------------------------------------------------------

    def buy(self, symbol: str, pesos: Decimal, ts: int, *, stop_distance: Decimal | None = None,
            stop_pct: Decimal | None = None, take_profit_pct: Decimal | None = None, reason: str = "",
            thesis: str = "") -> Fill | None:
        s = self.state
        rules = self.rules[symbol]
        spendable = max(s.cash, Decimal("0")) / (1 + self.fee_rate)
        pesos = rules.round_quote(min(pesos, spendable))
        if pesos < rules.min_notional:
            log.info("%s skip buy %s: %s is below the minimum order %s", ts_str(ts), symbol, peso(pesos),
                     peso(rules.min_notional))
            return None
        try:
            fill = self.broker.market_buy(rules, pesos)
        except (OrderRejected, ExchangeError) as e:
            log.warning("%s buy %s failed, nothing bought: %s", ts_str(ts), symbol, e)
            return None
        if fill.base_qty <= 0:
            log.warning("%s buy %s filled nothing", ts_str(ts), symbol)
            return None
        s.cash -= fill.quote_qty
        s.fees_paid += fill.fee_quote
        stop = None
        if stop_distance is not None:
            stop = fill.avg_price - stop_distance
        elif stop_pct is not None:
            stop = fill.avg_price * (1 - stop_pct / 100)
        tp = fill.avg_price * (1 + take_profit_pct / 100) if take_profit_pct else None
        old = s.positions.get(symbol)
        if old:  # adding to a position: average the entry, keep the tighter stop
            qty = old.qty + fill.base_qty
            old.entry_price = (old.cost + fill.quote_qty) / qty
            old.qty, old.cost = qty, old.cost + fill.quote_qty
            if stop is not None:
                old.stop_price = max(stop, old.stop_price) if old.stop_price else stop
            old.take_profit = tp or old.take_profit
            old.thesis = thesis or old.thesis
        else:
            s.positions[symbol] = Position(symbol, fill.base_qty, fill.quote_qty, fill.avg_price, ts, stop, tp, thesis)
        log.info("%s BUY  %s %s for %s at %s | stop %s%s | %s", ts_str(ts), fill.base_qty, rules.base_asset,
                 peso(fill.quote_qty), peso(fill.avg_price), peso(stop) if stop else "-",
                 f" | target {peso(tp)}" if tp else "", reason)
        self.save()
        return fill

    def sell(self, symbol: str, ts: int, reason: str, fraction: Decimal = Decimal("1")) -> Fill | None:
        s = self.state
        pos = s.positions.get(symbol)
        if pos is None:
            return None
        rules = self.rules.get(symbol)
        price = self.price(symbol) or pos.entry_price
        if rules is None:
            log.error("%s cannot sell %s: market rules unknown", ts_str(ts), symbol)
            return None
        fraction = min(max(fraction, Decimal("0")), Decimal("1"))
        qty = rules.round_qty(pos.qty * fraction)
        remainder_after = pos.qty - qty
        if remainder_after > 0 and (remainder_after < rules.min_qty or remainder_after * price < rules.min_notional):
            qty = rules.round_qty(pos.qty)  # a leftover too small to sell later: sell it all now
        if qty <= 0 or qty < rules.min_qty or qty * price < rules.min_notional:
            log.warning("%s cannot sell %s %s: below the exchange minimum", ts_str(ts), pos.qty, symbol)
            return None
        try:
            fill = self.broker.market_sell(rules, qty)
        except (OrderRejected, ExchangeError) as e:
            log.error("%s SELL %s FAILED (%s); still holding: %s", ts_str(ts), symbol, reason, e)
            return None
        remainder = pos.qty - fill.base_qty
        closed = remainder <= 0 or remainder < rules.min_qty or remainder * price < rules.min_notional
        cost = pos.cost if closed else pos.cost * fill.base_qty / pos.qty
        pnl = fill.quote_qty - cost
        s.cash += fill.quote_qty
        s.fees_paid += fill.fee_quote
        s.trades.append({
            "symbol": symbol, "entry_time": pos.entry_time, "exit_time": ts, "entry_price": str(pos.entry_price),
            "exit_price": str(fill.avg_price), "qty": str(fill.base_qty), "cost": str(cost),
            "proceeds": str(fill.quote_qty), "pnl": str(pnl), "reason": reason,
        })
        if closed:
            if remainder > 0:
                s.dust[symbol] = s.dust.get(symbol, Decimal("0")) + remainder
            del s.positions[symbol]
        else:
            pos.qty, pos.cost = remainder, pos.cost - cost
        log.info("%s SELL %s %s for %s at %s | P&L %s | %s", ts_str(ts), fill.base_qty, rules.base_asset,
                 peso(fill.quote_qty), peso(fill.avg_price), peso(pnl), reason)
        self._after_trade(pnl, ts)
        self.save()
        return fill

    def _after_trade(self, pnl: Decimal, ts: int) -> None:
        s, r = self.state, self.cfg.risk
        if pnl >= 0:
            s.consecutive_losses = 0
            return
        s.consecutive_losses += 1
        if r.max_consecutive_losses and s.consecutive_losses >= r.max_consecutive_losses and r.rest_candles:
            s.rest_candles_left = r.rest_candles
            s.rest_until = ts + r.rest_candles * INTERVAL_MS[self.cfg.interval]
            s.consecutive_losses = 0
            log.warning("%s %d losses in a row: licking its wounds until %s",
                        ts_str(ts), r.max_consecutive_losses, ts_str(s.rest_until))

    # ---- metabolism ---------------------------------------------------------

    def charge(self, pesos: Decimal, ts: int, what: str) -> None:
        """Spend life on something other than trading: thinking (API costs) or upkeep (server)."""
        if pesos <= 0:
            return
        s = self.state
        s.cash -= pesos
        if what == "thinking":
            s.thinking_paid += pesos
        else:
            s.upkeep_paid += pesos
        log.debug("%s charged %s for %s", ts_str(ts), peso(pesos), what)

    def accrue_upkeep(self, ts: int) -> None:
        per_day = self.cfg.agent.upkeep_php_per_day
        s = self.state
        if per_day <= 0 or ts <= s.upkeep_until:
            s.upkeep_until = max(s.upkeep_until, ts)
            return
        self.charge(per_day * (ts - s.upkeep_until) / DAY_MS, ts, "upkeep")
        s.upkeep_until = ts

    # ---- reflexes and death -------------------------------------------------

    def reflexes(self, ts: int) -> list[str]:
        """Stop-losses and take-profits fire here, every price tick, without asking any brain."""
        events = []
        for sym, pos in list(self.state.positions.items()):
            p = self.price(sym)
            if p is None:
                continue
            if pos.stop_price is not None and p <= pos.stop_price:
                if self.sell(sym, ts, f"stop-loss (stop {peso(pos.stop_price)})"):
                    events.append(f"stop-loss sold {sym} at about {peso(p)}")
            elif pos.take_profit is not None and p >= pos.take_profit:
                if self.sell(sym, ts, f"take-profit (target {peso(pos.take_profit)})"):
                    events.append(f"take-profit sold {sym} at about {peso(p)}")
        return events

    def check_vitals(self, ts: int) -> Vitals:
        s = self.state
        v = assess(self.equity(), self.cfg.life, self.min_order, previous=Health(s.health))
        s.last_equity = v.equity
        if v.equity > s.peak_equity:
            s.peak_equity = v.equity
        if v.health.value != s.health:
            level = logging.WARNING if v.health in (Health.WOUNDED, Health.CRITICAL, Health.DEAD) else logging.INFO
            log.log(level, "%s %s -> %s | equity %s (%.1f%% of life)",
                    ts_str(ts), s.health, v.health.value, peso(v.equity), v.life_left * 100)
            s.health = v.health.value
        if v.health is Health.DEAD:
            self.die(v.cause_of_death or "unknown", ts)
        return v

    def die(self, cause: str, ts: int) -> None:
        s = self.state
        for sym in list(s.positions):
            log.warning("%s last breath: selling %s", ts_str(ts), sym)
            self.sell(sym, ts, "last breath")
            pos = s.positions.pop(sym, None)
            if pos is not None:  # unsellable remainder
                s.dust[sym] = s.dust.get(sym, Decimal("0")) + pos.qty
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

    def save(self) -> None:
        if self.store:
            self.store.save(self.state)
