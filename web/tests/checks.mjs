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

console.log(bad ? `\n${bad} check(s) failed` : "\nall checks passed");
process.exit(bad ? 1 : 0);
