#!/usr/bin/env python3
"""trader-backtest: test the day-trader pack's setups on years of real 5-minute bars.

Standard library only. Pulls 5-minute bars from Alpaca's market-data API (free
with a paper account), replays every setup in the pack on every stock and every
day, charges realistic costs, and writes a plain-English scorecard.

    python3 backtest.py check-keys                 # are the Alpaca keys set and working?
    python3 backtest.py fetch                      # download (and cache) the bars
    python3 backtest.py run                        # backtest every setup -> trader/backtest/
    python3 backtest.py run --quick                # 20 stocks, 1 year: a fast first look
    python3 backtest.py run --symbols AAPL,TSLA,NVDA --years 1
    python3 backtest.py run --setups orb15,orb30 --targets 1,2,3

Keys come from environment variables, never from the chat or a file in the repo:
APCA_API_KEY_ID and APCA_API_SECRET_KEY (or ALPACA_API_KEY and ALPACA_SECRET_KEY).

It never places an order. It reads past prices and does arithmetic. A setup
that made money in the past can lose money in the future.
"""

import argparse
import csv
import datetime
import gzip
import json
import math
import os
import random
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

VERSION = "1.0"
OPEN_MIN = 9 * 60 + 30            # 9:30 AM New York, in minutes
SESSION_BARS = 78                 # 5-minute bars from 9:30 to 16:00
INTERVAL = 300                    # seconds in a 5-minute bar
DATA_URL = "https://data.alpaca.markets/v2/stocks/bars"
MIN_GAP_SECONDS = 0.32            # stay under Alpaca's free 200 calls a minute

# A liquid, well-known default basket. Chosen today, so it carries survivorship
# bias (these names survived and many went up): the report says so.
DEFAULT_UNIVERSE = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO", "AMD", "NFLX",
    "COST", "JPM", "V", "MA", "UNH", "XOM", "LLY", "WMT", "HD", "BAC",
    "KO", "PEP", "ORCL", "CRM", "ADBE", "INTC", "CSCO", "QCOM", "MU", "AMAT",
    "LRCX", "PANW", "CRWD", "SNOW", "PLTR", "SHOP", "UBER", "ABNB", "PYPL", "COIN",
    "MSTR", "SMCI", "ARM", "MRVL", "DELL", "ANET", "NOW", "IBM", "DIS", "NKE",
    "SBUX", "MCD", "BA", "CAT", "GE", "GS", "MS", "C", "WFC", "CVX",
    "OXY", "PFE", "MRK", "ABBV", "T", "VZ", "F", "GM", "RIVN", "BABA",
    "SOFI", "HOOD", "RBLX", "DKNG", "ROKU", "AFRM", "UPST", "MARA", "TGT", "LULU",
]
QUICK_UNIVERSE = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AMD", "NFLX", "AVGO",
                  "JPM", "XOM", "LLY", "WMT", "BAC", "PLTR", "COIN", "UBER", "MU", "BA"]

# The pack's setups. "orb" = opening-range breakout (trader-entry --setup orb).
# "level" = first 5-minute close above the 20-day closing high (trader-entry
# --setup level with the scanner's 20-day high). A tag limits the ORB to stocks
# the daily scanner flagged the night before (trader-scanner daily).
SETUPS = {
    "orb15": {"kind": "orb", "or": 15, "tag": None, "label": "Opening-range breakout, 15-min range"},
    "orb30": {"kind": "orb", "or": 30, "tag": None, "label": "Opening-range breakout, 30-min range"},
    "orb15_bull_flag": {"kind": "orb", "or": 15, "tag": "bull_flag",
                        "label": "ORB 15, only stocks flagged bull_flag the night before"},
    "orb15_breakout_20d": {"kind": "orb", "or": 15, "tag": "breakout_20d",
                           "label": "ORB 15, only stocks flagged breakout_20d the night before"},
    "orb15_reclaim_ma20": {"kind": "orb", "or": 15, "tag": "reclaim_ma20",
                           "label": "ORB 15, only stocks flagged reclaim_ma20 the night before"},
    "orb15_oversold_bounce": {"kind": "orb", "or": 15, "tag": "oversold_bounce",
                              "label": "ORB 15, only stocks flagged oversold_bounce the night before"},
    "level20": {"kind": "level", "or": 15, "tag": None,
                "label": "First 5-min close above the 20-day closing high"},
}
STOPS = {"orb": ["or-mid", "or-low"], "level": ["pct:1", "atr:1.5"]}

DEFAULTS = {
    "budget": 5000.0, "max_loss_per_trade_usd": 50.0, "max_daily_loss_usd": 150.0,
    "max_trades_per_day": 3, "cash_buffer_pct": 10.0, "max_position_pct": 75.0,
    "no_entries_before": "09:45", "no_entries_after": "15:30", "flat_by": "15:55",
}

# Filters the report checks on every setup (does the setup do better when...?).
FILTERS = [
    ("market_uptrend", "SPY closed above its 50-day average yesterday",
     lambda t: t["spy_trend"] is True, lambda t: t["spy_trend"] is not None),
    ("market_downtrend", "SPY closed below its 50-day average yesterday",
     lambda t: t["spy_trend"] is False, lambda t: t["spy_trend"] is not None),
    ("spy_green", "SPY is up on the day at the moment of entry",
     lambda t: t["spy_up"] is True, lambda t: t["spy_up"] is not None),
    ("spy_red", "SPY is down on the day at the moment of entry",
     lambda t: t["spy_up"] is False, lambda t: t["spy_up"] is not None),
    ("volume_1_5x", "trigger bar volume at least 1.5x the opening range's average",
     lambda t: t["rvol"] is not None and t["rvol"] >= 1.5, lambda t: t["rvol"] is not None),
    ("gap_up_1pct", "stock opened at least 1% above yesterday's close",
     lambda t: t["gap"] is not None and t["gap"] >= 1.0, lambda t: t["gap"] is not None),
    ("no_big_gap", "stock opened within 1% of yesterday's close",
     lambda t: t["gap"] is not None and abs(t["gap"]) < 1.0, lambda t: t["gap"] is not None),
]


class BTError(Exception):
    pass


# ---------------------------------------------------------------- clock (US Eastern, no tz database needed)

def _nth_sunday(year, month, n):
    d = datetime.date(year, month, 1)
    d += datetime.timedelta(days=(6 - d.weekday()) % 7)
    return d + datetime.timedelta(weeks=n - 1)


def eastern_offset(utc_naive):
    """-4 in daylight time (2nd Sunday of March to 1st Sunday of November), else -5."""
    y = utc_naive.year
    start = datetime.datetime.combine(_nth_sunday(y, 3, 2), datetime.time(7))
    end = datetime.datetime.combine(_nth_sunday(y, 11, 1), datetime.time(6))
    return -4 if start <= utc_naive < end else -5


def to_eastern(ts):
    utc = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).replace(tzinfo=None)
    return utc + datetime.timedelta(hours=eastern_offset(utc))


def hhmm(text, what):
    try:
        h, m = str(text).split(":")
        v = int(h) * 60 + int(m)
    except ValueError:
        raise BTError("%s must look like 15:30, got %r" % (what, text))
    if not 0 <= v < 24 * 60:
        raise BTError("%s must be a clock time, got %r" % (what, text))
    return v


