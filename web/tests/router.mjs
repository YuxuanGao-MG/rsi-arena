import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
// Resolved from this file, not from where it happened to be written. These
// imported absolute paths into /tmp and passed locally for exactly as long as
// that directory survived, which is the oldest bug there is.
const HERE = dirname(fileURLToPath(import.meta.url));
const W = join(HERE, "..");
import { state } from "./harness.mjs";

// A document just real enough to run the router against.
const nodes = {};
function node(id) {
  if (nodes[id]) return nodes[id];
  const n = {
    id, _html: "", _text: "", dataset: {}, children: [], attrs: {},
    classList: { _s: new Set(), add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); },
                 contains(c) { return this._s.has(c); } },
    setAttribute(k, v) { n.attrs[k] = String(v); }, getAttribute: k => n.attrs[k] ?? null,
    removeAttribute(k) { delete n.attrs[k]; }, hasAttribute: k => k in n.attrs,
    addEventListener(kind, fn) { (n._on ||= {})[kind] = fn; },
    append(x) { n.children.push(x); }, appendChild(x) { n.children.push(x); return x; },
    querySelector: () => node("scratch"), querySelectorAll: () => [],
    closest: () => null, focus() { n._focused = true; },
    getBoundingClientRect: () => ({ left: 0, top: 0, width: 900, height: 200 }), clientWidth: 900,
  };
  Object.defineProperty(n, "innerHTML", { get: () => n._html, set: v => { n._html = String(v); } });
  Object.defineProperty(n, "textContent", { get: () => n._text, set: v => { n._text = String(v); } });
  Object.defineProperty(n, "firstChild", { get: () => (n._html ? {} : null) });
  Object.defineProperty(n, "tabIndex", { get: () => n._tab ?? -1, set: v => { n._tab = v; } });
  nodes[id] = n;
  return n;
}
const listeners = {};
globalThis.document = {
  addEventListener(kind, fn) { listeners[kind] = fn; },
  createElement: () => node("scratch"), getElementById: id => node(id),
  querySelector: () => node("scratch"), querySelectorAll: () => [],
  documentElement: { dataset: {} },
  get title() { return this._t; }, set title(v) { this._t = v; },
};
globalThis.location = { hash: "" };
globalThis.addEventListener = (kind, fn) => { listeners[kind] = fn; };
globalThis.setTimeout = ((real) => (fn, ms) => (ms >= 1000 ? 0 : real(fn, ms)))(setTimeout);

await import(`${W}/app.js`);

const routes = ["#/", "#/generation/gen1-floored", "#/generation/gen1-floored?side=baseline",
                "#/generation/gen5?side=baseline", "#/generation/gen4",
                "#/window/3", "#/lineage", "#/archive", "#/compare",
                "#/compare/gen1-floored", "#/compare/gen1-floored/401878780",
                "#/votes", "#/live", "#/cost", "#/nonsense"];
let bad = 0;
for (const hash of routes) {
  location.hash = hash;
  await listeners.hashchange();
  await new Promise(r => setTimeout(r, 60));
  const view = node("view");
  const title = document.title;
  const ok = view.innerHTML.length > 300 && !/Something did not load/.test(view.innerHTML);
  const announced = node("announce").textContent;
  if (!ok && hash !== "#/nonsense") { bad++; console.log(`FAIL ${hash}: ${view.innerHTML.slice(0, 200)}`); }
  else console.log(`ok   ${hash.padEnd(38)} ${String(view.innerHTML.length).padStart(6)}b  title=${JSON.stringify(title)}  busy=${view.getAttribute("aria-busy")}  announced=${JSON.stringify(announced)}`);
}

// A route change mid-flight must not let the old route paint.
let slow;
state.failNext = (url) => new Promise(resolve => { slow = resolve; });
location.hash = "#/cost";
const pending = listeners.hashchange();
location.hash = "#/votes";
await listeners.hashchange();
const afterSwitch = node("view").innerHTML;
slow && slow({ ok: true, status: 200, headers: { get: () => "application/json" },
               json: async () => [], text: async () => "[]" });
await pending.catch(() => {});
await new Promise(r => setTimeout(r, 60));
const settled = node("view").innerHTML;
console.log(settled === afterSwitch
  ? "ok   a superseded route does not paint over the current one"
  : "FAIL a superseded route painted");
if (settled !== afterSwitch) bad++;
console.log(bad ? `\n${bad} router failure(s)` : "\nrouter clean");
process.exit(bad ? 1 : 0);
