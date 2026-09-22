/* What the arena is doing right now — the state the chrome carries.
 *
 * The loop keeps one row per run in `rsi.progress` current as it moves
 * (baseline → search → cascade → holdout → audit → done), and this module
 * turns those rows into exactly one of five honest states:
 *
 *   live          a non-done row updated inside the staleness window
 *   stale         a non-done row nobody has touched in five minutes — the run
 *                 died mid-phase, and saying "live" forever would be a lie
 *   running-blind GitHub says a workflow is executing but no heartbeat row
 *                 exists — the run predates the heartbeat, or died before its
 *                 first write. First-class, not an error: the generation
 *                 running the day this shipped emits no rows at all
 *   idle          the newest word is a verdict; next start is on the cron
 *   reconnecting  the poll itself failed — the previous state is kept and
 *                 labelled, because a dead dot teaches a reader to ignore it
 *
 * Classification is a pure function of (rows, github, now) so the tests can
 * hold every state without faking clocks globally.
 */

import { q, external, serverNow, ApiError, topicFilter } from "./data.js";
import { TOPICS, DEFAULT, currentTopic } from "./topics.js";

/** Mirrors rsi_arena/loop/progress.py:STALE_AFTER_S. */
export const STALE_AFTER_S = 300;

/** The Kalshi loop's crons, stated for prose; clock.js computes the countdown
 * and topics.js holds every topic's. */
export const NEXT_SCHEDULED = TOPICS[DEFAULT].loopWhen;

// Repo-level rather than per-workflow: one request covers both the loop and
// the live collector, and 60/hr splits badly in two.
const GH_RUNS = "https://api.github.com/repos/YuxuanGao-MG/rsi-arena/actions/" +
                "runs?per_page=10&branch=main";
const GH_MIN_INTERVAL_MS = 90_000;     // 60 req/hr is a budget, not a suggestion
const POLL_LIVE_MS = 20_000;
const POLL_IDLE_MS = 120_000;

/** The workflow runs, split by file. Accepts the old per-workflow shape too. */
function ghRuns(github) {
  const all = (github && github.workflow_runs) || [];
  const of = name => all.find(r => !r.path || String(r.path).endsWith(name));
  return { loop: of("loop.yml"), live: all.find(r => String(r.path || "").endsWith("live.yml")) };
}

const executing = r => r && (r.status === "in_progress" || r.status === "queued");

/** Pure: progress rows + optional GitHub payload + a clock → one state.
 * The default clock is the database's, not this machine's: staleness compares
 * our now with the row's updated_at, and a fast local clock would call every
 * live run dead. */
export function classify(rows, github, now = serverNow()) {
  const sorted = [...(rows || [])].sort((a, b) =>
    new Date(b.updated_at) - new Date(a.updated_at));
  const newest = sorted[0];
  const gh = ghRuns(github);
  // The collector is not a generation; it rides along on whatever state the
  // loop is in, so the chrome can say "live collection running" while idle.
  const collection = executing(gh.live)
    ? { running: true, id: gh.live.id, startedAt: gh.live.run_started_at || gh.live.created_at }
    : null;

  if (newest && newest.phase !== "done") {
    const age = Math.max(0, (now - new Date(newest.updated_at).getTime()) / 1000);
    const detail = newest.detail || {};
    if (age <= STALE_AFTER_S) {
      return { kind: "live", run: newest.run_id, phase: newest.phase,
               spentUsd: detail.spent_usd ?? null,
               budgetUsd: detail.budget_usd ?? null,
               evaluations: detail.evaluations ?? null,
               startedAt: newest.started_at, updatedAt: newest.updated_at, collection };
    }
    return { kind: "stale", run: newest.run_id, phase: newest.phase,
             lastSeen: newest.updated_at, ageS: Math.round(age), collection };
  }

  // No heartbeat mid-run. GitHub is the cross-check for the runs that predate
  // it — and only an *executing* workflow counts as evidence of one.
  if (executing(gh.loop)) {
    return { kind: "running-blind", ghRun: gh.loop.id, ghStatus: gh.loop.status,
             startedAt: gh.loop.run_started_at || gh.loop.created_at, collection };
  }

  return { kind: "idle",
           lastRun: newest ? newest.run_id : null,
           conclusion: newest ? (newest.detail || {}).conclusion ?? null : null,
           reason: newest ? (newest.detail || {}).reason ?? null : null,
           finishedAt: newest ? newest.updated_at : null,
           github: gh.loop ? { id: gh.loop.id, conclusion: gh.loop.conclusion } : null,
           collection };
}

