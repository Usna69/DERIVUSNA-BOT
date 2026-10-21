"""
Strategy lab: test ideas on real forex candles before any money is involved.

  python lab.py fetch   [--symbol frxUSDJPY --tf 300]
  python lab.py compare [--symbol frxUSDJPY --tf 300 --session 12-17 --trend 200]
  python lab.py sweep sma_cross [--symbol ...]       walk-forward: tune on 1st half, judge on 2nd

Every trade: signal on a CLOSED bar, entry at the NEXT bar's open, stop = sl_atr x ATR, target = rr x stop,
spread charged as a share of the stop, both-hit-in-one-bar counts as a loss. Results are in R (1R = amount risked).
Verdict (PASS) needs >= 300 trades and a 95% interval above zero in BOTH halves of the data; a random-entry
baseline shows how often luck alone beats the strategy.
"""
import argparse, asyncio, math, random, time

SPREAD_PCT = 0.009          # USDJPY on your account (scan: 0.009% of price); override with --spread-pct
MAX_HOLD = 200


# ---------------------------------------------------------------- indicators (all use data up to bar i only)
class Series:
    def __init__(self, rows, gran=300):
        self.t = [r[0] for r in rows]
        self.o = [float(r[1]) for r in rows]
        self.h = [float(r[2]) for r in rows]
        self.l = [float(r[3]) for r in rows]
        self.c = [float(r[4]) for r in rows]
        self.n, self.gran, self._cache = len(rows), gran, {}

    def memo(self, key, fn):
        if key not in self._cache:
            self._cache[key] = fn()
        return self._cache[key]

    def sma(self, n):
        def build():
            cs, out = [0.0], [None] * self.n
            for x in self.c:
                cs.append(cs[-1] + x)
            for i in range(n - 1, self.n):
                out[i] = (cs[i + 1] - cs[i + 1 - n]) / n
            return out
        return self.memo(("sma", n), build)

    def atr(self, n=14):
        def build():
            tr = [None] + [max(self.h[i] - self.l[i], abs(self.h[i] - self.c[i - 1]), abs(self.l[i] - self.c[i - 1])) for i in range(1, self.n)]
            cs, out = [0.0, 0.0], [None] * self.n
            for x in tr[1:]:
                cs.append(cs[-1] + x)
            for i in range(n, self.n):
                out[i] = (cs[i + 1] - cs[i + 1 - n]) / n
            return out
        return self.memo(("atr", n), build)

    def rsi(self, n=14):
        def build():
            out, avg_g, avg_l = [None] * self.n, 0.0, 0.0
            for i in range(1, self.n):
                d = self.c[i] - self.c[i - 1]
                g, l = max(d, 0.0), max(-d, 0.0)
                if i <= n:
                    avg_g, avg_l = avg_g + g / n, avg_l + l / n
                else:
                    avg_g, avg_l = (avg_g * (n - 1) + g) / n, (avg_l * (n - 1) + l) / n
                if i >= n:
                    out[i] = 100.0 if avg_l == 0 else 100.0 - 100.0 / (1 + avg_g / avg_l)
            return out
        return self.memo(("rsi", n), build)

    def boll(self, n=20, k=2.0):
        def build():
            cs, cq = [0.0], [0.0]
            for x in self.c:
                cs.append(cs[-1] + x)
                cq.append(cq[-1] + x * x)
            lo, hi = [None] * self.n, [None] * self.n
            for i in range(n - 1, self.n):
                m = (cs[i + 1] - cs[i + 1 - n]) / n
                var = max((cq[i + 1] - cq[i + 1 - n]) / n - m * m, 0.0)
                lo[i], hi[i] = m - k * math.sqrt(var), m + k * math.sqrt(var)
            return lo, hi
        return self.memo(("boll", n, k), build)


