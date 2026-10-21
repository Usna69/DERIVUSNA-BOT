"""
Simple Deriv bot: SMA crossover on ticks, fixed stake, hard loss limits.

Modes (env BOT_MODE):
  paper  (default) - no account, no token. Watches live ticks and simulates trades.
  demo             - places real contracts on a DEMO (virtual) account. Refuses a real account.
  real             - real money. Also needs ALLOW_REAL=1. Do not use until paper and demo
                     have run for weeks and you accept you can lose the whole stake.

Setup:  pip install websockets
Trading needs two values from developers.deriv.com (log in, then create an app and a
        Personal Access Token with the "trade" scope only, never payments/admin):
          DERIV_TOKEN   = the Personal Access Token
          DERIV_APP_ID  = your app id
        Put them in the environment, never in chat or code. Paper mode needs neither.
Run:    python deriv_bot.py

Uses Deriv's current API (developers.deriv.com): public tick feed with no login; trading
via a one-time-password WebSocket URL issued by the REST API.
This is an experiment, not a money machine: most short-term traders lose.
"""
import asyncio, csv, json, os, time, urllib.error, urllib.request
from collections import deque
import websockets
import db

MODE = os.getenv("BOT_MODE", "paper")
APP_ID = os.getenv("DERIV_APP_ID", "")
TOKEN = os.getenv("DERIV_TOKEN", "")
API = "https://api.derivws.com"
PUBLIC_WS = "wss://api.derivws.com/trading/v1/options/ws/public"

SYMBOL = os.getenv("SYMBOL", "R_100")               # Volatility 100 Index
STAKE = float(os.getenv("STAKE", "1"))              # fixed, never increased after a loss
DURATION_TICKS = int(os.getenv("DURATION_TICKS", "5"))
FAST = int(os.getenv("FAST", "10"))                 # SMA lengths in ticks
SLOW = int(os.getenv("SLOW", "30"))
COOLDOWN_S = int(os.getenv("COOLDOWN_S", "30"))     # min seconds between trades
STALE_S = int(os.getenv("STALE_S", "30"))           # no message for this long = dead feed, reconnect
STATE_FILE = f"state_{MODE}.json"                   # survives crashes/restarts: day P&L, trade count, open contracts
STOP_FILE = "STOP"                                  # create this file to halt new trades
MAX_TRADES_PER_DAY = int(os.getenv("MAX_TRADES", "20"))
MAX_DAILY_LOSS = float(os.getenv("MAX_DAILY_LOSS", "5"))   # stop for the day at this loss
PAPER_PAYOUT = 0.95                                 # assumed payout ratio in paper mode
LOG = "trades.csv"
HEADER = ["time", "mode", "symbol", "direction", "stake", "profit", "day_pnl", "ask_price", "payout_ratio"]
TG_TOKEN = os.getenv("TELEGRAM_TOKEN", "")          # optional alerts; get a token from @BotFather
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")


def send_telegram(text):
    """Best-effort alert. Never raises, so a Telegram outage cannot stop the bot."""
    if not (TG_TOKEN and TG_CHAT):
        return
    try:
        body = json.dumps({"chat_id": TG_CHAT, "text": text}).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data=body,
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10).close()
    except Exception as e:
        print("telegram failed:", e.__class__.__name__)


def bg(fn, *args):
    """Run a blocking helper (Telegram, Postgres) off the event loop; best effort."""
    try:
        asyncio.get_running_loop().create_task(asyncio.to_thread(fn, *args))
    except RuntimeError:                             # no running loop
        fn(*args)


def alert(text):
    print(f"[alert] {text}")
    if db.enabled():
        bg(db.event, MODE, "error" if any(w in text for w in ("error", "lost", "STOP", "limit")) else "info", text)
    try:
        asyncio.get_running_loop().create_task(asyncio.to_thread(send_telegram, f"{MODE}: {text}"))
    except RuntimeError:                             # no running loop
        send_telegram(f"{MODE}: {text}")


def sma(xs, n):
    return sum(list(xs)[-n:]) / n


