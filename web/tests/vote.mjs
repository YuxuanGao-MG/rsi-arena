// The vote flow, driven through the same click path a reader uses: the
// delegated document listener, a real button element, and the reveal that
// paints from what the RPC returned. The previous version of this file ended
// on `typeof voteFn !== "undefined"` — a tautology on a declared variable —
// which is to say it ended by checking that JavaScript still had variables.
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
const HERE = dirname(fileURLToPath(import.meta.url));
const W = join(HERE, "..");

// Capture the delegated click listener before actions.js installs it.
import "./harness.mjs";
import { rpcState } from "./harness.mjs";
let clickHandler = null;
const origAdd = globalThis.document.addEventListener;
globalThis.document.addEventListener = (kind, fn) => {
  if (kind === "click") clickHandler = fn;
  origAdd.call(globalThis.document, kind, fn);
};

const { toHTML } = await import(`${W}/dom.js`);
const { invalidate } = await import(`${W}/data.js`);
const { compareView } = await import(`${W}/views/compare.js`);

let bad = 0;
const check = (name, got, want) => {
  if (got !== want) { bad++; console.log(`FAIL ${name}: got ${got}, want ${want}`); }
  else console.log(`ok   ${name}`);
};
const click = el => clickHandler({ target: { closest: () => el }, preventDefault() {} });
const sleep = ms => new Promise(r => setTimeout(r, ms));

// A vote box just real enough to be painted into.
function makeBox() {
  const buttons = [
    { disabled: false, dataset: { action: "vote", chose: "candidate" }, textContent: "A" },
    { disabled: false, dataset: { action: "vote", chose: "baseline" }, textContent: "B" },
    { disabled: false, dataset: { action: "vote", chose: "neither" }, textContent: "neither" },
  ];
  const box = { _html: "", children: [], buttons,
                querySelectorAll: sel => (sel === "button" ? buttons : []),
                querySelector: () => null,
                setAttribute() {}, classList: { add() {}, remove() {} } };
  Object.defineProperty(box, "innerHTML",
    { get: () => box._html, set: v => { box._html = String(v); } });
  return box;
}

async function renderDuel() {
  invalidate();
  const out = await compareView({ params: { runId: "gen1-floored", fixture: "401878780" },
                                  query: {}, signal: new AbortController().signal });
  const box = makeBox();
  globalThis.document.getElementById = id => (id === "voteBox" ? box : {
    innerHTML: "", setAttribute() {}, textContent: "",
  });
  out.ready({ querySelector: () => box, addEventListener() {} });
  return { out, box };
}

// 1. The answer is nowhere in the page before the vote.
const first = await renderDuel();
const body = toHTML(first.out.body);
check("no pooled skill in the page before the vote", /0\.04[0-9]/.test(body), false);
check("the click listener is the real one", typeof clickHandler, "function");

// 2. A successful vote, end to end: disabled in flight, reveal from the RPC.
click(first.box.buttons[0]);
check("buttons disabled while in flight",
      first.box.buttons.every(b => b.disabled), true);
await sleep(30);
check("the reveal shows the RPC's incumbent skill", /0\.043/.test(first.box.innerHTML), true);
check("the reveal shows the RPC's candidate skill", /0\.041/.test(first.box.innerHTML), true);
check("the reveal says agree or disagree", /agree/.test(first.box.innerHTML), true);

// 3. A refusal, in the RPC's own words — the sentence a gen5 vote now gets.
const second = await renderDuel();
rpcState.fail = "no paired scored windows for 401878780 on gen5";
click(second.box.buttons[1]);
await sleep(30);
rpcState.fail = null;
check("a refused vote is not dressed as recorded",
      /not recorded/i.test(second.box.innerHTML), true);
check("the RPC's human sentence is shown verbatim",
      /no paired scored windows for 401878780 on gen5/.test(second.box.innerHTML), true);

console.log(bad ? `\n${bad} vote check(s) failed` : "\nvote flow clean, end to end");
process.exit(bad ? 1 : 0);
