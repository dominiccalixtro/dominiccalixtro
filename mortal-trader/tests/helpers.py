from decimal import Decimal

from mortalbot.config import BotConfig
from mortalbot.exchange.paper import PaperBroker, paper_rules
from mortalbot.models import Candle

D = Decimal
HOUR = 3_600_000


def rules(min_notional="100"):
    return paper_rules("BTCPHP", "BTC", "PHP", min_qty=D("0.00000001"), step=D("0.00000001"),
                       min_notional=D(min_notional))


def candles_from_closes(closes, start=1_767_225_600_000, spread=0.002):
    out = []
    prev = closes[0]
    for i, c in enumerate(closes):
        o = prev
        hi = max(o, c) * (1 + spread)
        lo = min(o, c) * (1 - spread)
        t = start + i * HOUR
        out.append(Candle(t, o, hi, lo, c, 1.0, t + HOUR - 1))
        prev = c
    return out


def small_cfg(**life):
    """Short indicator periods so tests need few candles."""
    cfg = BotConfig()
    cfg.strategy.fast_ema, cfg.strategy.slow_ema, cfg.strategy.trend_ema, cfg.strategy.atr_period = 3, 6, 10, 5
    cfg.history = 50
    cfg.execution.fee_rate = D("0.001")
    cfg.execution.slippage_bps = D("0")
    for k, v in life.items():
        setattr(cfg.life, k, D(str(v)) if isinstance(getattr(cfg.life, k), Decimal) else v)
    return cfg


def broker(cfg):
    return PaperBroker(cfg.execution.fee_rate, cfg.execution.slippage_bps)
