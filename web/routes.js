/* Where every link points.
 *
 * Kept apart from the router so views can build an href without importing the
 * router and the router can import the views. Every id here is
 * `encodeURIComponent`-ed on the way in and decoded on the way out — run ids
 * and fixtures come from Kalshi and ESPN, not from us.
 */

const e = encodeURIComponent;

export const href = {
  runs: () => "#/",
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
  ["Generations", href.runs(), "runs"],
  ["Live", href.live(), "live"],
  ["Archive", href.archive(), "archive"],
  ["Lineage", href.lineage(), "lineage"],
  ["Compare", href.compare(), "compare"],
  ["Votes", href.votes(), "votes"],
  ["Cost", href.cost(), "cost"],
];

/** `#/compare/<run>/<fixture>?side=x` → { name, params, query }. */
export function parse(hash) {
  const raw = String(hash || "").replace(/^#\/?/, "");
  const [path, search = ""] = raw.split("?");
  const parts = path.split("/").filter(Boolean).map(decodeURIComponent);
  const query = Object.fromEntries(new URLSearchParams(search));
  const [head, ...rest] = parts;

  switch (head) {
    case undefined:       return { name: "runs", params: {}, query };
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
