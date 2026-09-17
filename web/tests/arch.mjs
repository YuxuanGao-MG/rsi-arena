// archive.js against an independent reimplementation of loop/archive.py's
// spec, written naively here on purpose: the loop commits a new archive every
// day it runs, so pinned counts go stale overnight, but two implementations
// of the same spec must agree on ANY archive. The invariants at the bottom
// are the ones that were once bugs — a flat refusal entry owning the archive,
// the seed wearing the promoted treatment.
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { readFileSync } from "node:fs";
const HERE = dirname(fileURLToPath(import.meta.url));
const W = join(HERE, "..");
import "./harness.mjs";
const A = await import(`${W}/archive.js`);

let bad = 0;
const check = (name, got, want) => {
  const ok = JSON.stringify(got) === JSON.stringify(want);
  if (!ok) { bad++; console.log(`FAIL ${name}: got ${JSON.stringify(got)}, want ${JSON.stringify(want)}`); }
  else console.log(`ok   ${name}: ${JSON.stringify(got).slice(0, 60)}`);
};

const RAW = JSON.parse(readFileSync(join(HERE, "..", "..", "runs", "archive.json"), "utf8")).entries;

// -- the naive reference: same spec, different shape ------------------------
const refContested = (() => {
  const seen = {};
  for (const e of RAW) for (const i of Object.keys(e.scores)) seen[i] = (seen[i] || 0) + 1;
  return new Set(Object.keys(seen).filter(i => seen[i] > 1));
})();
const refBest = (() => {
  const best = new Map();
  for (const i of refContested) {
    let top = -Infinity;
    for (const e of RAW) if (i in e.scores && e.scores[i] > top) top = e.scores[i];
    best.set(i, top);
  }
  return best;
})();
const refWins = new Map(RAW.map(e => [e.id,
  Object.keys(e.scores).filter(i => refBest.has(i) && e.scores[i] >= refBest.get(i))]));
const refDominates = (a, b) => {
  const shared = Object.keys(a.scores).filter(i => i in b.scores);
  if (shared.length < 4) return false;
  return shared.every(i => a.scores[i] >= b.scores[i])
      && shared.some(i => a.scores[i] > b.scores[i]);
};
const refFrontier = (() => {
  const contenders = RAW.filter(e => refWins.get(e.id).length
    || !Object.keys(e.scores).some(i => refContested.has(i)));
  return new Set(contenders
    .filter(e => !contenders.some(o => o.id !== e.id && refDominates(o, e)))
    .map(e => e.id));
})();

// -- archive.js, on the same data -------------------------------------------
const entries = await A.loadArchive({});
const disputed = A.contested(entries);
const best = A.instanceBest(entries, disputed);
const won = A.wins(entries, best);
const front = A.frontier(entries, won, disputed);

check("entry count matches the file", entries.length, RAW.length);
check("contested sets agree", disputed.size, refContested.size);
check("win counts agree per entry",
  Object.fromEntries(entries.map(e => [e.id, (won.get(e.id) || []).length])),
  Object.fromEntries(RAW.map(e => [e.id, refWins.get(e.id).length])));
check("frontiers agree", [...front].sort(), [...refFrontier].sort());

// -- the invariants that were once bugs --------------------------------------
const seed = entries.find(e => e.isSeed);
check("the seed is identified", !!seed, true);
check("the seed carries the promoted flag it must not wear as green", seed.promoted, true);
const flatEntries = entries.filter(e => e.flat);
check("a flat entry exists to test with", flatEntries.length >= 1, true);
check("no flat entry sits on the frontier",
      flatEntries.filter(e => front.has(e.id)).map(e => e.id), []);
for (const e of entries)
  for (const i of won.get(e.id) || [])
    if (!disputed.has(i)) { bad++; console.log(`FAIL win on uncontested instance ${i}`); }

console.log(bad ? `\n${bad} archive check(s) failed` : "\narchive matches the reference");
process.exit(bad ? 1 : 0);
