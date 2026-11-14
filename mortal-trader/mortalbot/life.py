"""The bot's life: health bands, death, and the obituary.

Equity is marked at liquidation value (what it would get if it sold
everything right now, after fees), so the bot never thinks it is healthier
than it is.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from .config import LifeConfig
from .models import peso, ts_str


class Health(str, Enum):
    THRIVING = "THRIVING"  # above its starting budget
    HEALTHY = "HEALTHY"
    WOUNDED = "WOUNDED"  # trades smaller
    CRITICAL = "CRITICAL"  # trades much smaller
    DEAD = "DEAD"


@dataclass(frozen=True)
class Vitals:
    equity: Decimal
    life_left: float  # equity / starting budget
    health: Health
    cause_of_death: str | None = None


_RANK = {Health.THRIVING: 0, Health.HEALTHY: 1, Health.WOUNDED: 2, Health.CRITICAL: 3, Health.DEAD: 4}
# Wounds heal slowly: to move up a band, equity must clear the line by this much.
RECOVERY_MARGIN = 0.02


def _band(left: float, life: LifeConfig) -> Health:
    if left >= 1.0:
        return Health.THRIVING
    if left >= life.wounded_below:
        return Health.HEALTHY
    if left >= life.critical_below:
        return Health.WOUNDED
    return Health.CRITICAL


def assess(equity: Decimal, life: LifeConfig, min_notional: Decimal, previous: Health | None = None) -> Vitals:
    left = float(equity / life.budget_php)
    if equity <= life.death_floor_php:
        return Vitals(
            equity,
            left,
            Health.DEAD,
            f"bled out: equity {peso(equity)} reached the death floor of {peso(life.death_floor_php)}",
        )
    if equity < min_notional:
        return Vitals(
            equity,
            left,
            Health.DEAD,
            f"starved: equity {peso(equity)} is below the exchange's smallest order "
            f"({peso(min_notional)}), so it can never trade again",
        )
    health = _band(left, life)
    if previous is not None and previous is not Health.DEAD and _RANK[health] < _RANK[previous]:
        # Getting better: only upgrade as far as the margin allows (never worse than before).
        healed = _band(left - RECOVERY_MARGIN, life)
        health = healed if _RANK[healed] < _RANK[previous] else previous
    return Vitals(equity, left, health)


def size_multiplier(health: Health, life: LifeConfig) -> float:
    """A hurt bot takes smaller bites. Losing money makes it more careful, not more desperate."""
    return {
        Health.THRIVING: 1.0,
        Health.HEALTHY: 1.0,
        Health.WOUNDED: life.wounded_size,
        Health.CRITICAL: life.critical_size,
        Health.DEAD: 0.0,
    }[health]


def _lifespan(ms: int) -> str:
    hours = max(ms, 0) // 3_600_000
    days, hours = divmod(hours, 24)
    if days:
        return f"{days} day{'s' * (days != 1)}, {hours} hour{'s' * (hours != 1)}"
    return f"{hours} hour{'s' * (hours != 1)}"


def obituary(state) -> str:
    """A short, plain-text life summary written when the bot dies."""
    trades = state.trades
    pnls = [Decimal(t["pnl"]) for t in trades]
    wins = sum(1 for p in pnls if p > 0)
    lines = [
        "+--------------------------------------------------+",
        f"  R.I.P.  {state.name}   ({state.mode}, {state.symbol})",
        "+--------------------------------------------------+",
        f"  Born:    {ts_str(state.born_at)}",
        f"  Died:    {ts_str(state.died_at) if state.died_at else '-'}",
        f"  Lived:   {_lifespan((state.died_at or state.born_at) - state.born_at)}",
        f"  Cause:   {state.cause_of_death}",
        f"  Life:    started {peso(state.budget)}, peaked {peso(state.peak_equity)}, "
        f"left {peso(state.cash)}",
        f"  Trades:  {len(trades)} ({wins} won, {len(trades) - wins} lost), fees paid {peso(state.fees_paid)}",
    ]
    if pnls:
        lines.append(f"  Best:    {peso(max(pnls))}   Worst: {peso(min(pnls))}")
    if state.dust_qty > 0:
        lines.append(f"  Dust:    {state.dust_qty} {state.symbol} too small to sell")
    lines.append("+--------------------------------------------------+")
    return "\n".join(lines)
