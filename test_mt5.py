"""Offline tests for mt5_bot.py using a fake MetaTrader5 module (no terminal, no account)."""
import datetime, os, tempfile, unittest
from types import SimpleNamespace as NS
import deriv_bot as d
import mt5_bot as m


def closes_with_call_cross():
    c = [100.0] * 40
    for k in range(1, 40):
        cand = c + [100.0 + 3 * i for i in range(1, k + 1)]
        if m.d.signal(cand, m.FAST, m.SLOW) == "CALL":
            return cand
    raise AssertionError("could not build a cross")


class FakeMT5:
    TIMEFRAME_M15 = 15
    ORDER_FILLING_IOC, ORDER_FILLING_FOK, ORDER_FILLING_RETURN = 1, 0, 2
    TRADE_ACTION_DEAL, ORDER_TYPE_BUY, ORDER_TYPE_SELL, ORDER_TIME_GTC, TRADE_RETCODE_DONE = 1, 0, 1, 0, 10009

    def __init__(self, closes, equity=10.0, min_lot=0.01, spread=0.5, contract=1.0, stops=0):
        self.closes, self.equity = closes, equity
        self.si = NS(name="BTCUSD", volume_min=min_lot, volume_max=1.0, volume_step=0.01, trade_contract_size=contract,
                     digits=2, point=0.01, trade_stops_level=stops, trade_mode=4, filling_mode=2, visible=True)
        self.tick = NS(bid=closes[-1], ask=closes[-1] + spread)
        self.sent, self.positions, self.deals, self.send_retcode, self.bar_time = [], [], [], 10009, 1000

    def symbol_select(self, s, on): return True
    def symbol_info(self, s): return self.si
    def symbol_info_tick(self, s): return self.tick
    def symbols_get(self): return [self.si]
    def last_error(self): return (1, "fake")
    def account_info(self): return NS(equity=self.equity, balance=self.equity, trade_mode=0, login=1, currency="USD")
    def order_calc_profit(self, t, s, vol, a, b): return -abs(a - b) * vol * self.si.trade_contract_size
    def order_check(self, req): return NS(retcode=0, comment="")
    def positions_get(self, symbol=None): return tuple(self.positions)
    def history_deals_get(self, position=None): return self.deals

    def copy_rates_from_pos(self, s, tf, start, n):
        tail = self.closes[-n:]
        return [{"time": self.bar_time, "open": c, "high": c + 1, "low": c - 1, "close": c} for c in tail]

    def order_send(self, req):
        self.sent.append(req)
        return NS(retcode=self.send_retcode, order=777, comment="fake")


class MT5Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = (d.LOG, d.STATE_FILE, d.STOP_FILE, d.send_telegram, m.mt5)
        d.LOG, d.STATE_FILE = os.path.join(self.tmp.name, "t.csv"), os.path.join(self.tmp.name, "s.json")
        d.STOP_FILE = os.path.join(self.tmp.name, "STOP")
        d.send_telegram = lambda t: None
        self.fake = FakeMT5(closes_with_call_cross())
        m.mt5 = self.fake
        self.acc = self.fake.account_info()

    def tearDown(self):
        d.LOG, d.STATE_FILE, d.STOP_FILE, d.send_telegram, m.mt5 = self.saved
        self.tmp.cleanup()



