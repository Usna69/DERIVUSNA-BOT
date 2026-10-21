"""
Real forex candles from Deriv's public feed (no login), cached to candles_<symbol>_<seconds>.csv.
  python candles.py frxUSDJPY 300        # fetch/refresh (about 4 months of M5)
The feed rate-limits history requests, so pages are throttled and retried with backoff.
"""
import asyncio, json, os, sys, time
import websockets

URL = "wss://api.derivws.com/trading/v1/options/ws/public"
PAGE = 5000
PAUSE_S = 1.0


def cache_path(symbol, gran, folder="."):
    return os.path.join(folder, f"candles_{symbol}_{gran}.csv")


def load(symbol, gran, folder="."):
    """Cached rows as {epoch: (o, h, l, c)}."""
    rows = {}
    try:
        with open(cache_path(symbol, gran, folder)) as f:
            for line in f:
                try:
                    t, o, h, l, c = line.split(",")
                    rows[int(t)] = (float(o), float(h), float(l), float(c))
                except ValueError:
                    continue
    except OSError:
        pass
    return rows


def save(symbol, gran, rows, folder="."):
    tmp = cache_path(symbol, gran, folder) + ".tmp"
    with open(tmp, "w") as f:
        f.writelines(f"{t},{o},{h},{l},{c}\n" for t, (o, h, l, c) in sorted(rows.items()))
    os.replace(tmp, cache_path(symbol, gran, folder))


MIN_NEW = 50                                          # a page adding fewer candles than this means we hit the feed's history limit


def next_stalls(stalls, added, min_new=MIN_NEW):
    """Count consecutive near-empty pages. Near the history limit the feed trickles 1 candle per page, so 'no new
    candles' alone never fires and the download crawls forever."""
    return stalls + 1 if added < min_new else 0


async def _ask(ws, payload, retries=6):
    for attempt in range(retries):
        await ws.send(json.dumps(payload))
        res = json.loads(await asyncio.wait_for(ws.recv(), 30))
        err = res.get("error")
        if err and err.get("code") == "RateLimit":
            await asyncio.sleep(5 * (attempt + 1))
            continue
        return res
    raise RuntimeError("rate limited too long; try again in a few minutes")


async def fetch(symbol, gran, bars, folder=".", verbose=True):
    """Return sorted [(epoch, o, h, l, c)] with at least `bars` rows if the feed has them. Extends the cache both ways."""
    rows = load(symbol, gran, folder)
    async with websockets.connect(URL, ping_interval=25) as ws:
        if rows:                                      # top up the recent end first
            res = await _ask(ws, {"ticks_history": symbol, "end": "latest", "count": PAGE, "style": "candles",
                                  "granularity": gran, "req_id": 1})
            for c in res.get("candles", []):
                rows[c["epoch"]] = (c["open"], c["high"], c["low"], c["close"])
        end = (min(rows) - 1) if rows else "latest"
        stalls = 0
        while len(rows) < bars and stalls < 2:
            res = await _ask(ws, {"ticks_history": symbol, "end": end, "count": PAGE, "style": "candles",
                                  "granularity": gran, "req_id": 1})
            if res.get("error"):
                raise RuntimeError(res["error"].get("message", "feed error"))
            before = len(rows)
            for c in res.get("candles", []):
                rows[c["epoch"]] = (c["open"], c["high"], c["low"], c["close"])
            stalls = next_stalls(stalls, len(rows) - before)
            end = min(rows) - 1
            if verbose:
                print(f"  {symbol} {gran}s: {len(rows)} candles")
            await asyncio.sleep(PAUSE_S)
    save(symbol, gran, rows, folder)
    return [(t, *rows[t]) for t in sorted(rows)][-bars:]


if __name__ == "__main__":
    sym = sys.argv[1] if len(sys.argv) > 1 else "frxUSDJPY"
    g = int(sys.argv[2]) if len(sys.argv) > 2 else 300
    n = int(sys.argv[3]) if len(sys.argv) > 3 else 60000
    out = asyncio.run(fetch(sym, g, n))
    print(f"{sym}: {len(out)} candles, {time.strftime('%Y-%m-%d', time.gmtime(out[0][0]))} to "
          f"{time.strftime('%Y-%m-%d', time.gmtime(out[-1][0]))}  -> {cache_path(sym, g)}")
