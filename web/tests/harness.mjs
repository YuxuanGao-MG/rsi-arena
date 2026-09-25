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

// The columns each public view actually has, transcribed from the migrations
// (007 is why votes has no `note`: the page promised notes are not published,
// and the view now keeps the promise). A select naming a column that is not
// here is a 400 in production — the exact class of query that once turned
// every generation page into an error while the whole suite stayed green,
// because the fixture happily served columns that do not exist.
const SCHEMA = {
  runs: ["id", "topic", "created", "parent", "incumbent", "incumbent_fp",
         "candidate_fp", "accepted", "reasons", "baseline", "candidate",
         "decision", "search", "llm", "split", "audit"],
  // 008 added topic and unit to rollouts, topic/symbol/venue/context/unit to
  // live_forecasts, and topic to progress and guesses. 010 added quote, fills
  // and path to rollouts and live_forecasts: what the paper book offered on
  // that window, what crossed it, and what the path did.
  rollouts: ["id", "run_id", "side", "split", "fixture", "ticker", "at",
             "mid_now", "realised", "predicted", "half_width", "err",
             "naive_error", "skill", "echoed", "unmeasurable", "scored",
             "cost_usd", "ok", "error_text", "output", "game", "feedback",
             "topic", "unit", "quote", "fills", "path"],
  traces: ["rollout_id", "spans"],
  votes: ["id", "created", "run_id", "fixture", "chose", "left_side",
          "baseline_skill", "candidate_skill", "voter", "server_computed"],
  live_forecasts: ["at", "league", "game_id", "ticker", "mid_now", "realised",
                   "harness", "output", "game", "spans", "skill", "scored",
                   "ok", "error_text", "topic", "symbol", "venue", "context", "unit",
                   "quote", "fills", "path"],
  progress: ["run_id", "phase", "detail", "started_at", "updated_at", "topic"],
  trace_feedback: ["id", "created", "rollout_id", "verdict", "voter"],
  guesses: ["id", "created", "guess", "voter", "topic"],
  book_snapshots: ["topic", "symbol", "at", "book"],
  // 009: the paper books. One row per book (live per topic, replay per run
  // side), the trades it made, and an equity mark per cycle.
  books: ["topic", "book_id", "harness_fp", "harness_name", "kind", "run_id",
          "side", "split", "started_at", "updated_at", "stats"],
  trades: ["id", "topic", "book_id", "harness_fp", "run_id", "instance_id",
           "side", "instrument", "opened_at", "closed_at", "entry_px", "exit_px",
           "qty", "size_usd", "fees_usd", "pnl_usd", "reason", "source"],
  book_marks: ["topic", "book_id", "at", "equity_usd", "cash_usd",
               "gross_exposure_usd", "open_positions", "drawdown", "event"],
  // 010: the search itself. One row per candidate a generation proposed, with
  // the diff against its seed, and one per (candidate, instance) score.
  candidates: ["topic", "run_id", "candidate_idx", "fingerprint", "parent_idx",
               "changed_components", "accepted", "valset_mean", "objectives",
               "components", "context_chars", "discovered_after_calls", "created"],
  candidate_scores: ["topic", "run_id", "candidate_idx", "instance_id", "score"],
};

// The three RPCs' argument names, from their SQL signatures. PostgREST
// resolves a function by name AND named-argument set, so a wrong set is the
// same 404 PGRST202 a missing function gets — the stub used to answer
// success to any name with any body, which certifies typos. rsi_cast_guess
// is overloaded since 008: (guess, voter) means Kalshi, (guess, voter, topic)
// names the loop.
const RPCS = {
  rsi_cast_vote: { required: ["run_id", "fixture", "chose", "left_side"],
                   optional: ["voter", "note"] },
  rsi_flag_trace: { required: ["rollout_id", "verdict", "voter"], optional: [] },
  rsi_cast_guess: { required: ["guess", "voter"], optional: ["topic"] },
};

