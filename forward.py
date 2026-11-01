#!/usr/bin/env python3
"""forward.py: forward-test the one lead the backtest found, on days the backtest never saw.

The lead (from the 2-year, 80-stock backtest): the opening-range breakout on stocks the daily scanner
tagged oversold_bounce the night before, ONLY when the stock opens within 1% of yesterday's close.
Stop at the opening-range midpoint, target 1R, flat by 15:55. Backtest result: +0.111R a trade after
costs (+0.090R in the late period), 244 trades. It was found by looking at many filters, so it may
be luck. This script checks it on new days with exactly the same rules and costs.

    python3 forward.py evening     # after 4:20 PM ET: score today, log it, print tomorrow's watchlist
    python3 forward.py morning     # after 9:50 AM ET: which watchlist stocks opened within 1%
    python3 forward.py status      # the running scorecard

Run "evening" once a day after the close (missed days are caught up next time). It never places an
order. To practise placing the trades yourself on your Alpaca paper account, "morning" prints the
pack's own commands for each stock that qualifies.
"""

import argparse
import csv
import datetime
import json
import os
import random
import statistics
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import backtest as bt  # noqa: E402

CFG_ID = "orb15_oversold_bounce|or-mid|1R"
BENCH_R = 0.111            # the backtest's average R for this exact rule
BENCH_LATE_R = 0.090       # its late (held-out) period
BENCH_N = 244
MAX_GAP_PCT = 1.0
DEFAULT_SINCE = "2026-10-26"   # the first session after the backtest's data ended
GOAL_TRADES = 100
FIELDS = ["date", "symbol", "gap_pct", "entry_time", "entry", "entry_fill", "stop", "target",
          "risk_per_share", "outcome", "exit_time", "exit_fill", "R_after_costs"]


def out_dir(a):
    d = os.path.join(a.trader_dir, "forward")
    os.makedirs(d, exist_ok=True)
    return d


def read_state(path):
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return {}


def write_json(path, doc):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1)
    os.replace(tmp, path)


def read_trades(path):
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    for r in rows:
        r["R_after_costs"] = float(r["R_after_costs"])
    return rows


def params(a):
    ns = types.SimpleNamespace(config=os.path.join(a.trader_dir, "config.json"), budget=None, risk_pct=None,
                               max_trades_per_day=None, no_entries_after=None, fill="next_open",
                               slippage_bps=5.0, min_price=5.0)
    return bt.resolve_params(ns)


def the_cfg():
    cfg = [c for c in bt.build_configs(["orb15_oversold_bounce"], [1.0]) if c["id"] == CFG_ID]
    assert len(cfg) == 1
    return cfg


def universe(a):
    if a.symbols:
        return [s.strip().upper() for s in a.symbols.split(",") if s.strip()]
    return list(bt.DEFAULT_UNIVERSE)


def load_all(a, end, log):
    """SPY and every stock's 5-minute bars through `end`, from the backtest's cache, topped up."""
    warm = (datetime.date.fromisoformat(end) - datetime.timedelta(days=130)).isoformat()
    api = None if a.offline else bt.Alpaca(feed="sip", quiet=a.quiet)
    out = {}
    for sym in ["SPY"] + universe(a):
        try:
            if a.offline:
                bars = bt.load_offline(a.offline, sym)
            else:
                bars, _ = bt.get_bars(api, a.cache, sym, warm, end, lambda m: None)
        except bt.BTError as e:
            if sym == "SPY":
                raise
            log("  %-6s skipped: %s" % (sym, e))
            continue
        if bars:
            out[sym] = bt.sessionize(bars)
    if "SPY" not in out:
        raise bt.BTError("no SPY bars")
    return out


