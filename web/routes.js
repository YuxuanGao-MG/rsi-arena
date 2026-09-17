/* Where every link points.
 *
 * Three top-level views — overview, metrics, about — after the shape of
 * Xiaomi's MiMo RL page, where the running system is the exhibit and the page
 * carries its state. Every route the site ever had still resolves: a deep
 * link into a generation or a window is someone's bookmark, and bookmarks
 * outrank information architecture. The old pages nest under Metrics rather
 * than redirect, so the URL a reader saved is the URL they stay on.
 *
 * Kept apart from the router so views can build an href without importing the
 * router and the router can import the views. Every id here is
 * `encodeURIComponent`-ed on the way in and decoded on the way out — run ids
 * and fixtures come from Kalshi and ESPN, not from us.
 */

const e = encodeURIComponent;

export const href = {
  overview: () => "#/",
  metrics: () => "#/metrics",
  about: () => "#/about",
  runs: () => "#/metrics",               // the generations table's home now
  run: (id, side) => `#/generation/${e(id)}${side && side !== "candidate" ? `?side=${e(side)}` : ""}`,
  window: id => `#/window/${e(id)}`,
  lineage: () => "#/lineage",
  compare: (runId, fixture) =>
    "#/compare" + (runId ? `/${e(runId)}` : "") + (fixture ? `/${e(fixture)}` : ""),
  votes: () => "#/votes",
  archive: () => "#/archive",
  live: () => "#/live",
  cost: () => "#/cost",
};

export const NAV = [
  ["Overview", href.overview(), "overview"],
  ["Metrics", href.metrics(), "metrics"],
  ["About", href.about(), "about"],
];

/** The Metrics sub-nav: every section that lives under it. */
export const METRICS_NAV = [
  ["Generations", href.metrics(), "metrics"],
  ["Live", href.live(), "live"],
  ["Archive", href.archive(), "archive"],
  ["Lineage", href.lineage(), "lineage"],
  ["Compare", href.compare(), "compare"],
  ["Votes", href.votes(), "votes"],
  ["Cost", href.cost(), "cost"],
];

/** Which top-level nav entry owns a route. */
export function navOf(name) {
  if (name === "overview") return "overview";
  if (name === "about") return "about";
  return "metrics";
}

/** `#/compare/<run>/<fixture>?side=x` → { name, params, query }. */
export function parse(hash) {
  const raw = String(hash || "").replace(/^#\/?/, "");
  const [path, search = ""] = raw.split("?");
  const parts = path.split("/").filter(Boolean).map(decodeURIComponent);
  const query = Object.fromEntries(new URLSearchParams(search));
  const [head, ...rest] = parts;

  switch (head) {
    case undefined:       return { name: "overview", params: {}, query };
    case "metrics":       return { name: "metrics", params: {}, query };
    case "about":         return { name: "about", params: {}, query };
    case "generation":    return { name: "run", params: { id: rest[0] }, query };
    case "window":        return { name: "window", params: { id: rest[0] }, query };
    case "lineage":       return { name: "lineage", params: {}, query };
    case "compare":       return { name: "compare", params: { runId: rest[0], fixture: rest[1] }, query };
    case "votes":         return { name: "votes", params: {}, query };
    case "archive":       return { name: "archive", params: { id: rest[0] }, query };
    case "live":          return { name: "live", params: {}, query };
    case "cost":          return { name: "cost", params: {}, query };
    default:              return { name: "missing", params: { path }, query };
  }
}