def signal(ticks, fast=None, slow=None):
    """CALL on a fast-over-slow SMA cross, PUT on the reverse, else None. Needs slow+1 ticks."""
    fast, slow = fast or FAST, slow or SLOW
    t = list(ticks)
    if len(t) < slow + 1:
        return None
    prev = t[:-1]
    f0, s0, f1, s1 = sma(prev, fast), sma(prev, slow), sma(t, fast), sma(t, slow)
    return "CALL" if (f0 <= s0 and f1 > s1) else "PUT" if (f0 >= s0 and f1 < s1) else None


class ApiError(RuntimeError):
    pass


def save_state(bot):
    """Atomic write so a crash mid-save cannot corrupt the file."""
    data = {"pnl": bot.pnl, "trades": bot.trades, "last_trade": bot.last_trade, "day": bot.day,
            "open_contracts": {str(k): list(v) for k, v in bot.open_contracts.items()}}
    try:
        with open(STATE_FILE + ".tmp", "w") as f:
            json.dump(data, f)
        os.replace(STATE_FILE + ".tmp", STATE_FILE)
    except OSError as e:
        print("could not save state:", e)


def load_state():
    """Restore counters (only for today) and any contracts left open. Returns {} if none."""
    try:
        with open(STATE_FILE) as f:
            d = json.load(f)
    except (OSError, ValueError):
        return {}
    state = {"open_contracts": {int(k): tuple(v) for k, v in d.get("open_contracts", {}).items()}}
    if d.get("day") == time.strftime("%Y-%m-%d"):
        state.update({k: d[k] for k in ("pnl", "trades", "last_trade", "day") if k in d})
    return state