def score_days(days_by_sym, dates, P):
    """The backtest's exact rule on each of these dates -> (trades, signals_skipped_for_gap)."""
    cfgs = the_cfg()
    spy = bt.spy_context(days_by_sym["SPY"])
    trades, gapped = [], []
    for sym, days in days_by_sym.items():
        if sym == "SPY":
            continue
        for d in dates:
            if d not in days:
                continue
            trs, _ = bt.replay_symbol(sym, days, cfgs, P, spy, d, d)
            for t in trs:
                if t["gap"] is not None and abs(t["gap"]) < MAX_GAP_PCT:
                    trades.append(t)
                else:
                    gapped.append((sym, d, t["gap"]))
    trades.sort(key=lambda t: (t["date"], t["k"], t["symbol"]))
    return trades, gapped


def watchlist(days_by_sym, as_of, P):
    """Stocks the daily scanner rule tags oversold_bounce after the close of `as_of`."""
    out = []
    for sym, days in days_by_sym.items():
        if sym == "SPY":
            continue
        hist = [h for h in bt.daily_history(days) if h[0] <= as_of]
        if not hist or hist[-1][0] != as_of:
            continue
        closes = [h[2] for h in hist[-63:]]
        if len(closes) < 30 or closes[-1] < P["min_price"]:
            continue
        if "oversold_bounce" in bt.daily_tags(closes):
            c = closes[-1]
            out.append({"symbol": sym, "close": round(c, 2), "rsi": round(bt.rsi(closes), 1),
                        "open_low": round(c * (1 - MAX_GAP_PCT / 100), 2),
                        "open_high": round(c * (1 + MAX_GAP_PCT / 100), 2)})
    return sorted(out, key=lambda w: w["symbol"])


def scorecard(rows):
    if not rows:
        return None
    R = [r["R_after_costs"] for r in rows]
    s = {"n": len(R), "mean": statistics.mean(R), "win_rate": sum(1 for x in R if x > 0) / len(R),
         "total_R": sum(R), "days": len({r["date"] for r in rows})}
    if len(R) >= 20:
        rng = random.Random(19)
        per = {}
        for r in rows:
            per.setdefault(r["date"], []).append(r["R_after_costs"])
        groups = list(per.values())
        means = []
        for _ in range(2000):
            pick = [groups[rng.randrange(len(groups))] for _ in groups]
            flat = [x for g in pick for x in g]
            means.append(sum(flat) / len(flat))
        means.sort()
        s["lo"], s["hi"] = means[50], means[1949]
    return s


def print_scorecard(rows, a):
    s = scorecard(rows)
    print("")
    print("Forward test so far (new days only, same rules and costs as the backtest):")
    if not s:
        print("  no trades yet. Signals are rare: about one every two trading days across 80 stocks.")
        return
    risk = bt.DEFAULTS["max_loss_per_trade_usd"]
    print("  %d trades on %d days, win rate %.0f%%, average %+.3fR a trade (backtest: %+.3fR, late period %+.3fR)"
          % (s["n"], s["days"], 100 * s["win_rate"], s["mean"], BENCH_R, BENCH_LATE_R))
    print("  at $%.0f risk a trade that is %s total, %s a trade"
          % (risk, bt.money(s["total_R"] * risk), "%+.2f" % (s["mean"] * risk)))
    if "lo" in s:
        print("  95%% range of the average so far: %+.3fR to %+.3fR" % (s["lo"], s["hi"]))
    if s["n"] < GOAL_TRADES:
        print("  %d of %d trades: too early to judge. Keep going." % (s["n"], GOAL_TRADES))
    elif s.get("lo", -1) > 0:
        print("  VERDICT: holding up on new data (the whole 95%% range is above zero).")
    elif s["mean"] <= 0:
        print("  VERDICT: not holding up. After %d new trades the average is at or below zero: the backtest "
              "result was most likely luck." % s["n"])
    else:
        print("  VERDICT: positive but not clear of zero yet. Keep going, or call it unproven.")


