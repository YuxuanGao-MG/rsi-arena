import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
// Resolved from this file, not from where it happened to be written. These
// imported absolute paths into /tmp and passed locally for exactly as long as
// that directory survived, which is the oldest bug there is.
const HERE = dirname(fileURLToPath(import.meta.url));
const W = join(HERE, "..");
import { state, root } from "./harness.mjs";
import { readFileSync } from "node:fs";
const { pooled, pooledOnMoves } = await import(`${W}/stats.js`);
const { q, qAll, invalidate, ApiError, configProblem } = await import(`${W}/data.js`);
const { toHTML } = await import(`${W}/dom.js`);
const FX = JSON.parse(readFileSync(join(HERE, "fixtures.json"), "utf8"));

let bad = 0;
const check = (name, got, want, tol = 0) => {
  const ok = typeof want === "number" ? Math.abs(got - want) <= tol : got === want;
  if (!ok) { bad++; console.log(`FAIL ${name}: got ${got}, want ${want}`); }
  else console.log(`ok   ${name}: ${got}`);
};

// 1. Pooled skill on today's formula, against values computed independently in
// Python from the same rollouts. The manifests do not agree with these and are
// not meant to: gen1-floored was scored under the superseded metric.
for (const [run, side, split, want] of [
  ["gen1-floored", "candidate", "holdout", -0.0127],
  ["gen1-floored", "baseline", "holdout", -0.0111],
  ["gen1-floored", "candidate", "train", 0.0012],
  ["gen1-1k", "candidate", "holdout", -0.0366],
]) {
  const rows = FX.rollouts.filter(r => r.run_id === run && r.side === side && r.split === split);
  check(`pooled ${run}/${side}/${split}`, Number(pooled(rows).skill.toFixed(4)), want, 0.0002);
}
// skill_on_moves from the same manifest
const cand = FX.rollouts.filter(r => r.run_id === "gen1-floored" && r.side === "candidate" && r.split === "holdout");
check("skill_on_moves", Number(pooledOnMoves(cand).skill.toFixed(4)), -0.0135, 0.0002);
// The level moved with the metric; the difference the gate reads did not.
const inc = FX.rollouts.filter(r => r.run_id === "gen1-floored" && r.side === "baseline" && r.split === "holdout");
check("the gate's difference survives the metric change",
      Number((pooled(cand).skill - pooled(inc).skill).toFixed(4)), -0.0016, 0.0005);
check("quiet count", pooled(cand).quiet, 17);

// 2. Config validation.
const saved = globalThis.RSI;
globalThis.RSI = { url: "", key: "" };
check("configProblem empty url", /no SUPABASE_URL/.test(configProblem() || ""), true);
globalThis.RSI = { url: "/", key: "x" };
check("configProblem relative url", /not an absolute/.test(configProblem() || ""), true);
globalThis.RSI = saved;
check("configProblem when set", configProblem(), null);

// 3. Failure classification.
invalidate();
state.failNext = async () => { throw new TypeError("Failed to fetch"); };
try { await q("runs?select=*"); bad++; console.log("FAIL network did not throw"); }
catch (e) { check("network error kind", e.kind, "network"); check("network retryable", e.retryable, true); }

invalidate();
state.failNext = async () => ({ ok: true, status: 200,
  headers: { get: k => (k.toLowerCase() === "content-type" ? "text/html" : null) },
  json: async () => ({}), text: async () => "<!doctype html>" });
try { await q("runs?select=*"); bad++; console.log("FAIL html did not throw"); }
catch (e) { check("html-instead-of-json kind", e.kind, "parse"); }

invalidate();
state.failNext = async () => ({ ok: false, status: 400,
  headers: { get: () => "application/json" }, text: async () => '{"hint":"column rsi.x does not exist"}' });
try { await q("runs?select=*"); bad++; console.log("FAIL 400 did not throw"); }
catch (e) {
  check("http error kind", e.kind, "http");
  check("http message hides the body", /column/.test(e.message), false);
}

// 4. The error panel renders and offers a retry.
const { errorPanel } = await import(`${W}/dom.js`);
const panel = toHTML(errorPanel(new ApiError("timeout", "The database did not answer within 15s."), "retry"));
check("error panel has retry button", /data-action="retry"/.test(panel), true);
check("error panel escapes", /<script/.test(toHTML(errorPanel(new ApiError("http", "<script>x</script>"), "retry"))), false);

