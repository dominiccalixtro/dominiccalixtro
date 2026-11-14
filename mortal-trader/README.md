# MortalBot

An autonomous crypto trader whose budget is its life. You give it ₱2,500 and walk away. **Claude makes the trading decisions**: which coin, when, how much, and when to take risk off. It reads market data and the news, and keeps a journal to learn from its own mistakes. Its thinking costs money, and that comes out of its life too. When the money runs out, it **dies**: it sells what it holds, writes its obituary, and never trades again.

It trades on **Coins.ph** (Philippines), in Python.

> **Status: v0.3.** The bot logic, guardrails, paper trading and backtests are tested (94 tests). A dry run on **live Coins.ph market data and live news** worked end to end. Two things haven't touched the real services: the Claude call itself (tested against a simulated API) and order placement (tested against canned responses). Follow [the path to real money](#path-to-real-money) before funding it.

---

## The strategy: ride daily uptrends, sit out downtrends

**The rule (enforced by code):** a coin may only be held while its **daily close is above its 50-day average**. Below that, the bot holds pesos. A held coin is sold automatically after a daily close below the average. Spot markets can't short, so in a downtrend "pesos" *is* the position.

**Claude's job is everything inside that rule:** whether a fresh uptrend is worth joining now or is a likely false start, how much to commit, BTC or ETH, when to take risk off early (exchange, regulatory, security or stablecoin trouble; protecting a parabolic gain with a tighter stop), and when the news makes waiting wiser. It thinks in days, not hours.

**Why this rule.** Tested on 4 years of real Coins.ph daily candles (Oct 2022 - Nov 2026), with 0.4% fees plus slippage per trade ([`research/trend_filters.py`](research/trend_filters.py)):

| | Return | Worst drop | Worst 1-year window |
|---|---|---|---|
| BTC buy & hold | +181% | -50% | -41% |
| **BTC trend rule** | **+201%** | **-26%** | **-15%** |
| ETH buy & hold | +19% | -65% | -47% |
| **ETH trend rule** | **+131%** | **-34%** | **-2%** |

The implemented rules-only version, at the bot's real position sizes with a ₱2,500 life, made +154% on BTC (worst drop -13%) and +59% on ETH (worst drop -20%).

- **Expect to lose most trades.** About 1 trade in 4 won, but winners averaged ~+25% and losers ~-3%. Many small losses buy a few big trends. Both Claude and you need to be comfortable with that.
- **What failed:** the earlier hourly strategy lost **44%** over the last real year of BTC (93 trades, ₱1,491 in fees). Short-term signals churn and fees eat them. Trend rules on **SOL and XRP** were unreliable (drops of 40-75%), so the default universe is BTC and ETH.
- **Caveats:** one market cycle; nine variants tested on the same data, so some of the edge is luck; the past doesn't promise the future.
- **Right now (Nov 14, 2026):** BTC and ETH have both been below their 50-day averages for weeks, so a newborn bot would hold pesos and wait.

## How it works

```
every 30 seconds (free, no AI)              on events (costs money)
------------------------------              -----------------------
prices from Coins.ph                        Claude reads: vitals, positions, market table with
stop-loss / take-profit fire                daily trends, journal + lessons, ~25 news headlines,
daily trend checked; trend exits fire       and may run 2 web searches
upkeep charged, vital signs, death                 |
wake the brain when:                        decision: BUY / SELL / ADJUST_STOP / hold
  - a coin's daily trend turns up or down          |
  - the trend rule or a stop sold something guardrails (code) shrink or block orders
  - a coin moved 8%+                               |
  - its chosen review time arrived (4h-7d)  body executes; journal entry
```

**Claude proposes, code disposes.** Claude has freedom *inside* hard limits that only code enforces and Claude cannot change:

