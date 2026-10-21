"""
Backtest the SMA-crossover strategy from deriv_bot.py on past ticks (public feed, no login).

  python backtest.py                       # last ~20000 ticks of SYMBOL
  TICKS=50000 PAYOUT=0.95 python backtest.py

Uses the same signal(), stake, duration, cooldown and daily limits as the live bot.
PAYOUT is the assumed profit ratio on a win; check real proposals on demo, it varies.
"""
import asyncio, json, os, time
import websockets
import deriv_bot as b

TICKS = int(os.getenv("TICKS", "20000"))
PAYOUT = float(os.getenv("PAYOUT", str(b.PAPER_PAYOUT)))


async def fetch(n):
    """Page backwards through ticks_history (max 5000 per request). Returns [(epoch, price)].
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
    out = sorted(rows.items())
    if len(out) < n:
        print(f"note: only {len(out)} ticks available (feed history limit)")
    return out[-n:]


def run(rows):
    prices = [p for _, p in rows]
    wins = losses = 0
    pnl = peak = worst_dd = 0.0
    day, day_pnl, day_trades, last_t, i = None, 0.0, 0, 0, b.SLOW
    while i < len(rows) - b.DURATION_TICKS:
        t = rows[i][0]
        d = time.strftime("%Y-%m-%d", time.gmtime(t))
        if d != day:
            day, day_pnl, day_trades = d, 0.0, 0
        d_ok = day_pnl > -b.MAX_DAILY_LOSS and day_trades < b.MAX_TRADES_PER_DAY and t - last_t >= b.COOLDOWN_S
        direction = b.signal(prices[i - b.SLOW:i + 1]) if d_ok else None
        if direction:
            entry, exit_ = prices[i], prices[i + b.DURATION_TICKS]
            win = exit_ > entry if direction == "CALL" else exit_ < entry
            profit = b.STAKE * PAYOUT if win else -b.STAKE
            wins, losses = wins + win, losses + (not win)
            pnl += profit
            day_pnl += profit
            day_trades += 1
            last_t = t
            peak = max(peak, pnl)
            worst_dd = max(worst_dd, peak - pnl)
            i += b.DURATION_TICKS                    # one trade at a time, like the live bot
        i += 1
    n = wins + losses
    span_h = (rows[-1][0] - rows[0][0]) / 3600
    print(f"{b.SYMBOL}: {len(rows)} ticks over {span_h:.1f}h, stake {b.STAKE}, {b.DURATION_TICKS}-tick contracts, payout {PAYOUT:.0%}")
    if not n:
        print("no trades triggered")
        return
    print(f"trades {n}  wins {wins}  losses {losses}  win rate {wins / n:.1%}  (break-even {1 / (1 + PAYOUT):.1%})")
    print(f"P&L {pnl:+.2f}  per trade {pnl / n:+.3f}  max drawdown {worst_dd:.2f}")


if __name__ == "__main__":
    run(asyncio.run(fetch(TICKS)))
