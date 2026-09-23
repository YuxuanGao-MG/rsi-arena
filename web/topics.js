/* The topics: what the arena forecasts, one table, and the arithmetic that
 * differs between them.
 *
 * The site began as one topic — Kalshi soccer contracts, a five-minute move in
 * cents of a 0-1 price — and every page said "match" and "cents" because there
 * was nothing else to say. Two more topics run the same loop through the same
 * tables now, with a different unit (basis points of a relative move), a
 * different instance ("news item", "window") and a different grouping the
 * split respects ("symbol-day", "UTC day"). Everything a page needs to say in
 * words or compute in the unit lives here, keyed by topic id, so a view asks
 * the table rather than assuming Kalshi.
 *
 * Two rules keep this honest:
 *
 *   - a rollout row that carries no `topic` is a Kalshi row: the column was
 *     added in migration 008 and every row published before it is soccer;
 *   - the Kalshi entry reproduces today's numbers exactly — tick 0.01, move
 *     in cents, prices clamped to (0.01, 0.99) — so the pages the site has
 *     always shown do not shift by a hair when the table gains a second row.
 *
 * Deliberately imports nothing. stats.js, routes.js, clock.js and every view
 * read from here, so a dependency in the other direction would be a cycle.
 */

export const DEFAULT = "kalshi-horizon-5m";

const STORE = "rsi_topic";

/** `SYM@2026-09-21T14:05:00+00:00#id` → the `YYYYMMDD` of the instant. */
function dayOf(instance) {
  const stamp = String(instance).split("@")[1] || "";
  const day = stamp.slice(0, 10).replace(/-/g, "");
  return /^\d{8}$/.test(day) ? day : "";
}

export const TOPICS = {
  "kalshi-horizon-5m": {
    id: "kalshi-horizon-5m",
    title: "Kalshi soccer",
    unit: "cents",
    tick: 0.01,
    relative: false,
    deltaKey: "delta_cents",
    widthKey: "half_width_cents",
    words: { instance: "window", group: "match", groups: "matches", subject: "contract" },
    archive: "/archive/kalshi-jev.json",
    // The loop fires twice a day and a guard keeps the second firing from
    // meaning a second generation; the live collector follows the football.
    loopCrons: [[3, 17], [11, 17], [19, 17]],
    loopText: "three a day: 03:17, 11:17 and 19:17 UTC",
    loopWhen: "03:17, 11:17 and 19:17 UTC",
    liveCrons: [{ h: 1, m: 5 }, { h: 19, m: 5, dows: [1, 2, 3, 4, 5] },
                { h: 15, m: 5, dows: [0, 6] }],
    liveText: "19:05 weekdays · 15:05 weekends · 01:05 daily, UTC",
    /** `KXEPLGAME-26SEP05NFOTOT-NFO@2026-09-05T14:05:00+00:00` → the event ticker. */
    groupOf(instance) {
      const ticker = String(instance).split("@")[0];
      const cut = ticker.lastIndexOf("-");
      return cut > 0 ? ticker.slice(0, cut) : ticker;
    },
    blurb: "An LLM harness forecasts where a Kalshi soccer contract's mid price will " +
           "be five minutes later, in cents of a 0-1 price. Each generation, another " +
           "LLM rewrites it from its failures, and a rewrite is promoted only if it " +
           "beats its parent on matches neither ever saw.",
  },
  "news-equity-5m": {
    id: "news-equity-5m",
    title: "News → equities",
    unit: "bps",
    tick: 5,
    relative: true,
    deltaKey: "delta_bps",
    widthKey: "half_width_bps",
    words: { instance: "news item", group: "symbol-day", groups: "symbol-days", subject: "symbol" },
    archive: "/archive/news-equity-5m.json",
    loopCrons: [[8, 17], [16, 17], [0, 17]],
    loopText: "three a day: 08:17, 16:17 and 00:17 UTC",
    loopWhen: "08:17, 16:17 and 00:17 UTC",
    liveCrons: [13 * 60 + 35, 16 * 60 + 5, 18 * 60 + 5, 20 * 60 + 5]
      .map(t => ({ h: Math.floor(t / 60), m: t % 60, dows: [1, 2, 3, 4, 5] })),
    liveText: "13:35 · 16:05 · 18:05 · 20:05 UTC, weekdays",
    /** `AAPL@2026-09-21T14:05:00+00:00#id` → `AAPL-20260921`. */
    groupOf(instance) {
      const sym = String(instance).split("@")[0];
      const day = dayOf(instance);
      return day ? `${sym}-${day}` : sym;
    },
    blurb: "An LLM harness reads a news item about a US stock or ETF and forecasts " +
           "where the price will be five minutes later, in basis points relative to " +
           "the mid at the time. Each generation, another LLM rewrites it from its " +
           "failures, and a rewrite is promoted only if it beats its parent on " +
           "symbol-days neither ever saw.",
  },
  "crypto-horizon-1m": {
    id: "crypto-horizon-1m",
    title: "Crypto spot",
    unit: "bps",
    tick: 2,
    relative: true,
    deltaKey: "delta_bps",
    widthKey: "half_width_bps",
    words: { instance: "window", group: "day", groups: "days", subject: "symbol" },
    archive: "/archive/crypto-horizon-1m.json",
    loopCrons: [[5, 47], [13, 47], [21, 47]],
    loopText: "three a day: 05:47, 13:47 and 21:47 UTC",
    loopWhen: "05:47, 13:47 and 21:47 UTC",
    liveCrons: Array.from({ length: 24 }, (_, h) => ({ h, m: 5 })),
    liveText: "every hour at :05 UTC, fifty-five minutes each",
    /** `BTC@2026-09-21T14:05:00+00:00` → `D20260921`, the UTC day. */
    groupOf(instance) {
      const day = dayOf(instance);
      return day ? `D${day}` : String(instance).split("@")[0];
    },
    blurb: "An LLM harness forecasts the one-minute move in BTC, ETH and SOL spot, " +
           "in basis points relative to the mid at the time. Each generation, " +
           "another LLM rewrites it from its failures, and a rewrite is promoted " +
           "only if it beats its parent on UTC days neither ever saw.",
  },
};

