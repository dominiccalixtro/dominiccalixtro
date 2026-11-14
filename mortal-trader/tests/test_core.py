import tempfile
import unittest
from decimal import Decimal

from helpers import D, broker, candles_from_closes, rules, small_cfg

from mortalbot.config import BotConfig, ConfigError, load_config, validate
from mortalbot.indicators import atr, ema
from mortalbot.life import Health, assess, size_multiplier
from mortalbot.models import OrderRejected, floor_to_step, fmt_decimal, peso
from mortalbot.risk import entry_quote_amount
from mortalbot.state import BotIsDead, BotState, StateMismatch, StateStore, awaken
from mortalbot.strategy import Action, decide


class Indicators(unittest.TestCase):
    def test_ema_constant_series_is_constant(self):
        self.assertEqual(ema([5.0] * 10, 3)[-1], 5.0)

    def test_ema_known_values(self):
        out = ema([1, 2, 3, 4, 5], 3)
        self.assertEqual(out[:2], [None, None])
        self.assertAlmostEqual(out[2], 2.0)  # SMA seed
        self.assertAlmostEqual(out[3], 3.0)  # 4*0.5 + 2*0.5
        self.assertAlmostEqual(out[4], 4.0)

    def test_atr_of_constant_range(self):
        cs = candles_from_closes([100.0] * 20, spread=0.01)
        self.assertAlmostEqual(atr(cs, 5)[-1], 2.0, places=6)


class Money(unittest.TestCase):
    def test_floor_to_step_never_rounds_up(self):
        self.assertEqual(floor_to_step(D("0.123456789"), D("0.00001")), D("0.12345"))

    def test_fmt_decimal_has_no_exponent(self):
        self.assertEqual(fmt_decimal(D("1E-8")), "0.00000001")
        self.assertEqual(fmt_decimal(D("100.50")), "100.5")

    def test_peso_negative(self):
        self.assertEqual(peso(D("-1234.5")), "-₱1,234.50")


class Life(unittest.TestCase):
    def setUp(self):
        self.life = BotConfig().life

    def test_bands(self):
        mn = D("100")
        self.assertIs(assess(D("5200"), self.life, mn).health, Health.THRIVING)
        self.assertIs(assess(D("4000"), self.life, mn).health, Health.HEALTHY)
        self.assertIs(assess(D("3000"), self.life, mn).health, Health.WOUNDED)
        self.assertIs(assess(D("1500"), self.life, mn).health, Health.CRITICAL)

    def test_dies_when_it_cannot_afford_the_smallest_order(self):
        v = assess(D("99.99"), self.life, D("100"))
        self.assertIs(v.health, Health.DEAD)
        self.assertIn("starved", v.cause_of_death)

    def test_dies_at_death_floor(self):
        self.life.death_floor_php = D("2500")
        v = assess(D("2500"), self.life, D("100"))
        self.assertIs(v.health, Health.DEAD)
        self.assertIn("death floor", v.cause_of_death)

    def test_wounds_heal_slowly(self):
        # Just above the 75% line is not enough to recover from WOUNDED...
        self.assertIs(assess(D("3760"), self.life, D("100"), previous=Health.WOUNDED).health, Health.WOUNDED)
        # ...but clearly above it is.
        self.assertIs(assess(D("3900"), self.life, D("100"), previous=Health.WOUNDED).health, Health.HEALTHY)
        # Getting worse is immediate.
        self.assertIs(assess(D("3700"), self.life, D("100"), previous=Health.HEALTHY).health, Health.WOUNDED)

    def test_hurt_bots_trade_smaller(self):
        self.assertLess(size_multiplier(Health.CRITICAL, self.life), size_multiplier(Health.WOUNDED, self.life))
        self.assertLess(size_multiplier(Health.WOUNDED, self.life), size_multiplier(Health.HEALTHY, self.life))