class Bot:
    def __init__(self, ws, open_contracts=None):
        self.ws = ws
        self.open_contracts = open_contracts if open_contracts is not None else {}   # cid -> direction
        self.req = 0
        self.pending = {}
        self.ticks = deque(maxlen=SLOW + 2)
        self.pnl = 0.0
        self.trades = 0
        self.last_trade = 0.0
        self.open_paper = None                      # (direction, entry, ticks_left)
        self.busy = False
        self.day = time.strftime("%Y-%m-%d")
        self.limit_alerted = False
        self.stop_alerted = False
        self.last_hb = 0.0
        self.resumed = False                        # True after a reconnect, to skip the start alert

    async def call(self, payload):
        self.req += 1
        payload["req_id"] = self.req
        fut = asyncio.get_event_loop().create_future()
        self.pending[self.req] = fut
        await self.ws.send(json.dumps(payload))
        res = await asyncio.wait_for(fut, 20)
        if res.get("error"):
            raise ApiError(res["error"].get("message"))
        return res

    async def reader(self):
        while True:
            raw = await asyncio.wait_for(self.ws.recv(), STALE_S)     # silent feed -> TimeoutError -> reconnect
            m = json.loads(raw)
            fut = self.pending.pop(m.get("req_id"), None)
            if fut and not fut.done():
                fut.set_result(m)
            if m.get("msg_type") == "tick":
                await self.on_tick(float(m["tick"]["quote"]))

    def log(self, row, cid=None):
        """Append to trades.csv. Never raises: a locked file (e.g. open in Excel) must not break a live trade."""
        try:
            if os.path.exists(LOG):                  # old column layout: keep it, start a fresh log
                with open(LOG, newline="") as f:
                    header = next(csv.reader(f), None)
                if header != HEADER:                 # file closed before renaming; Windows cannot rename an open file
                    os.replace(LOG, os.path.join(os.path.dirname(LOG), f"trades_old_{int(time.time())}.csv"))
            new = not os.path.exists(LOG)
            with open(LOG, "a", newline="") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(HEADER)
                w.writerow(row)
        except OSError as e:
            print(f"could not write {LOG} ({e}); close it in Excel. Row: {row}")
        if db.enabled():
            _t, mode, symbol, direction, stake, profit, day_pnl, ask, ratio = row
            bg(db.insert_trade, mode, symbol, direction, stake, profit, day_pnl, ask, ratio, cid)

    def allowed(self):
        today = time.strftime("%Y-%m-%d")
        if today != self.day:                        # new day: reset limits
            self.day, self.pnl, self.trades, self.limit_alerted = today, 0.0, 0, False
            save_state(self)
        if os.path.exists(STOP_FILE):
            if not self.stop_alerted:
                self.stop_alerted = True
                alert("STOP file found: no new trades until it is deleted.")
            return False
        self.stop_alerted = False
        if self.pnl <= -MAX_DAILY_LOSS or self.trades >= MAX_TRADES_PER_DAY:
            if not self.limit_alerted:
                self.limit_alerted = True
                alert(f"daily limit hit: pnl {self.pnl:+.2f}, trades {self.trades}. Pausing until tomorrow.")
            return False
        return time.time() - self.last_trade >= COOLDOWN_S

    def heartbeat(self):
        """Tell the dashboard we are alive (every 30s)."""
        if not db.enabled() or time.time() - self.last_hb < 30:
            return
        self.last_hb = time.time()
        status = "stopped" if os.path.exists(STOP_FILE) else "limit" if self.limit_alerted else "running"
        bg(db.heartbeat, MODE, SYMBOL, round(self.pnl, 2), self.trades, status)

    async def on_tick(self, price):
        self.ticks.append(price)
        self.heartbeat()
        if self.open_paper:                          # settle a paper trade
            d, entry, left = self.open_paper
            left -= 1
            if left > 0:
                self.open_paper = (d, entry, left)
            else:
                win = price > entry if d == "CALL" else price < entry
                profit = STAKE * PAPER_PAYOUT if win else -STAKE
                self.pnl += profit
                self.log([time.strftime("%H:%M:%S"), MODE, SYMBOL, d, STAKE, round(profit, 2), round(self.pnl, 2), "", PAPER_PAYOUT])
                save_state(self)
                alert(f"paper {d} {'WIN' if win else 'LOSS'} {profit:+.2f}, day {self.pnl:+.2f}")
                self.open_paper = None
            return
        if len(self.ticks) < SLOW + 1 or self.busy or not self.allowed():
            return
        direction = signal(self.ticks)
        if not direction:
            return
        self.trades += 1
        self.last_trade = time.time()
        save_state(self)
        if MODE == "paper":
            self.open_paper = (direction, price, DURATION_TICKS)
            print(f"paper {direction} at {price}")
        else:
            self.busy = True
            asyncio.create_task(self.trade(direction))

    async def trade(self, direction):
        try:
            p = await self.call({"proposal": 1, "amount": STAKE, "basis": "stake", "contract_type": direction,
                                 "currency": self.currency, "duration": DURATION_TICKS,
                                 "duration_unit": "t", "underlying_symbol": SYMBOL})
            prop = p["proposal"]
            ask = float(prop["ask_price"])
            ratio = round((float(prop["payout"]) - ask) / ask, 4)   # real profit ratio on a win
            b = await self.call({"buy": prop["id"], "price": prop["ask_price"]})
            cid = b["buy"]["contract_id"]
            print(f"{MODE} {direction} bought, contract {cid}, ask {ask}, payout {ratio:.1%}")
            self.open_contracts[cid] = (direction, ask, ratio)
            save_state(self)
            await self.settle(cid)
        except Exception as e:
            alert(f"trade error: {e}")
        finally:
            self.busy = False

    async def settle(self, cid):
        """Poll a contract until sold and book the profit once. Survives reconnects via open_contracts."""
        for _ in range(60):
            await asyncio.sleep(2)
            c = (await self.call({"proposal_open_contract": 1, "contract_id": cid}))["proposal_open_contract"]
            if c.get("is_sold"):
                info = self.open_contracts.pop(cid, None)
                if info is None:                     # already booked
                    return
                direction, ask, ratio = info
                profit = float(c["profit"])
                self.pnl += profit
                self.log([time.strftime("%H:%M:%S"), MODE, SYMBOL, direction, STAKE, profit, round(self.pnl, 2), ask, ratio], cid)
                save_state(self)
                alert(f"{direction} {'WIN' if profit > 0 else 'LOSS'} {profit:+.2f}, day {self.pnl:+.2f}")
                return

    async def resume(self, cid):
        try:
            await self.settle(cid)
        except Exception as e:
            print("resume error:", e)

    async def run(self):
        reader = asyncio.create_task(self.reader())
        await self.call({"ticks": SYMBOL, "subscribe": 1})
        for cid in list(self.open_contracts):        # contracts left open by a dropped connection
            print(f"resuming contract {cid}")
            asyncio.create_task(self.resume(cid))
        print(f"{MODE} mode on {SYMBOL}, stake {STAKE}, daily loss limit {MAX_DAILY_LOSS}. Ctrl+C to stop.")
        if not self.resumed:
            alert(f"started on {SYMBOL}, stake {STAKE}, daily loss limit {MAX_DAILY_LOSS}")
        await reader


