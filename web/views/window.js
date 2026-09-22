/* One window: one ticker at one instant, which is the unit that gets scored.
 *
 * The forecast, what it was worth, and — when the generation was scored with
 * `--trace` — every step the harness took to produce it.
 */

import { q, rpc, ApiError } from "../data.js";
import { href } from "../routes.js";
import {
  html, raw, pill, n3, usd, dir, empty, stamp, plural,
} from "../dom.js";
import { priceTrack } from "../charts.js";
import { windowSkill } from "../stats.js";
import { addActions } from "../actions.js";
import { VOTER } from "../voter.js";
import { topicOf, forecastOf, fmtMove, fmtErr, fmtWidth, wordsOf } from "../topics.js";

export async function windowView({ params, signal }) {
  const rid = String(params.id || "");
  if (!/^\d+$/.test(rid))
    return { title: "Window", heading: "No such window",
             body: empty("A window id is a number; this address does not carry one.") };

  // All keyed on the same id and none needs another's answer. The feedback
  // select is tiny and its failure must not cost the page: flags are a
  // decoration on the window, not the window.
  const [[r], traces, flags] = await Promise.all([
    q(`rollouts?id=eq.${encodeURIComponent(rid)}&select=*`, { signal }),
    q(`traces?rollout_id=eq.${encodeURIComponent(rid)}&select=spans`, { signal }),
    q(`trace_feedback?rollout_id=eq.${encodeURIComponent(rid)}&select=verdict,voter`,
      { signal }).catch(() => []),
  ]);
  const tally = { good: 0, bad: 0, unsure: 0 };
  let mine = null;
  for (const f of flags) {
    if (f.verdict in tally) tally[f.verdict] += 1;
    if (f.voter === VOTER) mine = f.verdict;
  }
  if (!r) return { title: "Window", heading: "No such window",
                   body: empty(html`Nothing published with id <code>${rid}</code>.`) };

  const spans = (traces[0] && traces[0].spans) || [];
  // The row's own topic, when the column was published; a row without one is
  // Kalshi. Keyed on the row rather than the route so a bookmark to a Kalshi
  // window reads as Kalshi whatever topic the reader was last on.
  const t = topicOf(r);
  const w = wordsOf(t.id);
  // Recomputed from this window's own errors, for the same reason the tables
  // are: the stored column carries whatever the metric said that week.
  const skill = windowSkill(r);
  const restated = r.skill != null && Math.abs(r.skill - skill) > 0.002;
  const o = r.output || {};
  const { delta, width } = forecastOf(o, t.id);
  const covered = r.realised != null && r.predicted != null && r.half_width != null
    && Math.abs(r.realised - r.predicted) <= r.half_width;

  const body = html`
    <div class="grid-2 split">
      <section class="panel">
        <div class="panel-h">
          <h2 class="mono ticker">${r.ticker}</h2>
          ${pill(stamp(r.at))}
          ${pill(r.split, r.split === "holdout" ? "brand" : "")}
          ${pill(r.side === "baseline" ? "incumbent" : "candidate")}
        </div>
        <div class="panel-b">
          ${raw(priceTrack(r, t.id))}
          <div class="fc">
            <div class="box"><div class="k">it said</div>
              <div class="big">${fmtMove(delta, t.id)}</div>
              <p>quoting ${fmtWidth(width, t.id)}${o.confidence != null
                ? `, confidence ${o.confidence}` : ""}${o.reconstructed
                ? " · rebuilt from the score, so no reasoning was kept" : ""}.
                ${r.realised != null && r.half_width
                  ? covered ? "The price printed inside that quote."
                            : "The price printed outside that quote." : ""}</p></div>
            <div class="box"><div class="k">it was worth</div>
              <div class="big ${dir(skill)}">${n3(skill)}</div>
              <p>against no change${r.unmeasurable
                ? " — but the market did not move, so there was no error to remove" : ""}.
                ${r.err != null && r.naive_error != null
                  ? `Missed by ${fmtErr(r.err, t.id)} where saying nothing would have missed by
                     ${fmtErr(r.naive_error, t.id)}.` : ""}
                ${restated ? `The scoreboard of the day recorded ${n3(r.skill)} here, under a
                   metric that has since changed.` : ""}</p></div>
          </div>
          ${r.cost_usd != null ? html`<p class="note">This window cost ${usd(r.cost_usd)}.</p>` : ""}
          ${r.ok === false ? html`<div class="box"><div class="k">the run failed</div>
            <p>${r.error_text || "no reason recorded"} — a failed run is scored as silence,
            never dropped, because dropping it would reward failing on hard windows.</p></div>` : ""}
        </div>
      </section>

      <section class="panel">
        <div class="panel-h"><h2>Its reasoning</h2></div>
        <div class="panel-b">
          ${o.driver ? html`<div class="box"><div class="k">driver</div><p>${o.driver}</p></div>` : ""}
          ${o.falsifier ? html`<div class="box"><div class="k">what would prove it wrong</div>
            <p>${o.falsifier}</p></div>` : ""}
          ${!o.driver && !o.falsifier ? html`<p class="note">This generation was scored without
            <code>--trace</code>, so only the numbers were kept.</p>` : ""}
          ${r.feedback ? html`<div class="box"><div class="k">how it was scored</div>
            <p>${r.feedback}</p></div>` : ""}
          ${o.driver || o.falsifier ? html`<div class="flag" id="flag-box">
            <p class="note">Was this reasoning sound? Your read becomes training signal.</p>
            <div class="btn-row">
              ${["good", "bad", "unsure"].map(v => html`<button class="btn btn-sm" type="button"
                data-action="flag" data-verdict="${v}"
                ${raw(mine === v ? 'aria-pressed="true"' : 'aria-pressed="false"')}>${v}</button>`)}
            </div>
            <p class="note" id="flag-tally">${tallyText(tally)}</p>
          </div>` : ""}
        </div>
      </section>
    </div>

    <section class="panel">
      <div class="panel-h"><h2>What it did</h2>${pill(plural(spans.length, "step"))}</div>
      <div class="panel-b">
        ${spans.length ? spans.map(s => html`
          <details class="span" data-k="${s.kind}" ${raw(s.kind === "llm" ? "open" : "")}>
            <summary>
              ${pill(s.kind, s.kind === "llm" ? "brand" : "")}
              <span class="nm">${s.name}</span>
              ${s.error ? pill("error", "down") : ""}
              <span class="meta">${s.duration_s != null ? s.duration_s + "s" : ""}${
                s.cost_usd ? " · $" + s.cost_usd.toFixed(5) : ""}${s.cached ? " · cached" : ""}</span>
            </summary>
            ${s.input != null ? html`<pre>${s.input}</pre>` : ""}
            ${s.output != null ? html`<pre>${s.output}</pre>` : ""}
            ${s.error ? html`<pre class="down">${s.error}</pre>` : ""}
          </details>`)
          : html`<p class="note">No trace was kept for this generation. Traces are written only
            when a run is scored with <code>--trace</code>; they are two orders larger than the
            row they hang off.</p>`}
      </div>
    </section>`;

  return {
    title: r.ticker,
    heading: html`${r.ticker} <span class="crumb">· ${stamp(r.at)}</span>`,
    lead: html`One moment in one ${w.group}: the forecaster was asked where this price would be
      five minutes later. Here is what it said, why, and what actually happened.`,
    crumbs: [["generations", href.runs()], [r.run_id, href.run(r.run_id, r.side)], ["window"]],
    body,
    ready: root => addActions({
      flag: el => castFlag(el, root, Number(rid)),
    }),
  };
}

