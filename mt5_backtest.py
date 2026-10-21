"""
Backtest mt5_bot's strategy on your terminal's own history:   run.bat mt5backtest
(set MT5_SYMBOL=BTCUSD, MT5_TF=M15, BARS=40000; needs MT5 desktop open and the symbol in Market Watch)

Entry on the open of the bar after a signal, stop/target from ATR, both-hit-in-one-bar counts as a loss,
the bar's own spread is charged on every trade. Results are in R (1R = the amount risked), then in USD.
Judge it on the SECOND HALF and the confidence interval, never on the headline number.
"""
import math, os
import mt5_bot as m

BARS = int(os.getenv("BARS", "40000"))
MAX_HOLD = 200


def simulate(rows, fast, slow, atr_n, sl_atr, rr, point):
    """rows: list of dicts with open/high/low/close/spread(points). Returns list of per-trade R results."""
    closes = [float(r["close"]) for r in rows]
    highs = [float(r["high"]) for r in rows]
    lows = [float(r["low"]) for r in rows]
    out, i, n = [], max(slow, atr_n) + 2, len(rows)
    while i < n - 2:
        sig = m.d.signal(closes[i - slow:i + 1], fast, slow)
        a = m.atr(highs[i - atr_n:i + 1], lows[i - atr_n:i + 1], closes[i - atr_n:i + 1], atr_n) if sig else None
        if not sig or not a:
            i += 1
            continue
        buy = sig == "CALL"
        entry = float(rows[i + 1]["open"])
        dist = a * sl_atr
        sl, tp = (entry - dist, entry + dist * rr) if buy else (entry + dist, entry - dist * rr)
        cost_r = float(rows[i + 1]["spread"]) * point / dist
        result, j = None, i + 1
        while j < min(n, i + 1 + MAX_HOLD):
            hit_sl = lows[j] <= sl if buy else highs[j] >= sl
            hit_tp = highs[j] >= tp if buy else lows[j] <= tp
            if hit_sl:                                  # stop first when both are inside one bar (conservative)
                result = -1.0
            elif hit_tp:
                result = rr
            if result is not None:
                break
            j += 1
        if result is None:                              # timed out: mark to the last close
            move = (closes[min(j, n - 1)] - entry) * (1 if buy else -1)
            result = max(-1.0, min(rr, move / dist))
        out.append(result - cost_r)
        i = j + 1
    return out


def summarize(rs):
    n = len(rs)
    if n < 2:
        return None
    mean = sum(rs) / n
    sd = math.sqrt(sum((x - mean) ** 2 for x in rs) / (n - 1))
    se = sd / math.sqrt(n)
    z = mean / se if se else 0.0
    return {"n": n, "wins": sum(x > 0 for x in rs), "mean": mean, "lo": mean - 1.96 * se, "hi": mean + 1.96 * se,
            "p_edge": 0.5 * math.erfc(z / math.sqrt(2))}


def show(label, rs):
    s = summarize(rs)
    if not s:
        print(f"{label}: too few trades ({len(rs)})")
        return
    print(f"{label}: trades {s['n']}  win rate {s['wins'] / s['n']:.1%}  expectancy {s['mean']:+.3f}R "
          f"(95% CI {s['lo']:+.3f} to {s['hi']:+.3f})  p(edge) {s['p_edge']:.2f}  "
          f"=> {s['mean'] * m.RISK_USD:+.3f} USD/trade at {m.RISK_USD:.2f} USD risk, total {sum(rs) * m.RISK_USD:+.2f} USD")


if __name__ == "__main__":
    m.connect()
    sym = m.SYMBOL if m.SYMBOL != "auto" else "BTCUSD"
    m.mt5.symbol_select(sym, True)
    si = m.mt5.symbol_info(sym)
    rows = m.mt5.copy_rates_from_pos(sym, m.tf_const(m.TF_NAME), 1, BARS)
    if rows is None or len(rows) < 500:
        raise SystemExit(f"Not enough {sym} {m.TF_NAME} history ({0 if rows is None else len(rows)} bars). Scroll the chart back in MT5 to load more.")
    rows = [{k: r[k] for k in ("open", "high", "low", "close", "spread")} for r in rows]
    print(f"{sym} {m.TF_NAME}: {len(rows)} bars, fast {m.FAST} slow {m.SLOW} ATR {m.ATR_N}, stop {m.SL_ATR}xATR, target {m.RR}R")
    rs = simulate(rows, m.FAST, m.SLOW, m.ATR_N, m.SL_ATR, m.RR, si.point)
    show("all   ", rs)
    half = len(rs) // 2
    show("1st half", rs[:half])
    show("2nd half", rs[half:])
    print(f"break-even win rate at {m.RR}R before costs: {1 / (1 + m.RR):.1%}. Only trust a positive result whose CI stays above 0 in BOTH halves.")
