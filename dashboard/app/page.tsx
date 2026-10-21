import Link from "next/link";
import { getSql } from "@/lib/db";
import { summarize, verdict, type Trade } from "@/lib/stats";
import Refresh from "./refresh";

export const dynamic = "force-dynamic";

type Status = { mode: string; symbol: string; day_pnl: string; trades_today: number; status: string; message: string; last_seen: Date };
type Row = { ts: Date; mode: string; symbol: string; direction: string; stake: string; profit: string; day_pnl: string; payout_ratio: string | null };
type Event = { ts: Date; mode: string; level: string; text: string };

const MODES = ["paper", "demo", "real"];
const money = (x: number) => `${x >= 0 ? "+" : "-"}${Math.abs(x).toFixed(2)}`;
const pct = (x: number) => `${(x * 100).toFixed(1)}%`;
const ago = (d: Date) => {
  const s = Math.max(0, Math.round((Date.now() - new Date(d).getTime()) / 1000));
  return s < 90 ? `${s}s ago` : s < 5400 ? `${Math.round(s / 60)}m ago` : s < 172800 ? `${Math.round(s / 3600)}h ago` : `${Math.round(s / 86400)}d ago`;
};
const clock = (d: Date) => new Date(d).toISOString().slice(5, 16).replace("T", " ");

function Card({ label, value, sub, tone }: { label: string; value: string; sub?: string; tone?: "good" | "bad" }) {
  const color = tone === "good" ? "text-emerald-600 dark:text-emerald-400" : tone === "bad" ? "text-rose-600 dark:text-rose-400" : "";
  return (
    <div className="rounded-xl border border-zinc-200 bg-white p-4 dark:border-zinc-800 dark:bg-zinc-900">
      <div className="text-xs uppercase tracking-wide text-zinc-500">{label}</div>
      <div className={`mt-1 text-2xl font-semibold tabular-nums ${color}`}>{value}</div>
      {sub && <div className="mt-1 text-xs text-zinc-500">{sub}</div>}
    </div>
  );
}

function Curve({ points }: { points: number[] }) {
  if (points.length < 2) return <div className="grid h-40 place-items-center text-sm text-zinc-500">Needs at least 2 trades.</div>;
  const w = 800, h = 180, pad = 8;
  const all = [0, ...points];
  const lo = Math.min(...all), hi = Math.max(...all);
  const span = hi - lo || 1;
  const x = (i: number) => pad + (i / (points.length - 1)) * (w - 2 * pad);
  const y = (v: number) => h - pad - ((v - lo) / span) * (h - 2 * pad);
  const line = points.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  const up = points[points.length - 1] >= 0;
  return (
    <svg viewBox={`0 0 ${w} ${h}`} className="h-44 w-full" role="img" aria-label="Cumulative profit per trade">
      <line x1={pad} x2={w - pad} y1={y(0)} y2={y(0)} className="stroke-zinc-300 dark:stroke-zinc-700" strokeDasharray="4 4" />
      <polyline points={line} fill="none" strokeWidth="2" strokeLinejoin="round" className={up ? "stroke-emerald-500" : "stroke-rose-500"} />
    </svg>
  );
}

