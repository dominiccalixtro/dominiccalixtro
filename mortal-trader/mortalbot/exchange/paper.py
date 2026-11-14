"""Simulated execution: fills at the last marked price, with fees and slippage.

Used for backtests and paper trading. It is optimistic in one way you should
know about: it always fills the whole order at one price. Real market orders
on a thin order book can fill worse, especially in a crash.
"""

from __future__ import annotations

import itertools
from decimal import Decimal

from ..models import Fill, OrderRejected, SymbolRules


class PaperBroker:
    def __init__(self, fee_rate: Decimal, slippage_bps: Decimal):
        self.fee_rate = fee_rate
        self.slip = slippage_bps / Decimal("10000")
        self.price: Decimal | None = None
        self.ts = 0
        self._ids = itertools.count(1)

    def mark(self, price: Decimal, ts: int) -> None:
        """Tell the simulator the current market price."""
        self.price = price
        self.ts = ts

    def _px(self) -> Decimal:
        if self.price is None or self.price <= 0:
            raise OrderRejected("paper broker has no market price yet")
        return self.price

    def market_buy(self, rules: SymbolRules, quote_amount: Decimal) -> Fill:
        px = self._px() * (1 + self.slip)
        qty = rules.round_qty(quote_amount / (px * (1 + self.fee_rate)))
        gross = qty * px
        if qty < rules.min_qty or gross < rules.min_notional:
            raise OrderRejected(f"order of {gross:.2f} {rules.quote_asset} is below the exchange minimum")
        fee = gross * self.fee_rate
        return Fill("BUY", qty, gross + fee, px, fee, f"paper-{next(self._ids)}", self.ts)

    def market_sell(self, rules: SymbolRules, base_qty: Decimal) -> Fill:
        px = self._px() * (1 - self.slip)
        qty = rules.round_qty(base_qty)
        gross = qty * px
        if qty < rules.min_qty or gross < rules.min_notional:
            raise OrderRejected(f"order of {gross:.2f} {rules.quote_asset} is below the exchange minimum")
        fee = gross * self.fee_rate
        return Fill("SELL", qty, gross - fee, px, fee, f"paper-{next(self._ids)}", self.ts)


def paper_rules(symbol: str, base: str, quote: str, *, min_qty: Decimal, step: Decimal, min_notional: Decimal) -> SymbolRules:
    return SymbolRules(symbol=symbol, base_asset=base, quote_asset=quote, min_qty=min_qty,
                       step_size=step, min_notional=min_notional)
