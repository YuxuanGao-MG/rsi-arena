/* Human names for things the loop names for machines.
 *
 * "kalshi-horizon-5m+gen1-1k" and a twelve-hex fingerprint identify a harness
 * precisely and tell a visitor nothing. The unit a person thinks in is the
 * generation and its fate — "generation 5's rewrite (ran out of money)" — so
 * every page speaks that language and keeps the internal name in small mono
 * underneath for whoever wants it.
 *
 * Nothing has ever been promoted, so every incumbent is still the original
 * harness; the helpers check rather than assume, because the first promotion
 * would otherwise turn every label on the site into a lie at once.
 */

import { runStatus } from "./stats.js";

/**
 * "generation N" by creation order, given the runs to order by.
 *
 * Not parsed out of the id: the first three run directories are gen1,
 * gen1-1k and gen1-floored — three experiments the reader would meet as
 * "generation 1" three times, with internal suffixes leaking into prose.
 * Creation order is the story a person actually follows, and the internal id
 * stays in small mono wherever the name appears.
 */
export function genName(runId, runs) {
  const ordered = [...(runs || [])]
    .filter(r => r.created)
    .sort((a, b) => new Date(a.created) - new Date(b.created));
  const n = ordered.findIndex(r => r.id === runId);
  if (n >= 0) return `generation ${n + 1}`;
  return String(runId || "");
}

/** One word (or three) of fate, for pinning to a name. */
export function fateOf(run) {
  const status = runStatus(run);
  if (status === "incomplete") return "crashed";
  if (status === "exhausted") return "ran out of money";
  if (run.accepted) return "promoted";
  if (run.decision?.holdout?.underpowered) return "rejected — too close to call";
  if (run.candidate_fp && run.candidate_fp === run.incumbent_fp)
    return "changed nothing";
  return "rejected";
}

/** "generation 5's rewrite (ran out of money)". */
export const rewriteLabel = (run, runs) =>
  `${genName(run.id, runs)}'s rewrite (${fateOf(run)})`;

/** What the rewrite was up against — the original until something survives. */
export function incumbentLabel(run, allRuns = []) {
  const promotedBefore = allRuns.some(r =>
    r.accepted && new Date(r.created) < new Date(run.created));
  return promotedBefore ? "the incumbent it challenged" : "the original harness";
}

/** Skill for humans: points of the no-change baseline's error removed. */
export function pointsText(v) {
  if (v == null || Number.isNaN(v)) return "not measured";
  const pts = Math.abs(v * 100).toFixed(1);
  if (Math.abs(v) < 0.0005) return "matched the no-change guess exactly";
  return v > 0 ? `beat the no-change guess by ${pts} points`
               : `lost to the no-change guess by ${pts} points`;
}
