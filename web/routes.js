/* Where every link points.
 *
 * Three top-level views — overview, metrics, about — after the shape of
 * Xiaomi's MiMo RL page, where the running system is the exhibit and the page
 * carries its state. Every route the site ever had still resolves: a deep
 * link into a generation or a window is someone's bookmark, and bookmarks
 * outrank information architecture. The old pages nest under Metrics rather
 * than redirect, so the URL a reader saved is the URL they stay on.
 *
 * A route may start with `t/<topic>/`, which picks which of the arena's topics
 * the page is about. The prefix is omitted for the default topic, so every
 * link the site has ever handed out — and every bookmark — still means what it
 * meant: `#/generation/gen5` is Kalshi's gen5, and
 * `#/t/crypto-horizon-1m/generation/gen5` is the crypto loop's.
 *
 * Kept apart from the router so views can build an href without importing the
 * router and the router can import the views. Every id here is
 * `encodeURIComponent`-ed on the way in and decoded on the way out — run ids
 * and fixtures come from Kalshi and ESPN, not from us.
 */

import { TOPICS, TOPIC_IDS, DEFAULT, currentTopic, storedTopic } from "./topics.js";
import { html, raw } from "./dom.js";

const e = encodeURIComponent;

/** `#/metrics` → `#/t/<topic>/metrics`, unless the topic is the default. */
export function withTopic(path, topic = currentTopic()) {
  if (!topic || topic === DEFAULT || !(topic in TOPICS)) return path;
  return `#/t/${e(topic)}/` + String(path).replace(/^#\/?/, "");
}

export const href = {
  overview: () => withTopic("#/"),
  metrics: () => withTopic("#/metrics"),
  about: () => withTopic("#/about"),
  runs: () => withTopic("#/metrics"),    // the generations table's home now
  run: (id, side) => withTopic(
    `#/generation/${e(id)}${side && side !== "candidate" ? `?side=${e(side)}` : ""}`),
  window: id => withTopic(`#/window/${e(id)}`),
  lineage: () => withTopic("#/lineage"),
  compare: (runId, fixture) => withTopic(
    "#/compare" + (runId ? `/${e(runId)}` : "") + (fixture ? `/${e(fixture)}` : "")),
  votes: () => withTopic("#/votes"),
  archive: () => withTopic("#/archive"),
  live: () => withTopic("#/live"),
  /** The paper books; `book` picks one other than the live book for the curve. */
  trading: book => withTopic("#/trading" + (book ? `?book=${e(book)}` : "")),
  cost: () => withTopic("#/cost"),
  /** The same page, on another topic. Always explicit, so it overrides what
   *  the browser remembered — that is the whole point of clicking it. */
  topic: (id, path = "#/") => `#/t/${e(id)}/` + String(path).replace(/^#\/?/, ""),
};

/** Entries are `[label, hrefFn, name]`: the href depends on the topic. */
export const NAV = [
  ["Overview", href.overview, "overview"],
  ["Metrics", href.metrics, "metrics"],
  ["About", href.about, "about"],
];

/** The Metrics sub-nav: every section that lives under it. */
export const METRICS_NAV = [
  ["Generations", href.metrics, "metrics"],
  ["Live", href.live, "live"],
  ["Trading", href.trading, "trading"],
  ["Archive", href.archive, "archive"],
  ["Lineage", href.lineage, "lineage"],
  ["Compare", href.compare, "compare"],
  ["Votes", href.votes, "votes"],
  ["Cost", href.cost, "cost"],
];

/** Which top-level nav entry owns a route. */
export function navOf(name) {
  if (name === "overview") return "overview";
  if (name === "about") return "about";
  return "metrics";
}

/**
 * The topic switcher: one real link per topic, the current one marked.
 *
 * Links rather than buttons because a topic is a place — it has a URL, it can
 * be bookmarked, middle-clicked, and read back by a screen reader as "current
 * page". Exported so the accessibility checks can render it without a DOM.
 */
export function topicNav(active = currentTopic()) {
  return html`${TOPIC_IDS.map(id => html`
    <li><a href="${raw(href.topic(id))}"
           ${raw(id === active ? 'aria-current="page"' : "")}
           title="${TOPICS[id].title}">${TOPICS[id].title}</a></li>`)}`;
}

/** `#/t/<topic>/compare/<run>/<fixture>?side=x` → { name, params, query, topic }. */
export function parse(hash) {
  const raw = String(hash || "").replace(/^#\/?/, "");
  const [path, search = ""] = raw.split("?");
  let parts = path.split("/").filter(Boolean).map(decodeURIComponent);
  const query = Object.fromEntries(new URLSearchParams(search));

  // The topic prefix, when there is one. An unknown topic is a missing page,
  // not a silent fall-through to Kalshi: `#/t/typo/` should say so.
  let topic = storedTopic() || DEFAULT;
  if (parts[0] === "t") {
    if (!(parts[1] in TOPICS)) return { name: "missing", params: { path }, query, topic };
    topic = parts[1];
    parts = parts.slice(2);
  }
  const [head, ...rest] = parts;

  switch (head) {
    case undefined:       return { name: "overview", params: {}, query, topic };
    case "metrics":       return { name: "metrics", params: {}, query, topic };
    case "about":         return { name: "about", params: {}, query, topic };
    case "generation":    return { name: "run", params: { id: rest[0] }, query, topic };
    case "window":        return { name: "window", params: { id: rest[0] }, query, topic };
    case "lineage":       return { name: "lineage", params: {}, query, topic };
    case "compare":       return { name: "compare", params: { runId: rest[0], fixture: rest[1] }, query, topic };
    case "votes":         return { name: "votes", params: {}, query, topic };
    case "archive":       return { name: "archive", params: { id: rest[0] }, query, topic };
    case "live":          return { name: "live", params: {}, query, topic };
    case "trading":       return { name: "trading", params: {}, query, topic };
    case "cost":          return { name: "cost", params: {}, query, topic };
    default:              return { name: "missing", params: { path }, query, topic };
  }
}
