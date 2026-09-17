import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
const HERE = dirname(fileURLToPath(import.meta.url));
import { readFileSync } from "node:fs";

const FX = JSON.parse(readFileSync(join(HERE, "fixtures.json"), "utf8"));
export const state = { failNext: null, calls: [] };

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

function rows(table, params) {
  let out = [...(FX[table] || [])];
  for (const [key, raw] of params) {
    if (["select", "order", "limit", "offset"].includes(key)) continue;
    const [op, ...rest] = raw.split(".");
    const value = decodeURIComponent(rest.join("."));
    if (op === "eq") out = out.filter(r => String(r[key]) === value);
    else if (op === "in") {
      const list = value.replace(/^\(|\)$/g, "").split(",").map(s => s.replace(/^"|"$/g, ""));
      out = out.filter(r => list.includes(String(r[key])));
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
  if (url === "/archive.json")
    return json(JSON.parse(readFileSync("runs/archive.json", "utf8")));
  const u = new URL(url);
  if (u.pathname.startsWith("/rest/v1/rpc/")) {
    return json({ stored: true, already_voted: false, vote_id: 9,
                  baseline_skill: 0.043, candidate_skill: 0.041, windows: 34, quiet: 8 });
  }
  const table = u.pathname.replace("/rest/v1/rsi_", "");
  if (!FX[table]) return { ok: false, status: 404, headers: hdr("application/json"),
                           text: async () => '{"code":"PGRST205"}' };
  const all = rows(table, u.searchParams);
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
