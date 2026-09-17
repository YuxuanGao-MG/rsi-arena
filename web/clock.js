/* The one-second heartbeat for everything that can stay honest without a fetch.
 *
 * A countdown to a cron is real liveness — the schedule is a fact, and the
 * arithmetic is local. A relative time that silently ages ("2m ago", forever)
 * is a small lie of omission. One interval walks the DOM for `data-tick`
 * elements and rewrites their text:
 *
 *   data-tick="rel"      datetime=ISO   → "3m ago", self-ageing
 *   data-tick="elapsed"  datetime=ISO   → "1h 34m", counting up
 *   data-tick="until"    data-target=loop|live → "in 4h 12m", counting down
 *
 * Content updates, not animation: `prefers-reduced-motion` turns off the
 * pulses and slides elsewhere, but a clock that stops ticking is not a
 * calmer clock, it is a wrong one. The interval parks while the tab is
 * hidden — nothing is watching, and a backgrounded timer is battery spent
 * on nobody.
 */

import { relative, elapsed } from "./status.js";

/** Next 03:17 UTC — the loop's cron. Pure, for the tests. */
export function nextLoopRun(now = Date.now()) {
  const t = new Date(now);
  const next = new Date(Date.UTC(t.getUTCFullYear(), t.getUTCMonth(), t.getUTCDate(), 3, 17));
  if (next <= t) next.setUTCDate(next.getUTCDate() + 1);
  return next.getTime();
}

/** Next live collection: 19:05 UTC Mon–Fri, 15:05 Sat–Sun, 01:05 daily. */
export function nextLiveRun(now = Date.now()) {
  const t = new Date(now);
  const candidates = [];
  for (let d = 0; d < 3; d++) {
    const day = new Date(Date.UTC(t.getUTCFullYear(), t.getUTCMonth(), t.getUTCDate() + d));
    const dow = day.getUTCDay();
    const hours = [[1, 5], dow === 0 || dow === 6 ? [15, 5] : [19, 5]];
    for (const [h, m] of hours)
      candidates.push(Date.UTC(day.getUTCFullYear(), day.getUTCMonth(), day.getUTCDate(), h, m));
  }
  return Math.min(...candidates.filter(c => c > now));
}

/** "in 4h 12m", or "in 3m 20s" close in so the tick is visible. */
export function untilText(target, now = Date.now()) {
  const s = Math.max(0, Math.round((target - now) / 1000));
  if (s < 600) return `in ${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
  const h = Math.floor(s / 3600), m = Math.round((s % 3600) / 60);
  return h ? `in ${h}h ${m}m` : `in ${m}m`;
}

const TARGETS = { loop: nextLoopRun, live: nextLiveRun };

export function tickAll(root = document, now = Date.now()) {
  for (const el of root.querySelectorAll("[data-tick]")) {
    const kind = el.getAttribute("data-tick");
    if (kind === "rel") {
      const at = el.getAttribute("datetime");
      if (at) el.textContent = relative(at, now);
    } else if (kind === "elapsed") {
      const at = el.getAttribute("datetime");
      if (at) el.textContent = elapsed(at, now);
    } else if (kind === "until") {
      const fn = TARGETS[el.getAttribute("data-target")];
      if (fn) el.textContent = untilText(fn(now), now);
    }
  }
}

let timer = 0;

export function startClock() {
  if (timer) return;
  const beat = () => {
    if (typeof document !== "undefined" && !document.hidden) tickAll();
  };
  timer = setInterval(beat, 1000);
  if (typeof document !== "undefined") {
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) tickAll();       // catch up the moment eyes return
    });
  }
}
