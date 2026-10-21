"""
MT5 bot for a Deriv MetaTrader 5 account (Windows only; needs the MT5 DESKTOP app installed and logged in).

  run.bat mt5scan     rank symbols: spread, min lot, and what a stop would risk at min lot. Places nothing.
  run.bat mt5check    connect, print the account, build the next order and validate it with order_check. Places nothing.
  run.bat mt5         same loop, but ONLY places orders when MT5_ORDERS=1 (and ALLOW_REAL=1 on a real account).

Rules, enforced in code:
  - every order has a stop-loss AND take-profit set at the broker, so it stays protected if the bot or PC dies
  - lot size comes from MT5_RISK_USD; the lot is rounded DOWN, and if even the minimum lot risks more, no trade
  - one open position at a time; daily loss cap; equity floor; max trades per day; spread filter
  - STOP file halts new orders; 3 failed orders in a row halts the bot
Strategy: SMA crossover on closed bars, ATR-based stop. NOT validated: run.bat mt5backtest first and read the p-value.
"""
import math, os, sys, time

os.environ["BOT_MODE"] = "mt5"                       # always: tags alerts, trades.csv rows, state file and dashboard tab

import deriv_bot as d

SYMBOL = os.getenv("MT5_SYMBOL", "auto")             # auto = best feasible from the scan
TF_NAME = os.getenv("MT5_TF", "M15")
FAST = int(os.getenv("MT5_FAST", "10"))
SLOW = int(os.getenv("MT5_SLOW", "30"))
ATR_N = int(os.getenv("MT5_ATR", "14"))
SL_ATR = float(os.getenv("MT5_SL_ATR", "1.5"))       # stop distance = SL_ATR x ATR
RR = float(os.getenv("MT5_RR", "1.5"))               # take-profit = RR x stop distance
RISK_USD = float(os.getenv("MT5_RISK_USD", "0.5"))   # max loss per trade if the stop is hit
DAILY_LOSS_USD = float(os.getenv("MT5_DAILY_LOSS_USD", "1.0"))
EQUITY_FLOOR = float(os.getenv("MT5_EQUITY_FLOOR", "5.0"))   # below this equity: stop for good
MAX_TRADES = int(os.getenv("MT5_MAX_TRADES", "6"))
MAX_SPREAD_FRAC = float(os.getenv("MT5_MAX_SPREAD_FRAC", "0.25"))   # spread must be <= this share of the stop distance
ALLOW_SYNTHETIC = os.getenv("MT5_ALLOW_SYNTHETIC") == "1"   # Boom/Crash/Volatility/Step: random by design, no edge possible
MAGIC = 20260101
POLL_S = 5
mt5 = None                                           # MetaTrader5 module, set by connect() (tests inject a fake)


def tf_const(name):
    return getattr(mt5, f"TIMEFRAME_{name}")


def candidate_paths(appdata=None):
    """terminal64.exe of every MT5 install, read from <APPDATA>\\MetaQuotes\\Terminal\\*\\origin.txt."""
    import glob
    root = os.path.join(appdata or os.getenv("APPDATA", ""), "MetaQuotes", "Terminal")
    out = []
    for f in glob.glob(os.path.join(root, "*", "origin.txt")):
        with open(f, "rb") as fh:
            raw = fh.read()
        text = raw.decode("utf-16" if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else "utf-8", errors="ignore").strip()
        exe = os.path.join(text, "terminal64.exe")
        if text and os.path.exists(exe) and exe not in out:
            out.append(exe)
    return out


def _init(path=None):
    return mt5.initialize(path=path, timeout=60000) if path else mt5.initialize(timeout=60000)


