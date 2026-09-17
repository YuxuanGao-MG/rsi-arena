/* The router, the chrome, and the three things a route change has to get right:
 * cancel what the last route was fetching, move focus to the new heading, and
 * say out loud that the page changed.
 *
 * Navigation is real `<a href="#/...">` and actions are real `<button>`. The
 * page this replaces had neither — every control was a `<div onclick>`, which
 * meant no keyboard, no focus ring, no middle-click, and no way for a screen
 * reader to know any of it was interactive.
 */

import { NAV, METRICS_NAV, navOf, parse } from "./routes.js";
import { mount, html, raw, errorPanel, skeleton } from "./dom.js";
import { clearCharts } from "./charts.js";
import { invalidate } from "./data.js";
import { resetActions } from "./actions.js";
import { startStatus, onStatus, setOverviewVisible } from "./status.js";
import { startTicker } from "./ticker.js";
import { startClock, tickAll } from "./clock.js";

import { overviewView } from "./views/overview.js";
import { metricsView } from "./views/metrics.js";
import { aboutView } from "./views/about.js";
import { runView } from "./views/run.js";
import { windowView } from "./views/window.js";
import { lineageView } from "./views/lineage.js";
import { compareView } from "./views/compare.js";
import { votesView } from "./views/votes.js";
import { liveView } from "./views/live.js";
import { costView } from "./views/cost.js";
import { archiveView } from "./views/archive.js";

const VIEWS = {
  overview: overviewView, metrics: metricsView, about: aboutView,
  run: runView, window: windowView, lineage: lineageView,
  compare: compareView, votes: votesView, live: liveView, cost: costView,
  archive: archiveView,
};

const viewEl = document.getElementById("view");
const announceEl = document.getElementById("announce");
const navEl = document.getElementById("nav");
const navStatusEl = document.getElementById("nav-status");

let inflight = null;      // the running route's AbortController
let lastName = null;
let firstPaint = true;
let slowTimer = 0;        // says so out loud when a fetch is taking its time

/* ---------- chrome -------------------------------------------------------- */

function paintNav(active) {
  mount(navEl, NAV.map(([label, to, name]) => html`
    <li><a href="${raw(to)}" ${raw(name === active ? 'aria-current="page"' : "")}>${label}</a></li>`));
}

/* The MiMo signature: the run's state lives in the chrome, always visible,
 * and its connection states are part of the honesty — a failed poll says
 * "reconnecting", never a dot that quietly stopped meaning anything. */
function paintNavStatus(s) {
  if (!navStatusEl) return;
  const collecting = s.collection && s.collection.running;
  const dot = s.kind === "idle" && collecting ? "live"
    : { live: "live", "running-blind": "blind", stale: "stale",
        idle: "idle", loading: "idle" }[s.kind] || "idle";
  const text =
    s.reconnecting ? "reconnecting…"
    : s.kind === "live" ? `${s.run} · ${s.phase}` +
        (s.spentUsd != null
          ? ` · $${s.spentUsd}${s.budgetUsd ? ` of $${s.budgetUsd}` : ""}` : "")
    : s.kind === "running-blind" ? "running · no heartbeat"
    : s.kind === "stale" ? `stale · ${s.run}`
    : s.kind === "loading" ? "…"
    : collecting ? "live collection running"
    : "idle";
  mount(navStatusEl, html`
    <span class="status-dot ${raw(dot)} ${raw(s.reconnecting ? "reconnecting" : "")}"
          aria-hidden="true"></span>
    <span class="status-text">${text}</span>`);
  navStatusEl.setAttribute("aria-label", `Run status: ${text}`);
}

function announce(text) {
  // Cleared first: repeating the same string is not an update, and a reader
  // that lands twice on Generations should still hear it the second time.
  announceEl.textContent = "";
  setTimeout(() => { announceEl.textContent = text; }, 40);
}

function paintTheme() {
  const set = document.documentElement.dataset.theme;
  const dark = set ? set === "dark"
                   : matchMedia("(prefers-color-scheme: dark)").matches;
  const btn = document.getElementById("theme");
  btn.setAttribute("aria-pressed", String(dark));
  btn.setAttribute("aria-label", `Theme: ${set || "matching the system"}. Switch to ${dark ? "light" : "dark"}.`);
  document.getElementById("theme-label").textContent = dark ? "Dark" : "Light";
  for (const meta of document.querySelectorAll('meta[name="theme-color"]')) {
    if (!set) continue;                       // leave the media pair alone
    meta.removeAttribute("media");
    meta.setAttribute("content", dark ? "#0d1114" : "#f6f7f5");
  }
}

document.getElementById("theme").addEventListener("click", () => {
  const root = document.documentElement;
  const dark = root.dataset.theme
    ? root.dataset.theme === "dark"
    : matchMedia("(prefers-color-scheme: dark)").matches;
  root.dataset.theme = dark ? "light" : "dark";
  try { localStorage.setItem("rsi_theme", root.dataset.theme); } catch (e) { /* private mode */ }
  paintTheme();
});

/* ---------- the route ------------------------------------------------------ */

