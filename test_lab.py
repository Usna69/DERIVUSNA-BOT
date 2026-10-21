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
