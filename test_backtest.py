"""Offline tests for trader-backtest. Run: python3 -m unittest test_backtest -v (from this folder)."""
import datetime
import gzip
import json
import os
import random
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import backtest as bt  # noqa: E402

PACK_FIXTURE = os.path.join(HERE, "..", "tests", "fixtures", "trader-timemachine", "TSLA-60d5m.json.gz")


def flat_day(price=100.0, n=bt.SESSION_BARS):
    return [(price, price + 0.1, price - 0.1, price, 1000.0) for _ in range(n)]


def synthetic_bars(symbol, start, days, seed, drift=0.0, vol=0.0015, price=100.0):
    """Weekday sessions of 78 five-minute bars, as [ts, o, h, l, c, v], New York 9:30 to 16:00."""
    rng = random.Random(seed)
    out = []
    d = datetime.date.fromisoformat(start)
    made = 0
    while made < days:
        if d.weekday() < 5:
            noon_utc = datetime.datetime(d.year, d.month, d.day, 12)
            off = bt.eastern_offset(noon_utc)
            open_utc = datetime.datetime(d.year, d.month, d.day, 9, 30) - datetime.timedelta(hours=off)
            ts0 = int(open_utc.replace(tzinfo=datetime.timezone.utc).timestamp())
            price *= 1 + rng.gauss(0, 0.01)
            for i in range(bt.SESSION_BARS):
                o = price
                c = o * (1 + rng.gauss(drift, vol))
                h = max(o, c) * (1 + abs(rng.gauss(0, vol / 2)))
                l = min(o, c) * (1 - abs(rng.gauss(0, vol / 2)))
                out.append([ts0 + 300 * i, round(o, 2), round(h, 2), round(l, 2), round(c, 2),
                            int(rng.uniform(5e4, 2e5) * (3 if i < 3 else 1))])
                price = c
            made += 1
        d += datetime.timedelta(days=1)
    return out


class TestFills(unittest.TestCase):
    def test_stop_and_target_in_one_bar_counts_the_stop(self):
        D = flat_day()
        D[5] = (100, 103, 97, 100, 1000)
        r = bt.play(D, 4, 100.0, 98.0, 102.0, 76, fill="close", slip=0.0)
        self.assertEqual(r["outcome"], "stop")
        self.assertEqual(r["x_raw"], 98.0)

    def test_gap_through_stop_fills_at_the_open(self):
        D = flat_day()
        D[5] = (96.0, 96.5, 95.5, 96.2, 1000)
        r = bt.play(D, 4, 100.0, 98.0, 102.0, 76, fill="close", slip=0.0, gap_fills=True)
        self.assertEqual((r["outcome"], r["x_raw"]), ("stop", 96.0))
        r = bt.play(D, 4, 100.0, 98.0, 102.0, 76, fill="close", slip=0.0, gap_fills=False)
        self.assertEqual((r["outcome"], r["x_raw"]), ("stop", 98.0))

    def test_gap_past_target_fills_at_the_open(self):
        D = flat_day()
        D[6] = (103.0, 103.5, 102.8, 103.2, 1000)
        r = bt.play(D, 4, 100.0, 98.0, 102.0, 76, fill="close", slip=0.0)
        self.assertEqual((r["outcome"], r["x_raw"]), ("target", 103.0))

    def test_next_open_entry_and_slippage(self):
        D = flat_day()
        D[5] = (100.5, 100.6, 100.4, 100.5, 1000)
        r = bt.play(D, 4, 100.0, 98.0, 102.0, 76, fill="next_open", slip=0.001)
        self.assertAlmostEqual(r["e_fill"], 100.5 * 1.001)
        self.assertEqual(r["outcome"], "close")
        self.assertAlmostEqual(r["x_fill"], 100.0 * 0.999)

    def test_target_is_a_limit_order_without_slippage(self):
        D = flat_day()
        D[7] = (101, 102.5, 100.9, 102.2, 1000)
        r = bt.play(D, 4, 100.0, 98.0, 102.0, 76, fill="close", slip=0.001)
        self.assertEqual(r["outcome"], "target")
        self.assertEqual(r["x_fill"], 102.0)

    def test_flat_by_the_close(self):
        D = flat_day()
        D[76] = (100, 100.1, 99.9, 99.5, 1000)
        r = bt.play(D, 4, 100.0, 98.0, 102.0, 76, fill="close", slip=0.0)
        self.assertEqual((r["outcome"], r["x_raw"], r["exit_slot"]), ("close", 99.5, 76))

    def test_no_bar_left_to_fill_at_next_open(self):
        self.assertIsNone(bt.play(flat_day(), 76, 100.0, 98.0, 102.0, 76, fill="next_open"))