function checkSelect(table, params) {
  const select = params.get("select");
  if (!select || select === "*") return null;
  const known = SCHEMA[table] || [];
  for (const col of select.split(",").map(c => c.trim()))
    if (col !== "*" && !known.includes(col)) return col;
  return null;
}

function rowsFrom(table, data, params) {
  const unknown = checkSelect(table, params);
  if (unknown !== null)
    throw new FilterError(`column ${table}.${unknown} does not exist`);
  let out = [...(data || [])];
  for (const [key, raw] of params) {
    if (["select", "order", "limit", "offset"].includes(key)) continue;
    if (SCHEMA[table] && !SCHEMA[table].includes(key))
      throw new FilterError(`column ${table}.${key} does not exist`);
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
    const sign = how.startsWith("desc") ? -1 : 1;
    out.sort((a, b) => {
      const x = a[col], y = b[col];
      if (typeof x === "number" && typeof y === "number") return (x - y) * sign;
      // Timestamps and ids sort as strings, the way PostgREST's text
      // collation does; numeric subtraction on them is NaN, and a NaN
      // comparator is a sort that silently does nothing.
      return String(x ?? "").localeCompare(String(y ?? "")) * sign;
    });
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
  // The archives, served the way web/server.py serves them: the bare file is
  // Kalshi's, `/archive/<topic>.json` is that topic's `runs/archive.<topic>.json`.
  if (url === "/archive.json" || url === "/archive/kalshi-horizon-5m.json")
    return json(JSON.parse(readFileSync(join(HERE, "..", "..", "runs", "archive.json"), "utf8")));
  const topicArchive = /^\/archive\/([a-z0-9-]+)\.json$/.exec(String(url));
  if (topicArchive) {
    const file = join(HERE, "..", "..", "runs", `archive.${topicArchive[1]}.json`);
    try { return json(JSON.parse(readFileSync(file, "utf8"))); }
    catch (e) {
      return { ok: false, status: 404, headers: hdr("text/plain"),
               json: async () => ({}), text: async () => "404 not found" };
    }
  }
  const u = new URL(url);
  if (u.pathname.startsWith("/rest/v1/rpc/")) {
    if (rpcState.fail)
      return { ok: false, status: 400, headers: hdr("application/json"),
               json: async () => ({ message: rpcState.fail }),
               text: async () => JSON.stringify({ message: rpcState.fail }) };
    const fn = u.pathname.split("/rpc/")[1];
    const args = JSON.parse(opts.body || "{}");
    const sig = RPCS[fn];
    const badArgs = sig && (sig.required.some(k => !(k in args))
      || Object.keys(args).some(k => !sig.required.includes(k) && !sig.optional.includes(k)));
    if (!sig || badArgs)
      return { ok: false, status: 404, headers: hdr("application/json"),
               json: async () => ({ code: "PGRST202",
                 message: `Could not find the function public.${fn}(${Object.keys(args).sort().join(", ")})` }),
               text: async () => JSON.stringify({ code: "PGRST202",
                 message: `Could not find the function public.${fn}(${Object.keys(args).sort().join(", ")})` }) };
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
      const topic = args.topic || "kalshi-horizon-5m";
      const mine = FX.guesses.find(g => g.voter === args.voter && (g.topic || "kalshi-horizon-5m") === topic);
      if (mine) { mine.guess = args.guess; mine.created = new Date().toISOString(); }
      else FX.guesses.push({ id: FX.guesses.length + 1, topic,
        created: new Date().toISOString(), guess: args.guess, voter: args.voter });
      const crowd = FX.guesses.filter(g => (g.topic || "kalshi-horizon-5m") === topic);
      return json({ stored: true, topic, crowd: {
        yes: crowd.filter(g => g.guess).length,
        no: crowd.filter(g => !g.guess).length } });
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
    all = rowsFrom(table, source[table], u.searchParams);
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
