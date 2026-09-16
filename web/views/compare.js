/* Two harnesses, one match, every window they were both scored on.
 *
 * The scored answer is deliberately not on this page. Neither the per-window
 * skill nor the realised price is selected, and the pooled numbers arrive only
 * in the reply to the vote — the page this replaces passed both pooled skills
 * into the click handler as arguments, so "hidden until you vote" was hidden
 * from nobody with a developer console, and the same two numbers were what the
 * browser then posted as the record of what the arithmetic said.
 */

import { q, qAll, rpc, invalidate, ApiError } from "../data.js";
import { href } from "../routes.js";
import {
  html, raw, mount, pill, n3, dir, empty, clock, cents, price, plural, stat,
} from "../dom.js";
import { addActions } from "../actions.js";
import { groupBy } from "../stats.js";

const VOTER = (() => {
  try {
    let v = localStorage.getItem("rsi_voter");
    if (!v) { v = Math.random().toString(36).slice(2, 12); localStorage.setItem("rsi_voter", v); }
    return v;
  } catch (e) { return "no-storage"; }         // private mode still gets to vote
})();

export async function compareView(ctx) {
  if (!ctx.params.runId) return pickGeneration(ctx);
  if (!ctx.params.fixture) return pickFixture(ctx);
  return duel(ctx);
}

/* ---------- pick a generation --------------------------------------------- */

async function pickGeneration({ signal }) {
  const runs = await qAll("runs?select=id,created,accepted,candidate_fp,incumbent_fp&order=created.desc",
                          { signal, pageSize: 200, max: 2000 });
  if (!runs.length)
    return { title: "Compare", heading: "Nothing to compare", body: empty("No generation has been published.") };

  return {
    title: "Compare",
    heading: "Which set of judgements would you rather have had?",
    lead: html`Two harnesses, one match, every window they were both scored on. One is the
      incumbent and one is a rewrite; a coin decides which sits on the left, and which way it
      landed is stored with your vote.`,
    body: html`
      <section class="panel"><div class="panel-b prose">
        <p><strong>Why a whole match rather than one forecast.</strong> A single call is
        verifiable five minutes later, so asking which reads better mostly measures which reads
        better — and a harness can argue well for a call that loses. Thirty-four windows show
        when a harness stays quiet, whether its half-width tracks its uncertainty, and whether
        its reasons change with the situation or are one argument with the numbers swapped.
        Those are visible to a reader and invisible in a single row.</p>
        <p class="note">The number worth watching is not who wins. It is how often a reader and
        the arithmetic disagree — because a harness that reads well and scores badly is the thing
        most likely to fool the rewriter, which is judging text too.
        <a href="${raw(href.votes())}">What the crowd has said so far</a>.</p>
      </div></section>
      <section class="panel">
        <div class="panel-h"><h2>Pick a generation</h2></div>
        <ul class="rows">${runs.map(r => html`<li>
          <a class="row" href="${raw(href.compare(r.id))}">
            <span class="mark brand-mark" aria-hidden="true"></span>
            <span><span class="name">${r.id}</span>
              <span class="why">${r.candidate_fp === r.incumbent_fp
                ? "the search returned the incumbent, so both columns are the same harness"
                : "a rewrite against its incumbent"}</span></span>
            <span class="spark"></span>
            <span class="right crumb">choose a match</span>
          </a></li>`)}</ul>
      </section>`,
  };
}

/* ---------- pick a match --------------------------------------------------- */

