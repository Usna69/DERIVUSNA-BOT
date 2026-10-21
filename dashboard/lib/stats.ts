// Mirrors backtest.py: win rate, Wilson 95% CI, and a one-sided p-value against break-even.

export function breakeven(payout: number) {
  return 1 / (1 + payout);
}

// Standard normal upper tail, Abramowitz-Stegun 7.1.26 erf approximation (error < 1.5e-7).
function normalUpperTail(z: number) {
  const x = Math.abs(z) / Math.SQRT2;
  const t = 1 / (1 + 0.3275911 * x);
  const poly = t * (0.254829592 + t * (-0.284496736 + t * (1.421413741 + t * (-1.453152027 + t * 1.061405429))));
  const erfc = poly * Math.exp(-x * x);
  const upper = 0.5 * erfc;
  return z >= 0 ? upper : 1 - upper;
}

export function winStats(wins: number, n: number, payout: number) {
  if (n === 0) return null;
  const p0 = breakeven(payout);
  const p = wins / n;
  const z = 1.96;
  const den = 1 + (z * z) / n;
  const centre = (p + (z * z) / (2 * n)) / den;
  const half = (z * Math.sqrt((p * (1 - p)) / n + (z * z) / (4 * n * n))) / den;
  const zscore = (p - p0) / Math.sqrt((p0 * (1 - p0)) / n);
  return { rate: p, lo: centre - half, hi: centre + half, breakeven: p0, pValue: normalUpperTail(zscore) };
}

export type Trade = { ts: Date; direction: string; stake: number; profit: number; payout_ratio: number | null };


export function summarize(trades: Trade[]) {
  const n = trades.length;
  const wins = trades.filter((t) => t.profit > 0).length;
  const pnl = trades.reduce((a, t) => a + t.profit, 0);
  const ratios = trades.map((t) => t.payout_ratio).filter((x): x is number => x != null && x > 0);
  const payout = ratios.length ? ratios.reduce((a, b) => a + b, 0) / ratios.length : 0.95;
  let cum = 0, peak = 0, maxDd = 0;
  const curve = trades.map((t) => {
    cum += t.profit;
    peak = Math.max(peak, cum);
    maxDd = Math.max(maxDd, peak - cum);
    return cum;
  });
  return { n, wins, pnl, payout, maxDd, curve, stats: winStats(wins, n, payout) };
}

export function verdict(s: ReturnType<typeof summarize>) {
  if (!s.stats || s.n < 100) return { tone: "neutral" as const, text: `Only ${s.n} trades. Too few to tell skill from luck (aim for hundreds).` };
  if (s.stats.pValue < 0.05 && s.pnl > 0) return { tone: "good" as const, text: "Win rate is above break-even with statistical support. Verify out of sample before trusting it." };
  return { tone: "bad" as const, text: "No evidence of an edge: the win rate is not distinguishable from break-even or is below it." };
}

// ---- R-multiple view for the MT5 bot: the stake column holds the planned risk, so R = profit / stake.
export function rStats(trades: Trade[]) {
  const rs = trades.filter((t) => t.stake > 0).map((t) => t.profit / t.stake);
  const part = (xs: number[]) => {
    const n = xs.length;
    if (n < 2) return { n, mean: n ? xs[0] : 0, lo: 0, hi: 0, p: 1 };
    const mean = xs.reduce((a, b) => a + b, 0) / n;
    const se = Math.sqrt(xs.reduce((a, x) => a + (x - mean) ** 2, 0) / (n - 1)) / Math.sqrt(n);
    return { n, mean, lo: mean - 1.96 * se, hi: mean + 1.96 * se, p: se ? normalUpperTail(mean / se) : 1 };
  };
  const mid = Math.floor(rs.length / 2);
  return { all: part(rs), first: part(rs.slice(0, mid)), second: part(rs.slice(mid)) };
}

// Same gate as lab.py: 300+ trades and a 95% interval above zero in BOTH halves.
export function rVerdict(r: ReturnType<typeof rStats>) {
  if (r.all.n < 300) return { tone: "neutral" as const, label: "Too few trades", text: `${r.all.n} of 300 trades needed before any verdict is meaningful.` };
  if (r.first.lo > 0 && r.second.lo > 0) return { tone: "good" as const, label: "Pass", text: "Positive with statistical support in both halves. Keep watching forward results." };
  if (r.first.mean > 0 && r.second.mean > 0) return { tone: "neutral" as const, label: "Promising", text: "Positive in both halves but not yet statistically solid." };
  return { tone: "bad" as const, label: "Fail", text: "No evidence of an edge: average R is not reliably above zero in both halves." };
}

// P&L per UTC day, oldest first, last `days` days that had trades.
export function dailyPnl(trades: Trade[], days = 14) {
  const by = new Map<string, number>();
  for (const t of trades) {
    const d = new Date(t.ts).toISOString().slice(0, 10);
    by.set(d, (by.get(d) ?? 0) + t.profit);
  }
  return [...by.entries()].sort((a, b) => a[0].localeCompare(b[0])).slice(-days).map(([day, pnl]) => ({ day, pnl }));
}