def slot_closing_at(minute):
    """Index of the 5-minute bar that CLOSES at this New York minute (10:15 -> the 10:10 bar)."""
    return (minute - OPEN_MIN) // 5 - 1


def clock12(minute):
    h, m = divmod(minute, 60)
    return "%d:%02d %s" % (h % 12 or 12, m, "AM" if h < 12 else "PM")


BAR_CLOSE_LABEL = [clock12(OPEN_MIN + 5 * (k + 1)) for k in range(SESSION_BARS)]   # shared strings


def parse_iso_utc(text):
    """Alpaca's '2024-01-03T14:30:00Z' -> epoch seconds."""
    d = datetime.datetime.strptime(text[:19], "%Y-%m-%dT%H:%M:%S")
    return int(d.replace(tzinfo=datetime.timezone.utc).timestamp())


# ---------------------------------------------------------------- data: Alpaca

def alpaca_keys():
    key = os.environ.get("APCA_API_KEY_ID") or os.environ.get("ALPACA_API_KEY")
    secret = os.environ.get("APCA_API_SECRET_KEY") or os.environ.get("ALPACA_SECRET_KEY")
    if not key or not secret:
        raise BTError(
            "no Alpaca keys found. Set them as environment variables in your terminal (never in the chat):\n"
            "  export APCA_API_KEY_ID=\"your key id\"\n"
            "  export APCA_API_SECRET_KEY=\"your secret\"\n"
            "Find them in your Alpaca dashboard (paper account) under API Keys.")
    return key, secret


def api_end(day):
    """A date-only end would stop at midnight UTC and drop that whole day, which the cache would then never
    fetch. Ask for the end of the day instead, but never later than 16 minutes ago: the free plan cannot
    query the latest 15 minutes of SIP data."""
    if len(day) != 10:
        return day
    eod = datetime.datetime.strptime(day, "%Y-%m-%d").replace(hour=23, minute=59, second=59,
                                                              tzinfo=datetime.timezone.utc)
    latest = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=16)
    return min(eod, latest).strftime("%Y-%m-%dT%H:%M:%SZ")


class Alpaca:
    def __init__(self, feed="sip", quiet=False):
        self.key, self.secret = alpaca_keys()
        self.feed = feed
        self.quiet = quiet
        self._last = 0.0
        self.calls = 0

    def _get(self, params):
        url = DATA_URL + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={
            "APCA-API-KEY-ID": self.key, "APCA-API-SECRET-KEY": self.secret,
            "Accept": "application/json", "Accept-Encoding": "gzip",
            "User-Agent": "trader-backtest/" + VERSION})
        for attempt in range(6):
            wait = MIN_GAP_SECONDS - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()
            self.calls += 1
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    raw = resp.read()
                    if resp.headers.get("Content-Encoding") == "gzip":
                        raw = gzip.decompress(raw)
                    return json.loads(raw.decode("utf-8"))
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "replace")[:300]
                if e.code == 429 or e.code >= 500:
                    time.sleep(min(60, 2 ** attempt * 2))
                    continue
                if e.code in (401, 403) and "subscription" not in body.lower():
                    raise BTError("Alpaca refused the keys (HTTP %d): %s. Check that they are your PAPER "
                                  "account's keys and that both are set." % (e.code, body.strip()))
                raise BTError("Alpaca HTTP %d: %s" % (e.code, body.strip()), ) from None
            except urllib.error.URLError as e:
                if attempt < 5:
                    time.sleep(2 ** attempt)
                    continue
                raise BTError("could not reach Alpaca: %s" % e.reason)
        raise BTError("Alpaca kept rate-limiting or failing; try again in a few minutes")

    def bars(self, symbol, start, end, timeframe="5Min"):
        """Every bar for one symbol, start..end (YYYY-MM-DD, both days included), as [ts, o, h, l, c, v]."""
        out, token = [], None
        end_param = api_end(end)
        while True:
            params = {"symbols": symbol, "timeframe": timeframe, "start": start, "end": end_param,
                      "limit": 10000, "adjustment": "split", "feed": self.feed, "sort": "asc"}
            if token:
                params["page_token"] = token
            try:
                doc = self._get(params)
            except BTError as e:
                if self.feed == "sip" and "subscription" in str(e).lower():
                    if not self.quiet:
                        print("  note: this account cannot query SIP history; switching to the IEX feed. "
                              "IEX is one exchange (a few % of all volume), so volume-based filters are rougher.")
                    self.feed = "iex"
                    out, token = [], None
                    continue
                raise
            for b in (doc.get("bars") or {}).get(symbol, []) or []:
                out.append([parse_iso_utc(b["t"]), b["o"], b["h"], b["l"], b["c"], b.get("v", 0)])
            token = doc.get("next_page_token")
            if not token:
                return out


# ---------------------------------------------------------------- cache

def regular_session_only(bars):
    keep = []
    for b in bars:
        ts = int(b[0])
        if ts % INTERVAL:
            continue
        loc = to_eastern(ts)
        m = loc.hour * 60 + loc.minute - OPEN_MIN
        if 0 <= m < SESSION_BARS * 5:
            keep.append(b)
    return keep


def cache_path(cache_dir, feed, symbol):
    return os.path.join(cache_dir, feed, symbol.replace("/", "_") + "_5Min.json.gz")


def load_cache(path):
    if not os.path.isfile(path):
        return None
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def save_cache(path, doc):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with gzip.open(tmp, "wt", encoding="utf-8") as fh:
        json.dump(doc, fh, separators=(",", ":"))
    os.replace(tmp, path)


def last_complete_day(now_ts=None):
    """The latest New York date whose regular session is fully over and past the free plan's 15-minute
    delay: today after 16:20 ET, otherwise yesterday. (Weekends and holidays simply have no bars.)"""
    loc = to_eastern(now_ts if now_ts is not None else time.time())
    if loc.hour * 60 + loc.minute >= 16 * 60 + 20:
        return loc.date().isoformat()
    return (loc.date() - datetime.timedelta(days=1)).isoformat()


def get_bars(api, cache_dir, symbol, start, end, log):
    """Bars for start..end, from the cache when it covers them, topping it up otherwise."""
    end = min(end, last_complete_day())          # never cache a session that is still trading
    feed = api.feed if api else "sip"
    for f in ([feed] if api else ["sip", "iex"]):
        doc = load_cache(cache_path(cache_dir, f, symbol))
        if doc and doc.get("first") <= start and doc.get("last") >= end:
            return doc["bars"], f
    if api is None:
        raise BTError("no cached bars for %s covering %s..%s and no Alpaca connection" % (symbol, start, end))
    path = cache_path(cache_dir, api.feed, symbol)
    doc = load_cache(path)
    if doc and doc.get("first") <= start and doc.get("last") < end and doc["bars"]:
        # Re-read the last cached session too: if its prices changed, a split happened since the
        # cache was made, so the old bars are on a different scale and the whole history is fetched again.
        last_day = to_eastern(doc["bars"][-1][0]).date().isoformat()
        new = regular_session_only(api.bars(symbol, last_day, end))
        old = {b[0]: b for b in doc["bars"]}
        overlap = [(old[b[0]][4], b[4]) for b in new if b[0] in old]
        if overlap and any(abs(o / n - 1) > 0.005 for o, n in overlap if n):
            log("  %-6s prices changed since the cache was made (a split?): downloading it again" % symbol)
            doc = None
        else:
            for b in new:
                old[b[0]] = b
            doc["bars"] = [old[t] for t in sorted(old)]
            doc["last"] = end
    if doc is None or not (doc.get("first") <= start and doc.get("last") >= end):
        doc = {"symbol": symbol, "feed": api.feed, "adjustment": "split", "first": start, "last": end,
               "bars": regular_session_only(api.bars(symbol, start, end))}
    save_cache(cache_path(cache_dir, api.feed, symbol), doc)
    log("  %-6s %6d bars (%s feed)" % (symbol, len(doc["bars"]), api.feed))
    return doc["bars"], api.feed


