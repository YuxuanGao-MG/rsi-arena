import "/tmp/rsicheck/harness.mjs";
const W = "/Users/vincent_lancaster/Desktop/OctoMesh/rsi-arena-mine/web";
const A = await import(`${W}/archive.js`);
const entries = await A.loadArchive({});
const best = A.instanceBest(entries), won = A.wins(entries, best), front = A.frontier(entries, won);
console.log("js:     entries", entries.length, "instances", best.size, "frontier", front.size, [...front]);
console.log("wins:", [...won].map(([k,v])=>[v.length,k]).sort((a,b)=>b[0]-a[0]).slice(0,6));