# ---------------------------------------------------------------- strategies: bar i -> 1 (buy), -1 (sell), 0
def sma_cross(s, fast=10, slow=30):
    f, sl = s.sma(fast), s.sma(slow)
    out = [0] * s.n
    for i in range(slow, s.n):
        if f[i - 1] <= sl[i - 1] and f[i] > sl[i]:
            out[i] = 1
        elif f[i - 1] >= sl[i - 1] and f[i] < sl[i]:
            out[i] = -1
    return out


def rsi_revert(s, n=14, lo=30, hi=70):
    r, out = s.rsi(n), [0] * s.n
    for i in range(n + 1, s.n):
        if r[i - 1] < lo <= r[i]:
            out[i] = 1
        elif r[i - 1] > hi >= r[i]:
            out[i] = -1
    return out


def bb_revert(s, n=20, k=2.0):
    lo, hi = s.boll(n, k)
    out = [0] * s.n
    for i in range(n, s.n):
        if s.c[i - 1] < lo[i - 1] and s.c[i] >= lo[i]:
            out[i] = 1
        elif s.c[i - 1] > hi[i - 1] and s.c[i] <= hi[i]:
            out[i] = -1
    return out


def donchian(s, n=20):
    out = [0] * s.n
    for i in range(n, s.n):
        if s.c[i] > max(s.h[i - n:i]):
            out[i] = 1
        elif s.c[i] < min(s.l[i - n:i]):
            out[i] = -1
    return out


def trend_pullback(s, trend=200, rsi_n=14, lo=40, hi=60):
    ma, r, out = s.sma(trend), s.rsi(rsi_n), [0] * s.n
    for i in range(trend, s.n):
        if s.c[i] > ma[i] and r[i - 1] < lo <= r[i]:
            out[i] = 1
        elif s.c[i] < ma[i] and r[i - 1] > hi >= r[i]:
            out[i] = -1
    return out


STRATEGIES = {
    "sma_cross": (sma_cross, {"fast": 10, "slow": 30}, {"fast": [3, 5, 10, 20], "slow": [20, 30, 60, 100]}),
    "rsi_revert": (rsi_revert, {"n": 14, "lo": 30, "hi": 70}, {"n": [7, 14], "lo": [20, 25, 30], "hi": [70, 75, 80]}),
    "bb_revert": (bb_revert, {"n": 20, "k": 2.0}, {"n": [10, 20, 40], "k": [1.5, 2.0, 2.5]}),
    "donchian": (donchian, {"n": 20}, {"n": [10, 20, 40, 80]}),
    "trend_pullback": (trend_pullback, {"trend": 200, "rsi_n": 14, "lo": 40, "hi": 60},
                       {"trend": [100, 200], "rsi_n": [7, 14], "lo": [30, 40], "hi": [60, 70]}),
}


# ---------------------------------------------------------------- filters
def session_filter(sig, s, start, end):
    """Keep signals only when the bar's UTC hour is in [start, end)."""
    return [x if start <= time.gmtime(s.t[i]).tm_hour < end else 0 for i, x in enumerate(sig)]


def trend_filter(sig, s, n):
    """Buys only above the n-bar average, sells only below it."""
    ma = s.sma(n)
    return [x if ma[i] is not None and ((x == 1 and s.c[i] > ma[i]) or (x == -1 and s.c[i] < ma[i])) else 0 for i, x in enumerate(sig)]


