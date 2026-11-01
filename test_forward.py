"""Offline tests for forward.py. Run: python3 -m unittest discover -s trader-backtest -p "test_*.py" """
import contextlib
import csv
import gzip
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import backtest as bt  # noqa: E402
import forward as fw  # noqa: E402
from test_backtest import synthetic_bars  # noqa: E402

SYMS = ["SPY"] + ["S%02d" % i for i in range(12)]


class TestForward(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.bars = os.path.join(cls.tmp, "bars")
        os.makedirs(cls.bars)
        for i, sym in enumerate(SYMS):
            b = synthetic_bars(sym, "2025-01-02", 300, seed=100 + i, vol=0.0025)
            with gzip.open(os.path.join(cls.bars, sym + ".json.gz"), "wt") as fh:
                json.dump({"bars": b}, fh)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def run_fw(self, *args):
        tdir = os.path.join(self.tmp, "trader")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = fw.main(["--trader-dir", tdir, "--offline", self.bars, "--symbols", ",".join(SYMS[1:]),
                          "--quiet"] + list(args))
        return rc, buf.getvalue(), tdir

    def test_matches_the_backtest_and_is_idempotent(self):
        rc, out, tdir = self.run_fw("evening", "--since", "2025-06-02", "--date", "2026-01-30")
        self.assertEqual(rc, 0, out)
        with open(os.path.join(tdir, "forward", "trades.csv"), newline="") as fh:
            logged = [(r["date"], r["symbol"], round(float(r["R_after_costs"]), 4)) for r in csv.DictReader(fh)]
        # the same rule straight from the backtest engine, with its gap filter
        P = fw.params(type("A", (), {"trader_dir": tdir})())
        cfgs = fw.the_cfg()
        spy = bt.spy_context(bt.sessionize(bt.load_offline(self.bars, "SPY")))
        want = []
        for sym in SYMS[1:]:
            days = bt.sessionize(bt.load_offline(self.bars, sym))
            trs, _ = bt.replay_symbol(sym, days, cfgs, P, spy, "2025-06-02", "2026-01-30")
            want += [(t["date"], sym, round(t["R"], 4)) for t in trs if t["gap"] is not None and abs(t["gap"]) < 1.0]
        self.assertGreater(len(want), 0, "the synthetic data should produce some signals")
        self.assertEqual(sorted(logged), sorted(want))
        # running again adds nothing
        rc, out, _ = self.run_fw("evening", "--date", "2026-01-30")
        with open(os.path.join(tdir, "forward", "trades.csv"), newline="") as fh:
            self.assertEqual(len(list(csv.DictReader(fh))), len(logged))
        # a later evening only scores the new days
        rc, out, _ = self.run_fw("evening", "--date", "2026-02-06")
        with open(os.path.join(tdir, "forward", "state.json")) as fh:
            st = json.load(fh)
        self.assertEqual(st["scored"][-1], "2026-02-06")
        self.assertEqual(st["watchlist"]["after_close_of"], "2026-02-06")
        rc, out, _ = self.run_fw("status")
        self.assertEqual(rc, 0)

    def test_watchlist_and_morning(self):
        rc, out, tdir = self.run_fw("evening", "--since", "2026-02-09", "--date", "2026-02-13")
        with open(os.path.join(tdir, "forward", "state.json")) as fh:
            wl = json.load(fh)["watchlist"]
        for x in wl["stocks"]:
            self.assertLess(x["open_low"], x["close"])
            self.assertGreater(x["open_high"], x["close"])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = fw.main(["--trader-dir", tdir, "--offline", self.bars, "morning", "--date", "2026-02-16"])
        self.assertEqual(rc, 0, buf.getvalue())


if __name__ == "__main__":
    unittest.main()