class MT5Tests(MT5Fixture):
    def runner(self, orders=True):
        return m.Runner(orders, "BTCUSD")

    # --- sizing
    def test_lot_rounds_down_and_never_exceeds_risk(self):
        si = NS(name="X", volume_min=0.01, volume_max=5, volume_step=0.01, trade_contract_size=1, digits=2, point=0.01, trade_stops_level=0)
        tick = NS(bid=100.0, ask=100.1)
        plan, why = m.plan_order(si, tick, "CALL", 10.0, 5.0, lambda *a: -(a[3] - a[4]) * a[2])
        self.assertIsNone(why)
        self.assertLessEqual(plan["risk"], 5.0)
        self.assertGreater(plan["risk"], 4.0)

    def test_refuses_when_min_lot_exceeds_risk(self):
        plan, why = m.plan_order(self.fake.si, self.fake.tick, "CALL", 100.0, 0.5, self.fake.order_calc_profit)
        self.assertIsNone(plan)
        self.assertIn("min lot", why)

    def test_wide_spread_skipped(self):
        fake = FakeMT5(self.fake.closes, spread=50)
        plan, why = m.plan_order(fake.si, fake.tick, "CALL", 20.0, 100.0, fake.order_calc_profit)
        self.assertIsNone(plan)
        self.assertIn("spread", why)

    def test_sell_stops_are_on_the_right_side(self):
        si, tick = self.fake.si, NS(bid=100.0, ask=100.1)
        plan, _ = m.plan_order(si, tick, "PUT", 2.0, 5.0, self.fake.order_calc_profit)
        self.assertGreater(plan["sl"], plan["entry"])
        self.assertLess(plan["tp"], plan["entry"])

    def test_every_request_has_sl_tp_and_magic(self):
        plan, _ = m.plan_order(self.fake.si, self.fake.tick, "CALL", 2.0, 5.0, self.fake.order_calc_profit)
        req = m.request_for(self.fake.si, plan)
        self.assertTrue(req["sl"] and req["tp"])
        self.assertEqual(req["magic"], m.MAGIC)

    # --- runner safety
    def risk_ok(self):
        m.RISK_USD = 1000.0                          # let sizing pass so we test the gates
        self.addCleanup(setattr, m, "RISK_USD", 0.5)

    def test_check_mode_and_signals_only_send_nothing(self):
        self.risk_ok()
        self.assertEqual(self.runner(False).step(self.acc, check_only=True), "validated")
        self.assertEqual(self.runner(False).step(self.acc), "validated")
        self.assertEqual(self.fake.sent, [])

    def test_places_one_order_then_blocks_while_open(self):
        self.risk_ok()
        r = self.runner()
        self.assertEqual(r.step(self.acc), "opened")
        self.assertEqual(len(self.fake.sent), 1)
        self.fake.positions = [NS(ticket=777, magic=m.MAGIC, profit=0.0)]
        self.fake.bar_time += 900
        self.assertEqual(r.step(self.acc), "position open")
        self.assertEqual(len(self.fake.sent), 1)

    def test_same_bar_not_traded_twice(self):
        self.risk_ok()
        r = self.runner(False)
        r.step(self.acc)
        self.assertEqual(r.step(self.acc), "waiting for next bar")

    def test_stop_file_equity_floor_daily_cap_and_trade_cap(self):
        self.risk_ok()
        r = self.runner()
        open(d.STOP_FILE, "w").close()
        self.assertEqual(r.step(self.acc), "STOP file present")
        os.remove(d.STOP_FILE)
        self.assertEqual(r.step(NS(equity=1.0)), "equity floor")
        self.assertEqual(r.step(self.acc), "halted")           # stays halted
        r2 = self.runner()
        r2.state.pnl = -m.DAILY_LOSS_USD
        self.assertEqual(r2.step(self.acc), "daily loss cap")
        r3 = self.runner()
        r3.state.trades = m.MAX_TRADES
        self.assertEqual(r3.step(self.acc), "max trades")
        self.assertEqual(self.fake.sent, [])

    def test_three_failed_orders_halt(self):
        self.risk_ok()
        self.fake.send_retcode = 10004
        r = self.runner()
        for _ in range(3):
            self.fake.bar_time += 900
            self.assertEqual(r.step(self.acc), "order failed")
        self.assertTrue(r.halted)
        self.fake.bar_time += 900
        self.assertEqual(r.step(self.acc), "halted")

    def test_closed_position_books_pnl_and_logs(self):
        r = self.runner()
        r.state.open_contracts = {777: ("CALL", 100.0, 0.5)}
        self.fake.deals = [NS(profit=-0.40, commission=-0.02, swap=0.0, fee=0.0)]
        r.settle_closed()
        self.assertAlmostEqual(r.state.pnl, -0.42)
        self.assertEqual(r.state.open_contracts, {})
        self.assertIn("mt5", open(d.LOG).read())

    def test_synthetic_indices_are_skipped_by_default(self):
        self.fake.si.path, self.fake.si.name = "Derived\\Volatility Indices\\Volatility 75 Index", "Volatility 75 Index"
        self.assertTrue(m.is_synthetic(self.fake.si))
        self.assertEqual(m.scan(print_rows=False), [])
        m.ALLOW_SYNTHETIC = True
        self.addCleanup(setattr, m, "ALLOW_SYNTHETIC", False)
        self.assertEqual(m.scan(print_rows=False), ["Volatility 75 Index"])
        self.assertFalse(m.is_synthetic(NS(path="Forex\\Majors\\EURUSD", name="EURUSD")))

    def test_scan_rejects_big_notional_and_accepts_small(self):
        m.mt5 = FakeMT5([c * 800 for c in self.fake.closes])    # BTC-sized prices: 0.01 lot risks several USD
        self.assertEqual(m.scan(print_rows=False), [])
        m.mt5 = self.fake                                        # price ~100: 0.01 lot risks cents
        self.assertEqual(m.scan(print_rows=False), ["BTCUSD"])