# ---------------------------------------------------------------- engine
def simulate(s, sig, sl_atr=1.5, rr=1.5, spread_pct=SPREAD_PCT, atr_n=14, max_hold=MAX_HOLD):
    """Returns [(epoch, R)] for trades taken one at a time. No lookahead: decide on bar i, enter at open of i+1."""
    atr, out, i = s.atr(atr_n), [], atr_n + 1
    while i < s.n - 2:
        if sig[i] == 0 or atr[i] is None or s.t[i + 1] - s.t[i] > 3 * s.gran:     # skip weekend-gap entries
            i += 1
            continue
        buy = sig[i] == 1
        entry, dist = s.o[i + 1], atr[i] * sl_atr
        sl, tp = (entry - dist, entry + dist * rr) if buy else (entry + dist, entry - dist * rr)
        cost = spread_pct / 100 * entry / dist
        r, j = None, i + 1
        while j < min(s.n, i + 1 + max_hold):
            hit_sl = s.l[j] <= sl if buy else s.h[j] >= sl
            hit_tp = s.h[j] >= tp if buy else s.l[j] <= tp
            if hit_sl:
                r = -1.0                                  # stop first when both are inside one bar (conservative)
            elif hit_tp:
                r = rr
            if r is not None:
                break
            j += 1
        if r is None:                                     # timed out: mark to the last close
            j = min(j, s.n - 1)
            r = max(-1.0, min(rr, (s.c[j] - entry) * (1 if buy else -1) / dist))
        out.append((s.t[i], r - cost))
        i = j + 1
    return out


def summarize(rs):
    n = len(rs)
    if n < 2:
        return {"n": n, "mean": 0.0, "lo": 0.0, "hi": 0.0, "wins": sum(x > 0 for x in rs)}
    m = sum(rs) / n
    se = math.sqrt(sum((x - m) ** 2 for x in rs) / (n - 1)) / math.sqrt(n)
    return {"n": n, "mean": m, "lo": m - 1.96 * se, "hi": m + 1.96 * se, "wins": sum(x > 0 for x in rs)}


def halves(trades):
    rs = [r for _, r in trades]
    mid = len(rs) // 2
    return summarize(rs), summarize(rs[:mid]), summarize(rs[mid:])


def verdict(trades, min_trades=300):
    a, h1, h2 = halves(trades)
    if a["n"] < min_trades:
        return "TOO FEW"
    if h1["lo"] > 0 and h2["lo"] > 0:
        return "PASS"
    if h1["mean"] > 0 and h2["mean"] > 0:
        return "PROMISING"
    return "FAIL"


def random_baseline(s, n_trades, sl_atr, rr, spread_pct, observed, trials=100, seed=7):
    """Same exits and costs, random entries. Returns the share of random runs at least as good as `observed`."""
    if n_trades < 10:
        return None
    rng, q, better = random.Random(seed), min(0.5, n_trades / max(1, s.n)), 0
    for _ in range(trials):
        sig = [rng.choice((1, -1)) if rng.random() < q else 0 for _ in range(s.n)]
        t = simulate(s, sig, sl_atr, rr, spread_pct)
        if t and sum(r for _, r in t) / len(t) >= observed:
            better += 1
    return better / trials


# ---------------------------------------------------------------- reporting
def line(name, trades, s=None, args=None):
    a, h1, h2 = halves(trades)
    if not a["n"]:
        return f"{name:<24} no trades"
    base = ""
    if s is not None and args is not None:
        p = random_baseline(s, a["n"], args.sl_atr, args.rr, args.spread_pct, a["mean"])
        base = f"  random>=: {p:.0%}" if p is not None else ""
    return (f"{name:<24} n={a['n']:<5} win {a['wins'] / a['n']:5.1%}  exp {a['mean']:+.3f}R [{a['lo']:+.3f},{a['hi']:+.3f}]  "
            f"1st {h1['mean']:+.3f} 2nd {h2['mean']:+.3f}  {verdict(trades):<9}{base}")


def build_signals(s, name, params, session, trend):
    sig = STRATEGIES[name][0](s, **params)
    if trend:
        sig = trend_filter(sig, s, trend)
    if session:
        sig = session_filter(sig, s, *session)
    return sig


def load_series(args):
    import candles
    try:
        rows = asyncio.run(candles.fetch(args.symbol, args.tf, args.bars))
    except Exception as e:                                 # noqa: BLE001 - fall back to whatever is cached
        print(f"fetch failed ({e}); using cache")
        rows = [(t, *v) for t, v in sorted(candles.load(args.symbol, args.tf).items())][-args.bars:]
    if len(rows) < 2000:
        raise SystemExit(f"only {len(rows)} candles for {args.symbol} {args.tf}s; need at least 2000")
    s = Series(rows, args.tf)
    days = (s.t[-1] - s.t[0]) / 86400
    print(f"{args.symbol} {args.tf}s: {s.n} candles over {days:.0f} days, spread {args.spread_pct}% of price, "
          f"stop {args.sl_atr}xATR, target {args.rr}R")
    return s


