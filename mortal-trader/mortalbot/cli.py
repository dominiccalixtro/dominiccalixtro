"""Command line: backtest, paper, live, status, fetch."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from decimal import Decimal
from pathlib import Path

from .backtest import BacktestResult, run_backtest
from .config import ConfigError, load_config
from .data import REGIMES, load_csv, save_csv, synthetic
from .exchange.coinsph import CoinsPhClient, ShadowBroker
from .exchange.paper import PaperBroker
from .life import obituary
from .models import ExchangeError, peso, ts_str
from .brain import ClaudeBrain
from .runner import LedgerMismatch, run_agent, run_rules
from .state import BotIsDead, StateMismatch, StateStore

EXIT_DEAD = 3


def _setup_logging(verbose: bool, default: int) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else default,
                        format="%(asctime)s %(levelname)-8s %(message)s", datefmt="%H:%M:%S")


def _client(cfg, *, need_keys: bool) -> CoinsPhClient:
    key, secret = os.environ.get("COINS_API_KEY"), os.environ.get("COINS_API_SECRET")
    if need_keys and not (key and secret):
        raise SystemExit("set COINS_API_KEY and COINS_API_SECRET in the environment (never in config.toml)")
    return CoinsPhClient(key, secret, base_url=cfg.execution.base_url, fee_rate_estimate=cfg.execution.fee_rate)


def _print_backtest(r: BacktestResult, source: str) -> None:
    s = r.state
    n = len(s.trades)
    print(f"\nBacktest  {s.symbol}  {r.candles_seen} candles  ({source})")
    if source.startswith("synthetic"):
        print("  NOTE: synthetic data tests the bot's mechanics, not whether it makes money.")
    print(f"  Life        {peso(s.budget)} -> {peso(r.final_equity)}  ({r.total_return:+.2%})")
    print(f"  Buy & hold  {r.buy_hold_return:+.2%} over the same candles")
    print(f"  Max drawdown {r.max_drawdown:.2%}   peak equity {peso(s.peak_equity)}")
    print(f"  Trades      {n}  won {r.wins}  ({(r.wins / n if n else 0):.0%} win rate)   fees {peso(s.fees_paid)}")
    print(f"  Status      {'ALIVE, ' + s.health if s.alive else 'DEAD'}")
    if not s.alive:
        print("\n" + (r.bot.obituary or obituary(s)))


def cmd_backtest(args, cfg) -> int:
    if args.csv:
        candles, source = load_csv(args.csv), f"csv: {args.csv}"
    else:
        regime = args.synthetic or "mixed"
        candles = synthetic(args.candles, regime=regime, seed=args.seed)
        source = f"synthetic: {regime}, seed {args.seed}"
    if args.budget:
        cfg.life.budget_php = Decimal(args.budget)
    if args.death_floor is not None:
        cfg.life.death_floor_php = Decimal(args.death_floor)
    result = run_backtest(candles, cfg)
    _print_backtest(result, source)
    return 0 if result.state.alive else EXIT_DEAD


def cmd_trade(args, cfg, mode: str) -> int:
    if mode == "live" and not args.confirm_real_money:
        print("Live mode spends real money. Re-run with --confirm-real-money once paper trading "
              "and --validate-orders have run cleanly.", file=sys.stderr)
        return 2
    validate = mode == "paper" and args.validate_orders
    client = _client(cfg, need_keys=(mode == "live" or validate))
    paper = PaperBroker(cfg.execution.fee_rate, cfg.execution.slippage_bps)
    broker = client if mode == "live" else (ShadowBroker(client, paper) if validate else paper)
    store = StateStore(cfg.state_dir, cfg.name, mode)
    try:
        if cfg.brain == "claude":
            if not os.environ.get("ANTHROPIC_API_KEY"):
                logging.getLogger("mortalbot").warning(
                    "ANTHROPIC_API_KEY is not set; the Claude brain will only work if another Anthropic "
                    "credential (e.g. `ant auth login`) is configured")
            bot = run_agent(cfg, mode, client, broker, store, ClaudeBrain(cfg))
        else:
            bot = run_rules(cfg, mode, client, broker, store)
    except BotIsDead as e:
        print(f"{e}\n")
        print(obituary(e.state))
        return EXIT_DEAD
    except (StateMismatch, LedgerMismatch) as e:
        print(f"refusing to trade: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nstopped; state saved. Run the same command to resume this life.")
        return 0
    return 0 if bot.alive else EXIT_DEAD


def cmd_status(args, cfg) -> int:
    store = StateStore(cfg.state_dir, cfg.name, args.mode)
    s = store.load()
    if s is None:
        print(f"{cfg.name} ({args.mode}) has not been born yet.")
        return 0
    if not s.alive:
        print(obituary(s))
        return EXIT_DEAD
    print(f"{s.name} ({s.mode}, {s.symbol}), born {ts_str(s.born_at)}")
    print(f"  health   {s.health}, equity {peso(s.last_equity)} of {peso(s.budget)} "
          f"({float(s.last_equity / s.budget):.1%}), peak {peso(s.peak_equity)}")
    print(f"  cash     {peso(s.cash)}")
    for sym, p in s.positions.items():
        stop = peso(p.stop_price) if p.stop_price else "-"
        print(f"  holding  {sym} {p.qty} at {peso(p.entry_price)}, stop {stop}"
              + (f", target {peso(p.take_profit)}" if p.take_profit else ""))
    print(f"  spent    fees {peso(s.fees_paid)}, thinking {peso(s.thinking_paid)}, upkeep {peso(s.upkeep_paid)}; "
          f"{len(s.trades)} trades")
    for j in s.journal[-args.journal:] if args.journal else []:
        print(f"\n  [{ts_str(j['t'])}, {peso(Decimal(j['cost']))}] woke: {j['why']}")
        print(f"    view: {j['view']}")
        print(f"    did:  {j['did'] or 'nothing'}" + (f"\n    guardrails: {j['blocked']}" if j['blocked'] else ""))
        if j.get("lesson"):
            print(f"    lesson: {j['lesson']}")
    return 0


def cmd_simulate(args, cfg) -> int:
    import json

    from .simulate import SCENARIOS, PlaybookBrain, export, history_market, simulate

    markets = []
    if args.history:
        daily = {s: load_csv(f"{args.history}/{s}-1d.csv") for s in cfg.agent.universe}
        markets.append(history_market(daily))
    names = list(SCENARIOS) if args.scenario == "all" else [args.scenario] if args.scenario else []
    markets += [SCENARIOS[n]() for n in names]
    if not markets:
        print("choose --scenario NAME|all and/or --history DIR", file=sys.stderr)
        return 2
    if args.brain == "claude":
        print("Using real Claude: every thought is a billed API call. On --history, Claude may already know "
              "what happened next, so only the synthetic scenarios are a fair test.", file=sys.stderr)
    print(f"{'market':<16} {'days':>4} {'BTC':>7} {'bot':>8} {'worst drop':>10} {'trades':>6} {'won':>4} "
          f"{'thoughts':>8} {'thinking':>9}  status")
    out = []
    for m in markets:
        brain = ClaudeBrain(cfg) if args.brain == "claude" else PlaybookBrain(cfg, Decimal(args.cost_usd))
        r = simulate(cfg, m, brain)
        x = r.summary()
        print(f"{m.name:<16} {x['days']:>4} {x['btc_change']:>+7.0%} {x['bot_change']:>+8.1%} {x['max_drop']:>10.1%} "
              f"{x['trades']:>6} {x['wins']:>4} {x['thoughts']:>8} {peso(x['thinking']):>9}  "
              f"{'alive' if x['alive'] else 'DEAD: ' + str(x['cause_of_death'])}")
        out.append(export(r))
    if args.json:
        Path(args.json).write_text(json.dumps(out), encoding="utf-8")
        print(f"\nwrote {args.json}")
    if args.brain != "claude":
        print("\nBrain: scripted stand-in that follows the playbook mechanically (no judgement, gullible to planted "
              "news). --brain claude runs the real thing.")
    return 0


def cmd_fetch(args, cfg) -> int:
    client = _client(cfg, need_keys=False)
    symbol, interval = args.symbol or cfg.symbol, args.interval or cfg.interval
    out, end = [], None
    while len(out) < args.limit:
        batch = client.klines(symbol, interval, limit=min(1000, args.limit - len(out)), end_time=end)
        if not batch:
            break
        out = batch + out
        end = batch[0].open_time - 1
        if len(batch) < 2:
            break
    dedup = {c.open_time: c for c in out}
    candles = [dedup[k] for k in sorted(dedup)]
    path = args.out or f"data/{symbol}-{interval}.csv"
    save_csv(candles, path)
    print(f"saved {len(candles)} {symbol} {interval} candles to {path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mortalbot", description="A trading bot whose budget is its life.")
    p.add_argument("-c", "--config", help="TOML config file (default: config.toml if it exists)")
    p.add_argument("-v", "--verbose", action="store_true", help="log every decision")
    sub = p.add_subparsers(dest="cmd", required=True)

    bt = sub.add_parser("backtest", help="replay history through the bot")
    src = bt.add_mutually_exclusive_group()
    src.add_argument("--csv", help="candles CSV (from `mortalbot fetch`)")
    src.add_argument("--synthetic", choices=["mixed", *REGIMES], help="generated market (default: mixed)")
    bt.add_argument("--seed", type=int, default=7)
    bt.add_argument("--candles", type=int, default=3000)
    bt.add_argument("--budget", help="override life.budget_php")
    bt.add_argument("--death-floor", help="override life.death_floor_php")

    pa = sub.add_parser("paper", help="live Coins.ph prices, simulated money")
    pa.add_argument("--validate-orders", action="store_true",
                    help="send every order to Coins.ph's test endpoint (needs API keys, spends nothing)")

    lv = sub.add_parser("live", help="real orders on Coins.ph")
    lv.add_argument("--confirm-real-money", action="store_true")

    st = sub.add_parser("status", help="show the bot's vital signs")
    st.add_argument("--mode", choices=["paper", "live"], default="paper")
    st.add_argument("--journal", type=int, default=3, metavar="N", help="show the last N thoughts (default 3)")

    si = sub.add_parser("simulate", help="run the full bot through simulated or historical markets")
    si.add_argument("--scenario", help="steady-bull, bull-then-crash, sideways-chop, long-bear, fake-news, or all")
    si.add_argument("--history", metavar="DIR", help="replay real daily candles from DIR/<SYMBOL>-1d.csv")
    si.add_argument("--brain", choices=["stand-in", "claude"], default="stand-in")
    si.add_argument("--cost-usd", default="0.05", help="stand-in's simulated cost per thought (default ~Opus)")
    si.add_argument("--json", help="write timelines and events to this file")

    fe = sub.add_parser("fetch", help="download Coins.ph candles to CSV for backtesting")
    fe.add_argument("--symbol")
    fe.add_argument("--interval")
    fe.add_argument("--limit", type=int, default=3000)
    fe.add_argument("--out")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    path = args.config or ("config.toml" if Path("config.toml").exists() else None)
    try:
        cfg = load_config(path)
    except (ConfigError, OSError) as e:
        print(e, file=sys.stderr)
        return 2
    _setup_logging(args.verbose, logging.WARNING if args.cmd == "backtest" else logging.INFO)
    try:
        if args.cmd == "backtest":
            return cmd_backtest(args, cfg)
        if args.cmd in ("paper", "live"):
            return cmd_trade(args, cfg, args.cmd)
        if args.cmd == "status":
            return cmd_status(args, cfg)
        if args.cmd == "simulate":
            return cmd_simulate(args, cfg)
        return cmd_fetch(args, cfg)
    except ExchangeError as e:
        print(f"Coins.ph error: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"network error (is api.pro.coins.ph reachable?): {e}", file=sys.stderr)
        return 1
