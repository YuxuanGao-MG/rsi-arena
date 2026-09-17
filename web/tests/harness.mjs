import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
const HERE = dirname(fileURLToPath(import.meta.url));
import { readFileSync } from "node:fs";

const FX = JSON.parse(readFileSync(join(HERE, "fixtures.json"), "utf8"));
export const state = {
  failNext: null, calls: [],
  // Overridable per test: the four status states are progress-row shapes, and
  // the GitHub cross-check is a stub whose 403 path must be walkable.
  progress: null,            // array overrides FX.progress
  github: null,              // object overrides the default completed run
  gh403: false,              // simulate the rate limit
};
export const isoAgo = s => new Date(Date.now() - s * 1000).toISOString();

// The RPC stubs are stateful on purpose: "switch sides and the count stays 1"
// is only testable against a store that remembers the first cast. They share
// the arrays the table reads serve, so a cast shows up in the next select.
FX.trace_feedback = FX.trace_feedback || [];
FX.guesses = FX.guesses || [];
export const rpcState = { fail: null };  // set to a message to make every RPC 400

function el(tag = "div") {
  const node = {
    tagName: tag, children: [], _html: "", className: "", dataset: {}, style: {}, hidden: false,
    classList: { add() {}, remove() {}, contains: () => false },
    setAttribute() {}, removeAttribute() {}, getAttribute: () => "",
    addEventListener() {}, removeEventListener() {}, append(...k) { node.children.push(...k); },
    appendChild(k) { node.children.push(k); return k; },
    querySelector: () => el(), querySelectorAll: () => [],
    closest: () => null, focus() {}, getBoundingClientRect: () => ({ left: 0, top: 0, width: 900, height: 300 }),
    clientWidth: 900, remove() {},
  };
  Object.defineProperty(node, "innerHTML", { get: () => node._html, set: v => { node._html = String(v); } });
  Object.defineProperty(node, "textContent", { get: () => node._text || "", set: v => { node._text = String(v); } });
  Object.defineProperty(node, "firstChild", { get: () => node.children[0] || null });
  return node;
}

globalThis.document = {
  addEventListener() {}, createElement: tag => el(tag),
  getElementById: () => el(), querySelector: () => el(), querySelectorAll: () => [],
  documentElement: { dataset: {} }, title: "",
};
globalThis.localStorage = {
  _v: {}, getItem(k) { return this._v[k] ?? null; }, setItem(k, v) { this._v[k] = String(v); },
};
globalThis.matchMedia = () => ({ matches: false, addEventListener() {} });
globalThis.window = globalThis;
globalThis.RSI = { url: "https://example.supabase.co", key: "anon-key",
                   credit: { remaining: "12.40", total: "5626", asOf: "2026-09-16" } };

class FilterError extends Error {}

function rowsFrom(data, params) {
  let out = [...(data || [])];
  for (const [key, raw] of params) {
    if (["select", "order", "limit", "offset"].includes(key)) continue;
    const [op, ...rest] = raw.split(".");
    const value = decodeURIComponent(rest.join("."));
    if (op === "eq") out = out.filter(r => String(r[key]) === value);
    else if (op === "in") {
      const list = value.replace(/^\(|\)$/g, "").split(",").map(s => s.replace(/^"|"$/g, ""));
      out = out.filter(r => list.includes(String(r[key])));
    } else {
      // PostgREST answers 400 "failed to parse filter" for an operator it does
      // not know — including a bare value, which is what `run_id=gen5` is. This
      // fixture used to shrug and skip the filter, so a query the real backend
      // rejects passed every check here while every generation page on the live
      // site was an error. A fixture more lenient than production is a fixture
      // that certifies broken pages.
      throw new FilterError(`failed to parse filter (${raw})`);
    }
  }
  const order = params.get("order");
  if (order) {
    const [col, how = "asc"] = order.split(".");
    out.sort((a, b) => ((a[col] ?? -Infinity) - (b[col] ?? -Infinity)) * (how.startsWith("desc") ? -1 : 1));
  }
  const limit = Number(params.get("limit") || 0);
  if (limit) out = out.slice(0, limit);
  return out;
}