def connect():
    global mt5
    if mt5 is None:
        try:
            import MetaTrader5 as m
        except ImportError:
            raise SystemExit("MetaTrader5 package missing (Windows only): pip install MetaTrader5")
        mt5 = m
    explicit = os.getenv("MT5_PATH")
    ok = _init(explicit)
    if not ok and not explicit:                      # several installs: try each, keep the one logged in to Deriv
        for path in candidate_paths():
            mt5.shutdown()
            if _init(path) and "deriv" in str(getattr(mt5.account_info(), "server", "")).lower():
                print(f"using MT5 at {path}")
                ok = True
                break
    if not ok:
        raise SystemExit(
            f"Could not connect to the MT5 desktop app: {mt5.last_error()}.\n"
            "  1. Open MT5, log in to your Deriv account, and wait until it shows prices.\n"
            "  2. Run MT5 and this CMD window at the same privilege level (both normal, or both 'Run as administrator').\n"
            "  3. 64-bit Python needs 64-bit MT5 (terminal64.exe).\n"
            "  4. Still failing? Print your install paths with:  type %APPDATA%\\MetaQuotes\\Terminal\\*\\origin.txt\n"
            "     then set MT5_PATH=<that folder>\\terminal64.exe  (real text, no < > brackets)")
    acc = mt5.account_info()
    if acc is None:
        raise SystemExit("MT5 is running but not logged in to an account.")
    return acc


def is_real(acc):
    return getattr(acc, "trade_mode", 0) == 2


def atr(highs, lows, closes, n):
    trs = [max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])) for i in range(1, len(closes))]
    return sum(trs[-n:]) / n if len(trs) >= n else None


def floor_step(x, step):
    return math.floor(round(x / step, 9)) * step


def plan_order(si, tick, direction, atr_value, risk_usd, order_calc_profit):
    """Return (plan, None) or (None, reason). Pure: all market data is passed in."""
    buy = direction == "CALL"
    entry = tick.ask if buy else tick.bid
    min_dist = (si.trade_stops_level + 2) * si.point
    dist = max(atr_value * SL_ATR, min_dist)
    spread = tick.ask - tick.bid
    if spread > MAX_SPREAD_FRAC * dist:
        return None, f"spread {spread:.5g} is more than {MAX_SPREAD_FRAC:.0%} of the {dist:.5g} stop"
    sl = entry - dist if buy else entry + dist
    tp = entry + dist * RR if buy else entry - dist * RR
    per_lot = order_calc_profit(0 if buy else 1, si.name, 1.0, entry, sl)
    per_lot = abs(per_lot) if per_lot else dist * si.trade_contract_size
    lot = floor_step(risk_usd / per_lot, si.volume_step)
    if lot < si.volume_min:
        return None, f"min lot {si.volume_min} would risk {si.volume_min * per_lot:.2f} USD, above your {risk_usd:.2f} USD limit"
    lot = min(lot, si.volume_max)
    return {"buy": buy, "entry": entry, "sl": round(sl, si.digits), "tp": round(tp, si.digits), "lot": round(lot, 8),
            "risk": lot * per_lot, "dist": dist}, None


def fill_mode(si):
    f = getattr(si, "filling_mode", 0)
    return mt5.ORDER_FILLING_IOC if f & 2 else mt5.ORDER_FILLING_FOK if f & 1 else mt5.ORDER_FILLING_RETURN


def request_for(si, plan):
    return {"action": mt5.TRADE_ACTION_DEAL, "symbol": si.name, "volume": plan["lot"],
            "type": mt5.ORDER_TYPE_BUY if plan["buy"] else mt5.ORDER_TYPE_SELL, "price": plan["entry"],
            "sl": plan["sl"], "tp": plan["tp"], "deviation": 20, "magic": MAGIC, "comment": "derivusna",
            "type_time": mt5.ORDER_TIME_GTC, "type_filling": fill_mode(si)}


def bars(symbol, n):
    rates = mt5.copy_rates_from_pos(symbol, tf_const(TF_NAME), 1, n)      # offset 1 = closed bars only
    if rates is None or len(rates) < n:
        return None
    return rates


def is_synthetic(si):
    text = f"{getattr(si, 'path', '')} {getattr(si, 'name', '')}".lower()
    return any(w in text for w in ("derived", "synthetic", "volatility", "boom", "crash", "step ", "jump", "range break", "dex "))


