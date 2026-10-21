import Link from "next/link";
import { getSql } from "@/lib/db";
import Refresh from "../refresh";

export const dynamic = "force-dynamic";

type Row = {
  symbol: string; tf_seconds: number; strategy: string; filters: string; stop_fill: string; spread_pct: string | null;
  trades: number; win_rate: string | null; mean_r: string | null; ci_lo: string | null; ci_hi: string | null;
  first_r: string | null; second_r: string | null; verdict: string | null; random_better: string | null; days: string | null; ts: Date;
};

const TF: Record<number, string> = { 60: "1m", 300: "5m", 900: "15m", 1800: "30m", 3600: "1h", 14400: "4h", 86400: "1d" };
const num = (x: string | null) => (x == null ? null : Number(x));
const r = (x: string | null) => (x == null ? "—" : `${Number(x) >= 0 ? "+" : ""}${Number(x).toFixed(3)}`);
const ago = (d: Date) => {
  const s = Math.max(0, Math.round((Date.now() - new Date(d).getTime()) / 1000));
  return s < 5400 ? `${Math.round(s / 60)}m ago` : s < 172800 ? `${Math.round(s / 3600)}h ago` : `${Math.round(s / 86400)}d ago`;
};
const badge: Record<string, string> = {
  PASS: "bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-300",
  PROMISING: "bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-300",
  FAIL: "bg-rose-100 text-rose-800 dark:bg-rose-950 dark:text-rose-300",
  "TOO FEW": "bg-zinc-200 text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300",
};

export default async function Lab() {
  let rows: Row[];
  try {
    const sql = getSql();
    rows = await sql<Row[]>`select * from lab_runs order by symbol, tf_seconds, mean_r desc nulls last limit 1000`;
  } catch (e) {
    const msg = e instanceof Error ? e.message : "Unknown error";
    const missing = /relation .* does not exist/.test(msg);
    return (
      <main className="mx-auto max-w-3xl p-6">
        <Link href="/" className="text-sm text-zinc-500 hover:underline">← Monitor</Link>
        <div className="mt-3 rounded-xl border border-amber-300 bg-amber-50 p-4 text-amber-900 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-200">
          <div className="font-semibold">{missing ? "No lab results yet" : "Can't reach the database"}</div>
          <div className="mt-1 text-sm">
            {missing ? <>Run <code>run.bat dbinit</code> once, then <code>run.bat lab compare --publish</code> on your PC.</> : msg}
          </div>
        </div>
      </main>
    );
  }

  const groups = new Map<string, Row[]>();
  for (const x of rows) {
    const k = `${x.symbol} · ${TF[x.tf_seconds] ?? x.tf_seconds + "s"}`;
    groups.set(k, [...(groups.get(k) ?? []), x]);
  }
  const count = (v: string) => rows.filter((x) => x.verdict === v).length;

  return (
    <main className="mx-auto max-w-5xl space-y-6 p-4 sm:p-6">
      <Refresh />
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">Strategy lab</h1>
          <div className="mt-1 text-sm text-zinc-500">Backtests on real candles, after spread. Expectancy is in R (1R = the amount risked).</div>
        </div>
        <Link href="/" className="text-sm text-zinc-500 hover:underline">← Monitor</Link>
      </header>

      <section className="grid grid-cols-2 gap-3 md:grid-cols-4">
        {[["Results", String(rows.length)], ["Pass", String(count("PASS"))], ["Promising", String(count("PROMISING"))], ["Fail", String(count("FAIL"))]].map(([l, v]) => (
          <div key={l} className="rounded-xl border border-zinc-200 bg-white p-4 dark:border-zinc-800 dark:bg-zinc-900">
            <div className="text-xs uppercase tracking-wide text-zinc-500">{l}</div>
            <div className="mt-1 text-2xl font-semibold tabular-nums">{v}</div>
          </div>
        ))}
      </section>

      {rows.length === 0 && (
        <div className="rounded-xl border border-zinc-200 bg-white p-6 text-center text-sm text-zinc-500 dark:border-zinc-800 dark:bg-zinc-900">
          Nothing published yet. On your PC run <code>run.bat lab compare --publish</code> with DATABASE_URL set.
        </div>
      )}

      {[...groups.entries()].map(([title, list]) => (
        <section key={title} className="overflow-hidden rounded-xl border border-zinc-200 bg-white dark:border-zinc-800 dark:bg-zinc-900">
          <h2 className="border-b border-zinc-200 px-4 py-2 text-sm font-medium dark:border-zinc-800">
            {title} <span className="ml-2 text-xs font-normal text-zinc-500">{list[0].days ? `${Number(list[0].days).toFixed(0)} days · ` : ""}spread {list[0].spread_pct ?? "?"}% · updated {ago(list[0].ts)}</span>
          </h2>
          <div className="overflow-x-auto">
            <table className="w-full text-sm tabular-nums">
              <thead className="text-left text-xs text-zinc-500">
                <tr><th className="px-4 py-2">Strategy</th><th>Filters</th><th className="text-right">Trades</th><th className="text-right">Expectancy</th><th className="text-right">95% interval</th><th className="text-right">1st / 2nd half</th><th className="px-4">Verdict</th></tr>
              </thead>
              <tbody>
                {list.map((x, i) => {
                  const m = num(x.mean_r) ?? 0;
                  return (
                    <tr key={i} className="border-t border-zinc-100 dark:border-zinc-800">
                      <td className="px-4 py-1.5 font-medium">{x.strategy}{x.stop_fill === "extreme" && <span className="ml-1 text-xs text-zinc-500">(worst fills)</span>}</td>
                      <td className="text-zinc-500">{x.filters}</td>
                      <td className="text-right">{x.trades}</td>
                      <td className={`text-right ${m >= 0 ? "text-emerald-600 dark:text-emerald-400" : "text-rose-600 dark:text-rose-400"}`}>{r(x.mean_r)}R</td>
                      <td className="text-right text-zinc-500">{r(x.ci_lo)} to {r(x.ci_hi)}</td>
                      <td className="text-right text-zinc-500">{r(x.first_r)} / {r(x.second_r)}</td>
                      <td className="px-4"><span className={`rounded-md px-2 py-0.5 text-xs font-medium ${badge[x.verdict ?? ""] ?? badge["TOO FEW"]}`}>{x.verdict ?? "—"}</span></td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </section>
      ))}
      <p className="text-center text-xs text-zinc-500">
        PASS needs 300+ trades and a 95% interval above zero in both halves. Many strategies are tested, so an occasional good-looking result is luck. Backtests are not a promise of live results.
      </p>
    </main>
  );
}