class TestRules(unittest.TestCase):
    def test_stop_rounds_down_to_the_cent(self):
        self.assertEqual(bt.cents_down((362.18 + 354.47) / 2), 358.32)

    def test_clock_slots(self):
        self.assertEqual(bt.slot_closing_at(bt.hhmm("10:15", "t")), 8)
        self.assertEqual(bt.slot_closing_at(bt.hhmm("15:55", "t")), 76)
        self.assertEqual(bt.slot_closing_at(bt.hhmm("15:30", "t")), 71)

    def test_eastern_offset(self):
        self.assertEqual(bt.eastern_offset(datetime.datetime(2026, 7, 1, 15)), -4)
        self.assertEqual(bt.eastern_offset(datetime.datetime(2026, 1, 15, 15)), -5)

    def test_daily_tags_breakout(self):
        c = [100.0] * 40 + [110.0]
        self.assertIn("breakout_20d", bt.daily_tags(c))

    def test_verdict_rules(self):
        good = {"n": 500, "mean": 0.1}
        self.assertEqual(bt.verdict(good, {"n": 300, "mean": 0.1}, {"n": 200, "mean": 0.08}, 0.02)[0],
                         "WORTH PAPER TRADING")
        self.assertEqual(bt.verdict(good, {"n": 300, "mean": 0.2}, {"n": 200, "mean": -0.05}, 0.01)[0], "NOT PROVEN")
        self.assertEqual(bt.verdict(good, {"n": 300, "mean": 0.1}, {"n": 200, "mean": 0.08}, -0.01)[0], "NOT PROVEN")
        self.assertEqual(bt.verdict({"n": 500, "mean": -0.01}, {}, {}, None)[0], "LOSES MONEY")
        self.assertEqual(bt.verdict({"n": 40, "mean": 0.5}, {}, {}, None)[0], "TOO FEW TRADES")


class TestPortfolio(unittest.TestCase):
    P = {"budget": 5000.0, "risk_pct": 1.0, "daily_loss_pct": 3.0, "max_trades_per_day": 3,
         "cash_buffer_pct": 10.0, "max_position_pct": 75.0}

    def trade(self, d, k, sym, e=100.0, risk=2.0, x=102.0):
        return {"date": d, "k": k, "symbol": sym, "rvol": 1.0, "risk": risk, "e_fill": e, "x_fill": x}

    def test_sizing_and_daily_cap(self):
        trs = [self.trade("2026-01-05", k, s) for k, s in [(3, "A"), (4, "B"), (5, "C"), (6, "D")]]
        spy = {"2026-01-05": {"open": 100.0, "close": 101.0}}
        r = bt.portfolio(trs, ["2026-01-05"], dict(self.P, max_position_pct=100.0, cash_buffer_pct=0.0), spy)
        # 1% of 5000 = $50 risk / $2 = 25 shares, $2,500 each: cash allows two, and the 3rd is skipped
        self.assertEqual(r["trades_taken"], 2)
        self.assertAlmostEqual(r["final"], 5000 + 2 * 25 * 2.0)
        self.assertAlmostEqual(r["spy_return_pct"], 1.0)

    def test_tiny_risk_is_capped_by_position_size(self):
        trs = [self.trade("2026-01-05", 3, "A", e=100.0, risk=0.10, x=100.10)]
        r = bt.portfolio(trs, ["2026-01-05"], self.P, {"2026-01-05": {"open": 1.0, "close": 1.0}})
        self.assertEqual(r["trades_taken"], 1)
        self.assertAlmostEqual(r["final"], 5000 + 37 * 0.10)   # 75% of $5,000 buys 37 shares, not 500


@unittest.skipUnless(os.path.isfile(PACK_FIXTURE), "the day-trader pack's TSLA fixture is not next to this skill")
class TestMatchesThePack(unittest.TestCase):
    def test_tsla_history_replay(self):
        days = bt.sessionize(bt.load_offline(PACK_FIXTURE, "TSLA"))
        P = {"no_entries_before": bt.hhmm("09:35", "x"), "no_entries_after": bt.hhmm("15:55", "x"),
             "flat_by": bt.hhmm("15:55", "x"), "fill": "close", "slip": 0.0, "gap_fills": False,
             "min_risk_pct": 0.0, "min_price": 0.0}
        cfgs = [c for c in bt.build_configs(["orb15"], [1.0]) if c["stop"] == "or-mid"]
        trades, _ = bt.replay_symbol("TSLA", days, cfgs, P, {}, "2000-01-01", "2026-10-18")
        oc = [t["outcome"] for t in trades]
        self.assertEqual((len(trades), oc.count("target"), oc.count("stop"), oc.count("close")), (33, 13, 15, 5))


class TestEndToEnd(unittest.TestCase):
    def test_run_offline_writes_the_scorecard(self):
        tmp = tempfile.mkdtemp()
        try:
            data = os.path.join(tmp, "bars")
            os.makedirs(data)
            for i, sym in enumerate(["SPY", "AAA", "BBB", "CCC", "DDD"]):
                bars = synthetic_bars(sym, "2025-01-02", 300, seed=i, drift=0.00002 * (i - 2))
                with gzip.open(os.path.join(data, sym + ".json.gz"), "wt") as fh:
                    json.dump({"bars": bars}, fh)
            out = os.path.join(tmp, "out")
            rc = bt.main(["run", "--offline", data, "--symbols", "AAA,BBB,CCC,DDD", "--start", "2025-06-01",
                          "--end", "2026-02-20", "--out", out, "--config", os.path.join(tmp, "none.json"),
                          "--quiet"])
            self.assertEqual(rc, 0)
            for f in ("report.html", "summary.json", "trades.csv.gz"):
                self.assertTrue(os.path.isfile(os.path.join(out, f)), f)
            with open(os.path.join(out, "summary.json")) as fh:
                s = json.load(fh)
            self.assertEqual(len(s["results"]), 28)
            orb = [r for r in s["results"] if r["id"] == "orb15|or-mid|1R"][0]
            self.assertGreater(orb["all"]["n"], 100)
            # Random-walk prices have no edge: after costs the average trade should not be clearly positive
            self.assertLess(orb["all"]["mean"], 0.05)
        finally:
            shutil.rmtree(tmp)


if __name__ == "__main__":
    unittest.main()
