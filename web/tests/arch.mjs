// archive.js against loop/archive.py: same contested set, same frontier, same
// win counts — computed independently in Python and pinned here. The pinned
// numbers are the ones that caught gen5's refused harness owning the archive:
// scored an identical 0.5 on 2,600 windows nobody else had seen, it was
// "instance-best" on 2,586 of them until only contested instances counted.
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
const HERE = dirname(fileURLToPath(import.meta.url));
const W = join(HERE, "..");
import "./harness.mjs";
const A = await import(`${W}/archive.js`);

let bad = 0;
const check = (name, got, want) => {
  if (got !== want) { bad++; console.log(`FAIL ${name}: got ${got}, want ${want}`); }
  else console.log(`ok   ${name}: ${got}`);
};

const entries = await A.loadArchive({});
const disputed = A.contested(entries);
const best = A.instanceBest(entries, disputed);
const won = A.wins(entries, best);
const front = A.frontier(entries, won, disputed);

check("entries", entries.length, 15);
check("contested instances", disputed.size, 102);
check("frontier size", front.size, 14);

const gen5 = entries.find(e => e.generation === "gen5");
check("gen5 refusal entry wins on contested only", (won.get(gen5.id) || []).length, 2);
check("gen5 refusal entry is flat", gen5.flat, true);

const seed = entries.find(e => e.isSeed);
check("the seed is identified", !!seed, true);
check("the seed is the promoted-flagged entry", seed.promoted, true);

// The seed has no scores at all — it is kept as "never compared", not as a
// winner — and no flat entry may outrank a candidate that actually forecast.
const flatOnFrontier = entries.filter(e => e.flat && front.has(e.id));
check("no flat entry sits on the frontier", flatOnFrontier.length, 0);

console.log(bad ? `\n${bad} archive check(s) failed` : "\narchive matches the Python");
process.exit(bad ? 1 : 0);
