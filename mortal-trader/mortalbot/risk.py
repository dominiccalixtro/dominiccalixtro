"""Position sizing: decide how many pesos to put into a trade.

The bot sizes by risk, not by conviction: it buys an amount such that hitting
the stop-loss costs about `risk_per_trade` of its equity (2% by default),
scaled down when it is wounded or critical.
"""

from __future__ import annotations

from decimal import Decimal

from .config import RiskConfig
from .models import peso


def entry_quote_amount(
    *,
    equity: Decimal,
    cash: Decimal,
    price: Decimal,
    atr: Decimal,
    multiplier: float,
    risk: RiskConfig,
    fee_rate: Decimal,
    min_notional: Decimal,
    quote_step: Decimal = Decimal("0.01"),
) -> tuple[Decimal, str]:
    """Return (pesos to spend, explanation). Zero pesos means: do not trade."""
    if atr <= 0 or price <= 0:
        return Decimal("0"), "no volatility reading yet"
    if multiplier <= 0:
        return Decimal("0"), "too hurt to trade"
    stop_distance = atr * Decimal(str(risk.atr_stop_mult))
    full_risk = equity * Decimal(str(risk.risk_per_trade))
    risk_budget = full_risk * Decimal(str(multiplier))
    notional = risk_budget / stop_distance * price
    cap = cash * Decimal(str(risk.max_position_fraction)) / (1 + fee_rate)
    reason = f"risking {peso(risk_budget)} with the stop {peso(stop_distance)} below entry"
    if notional > cap:
        notional = cap
        reason += f"; capped at {risk.max_position_fraction:.0%} of cash"
    if notional < min_notional:
        # One minimum-size order is fine if its risk still fits the un-shrunk budget.
        # 1% headroom so rounding the coin quantity down cannot drop the order under the minimum.
        min_order = min_notional * Decimal("1.01")
        min_trade_risk = min_order * stop_distance / price
        if cap >= min_order and min_trade_risk <= full_risk:
            notional = min_order
            reason = f"exchange minimum order {peso(min_notional)} (risk {peso(min_trade_risk)})"
        else:
            return Decimal("0"), (
                f"too weak to hunt: wanted {peso(notional)}, exchange minimum is {peso(min_notional)}"
            )
    amount = (notional / quote_step).to_integral_value(rounding="ROUND_DOWN") * quote_step
    return amount, reason