// 5. Escaping: a fixture id carrying a quote must not break an attribute.
const { html, esc } = await import(`${W}/dom.js`);
const nasty = `4018'78780" onmouseover=alert(1) <img src=x>`;
const out = toHTML(html`<a href="${nasty}" data-x="${nasty}">${nasty}</a>`);
check("no raw quote escapes attribute", /onmouseover=/.test(out.replace(/&#39;|&quot;/g, "")), true);
check("no raw < survives", /<img/.test(out), false);

// 6. Range pagination assembles every page.
invalidate();
const all = await qAll("rollouts?select=id&run_id=eq.gen1-floored", { pageSize: 100 });
check("qAll paginates", all.length, FX.rollouts.filter(r => r.run_id === "gen1-floored").length);
check("qAll reports the total", all.total, all.length);

// 7. A view whose table is missing degrades rather than throws.
invalidate();
const { liveView } = await import(`${W}/views/live.js`);
state.failNext = async () => ({ ok: false, status: 404, headers: { get: () => "application/json" },
                                text: async () => '{"code":"PGRST205"}' });
const live = await liveView({ params: {}, query: {}, signal: new AbortController().signal });
check("missing live table explains itself", /not installed/.test(toHTML(live.heading)), true);

// ---------------------------------------------------------------------------
// A generation that ran out of money is a gap, not a measurement.
//
// gen5's candidate never answered a window and 555 of its 800 baseline-holdout
// rollouts are budget refusals stored with the mid echoed back. Pooled naively
// they scored the *absence* of a harness as a harness, and the front page
// briefly showed 0.000 — gen5's refusals — as the best held-out skill on the
// site.

const { refused, runStatus } = await import(`${W}/stats.js`);
const g5base = FX.rollouts.filter(r => r.run_id === "gen5" && r.side === "baseline" && r.split === "holdout");
check("gen5 refusals identified", g5base.filter(refused).length, 555);
const g5pool = pooled(g5base);
check("refusals excluded from pooling", g5pool.scored, 245);
check("refusals counted separately", g5pool.refusals, 555);
// The refusals echo the mid, so pooling them drags the statistic toward zero;
// excluded, the 245 real forecasts keep their own (worse) number.
const g5naive = pooled(g5base.map(r => ({ ...r, ok: true, scored: true })));
check("pooling refusals would flatter the number",
      Math.abs(g5naive.skill) < Math.abs(g5pool.skill), true);

const g5run = FX.runs.find(r => r.id === "gen5");
const g4run = FX.runs.find(r => r.id === "gen4");
check("gen5 is an exhausted record", runStatus(g5run), "exhausted");
check("gen4 is an incomplete record", runStatus(g4run), "incomplete");

// The front page: exhausted and incomplete runs are gaps, never data points,
// and never the best-of.
invalidate();
const { metricsView } = await import(`${W}/views/metrics.js`);
const front = await metricsView({ params: {}, query: {}, signal: new AbortController().signal });
const frontHTML = toHTML(front.body);
const bestTile = frontHTML.slice(frontHTML.indexOf("best held-out skill") - 400,
                                 frontHTML.indexOf("best held-out skill"));
check("best-of tile is not gen5's 0.000", />\+?0\.000</.test(bestTile), false);
check("front page names the money running out", /ran out of money/.test(frontHTML), true);
check("gen4 renders as incomplete, never dropped", /incomplete/.test(frontHTML), true);
const g4row = frontHTML.slice(frontHTML.indexOf('generation/gen4'), frontHTML.indexOf('generation/gen4') + 900);
check("gen4's row does not say dropped", />dropped</.test(g4row), false);
check("metrics marks the gaps in its rows",
      (frontHTML.match(/no measurement/g) || []).length >= 2, true);

// The exhausted run's own page says what its windows are.
invalidate();
const { runView } = await import(`${W}/views/run.js`);
const g5page = await runView({ params: { id: "gen5" }, query: { side: "baseline" },
                               signal: new AbortController().signal });
const g5HTML = toHTML(g5page.body);
check("run page counts the refusals out loud",
      /555 of 800 windows here are budget-exhaustion\s+refusals/.test(g5HTML), true);
check("run page heading says it ran out of money", /Ran out of money/.test(g5HTML), true);

// And the candidate side, which has nothing at all, says why.
invalidate();
const g5cand = await runView({ params: { id: "gen5" }, query: {},
                               signal: new AbortController().signal });
check("the empty candidate side blames the money",
      /money ran out/.test(toHTML(g5cand.body)), true);

// ---------------------------------------------------------------------------
// The live chrome: four honest states, classified purely, plus the GitHub
// fallback whose 403 path has to degrade rather than retry.

const { classify, STALE_AFTER_S } = await import(`${W}/status.js`);
const { isoAgo } = await import("./harness.mjs");
const now = Date.now();

const liveRow = { run_id: "gen7", phase: "search",
  detail: { evaluations: 220, spent_usd: 14, budget_usd: 66 },
  started_at: isoAgo(1800), updated_at: isoAgo(30) };
const staleRow = { ...liveRow, updated_at: isoAgo(STALE_AFTER_S + 60) };
const doneRow = { run_id: "gen5", phase: "done",
  detail: { conclusion: "incomplete", reason: "budget exhausted during the baseline" },
  started_at: isoAgo(9000), updated_at: isoAgo(7200) };
const ghRunning = { workflow_runs: [{ id: 35181403396, status: "in_progress",
                                      run_started_at: isoAgo(600) }] };
const ghDone = { workflow_runs: [{ id: 35181403396, status: "completed", conclusion: "success" }] };

let st = classify([liveRow], null, now);
check("live: a fresh non-done row", st.kind, "live");
check("live carries phase", st.phase, "search");
check("live carries spend", st.spentUsd, 14);
st = classify([staleRow], ghRunning, now);
check("stale: a five-minute-silent row beats a running workflow", st.kind, "stale");
check("stale keeps the last-seen time", !!st.lastSeen, true);
st = classify([doneRow], ghRunning, now);
check("running-blind: GitHub says executing, no heartbeat row", st.kind, "running-blind");
st = classify([], ghRunning, now);
check("running-blind with no rows at all", st.kind, "running-blind");
st = classify([doneRow], ghDone, now);
check("idle: the newest word is a verdict", st.kind, "idle");
check("idle carries the conclusion", st.conclusion, "incomplete");
st = classify([], null, now);
check("idle with nothing anywhere", st.kind, "idle");

// The overview hero renders each honest state, exported for exactly this.
const { overviewView, heroFor } = await import(`${W}/views/overview.js`);
invalidate();
const ov = await overviewView({ params: {}, query: {}, signal: new AbortController().signal });
const ovHTML = toHTML(ov.body);
check("overview renders", ovHTML.length > 2000, true);
check("overview offers the guess", /Will the next generation be promoted\?/.test(ovHTML), true);
const ovTable = ovHTML.slice(ovHTML.indexOf("<table"), ovHTML.indexOf("</table>"));
check("no 0.000-to-0.000 interval renders", /0\.000 to 0\.000/.test(ovTable), false);
check("the overview table marks the gaps", (ovTable.match(/no measurement/g) || []).length, 2);

const gFake = { runs: FX.runs, statusOf: new Map(FX.runs.map(r => [r.id, "complete"])),
                level: new Map() };
for (const [label, snap, want] of [
  ["live", { kind: "live", run: "gen7", phase: "search", spentUsd: 14, budgetUsd: 66,
             evaluations: 220, startedAt: isoAgo(1800), updatedAt: isoAgo(30) },
   /gen7 · search/],
  ["stale", { kind: "stale", run: "gen6", phase: "holdout", lastSeen: isoAgo(4000), ageS: 4000 },
   /stopped mid-holdout/],
  ["running-blind", { kind: "running-blind", ghRun: 35181403396, ghStatus: "in_progress",
                      startedAt: isoAgo(600) }, /no progress row exists/],
  ["idle", { kind: "idle", lastRun: "gen5", conclusion: "incomplete",
             reason: "budget exhausted during the baseline" }, /next generation/],
]) {
  const out = toHTML(heroFor(snap, gFake));
  check(`hero ${label} says the right thing`, want.test(out), true);
  check(`hero ${label} leaks nothing`, /undefined|NaN|\[object/.test(out), false);
}
check("hero live shows a budget meter",
      /budget-meter/.test(toHTML(heroFor({ kind: "live", run: "g", phase: "search",
        spentUsd: 14, budgetUsd: 66, startedAt: isoAgo(60), updatedAt: isoAgo(5) }, gFake))), true);
check("hero idle counts down to both crons",
      (toHTML(heroFor({ kind: "idle" }, gFake)).match(/data-tick="until"/g) || []).length, 2);
check("hero reconnecting is labelled",
      /reconnecting/.test(toHTML(heroFor({ kind: "idle", reconnecting: true }, gFake))), true);
check("hero shows a running collection",
      /live collection sweep/.test(toHTML(heroFor({ kind: "idle",
        collection: { running: true, id: 9, startedAt: isoAgo(120) } }, gFake))), true);

// The countdown arithmetic, against hand-computed instants.
const { nextLoopRun, nextLiveRun, untilText } = await import(`${W}/clock.js`);
const wed2am = Date.UTC(2026, 8, 16, 2, 0);        // a Wednesday
check("loop cron from 02:00 is 03:17 same day",
      new Date(nextLoopRun(wed2am)).toISOString(), "2026-09-16T03:17:00.000Z");
check("live cron on a weekday is 19:05",
      new Date(nextLiveRun(wed2am)).toISOString(), "2026-09-16T19:05:00.000Z");
check("live cron after Saturday 15:05 is Sunday 01:05",
      new Date(nextLiveRun(Date.UTC(2026, 8, 19, 16, 0))).toISOString(),
      "2026-09-20T01:05:00.000Z");
check("countdown text", untilText(wed2am + 4 * 3600e3 + 12 * 60e3, wed2am), "in 4h 12m");

// The collection rides along on classify.
const withLive = { workflow_runs: [
  { id: 1, status: "completed", path: ".github/workflows/loop.yml" },
  { id: 2, status: "in_progress", path: ".github/workflows/live.yml",
    run_started_at: isoAgo(300) }] };
st = classify([doneRow], withLive, now);
check("idle while collecting stays idle", st.kind, "idle");
check("but carries the collection", !!(st.collection && st.collection.running), true);

// ---------------------------------------------------------------------------
// The feedback surfaces, against the stateful RPC stubs.

const { rpc } = await import(`${W}/data.js`);
const { rpcState } = await import("./harness.mjs");
const me = "test-voter-1";

// Cast, then read the tally back; switch sides, and the count stays one.
let fb = await rpc("flag_trace", { rollout_id: 3, verdict: "bad", voter: me });
check("flag stored", fb.stored, true);
check("tally counts both voters", (fb.tally.good || 0) + (fb.tally.bad || 0), 2);
fb = await rpc("flag_trace", { rollout_id: 3, verdict: "good", voter: me });
check("switching sides keeps one row",
      (fb.tally.good || 0) + (fb.tally.bad || 0) + (fb.tally.unsure || 0), 2);
check("and moves the verdict", fb.tally.good, 2);

// The guess: cast, switch, crowd stays the same size.
fb = await rpc("cast_guess", { guess: true, voter: me });
const crowdBefore = fb.crowd.yes + fb.crowd.no;
fb = await rpc("cast_guess", { guess: false, voter: me });
check("switching a guess replaces it", fb.crowd.yes + fb.crowd.no, crowdBefore);

// The RPC's own error text reaches the caller.
rpcState.fail = "verdict must be good, bad or unsure";
let msg = "";
try { await rpc("flag_trace", { rollout_id: 3, verdict: "bad", voter: me }); }
catch (e) { try { msg = JSON.parse(e.detail).message; } catch (x) { msg = ""; } }
rpcState.fail = null;
check("the RPC's human message survives the error path",
      msg, "verdict must be good, bad or unsure");

// Guess grading, both outcomes, pure.
const { build } = await import(`${W}/guess.js`);
const asProphet = build(FX.guesses, FX.runs, "prophet");
check("a pre-run 'no' against a rejected run is a hit",
      !!(asProphet.mine && asProphet.myGrade && asProphet.myGrade.hit), true);
const asOptimist = build(FX.guesses, FX.runs, "optimist");
check("a pre-run 'yes' against a rejected run is a miss",
      !!(asOptimist.myGrade && asOptimist.myGrade.hit === false), true);
check("the crowd hit rate counts both outcomes",
      asProphet.graded >= 2 && asProphet.hitRate > 0 && asProphet.hitRate < 1, true);
check("an open guess after every run stays ungraded",
      build(FX.guesses, FX.runs, "hopeful").myGrade, null);

// GH 403: the poller marks the API down and stops asking inside the hour.
const { external } = await import(`${W}/data.js`);
state.gh403 = true;
let got403 = false;
try {
  await external("https://api.github.com/repos/YuxuanGao-MG/rsi-arena/actions/workflows/loop.yml/runs?per_page=1");
} catch (e) { got403 = e.status === 403; }
check("GitHub 403 surfaces as a typed http error", got403, true);
state.gh403 = false;

console.log(bad ? `\n${bad} check(s) failed` : "\nall checks passed");
process.exit(bad ? 1 : 0);