globalThis.fetch = async (url, opts = {}) => {
  state.calls.push(url);
  if (state.failNext) { const f = state.failNext; state.failNext = null; return f(url, opts); }
  if (String(url).startsWith("https://api.github.com/")) {
    if (state.gh403)
      return { ok: false, status: 403, headers: hdr("application/json"),
               json: async () => ({ message: "API rate limit exceeded" }),
               text: async () => '{"message":"API rate limit exceeded"}' };
    return json(state.github ?? { workflow_runs: [
      { id: 35180102504, status: "completed", conclusion: "success" }] });
  }
  if (url === "/archive.json")
    return json(JSON.parse(readFileSync("runs/archive.json", "utf8")));
  const u = new URL(url);
  if (u.pathname.startsWith("/rest/v1/rpc/")) {
    if (rpcState.fail)
      return { ok: false, status: 400, headers: hdr("application/json"),
               json: async () => ({ message: rpcState.fail }),
               text: async () => JSON.stringify({ message: rpcState.fail }) };
    const fn = u.pathname.split("/rpc/")[1];
    const args = JSON.parse(opts.body || "{}");
    if (fn === "rsi_flag_trace") {
      if (!["good", "bad", "unsure"].includes(args.verdict))
        return { ok: false, status: 400, headers: hdr("application/json"),
                 text: async () => '{"message":"verdict must be good, bad or unsure"}' };
      const mine = FX.trace_feedback.find(f =>
        f.voter === args.voter && f.rollout_id === args.rollout_id);
      if (mine) mine.verdict = args.verdict;
      else FX.trace_feedback.push({ id: FX.trace_feedback.length + 1,
        created: new Date().toISOString(), rollout_id: args.rollout_id,
        verdict: args.verdict, voter: args.voter });
      const tally = {};
      for (const f of FX.trace_feedback)
        if (f.rollout_id === args.rollout_id) tally[f.verdict] = (tally[f.verdict] || 0) + 1;
      return json({ stored: true, id: 1, tally });
    }
    if (fn === "rsi_cast_guess") {
      const mine = FX.guesses.find(g => g.voter === args.voter);
      if (mine) { mine.guess = args.guess; mine.created = new Date().toISOString(); }
      else FX.guesses.push({ id: FX.guesses.length + 1,
        created: new Date().toISOString(), guess: args.guess, voter: args.voter });
      return json({ stored: true, crowd: {
        yes: FX.guesses.filter(g => g.guess).length,
        no: FX.guesses.filter(g => !g.guess).length } });
    }
    return json({ stored: true, already_voted: false, vote_id: 9,
                  baseline_skill: 0.043, candidate_skill: 0.041, windows: 34, quiet: 8 });
  }
  const table = u.pathname.replace("/rest/v1/rsi_", "");
  const source = table === "progress" && state.progress ? { progress: state.progress } : FX;
  if (!source[table]) return { ok: false, status: 404, headers: hdr("application/json"),
                               text: async () => '{"code":"PGRST205"}' };
  let all;
  try {
    all = rowsFrom(source[table], u.searchParams);
  } catch (e) {
    if (e instanceof FilterError)
      return { ok: false, status: 400, headers: hdr("application/json"),
               text: async () => JSON.stringify({ code: "PGRST100", message: e.message }) };
    throw e;
  }
  const range = (opts.headers || {}).Range;
  if (range) {
    const [from, to] = range.split("-").map(Number);
    const page = all.slice(from, to + 1);
    return json(page, `${from}-${from + Math.max(0, page.length - 1)}/${all.length}`);
  }
  return json(all);
};

function hdr(type, range) {
  return { get: k => (k.toLowerCase() === "content-type" ? type
                    : k.toLowerCase() === "content-range" ? range || null : null) };
}
function json(body, range) {
  return { ok: true, status: 200, headers: hdr("application/json", range),
           json: async () => body, text: async () => JSON.stringify(body) };
}

export function root() {
  const node = el();
  node.querySelector = () => el();
  return node;
}
