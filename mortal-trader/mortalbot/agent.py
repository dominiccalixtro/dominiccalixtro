"""The autonomous Claude trader: reflexes every tick, thoughts when it matters.

Every price tick (cheap, no AI):
  - stop-losses and take-profits fire
  - upkeep is charged, vital signs checked, death handled
  - decide whether to wake the brain: its chosen check time arrived, a coin
    moved sharply since its last thought, or a stop/target just fired

A thought (costs money, paid from its life):
  - read the market snapshot, its positions, its journal and the news
  - Claude decides; guardrails review; the body executes
  - Claude writes a journal entry and picks when to think next
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from .body import Body
from .brain import BrainUnavailable, ClaudeBrain
from .config import BotConfig
from .guardrails import Order, review
from .market import CoinSnapshot, snapshot
from .models import Candle, peso, ts_str
from .news import Headline
from .prompts import briefing, local_time
from .state import BotState
from .strategy import Regime, regime

log = logging.getLogger("mortalbot")
MINUTE = 60_000


def next_local_midnight(now: int, offset_hours: int) -> int:
    local = datetime.fromtimestamp(now / 1000, tz=timezone.utc) + timedelta(hours=offset_hours)
    midnight = (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return int((midnight - timedelta(hours=offset_hours)).timestamp() * 1000)


REGIME_REFRESH_MS = 15 * MINUTE


class ClaudeTrader:
    def __init__(self, cfg: BotConfig, body: Body, brain: ClaudeBrain, *,
                 klines: Callable[[str, str, int], list[Candle]], headlines: Callable[[int], list[Headline]]):
        self.cfg = cfg
        self.body = body
        self.brain = brain
        self.klines = klines  # (symbol, interval, limit) -> candles, oldest first
        self.headlines = headlines
        self.tired_until = 0  # brain unavailable or out of budget: don't retry before this
        self._regimes: dict[str, Regime] = {}
        self._regimes_at = 0

    @property
    def state(self) -> BotState:
        return self.body.state

    @property
    def alive(self) -> bool:
        return self.body.alive

    def symbols(self) -> list[str]:
        """What Claude may name: its tradable universe plus anything it still holds."""
        return sorted(set(self.cfg.agent.universe) & set(self.body.rules) | set(self.state.positions))

    # ---- the daily trend (free: code only) --------------------------------------

    def regimes(self, now: int) -> dict[str, Regime]:
        """Daily trend per coin from closed daily candles, refreshed every 15 minutes."""
        if self.cfg.agent.trend_rule == "off":
            return {}
        if now - self._regimes_at < REGIME_REFRESH_MS and self._regimes:
            return self._regimes
        days = self.cfg.strategy.trend_days
        fresh = {}
        for sym in self.symbols():
            try:
                daily = [c for c in self.klines(sym, "1d", days + 40) if c.close_time < now]
                r = regime(sym, daily, days, self.cfg.strategy.atr_period)
            except Exception as e:  # keep the last known trend rather than none
                log.warning("no daily candles for %s: %s", sym, e)
                r = self._regimes.get(sym)
            if r:
                fresh[sym] = r
        self._regimes, self._regimes_at = fresh, now
        return fresh

    def _trend_reflexes(self, now: int) -> list[str]:
        """Wake on trend flips; under "enforce", sell a coin after a daily close below its average."""
        s, a = self.state, self.cfg.agent
        events = []
        regimes = self.regimes(now)
        for sym, r in regimes.items():
            before = s.regime_seen.get(sym)
            s.regime_seen[sym] = r.up
            if before is not None and before != r.up:
                side = "above" if r.up else "below"
                events.append(f"{sym} turned {'UP' if r.up else 'DOWN'}: its daily close is {side} its "
                              f"{r.days}-day average")
        if a.trend_rule == "enforce":
            for sym, pos in list(s.positions.items()):
                r = regimes.get(sym)
                if r and not r.up and r.close_time > pos.entry_time:
                    fill = self.body.sell(sym, now, f"trend exit: daily close {peso(r.close)} below its "
                                                    f"{r.days}-day average {peso(r.average)}")
                    if fill:
                        events.append(f"the trend rule sold {sym} (daily close below its {r.days}-day average)")
        return events

    # ---- every tick -----------------------------------------------------------

    def step(self, prices: dict[str, Decimal], now: int) -> None:
        if not self.alive:
            return
        self.body.mark(prices)
        events = self.body.reflexes(now) + self._trend_reflexes(now)
        self.state.pending_wake = (self.state.pending_wake + events)[-10:]
        self.body.accrue_upkeep(now)
        self.body.check_vitals(now)
        if not self.alive:
            return
        reason = self.wake_reason(now)
        if reason:
            self.think(now, reason)
        self.body.save()

    def wake_reason(self, now: int) -> str | None:
        s, a = self.state, self.cfg.agent
        if now < self.tired_until:
            return None
        if s.last_think_at == 0:
            return "you were just born; this is your first look at the market"
        if now - s.last_think_at < a.min_gap_minutes * MINUTE:
            return None
        if s.pending_wake:
            return "; ".join(s.pending_wake)
        if now >= s.next_think_at:
            return "your scheduled check"
        for sym, before in s.think_prices.items():
            p = s.last_prices.get(sym)
            if p and before and abs(p / before - 1) * 100 >= Decimal(str(a.wake_on_move_pct)):
                return f"{sym} moved {(p / before - 1) * 100:+.1f}% since your last thought"
        return None

    # ---- a thought --------------------------------------------------------------

    def think(self, now: int, reason: str) -> None:
        s, a = self.state, self.cfg.agent
        today = local_time(now, a.utc_offset_hours)[:10]
        if s.think_day != today:
            s.think_day, s.think_spent_today = today, Decimal("0")
        if s.think_spent_today >= a.daily_thinking_budget_php:
            self.tired_until = next_local_midnight(now, a.utc_offset_hours)
            log.warning("%s too tired to think: today's thinking budget (%s) is spent; sleeping until %s",
                        ts_str(now), peso(a.daily_thinking_budget_php), ts_str(self.tired_until))
            return

        symbols = self.symbols()
        snaps: list[CoinSnapshot] = []
        for sym in symbols:
            try:
                snap = snapshot(sym, self.klines(sym, "1h", 200))
                if snap:
                    snaps.append(snap)
            except Exception as e:  # a missing chart must not stop the thought
                log.warning("no market data for %s: %s", sym, e)
        try:
            heads = self.headlines(now)
        except Exception as e:
            log.warning("news unavailable: %s", e)
            heads = []
        regimes = self.regimes(now)
        text = briefing(cfg=self.cfg, state=s, now=now, reason=reason, equity=self.body.equity(),
                        invested=self.body.invested(), snaps=snaps, headlines=heads, prices=dict(s.last_prices),
                        regimes=regimes)
        try:
            thought = self.brain.think(text, symbols)
        except BrainUnavailable as e:
            self.tired_until = now + (60 if e.fatal else 10) * MINUTE
            log.error("%s cannot think (%s); reflexes still active, retrying after %s",
                      ts_str(now), e, ts_str(self.tired_until))
            return

        cost = (thought.cost_usd * a.usd_php).quantize(Decimal("0.01"))
        if a.charge_thinking_to_life:
            self.body.charge(cost, now, "thinking")
        else:
            s.thinking_paid += cost
        s.think_spent_today += cost

        decision = thought.decision or {}
        verdict = review(decision, self.body, self.cfg, now, regimes) if thought.decision else None
        did = self._execute(verdict.approved, now) if verdict else []
        try:
            minutes = int(decision.get("next_check_minutes") or 0)
        except (TypeError, ValueError):
            minutes = 0
        minutes = min(max(minutes or 240, a.min_check_minutes), a.max_check_minutes)

        s.journal.append({
            "t": now, "why": reason[:300], "view": str(decision.get("market_view", thought.note))[:600],
            "did": "; ".join(did), "blocked": "; ".join(verdict.notes) if verdict else "",
            "lesson": str(decision.get("lesson", ""))[:400], "cost": str(cost), "note": thought.note,
            "searches": thought.searches, "equity": str(self.body.equity().quantize(Decimal("0.01"))),
        })
        s.journal = s.journal[-200:]
        s.last_think_at = now
        s.next_think_at = now + minutes * MINUTE
        s.think_prices = {sym: s.last_prices[sym] for sym in symbols if sym in s.last_prices}
        s.pending_wake = []
        log.info("%s THOUGHT (%s, %s, %d searches) %s | did: %s%s | next in %d min",
                 ts_str(now), thought.note, peso(cost), thought.searches, decision.get("market_view", ""),
                 "; ".join(did) or "nothing", f" | guardrails: {'; '.join(verdict.notes)}" if verdict and verdict.notes else "",
                 minutes)
        self.body.check_vitals(now)  # thinking costs can kill, too
        self.body.save()

    def _execute(self, orders: list[Order], now: int) -> list[str]:
        s, a = self.state, self.cfg.agent
        done = []
        for o in orders:
            if o.action == "SELL":
                fill = self.body.sell(o.symbol, now, f"decided: {o.reason}", o.fraction)
                done.append(f"sold {o.symbol} for {peso(fill.quote_qty)}" if fill else f"SELL {o.symbol} failed")
            elif o.action == "ADJUST_STOP":
                pos = s.positions.get(o.symbol)
                price = self.body.price(o.symbol)
                if pos and price:
                    pos.stop_price = price * (1 - o.stop_pct / 100)
                    done.append(f"raised {o.symbol} stop to {peso(pos.stop_price)}")
            else:
                fill = self.body.buy(o.symbol, o.pesos, now, stop_pct=o.stop_pct,
                                     take_profit_pct=o.take_profit_pct or None, reason=f"decided: {o.reason}",
                                     thesis=o.reason)
                if fill:
                    today = local_time(now, a.utc_offset_hours)[:10]
                    if s.buys_day != today:
                        s.buys_day, s.buys_today = today, 0
                    s.buys_today += 1
                    done.append(f"bought {o.symbol} for {peso(fill.quote_qty)}")
                else:
                    done.append(f"BUY {o.symbol} failed")
        return done
