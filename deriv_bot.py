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
import asyncio, csv, json, os, time, urllib.request
from collections import deque
import websockets

MODE = os.getenv("BOT_MODE", "paper")
APP_ID = os.getenv("DERIV_APP_ID", "")
TOKEN = os.getenv("DERIV_TOKEN", "")
API = "https://api.derivws.com"
PUBLIC_WS = "wss://api.derivws.com/trading/v1/options/ws/public"

SYMBOL = os.getenv("SYMBOL", "R_100")               # Volatility 100 Index
STAKE = float(os.getenv("STAKE", "1"))              # fixed, never increased after a loss
DURATION_TICKS = int(os.getenv("DURATION_TICKS", "5"))
FAST, SLOW = 10, 30                                 # SMA lengths in ticks
COOLDOWN_S = 30                                     # min seconds between trades
MAX_TRADES_PER_DAY = int(os.getenv("MAX_TRADES", "20"))
MAX_DAILY_LOSS = float(os.getenv("MAX_DAILY_LOSS", "5"))   # stop for the day at this loss
PAPER_PAYOUT = 0.95                                 # assumed payout ratio in paper mode
LOG = "trades.csv"


def sma(xs, n):
    return sum(list(xs)[-n:]) / n


class Bot:
    def __init__(self, ws):
        self.ws = ws
        self.req = 0
        self.pending = {}
        self.ticks = deque(maxlen=SLOW + 2)
        self.pnl = 0.0
        self.trades = 0
        self.last_trade = 0.0
        self.open_paper = None                      # (direction, entry, ticks_left)
        self.busy = False
        self.day = time.strftime("%Y-%m-%d")

    async def call(self, payload):
        self.req += 1
        payload["req_id"] = self.req
        fut = asyncio.get_event_loop().create_future()
        self.pending[self.req] = fut
        await self.ws.send(json.dumps(payload))
        res = await asyncio.wait_for(fut, 20)
        if res.get("error"):
            raise RuntimeError(res["error"].get("message"))
        return res

    async def reader(self):
        async for raw in self.ws:
            m = json.loads(raw)
            fut = self.pending.pop(m.get("req_id"), None)
            if fut and not fut.done():
                fut.set_result(m)
            if m.get("msg_type") == "tick":
                await self.on_tick(float(m["tick"]["quote"]))

    def log(self, row):
        new = not os.path.exists(LOG)
        with open(LOG, "a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["time", "mode", "symbol", "direction", "stake", "profit", "day_pnl"])
            w.writerow(row)

    def allowed(self):
        today = time.strftime("%Y-%m-%d")
        if today != self.day:                        # new day: reset limits
            self.day, self.pnl, self.trades = today, 0.0, 0
        if self.pnl <= -MAX_DAILY_LOSS:
            return False
        if self.trades >= MAX_TRADES_PER_DAY:
            return False
        return time.time() - self.last_trade >= COOLDOWN_S

    async def on_tick(self, price):
        self.ticks.append(price)
        if self.open_paper:                          # settle a paper trade
            d, entry, left = self.open_paper
            left -= 1
            if left > 0:
                self.open_paper = (d, entry, left)
            else:
                win = price > entry if d == "CALL" else price < entry
                profit = STAKE * PAPER_PAYOUT if win else -STAKE
                self.pnl += profit
                self.log([time.strftime("%H:%M:%S"), MODE, SYMBOL, d, STAKE, round(profit, 2), round(self.pnl, 2)])
                print(f"paper {d} {'WIN' if win else 'LOSS'} {profit:+.2f}  day {self.pnl:+.2f}")
                self.open_paper = None
            return
        if len(self.ticks) < SLOW + 1 or self.busy or not self.allowed():
            return
        prev = list(self.ticks)[:-1]
        f0, s0 = sma(prev, FAST), sma(prev, SLOW)
        f1, s1 = sma(self.ticks, FAST), sma(self.ticks, SLOW)
        direction = "CALL" if (f0 <= s0 and f1 > s1) else "PUT" if (f0 >= s0 and f1 < s1) else None
        if not direction:
            return
        self.trades += 1
        self.last_trade = time.time()
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
            b = await self.call({"buy": prop["id"], "price": prop["ask_price"]})
            cid = b["buy"]["contract_id"]
            print(f"{MODE} {direction} bought, contract {cid}")
            for _ in range(60):                      # poll until sold
                await asyncio.sleep(2)
                c = (await self.call({"proposal_open_contract": 1, "contract_id": cid}))["proposal_open_contract"]
                if c.get("is_sold"):
                    profit = float(c["profit"])
                    self.pnl += profit
                    self.log([time.strftime("%H:%M:%S"), MODE, SYMBOL, direction, STAKE, profit, round(self.pnl, 2)])
                    print(f"{direction} {profit:+.2f}  day {self.pnl:+.2f}")
                    break
        except Exception as e:
            print("trade error:", e)
        finally:
            self.busy = False

    async def run(self):
        reader = asyncio.create_task(self.reader())
        await self.call({"ticks": SYMBOL, "subscribe": 1})
        print(f"{MODE} mode on {SYMBOL}, stake {STAKE}, daily loss limit {MAX_DAILY_LOSS}. Ctrl+C to stop.")
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
    state = {}
    while True:                                     # reconnect with backoff; fresh OTP each time
        try:
            if MODE == "paper":
                url, cur = PUBLIC_WS, "USD"
            else:
                url, cur = await asyncio.to_thread(trading_url)
            async with websockets.connect(url, ping_interval=25) as ws:
                bot = Bot(ws)
                bot.__dict__.update(state)           # keep pnl/trade counters across reconnects
                bot.currency = cur
                wait = 2
                try:
                    await bot.run()
                finally:
                    state = {k: bot.__dict__[k] for k in ("pnl", "trades", "last_trade", "day")}
        except (websockets.ConnectionClosed, OSError, asyncio.TimeoutError) as e:
            print(f"connection lost ({e.__class__.__name__}); retrying in {wait}s")
            await asyncio.sleep(wait)
            wait = min(wait * 2, 60)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("stopped")
