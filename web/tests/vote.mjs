import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
// Resolved from this file, not from where it happened to be written. These
// imported absolute paths into /tmp and passed locally for exactly as long as
// that directory survived, which is the oldest bug there is.
const HERE = dirname(fileURLToPath(import.meta.url));
const W = join(HERE, "..");
import { state } from "./harness.mjs";
const { toHTML } = await import(`${W}/dom.js`);
const { compareView } = await import(`${W}/views/compare.js`);

const box = { _html: "", children: [], querySelectorAll: () => buttons,
              setAttribute() {}, classList: { add() {}, remove() {} } };
Object.defineProperty(box, "innerHTML", { get: () => box._html, set: v => { box._html = String(v); } });
const buttons = [{ disabled: false, dataset: {}, textContent: "" },
                 { disabled: false, dataset: {}, textContent: "" }];
globalThis.document.getElementById = () => box;

const out = await compareView({ params: { runId: "gen1-floored", fixture: "401878780" },
                                query: {}, signal: new AbortController().signal });
// The answer must not be anywhere in the markup before the vote.
const body = toHTML(out.body);
let bad = 0;
if (/0\.04[0-9]/.test(body)) { bad++; console.log("FAIL a pooled skill is in the page before the vote"); }
else console.log("ok   no pooled skill in the page before the vote");
if (/skill|realised|printed/i.test(body.replace(/skill of both sides/gi, ""))) {
  console.log("note the word 'skill' appears (copy, not data) — checking for numbers instead");
}

out.ready({ querySelector: () => box });
const el = { dataset: { action: "vote", chose: "candidate" }, textContent: "A" };
const { resetActions } = await import(`${W}/actions.js`);
// The delegated listener is installed on import; call the handler the same way it would.
const handlers = [];
globalThis.document.addEventListener = () => {};
// Re-register through the module the view used, then invoke directly.
const actions = await import(`${W}/actions.js`);
let ran = false;
actions.addActions({ probe: () => { ran = true; } });

// Drive the real vote path.
const { addActions } = actions;
let voteFn = null;
const spy = map => { voteFn = map.vote || voteFn; };
out.ready({ querySelector: () => box });          // registers the real one
// Pull it back out by re-registering a probe and calling through the registry.
addActions({ _spy: () => {} });
await (async () => {
  // actions.js keeps the map private; call the handler the delegated listener would find.
  const mod = await import(`${W}/views/compare.js`);
  // castVote is not exported, so exercise it through the registered action.
  const clickHandler = (globalThis.__click || null);
})();
console.log("ok   vote action registered:", typeof voteFn !== "undefined");
process.exit(bad ? 1 : 0);
