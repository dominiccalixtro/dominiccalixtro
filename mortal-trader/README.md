# MortalBot

A crypto trading bot whose budget is its life. You give it ₱5,000. It trades to survive. If its money runs out, it **dies**: it sells what it holds, writes its obituary, and never trades again.

Built for **Coins.ph** (Philippines), in plain Python with no dependencies.

> **Status:** v0.1. The bot logic, backtester and paper trading are tested. The Coins.ph live adapter follows Coins.ph's published API and is tested against canned responses only. It has **not** placed a real order yet. Follow [the path to real money](#path-to-real-money) before funding it.

---

## Where a Filipino bot can trade (researched Nov 2026)

| Platform | Retail API? | Notes | Verdict |
|---|---|---|---|
| **Coins.ph** | **Yes.** One key per account (Security → API Management) | BSP-licensed VASP since 2017. Binance-style REST API at `api.pro.coins.ph`, peso pairs (BTCPHP, ETHPHP...) | **Target** |
| PDAX | Institutional only | API keys via institutions@pdax.ph; HMAC-SHA384 | Not for retail yet |
| PSE stock brokers (COL, FirstMetroSec, Philstocks...) | No public API found | PSE's Direct Market Access rules have prohibited algorithmic trading; a 2023 proposal would allow it. The PSE is also moving to Nasdaq Eqlipse in 2026 | Not possible today |
| Binance, Bybit, OKX | Yes, but... | Blocked by the SEC (Binance since 2024; OKX and Bybit named in the Aug 2025 advisory) under the CASP rules (SEC MC 4 and 5, s. 2025) | **Do not use**: regulatory and fund-access risk |
| Interactive Brokers | Yes (TWS API) | Third-party sites say it accepts PH residents; USD-based, no PSE | Possible later, for US stocks |

**Funding caveat:** in Oct 2026 the BSP restricted incoming InstaPay/PESONet transfers to DCPay, the company behind Coins.ph's peso wallet. Coins.ph says its crypto license is intact. Check that you can deposit and withdraw before you rely on it.

---

## How its life works

| Health | Equity vs. ₱5,000 life | Behaviour |
|---|---|---|
| THRIVING | ≥ 100% | normal size |
| HEALTHY | ≥ 75% | normal size |
| WOUNDED | ≥ 40% | trades at **half** size |
| CRITICAL | < 40% | trades at **quarter** size |
| **DEAD** | ≤ death floor, or below the exchange's minimum order | sells everything, stops forever |

- **Equity** = cash + coins valued at what they would sell for *right now, after fees*. The bot never thinks it's healthier than it is.
- **Death is permanent.** The state file is marked dead and the bot refuses to start under that name again. A new life needs a new `name`, and a life can't be topped up.
- **Wounds heal slowly.** Equity has to clear a band by 2% before the bot moves back up, so it doesn't flap between states.
- **It rests after a losing streak.** After 3 losses in a row it sits out 12 candles.
- **Only money kills it.** Network errors and exchange outages only trigger retries with backoff.
- **It only spends its own life.** The bot keeps its own ledger. If your account holds more than ₱5,000, it never touches the rest. In live mode it refuses to start if the account holds *less* than its ledger says it owns.

**About the death floor.** With `death_floor_php = 0` (the default, "dies when the money is gone"), death is slow. A hurt bot shrinks its bets, so it bleeds out instead of blowing up, and usually dies by *starvation*: it can no longer afford the smallest order. To make death a real stake, set a floor such as `2500` ("dies after losing half").

## Strategy

The strategy is a deliberately simple trend-follower:

- **Long only**, because spot markets can't short.
- **Entry:** EMA 12 crosses above EMA 26 *and* the price is above EMA 100.
- **Exit:** the crossover reverses, or the stop-loss is hit. The stop sits 2.5 × ATR below entry and trails up.
- **Size:** chosen so that hitting the stop costs about 2% of equity, scaled down when the bot is hurt.

The strategy is one function (`strategy.decide`). Swap it out and the life, risk and execution layers keep working.

---

## Quick start

Requires Python 3.11+. Nothing to install.

```bash
cd mortal-trader
cp config.example.toml config.toml

python3 -m unittest discover -s tests              # 50 tests
python3 -m mortalbot backtest --synthetic mixed     # mechanics check on generated data
python3 -m mortalbot backtest --synthetic chop --death-floor 4000   # watch it die

python3 -m mortalbot fetch --limit 3000             # real BTCPHP 1h candles from Coins.ph
python3 -m mortalbot backtest --csv data/BTCPHP-1h.csv

python3 -m mortalbot paper                          # live prices, fake money
python3 -m mortalbot status                         # vital signs
```

With API keys (environment variables only, never in `config.toml`):

