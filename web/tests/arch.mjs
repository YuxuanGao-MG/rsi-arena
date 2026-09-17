import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
// Resolved from this file, not from where it happened to be written. These
// imported absolute paths into /tmp and passed locally for exactly as long as
// that directory survived, which is the oldest bug there is.
const HERE = dirname(fileURLToPath(import.meta.url));
const W = join(HERE, "..");
import "./harness.mjs";
const A = await import(`${W}/archive.js`);
const entries = await A.loadArchive({});
const best = A.instanceBest(entries), won = A.wins(entries, best), front = A.frontier(entries, won);
console.log("js:     entries", entries.length, "instances", best.size, "frontier", front.size, [...front]);
console.log("wins:", [...won].map(([k,v])=>[v.length,k]).sort((a,b)=>b[0]-a[0]).slice(0,6));