def cmd_evening(a):
    log = (lambda m: None) if a.quiet else print
    d_out = out_dir(a)
    state_path = os.path.join(d_out, "state.json")
    trades_path = os.path.join(d_out, "trades.csv")
    state = read_state(state_path)
    since = a.since or state.get("since") or DEFAULT_SINCE
    end = a.date or bt.last_complete_day()
    now_et = bt.to_eastern(datetime.datetime.now(datetime.timezone.utc).timestamp())
    if not a.date and end < now_et.date().isoformat() and now_et.weekday() < 5:
        log("Note: it is %s ET. Today's session is scored after 4:20 PM (the free data plan is 15 minutes "
            "behind). Scoring through %s for now." % (now_et.strftime("%H:%M"), end))
    log("Updating 5-minute bars through %s for SPY and %d stocks (uses the backtest's cache)..."
        % (end, len(universe(a))))
    data = load_all(a, end, log)
    P = params(a)
    scored = set(state.get("scored", []))
    spy_days = data["SPY"]
    todo = sorted(d for d in spy_days if since <= d <= end and d not in scored
                  and all(b is not None for b in spy_days[d]))
    half = sorted(d for d in spy_days if since <= d <= end and d not in scored
                  and not all(b is not None for b in spy_days[d]))
    new_trades, gapped = score_days(data, todo, P)
    write_header = not os.path.isfile(trades_path)
    with open(trades_path, "a", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        if write_header:
            w.writerow(FIELDS)
        for t in new_trades:
            w.writerow([t["date"], t["symbol"], round(t["gap"], 2), t["entry_time"], t["entry"],
                        round(t["e_fill"], 4), t["stop"], t["target"], round(t["risk"], 4), t["outcome"],
                        t["exit_time"], round(t["x_fill"], 4), round(t["R"], 4)])
    state["since"] = since
    state["scored"] = sorted(scored | set(todo) | set(half))
    state["last_run"] = now_et.strftime("%Y-%m-%d %H:%M ET")
    if todo:
        print("")
        print("Scored %d new session(s): %s" % (len(todo), ", ".join(todo) if len(todo) <= 5
                                                 else "%s to %s" % (todo[0], todo[-1])))
        if new_trades:
            for t in new_trades:
                print("  %s %-5s gap %+.2f%%  in %s at %.2f (stop %.2f, target %.2f) -> %s at %s  %+.2fR"
                      % (t["date"], t["symbol"], t["gap"], t["entry_time"], t["e_fill"], t["stop"], t["target"],
                         t["outcome"], t["exit_time"], t["R"]))
        else:
            print("  no qualifying trades.")
        for sym, d, g in sorted(gapped, key=lambda x: (x[1], x[0])):
            print("  %s %-5s triggered but skipped: opened %+.2f%% from yesterday's close (rule: within 1%%)"
                  % (d, sym, g if g is not None else float("nan")))
    elif not half:
        print("")
        print("Nothing new to score (last scored: %s)." % (max(scored) if scored else "none yet"))
    if half:
        print("Skipped half day(s) or sessions with missing data, as the backtest does: %s" % ", ".join(half))
    # tomorrow's watchlist, from the last full session we have
    full = sorted(d for d in spy_days if d <= end and all(b is not None for b in spy_days[d]))
    if full:
        as_of = full[-1]
        wl = watchlist(data, as_of, P)
        state["watchlist"] = {"after_close_of": as_of, "stocks": wl}
        print("")
        print("Watchlist for the next session (tagged oversold_bounce after the close of %s):" % as_of)
        if not wl:
            print("  none. No trade possible next session.")
        for x in wl:
            print("  %-5s closed %.2f (RSI %.1f). Qualifies only if it opens between %.2f and %.2f."
                  % (x["symbol"], x["close"], x["rsi"], x["open_low"], x["open_high"]))
    write_json(state_path, state)
    print_scorecard(read_trades(trades_path), a)
    print("Log: %s" % trades_path)
    return 0


def cmd_morning(a):
    d_out = out_dir(a)
    state = read_state(os.path.join(d_out, "state.json"))
    wl = (state.get("watchlist") or {}).get("stocks")
    if wl is None:
        print("No watchlist yet: run  python3 forward.py evening  after a market close first.")
        return 1
    as_of = state["watchlist"]["after_close_of"]
    if not wl:
        print("The watchlist from %s is empty: no trade today." % as_of)
        return 0
    now_et = bt.to_eastern(datetime.datetime.now(datetime.timezone.utc).timestamp())
    today = a.date or now_et.date().isoformat()
    if today <= as_of:
        print("The watchlist is for the session after %s. Run this on that morning." % as_of)
        return 1
    if not a.date and not a.offline and now_et.hour * 60 + now_et.minute < 9 * 60 + 50:
        print("It is %s ET. The 9:30 bar shows up on the free data plan at about 9:50 AM ET; run this then."
              % now_et.strftime("%H:%M"))
        return 1
    api = None if a.offline else bt.Alpaca(feed="sip", quiet=True)
    print("Watchlist from the close of %s, checked on %s:" % (as_of, today))
    keep = []
    for x in wl:
        try:
            if a.offline:
                bars = bt.load_offline(a.offline, x["symbol"])
            else:
                bars = api.bars(x["symbol"], today, today)
        except bt.BTError as e:
            print("  %-5s could not read today's bars: %s" % (x["symbol"], e))
            continue
        day = bt.sessionize(bars).get(today)
        if not day or day[0] is None:
            print("  %-5s no 9:30 bar yet (or not trading today)" % x["symbol"])
            continue
        gap = (day[0][0] / x["close"] - 1) * 100
        ok = abs(gap) < MAX_GAP_PCT
        print("  %-5s opened %.2f (%+.2f%% vs %.2f)  %s" % (x["symbol"], day[0][0], gap, x["close"],
                                                            "QUALIFIES" if ok else "skip: gap too big"))
        if ok:
            keep.append(x["symbol"])
    if keep:
        print("")
        print("To place these as paper trades yourself, with the day-trader pack (from the pack's folder):")
        for s in keep:
            print("  python3 trader-entry/entry.py watch %s --until 15:30" % s)
        print("  then, when one triggers:  python3 trader-exit/exit.py plan --stop or-mid --target 1R --write")
        print("  then /trader-budget, /trader-risk and /trader-broker as usual (paper account).")
        print("The evening run scores these the same way whether or not you trade them.")
    return 0


def cmd_status(a):
    d_out = out_dir(a)
    state = read_state(os.path.join(d_out, "state.json"))
    rows = read_trades(os.path.join(d_out, "trades.csv"))
    print("Forward test of %s, since %s. Last run: %s." % (CFG_ID, state.get("since", DEFAULT_SINCE),
                                                          state.get("last_run", "never")))
    print_scorecard(rows, a)
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="forward.py", description=__doc__.split("\n\n")[0])
    p.add_argument("--trader-dir", default="trader", help="the pack's trader/ folder (default ./trader)")
    p.add_argument("--cache", default=os.path.join("trader", "backtest", "cache"))
    p.add_argument("--symbols", help="comma list (default: the backtest's 80 stocks; keep it the same)")
    p.add_argument("--offline", help="saved bars instead of Alpaca (for tests)")
    p.add_argument("--quiet", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("evening", help="score new sessions and print the next watchlist")
    sp.add_argument("--since", help="first session to score (default %s)" % DEFAULT_SINCE)
    sp.add_argument("--date", help="score through this date instead of the latest finished session")
    sp = sub.add_parser("morning", help="check the watchlist against today's open")
    sp.add_argument("--date", help="pretend it is this date (YYYY-MM-DD)")
    sub.add_parser("status", help="the running scorecard")
    a = p.parse_args(argv)
    try:
        return {"evening": cmd_evening, "morning": cmd_morning, "status": cmd_status}[a.cmd](a)
    except bt.BTError as e:
        print("error: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
