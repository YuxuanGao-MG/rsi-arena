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
  overview: [(await import(`${W}/views/overview.js`)).overviewView, { params: {}, query: {} }],
  metrics: [(await import(`${W}/views/metrics.js`)).metricsView, { params: {}, query: {} }],
  about: [(await import(`${W}/views/about.js`)).aboutView, { params: {}, query: {} }],
  run: [(await import(`${W}/views/run.js`)).runView, { params: { id: "gen1-floored" }, query: {} }],
  runExhausted: [(await import(`${W}/views/run.js`)).runView,
                 { params: { id: "gen5" }, query: { side: "baseline" } }],
  window: [(await import(`${W}/views/window.js`)).windowView, { params: { id: "3" }, query: {} }],
  lineage: [(await import(`${W}/views/lineage.js`)).lineageView, { params: {}, query: {} }],
  compare: [(await import(`${W}/views/compare.js`)).compareView,
            { params: { runId: "gen1-floored", fixture: "401878780" }, query: {} }],
  votes: [(await import(`${W}/views/votes.js`)).votesView, { params: {}, query: {} }],
  live: [(await import(`${W}/views/live.js`)).liveView, { params: {}, query: {} }],
  cost: [(await import(`${W}/views/cost.js`)).costView, { params: {}, query: {} }],
  archive: [(await import(`${W}/views/archive.js`)).archiveView, { params: {}, query: {} }],
  // The same pages on another topic, with its own words and unit.
  overviewCrypto: [(await import(`${W}/views/overview.js`)).overviewView,
                   { params: {}, query: {}, topic: "crypto-horizon-5m" }],
  aboutCrypto: [(await import(`${W}/views/about.js`)).aboutView,
                { params: {}, query: {}, topic: "crypto-horizon-5m" }],
  runCrypto: [(await import(`${W}/views/run.js`)).runView,
              { params: { id: "cgen1" }, query: {}, topic: "crypto-horizon-5m" }],
  liveCrypto: [(await import(`${W}/views/live.js`)).liveView,
               { params: {}, query: {}, topic: "crypto-horizon-5m" }],
  compareCrypto: [(await import(`${W}/views/compare.js`)).compareView,
                  { params: { runId: "cgen1", fixture: "D20260919" }, query: {},
                    topic: "crypto-horizon-5m" }],
  archiveNews: [(await import(`${W}/views/archive.js`)).archiveView,
                { params: {}, query: {}, topic: "news-equity-5m" }],
};

let bad = 0;
const fail = (view, msg) => { bad++; console.log(`  FAIL ${view}: ${msg}`); };

// The shell — parsed, not just grepped. A botched favicon edit once left an
// unencoded copy of the SVG after the data URI closed; the parser terminated
// <head> early, two <circle> elements landed in the body, and a stray '">'
// rendered as visible text on every route. Regex greps for the presence of
// tags saw nothing wrong; only checking what is BETWEEN the tags does.
{
  const headMatch = shell.match(/<head>([\s\S]*?)<\/head>/);
  if (!headMatch) fail("shell", "no parseable <head>");
  else {
    const head = headMatch[1];
    if (!/<link rel="stylesheet" href="app.css">/.test(head))
      fail("shell", "stylesheet link not inside <head> — head terminated early");
    // Strip element contents that legitimately hold text, then every tag;
    // anything left is stray text a broken tag spilled into the document.
    const text = head
      .replace(/<script[\s\S]*?<\/script>/g, "")
      .replace(/<title[\s\S]*?<\/title>/g, "")
      .replace(/<!--[\s\S]*?-->/g, "")
      .replace(/<[^<>]*>/g, "")
      .trim();
    if (text) fail("shell", `stray text in <head>: ${JSON.stringify(text.slice(0, 60))}`);
    // No attribute value may contain a raw angle bracket: a tag that fails
    // this check is two tags to the parser.
    for (const tag of head.match(/<(link|meta)\b[^>]*>/g) || [])
      if (/[<]/.test(tag.slice(1))) fail("shell", `malformed tag: ${tag.slice(0, 60)}`);
  }
  const bodyMatch = shell.match(/<body>([\s\S]*?)Skip to content/);
  if (!bodyMatch) fail("shell", "no skip link at the top of <body>");
  else {
    const beforeSkip = bodyMatch[1]
      .replace(/<!--[\s\S]*?-->/g, "")
      .replace(/<[^<>]*>/g, "")
      .trim();
    if (beforeSkip) fail("shell",
      `visible text before the skip link: ${JSON.stringify(beforeSkip.slice(0, 60))}`);
  }
}
if (!/lang="en"/.test(shell)) fail("shell", "no lang");
if (!/<noscript>/.test(shell)) fail("shell", "no noscript");

// The topic switcher: a labelled nav in the shell, filled with real links by
// routes.js — one marked current, every one named, none a button pretending.
if (!/<nav class="topics" aria-label="Topic"><ul id="topics">/.test(shell))
  fail("shell", "no labelled topic nav");
{
  const { topicNav } = await import(`${W}/routes.js`);
  const { TOPIC_IDS } = await import(`${W}/topics.js`);
  const nav = toHTML(topicNav("crypto-horizon-5m"));
  const links = nav.match(/<a\b[^>]*>[\s\S]*?<\/a>/g) || [];
  if (links.length !== TOPIC_IDS.length) fail("switcher", `${links.length} links for ${TOPIC_IDS.length} topics`);
  for (const a of links) {
    if (!/href="#\/t\/[a-z0-9-]+\/"/.test(a)) fail("switcher", `a link that is not a topic route: ${a.slice(0, 60)}`);
    if (!a.replace(/<[^>]+>/g, "").trim()) fail("switcher", "an unnamed topic link");
  }
  const current = links.filter(a => /aria-current="page"/.test(a));
  if (current.length !== 1) fail("switcher", `${current.length} links marked current`);
  if (!/crypto-horizon-5m/.test(current[0] || "")) fail("switcher", "the wrong link is current");
  if (/onclick|<button/.test(nav)) fail("switcher", "a control that is not a link");
  console.log(`  switcher ${String(nav.length).padStart(6)}b  ${links.length} links, 1 current`);
}
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