def symbol_view(symbol):
    """(si, tick, atr, closes) for one symbol, or None if unusable."""
    if not mt5.symbol_select(symbol, True):
        return None
    si, tick = mt5.symbol_info(symbol), mt5.symbol_info_tick(symbol)
    r = bars(symbol, max(SLOW, ATR_N) + 5)
    if si is None or tick is None or r is None or si.trade_mode != 4:      # 4 = full trading allowed
        return None
    a = atr([x["high"] for x in r], [x["low"] for x in r], [x["close"] for x in r], ATR_N)
    return (si, tick, a, [float(x["close"]) for x in r]) if a else None


def scan(print_rows=True):
    """Rank every crypto symbol in Market Watch. Returns feasible symbols best-first."""
    out = []
    for s in mt5.symbols_get() or []:
        if not (s.visible and s.trade_mode == 4) or (is_synthetic(s) and not ALLOW_SYNTHETIC):
            continue
        v = symbol_view(s.name)
        if not v:
            continue
        si, tick, a, _ = v
        mid = (tick.ask + tick.bid) / 2
        dist = max(a * SL_ATR, (si.trade_stops_level + 2) * si.point)
        per_lot = abs(mt5.order_calc_profit(0, s.name, 1.0, tick.ask, tick.ask - dist) or dist * si.trade_contract_size)
        out.append({"symbol": s.name, "spread_pct": (tick.ask - tick.bid) / mid * 100, "min_lot": si.volume_min,
                    "risk_at_min": si.volume_min * per_lot, "spread_vs_stop": (tick.ask - tick.bid) / dist})
    ok = sorted([x for x in out if x["risk_at_min"] <= RISK_USD and x["spread_vs_stop"] <= MAX_SPREAD_FRAC],
                key=lambda x: x["spread_pct"])
    if print_rows:
        print(f"{'symbol':10} {'spread%':>8} {'min lot':>8} {'risk@min lot':>13} {'spread/stop':>12}  (limit {RISK_USD:.2f} USD, {TF_NAME} bars)")
        for x in sorted(out, key=lambda x: (x not in ok, x["spread_pct"])):
            print(f"{x['symbol']:10} {x['spread_pct']:8.3f} {x['min_lot']:8g} {x['risk_at_min']:13.2f} {x['spread_vs_stop']:12.2f}  "
                  f"{'OK' if x in ok else 'too big for your risk limit' if x['risk_at_min'] > RISK_USD else 'spread too wide'}")
    return [x["symbol"] for x in ok]


