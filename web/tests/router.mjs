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

const { parse, href, withTopic } = await import(`${W}/routes.js`);

// Every route the site has ever had, then the same pages under a topic prefix.
// The prefixless ones must keep meaning Kalshi — they are bookmarks — and the
// prefixed ones must resolve to the same views with the other topic's data.
const routes = ["#/", "#/metrics", "#/about",
                "#/generation/gen1-floored", "#/generation/gen1-floored?side=baseline",
                "#/generation/gen5?side=baseline", "#/generation/gen4",
                "#/window/3", "#/lineage", "#/archive", "#/compare",
                "#/compare/gen1-floored", "#/compare/gen1-floored/401878780",
                "#/votes", "#/live", "#/trading", "#/cost", "#/nonsense",
                "#/t/crypto-horizon-1m/", "#/t/crypto-horizon-1m/metrics",
                "#/t/crypto-horizon-1m/trading",
                "#/t/crypto-horizon-1m/trading?book=cgen1:candidate:holdout",
                "#/t/crypto-horizon-1m/about", "#/t/crypto-horizon-1m/generation/cgen1",
                "#/t/crypto-horizon-1m/compare/cgen1/D20260919",
                "#/t/crypto-horizon-1m/live", "#/t/crypto-horizon-1m/archive",
                "#/t/crypto-horizon-1m/lineage", "#/t/crypto-horizon-1m/votes",
                "#/t/crypto-horizon-1m/cost",
                "#/t/news-equity-5m/", "#/t/news-equity-5m/live", "#/t/news-equity-5m/archive",
                "#/t/kalshi-horizon-5m/metrics", "#/t/typo/", "#/t/typo/metrics"];
const missing = new Set(["#/nonsense", "#/t/typo/", "#/t/typo/metrics"]);
let bad = 0;
for (const hash of routes) {
  location.hash = hash;
  await listeners.hashchange();
  await new Promise(r => setTimeout(r, 60));
  const view = node("view");
  const title = document.title;
  const ok = view.innerHTML.length > 300 && !/Something did not load/.test(view.innerHTML);
  const announced = node("announce").textContent;
  if (!ok && !missing.has(hash)) { bad++; console.log(`FAIL ${hash}: ${view.innerHTML.slice(0, 200)}`); }
  else if (missing.has(hash) && !/No such page/.test(view.innerHTML)) {
    bad++; console.log(`FAIL ${hash}: should be a missing page, got ${view.innerHTML.slice(0, 120)}`);
  }
  else console.log(`ok   ${hash.padEnd(46)} ${String(view.innerHTML.length).padStart(6)}b  title=${JSON.stringify(title)}  busy=${view.getAttribute("aria-busy")}  announced=${JSON.stringify(announced)}`);
}

// The topic prefix: parsed, omitted for the default, carried by every href.
const say = (name, got, want) => {
  if (got !== want) { bad++; console.log(`FAIL ${name}: got ${JSON.stringify(got)}, want ${JSON.stringify(want)}`); }
  else console.log(`ok   ${name}`);
};
localStorage.setItem("rsi_topic", "kalshi-horizon-5m");
say("a bare route is Kalshi", parse("#/metrics").topic, "kalshi-horizon-5m");
say("a prefixed route names its topic", parse("#/t/crypto-horizon-1m/metrics").topic, "crypto-horizon-1m");
say("the prefix leaves the page alone", parse("#/t/crypto-horizon-1m/generation/x").name, "run");
say("the prefix leaves the params alone", parse("#/t/crypto-horizon-1m/generation/x").params.id, "x");
say("an unknown topic is a missing page", parse("#/t/typo/metrics").name, "missing");
say("withTopic omits the default", withTopic("#/metrics", "kalshi-horizon-5m"), "#/metrics");
say("withTopic prefixes the rest", withTopic("#/metrics", "crypto-horizon-1m"), "#/t/crypto-horizon-1m/metrics");
say("withTopic on the root", withTopic("#/", "news-equity-5m"), "#/t/news-equity-5m/");
location.hash = "#/t/crypto-horizon-1m/";
say("href.run carries the page's topic", href.run("cgen1"), "#/t/crypto-horizon-1m/generation/cgen1");
say("href.compare carries the page's topic", href.compare("cgen1", "D1"), "#/t/crypto-horizon-1m/compare/cgen1/D1");
say("href.trading carries the page's topic", href.trading(), "#/t/crypto-horizon-1m/trading");
say("href.trading names a book in the query",
    href.trading("cgen1:candidate:holdout"), "#/t/crypto-horizon-1m/trading?book=cgen1%3Acandidate%3Aholdout");
say("the book survives the parse",
    parse("#/t/crypto-horizon-1m/trading?book=cgen1%3Acandidate%3Aholdout").query.book, "cgen1:candidate:holdout");
location.hash = "#/";
say("href.run on Kalshi is the old link", href.run("gen5"), "#/generation/gen5");
localStorage.setItem("rsi_topic", "crypto-horizon-1m");
say("a bare route follows the remembered topic", parse("#/").topic, "crypto-horizon-1m");
say("the switcher's Kalshi link is explicit, so it overrides the memory",
    href.topic("kalshi-horizon-5m"), "#/t/kalshi-horizon-5m/");
localStorage.setItem("rsi_topic", "kalshi-horizon-5m");

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