def load_offline(path, symbol):
    """A saved Yahoo chart JSON (the pack's fixtures) or a cache file, for testing without the network."""
    cands = [path] if os.path.isfile(path) else [
        os.path.join(path, symbol + ext) for ext in (".json", ".json.gz", "_5Min.json.gz")]
    for p in cands:
        if not os.path.isfile(p):
            continue
        opener = gzip.open if p.endswith(".gz") else open
        with opener(p, "rt", encoding="utf-8") as fh:
            doc = json.load(fh)
        if "chart" in doc:
            r = doc["chart"]["result"][0]
            q = r["indicators"]["quote"][0]
            out = []
            for i, ts in enumerate(r["timestamp"]):
                row = [q[k][i] for k in ("open", "high", "low", "close")]
                if ts is None or None in row:
                    continue
                out.append([int(ts)] + row + [q["volume"][i] or 0])
            return regular_session_only(out)
        if "bars" in doc:
            return regular_session_only(doc["bars"])
    raise BTError("no offline bars for %s in %s" % (symbol, path))


# ---------------------------------------------------------------- sessions and daily history

def sessionize(bars):
    """{date: [78 slots of (o, h, l, c, v) or None]}, New York time."""
    days = {}
    for b in bars:
        ts = int(b[0])
        if ts % INTERVAL:
            continue
        loc = to_eastern(ts)
        m = loc.hour * 60 + loc.minute - OPEN_MIN
        if 0 <= m < SESSION_BARS * 5:
            days.setdefault(loc.date().isoformat(), [None] * SESSION_BARS)[m // 5] = (
                float(b[1]), float(b[2]), float(b[3]), float(b[4]), float(b[5] or 0))
    return days


def daily_history(days):
    """[(date, open, close, volume)] for every session that has its 9:30 bar, oldest first."""
    out = []
    for d in sorted(days):
        S = days[d]
        bars = [b for b in S if b]
        if S[0] is None or not bars:
            continue
        out.append((d, S[0][0], bars[-1][3], sum(b[4] for b in bars)))
    return out


def rsi(closes, n=14):
    """Wilder's RSI, the same math as trader-scanner."""
    if len(closes) < n + 1:
        return None
    g = [max(0.0, closes[i] - closes[i - 1]) for i in range(1, len(closes))]
    lo = [max(0.0, closes[i - 1] - closes[i]) for i in range(1, len(closes))]
    ag, al = sum(g[:n]) / n, sum(lo[:n]) / n
    for i in range(n, len(g)):
        ag = (ag * (n - 1) + g[i]) / n
        al = (al * (n - 1) + lo[i]) / n
    return 100 - 100 / (1 + ag / al) if al else 100.0


def daily_tags(c):
    """trader-scanner's daily setups on closes (oldest first, >= 30), as of last night."""
    if len(c) < 30:
        return set()
    last = c[-1]
    hi20 = max(c[-21:-1])
    ma20 = sum(c[-20:]) / 20.0
    ma20_prev = sum(c[-21:-1]) / 20.0
    run = c[-6] / c[-16] - 1
    rng5 = (max(c[-5:]) - min(c[-5:])) / last
    r, r_prev = rsi(c), rsi(c[:-1])
    tags = set()
    if last > hi20:
        tags.add("breakout_20d")
    if run > 0.10 and rng5 < 0.05:
        tags.add("bull_flag")
    if c[-2] < ma20_prev and last > ma20:
        tags.add("reclaim_ma20")
    if r is not None and r < 30:
        tags.add("oversold")
    if r_prev is not None and r_prev < 30 and last > c[-2]:
        tags.add("oversold_bounce")
    return tags


# ---------------------------------------------------------------- the trade rules (trader-entry / trader-exit)

def cents_down(x):
    """A long stop rounds away from the entry: down, to the cent (trader-exit)."""
    return math.floor(round(x * 100, 4)) / 100.0


def opening_range(D, n):
    return max(b[1] for b in D[:n]), min(b[2] for b in D[:n])


def atr(D, k, n=14):
    """Simple average of the last n true ranges on the session's bars up to bar k."""
    trs = []
    for i in range(k + 1):
        o, h, l, c, v = D[i]
        trs.append(h - l if i == 0 else max(h - l, abs(h - D[i - 1][3]), abs(l - D[i - 1][3])))
    trs = trs[-n:]
    return sum(trs) / len(trs)


def make_stop(rule, oh, ol, entry, D, k):
    if rule == "or-mid":
        return cents_down((oh + ol) / 2)
    if rule == "or-low":
        return cents_down(ol)
    if rule.startswith("pct:"):
        return cents_down(entry * (1 - float(rule[4:]) / 100.0))
    if rule.startswith("atr:"):
        return cents_down(entry - float(rule[4:]) * atr(D, k))
    raise BTError("unknown stop rule %r" % rule)


def first_close_above(D, level, k0, k1):
    """trader-entry's trigger: the first completed bar from k0 to k1 that CLOSES above the level."""
    for k in range(k0, k1 + 1):
        if D[k][3] > level:
            return k
    return None


def play(D, k, entry, stop, target, last, fill="next_open", slip=0.0, gap_fills=True):
    """One trade, bar by bar after the trigger. A stop and a target inside the same bar count as the
    stop (conservative, like trader-timemachine). Returns None when there is no bar to fill on."""
    if fill == "close":
        e_raw = entry
    else:
        if k + 1 > last:
            return None
        e_raw = D[k + 1][0]                      # market order at the next bar's open
    start = k + 1
    outcome = x_raw = None
    xi = last
    for i in range(start, last + 1):
        o, h, l, c, v = D[i]
        if gap_fills:
            if o <= stop:                         # opened through the stop: it fills at the open
                outcome, x_raw, xi = "stop", o, i
                break
            if o >= target:                       # opened past the target: the limit fills at the open
                outcome, x_raw, xi = "target", o, i
                break
        if l <= stop:
            outcome, x_raw, xi = "stop", stop, i
            break
        if h >= target:
            outcome, x_raw, xi = "target", target, i
            break
    if outcome is None:
        outcome, x_raw = "close", D[last][3]      # flat by the close, win or lose
    e_fill = e_raw * (1 + slip)
    x_fill = x_raw if outcome == "target" else x_raw * (1 - slip)   # a target is a limit order
    return {"outcome": outcome, "exit_slot": xi, "e_raw": e_raw, "x_raw": x_raw,
            "e_fill": e_fill, "x_fill": x_fill}


