/* The archive: everything the search has ever proposed, and what it scored.
 *
 * Deliberately not in Supabase and deliberately not the lineage. `rsi.runs`
 * records what was *promoted*, which is a claim about held-out evidence that
 * only the gate may make. This records what was *found*, which is cheaper,
 * larger, and carries no claim at all — three generations of it were sitting in
 * each run's `gepa` directory as pickled GEPA state, never read by anything.
 * Conflating the two is how an archive turns into a leaderboard and stops
 * preserving the losers that make it worth having.
 *
 * The arithmetic below is `rsi_arena/loop/archive.py` rewritten in JavaScript.
 * If the two disagree, the Python is right and this is the bug.
 */

import { local } from "./data.js";

/** Scores are the optimizer's value, not skill: 0.5 is silence, 1.0 is +10c of edge. */
export const SILENCE = 0.5;

/** Fewer shared instances than this and a comparison is not worth making. */
const MIN_SHARED = 4;

export async function loadArchive({ signal } = {}) {
  const data = await local("/archive.json", { signal });
  const entries = (data && data.entries) || [];
  for (const e of entries) {
    const values = Object.values(e.scores || {});
    e.n = values.length;
    e.mean = values.length ? values.reduce((a, b) => a + b, 0) / values.length : null;
    e.beatSilence = values.filter(v => v > SILENCE).length;
  }
  return entries;
}

/** The best score anyone has recorded on each instance. */
export function instanceBest(entries) {
  const best = new Map();
  for (const e of entries)
    for (const [instance, score] of Object.entries(e.scores || {}))
      if (!best.has(instance) || score > best.get(instance)) best.set(instance, score);
  return best;
}

/**
 * What each candidate is instance-best at, ties included.
 *
 * Ties count for everyone who ties: two candidates that both solve an instance
 * perfectly are both evidence that the instance is solvable that way.
 */
export function wins(entries, best = instanceBest(entries)) {
  const out = new Map();
  for (const e of entries) {
    const mine = [];
    for (const [instance, score] of Object.entries(e.scores || {}))
      if (score >= best.get(instance)) mine.push(instance);
    out.set(e.id, mine);
  }
  return out;
}

/**
 * `a` is at least as good as `b` everywhere they overlap, and better somewhere.
 *
 * Judged only on shared instances: a candidate evaluated on forty windows
 * cannot dominate one evaluated on four hundred just by having been asked fewer
 * questions.
 */
export function dominates(a, b) {
  const shared = Object.keys(a.scores || {}).filter(k => k in (b.scores || {}));
  if (shared.length < MIN_SHARED) return false;
  let better = false;
  for (const instance of shared) {
    if (a.scores[instance] < b.scores[instance]) return false;
    if (a.scores[instance] > b.scores[instance]) better = true;
  }
  return better;
}

/** Candidates that are best at something and dominated by nothing. */
export function frontier(entries, won = wins(entries)) {
  const contenders = entries.filter(e => (won.get(e.id) || []).length);
  const keep = new Set();
  for (const e of contenders)
    if (!contenders.some(other => other.id !== e.id && dominates(other, e))) keep.add(e.id);
  return keep;
}

/** `KXEPLGAME-26SEP05NFOTOT-NFO@2026-09-05T14:05:00+00:00` → the event ticker. */
export function fixtureOf(instance) {
  const ticker = String(instance).split("@")[0];
  const cut = ticker.lastIndexOf("-");
  return cut > 0 ? ticker.slice(0, cut) : ticker;
}

/** Mean score per candidate over one fixture's instances, for the "only thing that worked" table. */
export function byFixture(entries) {
  const out = new Map();
  for (const e of entries) {
    for (const [instance, score] of Object.entries(e.scores || {})) {
      const fixture = fixtureOf(instance);
      if (!out.has(fixture)) out.set(fixture, new Map());
      const per = out.get(fixture);
      if (!per.has(e.id)) per.set(e.id, { sum: 0, n: 0 });
      const cell = per.get(e.id);
      cell.sum += score; cell.n += 1;
    }
  }
  return out;
}

/** The lineage the search actually walked: parent chains, cycles cut. */
export function tree(entries) {
  const byId = new Map(entries.map(e => [e.id, e]));
  const children = new Map();
  const roots = [];
  for (const e of entries) {
    const parent = e.parent && e.parent !== e.id && byId.has(e.parent) ? e.parent : null;
    if (parent) {
      if (!children.has(parent)) children.set(parent, []);
      children.get(parent).push(e);
    } else {
      roots.push(e);
    }
  }
  return { byId, children, roots,
           orphans: roots.filter(e => e.parent && !byId.has(e.parent)) };
}