| Guardrail | Default | Why |
|---|---|---|
| Trend rule | buy only in a daily uptrend; auto-sell below it | The one rule that halved worst drops in the data |
| Tradable coins | BTC, ETH (PHP pairs) | A headline saying "buy SCAMCOIN" can't be obeyed |
| Mandatory disaster stop | 5-15% below entry | Protection between daily closes, set wide enough to sit outside normal daily swings |
| Risk per trade | a stop-out costs ≤ 4% of equity | One bad call can't kill it |
| Per coin / positions | ≤ 45% of equity per coin, ≤ 2 coins | No all-in bets |
| Buys per day | 2 | Overtrading is just fees |
| Stops | can tighten, never loosen | It can't talk itself out of a stop |
| Withdrawals | impossible | The API key has no withdrawal permission |

When a guardrail changes an order, the next briefing tells Claude why, so it learns the rules. `agent.trend_rule = "advise"` makes the trend rule advice instead of law, and `"off"` removes it.

**Prompt injection.** News and web pages are untrusted text, and a crafted headline could try to steer the model. The prompt tells Claude to treat news as data, but the real defence is the guardrails: whatever the model is persuaded to *say*, the code won't trade outside those limits. A test covers exactly this attack (`test_prompt_injection_cannot_escape_the_universe`).

## Its life

| Health | Life left (equity above the death floor) | Effect |
|---|---|---|
| THRIVING / HEALTHY | ≥ 75% | normal risk |
| WOUNDED | ≥ 40% | half risk |
| CRITICAL | < 40% | quarter risk |
| **DEAD** | ≤ death floor, or can't afford the ₱50 minimum order | sells everything, stops forever |

- **₱2,500 is a hard cap.** Fund the Coins.ph account with exactly ₱2,500, and it can't lose more than that. The alternative is `budget_php = 5000` with `death_floor_php = 2500` ("start with ₱5,000, die after losing half"). That gives the same maximum loss in theory, but a fast crash could overshoot the floor.
- **Equity** = pesos plus coins at what they'd sell for right now, after fees.
- **It pays for its own thinking.** Each Claude call is priced from the API's token counts, converted at `usd_php`, and deducted from its pesos. The actual API bill goes to your Anthropic account. The bot books it against its own life, so **trading losses + thinking together never exceed ₱2,500**. Pesos it "spent" on thinking stay in your Coins.ph account to reimburse you.
- **Daily thinking budget** (default ₱6). Once it's spent, the brain sleeps until midnight Manila time. Stops and trend exits keep working.
- **Death is permanent.** A dead name never trades again; a new life needs a new `name`.

## What thinking costs

