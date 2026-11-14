"""Coins.ph adapter against canned responses (no network)."""

import json
import tempfile
import unittest
import urllib.parse
from decimal import Decimal

from helpers import D, HOUR, small_cfg

from mortalbot.exchange.coinsph import CoinsPhClient, ShadowBroker, parse_fill, parse_symbol_rules, sign
from mortalbot.exchange.paper import PaperBroker
from mortalbot.models import ExchangeError, OrderRejected
from mortalbot.runner import LedgerMismatch, run_loop
from mortalbot.state import StateStore

SYMBOL_INFO = {
    "symbol": "BTCPHP", "baseAsset": "BTC", "quoteAsset": "PHP", "quoteAssetPrecision": 2,
    "filters": [
        {"filterType": "PRICE_FILTER", "tickSize": "1"},
        {"filterType": "LOT_SIZE", "minQty": "0.000001", "stepSize": "0.000001"},
        {"filterType": "MIN_NOTIONAL", "minNotional": "50"},
    ],
}


class FakeTransport:
    def __init__(self, routes):
        self.routes = routes  # (method, path) -> (status, payload) or callable(params)
        self.calls = []

    def __call__(self, method, url, headers, body):
        parsed = urllib.parse.urlparse(url)
        raw = body.decode() if body else parsed.query
        params = dict(urllib.parse.parse_qsl(raw))
        self.calls.append({"method": method, "path": parsed.path, "headers": headers, "params": params, "raw": raw})
        route = self.routes[(method, parsed.path)]
        status, payload = route(params) if callable(route) else route
        return status, json.dumps(payload)


def client(routes, key="k", secret="s"):
    t = FakeTransport(routes)
    return CoinsPhClient(key, secret, transport=t, clock=lambda: 1_700_000_000.0), t


class Signing(unittest.TestCase):
    def test_known_vector(self):
        # Published HMAC-SHA256 example from the Binance spot API docs, which Coins.ph mirrors.
        secret = "NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0j"
        query = ("symbol=LTCBTC&side=BUY&type=LIMIT&timeInForce=GTC&quantity=1&price=0.1"
                 "&recvWindow=5000&timestamp=1499827319559")
        self.assertEqual(sign(secret, query), "c8db56825ae71d6d79447849e617115f4a920fa2acdcab2b053c4b2838bd6b71")

    def test_signed_request_shape(self):
        c, t = client({("GET", "/openapi/v1/account"): (200, {"balances": [
            {"asset": "PHP", "free": "5000.5", "locked": "0"}]})})
        bal = c.balances()
        self.assertEqual(bal["PHP"], (D("5000.5"), D("0")))
        call = t.calls[0]
        self.assertEqual(call["headers"]["X-COINS-APIKEY"], "k")
        unsigned, sig = call["raw"].rsplit("&signature=", 1)
        self.assertEqual(sig, sign("s", unsigned))
        self.assertIn("timestamp=1700000000000", unsigned)

    def test_post_sends_form_body(self):
        c, t = client({("POST", "/openapi/v1/order/test"): (200, {})})
        c.test_order({"symbol": "BTCPHP", "side": "BUY", "type": "MARKET", "quoteOrderQty": "100"})
        self.assertEqual(t.calls[0]["headers"]["Content-Type"], "application/x-www-form-urlencoded")
        self.assertEqual(t.calls[0]["params"]["quoteOrderQty"], "100")

    def test_private_call_without_keys_fails_fast(self):
        c, t = client({}, key=None, secret=None)
        with self.assertRaises(ExchangeError):
            c.balances()
        self.assertEqual(t.calls, [])

    def test_secret_never_in_repr(self):
        c, _ = client({}, secret="supersecret")
        self.assertNotIn("supersecret", repr(c))


