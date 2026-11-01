---
name: trader-backtest
description: >-
  Setup Backtester. Tests every setup in the day-trader pack (opening-range
  breakouts, the scanner's daily setups, 20-day-high breakouts) on years of
  real 5-minute bars from the user's Alpaca account, on a basket of liquid
  stocks, with realistic costs: next-bar-open entries, slippage, stops that
  gap, targets as limit orders, flat by the close. Splits the data into an
  early and a late period, puts a 95% range on every result, simulates the
  account under the pack's own limits next to buying SPY, and writes a
  plain-English scorecard with a verdict per variation. It never places an
  order. Use when the user says "backtest", "does this setup work", "which
  setups make money", "test before paper trading", "is ORB profitable",
  "check the setups on past data", or before paper trading anything from
  /trader-scanner and /trader-entry.
argument-hint: "[run|fetch|check-keys] [--quick] [--symbols A,B] [--years N] [--setups orb15,orb30]"
---

# trader-backtest

The Setup Backtester answers one question before any paper trading: has this
setup made money after costs, on many stocks over a long time, in a way that
is unlikely to be luck? The Time Machine looks at one stock's last 60 days;
this looks at dozens of stocks over years, so a setup is judged on thousands
of trades instead of thirty. It never places an order. Most people who day
trade lose money, and a good backtest does not change that.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/backtest.py" check-keys               # one small request: do the keys work?
python3 "${CLAUDE_SKILL_DIR}/backtest.py" run --quick              # 20 stocks, 1 year: a first look in minutes
python3 "${CLAUDE_SKILL_DIR}/backtest.py" run                      # 80 stocks, 2 years: the real test
python3 "${CLAUDE_SKILL_DIR}/backtest.py" run --symbols AAPL,TSLA,NVDA --years 3
python3 "${CLAUDE_SKILL_DIR}/backtest.py" run --setups orb15,orb30 --targets 1,1.5,2,3
python3 "${CLAUDE_SKILL_DIR}/backtest.py" run --slippage-bps 10    # harsher costs: does the edge survive?
python3 "${CLAUDE_SKILL_DIR}/backtest.py" fetch                    # download only (cached for later runs)
python3 -m unittest discover -s "${CLAUDE_SKILL_DIR}" -p "test_*.py"   # offline tests
```

Other flags: `--start/--end YYYY-MM-DD`, `--feed sip|iex`, `--fill
next_open|close`, `--budget`, `--risk-pct`, `--max-trades-per-day`,
`--no-entries-after HH:MM`, `--min-price`, `--max-symbols N`, `--out DIR`,
`--cache DIR`, `--offline DIR` (saved bars, for testing), `--quiet`.

## Keys

Alpaca's market data is free with the paper account. The keys live in
environment variables, never in the chat and never in a file in the project:
`APCA_API_KEY_ID` and `APCA_API_SECRET_KEY` (or `ALPACA_API_KEY` and
`ALPACA_SECRET_KEY`, the names Alpaca's MCP server uses). If they are missing,
tell the user how to set them in their own terminal and wait. Never ask the
user to paste a key into the chat. Run `check-keys` first.

## What it reads and writes

| file | what |
| --- | --- |
| Alpaca `/v2/stocks/bars` | 5-minute bars, split-adjusted, SIP feed (falls back to IEX if the account cannot query SIP) |
| `trader/config.json` | the pack's limits: budget, max loss per trade, max daily loss, trades a day, cash buffer, position cap, entry window, flat by. Optional |
| `trader/backtest/cache/` | downloaded bars, gzipped, reused and topped up on later runs |
| `trader/backtest/report.html` | the scorecard: every variation, its verdict, account curves vs SPY, filters |
| `trader/backtest/summary.json` | every number in the report |
| `trader/backtest/trades.csv.gz` | every simulated trade, for a spreadsheet |

## What it tests

Each setup x each stop x each target (1R and 2R by default) = 28 variations.

| setup | entry (trader-entry's rule) | stops tried |
| --- | --- | --- |
| `orb15`, `orb30` | first 5-minute close above the 15- or 30-minute opening range high | range midpoint, range low |
| `orb15_bull_flag`, `orb15_breakout_20d`, `orb15_reclaim_ma20`, `orb15_oversold_bounce` | the same ORB, only on stocks the daily scanner flagged the night before | range midpoint, range low |
| `level20` | first 5-minute close above the 20-day closing high | 1% below, 1.5x ATR below |

Every trade follows the pack: entries 9:45 to 15:30, one trigger a day, stop
rounded down to the cent, stops tighter than 0.05% refused, target at R times
the risk, flat by 15:55. Long side only.

**Costs and fills, on purpose harsher than the Time Machine:** the entry fills
at the NEXT bar's open (you cannot buy at a close you only see after it
prints), plus slippage each way (5 basis points by default). A bar that opens
through the stop fills at the open, not the stop. A stop and a target inside
one bar count as the stop. Targets are limit orders (no slippage). Half days
and sessions with missing bars are skipped.

**The account:** trading only that variation under the config's limits (1% of
the account at risk per trade from $50 / $5,000, three trades a day, a 10%
cash buffer, 75% max in one stock, whole shares, no margin), earliest trigger
first, next to buying SPY at the first day's open and holding.

## The verdict

| verdict | means |
| --- | --- |
| WORTH PAPER TRADING | made money after costs in BOTH the early 70% and the late 30% of days, and the 95% range of the average (resampling whole days) stays above zero, over at least 100 trades |
| NOT PROVEN | positive overall but failed one of those checks |
| LOSES MONEY | lost money on average after costs |
| TOO FEW TRADES | under 100 trades |

## Steps

1. Check the keys: `backtest.py check-keys`. If it fails, explain the fix in
   one line (env variables, paper keys) and stop.
2. Start with `run --quick` so the user sees results in minutes, then offer the
   full `run`. The full download is 80 stocks x 2 years of 5-minute bars, a few
   minutes the first time and cached after that. Run it in the background and
   say so.
3. Read `trader/backtest/summary.json` and tell the user, in plain words: how
   many variations passed, the best ones with their average R per trade after
   costs, the early vs late numbers, and how the account did next to SPY.
   Offer to open `report.html`.
4. If nothing passed, say it plainly: on this data none of these setups showed
   an edge after costs, and paper trading them is unlikely to change that.
   That is a useful result, not a failure.
5. If something passed, suggest paper trading ONLY that exact variation (setup,
   stop, target), and a harsher re-run (`--slippage-bps 10`) to see if it holds.
6. For any filter in the report, only mention one that helps in BOTH periods.

## How to talk about results

- Say "made money in this test", never "makes money" or "works". A backtest
  shows the past.
- Always give the number of variations tested. With 28 variations, about one
  can pass a 95% check by chance alone.
- Mention the stock list's survivorship bias when results look strong: the
  default list was picked today, and long-only setups on stocks that went up
  look better than they would have in real time.
- Never tune settings until something passes and then call it proven. A
  setting picked by looking at the results must be confirmed on new data or
  in paper trading.

## Forward test (forward.py)

The 2-year backtest found one lead: `orb15_oversold_bounce`, range-midpoint
stop, 1R target, ONLY when the stock opens within 1% of yesterday's close
(+0.111R a trade after costs on 244 trades, +0.090R in the late period). It
was found by looking at many filters, so it may be luck. `forward.py` checks it
on days the backtest never saw, with the same engine, rules and costs.

```bash
python3 "${CLAUDE_SKILL_DIR}/forward.py" evening   # after 4:20 PM ET: score new sessions, print tomorrow's watchlist
python3 "${CLAUDE_SKILL_DIR}/forward.py" morning   # after 9:50 AM ET: which watchlist stocks opened within 1%
python3 "${CLAUDE_SKILL_DIR}/forward.py" status    # the running scorecard
```

- `evening` scores every finished session since 2026-10-26 that it has not
  scored yet (missed days are caught up), appends to
  `trader/forward/trades.csv`, and saves the next session's watchlist in
  `trader/forward/state.json`. It reuses the backtest's bar cache.
- `morning` checks the watchlist against today's open and prints the pack's
  own commands (`trader-entry`, `trader-exit`, then budget, risk, broker) for
  the user to place paper trades by hand if they want the practice.
- The verdict needs at least 100 trades (roughly 6 to 9 months). Compare the
  running average with +0.11R. If it sits at or below zero after 100 trades,
  say plainly that the backtest result was most likely luck.
- Keep the stock list the same as the backtest's (the default).

## What it never does

It never places an order, never sees or prints the keys, never calls a past
result a promise, and never loosens costs to make a setup pass.

## Safety

- Education and tooling, not financial advice.
- Nothing in this pack places an order except trader-broker, and only with the
  user's explicit yes for that exact order. This skill only reads past prices.
- Broker keys live in environment variables or the Claude Desktop config, never in the chat.
