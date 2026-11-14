"""Plain data types shared by every part of the bot."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_DOWN, Decimal


class ExchangeError(Exception):
    """The exchange answered with an error (bad request, auth failure, rate limit...)."""

    def __init__(self, status: int, code: object = None, message: str = ""):
        self.status = status
        self.code = code
        self.message = message
        super().__init__(f"HTTP {status} code={code} {message}".strip())


class OrderRejected(Exception):
    """An order was refused before or by the exchange; no position change happened."""


@dataclass(frozen=True)
class Candle:
    open_time: int  # ms since epoch, UTC
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_time: int  # ms since epoch, UTC


@dataclass(frozen=True)
class SymbolRules:
    """Trading limits for one market, as the exchange publishes them."""

    symbol: str
    base_asset: str
    quote_asset: str
    min_qty: Decimal
    step_size: Decimal
    min_notional: Decimal
    tick_size: Decimal = Decimal("0.01")
    quote_step: Decimal = Decimal("0.01")

    def round_qty(self, qty: Decimal) -> Decimal:
        return floor_to_step(qty, self.step_size)

    def round_quote(self, amount: Decimal) -> Decimal:
        return floor_to_step(amount, self.quote_step)


@dataclass(frozen=True)
class Fill:
    """What actually happened when an order executed.

    base_qty  -- coins received (BUY) or given up (SELL), after any fee in coins
    quote_qty -- pesos spent (BUY) or received (SELL), after any fee in pesos
    fee_quote -- total fee expressed in pesos (for reporting)
    """

    side: str
    base_qty: Decimal
    quote_qty: Decimal
    avg_price: Decimal
    fee_quote: Decimal
    order_id: str
    ts: int


def floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def to_dec(x: object) -> Decimal:
    """Convert floats via str so 0.1 stays 0.1 and not 0.1000000000000000055..."""
    if isinstance(x, Decimal):
        return x
    return Decimal(str(x))


def fmt_decimal(d: Decimal) -> str:
    """Render a Decimal for an API parameter: no exponent, no trailing zeros."""
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def peso(d: Decimal | float) -> str:
    v = to_dec(d)
    return f"{'-' if v < 0 else ''}₱{abs(v):,.2f}"


def ts_str(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
