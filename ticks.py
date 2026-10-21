"""Tick storage + history fetch shared by collect.py and backtest.py (public feed, no login)."""
import asyncio, json, os
import websockets
import deriv_bot as b

FILE = f"ticks_{b.SYMBOL}.csv"                      # epoch,price per line; grows forever, so the backtest can see weeks


async def fetch(n):
    """Page backwards through ticks_history (max 5000 per request). Returns {epoch: price}.
    The feed keeps only ~24h of ticks; beyond that pages repeat, so stop when no older tick appears."""
    rows, end = {}, "latest"
    async with websockets.connect(b.PUBLIC_WS) as ws:
        while len(rows) < n:
            await ws.send(json.dumps({"ticks_history": b.SYMBOL, "end": end, "count": min(5000, n - len(rows)),
                                      "style": "ticks", "req_id": 1}))
            res = json.loads(await asyncio.wait_for(ws.recv(), 30))
            if res.get("error"):
                raise SystemExit(res["error"].get("message"))
            h = res["history"]
            before = len(rows)
            rows.update(zip(h["times"], h["prices"]))
            if len(rows) == before:                  # nothing new: reached the history limit
                break
            end = min(rows) - 1
    return rows


def load_file(path=FILE):
    rows = {}
    try:
        with open(path) as f:
            for line in f:
                try:
                    t, p = line.split(",")
                    rows[int(t)] = float(p)
                except ValueError:
                    continue                         # blank/partial line from a crash
    except OSError:
        pass
    return rows


def append_file(new, path=FILE):
    """Append {epoch: price} rows not already stored, in time order."""
    have = load_file(path)
    fresh = sorted((t, p) for t, p in new.items() if t not in have)
    if fresh:
        with open(path, "a") as f:
            f.writelines(f"{t},{p}\n" for t, p in fresh)
    return len(fresh)
