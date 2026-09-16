/* Forecasts on matches being played now.
 *
 * Every other page here is replay: the harness is put back at an instant of a
 * finished match with its tools frozen, and the answer is already in the candle
 * history. This one is not. `scripts/collect_live.py` runs the harness on a
 * market that is open, keeps the trace, and comes back five minutes later to
 * see what printed — so it is the only place the arena meets a market whose end
 * it has not already read.
 *
 * Nothing here feeds the gate. A live forecast is evidence about the harness,
 * not a promotion.
 */

import { qAll, ApiError } from "../data.js";
import { href } from "../routes.js";
import {
  html, raw, stat, pill, n3, n2, usd, price, cents, dir, empty, stamp, clock, plural,
} from "../dom.js";
import { predictedVsRealised } from "../charts.js";
import { pooled, pooledOnMoves } from "../stats.js";

const COLUMNS = "id,at,league,game_id,ticker,harness,mid_now,realised,predicted,half_width," +
                "err,naive_error,skill,scored,unscored_because,ok,error_text,cost_usd,output";

export async function liveView({ signal }) {
  let rows;
  try {
    rows = await qAll(`live_forecasts?select=${COLUMNS}&order=at.desc`, { signal, max: 5000 });
  } catch (err) {
    // 404 is the honest answer while the migration has not been run: PostgREST
    // cannot see a view that does not exist, and that is a deployment state
    // rather than a failure to report as one.
    if (err instanceof ApiError && err.status === 404) return notInstalled();
    throw err;
  }
  if (!rows.length) return notCollected();

  const done = rows.filter(r => r.scored && r.skill != null);
  const pending = rows.filter(r => !r.scored && r.error_text == null);
  const failed = rows.filter(r => r.ok === false);
  const all = pooled(done);
  const moved = pooledOnMoves(done);
  const cost = rows.reduce((a, r) => a + (r.cost_usd || 0), 0);
  const leagues = [...new Set(rows.map(r => r.league).filter(Boolean))];
  const latest = rows[0];

  const body = html`
    <div class="cards">
      ${stat({ value: n3(all.skill), tone: dir(all.skill), label: "live pooled skill",
               note: `${plural(done.length, "scored forecast")} · zero is silence` })}
      ${stat({ value: n3(moved.skill), tone: dir(moved.skill), label: "on the ones that moved",
               note: `${moved.instances} of ${done.length} moved a tick or more` })}
      ${stat({ value: pending.length, label: "waiting on the horizon",
               note: "scored five minutes after the fact, never before" })}
      ${stat({ value: usd(cost), label: "spent live",
               note: `${usd(cost / rows.length)} a forecast` })}
    </div>

    <section class="panel"><div class="panel-b prose">
      <p>These are the same harness on markets that were open when it was asked. The five-minute
      horizon has to print before anything can be scored, so a forecast sits unscored until it
      does — and one that never gets a two-sided quote at the horizon stays unscored rather than
      being counted as a miss.</p>
      <p class="note">Last sweep ${stamp(latest.at)} · ${plural(leagues.length, "league")}
      ${leagues.length ? `(${leagues.join(", ")})` : ""}
      ${failed.length ? html`· ${plural(failed.length, "run")} failed outright` : ""}</p>
    </div></section>

    ${done.length >= 4 ? html`<section class="panel">
      <div class="panel-h"><h2>What it said against what printed</h2>
        <span class="pill">live</span></div>
      <div class="panel-b">
        <div class="legend">
          <span><i class="dot" style="background:var(--c-cand)"></i> beat no change</span>
          <span><i class="dot" style="background:transparent;border:1.5px solid var(--down)"></i>
            lost to no change</span>
        </div>
        <figure class="chart">
          <div id="live-scatter"></div>
          <figcaption>Both axes are the five-minute move in cents, as on a generation's page —
            the diagonal is the move called exactly, the horizontal is saying nothing.</figcaption>
        </figure>
      </div>
    </section>` : ""}

    <section class="panel">
      <div class="panel-h"><h2>Every live forecast</h2><span class="pill">newest first</span></div>
      <div class="scroll"><table>
        <caption>One row per contract per sweep. A row is scored once the horizon prints.</caption>
        <thead><tr>
          <th scope="col">when</th><th scope="col">market</th><th scope="col" class="n">mid</th>
          <th scope="col" class="n">said</th><th scope="col" class="n">printed</th>
          <th scope="col" class="n">skill</th><th scope="col">why it said so</th>
        </tr></thead>
        <tbody>${rows.slice(0, 300).map(r => html`<tr>
          <td class="crumb mono">${stamp(r.at)}</td>
          <th scope="row"><span class="mono ticker">${r.ticker}</span>
            <span class="crumb">${r.league || ""}${r.harness ? ` · ${r.harness}` : ""}</span></th>
          <td class="n">${price(r.mid_now)}</td>
          <td class="n">${r.output && r.output.delta_cents != null
            ? cents(r.output.delta_cents) : price(r.predicted)}</td>
          <td class="n">${r.realised == null
            ? html`<span class="crumb">pending</span>` : price(r.realised)}</td>
          <td class="n ${dir(r.skill)}">${r.skill == null
            ? html`<span class="crumb">—</span>` : n3(r.skill)}</td>
          <td class="why-cell">${r.ok === false
            ? html`<span class="down">${r.error_text || "the run failed"}</span>`
            : r.unscored_because
              ? html`<span class="crumb">${r.unscored_because}</span>`
              : ((r.output && r.output.driver) || "").slice(0, 160)
                || html`<span class="crumb">not recorded</span>`}</td>
        </tr>`)}</tbody>
      </table></div>
    </section>`;

  return {
    title: "Live",
    heading: "The arena against a market it has not read the end of",
    lead: html`Replay is honest about the past because the tools are frozen at the instant. This
      is the other test: the same harness on a match being played now, scored when the five-minute
      horizon prints.`,
    body,
    ready: root => {
      if (done.length >= 4) predictedVsRealised(root.querySelector("#live-scatter"), done);
    },
  };
}

function notInstalled() {
  return {
    title: "Live", heading: "The live table is not installed yet",
    body: html`<section class="panel"><div class="panel-b prose">
      <p>This page reads <code>public.rsi_live_forecasts</code>, and the database does not have
      it. It is created by <code>supabase/migrations/002_votes_rpc.sql</code>, which nobody has
      run against this project yet.</p>
      <p class="note">Until then there is nothing to show here, and a retry will not change
      that.</p>
    </div></section>`,
  };
}

function notCollected() {
  return {
    title: "Live", heading: "No live forecasts yet",
    lead: html`The table exists and is empty: <code>scripts/collect_live.py</code> has not
      published a sweep.`,
    body: empty(html`Live forecasts appear here once the collector has run against open
      markets and the five-minute horizon has printed.`),
  };
}