async function pickFixture({ params, signal }) {
  const runId = params.runId;
  // Narrow on purpose. Counting distinct fixtures used to mean pulling four
  // thousand rollouts with their `output` jsonb, ordered by time and cut at the
  // limit — so the count printed beside the list was of whatever survived the
  // cut, not of the matches played.
  const index = await qAll(
    `rollouts?run_id=eq.${encodeURIComponent(runId)}&select=fixture,side,split&order=fixture.asc`,
    { signal, max: 40_000 });
  if (!index.length)
    return { title: runId, heading: "No rollouts for this generation",
             crumbs: [["compare", href.compare()], [runId]],
             body: empty("Nothing was published under this run id.") };

  const byFixture = groupBy(index, "fixture");
  const rows = [...byFixture.entries()].map(([fixture, rs]) => ({
    fixture,
    both: rs.filter(x => x.side === "baseline").length,
    holdout: rs.some(x => x.split === "holdout"),
  }));

  return {
    title: `${runId} · matches`,
    heading: "Pick a match",
    lead: html`${plural(rows.length, "match")}, every one of them read by both harnesses.`,
    crumbs: [["compare", href.compare()], [runId]],
    body: html`<section class="panel">
      <div class="panel-h"><h2>${plural(rows.length, "match")}</h2>
        ${index.truncated ? pill("list truncated", "warn") : ""}</div>
      <ul class="rows">${rows.map(r => html`<li>
        <a class="row" href="${raw(href.compare(runId, r.fixture))}">
          <span class="mark brand-mark" aria-hidden="true"></span>
          <span><span class="name mono ticker">${r.fixture}</span>
            <span class="why">${plural(r.both, "window")} each
              · ${r.holdout ? "held out" : "train"}</span></span>
          <span class="spark"></span>
          <span class="right crumb">read both</span>
        </a></li>`)}</ul>
    </section>`,
  };
}

/* ---------- the duel -------------------------------------------------------- */

async function duel({ params, signal }) {
  const { runId, fixture } = params;
  const rows = await qAll(
    `rollouts?run_id=eq.${encodeURIComponent(runId)}&fixture=eq.${encodeURIComponent(fixture)}` +
    `&select=side,ticker,at,mid_now,half_width,output&order=at.asc`,
    { signal, max: 4000 });

  const paired = new Map();
  for (const r of rows) {
    const key = `${r.ticker}|${r.at}`;
    if (!paired.has(key)) paired.set(key, {});
    paired.get(key)[r.side] = r;
  }
  const pairs = [...paired.values()].filter(p => p.baseline && p.candidate);
  if (!pairs.length)
    return { title: fixture, heading: "This match has no paired windows",
             crumbs: [["compare", href.compare()], [runId, href.compare(runId)], [fixture]],
             body: empty("Both harnesses have to have been scored on the same window for a "
                       + "comparison to mean anything.") };

  // A coin decides the left column, and which way it landed is stored with the
  // vote: position bias is real, and a vote nobody can correct for is unusable.
  const flipped = Math.random() < 0.5;
  const L = flipped ? "candidate" : "baseline", R = flipped ? "baseline" : "candidate";

  return {
    title: fixture,
    heading: html`<span class="mono">${fixture}</span>`,
    lead: html`${plural(pairs.length, "window")}, both harnesses on each. Read the pair, then say
      which set you would rather have had. Neither the scores nor the prices that printed are on
      this page; they come back with your vote.`,
    crumbs: [["compare", href.compare()], [runId, href.compare(runId)], [fixture]],
    body: html`
      <div class="duel">${column(L, flipped ? "B" : "A", pairs)}${column(R, flipped ? "A" : "B", pairs)}</div>
      <section class="panel" id="voteBox"><div class="panel-b">
        <p class="eyebrow">your call</p>
        <div class="btn-row">
          <button class="btn" type="button" data-action="vote" data-chose="${L}">
            ${flipped ? "B" : "A"} — the left column</button>
          <button class="btn" type="button" data-action="vote" data-chose="${R}">
            ${flipped ? "A" : "B"} — the right column</button>
          <button class="btn" type="button" data-action="vote" data-chose="neither">neither</button>
        </div>
        <p class="note">Your vote is stored with the coin flip and with the pooled skill of both
        sides, computed by the database rather than by this page.</p>
      </div></section>`,
    ready: () => addActions({
      vote: el => castVote(el, { runId, fixture, left: L }),
    }),
  };
}

