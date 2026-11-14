"""Configuration: dataclasses with safe defaults, loaded from a TOML file.

Secrets (API key and secret) are never read from this file; they come from
the COINS_API_KEY and COINS_API_SECRET environment variables.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field, fields
from decimal import Decimal
from pathlib import Path

VALID_INTERVALS = {"1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d", "3d", "1w"}


class ConfigError(ValueError):
    pass


@dataclass
class LifeConfig:
    budget_php: Decimal = Decimal("5000")
    # Equity at or below this kills the bot. 0 means "dies when the money is gone";
    # in practice it then dies when it can no longer afford the exchange's smallest order.
    death_floor_php: Decimal = Decimal("0")
    # Health bands, as a fraction of the starting budget.
    wounded_below: float = 0.75
    critical_below: float = 0.40
    # How much of its normal risk a hurt bot is willing to take.
    wounded_size: float = 0.5
    critical_size: float = 0.25


@dataclass
class StrategyConfig:
    fast_ema: int = 12
    slow_ema: int = 26
    trend_ema: int = 100
    atr_period: int = 14


@dataclass
class RiskConfig:
    risk_per_trade: float = 0.02  # fraction of equity lost if the stop-loss is hit
    atr_stop_mult: float = 2.5  # stop distance = ATR x this
    trailing_stop: bool = True
    max_position_fraction: float = 0.95  # never put more than this share of cash into one trade
    max_consecutive_losses: int = 3  # after this many losses in a row...
    rest_candles: int = 12  # ...sit out this many candles. 0 disables resting.


@dataclass
class ExecutionConfig:
    # Fee and slippage assumptions for paper trading, backtests and equity marking.
    # Set fee_rate to your actual Coins.ph fee tier.
    fee_rate: Decimal = Decimal("0.003")
    slippage_bps: Decimal = Decimal("10")
    poll_seconds: int = 30
    # Used for backtests, and as a fallback if the exchange does not publish a minimum.
    paper_min_notional_php: Decimal = Decimal("50")  # Coins.ph minimum, Nov 2026
    paper_min_qty: Decimal = Decimal("0.00000001")
    paper_step_size: Decimal = Decimal("0.00000001")
    base_url: str = "https://api.pro.coins.ph"


DEFAULT_FEEDS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    "https://decrypt.co/feed",
    "https://bitpinas.com/feed/",
]


@dataclass
class AgentConfig:
    """The Claude brain: which model thinks, what it may trade, and the hard limits it trades inside."""

    model: str = "claude-opus-5-5"
    effort: str = "medium"  # low | medium | high | xhigh | max (more effort = better reasoning, more cost)
    universe: list = field(default_factory=lambda: ["BTCPHP", "ETHPHP", "SOLPHP", "XRPPHP"])
    # Guardrails. Claude decides inside these; code enforces them and Claude cannot change them.
    max_positions: int = 3
    max_position_pct: float = 0.5  # one coin may be at most this share of equity
    max_buys_per_day: int = 4
    min_stop_pct: float = 2.0  # every buy has a stop-loss between these distances
    max_stop_pct: float = 15.0
    cash_reserve_pct: float = 0.03
    # When it thinks. Claude picks its next check inside these bounds.
    min_check_minutes: int = 60
    max_check_minutes: int = 1440
    min_gap_minutes: int = 20  # even urgent wake-ups wait this long after the last thought
    wake_on_move_pct: float = 5.0  # wake early if any coin moves this much since the last thought
    # Metabolism: thinking costs real money, and it comes out of the bot's life.
    daily_thinking_budget_php: Decimal = Decimal("25")
    usd_php: Decimal = Decimal("58")  # ASSUMPTION: set to the current exchange rate
    charge_thinking_to_life: bool = True
    upkeep_php_per_day: Decimal = Decimal("0")  # e.g. your server's cost per day
    # Senses.
    web_search_max_uses: int = 2  # Claude's own web searches per thought; 0 disables
    news_feeds: list = field(default_factory=lambda: list(DEFAULT_FEEDS))
    news_max_items: int = 25
    news_max_age_hours: int = 48
    journal_in_context: int = 8
    utc_offset_hours: int = 8  # Manila; used for "today" and for display


@dataclass
class BotConfig:
    name: str = "mortal-001"
    brain: str = "claude"  # "claude" (autonomous, news-aware) or "rules" (EMA crossover, no API needed)
    symbol: str = "BTCPHP"  # the market the rules brain trades
    base_asset: str = "BTC"
    quote_asset: str = "PHP"
    interval: str = "1h"
    history: int = 300  # candles fed to the strategy each decision
    state_dir: str = "state"
    life: LifeConfig = field(default_factory=LifeConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)


def warmup_candles(s: StrategyConfig) -> int:
    return max(s.slow_ema, s.trend_ema, s.atr_period) + 2


def _coerce(cls, data: dict, section: str):
    defaults = cls()
    names = {f.name for f in fields(cls)}
    kwargs = {}
    for key, value in data.items():
        if key not in names:
            raise ConfigError(f"unknown setting [{section}] {key}")
        default = getattr(defaults, key)
        try:
            if isinstance(default, Decimal):
                value = Decimal(str(value))
            elif isinstance(default, bool):
                if not isinstance(value, bool):
                    raise TypeError("expected true or false")
            elif isinstance(default, int):
                if isinstance(value, bool) or int(value) != value:
                    raise TypeError("expected a whole number")
                value = int(value)
            elif isinstance(default, float):
                value = float(value)
            elif isinstance(default, str):
                value = str(value)
            elif isinstance(default, list):
                if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                    raise TypeError("expected a list of strings")
        except (TypeError, ValueError, ArithmeticError) as e:
            raise ConfigError(f"[{section}] {key}: {e}") from None
        kwargs[key] = value
    return cls(**kwargs)


def load_config(path: str | Path | None) -> BotConfig:
    if path is None:
        cfg = BotConfig()
    else:
        with open(path, "rb") as fh:
            raw = tomllib.load(fh)
        nested = {"life": LifeConfig, "strategy": StrategyConfig, "risk": RiskConfig, "execution": ExecutionConfig,
                  "agent": AgentConfig}
        top = {k: v for k, v in raw.items() if k not in nested}
        if any(isinstance(v, dict) for v in top.values()):
            bad = [k for k, v in top.items() if isinstance(v, dict)]
            raise ConfigError(f"unknown section(s): {', '.join(bad)}")
        parts = {k: _coerce(cls, raw.get(k, {}), k) for k, cls in nested.items()}
        base = _coerce(BotConfig, top, "top level")
        cfg = BotConfig(**{f.name: getattr(base, f.name) for f in fields(BotConfig) if f.name not in nested}, **parts)
    validate(cfg)
    return cfg


def validate(cfg: BotConfig) -> None:
    errs: list[str] = []
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", cfg.name):
        errs.append("name: use 1-40 letters, digits, '-' or '_' (it becomes a file name)")
    if cfg.interval not in VALID_INTERVALS:
        errs.append(f"interval: must be one of {sorted(VALID_INTERVALS)}")
    if cfg.symbol != cfg.base_asset + cfg.quote_asset:
        errs.append("symbol must equal base_asset + quote_asset, e.g. BTC + PHP = BTCPHP")
    life = cfg.life
    if life.budget_php <= 0:
        errs.append("life.budget_php must be positive")
    if not (0 <= life.death_floor_php < life.budget_php):
        errs.append("life.death_floor_php must be >= 0 and below the budget")
    if not (0 < life.critical_below < life.wounded_below <= 1):
        errs.append("life: need 0 < critical_below < wounded_below <= 1")
    if not (0 < life.critical_size <= life.wounded_size <= 1):
        errs.append("life: need 0 < critical_size <= wounded_size <= 1")
    s = cfg.strategy
    if not (1 < s.fast_ema < s.slow_ema):
        errs.append("strategy: need 1 < fast_ema < slow_ema")
    if s.trend_ema < 2 or s.atr_period < 2:
        errs.append("strategy: trend_ema and atr_period must be >= 2")
    if cfg.history < warmup_candles(s):
        errs.append(f"history must be at least {warmup_candles(s)} for these indicator periods")
    r = cfg.risk
    if not (0 < r.risk_per_trade <= 0.1):
        errs.append("risk.risk_per_trade must be in (0, 0.1]")
    if r.atr_stop_mult <= 0:
        errs.append("risk.atr_stop_mult must be positive")
    if not (0 < r.max_position_fraction <= 1):
        errs.append("risk.max_position_fraction must be in (0, 1]")
    if r.max_consecutive_losses < 0 or r.rest_candles < 0:
        errs.append("risk: max_consecutive_losses and rest_candles must be >= 0")
    e = cfg.execution
    if not (0 <= e.fee_rate < Decimal("0.05")):
        errs.append("execution.fee_rate must be in [0, 0.05)")
    if not (0 <= e.slippage_bps < 500):
        errs.append("execution.slippage_bps must be in [0, 500)")
    if e.poll_seconds < 5:
        errs.append("execution.poll_seconds must be >= 5 (respect the exchange's rate limits)")
    if not e.base_url.startswith("https://"):
        errs.append("execution.base_url must use https")
    if cfg.brain not in ("claude", "rules"):
        errs.append('brain must be "claude" or "rules"')
    a = cfg.agent
    if not a.universe or any(not re.fullmatch(r"[A-Z0-9]{2,15}", s) or not s.endswith(cfg.quote_asset)
                             for s in a.universe):
        errs.append(f"agent.universe: list symbols like \"BTC{cfg.quote_asset}\"")
    if a.effort not in ("low", "medium", "high", "xhigh", "max"):
        errs.append("agent.effort must be low, medium, high, xhigh or max")
    if not (1 <= a.max_positions <= 10):
        errs.append("agent.max_positions must be 1-10")
    if not (0 < a.max_position_pct <= 1):
        errs.append("agent.max_position_pct must be in (0, 1]")
    if a.max_buys_per_day < 0:
        errs.append("agent.max_buys_per_day must be >= 0")
    if not (0.5 <= a.min_stop_pct <= a.max_stop_pct <= 50):
        errs.append("agent: need 0.5 <= min_stop_pct <= max_stop_pct <= 50")
    if not (0 <= a.cash_reserve_pct < 0.5):
        errs.append("agent.cash_reserve_pct must be in [0, 0.5)")
    if not (15 <= a.min_check_minutes <= a.max_check_minutes <= 10080):
        errs.append("agent: need 15 <= min_check_minutes <= max_check_minutes <= 10080")
    if not (5 <= a.min_gap_minutes <= a.min_check_minutes):
        errs.append("agent.min_gap_minutes must be between 5 and min_check_minutes")
    if a.daily_thinking_budget_php < 0 or a.usd_php <= 0 or a.upkeep_php_per_day < 0:
        errs.append("agent: budgets and usd_php must be non-negative (usd_php > 0)")
    if not (0 <= a.web_search_max_uses <= 10):
        errs.append("agent.web_search_max_uses must be 0-10")
    if any(not f.startswith("https://") for f in a.news_feeds):
        errs.append("agent.news_feeds must be https URLs")
    if errs:
        raise ConfigError("invalid config:\n  - " + "\n  - ".join(errs))