export const TOPIC_IDS = Object.keys(TOPICS);

/** The topic a hash names — `#/t/<topic>/...` — or null when it names none. */
export function topicOfHash(hash) {
  const m = /^#\/?t\/([^/?#]+)/.exec(String(hash || ""));
  if (!m) return null;
  let id;
  try { id = decodeURIComponent(m[1]); } catch (e) { return null; }
  return id in TOPICS ? id : null;
}

/** What the reader chose last time, if the browser remembers. */
export function storedTopic() {
  try {
    const v = localStorage.getItem(STORE);
    return v && v in TOPICS ? v : null;
  } catch (e) { return null; }
}

export function rememberTopic(id) {
  if (!(id in TOPICS)) return;
  try { localStorage.setItem(STORE, id); } catch (e) { /* private mode */ }
}

/**
 * The topic the page is on: the route's, else the remembered one, else Kalshi.
 *
 * Reads `location.hash` itself rather than importing the router, because the
 * router imports this file.
 */
export function currentTopic(hash) {
  if (hash === undefined)
    hash = typeof location !== "undefined" ? location.hash : "";
  return topicOfHash(hash) || storedTopic() || DEFAULT;
}

/** The table row for a topic id, a row that carries one, or nothing (Kalshi). */
export function topicOf(rowOrTopic) {
  if (rowOrTopic == null) return TOPICS[DEFAULT];
  if (typeof rowOrTopic === "string") return TOPICS[rowOrTopic] || TOPICS[DEFAULT];
  // A row: `topic` when the column was published, else the unit tells the
  // same story — every bps topic shares one tick, every cents topic the other.
  if (rowOrTopic.topic && TOPICS[rowOrTopic.topic]) return TOPICS[rowOrTopic.topic];
  if (rowOrTopic.unit === "bps") return TOPICS["crypto-horizon-1m"];
  return TOPICS[DEFAULT];
}

/** One tick: the smallest benchmark error that means anything. */
export const tickOf = rowOrTopic => topicOf(rowOrTopic).tick;

