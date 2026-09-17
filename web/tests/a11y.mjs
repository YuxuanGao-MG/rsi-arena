import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
// Resolved from this file, not from where it happened to be written. These
// imported absolute paths into /tmp and passed locally for exactly as long as
// that directory survived, which is the oldest bug there is.
const HERE = dirname(fileURLToPath(import.meta.url));
const W = join(HERE, "..");
import { state, root } from "./harness.mjs";
import { readFileSync } from "node:fs";
const { toHTML } = await import(`${W}/dom.js`);
const { invalidate } = await import(`${W}/data.js`);

const shell = readFileSync(`${W}/index.html`, "utf8");
const views = {
  runs: [(await import(`${W}/views/runs.js`)).runsView, { params: {}, query: {} }],
  run: [(await import(`${W}/views/run.js`)).runView, { params: { id: "gen1-floored" }, query: {} }],
  window: [(await import(`${W}/views/window.js`)).windowView, { params: { id: "3" }, query: {} }],
  lineage: [(await import(`${W}/views/lineage.js`)).lineageView, { params: {}, query: {} }],
  compare: [(await import(`${W}/views/compare.js`)).compareView,
            { params: { runId: "gen1-floored", fixture: "401878780" }, query: {} }],
  votes: [(await import(`${W}/views/votes.js`)).votesView, { params: {}, query: {} }],
  live: [(await import(`${W}/views/live.js`)).liveView, { params: {}, query: {} }],
  cost: [(await import(`${W}/views/cost.js`)).costView, { params: {}, query: {} }],
  archive: [(await import(`${W}/views/archive.js`)).archiveView, { params: {}, query: {} }],
};

let bad = 0;
const fail = (view, msg) => { bad++; console.log(`  FAIL ${view}: ${msg}`); };

// The shell.
if (!/lang="en"/.test(shell)) fail("shell", "no lang");
if (!/<noscript>/.test(shell)) fail("shell", "no noscript");
if (!/name="theme-color"/.test(shell)) fail("shell", "no theme-color");
if (!/color-scheme/.test(shell)) fail("shell", "no color-scheme");
if (!/class="skip"/.test(shell)) fail("shell", "no skip link");
if (!/aria-live/.test(shell)) fail("shell", "no live region");

for (const [name, [fn, ctx]] of Object.entries(views)) {
  invalidate();
  const out = await fn({ ...ctx, signal: new AbortController().signal });
  const body = toHTML(out.body) + toHTML(out.heading) + (out.lead ? toHTML(out.lead) : "");
  // The whole class of bug the rewrite was for.
  if (/onclick|onmouseover|javascript:/i.test(body)) fail(name, "inline handler or js: url");
  // Headings: the view supplies the h1, so anything it writes must be h2+.
  const levels = [...body.matchAll(/<h([1-6])\b/g)].map(m => Number(m[1]));
  if (levels.includes(1)) fail(name, "a second h1");
  // Tables.
  for (const table of body.match(/<table[\s\S]*?<\/table>/g) || []) {
    const heads = table.match(/<th\b[^>]*>/g) || [];
    if (heads.some(h => !/scope=/.test(h))) fail(name, "a th without scope");
    if (!/<caption/.test(table)) fail(name, "a table without a caption");
  }
  // Links and buttons must say something.
  for (const a of body.match(/<a\b[^>]*>[\s\S]*?<\/a>/g) || []) {
    const text = a.replace(/<[^>]+>/g, "").trim();
    if (!text && !/aria-label=/.test(a)) fail(name, `an empty link: ${a.slice(0, 60)}`);
    if (!/href=/.test(a)) fail(name, `a link with no href: ${a.slice(0, 60)}`);
  }
  for (const b of body.match(/<button\b[^>]*>[\s\S]*?<\/button>/g) || []) {
    const text = b.replace(/<[^>]+>/g, "").trim();
    if (!text && !/aria-label=/.test(b)) fail(name, "a button with no name");
    if (!/type="button"/.test(b)) fail(name, "a button without type");
  }
  // Graphics.
  for (const g of body.match(/role="img"[^>]*>/g) || [])
    if (!/aria-label=/.test(g)) fail(name, "role=img without a label");
  if (/tabindex="[1-9]/.test(body)) fail(name, "a positive tabindex");
  // Duplicate ids.
  const ids = [...body.matchAll(/\bid="([^"]+)"/g)].map(m => m[1]);
  const dupes = ids.filter((v, i) => ids.indexOf(v) !== i);
  if (dupes.length) fail(name, `duplicate id ${dupes[0]}`);
  const inline = (body.match(/style="/g) || []).length;
  console.log(`  ${name.padEnd(8)} ${String(body.length).padStart(6)}b  h-levels ${[...new Set(levels)].join(",") || "none"}  inline-styles ${inline}`);
}
console.log(bad ? `\n${bad} accessibility problem(s)` : "\nno accessibility problems found by these rules");
process.exit(bad ? 1 : 0);
