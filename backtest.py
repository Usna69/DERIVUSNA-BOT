"""
Backtest the SMA-crossover strategy from deriv_bot.py on past ticks (public feed, no login).

  python backtest.py                   # current settings on the last 24h + anything saved by collect.py
  SWEEP=1 python backtest.py           # walk-forward parameter sweep (tune on first half, test on second)
  TICKS=50000 PAYOUT=0.92 python backtest.py

Reports a win-rate confidence interval and a p-value against break-even, because a few dozen trades
cannot distinguish skill from luck. PAYOUT = profit ratio on a win: env, else measured from trades.csv
(demo/real rows), else 95%.
"""
import asyncio, csv, math, os, time
import deriv_bot as b
import ticks

TICKS = int(os.getenv("TICKS", "100000"))


def measured_payout():
    """Mean real payout ratio from demo/real trades in trades.csv, or None."""
    try:
        with open(b.LOG, newline="") as f:
            r = [float(x["payout_ratio"]) for x in csv.DictReader(f) if x["mode"] != "paper" and x.get("payout_ratio")]
    except (OSError, KeyError, ValueError):
        return None
    return sum(r) / len(r) if r else None


_m = measured_payout()
if os.getenv("PAYOUT"):
    PAYOUT, PAYOUT_SRC = float(os.environ["PAYOUT"]), "PAYOUT env"
elif _m:
    PAYOUT, PAYOUT_SRC = _m, "measured from trades.csv"
else:
    PAYOUT, PAYOUT_SRC = b.PAPER_PAYOUT, "assumed default"


def load_rows(n):
    """Live history + saved ticks, deduped and time-ordered. Returns [(epoch, price)] (last n)."""
    try:
        merged = asyncio.run(ticks.fetch(n))
    except Exception as e:
        print(f"history fetch failed ({e.__class__.__name__}); using saved ticks only")
        merged = {}
    merged.update(ticks.load_file())
    out = sorted(merged.items())
    if len(out) < n and not os.path.exists(ticks.FILE):
        print(f"note: only {len(out)} ticks (feed keeps ~24h). Run collect.py to build a longer history.")
    return out[-n:]


def breakeven(payout):
    return 1 / (1 + payout)


def stats(wins, n, payout):
    """Win rate, Wilson 95% CI, and one-sided p-value that the true win rate beats break-even."""
    p0 = breakeven(payout)
    if not n:
        return None
    p = wins / n
    z = 1.96
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    zscore = (p - p0) / math.sqrt(p0 * (1 - p0) / n)
    pval = 0.5 * math.erfc(zscore / math.sqrt(2))
    return {"rate": p, "lo": centre - half, "hi": centre + half, "z": zscore, "p": pval}


def simulate(rows, fast, slow, dur, payout, limits=True):
    """Same entry rule as signal(), O(n) via prefix sums. One trade at a time, like the live bot.
    limits=True applies the live daily loss/trade caps and cooldown; False gives more trades for research."""
    px = [p for _, p in rows]
    cs = [0.0]
    for p in px:
        cs.append(cs[-1] + p)
    sma = lambda i, k: (cs[i + 1] - cs[i + 1 - k]) / k
    wins = losses = 0
    pnl = peak = dd = 0.0
    day, day_pnl, day_n, last_t, i = None, 0.0, 0, 0, slow
    while i < len(rows) - dur:
        t = rows[i][0]
        if limits:
            d = time.strftime("%Y-%m-%d", time.gmtime(t))
            if d != day:
                day, day_pnl, day_n = d, 0.0, 0
            if day_pnl <= -b.MAX_DAILY_LOSS or day_n >= b.MAX_TRADES_PER_DAY or t - last_t < b.COOLDOWN_S:
                i += 1
                continue
        f0, s0, f1, s1 = sma(i - 1, fast), sma(i - 1, slow), sma(i, fast), sma(i, slow)
        direction = "CALL" if (f0 <= s0 and f1 > s1) else "PUT" if (f0 >= s0 and f1 < s1) else None
        if direction:
            win = px[i + dur] > px[i] if direction == "CALL" else px[i + dur] < px[i]
            profit = b.STAKE * payout if win else -b.STAKE
            wins, losses = wins + win, losses + (not win)
            pnl += profit
            day_pnl += profit
            day_n += 1
            last_t = t
            peak = max(peak, pnl)
            dd = max(dd, peak - pnl)
            i += dur
        i += 1
    return {"wins": wins, "n": wins + losses, "pnl": pnl, "dd": dd}


