/* The archive: everything the search has ever proposed, and what it scored.
 *
 * Deliberately not in Supabase and deliberately not the lineage. `rsi.runs`
 * records what was *promoted*, which is a claim about held-out evidence that
 * only the gate may make. This records what was *found*, which is cheaper,
 * larger, and carries no claim at all — generations of it were sitting in each
 * run's `gepa` directory as pickled GEPA state, never read by anything.
 * Conflating the two is how an archive turns into a leaderboard and stops
 * preserving the losers that make it worth having.
 *
 * The arithmetic below is `rsi_arena/loop/archive.py` rewritten in JavaScript.
 * If the two disagree, the Python is right and this is the bug.
 */

import { local } from "./data.js";
import { topicOf, DEFAULT } from "./topics.js";

/** Scores are the optimizer's value, not skill: 0.5 is silence, 1.0 is +10c of edge. */
export const SILENCE = 0.5;

/** Fewer shared instances than this and a comparison is not worth making. */
const MIN_SHARED = 4;

/** One archive per topic — each loop keeps its own file, served beside the page. */
export async function loadArchive({ signal, topic = DEFAULT } = {}) {
  const data = await local(topicOf(topic).archive, { signal });
  const entries = (data && data.entries) || [];
  for (const e of entries) {
    const values = Object.values(e.scores || {});
    e.n = values.length;
    e.mean = values.length ? values.reduce((a, b) => a + b, 0) / values.length : null;
    e.beatSilence = values.filter(v => v > SILENCE).length;
    // Every score identical separates nothing: silence across a whole valset
    // looks like this, and so does a harness refused identically everywhere.
    e.flat = values.length >= 4
      && new Set(values.map(v => Math.round(v * 1e9))).size === 1;
    // The seed is marked `promoted` because it is the standing incumbent, not
    // because the gate ever accepted anything. Two different claims, and the
    // green treatment belongs only to the second, which has never been made.
    e.isSeed = e.generation === "seed"
      || /harness this generation started from/.test(e.note || "");
  }
  return entries;
}

/**
 * Instances more than one candidate has actually been scored on.
 *
 * An instance only one candidate has seen cannot rank candidates — there is
 * nobody to be better *than*. Counting it anyway is how gen5's refused harness
 * came to own the archive: scored an identical 0.5 on all 2,600 windows of its
 * own split, none of which any earlier candidate had seen, it was trivially
 * instance-best on 2,586 of them.
 */
export function contested(entries) {
  const seen = new Map();
  for (const e of entries)
    for (const instance of Object.keys(e.scores || {}))
      seen.set(instance, (seen.get(instance) || 0) + 1);
  return new Set([...seen].filter(([, n]) => n > 1).map(([i]) => i));
}

/** The best score anyone has recorded on each contested instance. */
export function instanceBest(entries, disputed = contested(entries)) {
  const best = new Map();
  for (const e of entries)
    for (const [instance, score] of Object.entries(e.scores || {}))
      if (disputed.has(instance) && (!best.has(instance) || score > best.get(instance)))
        best.set(instance, score);
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
      if (best.has(instance) && score >= best.get(instance)) mine.push(instance);
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

/** Candidates that are best at something and dominated by nothing.
 *
 * Never compared is not the same as compared and beaten: a candidate whose
 * whole valset rotated away shares no contested instance with anything, has
 * won nothing, and has been beaten by nothing — it stays, so the frontier does
 * not empty every time the split moves.
 */
export function frontier(entries, won = wins(entries), disputed = contested(entries)) {
  const contenders = entries.filter(e => (won.get(e.id) || []).length
    || !Object.keys(e.scores || {}).some(i => disputed.has(i)));
  const keep = new Set();
  for (const e of contenders)
    if (!contenders.some(other => other.id !== e.id && dominates(other, e))) keep.add(e.id);
  return keep;
}

/** `KXEPLGAME-26SEP05NFOTOT-NFO@2026-09-05T14:05:00+00:00` → the event ticker.
 * Kalshi's grouping; every topic's is `TOPICS[t].groupOf`. */
export const fixtureOf = instance => topicOf(DEFAULT).groupOf(instance);

/** Mean score per candidate over one group's instances — one match, one
 * symbol-day, one UTC day — for the "only thing that worked" table. */
export function byFixture(entries, topic = DEFAULT) {
  const groupOf = topicOf(topic).groupOf;
  const out = new Map();
  for (const e of entries) {
    for (const [instance, score] of Object.entries(e.scores || {})) {
      const fixture = groupOf(instance);
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