export default async function Home({ searchParams }: { searchParams: Promise<{ mode?: string }> }) {
  const { mode: asked } = await searchParams;
  let data;
  try {
    const sql = getSql();
    const statuses = await sql<Status[]>`select * from bot_status order by mode`;
    const mode = MODES.includes(asked ?? "") ? asked! : statuses.find((s) => s.mode === "demo")?.mode ?? statuses[0]?.mode ?? "demo";
    const rows = await sql<Row[]>`select ts, mode, symbol, direction, stake, profit, day_pnl, payout_ratio from trades where mode = ${mode} order by ts asc limit 50000`;
    const events = await sql<Event[]>`select ts, mode, level, text from events where mode = ${mode} order by ts desc limit 15`;
    data = { statuses, mode, rows, events };
  } catch (e) {
    return (
      <main className="mx-auto max-w-3xl p-6">
        <div className="rounded-xl border border-rose-300 bg-rose-50 p-4 text-rose-900 dark:border-rose-900 dark:bg-rose-950 dark:text-rose-200">
          <div className="font-semibold">Can&apos;t reach the database</div>
          <div className="mt-1 text-sm">{e instanceof Error ? e.message : "Unknown error"}. Check DATABASE_URL and that the bot has run once with it set.</div>
        </div>
      </main>
    );
  }

  const { statuses, mode, rows, events } = data;
  const trades: Trade[] = rows.map((r) => ({ ts: r.ts, direction: r.direction, stake: Number(r.stake), profit: Number(r.profit), payout_ratio: r.payout_ratio == null ? null : Number(r.payout_ratio) }));
  const s = summarize(trades);
  const v = verdict(s);
  const st = statuses.find((x) => x.mode === mode);
  const stale = st ? Date.now() - new Date(st.last_seen).getTime() > 90_000 : true;
  const state = !st ? "no data yet" : stale ? "offline" : st.status;
  const dot = state === "running" ? "bg-emerald-500" : state === "limit" || state === "stopped" ? "bg-amber-500" : "bg-rose-500";
  const recent = [...rows].reverse().slice(0, 40);
  const vtone = v.tone === "good" ? "border-emerald-300 bg-emerald-50 text-emerald-900 dark:border-emerald-900 dark:bg-emerald-950 dark:text-emerald-200"
    : v.tone === "bad" ? "border-rose-300 bg-rose-50 text-rose-900 dark:border-rose-900 dark:bg-rose-950 dark:text-rose-200"
    : "border-zinc-300 bg-zinc-100 text-zinc-800 dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-300";

  return (
    <main className="mx-auto max-w-5xl space-y-6 p-4 sm:p-6">
      <Refresh />
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">Bot monitor</h1>
          <div className="mt-1 flex items-center gap-2 text-sm text-zinc-500">
            <span className={`inline-block h-2.5 w-2.5 rounded-full ${dot}`} />
            <span className="font-medium text-zinc-700 dark:text-zinc-300">{state}</span>
            {st && <span>· {st.symbol} · seen {ago(st.last_seen)}</span>}
          </div>
        </div>
        <nav className="flex gap-1 rounded-lg border border-zinc-200 p-1 text-sm dark:border-zinc-800">
          {MODES.map((m) => (
            <Link key={m} href={`/?mode=${m}`} className={`rounded-md px-3 py-1 ${m === mode ? "bg-zinc-900 text-white dark:bg-zinc-100 dark:text-zinc-900" : "text-zinc-600 hover:bg-zinc-100 dark:text-zinc-400 dark:hover:bg-zinc-800"}`}>
              {m}
            </Link>
          ))}
        </nav>
      </header>

      <div className={`rounded-xl border p-3 text-sm ${vtone}`}>{v.text}</div>

      <section className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <Card label="Today" value={st ? money(Number(st.day_pnl)) : "—"} sub={st ? `${st.trades_today} trades` : undefined} tone={st ? (Number(st.day_pnl) >= 0 ? "good" : "bad") : undefined} />
        <Card label="All-time P&L" value={money(s.pnl)} sub={`${s.n} trades${s.n ? `, ${money(s.pnl / s.n)} each` : ""}`} tone={s.pnl >= 0 ? "good" : "bad"} />
        <Card label="Win rate" value={s.stats ? pct(s.stats.rate) : "—"} sub={s.stats ? `95% CI ${pct(s.stats.lo)}–${pct(s.stats.hi)}` : undefined} />
        <Card label="Break-even" value={pct(1 / (1 + s.payout))} sub={`payout ${pct(s.payout)}${s.stats ? ` · p(edge) ${s.stats.pValue.toFixed(2)}` : ""}`} />
      </section>

      <section className="rounded-xl border border-zinc-200 bg-white p-4 dark:border-zinc-800 dark:bg-zinc-900">
        <div className="mb-2 flex items-baseline justify-between">
          <h2 className="text-sm font-medium">Cumulative profit</h2>
          <span className="text-xs text-zinc-500">max drawdown {s.maxDd.toFixed(2)}</span>
        </div>
        <Curve points={s.curve} />
      </section>

      <div className="grid gap-6 md:grid-cols-5">
        <section className="overflow-hidden rounded-xl border border-zinc-200 bg-white dark:border-zinc-800 dark:bg-zinc-900 md:col-span-3">
          <h2 className="border-b border-zinc-200 px-4 py-2 text-sm font-medium dark:border-zinc-800">Recent trades</h2>
          <div className="max-h-96 overflow-auto">
            <table className="w-full text-sm tabular-nums">
              <thead className="sticky top-0 bg-zinc-50 text-left text-xs text-zinc-500 dark:bg-zinc-900">
                <tr><th className="px-4 py-2">Time (UTC)</th><th>Dir</th><th className="text-right">Profit</th><th className="px-4 text-right">Day</th></tr>
              </thead>
              <tbody>
                {recent.length === 0 && <tr><td colSpan={4} className="px-4 py-6 text-center text-zinc-500">No trades yet.</td></tr>}
                {recent.map((r, i) => {
                  const p = Number(r.profit);
                  return (
                    <tr key={i} className="border-t border-zinc-100 dark:border-zinc-800">
                      <td className="px-4 py-1.5 text-zinc-500">{clock(r.ts)}</td>
                      <td>{r.direction}</td>
                      <td className={`text-right ${p >= 0 ? "text-emerald-600 dark:text-emerald-400" : "text-rose-600 dark:text-rose-400"}`}>{money(p)}</td>
                      <td className="px-4 text-right text-zinc-500">{money(Number(r.day_pnl))}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </section>

        <section className="overflow-hidden rounded-xl border border-zinc-200 bg-white dark:border-zinc-800 dark:bg-zinc-900 md:col-span-2">
          <h2 className="border-b border-zinc-200 px-4 py-2 text-sm font-medium dark:border-zinc-800">Events</h2>
          <ul className="max-h-96 divide-y divide-zinc-100 overflow-auto text-sm dark:divide-zinc-800">
            {events.length === 0 && <li className="px-4 py-6 text-center text-zinc-500">Nothing yet.</li>}
            {events.map((e, i) => (
              <li key={i} className="px-4 py-2">
                <div className={e.level === "error" ? "text-rose-600 dark:text-rose-400" : ""}>{e.text}</div>
                <div className="text-xs text-zinc-500">{ago(e.ts)}</div>
              </li>
            ))}
          </ul>
        </section>
      </div>
      <p className="text-center text-xs text-zinc-500">Refreshes every 15s. Demo and paper results are not a promise of real-money results.</p>
    </main>
  );
}
