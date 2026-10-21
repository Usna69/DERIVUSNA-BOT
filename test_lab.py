"""Offline tests for lab.py and candles.py (no network)."""
import os, random, tempfile, unittest
import candles
import lab


def make(prices, gran=300, spread_hl=0.5, t0=1_700_000_000):
    rows = []
    for i, p in enumerate(prices):
        o = prices[i - 1] if i else p
        rows.append((t0 + i * gran, o, max(o, p) + spread_hl, min(o, p) - spread_hl, p))
    return lab.Series(rows, gran)


def walk(n, seed=1, step=1.0):
    rnd, x, out = random.Random(seed), 100.0, []
    for _ in range(n):
        x += rnd.gauss(0, step)
        out.append(round(x, 3))
    return out


class IndicatorTests(unittest.TestCase):
    def test_sma_matches_naive(self):
        s = make(walk(200))
        sm = s.sma(10)
        self.assertIsNone(sm[8])
        for i in (9, 50, 199):
            self.assertAlmostEqual(sm[i], sum(s.c[i - 9:i + 1]) / 10)

    def test_rsi_bounds_and_direction(self):
        up = make([100 + i for i in range(60)]).rsi(14)
        down = make([200 - i for i in range(60)]).rsi(14)
        self.assertEqual(up[-1], 100.0)
        self.assertLess(down[-1], 1.0)
        self.assertTrue(all(0 <= x <= 100 for x in make(walk(300)).rsi(14) if x is not None))

    def test_bollinger_brackets_the_mean(self):
        s = make(walk(100))
        lo, hi = s.boll(20, 2.0)
        for i in (30, 99):
            self.assertLess(lo[i], s.sma(20)[i])
            self.assertGreater(hi[i], s.sma(20)[i])


class NoLookaheadTests(unittest.TestCase):
    def test_signal_at_bar_i_ignores_the_future(self):
        full = make(walk(600, seed=3, step=1.5))
        cut = 400
        part = lab.Series([(full.t[i], full.o[i], full.h[i], full.l[i], full.c[i]) for i in range(cut)], full.gran)
        for name, (fn, params, _) in lab.STRATEGIES.items():
            a, b = fn(full, **params), fn(part, **params)
            self.assertEqual(a[:cut], b, f"{name} changed its past signals when future bars were added")

    def test_strategies_actually_fire(self):
        s = make(walk(3000, seed=5, step=1.5))
        for name, (fn, params, _) in lab.STRATEGIES.items():
            self.assertGreater(sum(x != 0 for x in fn(s, **params)), 5, name)


