/* The generations, loaded once and read two ways.
 *
 * Overview draws the headline chart and the latest verdict; Metrics draws the
 * full table, the drift panel, and the banners. Both need the same recomputed
 * levels, the same status classification, and the same chart points — and two
 * copies of that arithmetic is how the two pages would eventually disagree.
 */

import { qAll, qs, topicFilter } from "./data.js";
import { groupBy, recompute, metricGap, runStatus } from "./stats.js";
import { DEFAULT } from "./topics.js";

export const RUN_COLUMNS = "id,topic,created,parent,accepted,reasons,incumbent_fp,candidate_fp," +
                           "baseline,candidate,decision,search,llm,split";

/** What a broken record's slot on the chart says instead of a number. */
export const GAP_LABEL = { exhausted: "ran out of money", incomplete: "crashed" };

export async function loadGenerations({ signal, topic = DEFAULT }) {
  const runs = await qAll(`runs?select=${RUN_COLUMNS}${topicFilter(topic)}&order=created.desc`,
                          { signal, pageSize: 200, max: 2000 });
  if (!runs.length) {
    return { runs, windows: [], level: new Map(), byRunSide: new Map(),
             statusOf: new Map(), points: [], measured: [], best: null,
             exhausted: [], incomplete: [], drifted: [], underpowered: [] };
  }

  // Every held-out window of every generation, both sides, narrow. It pays for
  // three things at once: the sparklines, the pooled levels recomputed on one
  // metric, and the refusal exclusion — `ok` and `scored` ride along so a
  // budget refusal is never pooled as a forecast.
  let windows = [];
  if (runs.length <= 24) {
    windows = await qAll(
      `rollouts?select=run_id,side,skill,err,naive_error,unmeasurable,ok,scored` +
      `&split=eq.holdout&run_id=${qs.inList(runs.map(r => r.id))}`,
      { signal, max: 24_000 });
    // The tick each window is floored at is its topic's. Stamped from the run
    // rather than selected: the runs were filtered by topic already, and a
    // window's own `topic` column only exists from migration 008 on.
    for (const w of windows) w.topic = topic;
  }
  const level = recompute(windows);
  const byRunSide = groupBy(windows, r => `${r.run_id}|${r.side}`);
  const statusOf = new Map(runs.map(r => [r.id, runStatus(r)]));
  const ordered = [...runs].reverse();          // oldest first, as time reads

  const points = ordered.map(r => {
    const status = statusOf.get(r.id);
    if (status !== "complete") {
      // A run that ran out of money or crashed measured nothing on held-out.
      // Its manifest still carries zero-shaped scoreboards, and falling
      // through to them is how a generation whose candidate never answered a
      // window briefly posted "0.000" as the best number on the front page.
      return { id: r.id, accepted: false, gap: GAP_LABEL[status] };
    }
    const mine = level.get(r.id) || {};
    const hold = r.decision?.holdout || {};
    return {
      id: r.id, accepted: !!r.accepted,
      // Sometimes both sides made the same forecast on every window — the
      // search returned its parent, or the rewrite never disagreed. The
      // zero-width interval that produces is true, and as "0.000 to 0.000" it
      // reads as a suspicious statistic rather than what it is.
      unchanged: (r.candidate_fp && r.candidate_fp === r.incumbent_fp)
        || (hold.diff === 0 && hold.low === 0 && hold.high === 0),
      inc: mine.baseline?.skill ?? r.baseline?.holdout?.statistic ?? null,
      cand: mine.candidate?.skill ?? r.candidate?.holdout?.statistic ?? null,
      diff: hold.diff ?? null,
      low: hold.usable === false ? null : hold.low ?? null,
      high: hold.usable === false ? null : hold.high ?? null,
      usable: hold.usable !== false,
      detectable: hold.detectable ?? null, underpowered: !!hold.underpowered,
    };
  });

  const measured = points.filter(p => !p.gap);
  const bestRaw = measured.reduce(
    (a, p) => Math.max(a, p.cand ?? -Infinity, p.inc ?? -Infinity), -Infinity);
  // The metric drifted under both sides — the loudest case is an *incumbent*
  // (+0.043 published, -0.011 recomputed) — so both are checked.
  const drifted = runs.flatMap(r => statusOf.get(r.id) !== "complete" ? [] :
    ["baseline", "candidate"].flatMap(side => {
      const g = metricGap(r, side, level.get(r.id));
      return g && g.differs ? [{ run: r, ...g }] : [];
    }));

  return {
    runs, windows, level, byRunSide, statusOf, points, measured,
    best: Number.isFinite(bestRaw) ? bestRaw : null,
    exhausted: runs.filter(r => statusOf.get(r.id) === "exhausted"),
    incomplete: runs.filter(r => statusOf.get(r.id) === "incomplete"),
    drifted,
    underpowered: runs.filter(r => r.decision?.holdout?.underpowered
                                    && statusOf.get(r.id) === "complete"),
  };
}