# ---------------------------------------------------------------- per-symbol replay

def build_configs(setups, targets):
    cfgs = []
    for s in setups:
        if s not in SETUPS:
            raise BTError("unknown setup %r (have: %s)" % (s, ", ".join(SETUPS)))
        for stop in STOPS[SETUPS[s]["kind"]]:
            for t in targets:
                cfgs.append({"id": "%s|%s|%gR" % (s, stop, t), "setup": s, "stop": stop, "target_r": t,
                             "label": SETUPS[s]["label"], "kind": SETUPS[s]["kind"],
                             "or": SETUPS[s]["or"], "tag": SETUPS[s]["tag"]})
    return cfgs


def spy_context(spy_days):
    """{date: {"trend": bool|None, "open": float, "closes": [78 closes or None]}} for SPY."""
    hist = daily_history(spy_days)
    ctx = {}
    for i, (d, o, c, v) in enumerate(hist):
        prior = [x[2] for x in hist[max(0, i - 50):i]]
        trend = (prior[-1] > sum(prior) / 50.0) if len(prior) == 50 else None
        S = spy_days[d]
        ctx[d] = {"trend": trend, "open": o, "close": c,
                  "closes": [b[3] if b else None for b in S]}
    return ctx


def replay_symbol(sym, days, cfgs, P, spy, start, end):
    """Every config on every full session of one stock -> (trades, counts)."""
    hist = daily_history(days)
    idx = {d: i for i, (d, _, _, _) in enumerate(hist)}
    trades = []
    counts = {"sessions": 0, "skipped_partial": 0, "refused_tight": 0, "expired": 0}
    k_first_clock = slot_closing_at(P["no_entries_before"])
    k1 = slot_closing_at(P["no_entries_after"])
    last = slot_closing_at(P["flat_by"])
    for d in sorted(days):
        if d < start or d > end:
            continue
        D = days[d]
        if any(b is None for b in D):
            counts["skipped_partial"] += 1          # half days and gaps in the data
            continue
        i = idx.get(d)
        if i is None:
            continue
        prior_close = hist[i - 1][2] if i >= 1 else None
        if (prior_close if prior_close is not None else D[0][0]) < P["min_price"]:
            continue
        counts["sessions"] += 1
        closes = [x[2] for x in hist[max(0, i - 63):i]]
        tags = daily_tags(closes) if len(closes) >= 30 else None
        level20 = max(closes[-20:]) if len(closes) >= 20 else None
        gap = (D[0][0] / prior_close - 1) * 100 if prior_close else None
        sc = spy.get(d)
        or_cache = {}
        for ci, cfg in enumerate(cfgs):
            if cfg["tag"] and (tags is None or cfg["tag"] not in tags):
                continue
            n = cfg["or"] // 5
            if n not in or_cache:
                or_cache[n] = opening_range(D, n)
            oh, ol = or_cache[n]
            if cfg["kind"] == "orb":
                level = oh
            else:
                if level20 is None:
                    continue
                level = level20
            k0 = max(n, k_first_clock)
            k = first_close_above(D, level, k0, k1)
            if k is None:
                counts["expired"] += 1
                continue
            entry = round(D[k][3], 2)
            stop = make_stop(cfg["stop"], oh, ol, entry, D, k)
            risk = entry - stop
            if risk <= 0 or risk < entry * P["min_risk_pct"] / 100.0:
                counts["refused_tight"] += 1            # trader-exit refuses a stop inside the spread
                continue
            target = round(entry + cfg["target_r"] * risk, 2)
            res = play(D, k, entry, stop, target, last, P["fill"], P["slip"], P["gap_fills"])
            if res is None:
                continue
            or_vol = sum(b[4] for b in D[:n]) / n
            spy_up = None
            if sc and sc["closes"][k] is not None and sc["open"]:
                spy_up = sc["closes"][k] > sc["open"]
            trades.append({
                "cfg": ci, "symbol": sym, "date": d, "entry_time": BAR_CLOSE_LABEL[k],
                "k": k, "entry": entry, "e_fill": res["e_fill"], "stop": stop, "target": target,
                "risk": risk, "outcome": res["outcome"], "exit_time": BAR_CLOSE_LABEL[res["exit_slot"]],
                "x_fill": res["x_fill"],
                "R_gross": (res["x_raw"] - res["e_raw"]) / risk,
                "R": (res["x_fill"] - res["e_fill"]) / risk,
                "spy_trend": sc["trend"] if sc else None, "spy_up": spy_up,
                "rvol": (D[k][4] / or_vol) if or_vol > 0 else None, "gap": gap,
            })
    return trades, counts


# ---------------------------------------------------------------- statistics

def r_stats(trs):
    if not trs:
        return {"n": 0}
    R = [t["R"] for t in trs]
    wins = [r for r in R if r > 0]
    losses = [r for r in R if r <= 0]
    return {
        "n": len(R), "mean": statistics.mean(R), "mean_gross": statistics.mean(t["R_gross"] for t in trs),
        "win_rate": len(wins) / len(R), "avg_win": statistics.mean(wins) if wins else 0.0,
        "avg_loss": statistics.mean(losses) if losses else 0.0,
        "profit_factor": (sum(wins) / -sum(losses)) if losses and sum(losses) < 0 else None,
        "target": sum(1 for t in trs if t["outcome"] == "target"),
        "stop": sum(1 for t in trs if t["outcome"] == "stop"),
        "close": sum(1 for t in trs if t["outcome"] == "close"),
    }


def cluster_ci(trs, B=1000, seed=19):
    """95% interval for the average R per trade, resampling whole DAYS (trades on one day move together)."""
    per = {}
    for t in trs:
        a = per.setdefault(t["date"], [0.0, 0])
        a[0] += t["R"]
        a[1] += 1
    groups = list(per.values())
    if len(groups) < 20:
        return None, None
    rng = random.Random(seed)
    means = []
    G = len(groups)
    for _ in range(B):
        s = n = 0
        for _ in range(G):
            a = groups[rng.randrange(G)]
            s += a[0]
            n += a[1]
        means.append(s / n)
    means.sort()
    return means[int(0.025 * B)], means[int(0.975 * B) - 1]


def verdict(full, dev, hold, lo):
    if full["n"] < 100:
        return "TOO FEW TRADES", "Under 100 trades: not enough to tell skill from luck."
    if full["mean"] <= 0:
        return "LOSES MONEY", "Lost money on average after costs."
    if dev.get("n", 0) and hold.get("n", 0) and dev["mean"] > 0 and hold["mean"] > 0 and lo is not None and lo > 0:
        return "WORTH PAPER TRADING", ("Made money after costs, in both the early and the late period, "
                                       "and the result is unlikely to be luck alone.")
    if hold.get("n", 0) and hold["mean"] <= 0:
        return "NOT PROVEN", "Made money early on but not in the most recent (held-out) period."
    return "NOT PROVEN", "Slightly positive, but the range of likely results includes losing money."


