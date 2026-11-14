"""Coins.ph Pro REST API adapter (stdlib only).

Coins.ph's API follows the Binance spot API conventions:
  * base URL https://api.pro.coins.ph
  * private calls carry the key in the X-COINS-APIKEY header and a
    `signature` parameter = HMAC-SHA256(secret, query string), plus
    `timestamp` and `recvWindow`
  * /openapi/v1/order/test validates an order without executing it

VERIFY BEFORE REAL MONEY: this adapter was written against the published
docs and tested against canned responses only; it has not been run against
the live API. Run `mortalbot paper --validate-orders` first. It sends every
order to the test endpoint, which proves your key, signing and parameters
work without spending anything.

API key hygiene: enable trading only, never withdrawals, and bind the key to
your server's IP address in Coins.ph's API Management page.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable
from decimal import Decimal

from ..models import Candle, ExchangeError, Fill, OrderRejected, SymbolRules, fmt_decimal

log = logging.getLogger("mortalbot.coinsph")

# (method, url, headers, body) -> (http status, response text)
Transport = Callable[[str, str, dict, bytes | None], tuple[int, str]]

DEFAULT_BASE_URL = "https://api.pro.coins.ph"


def urllib_transport(method: str, url: str, headers: dict, body: bytes | None, timeout: float = 15) -> tuple[int, str]:
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")


def sign(secret: str, payload: str) -> str:
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


class CoinsPhClient:
    def __init__(
        self,
        api_key: str | None = None,
        api_secret: str | None = None,
        *,
        base_url: str = DEFAULT_BASE_URL,
        transport: Transport | None = None,
        recv_window: int = 5000,
        clock: Callable[[], float] = time.time,
        fee_rate_estimate: Decimal = Decimal("0.003"),
    ):
        self.api_key = api_key
        self._secret = api_secret
        self.base_url = base_url.rstrip("/")
        self.transport = transport or urllib_transport
        self.recv_window = recv_window
        self.clock = clock
        self.time_offset_ms = 0
        self.fee_rate_estimate = fee_rate_estimate

    def __repr__(self) -> str:  # never print the secret
        return f"CoinsPhClient(base_url={self.base_url!r}, key={'set' if self.api_key else 'none'})"

    @property
    def has_keys(self) -> bool:
        return bool(self.api_key and self._secret)

    # ---- transport -------------------------------------------------------

    def _now_ms(self) -> int:
        return int(self.clock() * 1000) + self.time_offset_ms

    def _request(self, method: str, path: str, params: dict | None = None, *, signed: bool = False):
        params = {k: v for k, v in (params or {}).items() if v is not None}
        headers = {"Accept": "application/json", "User-Agent": "mortalbot/0.1"}
        if signed:
            if not self.has_keys:
                raise ExchangeError(0, None, "API key and secret are required for this call")
            params["recvWindow"] = self.recv_window
            params["timestamp"] = self._now_ms()
            query = urllib.parse.urlencode(params)
            query += "&signature=" + sign(self._secret, query)
            headers["X-COINS-APIKEY"] = self.api_key
        else:
            query = urllib.parse.urlencode(params)
        if method == "POST":
            url, body = self.base_url + path, query.encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            url, body = self.base_url + path + (f"?{query}" if query else ""), None
        status, text = self.transport(method, url, headers, body)
        try:
            data = json.loads(text) if text else {}
        except json.JSONDecodeError:
            raise ExchangeError(status, None, f"non-JSON response: {text[:200]}") from None
        code = data.get("code") if isinstance(data, dict) else None
        if status >= 400 or (isinstance(code, int) and code < 0):
            msg = data.get("msg", "") if isinstance(data, dict) else str(data)
            raise ExchangeError(status, code, msg)
        return data

    # ---- public market data ---------------------------------------------

    def sync_time(self) -> int:
        """Measure clock skew; signed calls fail if our clock is off by more than recvWindow."""
        server = int(self._request("GET", "/openapi/v1/time")["serverTime"])
        self.time_offset_ms = server - int(self.clock() * 1000)
        return self.time_offset_ms

    def symbol_rules(self, symbol: str) -> SymbolRules:
        data = self._request("GET", "/openapi/v1/exchangeInfo", {"symbol": symbol})
        match = [s for s in data.get("symbols", []) if s.get("symbol") == symbol]
        if not match:
            raise ExchangeError(200, None, f"symbol {symbol} not found on Coins.ph")
        return parse_symbol_rules(match[0])

    def klines(self, symbol: str, interval: str, limit: int = 500, *, start_time: int | None = None,
               end_time: int | None = None) -> list[Candle]:
        rows = self._request("GET", "/openapi/quote/v1/klines", {
            "symbol": symbol, "interval": interval, "limit": limit, "startTime": start_time, "endTime": end_time,
        })
        return [Candle(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]), int(r[6]))
                for r in rows]

    def ticker_price(self, symbol: str) -> Decimal:
        data = self._request("GET", "/openapi/quote/v1/ticker/price", {"symbol": symbol})
        if isinstance(data, list):
            data = next(d for d in data if d.get("symbol") == symbol)
        return Decimal(str(data["price"]))

    def ticker_prices(self, symbols) -> dict[str, Decimal]:
        """Prices for several markets: one call for all tickers, or one call each if that fails."""
        wanted = set(symbols)
        try:
            data = self._request("GET", "/openapi/quote/v1/ticker/price")
            if isinstance(data, list):
                found = {d["symbol"]: Decimal(str(d["price"])) for d in data if d.get("symbol") in wanted}
                if found.keys() == wanted:
                    return found
        except (ExchangeError, KeyError):
            pass
        return {s: self.ticker_price(s) for s in sorted(wanted)}

    # ---- private: account and orders ------------------------------------

    def balances(self) -> dict[str, tuple[Decimal, Decimal]]:
        """{asset: (free, locked)}"""
        data = self._request("GET", "/openapi/v1/account", signed=True)
        return {b["asset"]: (Decimal(str(b["free"])), Decimal(str(b["locked"]))) for b in data.get("balances", [])}

    def get_order(self, symbol: str, *, order_id: str | None = None, client_order_id: str | None = None) -> dict:
        return self._request("GET", "/openapi/v1/order", {
            "symbol": symbol, "orderId": order_id, "origClientOrderId": client_order_id}, signed=True)

    def test_order(self, params: dict) -> None:
        self._request("POST", "/openapi/v1/order/test", params, signed=True)

    def market_buy(self, rules: SymbolRules, quote_amount: Decimal) -> Fill:
        """Spend `quote_amount` pesos at market (quoteOrderQty)."""
        return self._place(rules, self.buy_params(rules, quote_amount))

    def market_sell(self, rules: SymbolRules, base_qty: Decimal) -> Fill:
        return self._place(rules, self.sell_params(rules, base_qty))

    @staticmethod
    def buy_params(rules: SymbolRules, quote_amount: Decimal) -> dict:
        return {"symbol": rules.symbol, "side": "BUY", "type": "MARKET",
                "quoteOrderQty": fmt_decimal(rules.round_quote(quote_amount)),
                "newClientOrderId": _client_id()}

    @staticmethod
    def sell_params(rules: SymbolRules, base_qty: Decimal) -> dict:
        return {"symbol": rules.symbol, "side": "SELL", "type": "MARKET",
                "quantity": fmt_decimal(rules.round_qty(base_qty)), "newClientOrderId": _client_id()}

    def _place(self, rules: SymbolRules, params: dict) -> Fill:
        cid = params["newClientOrderId"]
        try:
            data = self._request("POST", "/openapi/v1/order", params, signed=True)
        except ExchangeError as e:
            raise OrderRejected(f"Coins.ph rejected {params['side']}: {e}") from e
        except OSError as e:
            # The network failed mid-order: the order may or may not exist. Ask, don't guess.
            log.warning("network error while placing %s; checking whether it went through: %s", cid, e)
            data = self._find_order(rules.symbol, cid)
            if data is None:
                raise OrderRejected(f"order {cid} was not placed ({e})") from e
        if not data.get("fills") and data.get("status") not in ("FILLED", "PARTIALLY_FILLED", "CANCELED", "EXPIRED"):
            data = self._wait_filled(rules.symbol, data, cid)
        return parse_fill(data, rules, self.fee_rate_estimate, self._now_ms())

    def _find_order(self, symbol: str, cid: str) -> dict | None:
        for attempt in range(3):
            try:
                return self.get_order(symbol, client_order_id=cid)
            except ExchangeError:
                return None  # exchange says it does not know this order
            except OSError:
                time.sleep(2 * (attempt + 1))
        raise OrderRejected(f"cannot confirm whether order {cid} exists; stopping to avoid a double order")

    def _wait_filled(self, symbol: str, data: dict, cid: str, attempts: int = 5) -> dict:
        for i in range(attempts):
            time.sleep(0.5 * (i + 1))
            data = self.get_order(symbol, client_order_id=cid)
            if data.get("status") in ("FILLED", "PARTIALLY_FILLED", "CANCELED", "EXPIRED", "REJECTED"):
                break
        return data


class ShadowBroker:
    """Paper fills, but every order is first validated by Coins.ph's test endpoint.

    This proves the API key, the signature and the order parameters are right
    before any real peso is at stake.
    """

    def __init__(self, client: CoinsPhClient, paper):
        self.client = client
        self.paper = paper

    def mark(self, price: Decimal, ts: int, symbol: str | None = None) -> None:
        self.paper.mark(price, ts, symbol)

    def mark_all(self, prices: dict[str, Decimal], ts: int) -> None:
        self.paper.mark_all(prices, ts)

    def market_buy(self, rules: SymbolRules, quote_amount: Decimal) -> Fill:
        self._validate(self.client.buy_params(rules, quote_amount))
        return self.paper.market_buy(rules, quote_amount)

    def market_sell(self, rules: SymbolRules, base_qty: Decimal) -> Fill:
        self._validate(self.client.sell_params(rules, base_qty))
        return self.paper.market_sell(rules, base_qty)

    def _validate(self, params: dict) -> None:
        try:
            self.client.test_order(params)
        except ExchangeError as e:
            raise OrderRejected(f"Coins.ph test-order rejected {params['side']}: {e}") from e
        log.info("Coins.ph accepted test order %s %s", params["side"], params.get("quoteOrderQty") or params.get("quantity"))


# ---- parsing (pure functions, easy to test) ------------------------------

def _client_id() -> str:
    return "mortal-" + uuid.uuid4().hex[:20]


def _precision_step(digits: object, default: Decimal) -> Decimal:
    try:
        n = int(digits)
    except (TypeError, ValueError):
        return default
    return Decimal(1).scaleb(-n) if 0 <= n <= 18 else default


def parse_symbol_rules(s: dict) -> SymbolRules:
    filters = {f.get("filterType"): f for f in s.get("filters", [])}
    lot = filters.get("LOT_SIZE", {})
    price = filters.get("PRICE_FILTER", {})
    notional = filters.get("MIN_NOTIONAL") or filters.get("NOTIONAL") or {}
    quote_digits = s.get("quoteAssetPrecision", s.get("quotePrecision"))
    return SymbolRules(
        symbol=s["symbol"],
        base_asset=s.get("baseAsset", ""),
        quote_asset=s.get("quoteAsset", ""),
        min_qty=Decimal(str(lot.get("minQty", "0"))),
        step_size=Decimal(str(lot.get("stepSize", "0.00000001"))),
        min_notional=Decimal(str(notional.get("minNotional", "0"))),
        tick_size=Decimal(str(price.get("tickSize", "0.01"))),
        quote_step=_precision_step(quote_digits, Decimal("0.01")),
    )


def parse_fill(data: dict, rules: SymbolRules, fee_rate_estimate: Decimal, now_ms: int) -> Fill:
    side = data.get("side", "")
    executed = Decimal(str(data.get("executedQty", "0")))
    quote = Decimal(str(data.get("cummulativeQuoteQty", data.get("cumulativeQuoteQty", "0"))))
    fee_base = fee_quote_direct = fee_other = Decimal("0")
    fills = data.get("fills") or []
    for f in fills:
        c = Decimal(str(f.get("commission", "0")))
        asset = f.get("commissionAsset")
        if asset == rules.base_asset:
            fee_base += c
        elif asset == rules.quote_asset:
            fee_quote_direct += c
        else:
            fee_other += c
    avg = quote / executed if executed > 0 else Decimal("0")
    estimated = not fills and executed > 0
    if estimated:
        fee_quote_direct = quote * fee_rate_estimate
        log.warning("order %s returned no fill details; fee estimated at %s", data.get("orderId"), fee_rate_estimate)
    if fee_other:
        log.warning("fee of %s paid in a third asset; not deducted from the ledger", fee_other)
    if side == "BUY":
        base_qty = executed - fee_base
        quote_qty = quote + fee_quote_direct
    else:
        base_qty = executed + fee_base
        quote_qty = quote - fee_quote_direct
    return Fill(side=side, base_qty=base_qty, quote_qty=quote_qty, avg_price=avg,
                fee_quote=fee_quote_direct + fee_base * avg, order_id=str(data.get("orderId", "")),
                ts=int(data.get("transactTime", now_ms)))
