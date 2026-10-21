import { test } from "node:test";
import assert from "node:assert/strict";
import { breakeven, winStats, summarize, verdict } from "./stats.ts";

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