def rest(method, path):
    h = {"Authorization": f"Bearer {TOKEN}", "Deriv-App-ID": APP_ID, "Content-Type": "application/json"}
    req = urllib.request.Request(API + path, method=method, headers=h, data=b"{}" if method == "POST" else None)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def trading_url():
    """Pick the account for this mode and return (otp websocket url, currency)."""
    if not (TOKEN and APP_ID):
        raise SystemExit("Set DERIV_TOKEN and DERIV_APP_ID in the environment.")
    want = "real" if MODE == "real" else "demo"
    if MODE == "real" and os.getenv("ALLOW_REAL") != "1":
        raise SystemExit("Real mode needs ALLOW_REAL=1.")
    accts = [a for a in rest("GET", "/trading/v1/options/accounts")["data"]
             if a["account_type"] == want and a["status"] == "active"]
    if not accts:
        raise SystemExit(f"No active {want} account found for this token.")
    a = accts[0]
    print(f"using {want} account {a['account_id']}, balance {a['balance']} {a['currency']}")
    url = rest("POST", f"/trading/v1/options/accounts/{a['account_id']}/otp")["data"]["url"]
    return url, a["currency"]


async def main():
    wait = 2
    state = load_state()                            # empty on first run; restores today's P&L after a restart
    if state.get("pnl") or state.get("open_contracts"):
        print(f"restored state: pnl {state.get('pnl', 0):+.2f}, trades {state.get('trades', 0)}, "
              f"open contracts {len(state.get('open_contracts', {}))}")
    reconnect = False
    while True:                                     # reconnect with backoff; fresh OTP each time
        try:
            if MODE == "paper":
                url, cur = PUBLIC_WS, "USD"
            else:
                url, cur = await asyncio.to_thread(trading_url)
            async with websockets.connect(url, ping_interval=25) as ws:
                bot = Bot(ws, state.get("open_contracts"))
                bot.__dict__.update({k: v for k, v in state.items() if k != "open_contracts"})
                bot.currency = cur
                bot.resumed = reconnect
                wait, reconnect = 2, True
                try:
                    await bot.run()
                finally:
                    state = {k: bot.__dict__[k] for k in ("pnl", "trades", "last_trade", "day", "open_contracts")}
        except urllib.error.HTTPError as e:
            if e.code in (400, 401, 403):            # bad token / app id / scope: retrying will not help
                raise SystemExit(f"Deriv rejected the credentials (HTTP {e.code}). Check DERIV_TOKEN and DERIV_APP_ID.")
            print(f"REST error {e.code}; retrying in {wait}s")
            await asyncio.sleep(wait)
            wait = min(wait * 2, 60)
        except ApiError as e:
            raise SystemExit(f"Deriv API error: {e}")
        except (websockets.ConnectionClosed, OSError, asyncio.TimeoutError) as e:
            why = f"no data for {STALE_S}s" if isinstance(e, asyncio.TimeoutError) else e.__class__.__name__
            frame = getattr(e, "rcvd", None) or getattr(e, "sent", None)
            if frame is not None:                    # close code/reason from Deriv helps explain drops
                why += f", code {frame.code} {frame.reason!r}"
            alert(f"connection lost ({why}); retrying in {wait}s")
            await asyncio.sleep(wait)
            wait = min(wait * 2, 60)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("stopped")