function paint({ title, heading, lead, crumbs, body, ready, routeName }) {
  const inMetrics = routeName && navOf(routeName) === "metrics";
  const subnav = inMetrics ? html`<nav class="subnav" aria-label="Metrics sections"><ul>
      ${METRICS_NAV.map(([label, to, name]) => html`<li>
        <a href="${raw(to)}" ${raw(name === routeName ? 'aria-current="page"' : "")}>${label}</a>
      </li>`)}</ul></nav>` : "";
  const head = html`
    ${subnav}
    ${crumbs && crumbs.length ? html`<nav class="crumbs" aria-label="Breadcrumb"><ol>
      ${crumbs.map(([label, to]) => html`<li>${to ? html`<a href="${raw(to)}">${label}</a>` : label}</li>`)}
    </ol></nav>` : ""}
    <h1 id="page-title" tabindex="-1">${heading}</h1>
    ${lead ? html`<p class="sub">${lead}</p>` : ""}`;

  clearTimeout(slowTimer);
  mount(viewEl, html`<div class="fade">${head}${body}</div>`);
  viewEl.classList.remove("stale");
  viewEl.removeAttribute("aria-busy");
  enhance(viewEl);
  document.title = `${title} · rsi-arena`;
  if (ready) ready(viewEl);
  try { tickAll(viewEl); } catch (e) { /* stub DOM */ }

  const h1 = document.getElementById("page-title");
  if (!firstPaint && h1) h1.focus({ preventScroll: true });
  announce(`${title} loaded`);
  firstPaint = false;
}

/**
 * The two things a keyboard cannot reach unless something says so.
 *
 * A region that scrolls has to be focusable or there is no way to scroll it
 * without a mouse — that is every wide table on this site, and every trace step
 * whose output is taller than its box. Done here rather than in each view
 * because there are thirty of them and one of them would always be forgotten.
 */
function enhance(root) {
  for (const box of root.querySelectorAll(".scroll")) {
    if (box.hasAttribute("tabindex")) continue;
    const caption = box.querySelector("caption");
    const heading = box.closest("section")?.querySelector("h2");
    box.tabIndex = 0;
    box.setAttribute("role", "region");
    box.setAttribute("aria-label",
      (caption?.textContent || heading?.textContent || "Table").trim().slice(0, 120));
  }
  for (const pre of root.querySelectorAll("details.span pre")) {
    if (pre.hasAttribute("tabindex")) continue;
    pre.tabIndex = 0;
    pre.setAttribute("role", "region");
    pre.setAttribute("aria-label", "Step detail");
  }
}

async function route({ fresh = false } = {}) {
  const r = parse(location.hash);
  if (inflight) inflight.abort();
  inflight = new AbortController();
  const { signal } = inflight;

  // Whatever the old view subscribed to — the overview's status feed — is let
  // go before the new one paints, and the GitHub poll budget goes back to
  // sleep unless the next route is the overview.
  try { viewEl.dispatchEvent(new Event("view-teardown")); } catch (e) { /* stub DOM */ }
  setOverviewVisible(r.name === "overview");

  clearCharts();
  resetActions({ retry: () => route({ fresh: true }) });
  paintNav(navOf(r.name));

  if (fresh) invalidate();
  viewEl.setAttribute("aria-busy", "true");
  if (r.name === lastName && viewEl.firstChild) viewEl.classList.add("stale");
  else mount(viewEl, skeleton(4));
  lastName = r.name;

  // A skeleton that never resolves is the worst of the failure states, because
  // it looks like progress. After five seconds this says what is actually
  // happening; the fetch itself gives up at fifteen and shows a retry.
  clearTimeout(slowTimer);
  slowTimer = setTimeout(() => {
    if (signal.aborted) return;
    const note = document.createElement("p");
    note.className = "msg";
    note.textContent = "Still waiting on the database.";
    viewEl.append(note);
  }, 5000);

  const view = VIEWS[r.name];
  if (!view) {
    return paint({
      title: "Not found", heading: "No such page",
      body: html`<div class="panel"><div class="panel-b prose">
        <p>The address <code>${location.hash}</code> does not match a page here.</p>
        <p><a href="#/">Start at the overview</a>.</p></div></div>`,
    });
  }

  try {
    const out = await view({ ...r, signal, reload: () => route({ fresh: true }) });
    if (signal.aborted) return;                 // a newer route already owns the page
    paint({ ...out, routeName: r.name });
  } catch (err) {
    if (signal.aborted || err.kind === "aborted") return;
    // An ApiError has already been classified and its detail already logged.
    // Anything else is a bug in a view, and the console is where it belongs.
    if (!err.kind) console.error(err);
    const shown = err.kind ? err
      : { kind: "unknown", message: "Something in this page went wrong.", retryable: true };
    viewEl.classList.remove("stale");
    paint({ title: "Error", heading: "Something did not load",
            body: errorPanel(shown, "retry") });
  }
}

addEventListener("hashchange", () => route());
paintTheme();
onStatus(paintNavStatus);
startStatus();
startTicker(document.getElementById("foot-stream"));
startClock();
route();