class Runner:
    def __init__(self, orders_enabled, symbol):
        self.orders = orders_enabled
        self.symbol = symbol
        self.state = d.Bot(None)                     # reused as the persisted-state container (pnl, trades, day)
        saved = d.load_state()
        self.state.__dict__.update({k: v for k, v in saved.items() if k != "open_contracts"})
        self.state.open_contracts = saved.get("open_contracts", {})
        self.last_bar = 0
        self.fails = 0
        self.halted = False
        self.warned = set()

    def warn_once(self, key, text):
        if key not in self.warned:
            self.warned.add(key)
            d.alert(text)

    def day_reset(self):
        today = time.strftime("%Y-%m-%d")
        if self.state.day != today:
            self.state.day, self.state.pnl, self.state.trades, self.state.limit_alerted = today, 0.0, 0, False
            self.warned.clear()
            d.save_state(self.state)

    def positions(self):
        return [p for p in (mt5.positions_get(symbol=self.symbol) or []) if p.magic == MAGIC]

    def settle_closed(self):
        """A position we opened is gone: book its realised result (profit + commission + swap)."""
        pos = {p.ticket for p in self.positions()}
        for ticket in [t for t in self.state.open_contracts if t not in pos]:
            direction, entry, risk = self.state.open_contracts.pop(ticket)
            deals = mt5.history_deals_get(position=ticket) or []
            pnl = sum(x.profit + x.commission + x.swap + getattr(x, "fee", 0.0) for x in deals)
            self.state.pnl += pnl
            self.state.log([time.strftime("%H:%M:%S"), "mt5", self.symbol, direction, round(risk, 2), round(pnl, 2),
                            round(self.state.pnl, 2), entry, None], ticket)
            d.save_state(self.state)
            d.alert(f"{direction} closed {pnl:+.2f} USD, day {self.state.pnl:+.2f}")

    def blocked(self, acc):
        if self.halted:
            return "halted"
        if os.path.exists(d.STOP_FILE):
            return "STOP file present"
        if acc.equity < EQUITY_FLOOR:
            self.halted = True
            d.alert(f"equity {acc.equity:.2f} below floor {EQUITY_FLOOR:.2f}: bot halted for good. Delete state and restart to resume.")
            return "equity floor"
        open_pnl = sum(p.profit for p in self.positions())
        if self.state.pnl + open_pnl <= -DAILY_LOSS_USD:
            self.warn_once("daily", f"daily loss cap hit ({self.state.pnl + open_pnl:+.2f} USD): no new trades today.")
            return "daily loss cap"
        if self.state.trades >= MAX_TRADES:
            self.warn_once("maxtr", f"max {MAX_TRADES} trades today reached.")
            return "max trades"
        if self.positions():
            return "position open"
        return None

    def step(self, acc, check_only=False):
        self.day_reset()
        self.settle_closed()
        why = self.blocked(acc)
        if why and not check_only:
            return why
        v = symbol_view(self.symbol)
        if not v:
            return "no market data"
        si, tick, a, closes = v
        bar = int(mt5.copy_rates_from_pos(self.symbol, tf_const(TF_NAME), 1, 1)[0]["time"])
        if bar == self.last_bar and not check_only:
            return "waiting for next bar"
        self.last_bar = bar
        direction = d.signal(closes, FAST, SLOW)
        if not direction and not check_only:
            return "no signal"
        direction = direction or "CALL"             # check mode: validate an order even without a signal
        plan, reason = plan_order(si, tick, direction, a, RISK_USD, mt5.order_calc_profit)
        if not plan:
            self.warn_once(f"skip-{reason[:20]}", f"signal {direction} skipped: {reason}")
            return "skipped"
        req = request_for(si, plan)
        chk = mt5.order_check(req)
        if chk is None or chk.retcode != 0:
            self.warn_once("chk", f"order_check rejected: {getattr(chk, 'comment', mt5.last_error())}")
            return "order_check failed"
        desc = f"{direction} {self.symbol} lot {plan['lot']} @ {plan['entry']} SL {plan['sl']} TP {plan['tp']} (risk {plan['risk']:.2f} USD)"
        if check_only or not self.orders:
            print(f"[validated, NOT sent] {desc}")
            return "validated"
        res = mt5.order_send(req)
        if res is None or res.retcode != mt5.TRADE_RETCODE_DONE:
            self.fails += 1
            d.alert(f"order failed ({getattr(res, 'retcode', None)} {getattr(res, 'comment', '')}); {self.fails}/3")
            if self.fails >= 3:
                self.halted = True
                d.alert("3 failed orders in a row: bot halted.")
            return "order failed"
        self.fails = 0
        self.state.trades += 1
        self.state.open_contracts[res.order] = (direction, plan["entry"], plan["risk"])
        d.save_state(self.state)
        d.alert(f"OPENED {desc}")
        return "opened"


def orders_enabled(cmd, env=None):
    """Orders only for `run` with MT5_ORDERS=1. `signals` and `check` never place orders, whatever the environment says."""
    env = os.environ if env is None else env
    return cmd == "run" and env.get("MT5_ORDERS") == "1"