if __name__ == "__main__":
    unittest.main()


class MultiSymbolTests(MT5Fixture):
    """Reuses MT5Tests' fake terminal and temp state folder."""

    def risk_ok_multi(self):
        m.RISK_USD = 1000.0
        self.addCleanup(setattr, m, "RISK_USD", 0.5)

    def test_daily_loss_cap_is_shared_across_pairs(self):
        pool = []
        a, b = m.Runner(False, "BTCUSD", pool), m.Runner(False, "ETHUSD", pool)
        pool.extend([a, b])
        a.state.pnl, b.state.pnl = -0.5, -0.5                      # each below the cap alone, together at 1.00
        self.assertEqual(b.blocked(self.acc), "daily loss cap")
        self.assertTrue(a.limited and b.limited or b.limited)

    def test_trade_cap_is_shared_across_pairs(self):
        pool = []
        a, b = m.Runner(False, "BTCUSD", pool), m.Runner(False, "ETHUSD", pool)
        pool.extend([a, b])
        a.state.trades, b.state.trades = m.MAX_TRADES - 1, 1
        self.assertEqual(a.blocked(self.acc), "max trades")

    def test_each_pair_gets_its_own_state_file(self):
        a, b = m.Runner(False, "BTCUSD"), m.Runner(False, "ETHUSD")
        self.assertNotEqual(a.state_file, b.state_file)
        self.assertTrue(a.state_file.endswith("state_mt5_BTCUSD.json"))
        self.assertTrue(os.path.dirname(a.state_file) == os.path.dirname(d.STATE_FILE))   # nothing leaks into the cwd

    def test_legacy_single_state_file_still_loads(self):
        legacy = os.path.join(os.path.dirname(d.STATE_FILE), "state_mt5.json")
        old = d.Bot(None)
        old.pnl, old.trades, old.open_contracts = -0.2, 1, {55: ("PUT", 158.1, 0.35)}
        d.save_state(old, legacy)
        r = m.Runner(False, "BTCUSD", legacy_state=True)
        self.assertEqual(r.state.open_contracts, {55: ("PUT", 158.1, 0.35)})

    def test_counters_and_status_line_explain_a_silent_bot(self):
        m.mt5 = FakeMT5([c * 800 for c in self.fake.closes])        # BTC-sized prices: 0.01 lot risks several USD
        self.fake = m.mt5
        r = m.Runner(False, "BTCUSD")
        self.assertEqual(r.step(self.acc), "skipped")               # default 0.5 USD limit
        self.fake.bar_time += 900
        r.step(self.acc)
        line = r.status_line()
        self.assertIn("signals 2", line)
        self.assertIn("min lot above risk limit x2", line)
        self.assertIn("opened 0", line)

    def test_skip_keys(self):
        self.assertEqual(m.skip_key("spread 0.014 is more than 25% of the 0.05 stop"), "spread too wide")
        self.assertEqual(m.skip_key("min lot 0.01 would risk 0.73 USD"), "min lot above risk limit")

    def test_heartbeat_is_a_noop_without_a_database(self):
        r = m.Runner(False, "BTCUSD")
        self.assertEqual(m.heartbeat([r], 123.0), 123.0)