/** What printed, in the topic's unit: cents of price, or basis points of it. */
export function moveOf(row, topic) {
  const t = topicOf(topic ?? row);
  if (row.mid_now == null || row.realised == null) return null;
  return t.relative ? (row.realised / row.mid_now - 1) * 1e4
                    : (row.realised - row.mid_now) * 100;
}

/** What the harness said, in the same unit. */
export function saidOf(row, topic) {
  const t = topicOf(topic ?? row);
  if (row.mid_now == null || row.predicted == null) return null;
  return t.relative ? (row.predicted / row.mid_now - 1) * 1e4
                    : (row.predicted - row.mid_now) * 100;
}

/**
 * The price a forecast implied. Kalshi's is clamped to a tradeable price the
 * way `topics/kalshi_horizon/score.py:quote_from` clamps it; a relative topic
 * has no such bounds.
 */
export function predictedOf(mid, delta, topic) {
  const t = topicOf(topic);
  if (mid == null || delta == null) return null;
  if (t.relative) return mid * (1 + delta / 1e4);
  return Math.min(0.99, Math.max(0.01, mid + delta / 100));
}

/** The half-width a forecast quoted, as a price distance. */
export function halfWidthOf(mid, width, topic) {
  const t = topicOf(topic);
  if (width == null) return null;
  if (t.relative) return mid == null ? null : Math.abs(mid) * width / 1e4;
  return width / 100;
}

/** "+3.0c" / "+25bps": a signed move in the topic's unit. */
export function fmtMove(v, topic) {
  if (v == null || Number.isNaN(v)) return "—";
  const t = topicOf(topic);
  const sign = v > 0 ? "+" : "";
  return t.unit === "bps" ? `${sign}${Math.round(Number(v))}bps`
                          : `${sign}${Number(v).toFixed(1)}c`;
}

/** A stored `err`/`naive_error` — price units for cents, bps for bps — as text. */
export function fmtErr(v, topic) {
  if (v == null || Number.isNaN(v)) return "—";
  const t = topicOf(topic);
  return t.unit === "bps" ? `${Number(v).toFixed(1)}bps` : `${Number(v * 100).toFixed(2)}c`;
}

/** "±2c" / "±15bps": a quoted half-width in the topic's unit. */
export function fmtWidth(v, topic) {
  const t = topicOf(topic);
  if (v == null) return `±?${t.unit === "bps" ? "bps" : "c"}`;
  return t.unit === "bps" ? `±${Number(v)}bps` : `±${Number(v)}c`;
}

/** A price: three decimals for a 0-1 contract, sensible ones for a quote. */
export function fmtPrice(v, topic) {
  if (v == null) return "—";
  const t = topicOf(topic);
  const n = Number(v);
  if (t.unit !== "bps") return n.toFixed(3);
  return Math.abs(n) >= 1000 ? n.toFixed(0) : n.toFixed(2);
}

/** The forecast's delta and width, read from `output` by the topic's keys. */
export function forecastOf(output, topic) {
  const t = topicOf(topic);
  const o = output || {};
  const delta = typeof o[t.deltaKey] === "number" ? o[t.deltaKey] : null;
  const width = typeof o[t.widthKey] === "number" ? o[t.widthKey] : null;
  return { delta, width };
}

/** The words a topic uses for its parts. */
export const wordsOf = topic => topicOf(topic).words;

/** The next firing of a list of UTC crons, each `{h, m, dows?}`. */
export function nextCron(crons, now = Date.now()) {
  const t = new Date(now);
  const candidates = [];
  for (let d = 0; d < 8; d++) {
    const day = new Date(Date.UTC(t.getUTCFullYear(), t.getUTCMonth(), t.getUTCDate() + d));
    const dow = day.getUTCDay();
    for (const c of crons) {
      const [h, m, dows] = Array.isArray(c) ? [c[0], c[1], null] : [c.h, c.m, c.dows || null];
      if (dows && !dows.includes(dow)) continue;
      candidates.push(Date.UTC(day.getUTCFullYear(), day.getUTCMonth(), day.getUTCDate(), h, m));
    }
  }
  const future = candidates.filter(c => c > now);
  return future.length ? Math.min(...future) : NaN;
}
