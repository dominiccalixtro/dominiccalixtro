"""Guardrails: Claude proposes, code disposes.

Claude's decision is a request, not a command. Every order is checked here
against limits Claude cannot change: the tradable list, a mandatory stop-loss,
the risk per trade, position and daily limits, and the cash it actually has.
Orders are shrunk or blocked, and every change is written back to Claude's
journal so it can learn the rules.

This layer is also the defence against prompt injection: whatever a
headline or web page persuades the model to say, it cannot trade outside
these limits.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal, InvalidOperation

from .body import Body
from .config import BotConfig
from .life import Health, size_multiplier
from .models import peso
from .prompts import local_time

ORDER = {"SELL": 0, "ADJUST_STOP": 1, "BUY": 2}
MAX_ACTIONS = 6


@dataclass
class Order:
    action: str
    symbol: str
    pesos: Decimal = Decimal("0")
    fraction: Decimal = Decimal("1")
    stop_pct: Decimal = Decimal("0")
    take_profit_pct: Decimal = Decimal("0")
    reason: str = ""


@dataclass
class Review:
    approved: list[Order] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _num(x: object, default: str = "0") -> Decimal:
    try:
        d = Decimal(str(x))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal(default)
    return d if d.is_finite() else Decimal(default)


def _clamp(x: Decimal, lo: Decimal, hi: Decimal) -> Decimal:
    return min(max(x, lo), hi)


def review(decision: dict, body: Body, cfg: BotConfig, now: int) -> Review:
    a, s = cfg.agent, body.state
    out = Review()
    raw = decision.get("actions") if isinstance(decision, dict) else None
    raw = raw if isinstance(raw, list) else []
    if len(raw) > MAX_ACTIONS:
        out.notes.append(f"only the first {MAX_ACTIONS} actions were considered")
        raw = raw[:MAX_ACTIONS]
    orders = []
    for r in raw:
        if not isinstance(r, dict):
            continue
        orders.append(Order(
            action=str(r.get("action", "")).upper(), symbol=str(r.get("symbol", "")).upper(),
            pesos=_num(r.get("pesos")), fraction=_num(r.get("sell_fraction"), "1"),
            stop_pct=_num(r.get("stop_loss_pct")), take_profit_pct=_num(r.get("take_profit_pct")),
            reason=str(r.get("reason", ""))[:300],
        ))
    orders.sort(key=lambda o: ORDER.get(o.action, 9))  # sells free cash before buys spend it

    equity = body.equity()
    cash = s.cash
    held = set(s.positions)
    today = local_time(now, a.utc_offset_hours)[:10]
    buys_today = s.buys_today if s.buys_day == today else 0
    mult = Decimal(str(size_multiplier(Health(s.health), cfg.life)))
    min_stop, max_stop = Decimal(str(a.min_stop_pct)), Decimal(str(a.max_stop_pct))
    fee = body.fee_rate

    for o in orders:
        tag = f"{o.action} {o.symbol}"
        if o.action not in ORDER:
            out.notes.append(f"ignored unknown action {o.action!r}")
            continue
        if o.symbol not in body.rules:
            out.notes.append(f"{tag}: not a tradable market; blocked")
            continue
        price = body.price(o.symbol)
        if not price:
            out.notes.append(f"{tag}: no current price; blocked")
            continue

        if o.action == "SELL":
            if o.symbol not in held:
                out.notes.append(f"{tag}: you hold none; ignored")
                continue
            if o.fraction <= 0 or o.fraction > 1:
                out.notes.append(f"{tag}: sell_fraction {o.fraction} is outside 0-1; selling all")
                o.fraction = Decimal("1")
            pos = s.positions[o.symbol]
            cash += body.position_value(pos) * o.fraction
            if o.fraction == 1:
                held.discard(o.symbol)
            out.approved.append(o)

        elif o.action == "ADJUST_STOP":
            pos = s.positions.get(o.symbol)
            if pos is None or o.symbol not in held:
                out.notes.append(f"{tag}: you hold none; ignored")
                continue
            pct = _clamp(o.stop_pct, min_stop, max_stop)
            new_stop = price * (1 - pct / 100)
            if pos.stop_price is not None and new_stop <= pos.stop_price:
                out.notes.append(f"{tag}: a {pct}% stop ({peso(new_stop)}) would loosen the current stop "
                                 f"({peso(pos.stop_price)}); stops only move up")
                continue
            o.stop_pct = pct
            out.approved.append(o)

        else:  # BUY
            if s.rest_until > now:
                out.notes.append(f"{tag}: resting after a losing streak until "
                                 f"{local_time(s.rest_until, a.utc_offset_hours)}; blocked")
                continue
            if buys_today >= a.max_buys_per_day:
                out.notes.append(f"{tag}: already {buys_today} buys today (limit {a.max_buys_per_day}); blocked")
                continue
            if o.symbol not in held and len(held) >= a.max_positions:
                out.notes.append(f"{tag}: already holding {len(held)} coins (limit {a.max_positions}); blocked")
                continue
            if o.pesos <= 0:
                out.notes.append(f"{tag}: no peso amount given; blocked")
                continue
            if o.stop_pct <= 0:
                o.stop_pct = (min_stop + max_stop) / 2
                out.notes.append(f"{tag}: no stop-loss given; set to {o.stop_pct}%")
            elif not (min_stop <= o.stop_pct <= max_stop):
                clamped = _clamp(o.stop_pct, min_stop, max_stop)
                out.notes.append(f"{tag}: stop {o.stop_pct}% moved to {clamped}% (allowed {min_stop}-{max_stop}%)")
                o.stop_pct = clamped
            if o.take_profit_pct < 0:
                o.take_profit_pct = Decimal("0")
            elif o.take_profit_pct > 0:
                o.take_profit_pct = _clamp(o.take_profit_pct, Decimal("1"), Decimal("200"))

            existing = body.position_value(s.positions[o.symbol]) if o.symbol in s.positions else Decimal("0")
            limits = {
                "risk per trade": equity * Decimal(str(cfg.risk.risk_per_trade)) * mult / (o.stop_pct / 100),
                "max share of equity per coin": equity * Decimal(str(a.max_position_pct)) - existing,
                "cash on hand": (cash - equity * Decimal(str(a.cash_reserve_pct))) / (1 + fee),
            }
            name, cap = min(limits.items(), key=lambda kv: kv[1])
            amount = min(o.pesos, cap)
            amount = (max(amount, Decimal("0")) / body.rules[o.symbol].quote_step).to_integral_value(
                rounding=ROUND_DOWN) * body.rules[o.symbol].quote_step
            min_order = body.rules[o.symbol].min_notional * Decimal("1.01")
            if amount < min_order:
                out.notes.append(f"{tag}: {peso(amount)} after limits ({name}) is below the minimum order "
                                 f"{peso(min_order)}; blocked")
                continue
            if amount < o.pesos:
                out.notes.append(f"{tag}: reduced from {peso(o.pesos)} to {peso(amount)} (limit: {name})")
            o.pesos = amount
            cash -= amount * (1 + fee)
            held.add(o.symbol)
            buys_today += 1
            out.approved.append(o)
    return out
