# MortalBot

An autonomous crypto trader whose budget is its life. You give it ₱5,000 and walk away. **Claude decides everything**: what to buy, when, how much, and when to sell. It reads market data and the news, and keeps a journal to learn from its own mistakes. Its thinking costs money, and that comes out of its life too. When the money runs out, it **dies**: it sells what it holds, writes its obituary, and never trades again.

It trades on **Coins.ph** (Philippines), in Python.

> **Status: v0.2.** The bot logic, guardrails, paper trading and backtests are tested (88 tests). A dry run on **live Coins.ph market data and live news** worked end to end. Two things haven't touched the real services: the Claude call itself (tested against a simulated API) and order placement (tested against canned responses). Follow [the path to real money](#path-to-real-money) before funding it.

---

## How it works

```
every 30 seconds (free, no AI)              when it matters (costs money)
------------------------------              -----------------------------
prices from Coins.ph                        Claude reads: vitals, positions, market table,
stop-loss / take-profit fire                journal + lessons, ~25 news headlines,
upkeep charged, vital signs checked         and may run 2 web searches
death if equity is gone                            |
wake the brain if:                          decision: BUY / SELL / ADJUST_STOP / hold
  - its chosen check time arrived                  |
  - a coin moved 5%+                        guardrails (code) shrink or block orders
  - a stop or target just fired                    |
                                            body executes; journal entry; Claude picks
                                            when to think next (1h-24h)
```

**Claude proposes, code disposes.** Claude has full freedom *inside* hard limits that only code enforces and Claude cannot change:

| Guardrail | Default | Why |
|---|---|---|
| Tradable coins | BTC, ETH, SOL, XRP (PHP pairs) | A headline saying "buy SCAMCOIN" can't be obeyed |
| Mandatory stop-loss | 2-15% below entry | No position can bleed unattended |
| Risk per trade | a stop-out costs ≤ 2% of equity | One bad call can't kill it |
| Per coin / positions | ≤ 50% of equity per coin, ≤ 3 coins | No all-in bets |
| Buys per day | 4 | Overtrading is just fees |
| Stops | can tighten, never loosen | It can't talk itself out of a stop |
| Losing streak | 3 losses → 12 hours of rest | Cools off tilt |
| Withdrawals | impossible | The API key has no withdrawal permission |

When a guardrail changes an order, the next briefing tells Claude why, so it learns the rules.

**Prompt injection.** News and web pages are untrusted text, and a crafted headline could try to steer the model. The prompt tells Claude to treat news as data, but the real defence is the guardrails above: whatever the model is persuaded to *say*, the code won't trade outside those limits. A test covers exactly this attack (`test_prompt_injection_cannot_escape_the_universe`).

## Its life

| Health | Equity vs. ₱5,000 | Effect |
|---|---|---|
| THRIVING / HEALTHY | ≥ 75% | normal risk |
| WOUNDED | ≥ 40% | half risk |
| CRITICAL | < 40% | quarter risk |
| **DEAD** | ≤ death floor, or can't afford the smallest order | sells everything, stops forever |

- **Equity** = pesos plus coins at what they'd sell for right now, after fees.
- **It pays for its own thinking.** Each Claude call is priced from the API's token counts, converted at `usd_php`, and deducted from its pesos. The actual API bill goes to your Anthropic account. The bot books it against its own life, so **trading losses + thinking together never exceed ₱5,000**. Pesos it "spent" on thinking stay in your Coins.ph account to reimburse you.
- **Daily thinking budget** (default ₱25). Once it's spent, the brain sleeps until midnight Manila time. Stop-losses keep working.
- **Death is permanent.** A dead name never trades again; a new life needs a new `name`.

## What thinking costs

Every thought sends about 2,500 tokens of briefing plus prompt. Output (Claude's reasoning plus its decision) varies with `effort`. These are **estimates**; the bot logs the real cost of every thought and you can see it in `status`.

| Model (`agent.model`) | Price per M tokens (in / out) | Est. per thought | Thoughts per ₱25 day |
|---|---|---|---|
| `claude-opus-5-5` (default) | $4 / $20 | ₱2-4, up to ~₱12 with 2 web searches | ~3-10 |
| `claude-sonnet-5-5` | $2 / $10 | ₱1-2, up to ~₱7 with searches | ~5-20 |
| `claude-haiku-5-5` | $0.10 / $0.50 | under ₱0.20, ~₱1.5 with searches | 15-100+ |

At the default cap, thinking can cost up to **₱750/month, 15% of its life**. That's likely more than a ₱5,000 account can earn. Cheaper models or fewer searches (`web_search_max_uses = 0`) stretch its life; Opus reasons best. Your call, and you can change it in config at any time.

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
python3 -m unittest discover -s tests              # 88 tests, no network, no keys

export ANTHROPIC_API_KEY=...                       # from the Claude Console (separate from the Claude app)
python3 -m mortalbot paper                          # Claude brain, live prices, fake money
python3 -m mortalbot status --journal 5             # vital signs + its last 5 thoughts
```

The rules brain (`brain = "rules"`) needs no API key, and it's what `backtest` uses. An LLM can't be honestly backtested: it may already know how history played out.

```bash
python3 -m mortalbot backtest --synthetic mixed
python3 -m mortalbot fetch --limit 3000 && python3 -m mortalbot backtest --csv data/BTCPHP-1h.csv
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

- **There is no evidence that an LLM reading headlines beats the market.** News is usually priced in within minutes, and this bot thinks in hours. Treat it as an experiment with a hard ₱5,000 cap, not an income source.
- **Fees and thinking are a constant drain.** A round trip costs about 0.8%, and thinking up to ~₱25/day. The bot has to beat both just to stay alive.
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
  bot.py          rules brain (EMA crossover) for backtests / no-API mode
  life.py         health bands, death conditions, obituary
  state.py        persistent multi-coin ledger + journal; dead bots stay dead
  runner.py       paper/live loops, retries, account reconciliation
  exchange/       paper broker, Coins.ph client, validate-only shadow broker
deploy/           systemd unit
tests/            88 tests
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