def report(label, r, payout):
    n = r["n"]
    if not n:
        print(f"{label}: no trades")
        return
    s = stats(r["wins"], n, payout)
    print(f"{label}: trades {n}  win rate {s['rate']:.1%} (95% CI {s['lo']:.1%}-{s['hi']:.1%}, break-even {breakeven(payout):.1%})"
          f"  P&L {r['pnl']:+.2f}  per trade {r['pnl'] / n:+.3f}  max DD {r['dd']:.2f}  p(edge) {s['p']:.2f}")


def randomness(rows):
    """Is the series predictable at all? Lag-1 autocorrelation of tick changes, and share of up moves."""
    px = [p for _, p in rows]
    d = [y - x for x, y in zip(px, px[1:]) if y != x]
    n = len(d)
    mean = sum(d) / n
    var = sum((x - mean) ** 2 for x in d) / n
    ac = sum((d[i] - mean) * (d[i + 1] - mean) for i in range(n - 1)) / ((n - 1) * var)
    print(f"randomness: {n} moves, up {sum(x > 0 for x in d) / n:.1%}, lag-1 autocorr {ac:+.4f} "
          f"(noise band +/-{1.96 / math.sqrt(n):.4f}) -> {'looks random' if abs(ac) < 1.96 / math.sqrt(n) else 'some structure'}")


def sweep(rows, payout):
    """Walk-forward: pick the best parameters on the first half, judge them only on the second half."""
    mid = len(rows) // 2
    train, test = rows[:mid], rows[mid:]
    grid = [(f, s, d) for f in (3, 5, 10, 20) for s in (20, 30, 60, 100) for d in (1, 3, 5, 10) if f < s]
    res = []
    for f, s, d in grid:
        tr, te = simulate(train, f, s, d, payout, False), simulate(test, f, s, d, payout, False)
        res.append(((f, s, d), tr, te))
    ok = [x for x in res if x[1]["n"] >= 30]
    best = max(ok, key=lambda x: x[1]["pnl"] / x[1]["n"])
    print(f"sweep: {len(grid)} combos, train {len(train)} ticks, test {len(test)} ticks")
    report(f"best on TRAIN fast={best[0][0]} slow={best[0][1]} dur={best[0][2]}", best[1], payout)
    report("  same params on TEST (the honest number)", best[2], payout)
    per = [te["pnl"] / te["n"] for _, _, te in res if te["n"] >= 30]
    print(f"all combos on TEST: mean per-trade {sum(per) / len(per):+.3f}, profitable {sum(x > 0 for x in per)}/{len(per)}"
          f"  (with {len(per)} tries, a few 'winners' appear by luck alone)")


if __name__ == "__main__":
    rows = load_rows(TICKS)
    span = (rows[-1][0] - rows[0][0]) / 3600
    print(f"{b.SYMBOL}: {len(rows)} ticks over {span:.1f}h, payout {PAYOUT:.1%} ({PAYOUT_SRC})")
    randomness(rows)
    if os.getenv("SWEEP"):
        sweep(rows, PAYOUT)
    else:
        print(f"stake {b.STAKE}, fast {b.FAST}, slow {b.SLOW}, {b.DURATION_TICKS}-tick contracts, live limits applied")
        report("result", simulate(rows, b.FAST, b.SLOW, b.DURATION_TICKS, PAYOUT), PAYOUT)
        report("no limits", simulate(rows, b.FAST, b.SLOW, b.DURATION_TICKS, PAYOUT, False), PAYOUT)