class StrategySwitchTests(MT5Fixture):
    """MT5_STRATEGY picks the rule; the lab's own functions produce the signal."""

    def long_series_with_rsi_signal(self):
        import lab, random
        rnd, x, c = random.Random(8), 100.0, []
        for _ in range(700):
            x += rnd.gauss(0, 1.0)
            c.append(round(x, 3))
        s = lab.Series([(i, v, v + 0.5, v - 0.5, v) for i, v in enumerate(c)], 300)
        sig = lab.rsi_revert(s)
        i = max(k for k in range(300, len(c)) if sig[k] == 1)
        return c[:i + 1]

    def test_rsi_revert_signal_comes_from_the_lab_function(self):
        closes = self.long_series_with_rsi_signal()
        self.fake = FakeMT5(closes)
        m.mt5 = self.fake
        old = m.STRATEGY
        m.STRATEGY = "rsi_revert"
        m.RISK_USD = 1000.0
        self.addCleanup(lambda: (setattr(m, "STRATEGY", old), setattr(m, "RISK_USD", 0.5)))
        self.assertEqual(m.bars_needed(), 260)
        v = m.symbol_view("BTCUSD")
        self.assertEqual(m.strategy_signal(v[3], v[4]), "CALL")
        r = m.Runner(False, "BTCUSD")
        self.assertEqual(r.step(self.fake.account_info()), "validated")
        self.assertEqual(r.counts["signals"], 1)
        self.assertEqual(self.fake.sent, [])                      # orders are off

    def test_sma_cross_is_the_default_and_needs_few_bars(self):
        self.assertEqual(m.STRATEGY, "sma_cross")
        self.assertLess(m.bars_needed(), 100)

    def test_every_lab_strategy_can_run_through_the_bot_path(self):
        import lab
        closes = [c for c in self.long_series_with_rsi_signal()]
        self.fake = FakeMT5(closes)
        m.mt5 = self.fake
        old = m.STRATEGY
        self.addCleanup(setattr, m, "STRATEGY", old)
        for name in lab.STRATEGIES:
            m.STRATEGY = name
            v = m.symbol_view("BTCUSD")
            self.assertIn(m.strategy_signal(v[3], v[4]), ("CALL", "PUT", None), name)