class SimulateTests(unittest.TestCase):
    def series_with_trade(self, next_bar):
        """30 flat bars, then bar 31 (index) is the trade's entry bar, shaped by next_bar=(o,h,l,c)."""
        rows = [(1_700_000_000 + i * 300, 100.0, 100.5, 99.5, 100.0) for i in range(40)]
        rows[31] = (rows[31][0], *next_bar)
        return lab.Series(rows, 300)

    def sig_at(self, s, i, d=1):
        sig = [0] * s.n
        sig[i] = d
        return sig

    def test_target_hit_gives_plus_rr_minus_cost(self):
        s = self.series_with_trade((100.0, 103.0, 99.9, 102.0))     # atr=1.0 -> stop 99.0? dist 1.5, tp +2.25
        (t, r), = lab.simulate(s, self.sig_at(s, 30), sl_atr=1.5, rr=1.5, spread_pct=0.0)
        self.assertAlmostEqual(r, 1.5)
        (t, r2), = lab.simulate(s, self.sig_at(s, 30), sl_atr=1.5, rr=1.5, spread_pct=0.1)
        self.assertLess(r2, r)

    def test_stop_hit_gives_minus_one(self):
        s = self.series_with_trade((100.0, 100.2, 97.0, 98.0))
        (t, r), = lab.simulate(s, self.sig_at(s, 30), 1.5, 1.5, 0.0)
        self.assertAlmostEqual(r, -1.0)

    def test_both_hit_in_one_bar_counts_as_loss(self):
        s = self.series_with_trade((100.0, 110.0, 90.0, 100.0))
        (t, r), = lab.simulate(s, self.sig_at(s, 30), 1.5, 1.5, 0.0)
        self.assertAlmostEqual(r, -1.0)

    def test_extreme_stop_fill_books_the_gap_past_the_stop(self):
        s = self.series_with_trade((100.0, 100.2, 90.0, 95.0))          # stop at 98.5, bar low far beyond it
        (t, level), = lab.simulate(s, self.sig_at(s, 30), 1.5, 1.5, 0.0, stop_fill="level")
        (t, worst), = lab.simulate(s, self.sig_at(s, 30), 1.5, 1.5, 0.0, stop_fill="extreme")
        self.assertAlmostEqual(level, -1.0)
        self.assertLess(worst, -5.0)                                    # (100-90)/1.5 = -6.7R

    def test_extreme_fill_never_beats_the_level_fill(self):
        s = make(walk(2500, seed=4, step=1.0))
        sig = lab.sma_cross(s)
        a = sum(r for _, r in lab.simulate(s, sig, 1.5, 1.5, 0.0, stop_fill="level"))
        b = sum(r for _, r in lab.simulate(s, sig, 1.5, 1.5, 0.0, stop_fill="extreme"))
        self.assertLessEqual(b, a + 1e-9)

    def test_scale_out_banks_half_then_runner_stopped_at_entry(self):
        # ATR 1.0 -> dist 1.5. TP1 at 1R = entry+1.5 hit on the entry bar; runner then falls back to the entry.
        rows = [(1_700_000_000 + i * 300, 100.0, 100.5, 99.5, 100.0) for i in range(40)]
        rows[31] = (rows[31][0], 100.0, 101.8, 99.9, 101.5)             # reaches +1.5 (TP1), not the stop (98.5)
        rows[32] = (rows[32][0], 101.5, 101.6, 99.0, 100.0)             # falls back through the entry (100.0)
        s = lab.Series(rows, 300)
        (t, r), = lab.simulate(s, self.sig_at(s, 30), sl_atr=1.5, rr=3.0, spread_pct=0.0, tp1=1.0, tp1_frac=0.5)
        self.assertAlmostEqual(r, 0.5)                                   # 0.5 * 1R banked, runner flat

    def test_scale_out_runner_reaches_the_final_target(self):
        rows = [(1_700_000_000 + i * 300, 100.0, 100.5, 99.5, 100.0) for i in range(40)]
        rows[31] = (rows[31][0], 100.0, 101.8, 99.9, 101.5)             # TP1 (+1.5)
        rows[32] = (rows[32][0], 101.5, 105.5, 101.0, 105.0)            # runner target 3R = +4.5 -> hit
        s = lab.Series(rows, 300)
        (t, r), = lab.simulate(s, self.sig_at(s, 30), 1.5, 3.0, 0.0, tp1=1.0, tp1_frac=0.5)
        self.assertAlmostEqual(r, 0.5 * 1.0 + 0.5 * 3.0)                 # 2.0R

    def test_scale_out_stop_before_tp1_is_a_full_loss(self):
        s = self.series_with_trade((100.0, 100.2, 97.0, 98.0))
        (t, r), = lab.simulate(s, self.sig_at(s, 30), 1.5, 3.0, 0.0, tp1=1.0)
        self.assertAlmostEqual(r, -1.0)

    def test_scale_out_rejects_a_first_target_beyond_the_final_one(self):
        s = self.series_with_trade((100.0, 103.0, 99.9, 102.0))
        with self.assertRaises(ValueError):
            lab.simulate(s, self.sig_at(s, 30), 1.5, 1.5, 0.0, tp1=2.0)

    def test_sell_mirrors_buy(self):
        s = self.series_with_trade((100.0, 100.1, 97.0, 98.0))
        (t, r), = lab.simulate(s, self.sig_at(s, 30, -1), 1.5, 1.5, 0.0)
        self.assertAlmostEqual(r, 1.5)

    def test_weekend_gap_entry_is_skipped(self):
        s = self.series_with_trade((100.0, 103.0, 99.9, 102.0))
        s.t[31] += 10 * 300                                       # gap before the entry bar
        self.assertEqual(lab.simulate(s, self.sig_at(s, 30), 1.5, 1.5, 0.0), [])


class FilterTests(unittest.TestCase):
    def test_session_filter_keeps_only_the_window(self):
        s = make([100.0] * 300)                                   # 5-min bars from a known UTC time
        sig = lab.session_filter([1] * s.n, s, 0, 1)
        import time
        for i, x in enumerate(sig):
            self.assertEqual(x == 1, 0 <= time.gmtime(s.t[i]).tm_hour < 1)

    def test_trend_filter_blocks_counter_trend(self):
        s = make([100 + i * 0.1 for i in range(400)])             # steady uptrend
        out = lab.trend_filter([1, -1] * 200, s, 50)
        self.assertTrue(all(x != -1 for x in out))
        self.assertTrue(any(x == 1 for x in out[100:]))


