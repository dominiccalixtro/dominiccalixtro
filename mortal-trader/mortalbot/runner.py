"""Long-running loop for paper and live trading against Coins.ph.

Network trouble never kills the bot; only money does. On errors it backs
off and retries. Ctrl+C saves state and exits; the next start resumes the
same life.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import replace
from decimal import Decimal

from .bot import MortalBot
from .config import BotConfig
from .exchange.coinsph import CoinsPhClient
from .models import ExchangeError, OrderRejected, SymbolRules, peso
from .state import BotState, StateMismatch, StateStore, awaken

log = logging.getLogger("mortalbot")


class LedgerMismatch(Exception):
    pass


def load_rules(client: CoinsPhClient, cfg: BotConfig) -> SymbolRules:
    rules = client.symbol_rules(cfg.symbol)
    if rules.base_asset != cfg.base_asset or rules.quote_asset != cfg.quote_asset:
        raise StateMismatch(f"{cfg.symbol} is {rules.base_asset}/{rules.quote_asset} on Coins.ph, "
                            f"config says {cfg.base_asset}/{cfg.quote_asset}")
    if rules.min_notional <= 0:
        log.warning("Coins.ph published no minimum order for %s; assuming %s",
                    cfg.symbol, peso(cfg.execution.paper_min_notional_php))
        rules = replace(rules, min_notional=cfg.execution.paper_min_notional_php)
    return rules


def reconcile(client: CoinsPhClient, state: BotState, rules: SymbolRules) -> None:
    """Refuse to trade if the real account holds less than the bot's ledger says it owns."""
    bal = client.balances()
    quote_free = bal.get(rules.quote_asset, (Decimal("0"), Decimal("0")))[0]
    base_free = bal.get(rules.base_asset, (Decimal("0"), Decimal("0")))[0]
    tol = Decimal("0.995")
    if quote_free < state.cash * tol:
        raise LedgerMismatch(f"account has {peso(quote_free)} free but the bot's ledger says it owns "
                             f"{peso(state.cash)}. Fund the account or check for withdrawals.")
    if base_free < state.position_qty * tol:
        raise LedgerMismatch(f"account has {base_free} {rules.base_asset} free, ledger says "
                             f"{state.position_qty}. Someone moved coins; refusing to trade.")
    extra = quote_free - state.cash
    if extra > 0:
        log.info("account holds %s more %s than the bot's life; the bot will not touch it",
                 peso(extra), rules.quote_asset)


def run_loop(
    cfg: BotConfig,
    mode: str,
    client: CoinsPhClient,
    broker,
    store: StateStore,
    *,
    max_loops: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now_ms: Callable[[], int] = lambda: int(time.time() * 1000),
) -> MortalBot:
    rules = load_rules(client, cfg)
    if client.has_keys:
        offset = client.sync_time()
        if abs(offset) > 1000:
            log.warning("local clock is %d ms off Coins.ph; compensating", offset)
    state, born = awaken(store, name=cfg.name, mode=mode, symbol=cfg.symbol,
                         budget=cfg.life.budget_php, now_ms=now_ms())
    if state.budget != cfg.life.budget_php:
        log.warning("config budget is %s but %s was born with %s; a life cannot be topped up",
                    peso(cfg.life.budget_php), state.name, peso(state.budget))
    if mode == "live":
        reconcile(client, state, rules)
    log.info("%s %s: %s, %s %s, equity %s, health %s", state.name, "is born" if born else "wakes up",
             mode.upper(), cfg.symbol, cfg.interval, peso(state.last_equity), state.health)
    bot = MortalBot(cfg, state, broker, rules, store)
    poll = cfg.execution.poll_seconds
    errors = loops = 0
    while bot.alive:
        try:
            now = now_ms()
            price = client.ticker_price(cfg.symbol)
            if hasattr(broker, "mark"):
                broker.mark(price, now)
            bot.on_price(price, now)
            if bot.alive:
                candles = client.klines(cfg.symbol, cfg.interval, limit=cfg.history + 1)
                closed = [c for c in candles if c.close_time < now]
                if closed:
                    bot.on_candle_close(closed[-cfg.history:])
            store.save(bot.state)
            errors = 0
        except (OSError, ExchangeError, OrderRejected, ValueError, KeyError) as e:
            errors += 1
            log.warning("hiccup #%d (%s): %s", errors, type(e).__name__, e)
        loops += 1
        if max_loops is not None and loops >= max_loops:
            break
        if bot.alive:
            sleep(min(poll * 2 ** errors, 600) if errors else poll)
    if not bot.alive and bot.obituary:
        print(bot.obituary)
    return bot