class PercentLimitTests(MT5Fixture):
    def pct(self, risk=0.0, daily=0.0, floor=0.0):
        old = (m.RISK_PCT, m.DAILY_LOSS_PCT, m.EQUITY_FLOOR_PCT)
        m.RISK_PCT, m.DAILY_LOSS_PCT, m.EQUITY_FLOOR_PCT = risk, daily, floor
        self.addCleanup(lambda: (setattr(m, "RISK_PCT", old[0]), setattr(m, "DAILY_LOSS_PCT", old[1]), setattr(m, "EQUITY_FLOOR_PCT", old[2])))

    def test_percentages_follow_the_balance(self):
        self.pct(1.4, 3.0, 70.0)
        lim = m.effective_limits(35.0)
        self.assertAlmostEqual(lim["risk"], 0.49)
        self.assertAlmostEqual(lim["daily"], 1.05)
        self.assertAlmostEqual(lim["floor"], 24.5)
        self.assertTrue(lim["pct"])
        self.assertAlmostEqual(m.effective_limits(100.0)["risk"], 1.4)           # a top-up scales the limits by itself

    def test_usd_values_are_used_when_no_percentage_is_set(self):
        self.pct()
        lim = m.effective_limits(35.0)
        self.assertEqual((lim["risk"], lim["daily"], lim["floor"], lim["pct"]), (m.RISK_USD, m.DAILY_LOSS_USD, m.EQUITY_FLOOR, False))
        self.assertEqual(m.effective_limits(None)["risk"], m.RISK_USD)          # unknown balance never means "no limit"

    def test_day_balance_is_captured_once_saved_and_restored(self):
        self.pct(2.0)
        r = m.Runner(False, "BTCUSD")
        r.step(NS(equity=35.0, balance=35.0))
        self.assertEqual(r.state.day_balance, 35.0)
        r.step(NS(equity=30.0, balance=30.0))                                   # later in the day: base does not move
        self.assertEqual(r.state.day_balance, 35.0)
        self.assertAlmostEqual(r.limits()["risk"], 0.70)
        again = m.Runner(False, "BTCUSD")                                       # restart the same day
        self.assertEqual(again.state.day_balance, 35.0)

    def test_new_day_remeasures_the_base(self):
        r = m.Runner(False, "BTCUSD")
        r.state.day, r.state.day_balance = "2000-01-01", 10.0
        r.step(NS(equity=35.0, balance=35.0))
        self.assertEqual(r.state.day_balance, 35.0)

    def test_percent_floor_and_daily_cap_block(self):
        self.pct(0.0, 3.0, 70.0)
        r = m.Runner(False, "BTCUSD")
        r.state.day_balance = 35.0
        r.state.pnl = -1.2                                                       # beyond 3% of 35 = 1.05
        self.assertEqual(r.blocked(NS(equity=33.0, balance=33.0)), "daily loss cap")
        r2 = m.Runner(False, "ETHUSD")
        r2.state.day_balance = 35.0
        self.assertEqual(r2.blocked(NS(equity=20.0, balance=20.0)), "equity floor")   # below 70% of 35 = 24.5

    def test_scan_uses_the_percentage_risk(self):
        self.pct(risk=100.0)                                                     # absurdly high on purpose: everything fits
        m.mt5 = FakeMT5([c * 800 for c in self.fake.closes])
        self.assertEqual(m.scan(print_rows=False), ["BTCUSD"])


class SessionTests(MT5Fixture):
    def at(self, h, mnt=0):
        return datetime.datetime(2026, 10, 21, h, mnt, tzinfo=datetime.timezone.utc)

    def test_window_boundaries(self):
        w = (12, 17)
        self.assertFalse(m.in_session(self.at(11, 59), w))
        self.assertTrue(m.in_session(self.at(12, 0), w))
        self.assertTrue(m.in_session(self.at(16, 59), w))
        self.assertFalse(m.in_session(self.at(17, 0), w))

    def test_wraps_past_midnight_and_empty_means_always(self):
        self.assertTrue(m.in_session(self.at(23), (22, 6)))
        self.assertTrue(m.in_session(self.at(3), (22, 6)))
        self.assertFalse(m.in_session(self.at(12), (22, 6)))
        self.assertTrue(m.in_session(self.at(3), None) if m.SESSION is None else True)

    def test_parse_and_reject_bad_values(self):
        self.assertEqual(m.parse_session("12-17"), (12, 17))
        self.assertIsNone(m.parse_session(""))
        for bad in ("noon", "12", "12-12", "25-3"):
            with self.assertRaises(SystemExit):
                m.parse_session(bad)

    def test_signal_outside_the_session_is_counted_and_not_traded(self):
        m.RISK_USD = 1000.0
        old = (m.SESSION, m.utc_now)
        m.SESSION, m.utc_now = (12, 17), lambda: self.at(3)
        self.addCleanup(lambda: (setattr(m, "SESSION", old[0]), setattr(m, "utc_now", old[1]), setattr(m, "RISK_USD", 0.5)))
        r = m.Runner(True, "BTCUSD")
        self.assertEqual(r.step(self.acc), "outside session")
        self.assertEqual(r.counts["skipped"], {"outside session": 1})
        self.assertEqual(self.fake.sent, [])                                     # orders are on, but nothing was sent
        m.utc_now = lambda: self.at(13)
        self.fake.bar_time += 900
        self.assertEqual(r.step(self.acc), "opened")                             # same signal inside the window trades


