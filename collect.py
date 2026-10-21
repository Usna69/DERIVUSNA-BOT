"""
Record live ticks to ticks_<SYMBOL>.csv so the backtest can use weeks of data (the feed itself keeps ~24h).
Run it alongside the bot or on its own, and leave it running:   python collect.py
On start it back-fills the last ~24h, so short gaps are repaired. Needs no login.
"""
import asyncio, json, time
import websockets
import deriv_bot as b
import ticks

STALE_S = 30


async def stream():
    async with websockets.connect(b.PUBLIC_WS, ping_interval=25) as ws:
        await ws.send(json.dumps({"ticks": b.SYMBOL, "subscribe": 1, "req_id": 1}))
        last, saved = 0, 0
        with open(ticks.FILE, "a") as f:
            while True:
                m = json.loads(await asyncio.wait_for(ws.recv(), STALE_S))
                if m.get("error"):
                    raise SystemExit(m["error"].get("message"))
                if m.get("msg_type") != "tick":
                    continue
                t, p = int(m["tick"]["epoch"]), float(m["tick"]["quote"])
                if t <= last:
                    continue
                last = t
                f.write(f"{t},{p}\n")
                f.flush()
                saved += 1
                if saved % 300 == 0:
                    print(f"{time.strftime('%H:%M:%S')} {saved} ticks this session -> {ticks.FILE}")


async def main():
    try:                                              # repair any gap since the last run
        print(f"backfilled {ticks.append_file(await ticks.fetch(43200))} ticks")
    except Exception as e:
        print("backfill skipped:", e.__class__.__name__)
    wait = 2
    while True:
        try:
            await stream()
        except (websockets.ConnectionClosed, OSError, asyncio.TimeoutError) as e:
            print(f"connection lost ({e.__class__.__name__}); retrying in {wait}s")
            await asyncio.sleep(wait)
            wait = min(wait * 2, 60)
        else:
            wait = 2


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("stopped")