class Risk(unittest.TestCase):
    def kw(self, **over):
        base = dict(equity=D("5000"), cash=D("5000"), price=D("100"), atr=D("2"), multiplier=1.0,
                    risk=BotConfig().risk, fee_rate=D("0.003"), min_notional=D("100"))
        base.update(over)
        return base

    def test_risk_based_size(self):
        # risk 2% of 5000 = 100; stop 2.5*2 = 5 per coin -> 20 coins -> 2000 pesos
        amount, _ = entry_quote_amount(**self.kw())
        self.assertEqual(amount, D("2000.00"))

    def test_capped_by_cash(self):
        amount, why = entry_quote_amount(**self.kw(atr=D("0.1")))
        self.assertLessEqual(amount, D("5000") * D("0.95"))
        self.assertIn("capped", why)

    def test_wounded_bot_trades_smaller(self):
        full, _ = entry_quote_amount(**self.kw())
        half, _ = entry_quote_amount(**self.kw(multiplier=0.5))
        self.assertEqual(half, full / 2)

    def test_refuses_when_minimum_order_is_too_risky(self):
        amount, why = entry_quote_amount(**self.kw(equity=D("150"), cash=D("150"), atr=D("10")))
        self.assertEqual(amount, 0)
        self.assertIn("too weak", why)


class StateAndRebirth(unittest.TestCase):
    def test_roundtrip(self):
        s = BotState.newborn("x", "paper", "BTCPHP", D("5000"), 1)
        s.position_qty, s.stop_price = D("0.001"), D("123.45")
        self.assertEqual(BotState.from_dict(s.to_dict()), s)

    def test_dead_bots_stay_dead(self):
        with tempfile.TemporaryDirectory() as d:
            store = StateStore(d, "x", "paper")
            state, born = awaken(store, name="x", mode="paper", symbol="BTCPHP", budget=D("5000"), now_ms=1)
            self.assertTrue(born)
            state.alive, state.cause_of_death = False, "test"
            store.save(state)
            with self.assertRaises(BotIsDead):
                awaken(store, name="x", mode="paper", symbol="BTCPHP", budget=D("5000"), now_ms=2)

    def test_symbol_change_needs_new_life(self):
        with tempfile.TemporaryDirectory() as d:
            store = StateStore(d, "x", "paper")
            awaken(store, name="x", mode="paper", symbol="BTCPHP", budget=D("5000"), now_ms=1)
            with self.assertRaises(StateMismatch):
                awaken(store, name="x", mode="paper", symbol="ETHPHP", budget=D("5000"), now_ms=2)


class Strategy(unittest.TestCase):
    def test_enters_on_bullish_crossover_above_trend(self):
        cfg = small_cfg()
        closes = [100.0] * 30 + [99, 98, 97, 96, 95] + [100, 106, 112]
        acts = [decide(candles_from_closes(closes[:i]), cfg.strategy, False).action for i in range(15, len(closes) + 1)]
        self.assertIn(Action.ENTER, acts)

    def test_holds_while_warming_up(self):
        d = decide(candles_from_closes([100.0] * 5), small_cfg().strategy, False)
        self.assertIs(d.action, Action.HOLD)
        self.assertIn("warming up", d.reason)


class Config(unittest.TestCase):
    def test_defaults_are_valid(self):
        validate(BotConfig())

    def test_rejects_bad_values(self):
        cfg = BotConfig()
        cfg.life.death_floor_php = D("6000")
        with self.assertRaises(ConfigError):
            validate(cfg)

    def test_rejects_unknown_keys(self):
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
            f.write("[life]\nbudjet_php = 5000\n")
        with self.assertRaises(ConfigError):
            load_config(f.name)

    def test_example_config_loads(self):
        cfg = load_config(__file__.replace("tests/test_core.py", "config.example.toml"))
        self.assertEqual(cfg.life.budget_php, D("5000"))


class Paper(unittest.TestCase):
    def test_buy_then_sell_loses_exactly_the_fees(self):
        cfg = small_cfg()
        b, r = broker(cfg), rules()
        b.mark(D("1000000"), 0)
        buy = b.market_buy(r, D("1000"))
        self.assertLessEqual(buy.quote_qty, D("1000"))
        sell = b.market_sell(r, buy.base_qty)
        gross = buy.base_qty * D("1000000")
        self.assertEqual(buy.quote_qty - sell.quote_qty, gross * D("0.002"))

    def test_rejects_orders_under_minimum(self):
        b, r = broker(small_cfg()), rules("100")
        b.mark(D("1000000"), 0)
        with self.assertRaises(OrderRejected):
            b.market_buy(r, D("50"))


if __name__ == "__main__":
    unittest.main()
