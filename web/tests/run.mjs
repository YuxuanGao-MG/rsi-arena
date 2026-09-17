import { state, root } from "/tmp/rsicheck/harness.mjs";
const W = "/Users/vincent_lancaster/Desktop/OctoMesh/rsi-arena-mine/web";
const { toHTML } = await import(`${W}/dom.js`);
const { invalidate } = await import(`${W}/data.js`);

const views = {
  runs:    [(await import(`${W}/views/runs.js`)).runsView, { params: {}, query: {} }],
  run:     [(await import(`${W}/views/run.js`)).runView, { params: { id: "gen1-floored" }, query: {} }],
  runBase: [(await import(`${W}/views/run.js`)).runView, { params: { id: "gen1-floored" }, query: { side: "baseline" } }],
  window:  [(await import(`${W}/views/window.js`)).windowView, { params: { id: "3" }, query: {} }],
  lineage: [(await import(`${W}/views/lineage.js`)).lineageView, { params: {}, query: {} }],
  cmpPick: [(await import(`${W}/views/compare.js`)).compareView, { params: {}, query: {} }],
  cmpRun:  [(await import(`${W}/views/compare.js`)).compareView, { params: { runId: "gen1-floored" }, query: {} }],
  cmpDuel: [(await import(`${W}/views/compare.js`)).compareView,
            { params: { runId: "gen1-floored", fixture: "401878780" }, query: {} }],
  votes:   [(await import(`${W}/views/votes.js`)).votesView, { params: {}, query: {} }],
  live:    [(await import(`${W}/views/live.js`)).liveView, { params: {}, query: {} }],
  cost:    [(await import(`${W}/views/cost.js`)).costView, { params: {}, query: {} }],
};
let archiveView = null;
try { archiveView = (await import(`${W}/views/archive.js`)).archiveView; } catch (e) { /* not built yet */ }
if (archiveView) views.archive = [archiveView, { params: {}, query: {} }];

let bad = 0;
for (const [name, [fn, ctx]] of Object.entries(views)) {
  invalidate();
  try {
    const out = await fn({ ...ctx, signal: new AbortController().signal });
    const body = toHTML(out.body);
    if (out.ready) out.ready(root());
    const flaws = [];
    if (!out.title || !out.heading) flaws.push("no title/heading");
    if (body.length < 200) flaws.push("body suspiciously short");
    if (/undefined|\[object Object\]|NaN/.test(body)) {
      const m = body.match(/.{40}(undefined|\[object Object\]|NaN).{40}/);
      flaws.push(`leaks ${m ? JSON.stringify(m[0]) : "?"}`);
    }
    console.log(`${flaws.length ? "FAIL" : "ok  "} ${name.padEnd(9)} ${String(body.length).padStart(7)} bytes  ${toHTML(out.heading).replace(/<[^>]+>/g, "").slice(0, 60)}`);
    if (flaws.length) { bad++; console.log("       " + flaws.join("; ")); }
  } catch (err) {
    bad++;
    console.log(`FAIL ${name}: ${err && err.stack ? err.stack.split("\n").slice(0, 4).join("\n   ") : err}`);
  }
}
console.log(bad ? `\n${bad} view(s) failed` : "\nall views rendered");
