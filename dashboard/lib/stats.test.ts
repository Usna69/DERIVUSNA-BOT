import { test } from "node:test";
import assert from "node:assert/strict";
import { breakeven, winStats, summarize, verdict, rStats, rVerdict, dailyPnl } from "./stats.ts";

test("break-even at 92% payout", () => assert.ok(Math.abs(breakeven(0.92) - 0.5208) < 1e-3));

test("coin flip is not an edge", () => {
  const s = winStats(500, 1000, 0.95)!;
  assert.ok(s.pValue > 0.5);
  assert.ok(s.lo < 0.5 && 0.5 < s.hi);
});

test("a real edge is detected (matches backtest.py: p < 0.001)", () => {
  assert.ok(winStats(620, 1000, 0.95)!.pValue < 0.001);
});

test("summary: win rate, drawdown, curve", () => {
  const mk = (profit: number) => ({ ts: new Date(), direction: "CALL", stake: 1, profit, payout_ratio: 0.92 });
  const s = summarize([mk(0.92), mk(-1), mk(-1), mk(0.92)]);
  assert.equal(s.n, 4);
  assert.equal(s.wins, 2);
  assert.ok(Math.abs(s.pnl - -0.16) < 1e-9);
  assert.ok(Math.abs(s.maxDd - 2.0) < 1e-9);
  assert.equal(s.curve.length, 4);
  assert.equal(verdict(s).tone, "neutral");
});

const tr = (profit: number, stake = 0.5, ts = "2026-10-21T10:00:00Z") => ({ ts: new Date(ts), direction: "CALL", stake, profit, payout_ratio: null });

test("R stats: profit divided by planned risk", () => {
  const r = rStats([tr(0.75), tr(-0.5), tr(-0.5), tr(0.75)]);          // +1.5R, -1R, -1R, +1.5R
  assert.equal(r.all.n, 4);
  assert.ok(Math.abs(r.all.mean - 0.25) < 1e-9);
});

test("R verdict: too few, fail and pass", () => {
  assert.equal(rVerdict(rStats([tr(0.75), tr(-0.5)])).label, "Too few trades");
  const losers = Array.from({ length: 400 }, (_, i) => tr(i % 3 === 0 ? 0.75 : -0.5));   // ~33% win at 1.5R: negative
  assert.equal(rVerdict(rStats(losers)).label, "Fail");
  let seed = 1;
  const rnd = () => ((seed = (seed * 16807) % 2147483647) / 2147483647);
  const winners = Array.from({ length: 800 }, () => (rnd() < 0.55 ? tr(0.75) : tr(-0.5)));  // 55% at 1.5R: strong edge
  assert.equal(rVerdict(rStats(winners)).label, "Pass");
});

test("daily P&L groups by UTC day and keeps the latest days", () => {
  const d = dailyPnl([tr(1, 1, "2026-10-20T23:59:00Z"), tr(-0.4, 1, "2026-10-21T00:01:00Z"), tr(0.1, 1, "2026-10-21T09:00:00Z")], 14);
  assert.deepEqual(d.map((x) => x.day), ["2026-10-20", "2026-10-21"]);
  assert.ok(Math.abs(d[1].pnl - -0.3) < 1e-9);
  assert.equal(dailyPnl(Array.from({ length: 30 }, (_, i) => tr(1, 1, `2026-09-${String(i + 1).padStart(2, "0")}T10:00:00Z`)), 14).length, 14);
});