class VerdictTests(unittest.TestCase):
    def trades(self, rs):
        return [(i, r) for i, r in enumerate(rs)]

    def test_too_few(self):
        self.assertEqual(lab.verdict(self.trades([1.0] * 50)), "TOO FEW")

    def test_pass_needs_both_halves_clearly_positive(self):
        rnd = random.Random(2)
        good = [rnd.choice((1.5, -1.0, 1.5)) for _ in range(900)]       # strong edge
        self.assertEqual(lab.verdict(self.trades(good)), "PASS")

    def test_fail_when_second_half_is_negative(self):
        bad = [1.0, -1.0] * 100 + [-1.0, 1.0, -1.0] * 100
        self.assertEqual(lab.verdict(self.trades(bad)), "FAIL")

    def test_random_baseline_beats_nothing_on_a_random_walk(self):
        s = make(walk(2500, seed=9, step=1.0))
        p = lab.random_baseline(s, 80, 1.5, 1.5, 0.0, observed=5.0, trials=5)
        self.assertEqual(p, 0.0)                                         # +5R average is impossible by luck


class EvaluateTests(unittest.TestCase):
    class Args:
        symbol, tf, spread_pct, sl_atr, rr, stop_fill, session, trend, fast = "frxTEST", 300, 0.01, 1.5, 1.5, "level", (12, 17), 0, True

    def test_evaluate_and_format(self):
        trades = [(i, 1.5 if i % 3 == 0 else -1.0) for i in range(30)]
        ev = lab.evaluate(trades, make(walk(300)), self.Args)
        self.assertEqual(ev["n"], 30)
        self.assertIsNone(ev["random_better"])                    # --fast skips the baseline
        self.assertIn("n=30", lab.fmt("x", ev))
        self.assertEqual(lab.fmt("x", lab.evaluate([])), "x                        no trades")

    def test_row_for_the_database_has_every_column(self):
        s = make(walk(300))
        ev = lab.evaluate([(i, -1.0) for i in range(5)], s, self.Args)
        row = lab.to_row("sma_cross", ev, s, self.Args)
        self.assertEqual(set(row), set(__import__("db").LAB_COLS))
        self.assertEqual(row["filters"], "session 12-17")
        self.assertEqual(row["tf_seconds"], 300)


class BatchTests(unittest.TestCase):
    def test_default_spreads(self):
        self.assertEqual(lab.default_spread("frxUSDJPY"), 0.009)          # measured on the user's account
        self.assertEqual(lab.default_spread("frxEURGBP"), lab.DEFAULT_SPREAD)
        self.assertGreater(lab.default_spread("frxXPDUSD"), lab.default_spread("frxXAUUSD"))

    def test_batch_runs_every_pair_and_timeframe_and_survives_a_failure(self):
        calls = []
        orig = lab.cmd_compare
        def fake(a):
            calls.append((a.symbol, a.tf, a.spread_pct, a.fast))
            if a.symbol == "frxBAD":
                raise SystemExit("no data")
        lab.cmd_compare = fake
        try:
            lab.main(["batch", "--symbols", "frxUSDJPY,frxBAD", "--tfs", "300,900"])
        finally:
            lab.cmd_compare = orig
        self.assertEqual([c[:2] for c in calls], [("frxUSDJPY", 300), ("frxUSDJPY", 900), ("frxBAD", 300), ("frxBAD", 900)])
        self.assertTrue(all(c[3] for c in calls))                          # batch always skips the slow baseline
        self.assertEqual(calls[0][2], 0.009)

    def test_an_explicit_spread_overrides_the_default(self):
        calls = []
        orig = lab.cmd_compare
        lab.cmd_compare = lambda a: calls.append(a.spread_pct)
        try:
            lab.main(["batch", "--symbols", "frxUSDJPY", "--tfs", "300", "--spread-pct", "0.05"])
        finally:
            lab.cmd_compare = orig
        self.assertEqual(calls, [0.05])


class FakeRates:
    """MT5 rates as list-of-dicts (what the bot's own fake uses), with a bar limit like a real terminal."""
    def __init__(self, closes, spread=15, limit=None, t0=1_500_000_000, gran=3600):
        self.rows = [{"time": t0 + i * gran, "open": c, "high": c + 0.2, "low": c - 0.2, "close": c, "spread": spread} for i, c in enumerate(closes)]
        self.limit, self.asked, self.names = limit, [], []

    def symbol_select(self, name, on):
        self.names.append(name)
        return name != "NOPE"

    def symbol_info(self, name):
        return type("SI", (), {"point": 0.001})()

    def copy_rates_from_pos(self, name, tf, start, n):
        self.asked.append(n)
        if self.limit is not None and n > self.limit:
            return None
        return self.rows[-n:]


