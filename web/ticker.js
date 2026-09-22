/* The foot-stream: one line of what the arena last did, composed client-side.
 *
 * MiMo's footer carries a live event ticker; ours is assembled from the tables
 * the site already reads — verdicts, live forecasts, votes — because a second
 * event log on the backend would be a copy of data that already exists,
 * guaranteed to drift from it.
 *
 * Not a marquee. `prefers-reduced-motion` is respected by never animating in
 * the first place: it is a list, newest first, that updates in place. When
 * every fetch fails there is a static fallback rather than an empty footer,
 * labelled as such — a blank strip reads as a broken site, and an unlabelled
 * cache reads as live data, and both are worse than saying what happened.
 */

import { q, qs, topicFilter } from "./data.js";
import { html, raw, toHTML } from "./dom.js";
import { href } from "./routes.js";
import { relative } from "./status.js";
import { currentTopic, forecastOf, fmtMove, fmtPrice } from "./topics.js";

const REFRESH_MS = 90_000;
const MAX_ITEMS = 8;

let el = null;
let timer = 0;
let lastGood = null;
let topic = null;                        // whose events the strip carries

export function startTicker(target) {
  if (!target || el) return;
  el = target;
  if (typeof document !== "undefined") {
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) refresh();       // catch up the moment eyes return
    });
  }
  refresh();
}

/** The strip follows the page's topic; a switch refreshes it at once. */
export function setTickerTopic(id) {
  if (id === topic) return;
  topic = id;
  lastGood = null;
  if (el) refresh();
}

async function gather() {
  const mine = topic || currentTopic();
  // Votes hang off runs, so the run ids of this topic are the filter — one
  // narrow select rather than a `topic` column votes do not have.
  const ids = await q(`runs?select=id${topicFilter(mine)}`, {}).catch(() => []);
  // Three independent selects; one failing should not empty the others.
  const [runs, live, votes] = await Promise.all([
    q(`runs?select=id,created,accepted,reasons,llm,decision${topicFilter(mine)}` +
      "&order=created.desc&limit=3", {})
      .catch(() => []),
    q(`live_forecasts?select=at,ticker,symbol,mid_now,skill,scored,output${topicFilter(mine)}` +
      "&order=at.desc&limit=4", {})
      .catch(() => []),
    ids.length
      ? q(`votes?select=created,run_id,fixture,chose&run_id=${qs.inList(ids.map(r => r.id))}` +
          "&order=created.desc&limit=3", {}).catch(() => [])
      : [],
  ]);

  const events = [];
  for (const r of runs) {
    const llm = r.llm || {};
    const word = llm.incomplete || r.decision?.incomplete ? "crashed"
      : llm.exhausted ? "ran out of money"
      : r.accepted ? "was promoted" : "was dropped";
    events.push({ at: r.created, to: href.run(r.id),
                  text: `${r.id} ${word}` });
  }
  for (const f of live) {
    const { delta } = forecastOf(f.output, mine);
    const said = delta != null ? `said ${fmtMove(delta, mine)}` : "made no forecast";
    const scored = f.scored && f.skill != null ? ` · skill ${f.skill >= 0 ? "+" : ""}${f.skill}` : "";
    // A Kalshi ticker ends in the team; the other topics name the symbol.
    const who = f.symbol || String(f.ticker || "").split("-").pop();
    events.push({ at: f.at, to: href.live(),
                  text: `${who} quoted ${fmtPrice(f.mid_now, mine)}, ${said}${scored}` });
  }
  for (const v of votes) {
    const chose = v.chose === "baseline" ? "the incumbent"
      : v.chose === "candidate" ? "the rewrite" : "neither";
    events.push({ at: v.created, to: href.compare(v.run_id, v.fixture),
                  text: `a reader picked ${chose} on ${v.fixture}` });
  }
  events.sort((a, b) => new Date(b.at) - new Date(a.at));
  return events.slice(0, MAX_ITEMS);
}

function render(events, { fallback = false } = {}) {
  if (!el) return;
  el.innerHTML = toHTML(html`
    ${fallback ? html`<span class="tick-label">stream unreachable — last known:</span>`
               : html`<span class="tick-label">latest</span>`}
    <ul class="tick-list">
      ${events.length ? events.map(e => html`<li>
        <a href="${raw(e.to)}">${e.text}</a>
        ${e.static ? "" : html`<time class="crumb" data-tick="rel" datetime="${e.at}">${relative(e.at)}</time>`}
      </li>`) : html`<li><span class="crumb">nothing recorded yet</span></li>`}
    </ul>`);
}

async function refresh() {
  // A background tab does not need three selects every ninety seconds; the
  // visibilitychange handler refreshes the moment it is looked at again.
  if (typeof document !== "undefined" && document.hidden) {
    clearTimeout(timer);
    timer = setTimeout(refresh, REFRESH_MS);
    return;
  }
  try {
    const events = await gather();
    if (events.length) {
      lastGood = events;
      render(events);
    } else if (lastGood) {
      render(lastGood, { fallback: true });
    } else {
      render(STATIC_FALLBACK, { fallback: true });
    }
  } catch (err) {
    render(lastGood || STATIC_FALLBACK, { fallback: true });
  }
  clearTimeout(timer);
  timer = setTimeout(refresh, REFRESH_MS);
}

/** Shown when nothing has ever loaded: honest, and it still points somewhere. */
const STATIC_FALLBACK = [
  { at: new Date(0).toISOString(), to: href.metrics(),
    text: "the record of every generation" },
  { at: new Date(0).toISOString(), to: href.about(),
    text: "how the arena works" },
].map(e => ({ ...e, static: true }));
