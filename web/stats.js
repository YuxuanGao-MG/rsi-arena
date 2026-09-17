/* The arithmetic the page is allowed to do for itself.
 *
 * Pooled skill is not the mean of per-window skill, and the difference is not
 * academic: the mean divides by a benchmark that differs per window, so a
 * tenth-of-a-cent window and a ten-cent window carry equal weight in a mean and
 * wildly different weight in the pooled sum. The loop found this the hard way —
 * the optimizer's objective and the gate's statistic disagreed on exactly the
 * windows where the market did not move, and GEPA went straight for the gap.
 *
 * So this is `topics/kalshi_horizon/score.py:pooled_skill` rewritten in
 * JavaScript, floor and all, from the `err` and `naive_error` columns that
 * `scripts/publish_runs.py` already writes. If the two ever disagree, the
 * Python is right and this is the bug.
 */

/** One tick. The smallest benchmark error that means anything. */
export const TICK = 0.01;

/** The gated statistic: sum the error removed, sum the benchmark, then divide. */
export function pooled(rows) {
  let removed = 0, benchmark = 0, scored = 0, quiet = 0;
  for (const r of rows) {
    if (r.err == null || r.naive_error == null) continue;
    scored += 1;
    if (r.naive_error < 1e-4) quiet += 1;
    removed += r.naive_error - r.err;
    benchmark += Math.max(r.naive_error, TICK);
  }
  return {
    skill: benchmark ? removed / benchmark : null,
    removed, benchmark, scored, quiet,
    unscored: rows.length - scored,
  };
}

/**
 * The same sum over the windows that actually moved.
 *
 * The floor keeps a dead market scored rather than excused, which is right for
 * the gate and misleading as a headline: a quarter of these windows never moved
 * a tick, and on those the best a harness can do is score zero. This is the
 * number to read beside the pooled one, never instead of it.
 */
export function pooledOnMoves(rows) {
  const moved = rows.filter(r => (r.naive_error ?? 0) >= TICK);
  return { ...pooled(moved), instances: moved.length };
}

/** How many of these windows the harness answered by repeating the mid. */
export const echoes = rows => rows.filter(r => r.echoed).length;

/** Group rows by a key, preserving first-seen order. */
export function groupBy(rows, key) {
  const out = new Map();
  for (const r of rows) {
    const k = typeof key === "function" ? key(r) : r[key];
    if (!out.has(k)) out.set(k, []);
    out.get(k).push(r);
  }
  return out;
}

/**
 * The three published generations were scored under three versions of this
 * metric, and the numbers in their manifests are not on one scale.
 *
 * `runs/gen1-floored` was scored with the superseded per-window skill —
 * `1 - error/benchmark`, which handed a full point to a harness that said
 * nothing on a market that did not move — and published +0.043 for an
 * incumbent whose windows recompute to -0.011 today. `runs/gen1` and
 * `runs/gen1-1k` were scored with the numerator fixed but the denominator
 * unfloored. Every one of those numbers was the gate's statistic on the day it
 * ran, so none of them is wrong; they are just three different questions.
 *
 * The windows are the same either way, and `err` and `naive_error` are stored
 * per window, so anything published can be recomputed on today's formula. That
 * is what the cross-generation chart plots — otherwise a metric change reads as
 * a result.
 *
 * The difference the gate reads survives this almost untouched (-0.0016 either
 * way on gen1-floored), which is the reassuring half: the level moved, the
 * paired comparison did not.
 */
export function recompute(rows) {
  const byRun = new Map();
  for (const r of rows) {
    const key = `${r.run_id}|${r.side}`;
    if (!byRun.has(key)) byRun.set(key, []);
    byRun.get(key).push(r);
  }
  const out = new Map();
  for (const [key, group] of byRun) {
    const [runId, side] = key.split("|");
    if (!out.has(runId)) out.set(runId, {});
    out.get(runId)[side] = pooled(group);
  }
  return out;
}

/** How far a published statistic has drifted from today's arithmetic. */
export function metricGap(run, side, recomputed) {
  const published = run[side] && run[side].holdout && run[side].holdout.statistic;
  const here = recomputed && recomputed[side] && recomputed[side].skill;
  if (typeof published !== "number" || typeof here !== "number") return null;
  return { published, here, gap: here - published, differs: Math.abs(here - published) > 0.002 };
}

/** The best number anywhere in these runs, or null when there is not one yet. */
export function bestHoldout(runs) {
  const values = [];
  for (const r of runs)
    for (const side of ["baseline", "candidate"]) {
      const v = r[side] && r[side].holdout && r[side].holdout.statistic;
      if (typeof v === "number" && Number.isFinite(v)) values.push(v);
    }
  return values.length ? Math.max(...values) : null;
}

/**
 * The USD ceiling a generation was run under, or null.
 *
 * The loop grew a per-generation ceiling after the page did; `publish_runs.py`
 * copies `manifest["llm"]` through untouched, so whichever key the loop writes
 * turns up here. Reading several and showing nothing rather than a zero is the
 * difference between "this generation had no ceiling recorded" and a fabricated
 * limit of $0.00.
 */
export function ceilingOf(run) {
  const llm = run && run.llm;
  if (!llm) return null;
  for (const key of ["ceiling_usd", "budget_usd", "max_usd", "limit_usd", "cap_usd"]) {
    const v = llm[key];
    if (typeof v === "number" && Number.isFinite(v) && v > 0) return v;
  }
  return null;
}
