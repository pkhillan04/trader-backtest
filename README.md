# trader-backtest

A backtesting engine for intraday breakout setups, built to answer one question
before risking anything: **do these setups actually make money after costs?**

The short answer, on my data: **no.** Across 28 variations tested on 2 years of
5-minute bars, none showed an edge that survived realistic costs, and buying and
holding SPY beat every one of them. One marginal candidate survived and is now
being forward-tested on data the backtest never saw.

That negative result is the point of the project. I built this because I was
about to paper trade a set of day-trading rules, and I wanted evidence before
spending months finding out the slow way.

## Why it exists

The rules being tested come from a publicly published day-trading skill pack.
The pack ships a "Time Machine" that replays one stock over the last ~60 days —
roughly 30 trades, which is nowhere near enough to tell an edge from noise.

This tests dozens of stocks over years instead, so a verdict rests on thousands
of trades, and it is deliberately harsher than the original on every axis where
a backtest usually flatters itself.

## What it tests

28 variations: each setup, each stop rule, each profit target.

| setup | entry | stops tried |
| --- | --- | --- |
| `orb15`, `orb30` | first 5-min close above the 15- or 30-minute opening range high | range midpoint, range low |
| `orb15_bull_flag`, `orb15_breakout_20d`, `orb15_reclaim_ma20`, `orb15_oversold_bounce` | same ORB, only on stocks flagged by the prior night's daily scanner | range midpoint, range low |
| `level20` | first 5-min close above the 20-day closing high | 1% below, 1.5x ATR below |

Long only. Entries 9:45–15:30 ET, one trigger per day per symbol, flat by 15:55.

## Where it tries hard to not fool itself

This is most of the actual work in the repo.

- **Fills you could really get.** Entry fills at the *next* bar's open, not the
  close you only see after it prints. Slippage both ways (5 bps default).
- **Gaps fill at the gap.** A bar that opens through the stop fills at the open,
  not at the stop price. Stop and target inside the same bar counts as the stop.
- **Out-of-sample split.** A variation must make money in both the early 70% and
  the late 30% of the date range. Making money only in one regime doesn't pass.
- **A luck check.** 1000-sample bootstrap resampling *whole days* (not individual
  trades, which would ignore that trades on the same day are correlated). The 95%
  interval on the average has to stay above zero.
- **A trade floor.** Under 100 trades gets `TOO FEW TRADES`, not a verdict.
- **Portfolio simulation.** Results run through the pack's real limits — 1% risk
  per trade, 3 trades a day, 10% cash buffer, 75% single-position cap, whole
  shares, no margin — benchmarked against buy-and-hold SPY over the same window.

Verdicts: `WORTH PAPER TRADING`, `NOT PROVEN`, `LOSES MONEY`, `TOO FEW TRADES`.

## Results

Nothing earned `WORTH PAPER TRADING` on the full run. Tight intraday stops make
every basis point of slippage matter: on the pack's own Tesla example, adding
realistic costs moved the average trade from −0.08R to −0.17R.

One lead did emerge: `orb15_oversold_bounce` with a range-midpoint stop and a 1R
target, restricted to days where the stock opens within 1% of the prior close —
+0.111R per trade after costs over 244 trades, +0.090R in the late period.

I don't trust it, and the repo says so. It was found by scanning many filters,
and with 28 variations plus filter combinations, something clearing a 95% bar is
roughly what chance alone predicts. So `forward.py` scores it on sessions the
backtest never touched, using the same engine and the same costs, and will not
call it anything until it has 100 forward trades. If the running average sits at
or below zero by then, the original result was luck.

## Running it

Python 3 and **no third-party dependencies** — standard library only. Market data
comes from Alpaca, free with a paper account.

```bash
export APCA_API_KEY_ID="your key id"
export APCA_API_SECRET_KEY="your secret"

python3 backtest.py check-keys              # one small request: do the keys work?
python3 backtest.py run --quick             # 20 stocks, 1 year — a few minutes
python3 backtest.py run                     # 80 stocks, 2 years — the real test
python3 backtest.py run --slippage-bps 10   # harsher costs: does anything survive?
```

Output lands in `trader/backtest/`: `report.html` (scorecard with equity curves
rendered as inline SVG), `summary.json`, and `trades.csv.gz` with every simulated
trade. Bars are cached gzipped and topped up on later runs.

Forward test:

```bash
python3 forward.py evening   # after 4:20 PM ET — score new sessions, build tomorrow's watchlist
python3 forward.py morning   # after 9:50 AM ET — which watchlist names opened within 1%
python3 forward.py status    # running scorecard
```

Tests (offline, no keys or network needed):

```bash
python3 -m unittest discover -s . -p "test_*.py"
```

18 tests. They cover fill logic, gap handling, same-bar stop/target ambiguity,
and cost application, and they run the engine against synthetic price series —
random walks, where nothing should pass, and series with a planted edge, where it
has to be found. There's also a mock Alpaca server covering pagination, gzip,
auth failure, caching, and SIP→IEX feed fallback.

## Known limits

- **Survivorship bias.** The default symbol list was chosen today. Long-only
  setups on stocks that survived to be picked today look better than they would
  have in real time.
- **Multiple comparisons.** 28 variations, plus filters. Treat any single winner
  as a hypothesis, not a finding.
- **A backtest describes the past.** It shows whether something *has* worked, not
  whether it *will*.
- Most people who day trade lose money, and nothing here changes that. This is
  tooling and education, not financial advice. It never places an order.

## Credits

The setups, risk limits, and entry/exit rules come from a publicly published
day-trader skill pack; this repo tests them and does not claim to have invented
them. The backtesting engine, forward test, portfolio simulation, statistics, and
test suite are my own work.

Built with Claude as a coding partner: I designed what it needed to prove, set
the standards of evidence, and validated the output — including reproducing the
pack's own published Tesla figures (33 trades, 13 targets, 15 stops, 5 neither)
as a correctness check before trusting any new number it produced.

## License

MIT