class MT5SourceTests(unittest.TestCase):
    def setUp(self):
        import mt5data, mt5_bot
        self.md, self.bot = mt5data, mt5_bot
        self.saved = (mt5_bot.mt5, mt5data._connected)
        mt5data._connected = True                                    # skip the real terminal connection
        mt5_bot.mt5 = mt5_bot.mt5 or type("M", (), {})()
        mt5_bot.mt5.TIMEFRAME_H1 = 16385
        self.fake = FakeRates(walk(3000, seed=2, step=0.5))
        self.real = {k: getattr(mt5_bot.mt5, k, None) for k in ("symbol_select", "symbol_info", "copy_rates_from_pos")}
        for k in self.real:
            setattr(mt5_bot.mt5, k, getattr(self.fake, k))

    def tearDown(self):
        self.bot.mt5, self.md._connected = self.saved

    def test_name_mapping_rows_and_measured_spread(self):
        rows, spread = self.md.load("frxAUDJPY", 3600, 2000)
        self.assertEqual(self.fake.names, ["AUDJPY"])                  # public-feed name mapped to the MT5 name
        self.assertEqual(len(rows), 2000)
        self.assertEqual(rows[0][0] + 3600, rows[1][0])
        expected = 15 * 0.001 / rows[0][4] * 100
        self.assertLess(abs(spread - expected) / expected, 0.5)         # median of per-bar spread as % of price

    def test_halves_the_request_when_the_terminal_caps_bars(self):
        self.fake.limit = 1200
        rows, _ = self.md.load("frxAUDJPY", 3600, 100000)
        self.assertLessEqual(len(rows), 1200)
        self.assertGreater(len(self.fake.asked), 1)
        self.assertEqual(self.fake.asked[0], 100000)

    def test_unavailable_symbol_and_bad_timeframe_give_clear_errors(self):
        with self.assertRaises(RuntimeError) as cm:
            self.md.load("NOPE", 3600)
        self.assertIn("Market Watch", str(cm.exception))
        with self.assertRaises(RuntimeError):
            self.md.load("frxAUDJPY", 777)

    def test_split_by_age_and_holds(self):
        trades = [(100, 1.0), (200, -1.0), (900, 0.5)]
        older, recent = lab.split_by_age(trades, 1000, days=0.005)     # cut at 1000 - 432 = 568
        self.assertEqual([x[0] for x in older], [100, 200])
        self.assertEqual([x[0] for x in recent], [900])
        self.assertEqual(lab.holds({"n": 400, "mean": 0.1, "lo": 0.02, "hi": 0.2}), "YES")
        self.assertEqual(lab.holds({"n": 400, "mean": 0.1, "lo": -0.05, "hi": 0.25}), "INCONCLUSIVE")
        self.assertEqual(lab.holds({"n": 400, "mean": -0.05, "lo": -0.2, "hi": 0.1}), "NO")
        self.assertEqual(lab.holds({"n": 10, "mean": 0.5, "lo": 0.4, "hi": 0.6}), "TOO FEW")

    def test_candidates_command_runs_all_five_on_mt5_history(self):
        import io, contextlib
        self.fake.rows = FakeRates(walk(30000, seed=5, step=0.6)).rows          # about 3.4 years of hourly bars
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            lab.main(["candidates", "--bars", "30000"])
        text = out.getvalue()
        for pair, strat in lab.CANDIDATES:
            self.assertIn(f"{pair} 1h {strat}", text)
        self.assertEqual(text.count("holds on new data:"), 5)
        self.assertIn("older (new data)", text)


class StallTests(unittest.TestCase):
    def test_trickle_of_one_candle_counts_as_a_stall(self):
        s = 0
        for _ in range(2):
            s = candles.next_stalls(s, 1)
        self.assertEqual(s, 2)                                   # stops the loop (limit is 2)

    def test_a_healthy_page_resets_the_counter(self):
        self.assertEqual(candles.next_stalls(1, 690), 0)
        self.assertEqual(candles.next_stalls(0, 0), 1)


class CandleCacheTests(unittest.TestCase):
    def test_roundtrip_and_ordering(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = {300: (1.0, 2.0, 0.5, 1.5), 100: (1.1, 2.1, 0.6, 1.6)}
            candles.save("frxTEST", 300, rows, tmp)
            self.assertEqual(candles.load("frxTEST", 300, tmp), rows)
            with open(candles.cache_path("frxTEST", 300, tmp)) as f:
                self.assertEqual([int(l.split(",")[0]) for l in f], [100, 300])

    def test_partial_line_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(candles.cache_path("x", 60, tmp), "w") as f:
                f.write("1,1,2,0.5,1.5\n2,1,2\n")
            self.assertEqual(list(candles.load("x", 60, tmp)), [1])


if __name__ == "__main__":
    unittest.main()
