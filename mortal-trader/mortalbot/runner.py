"""Long-running loops for paper and live trading against Coins.ph.

Network trouble never kills the bot; only money does. On errors it backs
off and retries. Ctrl+C (or a service stop) saves state and exits; the next
start resumes the same life.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import replace
from decimal import Decimal

from .agent import ClaudeTrader
from .body import Body
from .bot import MortalBot
from .config import BotConfig
from .exchange.coinsph import CoinsPhClient
from .models import ExchangeError, OrderRejected, SymbolRules, peso
from .news import fetch_headlines
from .state import BotState, StateMismatch, StateStore, awaken

log = logging.getLogger("mortalbot")
Clock = Callable[[], int]


class LedgerMismatch(Exception):
    pass


def _now() -> int:
    return int(time.time() * 1000)


def load_rules(client: CoinsPhClient, cfg: BotConfig, symbols: list[str]) -> dict[str, SymbolRules]:
    out = {}
    for sym in symbols:
        try:
            rules = client.symbol_rules(sym)
        except ExchangeError as e:
            log.warning("skipping %s: not available on Coins.ph (%s)", sym, e)
            continue
        if rules.quote_asset != cfg.quote_asset:
            log.warning("skipping %s: priced in %s, not %s", sym, rules.quote_asset, cfg.quote_asset)
            continue
        if rules.min_notional <= 0:
            log.warning("Coins.ph published no minimum order for %s; assuming %s",
                        sym, peso(cfg.execution.paper_min_notional_php))
            rules = replace(rules, min_notional=cfg.execution.paper_min_notional_php)
        out[sym] = rules
    if not out:
        raise StateMismatch(f"none of {symbols} can be traded on Coins.ph")
    return out


def reconcile(client: CoinsPhClient, state: BotState, rules: dict[str, SymbolRules]) -> None:
    """Refuse to trade if the real account holds less than the bot's ledger says it owns."""
    bal = client.balances()
    zero = (Decimal("0"), Decimal("0"))
    quote = next(iter(rules.values())).quote_asset
    tol = Decimal("0.995")
    quote_free = bal.get(quote, zero)[0]
    if quote_free < state.cash * tol:
        raise LedgerMismatch(f"account has {peso(quote_free)} free but the bot's ledger says it owns "
                             f"{peso(state.cash)}. Fund the account or check for withdrawals.")
    for sym, pos in state.positions.items():
        base = rules[sym].base_asset if sym in rules else sym.removesuffix(quote)
        free = bal.get(base, zero)[0]
        if free < pos.qty * tol:
            raise LedgerMismatch(f"account has {free} {base} free, ledger says {pos.qty}. "
                                 "Someone moved coins; refusing to trade.")
    if quote_free > state.cash:
        log.info("account holds %s more %s than the bot's ledger; the bot will not touch it",
                 peso(quote_free - state.cash), quote)


def _prepare(cfg: BotConfig, mode: str, client: CoinsPhClient, store: StateStore, identity: str,
             symbols: list[str], now_ms: Clock) -> tuple[BotState, dict[str, SymbolRules]]:
    rules = load_rules(client, cfg, symbols)
    if client.has_keys:
        offset = client.sync_time()
        if abs(offset) > 1000:
            log.warning("local clock is %d ms off Coins.ph; compensating", offset)
    state, born = awaken(store, name=cfg.name, mode=mode, symbol=identity, budget=cfg.life.budget_php,
                         now_ms=now_ms())
    for sym in state.positions:  # it may hold a coin that has since left its universe
        if sym not in rules:
            rules.update(load_rules(client, cfg, [sym]))
    if state.budget != cfg.life.budget_php:
        log.warning("config budget is %s but %s was born with %s; a life cannot be topped up",
                    peso(cfg.life.budget_php), state.name, peso(state.budget))
    if mode == "live":
        reconcile(client, state, rules)
    log.info("%s %s: %s, %s brain, markets %s, equity %s, health %s", state.name,
             "is born" if born else "wakes up", mode.upper(), cfg.brain, ", ".join(sorted(rules)),
             peso(state.last_equity), state.health)
    return state, rules


def _loop(tick: Callable[[int], None], is_alive: Callable[[], bool], store: StateStore, state: BotState,
          poll: int, *, max_loops: int | None, sleep: Callable[[float], None], now_ms: Clock) -> None:
    errors = loops = 0
    while is_alive():
        try:
            tick(now_ms())
            store.save(state)
            errors = 0
        except (OSError, ExchangeError, OrderRejected, ValueError, KeyError) as e:
            errors += 1
            log.warning("hiccup #%d (%s): %s", errors, type(e).__name__, e)
        loops += 1
        if max_loops is not None and loops >= max_loops:
            break
        if is_alive():
            sleep(min(poll * 2 ** errors, 600) if errors else poll)


def run_rules(cfg: BotConfig, mode: str, client: CoinsPhClient, broker, store: StateStore, *,
              max_loops: int | None = None, sleep: Callable[[float], None] = time.sleep,
              now_ms: Clock = _now) -> MortalBot:
    state, rules = _prepare(cfg, mode, client, store, cfg.symbol, [cfg.symbol], now_ms)
    bot = MortalBot(cfg, state, broker, rules[cfg.symbol], store)

    def tick(now: int) -> None:
        price = client.ticker_price(cfg.symbol)
        if hasattr(broker, "mark"):
            broker.mark(price, now, cfg.symbol)
        bot.on_price(price, now)
        if bot.alive:
            candles = client.klines(cfg.symbol, cfg.interval, limit=cfg.history + 1)
            closed = [c for c in candles if c.close_time < now]
            if closed:
                bot.on_candle_close(closed[-cfg.history:])

    _loop(tick, lambda: bot.alive, store, state, cfg.execution.poll_seconds, max_loops=max_loops,
          sleep=sleep, now_ms=now_ms)
    if not bot.alive and bot.obituary:
        print(bot.obituary)
    return bot


def run_agent(cfg: BotConfig, mode: str, client: CoinsPhClient, broker, store: StateStore, brain, *,
              headlines: Callable[[int], list] | None = None, max_loops: int | None = None,
              sleep: Callable[[float], None] = time.sleep, now_ms: Clock = _now) -> ClaudeTrader:
    a = cfg.agent
    state, rules = _prepare(cfg, mode, client, store, "agent", list(a.universe), now_ms)
    body = Body(cfg, state, broker, rules, store)
    if headlines is None:
        def headlines(now: int) -> list:
            return fetch_headlines(a.news_feeds, now_ms=now, max_items=a.news_max_items,
                                   max_age_hours=a.news_max_age_hours)
    trader = ClaudeTrader(cfg, body, brain, klines=lambda sym: client.klines(sym, "1h", limit=200),
                          headlines=headlines)

    def tick(now: int) -> None:
        prices = client.ticker_prices(sorted(set(rules) | set(state.positions)))
        if hasattr(broker, "mark_all"):
            broker.mark_all(prices, now)
        trader.step(prices, now)

    _loop(tick, lambda: trader.alive, store, state, cfg.execution.poll_seconds, max_loops=max_loops,
          sleep=sleep, now_ms=now_ms)
    if not trader.alive and body.obituary:
        print(body.obituary)
    return trader
