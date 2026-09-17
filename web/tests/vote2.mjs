import { state } from "/tmp/rsicheck/harness.mjs";
const W = "/Users/vincent_lancaster/Desktop/OctoMesh/rsi-arena-mine/web";

const nodes = {};
function node(id) {
  if (nodes[id]) return nodes[id];
  const n = { id, _html: "", _text: "", dataset: {}, attrs: {}, children: [],
    classList: { add() {}, remove() {}, contains: () => false },
    setAttribute(k, v) { n.attrs[k] = v; }, getAttribute: k => n.attrs[k] ?? null,
    removeAttribute(k) { delete n.attrs[k]; }, hasAttribute: k => k in n.attrs,
    addEventListener() {}, append() {}, appendChild(x) { return x; },
    querySelector: () => node("scratch"),
    querySelectorAll: sel => (sel === "button" ? buttons : []),
    closest: () => null, focus() {}, clientWidth: 900,
    getBoundingClientRect: () => ({ left: 0, top: 0, width: 900, height: 200 }) };
  Object.defineProperty(n, "innerHTML", { get: () => n._html, set: v => { n._html = String(v); } });
  Object.defineProperty(n, "textContent", { get: () => n._text, set: v => { n._text = String(v); } });
  Object.defineProperty(n, "firstChild", { get: () => (n._html ? {} : null) });
  Object.defineProperty(n, "tabIndex", { get: () => n._t ?? -1, set: v => { n._t = v; } });
  return (nodes[id] = n);
}
const buttons = [{ disabled: false, dataset: { action: "vote", chose: "candidate" }, textContent: "A" },
                 { disabled: false, dataset: { action: "vote", chose: "baseline" }, textContent: "B" }];
const listeners = {};
globalThis.document = {
  addEventListener(kind, fn) { listeners[kind] = fn; },
  createElement: () => node("scratch"), getElementById: id => node(id),
  querySelector: () => node("scratch"), querySelectorAll: () => [],
  documentElement: { dataset: {} }, title: "",
};
globalThis.location = { hash: "#/compare/gen1-floored/401878780" };
globalThis.addEventListener = (kind, fn) => { listeners[kind] = fn; };

await import(`${W}/app.js`);
await listeners.hashchange();
await new Promise(r => setTimeout(r, 80));

let bad = 0;
const click = el => listeners.click({ target: { closest: () => el }, preventDefault() {} });

// 1. A successful vote reveals what the database computed, and only then.
click(buttons[0]);
await new Promise(r => setTimeout(r, 60));
const box = node("voteBox");
if (!buttons[0].disabled || !buttons[1].disabled) { bad++; console.log("FAIL buttons not disabled in flight"); }
else console.log("ok   both buttons disabled while the vote is in flight");
if (!/0\.043/.test(box.innerHTML) || !/0\.041/.test(box.innerHTML)) {
  bad++; console.log("FAIL the reveal does not show the server's numbers:", box.innerHTML.slice(0, 200));
} else console.log("ok   the reveal shows the numbers the database computed");
if (!/disagree|agree/.test(box.innerHTML)) { bad++; console.log("FAIL no verdict"); }
else console.log("ok   the reveal says whether the reader and the arithmetic agreed");

// 2. A missing function must not look like a recorded vote.
await listeners.hashchange();
await new Promise(r => setTimeout(r, 80));
buttons.forEach(b => { b.disabled = false; });
state.failNext = async () => ({ ok: false, status: 404, headers: { get: () => "application/json" },
                                text: async () => '{"code":"PGRST202"}' });
click(buttons[1]);
await new Promise(r => setTimeout(r, 60));
const after = node("voteBox").innerHTML;
if (!/not recorded/i.test(after) || !/002_votes_rpc\.sql/.test(after)) {
  bad++; console.log("FAIL a rejected vote does not say so:", after.slice(0, 300));
} else console.log("ok   a rejected vote says it was not recorded and names the migration");

console.log(bad ? `\n${bad} vote failure(s)` : "\nvote path clean");
process.exit(bad ? 1 : 0);