```bash
export COINS_API_KEY=...  COINS_API_SECRET=...
python3 -m mortalbot paper --validate-orders        # every order checked by Coins.ph's test endpoint; spends nothing
python3 -m mortalbot live --confirm-real-money      # real orders
python3 -m mortalbot status --mode live
```

Exit codes: `0` ok, `1` exchange/network error, `2` refused (config, ledger mismatch, missing confirmation), `3` the bot is dead.

## Path to real money

1. **Backtest on real Coins.ph candles** (`fetch`, then `backtest --csv`) and compare against buy-and-hold.
2. **Paper trade for at least 2 weeks** on an always-on machine.
3. **Create a restricted API key:** trading enabled, **withdrawals disabled**, bound to your server's IP.
4. **Run `paper --validate-orders` for a day.** This proves your key, signing and order parameters against the real API without spending anything.
5. **Set `execution.fee_rate`** to your actual Coins.ph fee tier.
6. **Fund the account with only what the bot may lose**, then run `live --confirm-real-money`.
7. **Run it on something that doesn't sleep** (a small VPS or a Raspberry Pi). The stop-loss is checked on every poll, so a sleeping laptop means an unwatched stop.

## What is verified and what isn't

| Verified (in this repo's tests) | Not yet verified |
|---|---|
| Life bands, death, permanent death, resting, sizing, stop-loss | Any call to the live Coins.ph API: it was unreachable from the build sandbox |
| Backtester determinism and ledger consistency | The exact live response shapes for klines, ticker and order fills |
| HMAC-SHA256 signing against a published reference example | Which asset Coins.ph charges fees in for your account |
| Signed request format, order parameters, fill parsing (canned responses) | Real slippage on Coins.ph's order book |
| Recovery when the network fails mid-order (it asks before assuming) | |

## Honest expectations

- **Synthetic backtests prove the mechanics, not profitability.** On generated data the bot made +27% where buy-and-hold made +177% (mixed market), and +11% against +823% in a strong bull market. Trend-followers with tight risk trade upside for survival.
- **Fees dominate small accounts.** The assumed 0.3% fee plus 0.1% slippage per side is about 0.8% per round trip, about ₱36 on a ₱4,500 position. A strategy that wins 40% of the time needs its winners to be much bigger than its losers just to break even.
- **Paper fills are optimistic.** They fill the whole order at one price, while real market orders can fill worse in a crash.
- Treat the ₱5,000 as tuition, and taxes on crypto gains are your responsibility.

---

## Layout

```
mortalbot/
  bot.py           the mortal bot: events, entries, exits, death
  life.py          health bands, death conditions, obituary
  state.py         ledger + atomic persistence; dead bots stay dead
  strategy.py      EMA crossover + trend filter (swap this)
  risk.py          risk-based position sizing
  indicators.py    EMA, ATR
  backtest.py      replays candles through the same bot code
  runner.py        paper/live loop, retries, account reconciliation
  data.py          CSV + synthetic market generator
  exchange/
    paper.py       simulated fills with fees and slippage
    coinsph.py     Coins.ph REST client, signing, ShadowBroker (validate-only)
  cli.py           backtest | paper | live | status | fetch
tests/             50 unit tests, no network needed
```

## Sources

- Coins.ph: [API key setup](https://support.coins.ph/hc/en-us/articles/11620395767193) · [REST API reference](https://coins-docs.github.io/rest-api) · [OpenAPI spec (apis.io)](https://apis.io/apis/coinsph/coinsph-spot-api/)
- PDAX: [API reference (institutional)](https://doc.restapi.pdax.ph/)
- PSE: [Consultation on algorithmic trading via DMA (CN-2023-0043)](https://documents.pse.com.ph/CircularOPSPDF/CN-2023-0043.pdf) · [Philstar, Jan 2024](https://www.philstar.com/business/2024/01/04/2323215/pse-introduce-more-products)
- SEC enforcement: [Yahoo Finance: SEC cuts access to offshore exchanges](https://finance.yahoo.com/news/philippine-sec-cuts-access-offshore-130201212.html) · [CCN: SEC blocks 10 exchanges](https://www.ccn.com/education/crypto/philippines-sec-bans-crypto-exchanges-funds-risk-explained/)
- Licensing: [BitPinas: licensed VASPs](https://bitpinas.com/feature/list-licensed-virtual-currency-exchanges-philippines/) · [Cointelegraph: Coins.ph license, Oct 2026](https://cointelegraph.com/news/coinsph-says-bsp-license-intact-amid-claims-of-potential-revocation)
- Brokers: [BrokerChooser: algo-trading brokers for the Philippines](https://brokerchooser.com/best-brokers/best-brokers-for-algo-trading-in-the-philippines)

*Not financial advice. This is software that can lose all of the money you give it. That's the point of it.*
