"""Offline tests for mt5_bot.py using a fake MetaTrader5 module (no terminal, no account)."""
import os, tempfile, unittest
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
