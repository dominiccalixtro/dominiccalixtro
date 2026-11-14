"""The simulator: the real agent through synthetic and historical markets."""

import unittest
from decimal import Decimal

from helpers import D

from mortalbot.config import BotConfig
from mortalbot.models import Candle
from mortalbot.simulate import DAY, SCENARIOS, PlaybookBrain, history_market, simulate


class Scenarios(unittest.TestCase):
    def setUp(self):
        self.cfg = BotConfig()

    def test_every_scenario_runs_and_survives(self):
        for name, make in SCENARIOS.items():
            with self.subTest(name):
                r = simulate(self.cfg, make())
                self.assertTrue(r.state.alive)
                self.assertGreater(len(r.timeline), 30)
                self.assertGreaterEqual(r.state.cash + sum(p.cost for p in r.state.positions.values()), 0)

    def test_trend_rule_beats_holding_in_a_bear_market(self):
        r = simulate(self.cfg, SCENARIOS["long-bear"]())
        s = r.summary()
        self.assertLess(s["btc_change"], -0.4)
        self.assertGreater(s["bot_change"], s["btc_change"] + 0.2)

    def test_fake_news_is_contained(self):
        r = simulate(self.cfg, SCENARIOS["fake-news"]())
        notes = " ".join(j["blocked"] for j in r.state.journal)
        self.assertIn("SCAMPHP: not a tradable market", notes)
        self.assertNotIn("SCAMPHP", {t["symbol"] for t in r.state.trades})
        for row in r.timeline:  # never more than two coins, never over the caps
            self.assertLessEqual(len(row["held"]), 2)

    def test_deterministic(self):
        a = simulate(self.cfg, SCENARIOS["bull-then-crash"]()).summary()
        b = simulate(self.cfg, SCENARIOS["bull-then-crash"]()).summary()
        self.assertEqual(a, b)

    def test_free_brain_costs_nothing(self):
        r = simulate(self.cfg, SCENARIOS["steady-bull"](), PlaybookBrain(self.cfg, Decimal("0")))
        self.assertEqual(r.state.thinking_paid, 0)
        self.assertGreater(r.state.thoughts, 0)


class History(unittest.TestCase):
    def test_aligns_coins_and_waits_for_warmup(self):
        def series(n, start, skip=None):
            return [Candle(start + i * DAY, 100.0 + i, 101.0 + i, 99.0 + i, 100.5 + i, 1.0, start + (i + 1) * DAY - 1)
                    for i in range(n) if i != skip]
        m = history_market({"BTCPHP": series(120, 0), "ETHPHP": series(120, 0, skip=5)})
        self.assertEqual(len(m.daily["BTCPHP"]), 119)
        self.assertEqual(m.birth_ms, 75 * DAY)
        r = simulate(BotConfig(), m)
        self.assertTrue(r.state.positions or r.state.trades, "a steady uptrend gets bought")


if __name__ == "__main__":
    unittest.main()