function tallyText(t) {
  const total = t.good + t.bad + t.unsure;
  return total
    ? `${total} so far — ${t.good} good · ${t.bad} bad · ${t.unsure} unsure`
    : "";
}

/** One verdict per browser per window; a change of mind replaces it. */
async function castFlag(el, root, rolloutId) {
  const box = root.querySelector("#flag-box");
  const buttons = [...box.querySelectorAll("button")];
  buttons.forEach(b => { b.disabled = true; });
  try {
    const out = await rpc("flag_trace", {
      rollout_id: rolloutId, verdict: el.dataset.verdict, voter: VOTER,
    });
    for (const b of buttons)
      b.setAttribute("aria-pressed", String(b.dataset.verdict === el.dataset.verdict));
    const tally = { good: 0, bad: 0, unsure: 0, ...(out && out.tally || {}) };
    box.querySelector("#flag-tally").textContent = tallyText(tally);
  } catch (err) {
    // The RPC's messages are written for humans; show them as sent.
    box.querySelector("#flag-tally").textContent = err instanceof ApiError && err.status === 404
      ? "flagging is not installed on this database yet"
      : (err.detail && safeMessage(err.detail)) || err.message;
  } finally {
    buttons.forEach(b => { b.disabled = false; });
  }
}

/** PostgREST wraps a raised exception's text in JSON; unwrap it, nothing else. */
function safeMessage(detail) {
  try { return JSON.parse(detail).message || null; } catch (e) { return null; }
}
