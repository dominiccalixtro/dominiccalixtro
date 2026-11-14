import tempfile
import unittest
from decimal import Decimal

from helpers import D, broker, candles_from_closes, rules, small_cfg

from mortalbot.backtest import run_backtest
from mortalbot.bot import MortalBot
from mortalbot.data import synthetic
from mortalbot.state import BotState, StateStore


def make_bot(cfg=None, store=None, cash="5000"):
    cfg = cfg or small_cfg()
    b = broker(cfg)
    state = BotState.newborn(cfg.name, "test", cfg.symbol, D(cash), 0)
    return MortalBot(cfg, state, b, rules(), store), b


def open_position(bot, b, price=D("1000000"), stop=D("950000"), pesos=D("4000")):
    b.mark(price, 0)
    bot.body.mark({bot.symbol: price})
    fill = bot.body.buy(bot.symbol, pesos, 0, stop_distance=price - stop)
    assert fill is not None
    return fill


class StopLoss(unittest.TestCase):
    def test_stop_loss_closes_the_position(self):
        bot, b = make_bot()
        open_position(bot, b)
        b.mark(D("949000"), 1)
        bot.on_price(D("949000"), 1)
        self.assertFalse(bot.in_position)
        self.assertEqual(len(bot.state.trades), 1)
        self.assertLess(Decimal(bot.state.trades[0]["pnl"]), 0)
        self.assertTrue(bot.alive)

    def test_price_above_stop_keeps_position(self):
        bot, b = make_bot()
        open_position(bot, b)
        bot.on_price(D("990000"), 1)
        self.assertTrue(bot.in_position)


class Death(unittest.TestCase):
    def test_dies_at_floor_sells_everything_and_writes_obituary(self):
        cfg = small_cfg(death_floor_php=4000)
        with tempfile.TemporaryDirectory() as d:
            store = StateStore(d, cfg.name, "test")
            bot, b = make_bot(cfg, store)
            open_position(bot, b, stop=D("1"))  # stop far away, so only death can save it
            crash = D("700000")
            b.mark(crash, 5)
            bot.on_price(crash, 5)
            self.assertFalse(bot.alive)
            self.assertFalse(bot.in_position, "last breath must liquidate")
            self.assertIn("death floor", bot.state.cause_of_death)
            self.assertTrue(store.obituary_path.exists())
            self.assertFalse(store.load().alive, "death must be saved to disk")

    def test_dead_bot_ignores_everything(self):
        bot, b = make_bot(cash="50")  # below the 100 peso minimum order
        bot.on_price(D("1000000"), 1)
        self.assertFalse(bot.alive)
        self.assertIn("starved", bot.state.cause_of_death)
        trades_before = len(bot.state.trades)
        bot.on_candle_close(candles_from_closes([100.0] * 60))
        bot.on_price(D("2000000"), 2)
        self.assertEqual(len(bot.state.trades), trades_before)


class Resting(unittest.TestCase):
    def test_rests_after_losing_streak(self):
        cfg = small_cfg()
        cfg.risk.max_consecutive_losses, cfg.risk.rest_candles = 2, 5
        bot, b = make_bot(cfg)
        for i in range(2):
            open_position(bot, b)
            b.mark(D("940000"), i + 1)
            bot.on_price(D("940000"), i + 1)
        self.assertEqual(bot.state.rest_candles_left, 5)
        self.assertEqual(bot.state.rest_until, 2 + 5 * 3_600_000)
        bot.on_candle_close(candles_from_closes([100.0] * 20))
        self.assertEqual(bot.state.rest_candles_left, 4)


class Backtest(unittest.TestCase):
    def test_deterministic(self):
        cs = synthetic(800, regime="mixed", seed=3)
        a, b = run_backtest(cs, small_cfg()), run_backtest(cs, small_cfg())
        self.assertEqual(a.final_equity, b.final_equity)
        self.assertEqual(len(a.state.trades), len(b.state.trades))

    def test_ledger_is_consistent(self):
        r = run_backtest(synthetic(1500, regime="mixed", seed=11), small_cfg())
        s = r.state
        realised = sum(Decimal(t["pnl"]) for t in s.trades)
        if not s.positions:
            self.assertEqual(s.cash, s.budget + realised)
        self.assertGreater(len(s.trades), 0)

    def test_choppy_market_with_high_floor_kills_it(self):
        r = run_backtest(synthetic(3000, regime="chop", seed=7), small_cfg(death_floor_php=4500))
        self.assertFalse(r.state.alive)
        self.assertIsNotNone(r.bot.obituary)
        self.assertLess(r.candles_seen, 3000)

    def test_never_spends_more_than_its_life(self):
        r = run_backtest(synthetic(2000, regime="bull", seed=5), small_cfg())
        self.assertGreaterEqual(r.state.cash, 0)
        for e in r.equity_curve:
            self.assertGreaterEqual(e, 0)


if __name__ == "__main__":
    unittest.main()