function column(side, tag, pairs) {
  const spoke = pairs.filter(p =>
    Math.abs((p[side].output && p[side].output.delta_cents) ?? 0) > 0.001).length;
  return html`<section class="side" aria-label="Harness ${tag}">
    <div class="head"><span class="tag" aria-hidden="true">${tag}</span>
      <span><span class="name">Harness ${tag}</span>
        <span class="crumb">had an opinion on ${spoke} of ${pairs.length} windows</span></span></div>
    <ul class="calls">${pairs.map(p => {
      const r = p[side], o = r.output || {};
      const d = o.delta_cents;
      return html`<li class="call">
        <div class="top">
          <span class="clock">${clock(r.at)}</span>
          <span class="move ${dir(d)}">${d == null ? "—" : cents(d)}</span>
          <span class="width">±${o.half_width_cents ?? "?"}c</span>
          <span class="clock spacer">mid ${price(r.mid_now)}</span>
        </div>
        ${o.driver ? html`<p class="say">${o.driver}</p>`
                   : html`<p class="say note">no reasoning kept for this generation</p>`}
        ${o.falsifier ? html`<p class="falsi">wrong if: ${o.falsifier}</p>` : ""}
      </li>`;
    })}</ul>
  </section>`;
}

/* ---------- the vote --------------------------------------------------------- */

async function castVote(el, { runId, fixture, left }) {
  const box = document.getElementById("voteBox");
  const buttons = [...box.querySelectorAll("button")];
  buttons.forEach(b => { b.disabled = true; });
  el.textContent = "recording…";

  let answer;
  try {
    answer = await rpc("cast_vote", {
      run_id: runId, fixture, chose: el.dataset.chose, left_side: left, voter: VOTER, note: null,
    });
    invalidate("votes");
  } catch (err) {
    const missing = err instanceof ApiError && err.status === 404;
    return mount(box, html`<div class="panel-b">
      <p class="eyebrow">not recorded</p>
      <p class="prose">${missing
        ? html`This database does not have the vote function installed, so nothing was stored.
            Someone has to run <code>supabase/migrations/002_votes_rpc.sql</code> against it.`
        : err.message}</p>
      <p class="note">Saying so is the point: the page this replaces swallowed the failure in an
      empty <code>catch</code> and showed the same thank-you either way.</p>
      <button class="btn" type="button" data-action="retry">Reload the page</button>
    </div>`);
  }

  const b = answer && answer.baseline_skill, c = answer && answer.candidate_skill;
  const better = b == null || c == null ? null : b === c ? "neither" : b > c ? "baseline" : "candidate";
  const agreed = better != null && el.dataset.chose === better;

  mount(box, html`<div class="panel-b fade">
    <p class="eyebrow">the answer</p>
    <div class="cards">
      ${stat({ value: n3(b), tone: dir(b), label: "incumbent" })}
      ${stat({ value: n3(c), tone: dir(c), label: "rewrite" })}
      ${stat({ value: better || "—", label: "the arithmetic preferred" })}
    </div>
    <p class="prose ${agreed ? "up" : "down"}">${agreed
      ? "Your reading and the score agree."
      : "Your reading and the score disagree — the interesting case. A harness can argue well "
        + "for a call that loses, and the rewriter is judging text too."}</p>
    <p class="note">${answer && answer.already_voted
      ? "You had already voted on this match, so the earlier vote stands and this one was not stored."
      : "Vote recorded."}
      ${answer && answer.windows != null
        ? `Pooled over ${plural(answer.windows, "paired window")}` +
          (answer.quiet ? `, of which ${answer.quiet} never moved a tick.` : ".")
        : ""}
      <a href="${raw(href.votes())}">How often the crowd and the arithmetic disagree</a>.</p>
  </div>`);
}