class Parsing(unittest.TestCase):
    def test_symbol_rules(self):
        r = parse_symbol_rules(SYMBOL_INFO)
        self.assertEqual((r.min_qty, r.step_size, r.min_notional, r.quote_step),
                         (D("0.000001"), D("0.000001"), D("50"), D("0.01")))

    def test_exchange_errors_raise(self):
        c, _ = client({("GET", "/openapi/v1/exchangeInfo"): (400, {"code": -1121, "msg": "Invalid symbol."})})
        with self.assertRaises(ExchangeError) as cm:
            c.symbol_rules("NOPE")
        self.assertEqual(cm.exception.code, -1121)

    def test_buy_fill_with_fee_in_coin(self):
        r = parse_symbol_rules(SYMBOL_INFO)
        f = parse_fill({"side": "BUY", "orderId": 9, "status": "FILLED", "executedQty": "0.001",
                        "cummulativeQuoteQty": "5000", "fills": [
                            {"price": "5000000", "qty": "0.001", "commission": "0.000003", "commissionAsset": "BTC"}]},
                       r, D("0.003"), 0)
        self.assertEqual(f.base_qty, D("0.000997"))
        self.assertEqual(f.quote_qty, D("5000"))
        self.assertEqual(f.fee_quote, D("15"))

    def test_sell_fill_with_fee_in_peso(self):
        r = parse_symbol_rules(SYMBOL_INFO)
        f = parse_fill({"side": "SELL", "executedQty": "0.001", "cummulativeQuoteQty": "5000", "fills": [
            {"commission": "15", "commissionAsset": "PHP"}]}, r, D("0.003"), 0)
        self.assertEqual((f.base_qty, f.quote_qty), (D("0.001"), D("4985")))

    def test_market_order_params(self):
        r = parse_symbol_rules(SYMBOL_INFO)
        c, t = client({("POST", "/openapi/v1/order"): lambda p: (200, {
            "side": p["side"], "orderId": 1, "status": "FILLED", "executedQty": "0.000999",
            "cummulativeQuoteQty": "4995", "fills": [{"commission": "0", "commissionAsset": "PHP"}]})})
        c.market_buy(r, D("4995.678"))
        p = t.calls[0]["params"]
        self.assertEqual((p["side"], p["type"], p["quoteOrderQty"]), ("BUY", "MARKET", "4995.67"))
        self.assertTrue(p["newClientOrderId"].startswith("mortal-"))
        c.market_sell(r, D("0.0009999"))
        self.assertEqual(t.calls[1]["params"]["quantity"], "0.000999")

    def test_network_failure_checks_before_assuming(self):
        r = parse_symbol_rules(SYMBOL_INFO)

        def flaky(method, url, headers, body):
            if method == "POST":
                raise TimeoutError("read timed out")
            return 200, json.dumps({"side": "BUY", "orderId": 5, "status": "FILLED", "executedQty": "0.001",
                                    "cummulativeQuoteQty": "5000", "fills": [
                                        {"commission": "0", "commissionAsset": "PHP"}]})

        c = CoinsPhClient("k", "s", transport=flaky)
        fill = c.market_buy(r, D("5000"))
        self.assertEqual(fill.base_qty, D("0.001"), "the order did go through; the ledger must know")


class ShadowMode(unittest.TestCase):
    def test_rejected_test_order_blocks_the_trade(self):
        r = parse_symbol_rules(SYMBOL_INFO)
        c, _ = client({("POST", "/openapi/v1/order/test"): (400, {"code": -1013, "msg": "Filter failure"})})
        paper = PaperBroker(D("0.003"), D("0"))
        paper.mark(D("5000000"), 0)
        with self.assertRaises(OrderRejected):
            ShadowBroker(c, paper).market_buy(r, D("1000"))


def candle_rows(n, now_ms, price=5_000_000.0):
    start = now_ms - n * HOUR
    return [[start + i * HOUR, str(price), str(price * 1.001), str(price * 0.999), str(price), "1",
             start + (i + 1) * HOUR - 1] for i in range(n)]


class Runner(unittest.TestCase):
    NOW = 1_767_225_600_000

    def routes(self, php_free="5000"):
        return {
            ("GET", "/openapi/v1/exchangeInfo"): (200, {"symbols": [SYMBOL_INFO]}),
            ("GET", "/openapi/v1/time"): (200, {"serverTime": self.NOW}),
            ("GET", "/openapi/quote/v1/ticker/price"): (200, {"symbol": "BTCPHP", "price": "5000000"}),
            ("GET", "/openapi/quote/v1/klines"): (200, candle_rows(60, self.NOW)),
            ("GET", "/openapi/v1/account"): (200, {"balances": [{"asset": "PHP", "free": php_free, "locked": "0"},
                                                                {"asset": "BTC", "free": "0", "locked": "0"}]}),
        }

    def test_paper_loop_runs_and_persists(self):
        cfg = small_cfg()
        with tempfile.TemporaryDirectory() as d:
            cfg.state_dir = d
            c, _ = client(self.routes())
            store = StateStore(d, cfg.name, "paper")
            paper = PaperBroker(cfg.execution.fee_rate, cfg.execution.slippage_bps)
            bot = run_loop(cfg, "paper", c, paper, store, max_loops=3, sleep=lambda s: None, now_ms=lambda: self.NOW)
            self.assertTrue(bot.alive)
            self.assertEqual(store.load().last_candle_close_time, self.NOW - 1)

    def test_live_refuses_underfunded_account(self):
        cfg = small_cfg()
        with tempfile.TemporaryDirectory() as d:
            c, _ = client(self.routes(php_free="1000"))
            with self.assertRaises(LedgerMismatch):
                run_loop(cfg, "live", c, c, StateStore(d, cfg.name, "live"), max_loops=1,
                         sleep=lambda s: None, now_ms=lambda: self.NOW)

    def test_network_errors_do_not_kill(self):
        cfg = small_cfg()
        routes = self.routes()
        routes[("GET", "/openapi/quote/v1/ticker/price")] = (503, {"code": -1001, "msg": "unavailable"})
        sleeps = []
        with tempfile.TemporaryDirectory() as d:
            c, _ = client(routes)
            paper = PaperBroker(cfg.execution.fee_rate, cfg.execution.slippage_bps)
            bot = run_loop(cfg, "paper", c, paper, StateStore(d, cfg.name, "paper"), max_loops=3,
                           sleep=sleeps.append, now_ms=lambda: self.NOW)
            self.assertTrue(bot.alive)
            self.assertEqual(sleeps, [60, 120], "exponential backoff")


if __name__ == "__main__":
    unittest.main()