def portfolio(trs, dates, P, spy):
    """Trade only this setup with the pack's limits: risk a fixed % of the account per trade, max trades
    a day, a cash buffer, a per-stock cap, no margin, whole shares. First triggers of the day go first."""
    by = {}
    for t in trs:
        by.setdefault(t["date"], []).append(t)
    eq = P["budget"]
    curve, taken, skipped = [], 0, 0
    for d in dates:
        cands = sorted(by.get(d, []), key=lambda t: (t["k"], -(t["rvol"] or 0), t["symbol"]))
        used = planned_loss = pnl = 0.0
        n_today = 0
        for t in cands:
            if n_today >= P["max_trades_per_day"]:
                skipped += 1
                continue
            risk_usd = eq * P["risk_pct"] / 100.0
            sh = int(risk_usd // t["risk"])
            sh = min(sh, int(eq * P["max_position_pct"] / 100.0 // t["e_fill"]),
                     int((eq * (1 - P["cash_buffer_pct"] / 100.0) - used) // t["e_fill"]))
            if sh < 1 or planned_loss + sh * t["risk"] > eq * P["daily_loss_pct"] / 100.0 + 0.01:
                skipped += 1
                continue
            used += sh * t["e_fill"]
            planned_loss += sh * t["risk"]
            pnl += sh * (t["x_fill"] - t["e_fill"])
            n_today += 1
            taken += 1
        eq += pnl
        curve.append((d, eq))
    peak, mdd = P["budget"], 0.0
    for _, v in curve:
        peak = max(peak, v)
        mdd = max(mdd, (peak - v) / peak if peak else 0)
    spy_first = next((spy[d]["open"] for d in dates if d in spy), None)
    bench = [(d, P["budget"] * spy[d]["close"] / spy_first) for d in dates if d in spy and spy_first]
    return {
        "final": eq, "return_pct": (eq / P["budget"] - 1) * 100, "max_drawdown_pct": mdd * 100,
        "trades_taken": taken, "signals_skipped": skipped, "curve": curve,
        "spy_return_pct": ((bench[-1][1] / P["budget"] - 1) * 100) if bench else None, "bench": bench,
    }


# ---------------------------------------------------------------- report

VERDICT_ORDER = {"WORTH PAPER TRADING": 0, "NOT PROVEN": 1, "LOSES MONEY": 2, "TOO FEW TRADES": 3}


def ranked(results):
    """Passing setups first, then the rest by average R per trade; too-few-trades rows last."""
    return sorted(results, key=lambda r: (VERDICT_ORDER.get(r["verdict"], 9), -(r["full"].get("mean") or -9)))


def money(x):
    return ("-$" if x < 0 else "$") + "{:,.0f}".format(abs(x))


def fr(x, digits=3):
    return "n/a" if x is None else ("%+." + str(digits) + "f") % x


def stop_words(rule):
    return {"or-mid": "range midpoint", "or-low": "range low"}.get(rule) or (
        rule.replace("pct:", "") + "% below" if rule.startswith("pct:") else rule.replace("atr:", "") + "x ATR below")


def svg_curves(series, width=720, height=260):
    """series: [(name, [(date, value)], css var)] -> inline SVG line chart, values normalized to 100."""
    pts = [v for _, s, _ in series for _, v in s]
    if not pts:
        return ""
    lo, hi = min(pts), max(pts)
    if hi == lo:
        hi = lo + 1
    pad = (hi - lo) * 0.08
    lo, hi = (max(0.0, lo - pad) if lo >= 0 else lo - pad), hi + pad
    L, R, T, B = 52, 12, 14, 26

    def xy(i, v, m):
        x = L + (width - L - R) * (i / max(1, m - 1))
        y = T + (height - T - B) * (1 - (v - lo) / (hi - lo))
        return "%.1f,%.1f" % (x, y)
    out = ['<svg viewBox="0 0 %d %d" role="img" aria-label="Account value over time" '
           'style="width:100%%;height:auto">' % (width, height)]
    for j in range(5):
        v = lo + (hi - lo) * j / 4
        y = T + (height - T - B) * (1 - j / 4)
        out.append('<line x1="%d" x2="%d" y1="%.1f" y2="%.1f" class="grid"/>' % (L, width - R, y, y))
        out.append('<text x="%d" y="%.1f" class="axis" text-anchor="end">%s</text>' % (L - 6, y + 4, money(v)))
    first = series[0][1]
    if first:
        out.append('<text x="%d" y="%d" class="axis">%s</text>' % (L, height - 6, first[0][0]))
        out.append('<text x="%d" y="%d" class="axis" text-anchor="end">%s</text>' % (width - R, height - 6, first[-1][0]))
    for name, s, color in series:
        if s:
            out.append('<polyline fill="none" stroke="var(%s)" stroke-width="2" points="%s"/>'
                       % (color, " ".join(xy(i, v, len(s)) for i, (_, v) in enumerate(s))))
    out.append("</svg>")
    return "".join(out)


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def write_report(path, summary, results, P):
    best = [r for r in results if r["verdict"] not in ("TOO FEW TRADES",)]
    best.sort(key=lambda r: r["full"].get("mean", -9), reverse=True)
    counts = {}
    for r in results:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    worth = [r for r in results if r["verdict"] == "WORTH PAPER TRADING"]
    if worth:
        bottom = ("%d of %d variations passed every check. Paper trade only those, with their exact stop and target, "
                  "and compare the paper results to this report." % (len(worth), len(results)))
    else:
        bottom = ("None of the %d variations passed every check. On this data, none of these setups showed a reliable "
                  "edge after costs. Paper trading them is unlikely to change that." % len(results))
    rows = []
    for r in ranked(results):
        f, pf = r["full"], r["portfolio"]
        cls = {"WORTH PAPER TRADING": "good", "LOSES MONEY": "bad", "NOT PROVEN": "meh"}.get(r["verdict"], "muted")
        rows.append(
            "<tr><td><b>%s</b><div class='sub'>stop: %s · target %gR</div></td><td>%s</td><td>%s</td>"
            "<td>%s</td><td><b>%s</b><div class='sub'>%s to %s</div></td><td>%s</td><td>%s</td>"
            "<td>%s<div class='sub'>SPY %s</div></td><td>%s</td><td><span class='badge %s'>%s</span></td></tr>" % (
                esc(r["cfg"]["setup"]), esc(stop_words(r["cfg"]["stop"])), r["cfg"]["target_r"],
                "{:,}".format(f.get("n", 0)),
                ("%.0f%%" % (100 * f["win_rate"])) if f.get("n") else "n/a",
                ("%+.2f / %+.2f" % (f["avg_win"], f["avg_loss"])) if f.get("n") else "n/a",
                fr(f.get("mean")), fr(r["ci"][0], 2), fr(r["ci"][1], 2),
                fr(r["dev"].get("mean")), fr(r["hold"].get("mean")),
                ("%+.1f%%" % pf["return_pct"]) if f.get("n") else "n/a",
                ("%+.1f%%" % pf["spy_return_pct"]) if pf.get("spy_return_pct") is not None else "n/a",
                ("%.1f%%" % pf["max_drawdown_pct"]) if f.get("n") else "n/a", cls, esc(r["verdict"])))
    charts = []
    for r in best[:3]:
        pf = r["portfolio"]
        charts.append("<h3>%s <span class='sub'>stop: %s · target %gR</span></h3>%s"
                      "<p class='legend'><span class='sw a'></span>this setup %s &nbsp; <span class='sw b'></span>"
                      "buy and hold SPY %s</p>" % (
                          esc(r["cfg"]["setup"]), esc(stop_words(r["cfg"]["stop"])), r["cfg"]["target_r"],
                          svg_curves([("setup", pf["curve"], "--accent"), ("SPY", pf["bench"], "--muted")]),
                          money(pf["final"]), money(pf["bench"][-1][1]) if pf["bench"] else "n/a"))
    frows = []
    for r in best[:6]:
        for f in r["filters"]:
            if f["n"] < 50:
                continue
            helps = (f["dev"] is not None and f["hold"] is not None and r["dev"].get("n") and r["hold"].get("n")
                     and f["dev"] > r["dev"]["mean"] and f["hold"] > r["hold"]["mean"] and f["hold"] > 0)
            frows.append("<tr><td>%s <span class='sub'>%s · %gR</span></td><td>%s</td><td>%s</td><td>%s</td>"
                         "<td>%s</td><td>%s</td></tr>" % (
                             esc(r["cfg"]["setup"]), esc(stop_words(r["cfg"]["stop"])), r["cfg"]["target_r"],
                             esc(f["label"]), "{:,}".format(f["n"]), fr(f["mean"]), fr(f["hold"]),
                             "<span class='badge good'>helps in both periods</span>" if helps else ""))
    html = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Setup Backtest Scorecard</title>
<style>
:root{--bg:#fbfaf7;--fg:#1d1d1b;--muted:#8a877f;--line:#e6e2d9;--card:#ffffff;--accent:#c4552b;
--good:#2f7d4f;--bad:#b23a3a;--meh:#a77a12}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){--bg:#171614;--fg:#ecebe6;--muted:#9a978f;
--line:#2d2b27;--card:#1f1e1b;--accent:#e2774c;--good:#5fbf86;--bad:#e06b6b;--meh:#d9a93a}}
:root[data-theme="dark"]{--bg:#171614;--fg:#ecebe6;--muted:#9a978f;--line:#2d2b27;--card:#1f1e1b;--accent:#e2774c;
--good:#5fbf86;--bad:#e06b6b;--meh:#d9a93a}
body{background:var(--bg);color:var(--fg);font:15px/1.55 -apple-system,Segoe UI,Inter,sans-serif;margin:0}
main{max-width:1100px;margin:0 auto;padding:28px 16px 60px}
h1{font-size:28px;margin:0 0 4px}h2{font-size:19px;margin:34px 0 10px}h3{font-size:15px;margin:22px 0 6px}
.sub{color:var(--muted);font-size:12.5px;font-weight:400}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 18px;margin:14px 0}
.bottom{border-left:4px solid var(--accent)}
.scroll{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:13.5px;min-width:760px}
th,td{text-align:left;padding:8px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:12px;color:var(--muted);font-weight:600}
.badge{display:inline-block;padding:2px 8px;border-radius:999px;font-size:11.5px;font-weight:600;
border:1px solid currentColor;white-space:nowrap}
.good{color:var(--good)}.bad{color:var(--bad)}.meh{color:var(--meh)}.muted{color:var(--muted)}
.grid{stroke:var(--line);stroke-width:1}.axis{fill:var(--muted);font-size:11px}
.legend{font-size:12.5px;color:var(--muted)}.sw{display:inline-block;width:14px;height:3px;vertical-align:middle;
margin-right:4px}.sw.a{background:var(--accent)}.sw.b{background:var(--muted)}
dl{display:grid;grid-template-columns:max-content 1fr;gap:6px 16px;margin:0}dt{font-weight:600}dd{margin:0}
ul{padding-left:20px}
</style></head><body><main>
<h1>Setup Backtest Scorecard</h1>
<div class="sub">%(period)s · %(nsym)d stocks · %(days)d trading days · costs: %(slip)s%% slippage each way,
entry at the next bar's open · generated %(when)s</div>
<div class="card bottom"><b>Bottom line.</b> %(bottom)s</div>
<h2>Every variation tested</h2>
<p class="sub">Setups that passed come first, then the rest by average result per trade after costs. "R" means multiples of the amount you risked:
+0.10R on a $50-risk trade is +$5 on average.</p>
<div class="card scroll"><table><thead><tr><th>Setup</th><th>Trades</th><th>Win rate</th><th>Avg win / loss (R)</th>
<th>Avg R per trade<br>(95%% range)</th><th>Early %(dev)s</th><th>Late %(hold)s</th><th>Account if traded</th>
<th>Worst drop</th><th>Verdict</th></tr></thead><tbody>%(rows)s</tbody></table></div>
<h2>Account value, best three variations vs buying SPY</h2>
<div class="card">%(charts)s<p class="sub">Starting %(budget)s, risking %(risk)s%% of the account per trade,
at most %(maxt)d trades a day, whole shares, no margin.</p></div>
<h2>Filters: does the setup work better under some conditions?</h2>
<p class="sub">Only trust a filter that helps in BOTH the early and the late period. With many filters, one
will look good by chance.</p>
<div class="card scroll"><table><thead><tr><th>Setup</th><th>Only when...</th><th>Trades</th><th>Avg R</th>
<th>Avg R, late period</th><th></th></tr></thead><tbody>%(frows)s</tbody></table></div>
<h2>How to read this</h2>
<div class="card"><dl>
<dt>Avg R per trade</dt><dd>The number that matters. Above zero = made money on average after costs. The 95%% range
shows how much of that could be luck: if it dips below zero, the setup may have no real edge.</dd>
<dt>Early / Late</dt><dd>The data split in two: the first 70%% of days and the last 30%%. A real edge should show up
in both. The late period was never used to pick anything, so it is the fairest test.</dd>
<dt>Account if traded</dt><dd>What the starting account would have become trading only that setup under the pack's
limits, next to just buying SPY on day one and holding it.</dd>
<dt>Worst drop</dt><dd>The biggest fall from a high point to a low point along the way.</dd>
<dt>Verdict</dt><dd>WORTH PAPER TRADING only when the setup made money after costs, in both periods, and the 95%%
range stays above zero.</dd></dl></div>
<h2>Limits of this test</h2>
<div class="card"><ul>
<li>%(ntested)d variations were tested. By chance alone, about 1 in 40 can pass a 95%% check, so treat a lone pass
with suspicion and confirm it with paper trading.</li>
<li>The stock list was chosen today. Stocks that crashed or were delisted are missing, which can flatter long-only results.</li>
<li>Fills are modelled, not real: next-bar open plus slippage, stops filled at the stop (or the open if it gapped through),
targets as limit orders. Real fills on fast breakouts can be worse.</li>
<li>Half days and sessions with missing bars were skipped. Prices are split-adjusted.</li>
<li>Past results do not predict future ones. This is research, not financial advice.</li>
</ul></div>
</main></body></html>"""
    fill = {
        "period": "%s to %s" % (summary["start"], summary["end"]), "nsym": summary["symbols_used"],
        "days": summary["trading_days"], "slip": "%g" % P["slippage_bps_display"], "when": summary["generated"],
        "bottom": esc(bottom), "rows": "".join(rows), "charts": "".join(charts) or "<p>No trades.</p>",
        "budget": money(P["budget"]), "risk": "%g" % P["risk_pct"], "maxt": P["max_trades_per_day"],
        "frows": "".join(frows) or "<tr><td colspan=6>Not enough trades to test filters.</td></tr>",
        "dev": "<div class='sub'>to %s</div>" % summary["split_date"],
        "hold": "<div class='sub'>from %s</div>" % summary["split_date"], "ntested": len(results)}
    html = html.replace("%%", "%")
    for k, v in fill.items():
        html = html.replace("%(" + k + ")s", str(v)).replace("%(" + k + ")d", str(v))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(html)


# ---------------------------------------------------------------- commands

def load_config(path):
    c = dict(DEFAULTS)
    if path and os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as fh:
                user = json.load(fh)
            for k in DEFAULTS:
                if user.get(k) is not None:
                    c[k] = user[k]
        except ValueError:
            raise BTError("%s is not valid JSON" % path)
    return c


def resolve_params(a):
    c = load_config(a.config)
    budget = float(a.budget or c["budget"])
    risk_pct = float(a.risk_pct) if a.risk_pct is not None else 100.0 * float(c["max_loss_per_trade_usd"]) / budget
    daily_pct = 100.0 * float(c["max_daily_loss_usd"]) / budget
    P = {
        "budget": budget, "risk_pct": risk_pct, "daily_loss_pct": daily_pct,
        "max_trades_per_day": int(a.max_trades_per_day or c["max_trades_per_day"]),
        "cash_buffer_pct": float(c["cash_buffer_pct"]), "max_position_pct": float(c["max_position_pct"]),
        "no_entries_before": hhmm(c["no_entries_before"], "no_entries_before"),
        "no_entries_after": hhmm(a.no_entries_after or c["no_entries_after"], "no_entries_after"),
        "flat_by": hhmm(c["flat_by"], "flat_by"),
        "fill": a.fill, "slip": a.slippage_bps / 10000.0, "slippage_bps_display": a.slippage_bps / 100.0,
        "gap_fills": True, "min_risk_pct": 0.05, "min_price": a.min_price,
    }
    if not slot_closing_at(P["no_entries_before"]) < slot_closing_at(P["no_entries_after"]) < \
            slot_closing_at(P["flat_by"]) <= SESSION_BARS - 1:
        raise BTError("the entry window and flat-by time do not fit in a session")
    return P


def resolve_dates(a):
    today = datetime.date.today()
    end = datetime.date.fromisoformat(a.end) if a.end else today - datetime.timedelta(days=1)
    if a.start:
        start = datetime.date.fromisoformat(a.start)
    else:
        start = end - datetime.timedelta(days=int(round(365.25 * a.years)))
    if start >= end:
        raise BTError("start must be before end")
    warm = start - datetime.timedelta(days=120)        # 50-day averages and 63-day RSI windows need history
    return start.isoformat(), end.isoformat(), warm.isoformat()


def resolve_symbols(a):
    if a.symbols:
        if os.path.isfile(a.symbols):
            with open(a.symbols, encoding="utf-8") as fh:
                syms = [s.strip().upper() for s in fh.read().replace(",", "\n").split() if s.strip()]
        else:
            syms = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]
    else:
        syms = list(QUICK_UNIVERSE if a.quick else DEFAULT_UNIVERSE)
    if a.max_symbols:
        syms = syms[:a.max_symbols]
    return [s for s in dict.fromkeys(syms) if s != "SPY"]


def say(a):
    return (lambda *x: None) if a.quiet else (lambda m: print(m, flush=True))


def cmd_check_keys(a):
    api = Alpaca(feed=a.feed, quiet=a.quiet)
    end = (datetime.date.today() - datetime.timedelta(days=3)).isoformat()
    start = (datetime.date.today() - datetime.timedelta(days=10)).isoformat()
    bars = api.bars("SPY", start, end, timeframe="1Day")
    print("Alpaca keys work. SPY: %d daily bars from %s to %s, feed %s." % (len(bars), start, end, api.feed))
    return 0


def iter_bars(a, symbols, warm, end, log, state):
    """Yield (symbol, bars) one stock at a time, so memory stays small however many stocks there are.
    SPY comes first. state["feed"] ends up as the feed actually used."""
    api = None if a.offline else Alpaca(feed=a.feed, quiet=a.quiet)
    t0 = time.time()
    for i, sym in enumerate(["SPY"] + symbols):
        try:
            if a.offline:
                bars, state["feed"] = load_offline(a.offline, sym), "offline"
            else:
                bars, state["feed"] = get_bars(api, a.cache, sym, warm, end, log)
        except BTError as e:
            if sym == "SPY":
                raise
            log("  %-6s skipped: %s" % (sym, e))
            continue
        if not bars:
            if sym == "SPY":
                raise BTError("no SPY bars: the market filters and the benchmark need them")
            log("  %-6s skipped: no bars (not trading in this period?)" % sym)
            continue
        if not a.offline and (i + 1) % 10 == 0:
            log("  ... %d of %d stocks done (%.0fs)" % (i + 1, len(symbols) + 1, time.time() - t0))
        yield sym, bars


def cmd_fetch(a):
    log = say(a)
    start, end, warm = resolve_dates(a)
    symbols = resolve_symbols(a)
    log("Downloading 5-minute bars, %s to %s, for SPY and %d stocks into %s" % (warm, end, len(symbols), a.cache))
    state = {"feed": None}
    n = sum(1 for _ in iter_bars(a, symbols, warm, end, log, state))
    log("Done: %d symbols cached (%s feed)." % (n, state["feed"]))
    return 0


def cmd_run(a):
    log = say(a)
    P = resolve_params(a)
    start, end, warm = resolve_dates(a)
    symbols = resolve_symbols(a)
    targets = [float(x) for x in a.targets.split(",")]
    setups = [s.strip() for s in a.setups.split(",")] if a.setups else list(SETUPS)
    cfgs = build_configs(setups, targets)
    log("Backtest: %d setups x stops x targets = %d variations, %d stocks, %s to %s"
        % (len(setups), len(cfgs), len(symbols), start, end))
    state = {"feed": None}
    stream = iter_bars(a, symbols, warm, end, log, state)
    _, spy_bars = next(stream)
    spy_days = sessionize(spy_bars)
    del spy_bars
    spy = spy_context(spy_days)
    dates = sorted(d for d in spy_days if start <= d <= end and all(b is not None for b in spy_days[d]))
    if len(dates) < 40:
        raise BTError("only %d full trading days between %s and %s: use a longer period" % (len(dates), start, end))
    split = dates[int(len(dates) * 0.7)]
    all_trades, totals = [], {"sessions": 0, "skipped_partial": 0, "refused_tight": 0, "expired": 0}
    log("Downloading (or reading the cache) and replaying every setup, one stock at a time...")
    for sym, bars in stream:
        days = sessionize(bars)
        del bars
        trs, cnt = replay_symbol(sym, days, cfgs, P, spy, start, end)
        all_trades.extend(trs)
        for k in totals:
            totals[k] += cnt[k]
    feed = state["feed"]
    results = []
    for ci, cfg in enumerate(cfgs):
        trs = [t for t in all_trades if t["cfg"] == ci]
        full = r_stats(trs)
        dev = r_stats([t for t in trs if t["date"] < split])
        hold = r_stats([t for t in trs if t["date"] >= split])
        lo, hi = cluster_ci(trs, seed=a.seed) if trs else (None, None)
        v, why = verdict(full, dev, hold, lo)
        filt = []
        for key, label, keep, known in FILTERS:
            sub = [t for t in trs if known(t) and keep(t)]
            sd = [t for t in sub if t["date"] < split]
            sh = [t for t in sub if t["date"] >= split]
            filt.append({"key": key, "label": label, "n": len(sub),
                         "mean": statistics.mean(t["R"] for t in sub) if sub else None,
                         "dev": statistics.mean(t["R"] for t in sd) if sd else None,
                         "hold": statistics.mean(t["R"] for t in sh) if sh else None})
        results.append({"cfg": cfg, "full": full, "dev": dev, "hold": hold, "ci": (lo, hi),
                        "verdict": v, "why": why, "filters": filt, "portfolio": portfolio(trs, dates, P, spy)})
    os.makedirs(a.out, exist_ok=True)
    summary = {
        "version": VERSION, "generated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "start": start, "end": end, "split_date": split, "trading_days": len(dates), "feed": feed,
        "symbols_requested": len(symbols), "symbols_used": len({t["symbol"] for t in all_trades}),
        "counts": totals, "params": {k: v for k, v in P.items()},
        "results": [{
            "id": r["cfg"]["id"], "setup": r["cfg"]["setup"], "label": r["cfg"]["label"], "stop": r["cfg"]["stop"],
            "target_r": r["cfg"]["target_r"], "verdict": r["verdict"], "why": r["why"],
            "all": r["full"], "early": r["dev"], "late": r["hold"], "ci95": r["ci"], "filters": r["filters"],
            "portfolio": {k: v for k, v in r["portfolio"].items() if k not in ("curve", "bench")},
        } for r in results],
    }
    with open(os.path.join(a.out, "summary.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=1, default=lambda x: round(x, 6) if isinstance(x, float) else str(x))
    fields = ["setup", "stop_rule", "target_r", "symbol", "date", "entry_time", "entry", "entry_fill", "stop",
              "target", "risk_per_share", "outcome", "exit_time", "exit_fill", "R_before_costs", "R_after_costs",
              "spy_above_50d", "spy_up_at_entry", "trigger_volume_x", "gap_pct"]
    with gzip.open(os.path.join(a.out, "trades.csv.gz"), "wt", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(fields)
        for t in all_trades:
            c = cfgs[t["cfg"]]
            w.writerow([c["setup"], c["stop"], c["target_r"], t["symbol"], t["date"], t["entry_time"], t["entry"],
                        round(t["e_fill"], 4), t["stop"], t["target"], round(t["risk"], 4), t["outcome"],
                        t["exit_time"], round(t["x_fill"], 4), round(t["R_gross"], 4), round(t["R"], 4),
                        t["spy_trend"], t["spy_up"], None if t["rvol"] is None else round(t["rvol"], 3),
                        None if t["gap"] is None else round(t["gap"], 3)])
    write_report(os.path.join(a.out, "report.html"), summary, results, P)
    if not a.quiet:
        print_summary(summary, results, a.out)
    return 0


def print_summary(summary, results, out):
    print("")
    print("Backtest %s to %s, %d stocks, %d trading days (early/late split at %s)."
          % (summary["start"], summary["end"], summary["symbols_used"], summary["trading_days"], summary["split_date"]))
    print("%-22s %-15s %5s %7s %6s %9s %8s %8s  %s" % ("setup", "stop", "tgt", "trades", "win%", "avg R", "early",
                                                      "late", "verdict"))
    for r in ranked(results):
        f = r["full"]
        print("%-22s %-15s %4gR %7d %5s %9s %8s %8s  %s" % (
            r["cfg"]["setup"], stop_words(r["cfg"]["stop"]), r["cfg"]["target_r"], f.get("n", 0),
            ("%.0f%%" % (100 * f["win_rate"])) if f.get("n") else "-", fr(f.get("mean")),
            fr(r["dev"].get("mean")), fr(r["hold"].get("mean")), r["verdict"]))
    worth = [r for r in results if r["verdict"] == "WORTH PAPER TRADING"]
    print("")
    if worth:
        print("%d variation(s) passed every check. Paper trade only those, with the same stop and target." % len(worth))
    else:
        print("No variation passed every check: no reliable edge after costs on this data.")
    print("Report: %s   (also summary.json and trades.csv.gz)" % os.path.join(out, "report.html"))
    print("Past results do not predict future ones. Research, not financial advice.")


def main(argv=None):
    p = argparse.ArgumentParser(prog="backtest.py", description="Backtest the day-trader pack's setups.")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--symbols", help="comma list or a file of tickers (default: 80 liquid US stocks)")
        sp.add_argument("--quick", action="store_true", help="20 stocks, 1 year: a fast first look")
        sp.add_argument("--max-symbols", type=int)
        sp.add_argument("--years", type=float, default=2.0)
        sp.add_argument("--start", help="YYYY-MM-DD (default: --years before --end)")
        sp.add_argument("--end", help="YYYY-MM-DD (default: yesterday)")
        sp.add_argument("--feed", default="sip", choices=["sip", "iex"])
        sp.add_argument("--cache", default=os.path.join("trader", "backtest", "cache"))
        sp.add_argument("--offline", help="read saved bars from a folder or file instead of Alpaca")
        sp.add_argument("--quiet", action="store_true")

    sp = sub.add_parser("check-keys", help="check the Alpaca keys with one small request")
    sp.add_argument("--feed", default="sip", choices=["sip", "iex"])
    sp.add_argument("--quiet", action="store_true")
    sp = sub.add_parser("fetch", help="download and cache the bars only")
    common(sp)
    sp = sub.add_parser("run", help="backtest every setup and write the scorecard")
    common(sp)
    sp.add_argument("--setups", help="comma list (default: all): " + ", ".join(SETUPS))
    sp.add_argument("--targets", default="1,2", help="profit targets in R, comma list (default 1,2)")
    sp.add_argument("--slippage-bps", type=float, default=5.0,
                    help="cost per side in basis points: 5 = 0.05%% on the way in and on the way out (default 5)")
    sp.add_argument("--fill", default="next_open", choices=["next_open", "close"],
                    help="entry price: the next bar's open (realistic, default) or the trigger bar's close")
    sp.add_argument("--min-price", type=float, default=5.0)
    sp.add_argument("--budget", type=float)
    sp.add_argument("--risk-pct", type=float, help="percent of the account risked per trade (default: config's "
                                                   "max_loss_per_trade_usd / budget = 1%%)")
    sp.add_argument("--max-trades-per-day", type=int)
    sp.add_argument("--no-entries-after", help="HH:MM (default 15:30)")
    sp.add_argument("--config", default=os.path.join("trader", "config.json"))
    sp.add_argument("--out", default=os.path.join("trader", "backtest"))
    sp.add_argument("--seed", type=int, default=19)
    a = p.parse_args(argv)
    if getattr(a, "quick", False) and not getattr(a, "start", None) and a.years == 2.0:
        a.years = 1.0
    try:
        return {"check-keys": cmd_check_keys, "fetch": cmd_fetch, "run": cmd_run}[a.cmd](a)
    except BTError as e:
        print("error: %s" % e, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nstopped. Bars downloaded so far stay cached; run again to continue.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