def diag():
    """Print everything that decides whether Python can attach to MT5. Places nothing, shows no secrets."""
    import platform, struct, subprocess
    print(f"python {platform.python_version()} {struct.calcsize('P') * 8}-bit on {platform.system()} {platform.release()}")
    try:
        import ctypes
        print("this CMD is administrator:", bool(ctypes.windll.shell32.IsUserAnAdmin()))
    except Exception:                                # noqa: BLE001 - non-Windows
        pass
    try:
        import MetaTrader5 as lib
    except ImportError:
        raise SystemExit("MetaTrader5 package missing: pip install MetaTrader5")
    print("MetaTrader5 package", getattr(lib, "__version__", "?"), "| author", getattr(lib, "__author__", "?"))
    try:
        out = subprocess.run(["tasklist", "/fi", "imagename eq terminal64.exe", "/v"], capture_output=True, text=True).stdout
        print("running terminal64 processes:\n" + (out.strip() or "(none)"))
    except Exception as e:                           # noqa: BLE001
        print("tasklist failed:", e)
    paths = ([os.environ["MT5_PATH"]] if os.getenv("MT5_PATH") else []) + candidate_paths()
    print("install paths found:", paths or "NONE (no origin.txt under %APPDATA%\\MetaQuotes\\Terminal)")
    for p in [None] + paths:
        lib.shutdown()
        ok = lib.initialize(path=p, timeout=20000) if p else lib.initialize(timeout=20000)
        print(f"initialize({p or 'default'}) -> {ok}  last_error={lib.last_error()}")
        if ok:
            ti, ai = lib.terminal_info(), lib.account_info()
            print("  terminal:", getattr(ti, "name", "?"), "build", getattr(ti, "build", "?"), "| connected:", getattr(ti, "connected", "?"),
                  "| trade_allowed:", getattr(ti, "trade_allowed", "?"))
            print("  account:", getattr(ai, "login", None), getattr(ai, "server", None), "balance", getattr(ai, "balance", None))
            break
    lib.shutdown()


def main(argv):
    if len(argv) > 1 and argv[1] == "diag":
        return diag()
    cmd = argv[1] if len(argv) > 1 else "check"
    if cmd not in ("scan", "check", "run", "signals"):
        raise SystemExit("usage: mt5_bot.py scan | check | signals | run   (only `run` can place orders, and only with MT5_ORDERS=1)")
    acc = connect()
    real = is_real(acc)
    print(f"account {acc.login} {'REAL' if real else 'DEMO'}, balance {acc.balance:.2f} {acc.currency}, equity {acc.equity:.2f}")
    if cmd == "scan":
        scan()
        return
    symbol = SYMBOL
    if symbol != "auto":
        si = mt5.symbol_info(symbol)
        if si is not None and is_synthetic(si) and not ALLOW_SYNTHETIC:
            raise SystemExit(f"{symbol} is a synthetic index: a random-number product with no exploitable pattern, so this "
                             "strategy cannot have an edge there. Set MT5_ALLOW_SYNTHETIC=1 only if you accept that.")
    if symbol == "auto":
        ranked = scan(print_rows=False)
        if not ranked:
            scan()
            raise SystemExit("\nNo symbol fits your risk limit at its minimum lot. Raise MT5_RISK_USD, use a shorter MT5_TF, "
                             "or add smaller-notional symbols to Market Watch. Nothing was traded.")
        symbol = ranked[0]
        print(f"auto-selected {symbol} (lowest spread among symbols that fit {RISK_USD:.2f} USD risk)")
    orders = orders_enabled(cmd)
    if orders and real and os.getenv("ALLOW_REAL") != "1":
        raise SystemExit("Real account: set ALLOW_REAL=1 as well as MT5_ORDERS=1.")
    r = Runner(orders, symbol)
    if cmd == "check":
        print("result:", r.step(acc, check_only=True))
        return
    print(f"{'PLACING REAL ORDERS' if orders and real else 'PLACING ORDERS (demo)' if orders else 'SIGNALS ONLY, no orders'} on {symbol} {TF_NAME}, "
          f"risk {RISK_USD:.2f} USD/trade, daily cap {DAILY_LOSS_USD:.2f} USD. Ctrl+C to stop.")
    d.alert(f"mt5 started on {symbol} ({'REAL' if real else 'DEMO'}, orders {'ON' if orders else 'OFF'})")
    while True:
        try:
            acc = mt5.account_info()
            r.step(acc)
            r.state.heartbeat()
        except Exception as e:                       # noqa: BLE001 - keep the loop alive, broker-side stops protect open trades
            d.alert(f"mt5 loop error: {e}")
        time.sleep(POLL_S)


if __name__ == "__main__":
    try:
        main(sys.argv)
    except KeyboardInterrupt:
        print("stopped")