def cmd_compare(args):
    s = load_series(args)
    print(f"filters: session {args.session or 'any'}, trend {args.trend or 'none'}\n")
    for name, (_, params, _) in STRATEGIES.items():
        sig = build_signals(s, name, params, args.session, args.trend)
        print(line(name, simulate(s, sig, args.sl_atr, args.rr, args.spread_pct), s, args))
    print("\nPASS = >=300 trades and 95% interval above 0 in BOTH halves. 'random>=' = share of random-entry runs at least this good.")


def cmd_sweep(args):
    if args.name not in STRATEGIES:
        raise SystemExit(f"unknown strategy; choose from {', '.join(STRATEGIES)}")
    s = load_series(args)
    fn, _, grid = STRATEGIES[args.name]
    keys = list(grid)
    combos = [dict(zip(keys, v)) for v in __import__("itertools").product(*[grid[k] for k in keys])]
    mid = s.n // 2
    train, test = Series([(s.t[i], s.o[i], s.h[i], s.l[i], s.c[i]) for i in range(mid)], s.gran), \
        Series([(s.t[i], s.o[i], s.h[i], s.l[i], s.c[i]) for i in range(mid, s.n)], s.gran)
    scored = []
    for p in combos:
        tr = simulate(train, build_signals(train, args.name, p, args.session, args.trend), args.sl_atr, args.rr, args.spread_pct)
        te = simulate(test, build_signals(test, args.name, p, args.session, args.trend), args.sl_atr, args.rr, args.spread_pct)
        scored.append((p, tr, te))
    ok = [x for x in scored if len(x[1]) >= 30]
    if not ok:
        raise SystemExit("no parameter set produced 30+ trades in the first half")
    best = max(ok, key=lambda x: summarize([r for _, r in x[1]])["mean"])
    print(f"\n{len(combos)} combinations tried; chosen on the FIRST half only: {best[0]}")
    print(line("  first half (tuned)", best[1]))
    print(line("  second half (honest)", best[2]))
    pos = [summarize([r for _, r in x[2]])["mean"] for x in ok if len(x[2]) >= 30]
    print(f"  all combos on 2nd half: mean {sum(pos) / len(pos):+.3f}R, {sum(m > 0 for m in pos)}/{len(pos)} positive "
          f"(some positives are expected by luck alone)")


def cmd_fetch(args):
    import candles
    out = asyncio.run(candles.fetch(args.symbol, args.tf, args.bars))
    print(f"{len(out)} candles cached for {args.symbol} {args.tf}s")


def parse_session(txt):
    if not txt:
        return None
    a, b = txt.split("-")
    return int(a), int(b)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("cmd", choices=["compare", "sweep", "fetch"])
    p.add_argument("name", nargs="?", default="sma_cross", help="strategy for sweep")
    p.add_argument("--symbol", default="frxUSDJPY")
    p.add_argument("--tf", type=int, default=300, help="candle size in seconds (300=M5, 900=M15, 3600=H1)")
    p.add_argument("--bars", type=int, default=60000)
    p.add_argument("--sl-atr", type=float, default=1.5)
    p.add_argument("--rr", type=float, default=1.5)
    p.add_argument("--spread-pct", type=float, default=SPREAD_PCT)
    p.add_argument("--session", default=None, help="UTC hours, e.g. 12-17")
    p.add_argument("--trend", type=int, default=0, help="only trade with the n-bar average, e.g. 200")
    args = p.parse_args(argv)
    args.session = parse_session(args.session)
    {"compare": cmd_compare, "sweep": cmd_sweep, "fetch": cmd_fetch}[args.cmd](args)


if __name__ == "__main__":
    main()