/* ---------------------------------------------------------------------------
 * The polling loop. One instance for the whole page: the nav dot and the
 * overview hero subscribe to the same store, so they can never disagree.
 */

const listeners = new Set();
let current = { kind: "loading" };
let reconnecting = false;
let timer = 0;
let ghLast = 0;
let ghCache = null;
let ghDown = false;                    // 403: stop asking until the next hour
let started = false;
let overviewVisible = false;
let topic = null;                      // which loop's heartbeat the chrome reads

export function statusNow() {
  return reconnecting ? { ...current, reconnecting: true } : current;
}

export function onStatus(fn) {
  listeners.add(fn);
  fn(statusNow());
  return () => listeners.delete(fn);
}

/** The overview polls GitHub; the rest of the site leaves the budget alone. */
export function setOverviewVisible(v) { overviewVisible = !!v; }

/**
 * Which topic's progress rows the chrome reads. Each topic runs its own loop,
 * so the dot in the header would otherwise say "live" for a generation the
 * page is not about. Switching resets to "loading" and polls at once rather
 * than showing the last topic's state under the new topic's name.
 */
export function setStatusTopic(id) {
  if (id === topic) return;
  topic = id;
  if (!started) return;
  current = { kind: "loading" };
  emit();
  poll();
}

function emit() {
  const snapshot = statusNow();
  for (const fn of listeners) {
    try { fn(snapshot); } catch (err) { console.error(err); }
  }
}

async function pollGithub() {
  const hidden = typeof document !== "undefined" && document.hidden;
  if (!overviewVisible || hidden || ghDown) return ghCache;
  if (Date.now() - ghLast < GH_MIN_INTERVAL_MS) return ghCache;
  ghLast = Date.now();
  try {
    ghCache = await external(GH_RUNS, {});
  } catch (err) {
    // 403 is the rate limit; asking again inside the hour only digs deeper.
    // Anything else is a blip and the next cycle retries naturally.
    if (err instanceof ApiError && err.status === 403) {
      ghDown = true;
      setTimeout(() => { ghDown = false; }, 3_600_000);
    }
  }
  return ghCache;
}

async function poll() {
  let rows = null;
  const mine = topic || currentTopic();
  try {
    try {
      rows = await q(`progress?select=*${topicFilter(mine)}&order=updated_at.desc&limit=5`,
                     { fresh: true });
    } catch (err) {
      // Before migration 008 the table has no `topic` column and PostgREST
      // answers 400. Every row there is Kalshi's, so for Kalshi the unfiltered
      // read is the same question; for any other topic there is nothing yet.
      if (!(err instanceof ApiError && err.status === 400)) throw err;
      rows = mine === DEFAULT
        ? await q("progress?select=*&order=updated_at.desc&limit=5", { fresh: true })
        : [];
    }
    reconnecting = false;
  } catch (err) {
    if (err && err.kind === "aborted") return;
    // Keep the last known state, say the connection is the problem, back off.
    reconnecting = true;
    emit();
    schedule(Math.min(POLL_IDLE_MS, (current.backoff = (current.backoff || 10_000) * 2)));
    return;
  }
  delete current.backoff;
  if (mine !== (topic || currentTopic())) return;   // a switch mid-flight already re-polled
  const github = await pollGithub();
  current = classify(rows, github);
  emit();
  schedule(current.kind === "live" || current.kind === "running-blind"
           ? POLL_LIVE_MS : POLL_IDLE_MS);
}

function schedule(ms) {
  clearTimeout(timer);
  timer = setTimeout(poll, ms);
}

export function startStatus() {
  if (started) return;
  started = true;
  if (typeof document !== "undefined") {
    document.addEventListener("visibilitychange", () => {
      // A tab coming back after an hour should not show the state it left.
      if (!document.hidden) poll();
    });
  }
  poll();
}

/* ---------- small helpers the chrome shares ------------------------------- */

/** "3m ago", "2h ago" — the footer ticker's clock. */
export function relative(iso, now = Date.now()) {
  const s = Math.max(0, Math.round((now - new Date(iso).getTime()) / 1000));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 172_800) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86_400)}d ago`;
}

export function elapsed(iso, now = Date.now()) {
  const s = Math.max(0, Math.round((now - new Date(iso).getTime()) / 1000));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return h ? `${h}h ${m}m` : `${m}m`;
}
