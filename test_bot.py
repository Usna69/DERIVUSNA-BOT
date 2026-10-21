"""Offline tests (no network, no account):  python -m unittest -v"""
import asyncio, os, random, tempfile, time, unittest
import backtest
import deriv_bot as d


class Isolated(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = (d.LOG, d.STATE_FILE, d.STOP_FILE)
        d.LOG = os.path.join(self.tmp.name, "trades.csv")
        d.STATE_FILE = os.path.join(self.tmp.name, "state.json")
        d.STOP_FILE = os.path.join(self.tmp.name, "STOP")
        d.send_telegram = lambda text: None

    def tearDown(self):
        d.LOG, d.STATE_FILE, d.STOP_FILE = self.saved
        self.tmp.cleanup()


class SignalTests(unittest.TestCase):
    def test_call_on_upward_cross(self):
        ticks = [10.0] * 30 + [11.0, 12.0, 13.0]
        hits = [d.signal(ticks[:i], 3, 10) for i in range(11, len(ticks) + 1)]
        self.assertIn("CALL", hits)
        self.assertNotIn("PUT", hits)

    def test_put_on_downward_cross(self):
        ticks = [10.0] * 30 + [9.0, 8.0, 7.0]
        self.assertIn("PUT", [d.signal(ticks[:i], 3, 10) for i in range(11, len(ticks) + 1)])

    def test_needs_enough_ticks(self):
        self.assertIsNone(d.signal([1.0] * 5, 3, 10))

    def test_fast_backtest_matches_signal(self):
        """backtest.simulate uses prefix sums; it must pick exactly the trades signal() would."""
        random.seed(1)
        px, x = [], 100.0
        for _ in range(3000):
            x += random.gauss(0, 1)
            px.append(round(x, 2))
        rows = [(1_700_000_000 + 2 * i, p) for i, p in enumerate(px)]
        fast, slow, dur = 5, 20, 3
        wins = n = 0
        i = slow
        while i < len(px) - dur:
            direction = d.signal(px[i - slow:i + 1], fast, slow)
            if direction:
                win = px[i + dur] > px[i] if direction == "CALL" else px[i + dur] < px[i]
                wins, n, i = wins + win, n + 1, i + dur
            i += 1
        r = backtest.simulate(rows, fast, slow, dur, 0.95, limits=False)
        self.assertEqual((r["wins"], r["n"]), (wins, n))
        self.assertGreater(n, 20)


class StatsTests(unittest.TestCase):
    def test_coin_flip_is_not_an_edge(self):
        s = backtest.stats(500, 1000, 0.95)
        self.assertGreater(s["p"], 0.5)
        self.assertTrue(s["lo"] < 0.5 < s["hi"])

    def test_real_edge_is_detected(self):
        s = backtest.stats(620, 1000, 0.95)
        self.assertLess(s["p"], 0.001)

    def test_breakeven(self):
        self.assertAlmostEqual(backtest.breakeven(0.92), 0.5208, places=3)


class LimitTests(Isolated):
    def make(self):
        bot = d.Bot(None)
        bot.last_trade = 0
        return bot

    def test_daily_loss_limit_blocks_and_alerts_once(self):
        bot, sent = self.make(), []
        orig, d.alert = d.alert, sent.append
        try:
            bot.pnl = -d.MAX_DAILY_LOSS
            self.assertFalse(bot.allowed())
            self.assertFalse(bot.allowed())
        finally:
            d.alert = orig
        self.assertEqual(len(sent), 1)

    def test_trade_cap(self):
        bot = self.make()
        bot.trades = d.MAX_TRADES_PER_DAY
        self.assertFalse(bot.allowed())

    def test_new_day_resets(self):
        bot = self.make()
        bot.day, bot.pnl, bot.trades = "2000-01-01", -99.0, 99
        self.assertTrue(bot.allowed())
        self.assertEqual((bot.pnl, bot.trades), (0.0, 0))

    def test_stop_file_halts_trading(self):
        bot = self.make()
        open(d.STOP_FILE, "w").close()
        self.assertFalse(bot.allowed())
        os.remove(d.STOP_FILE)
        self.assertTrue(bot.allowed())


class StateTests(Isolated):
    def test_roundtrip_keeps_today_and_open_contracts(self):
        bot = d.Bot(None)
        bot.pnl, bot.trades, bot.last_trade = -2.5, 4, 123.0
        bot.open_contracts = {77: ("CALL", 1.0, 0.92)}
        d.save_state(bot)
        s = d.load_state()
        self.assertEqual((s["pnl"], s["trades"]), (-2.5, 4))
        self.assertEqual(s["open_contracts"], {77: ("CALL", 1.0, 0.92)})

    def test_old_day_counters_dropped_but_contracts_kept(self):
        bot = d.Bot(None)
        bot.day, bot.pnl, bot.open_contracts = "2000-01-01", -4.0, {5: ("PUT", 1.0, 0.9)}
        d.save_state(bot)
        s = d.load_state()
        self.assertNotIn("pnl", s)
        self.assertIn(5, s["open_contracts"])

    def test_missing_or_corrupt_file(self):
        self.assertEqual(d.load_state(), {})
        with open(d.STATE_FILE, "w") as f:
            f.write("{not json")
        self.assertEqual(d.load_state(), {})


class LogTests(Isolated):
    def test_old_header_rotated_and_new_row_written(self):
        with open(d.LOG, "w") as f:
            f.write("time,mode\nx,y\n")
        d.Bot(None).log(["t"] * len(d.HEADER))
        self.assertEqual(open(d.LOG).readline().strip().split(",")[-1], "payout_ratio")
        self.assertTrue(any(n.startswith("trades_old_") for n in os.listdir(self.tmp.name)))

    def test_locked_file_does_not_raise(self):
        """Windows raised WinError 32 here and killed the trade; log() must swallow OSError."""
        with open(d.LOG, "w") as f:
            f.write("old,header\n")
        real = os.replace
        os.replace = lambda *a: (_ for _ in ()).throw(PermissionError("locked"))
        try:
            d.Bot(None).log(["t"] * len(d.HEADER))
        finally:
            os.replace = real


class SettleTests(Isolated):
    def run_async(self, coro):
        async def fast(_):
            pass
        real = d.asyncio.sleep
        d.asyncio.sleep = fast
        try:
            return asyncio.run(coro)
        finally:
            d.asyncio.sleep = real

    def test_settle_books_profit_once_and_logs_payout(self):
        bot = d.Bot(None)
        bot.open_contracts = {9: ("PUT", 1.0, 0.92)}

        async def call(p):
            return {"proposal_open_contract": {"is_sold": 1, "profit": "0.92"}}
        bot.call = call
        self.run_async(bot.settle(9))
        self.run_async(bot.settle(9))                # second settle must not double count
        self.assertAlmostEqual(bot.pnl, 0.92)
        self.assertEqual(bot.open_contracts, {})
        with open(d.LOG) as f:
            lines = f.read().strip().splitlines()
        self.assertEqual(len(lines), 2)              # header + one row
        self.assertTrue(lines[1].endswith("1.0,0.92"))
        self.assertEqual(d.load_state()["pnl"], 0.92)


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(os.getenv("TEST_DATABASE_URL"), "set TEST_DATABASE_URL to a scratch Postgres to run")
class DbTests(unittest.TestCase):
    def test_init_creates_tables(self):
        import db, psycopg
        db.DB_URL = os.environ["TEST_DATABASE_URL"]
        with psycopg.connect(db.DB_URL, autocommit=True) as c:
            for tbl in ("trades", "events", "bot_status"):
                c.execute(f"drop table if exists {tbl}")
        db.init()
        with psycopg.connect(db.DB_URL) as c:
            self.assertEqual(c.execute("select count(*) from information_schema.tables where table_name in ('trades','events','bot_status')").fetchone()[0], 3)

    def test_roundtrip_and_never_raises(self):
        import db, psycopg
        db.DB_URL, db._ready = os.environ["TEST_DATABASE_URL"], False
        db.insert_trade("test", "R_100", "CALL", 1, 0.92, 0.92, 1.0, 0.92, 42)
        db.event("test", "info", "hello")
        db.heartbeat("test", "R_100", 0.92, 1, "running")
        db.heartbeat("test", "R_100", 1.5, 2, "limit")      # upsert, not a second row
        with psycopg.connect(db.DB_URL) as c:
            self.assertEqual(c.execute("select count(*) from trades where mode='test'").fetchone()[0], 1)
            self.assertEqual(c.execute("select trades_today, status from bot_status where mode='test'").fetchone(), (2, "limit"))
            for t in ("trades", "events", "bot_status"):
                c.execute(f"delete from {t} where mode='test'")
        db.DB_URL = "postgresql://nobody@127.0.0.1:1/none"   # unreachable: must swallow the error
        db.insert_trade("test", "R_100", "CALL", 1, 0, 0)
