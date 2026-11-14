"""Which simple trend rule would have kept a small crypto account alive? (Coins.ph daily candles)

Long/flat only (spot, no shorting). Signal on day t's close -> trade at day t+1's open.
Cost per side = 0.3% fee + 0.1% slippage. Rolling 1-year windows start every 90 days, so
results don't hinge on one lucky entry date.

    for s in BTCPHP ETHPHP SOLPHP XRPPHP; do
        python3 -m mortalbot fetch --symbol $s --interval 1d --limit 4000 --out data/$s-1d.csv
    done
    python3 research/trend_filters.py data

Results as of Nov 2026 (Oct 2022 - Nov 2026, full period after a 200-day warm-up):

    BTCPHP  buy & hold +181%, worst drop 50%   | close > SMA50 +201%, worst drop 26%
    ETHPHP  buy & hold  +19%, worst drop 65%   | close > SMA50 +131%, worst drop 34%
    XRPPHP / SOLPHP: no rule was consistently good (worst drops 40-75%)

Caveats: one market cycle; nine variants tried on the same data (some luck is baked in);
past trends say nothing certain about future ones.
"""
import csv
import statistics
import sys

COST = 0.004  # 0.3% fee + 0.1% slippage per side
DATA = sys.argv[1] if len(sys.argv) > 1 else "data"


def load(sym):
    rows = list(csv.DictReader(open(f"{DATA}/{sym}-1d.csv")))
    return [(int(r["open_time"]), float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"])) for r in rows]


def sma(xs, n, i):
    return sum(xs[i - n + 1:i + 1]) / n if i >= n - 1 else None


def ema_series(xs, n):
    out, k, prev = [None] * len(xs), 2 / (n + 1), None
    for i, x in enumerate(xs):
        if i == n - 1:
            prev = sum(xs[:n]) / n
        elif i >= n:
            prev = x * k + prev * (1 - k)
        out[i] = prev
    return out


def atr_series(c, n=14):
    trs = [c[0][2] - c[0][3]] + [max(c[i][2] - c[i][3], abs(c[i][2] - c[i - 1][4]), abs(c[i][3] - c[i - 1][4]))
                                 for i in range(1, len(c))]
    out, prev = [None] * len(c), None
    for i, tr in enumerate(trs):
        if i == n - 1:
            prev = sum(trs[:n]) / n
        elif i >= n:
            prev = (prev * (n - 1) + tr) / n
        out[i] = prev
    return out


def run(c, want_in, start, stop_mult=None, warm=200):
    """want_in(i) -> bool desired exposure after close i. Optional ATR trailing stop (intraday)."""
    closes = [x[4] for x in c]
    atrs = atr_series(c)
    cash, coins, inside, stop = 1.0, 0.0, False, None
    trades, days_in, curve = 0, 0, []
    for i in range(max(start, warm), len(c) - 1):
        o_next, h_next, l_next = c[i + 1][1], c[i + 1][2], c[i + 1][3]
        want = want_in(i)
        if inside and stop_mult and stop is not None and closes[i] <= stop:
            want = False  # stop breached on close; exit next open
        if want and not inside:
            coins, cash, inside, trades = cash * (1 - COST) / o_next, 0.0, True, trades + 1
            stop = o_next - stop_mult * atrs[i] if stop_mult else None
        elif not want and inside:
            cash, coins, inside, stop = coins * o_next * (1 - COST), 0.0, False, None
        if inside and stop_mult:
            stop = max(stop, c[i + 1][4] - stop_mult * atrs[i + 1])
        days_in += inside
        curve.append(cash + coins * c[i + 1][4])
    peak, mdd, low = 0, 0, 1.0
    for e in curve:
        peak = max(peak, e)
        mdd = max(mdd, 1 - e / peak)
        low = min(low, e)
    n = len(curve)
    return {"ret": curve[-1] - 1 if curve else 0, "mdd": mdd, "low": low, "trades": trades,
            "in": days_in / n if n else 0, "years": n / 365}


def strategies(c):
    closes = [x[4] for x in c]
    e = {n: ema_series(closes, n) for n in (10, 20, 30, 50)}
    hi20 = lambda i: max(closes[i - 20:i]) if i >= 20 else None
    lo10 = lambda i: min(closes[i - 10:i]) if i >= 10 else None
    state = {"don": False}

    def donchian(i):
        if closes[i] > hi20(i):
            state["don"] = True
        elif closes[i] < lo10(i):
            state["don"] = False
        return state["don"]

    return {
        "buy & hold": (lambda i: True, None),
        "close > SMA20": (lambda i: closes[i] > sma(closes, 20, i), None),
        "close > SMA50": (lambda i: closes[i] > sma(closes, 50, i), None),
        "close > SMA100": (lambda i: closes[i] > sma(closes, 100, i), None),
        "close > SMA200": (lambda i: closes[i] > sma(closes, 200, i), None),
        "EMA20 > EMA50": (lambda i: e[20][i] > e[50][i], None),
        "EMA10 > EMA30": (lambda i: e[10][i] > e[30][i], None),
        "SMA50 + 3xATR stop": (lambda i: closes[i] > sma(closes, 50, i), 3.0),
        "Donchian 20/10": (donchian, None),
    }


def main():
    syms = ["BTCPHP", "ETHPHP", "XRPPHP", "SOLPHP"]
    for sym in syms:
        c = load(sym)
        print(f"\n=== {sym}  ({len(c)} days; full-period numbers, then rolling 1-year windows) ===")
        print(f"{'strategy':<20} {'return':>8} {'maxDD':>6} {'worst':>6} {'trades':>6} {'in mkt':>6} | "
              f"{'1y med':>7} {'1y worst':>8} {'1y worstDD':>10}")
        for name, (fn, stop) in strategies(c).items():
            full = run(c, fn, 0, stop)
            # rolling 1-year windows starting every 90 days (after 200-day warmup)
            wins = []
            for s0 in range(200, len(c) - 366, 90):
                sub = c[: s0 + 366]
                r = run(sub, strategies(sub)[name][0], s0, stop)
                wins.append(r)
            med = statistics.median(w["ret"] for w in wins) if wins else 0
            worst = min((w["ret"] for w in wins), default=0)
            wdd = max((w["mdd"] for w in wins), default=0)
            print(f"{name:<20} {full['ret']:>+7.0%} {full['mdd']:>6.0%} {full['low'] - 1:>+6.0%} {full['trades']:>6} "
                  f"{full['in']:>6.0%} | {med:>+7.0%} {worst:>+8.0%} {wdd:>10.0%}")


main()