class PositionRowTests(MT5Fixture):
    def test_rows_for_the_dashboard(self):
        self.fake.positions = [
            NS(ticket=1, magic=m.MAGIC, type=1, volume=0.01, price_open=158.1, sl=158.2, tp=158.0, profit=-0.1, time=1792580000),
            NS(ticket=2, magic=999, type=0, volume=1.0, price_open=1.0, sl=0, tp=0, profit=0.0, time=1792580000),   # someone else's
        ]
        rows = m.position_rows([m.Runner(False, "BTCUSD")])
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["ticket"], rows[0]["direction"], rows[0]["sl"], rows[0]["tp"]), (1, "PUT", 158.2, 158.0))
        self.assertIsNotNone(rows[0]["opened_at"].tzinfo)

    def test_no_positions_gives_an_empty_list(self):
        self.assertEqual(m.position_rows([m.Runner(False, "BTCUSD")]), [])


class OrderSwitchTests(unittest.TestCase):
    def test_only_run_with_flag_places_orders(self):
        self.assertTrue(m.orders_enabled("run", {"MT5_ORDERS": "1"}))
        self.assertFalse(m.orders_enabled("run", {}))
        self.assertFalse(m.orders_enabled("run", {"MT5_ORDERS": "0"}))

    def test_signals_and_check_never_place_orders_even_if_the_flag_leaked(self):
        """A previous mt5live left MT5_ORDERS=1 in the CMD window; `signals` must still be harmless."""
        for cmd in ("signals", "check", "scan"):
            self.assertFalse(m.orders_enabled(cmd, {"MT5_ORDERS": "1", "ALLOW_REAL": "1"}), cmd)


class PathTests(unittest.TestCase):
    def test_candidate_paths_reads_utf16_and_utf8_and_skips_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            installs = {}
            for name, enc in (("A", "utf-16"), ("B", "utf-8"), ("C", "utf-8")):
                home = os.path.join(tmp, "installs", name)
                os.makedirs(home)
                if name != "C":                                   # C points at a folder with no terminal64.exe
                    open(os.path.join(home, "terminal64.exe"), "w").close()
                data = os.path.join(tmp, "MetaQuotes", "Terminal", name)
                os.makedirs(data)
                with open(os.path.join(data, "origin.txt"), "w", encoding=enc) as f:
                    f.write(home)
                installs[name] = home
            found = m.candidate_paths(tmp)
        self.assertEqual(sorted(os.path.basename(os.path.dirname(p)) for p in found), ["A", "B"])
        self.assertTrue(all(p.endswith("terminal64.exe") for p in found))


class BacktestTests(unittest.TestCase):
    def test_uptrend_after_cross_hits_target_and_costs_are_charged(self):
        import mt5_backtest as bt
        base = closes_with_call_cross()
        rows = [{"open": c, "high": c + 1, "low": c - 1, "close": c, "spread": 0} for c in base]
        for k in range(1, 60):                                 # strong rally after the cross
            c = base[-1] + 25 * k
            rows.append({"open": c - 25, "high": c + 1, "low": c - 26, "close": c, "spread": 0})
        free = bt.simulate(rows, m.FAST, m.SLOW, m.ATR_N, m.SL_ATR, m.RR, 0.01)
        self.assertTrue(free and free[0] == m.RR)
        costly = [dict(r, spread=100) for r in rows]
        paid = bt.simulate(costly, m.FAST, m.SLOW, m.ATR_N, m.SL_ATR, m.RR, 0.01)
        self.assertLess(paid[0], free[0])

    def test_summary_flags_no_edge(self):
        import mt5_backtest as bt
        s = bt.summarize([1.5, -1.0] * 50)                     # 50% wins at 1.5R is a real edge here
        self.assertGreater(s["mean"], 0)
        s0 = bt.summarize([1.0, -1.0] * 50)
        self.assertAlmostEqual(s0["mean"], 0.0)
        self.assertGreater(s0["p_edge"], 0.4)