Every thought sends about 2,500 tokens of briefing plus prompt. Output (Claude's reasoning plus its decision) varies with `effort`. These are **estimates**; the bot logs the real cost of every thought and you can see it in `status`.

| Model (`agent.model`) | Price per M tokens (in / out) | Est. per thought | Est. per month (10-30 thoughts) |
|---|---|---|---|
| `claude-opus-5-5` (default) | $4 / $20 | ₱2-4, up to ~₱12 with 2 web searches | ₱30-120 |
| `claude-sonnet-5-5` | $2 / $10 | ₱1-2, up to ~₱7 with searches | ₱15-60 |
| `claude-haiku-5-5` | $0.10 / $0.50 | under ₱0.20, ~₱1.5 with searches | under ₱15 |

How many thoughts it has depends on events (a trend turning, a stop, an 8% move) and on how often Claude chooses to review. The ₱6 daily cap means at most ₱180 a month.

**The economics:** in the data above, the trend rule at the bot's position sizes averaged roughly 1-2% a month over 2022-2026, about ₱25-50 on ₱2,500. Opus's thinking costs about the same or more. So with Opus, Claude has to *add* value (avoid false starts, dodge bad news) just to pay for itself. Sonnet halves the cost, and Haiku makes it nearly free but reasons less well. Your call: it's one line in the config.

Server-side fallback is on (`fallbacks="default"`): if the model declines a request, the API retries it on its default fallback model.

---

## Where it can trade (researched Nov 2026)

| Platform | Retail API? | Verdict |
|---|---|---|
| **Coins.ph** | **Yes**, self-serve: one key per account (Security → API Management). BSP-licensed since 2017; peso pairs | **Target** |
| Gotrade | **Yes, but gated.** The Gotrade Trading API covers US stocks: create/list/get/cancel orders, account balance, market and fractional (dollar-amount) orders. A Gotrade rep must enable API access on your account; then you generate a 1-year token in the Gotrade Global app. The page never mentions the Philippines, and the API lives on `api.charlie.gotrade.admin.tr8.tech` | **Second venue later.** Ask Gotrade support in-app to enable API access. If they do, an adapter is a contained addition |
| Invesko | No API found. Singapore entity (WECAP Ventures); US stocks through a partner broker; states it is not itself a broker | Can't integrate |
| PDAX | Institutional only | Not for retail |
| PSE brokers (COL, FirstMetroSec...) | No public API; PSE rules have restricted algorithmic trading | Not possible today |
| Binance, Bybit, OKX | Blocked by the SEC (2024-2025) | **Don't use** |

**Funding caveat:** in Oct 2026 the BSP restricted InstaPay/PESONet transfers into DCPay, the company behind Coins.ph's peso wallet. Coins.ph says its crypto license is intact. Make sure deposits and withdrawals work for you.

---

## Quick start

Requires Python 3.11+ and `pip install -r requirements.txt` (only the Claude brain needs it).

```bash
cd mortal-trader
cp config.example.toml config.toml
python3 -m unittest discover -s tests              # 94 tests, no network, no keys

export ANTHROPIC_API_KEY=...                       # from the Claude Console (separate from the Claude app)
python3 -m mortalbot paper                          # Claude brain, live prices, fake money
python3 -m mortalbot status --journal 5             # vital signs + its last 5 thoughts
```

The rules brain (`brain = "rules"`) trades the trend rule alone, needs no API key, and is what `backtest` uses. An LLM can't be honestly backtested: it may already know how history played out.

```bash
python3 -m mortalbot fetch --interval 1d --limit 4000 && python3 -m mortalbot backtest --csv data/BTCPHP-1d.csv
python3 research/trend_filters.py data              # the strategy comparison above (fetch all 4 coins first)
```

Live, with Coins.ph keys in the environment:

```bash
export COINS_API_KEY=... COINS_API_SECRET=...
python3 -m mortalbot paper --validate-orders        # Coins.ph checks every order; nothing is spent
python3 -m mortalbot live --confirm-real-money
```

Exit codes: `0` ok, `1` exchange/network error, `2` refused (config, ledger mismatch, missing confirmation), `3` the bot is dead.

## Running it 24/7

It has to run somewhere that never sleeps: your phone can't host it, and a sleeping laptop leaves stop-losses unwatched. A small Linux server is enough (1 vCPU / 512 MB), e.g. AWS Lightsail or EC2 with a static IP, so you can bind the Coins.ph key to that IP. `deploy/mortalbot.service` is a ready systemd unit: it restarts after crashes, and it does **not** restart a dead bot (exit code 3). Set `upkeep_php_per_day` to the server's cost so the bot pays its own rent.

## Path to real money

1. **Paper trade for at least 2 weeks** with the Claude brain, and read its journal (`status --journal 20`). Does its reasoning make sense? What does a thought really cost?
2. **Create a restricted Coins.ph API key:** trading enabled, **withdrawals disabled**, bound to the server's IP.
3. **Run `paper --validate-orders` for a day.** This proves the key, signing and order format against the real exchange without spending anything.
4. **Set `execution.fee_rate`** (your Coins.ph fee tier) **and `agent.usd_php`** (today's rate).
5. **Fund the account with only what the bot may lose**, then `live --confirm-real-money`.

## What is verified and what isn't

| Verified | Not yet verified |
|---|---|
| **Live (Nov 2026):** Coins.ph server time, market rules for BTC/ETH/SOL/XRP (₱50 minimum order), bulk prices, hourly candles | A real Claude API call (no key in the build environment) |
| **Live:** all 4 news feeds parse; a full paper tick on live data produced a correct briefing | Real thinking costs; the estimates above are back-of-envelope |
| **Live:** the order-test endpoint exists and rejects unsigned calls in the format the bot handles | Signed calls: balances, placing orders, the shape of a real fill (needs your keys; use `--validate-orders`) |
| Guardrails, including a prompt-injection attempt | Real slippage on Coins.ph's order book |
| Request shape for the Claude API (model, strict tool, web search, fallbacks, effort, caching) | Whether the news sites mind automated reading over the long term |
| Refusal, pause/resume and "forgot to decide" handling; API errors never kill the bot | |
| Wake-ups, daily budget, thinking charged to life, death by overthinking | |
| Ledger, stop-loss, take-profit, permanent death, restart persistence, malicious-feed rejection | |

**Known gap: stops live in the bot, not on the exchange.** If the server is down, stop-losses don't fire. Coins.ph supports exchange-side `STOP_LOSS` and `TAKE_PROFIT` orders; moving stops there is the next safety upgrade.

## Honest expectations

- **The trend rule is the evidence-backed part; Claude's judgement on top is the experiment.** There is no evidence that an LLM reading headlines beats the market. News is usually priced in within minutes, and this bot thinks in days. Treat it as an experiment with a hard ₱2,500 cap, not an income source.
- **Expect long stretches of doing nothing,** and losing streaks of small trades. Both are the strategy working, not failing.
- **Fees and thinking are a constant drain.** A round trip costs about 0.8%, and thinking ₱1-4 a day with Opus. The bot has to beat both just to stay alive.
- The guardrails make blowing up unlikely, not impossible: a fast crash can gap through a stop.
- Taxes on crypto gains are your responsibility. Not financial advice.

---

## Layout

```
mortalbot/
  agent.py        the autonomous trader: reflexes, wake-ups, thoughts, journal
  brain.py        Claude API call, cost accounting, refusal/pause handling
  prompts.py      system prompt (cache-stable) + per-thought briefing + decision tool schema
  guardrails.py   code-enforced limits on every order Claude proposes
  body.py         ledger, execution, stop/target reflexes, metabolism, death (shared by both brains)
  news.py         RSS/Atom headlines, sanitised, untrusted
  market.py       candles -> compact market table
  strategy.py     the daily trend rule (regime) + its mechanical version
  bot.py          rules brain: the trend rule alone, for backtests / no-API mode
  life.py         health bands, death conditions, obituary
  state.py        persistent multi-coin ledger + journal; dead bots stay dead
  runner.py       paper/live loops, retries, account reconciliation
  exchange/       paper broker, Coins.ph client, validate-only shadow broker
research/         trend_filters.py: the strategy comparison on real Coins.ph data
deploy/           systemd unit
tests/            94 tests
```

## Sources

- Coins.ph: [API key setup](https://support.coins.ph/hc/en-us/articles/11620395767193) · [REST API reference](https://coins-docs.github.io/rest-api) · [OpenAPI spec (apis.io)](https://apis.io/apis/coinsph/coinsph-spot-api/)
- Gotrade: [Gotrade Global Trading API](https://www.heygotrade.com/api-key/) · [Gotrade + Alpaca Broker API](https://alpaca.markets/blog/gotrade-launches-fractional-shares-investing-in-us-stocks-to-150-countries-with-alpaca-broker-api/)
- Invesko: [Wazzup Pilipinas, Aug 2024](https://www.wazzuppilipinas.com/2024/08/filipino-founded-invesko-attracts-12000.html) · [App Store listing](https://apps.apple.com/ph/app/ztock/id6444173974)
- PDAX: [API reference (institutional)](https://doc.restapi.pdax.ph/)
- PSE: [Consultation on algorithmic trading via DMA (CN-2023-0043)](https://documents.pse.com.ph/CircularOPSPDF/CN-2023-0043.pdf)
- SEC enforcement: [Yahoo Finance](https://finance.yahoo.com/news/philippine-sec-cuts-access-offshore-130201212.html) · [CCN](https://www.ccn.com/education/crypto/philippines-sec-bans-crypto-exchanges-funds-risk-explained/)
- Licensing: [BitPinas: licensed VASPs](https://bitpinas.com/feature/list-licensed-virtual-currency-exchanges-philippines/) · [Cointelegraph, Oct 2026](https://cointelegraph.com/news/coinsph-says-bsp-license-intact-amid-claims-of-potential-revocation)

*Not financial advice. This is software that can lose all of the money you give it. That's the point of it.*
