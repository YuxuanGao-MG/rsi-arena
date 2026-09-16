/* One generation: what it scored on the matches it was held out on, where it
 * lost, where it won, and what its forecasts looked like against the prices.
 *
 * Three queries, all of them held-out only. The page this replaces filtered on
 * `run_id` and `side` but not `split`, so the cards said "held-out skill" and
 * the tables underneath were train and held-out pooled together. It also took
 * the best six rows of a 500-row ascending slice and labelled them "Where it
 * won", which on any run with more than 500 windows is the best of the worst.
 */

import { q, qAll } from "../data.js";
import { href } from "../routes.js";
import {
  html, raw, stat, pill, n3, usd, price, dir, empty, clock, plural, holdout, skillBar,
} from "../dom.js";
import { predictedVsRealised, skillHistogram } from "../charts.js";
import { pooled, pooledOnMoves } from "../stats.js";

const NARROW = "id,fixture,ticker,at,mid_now,realised,predicted,half_width," +
               "err,naive_error,skill,echoed,unmeasurable,cost_usd,ok";
const WITH_OUTPUT = NARROW + ",output";

export async function runView({ params, query, signal }) {
  const id = params.id;
  const side = query.side === "baseline" ? "baseline" : "candidate";
  if (!id) return { title: "Generation", heading: "No generation named", body: empty("Pick one from the list.") };

  const base = `rollouts?run_id=eq.${encodeURIComponent(id)}&side=eq.${side}&split=eq.holdout`;
  // Serial awaits here used to cost a round trip each; the run record and the
  // windows do not depend on one another.
  const [[run], all, worst, best] = await Promise.all([
    q(`runs?id=eq.${encodeURIComponent(id)}&select=*`, { signal }),
    qAll(`${base}&select=${NARROW}&order=skill.asc`, { signal, max: 8000 }),
    q(`${base}&select=${WITH_OUTPUT}&order=skill.asc&limit=12`, { signal }),
    q(`${base}&select=${WITH_OUTPUT}&order=skill.desc&limit=6`, { signal }),
  ]);

  if (!run) {
    return { title: "Generation", heading: "No such generation",
             body: empty(html`Nothing published under <code>${id}</code>.`) };
  }
  if (!all.length) {
    return {
      title: id, heading: id,
      crumbs: [["generations", href.runs()], [id]],
      body: empty(html`This generation has no held-out windows for the ${side}. It may have
        been published before the split was recorded, or scored on train only.`),
    };
  }

  const published = holdout(run, side) || {};
  const mine = pooled(all);
  const moved = pooledOnMoves(all);
  const cost = all.reduce((a, r) => a + (r.cost_usd || 0), 0);
  const drift = published.statistic != null && mine.skill != null
    && Math.abs(published.statistic - mine.skill) > 0.002;

  const body = html`
    <div class="cards">
      ${stat({ value: n3(published.statistic ?? mine.skill),
               tone: dir(published.statistic ?? mine.skill),
               label: "held-out pooled skill",
               note: `${plural(published.instances ?? all.length, "window")} · zero is silence` })}
      ${stat({ value: n3(moved.skill), tone: dir(moved.skill),
               label: "on the windows that moved",
               note: `${moved.instances} of ${all.length} moved a tick or more` })}
      ${stat({ value: published.echoed ?? all.filter(r => r.echoed).length,
               label: "echoed the mid", note: "worth exactly zero, by construction" })}
      ${stat({ value: usd(published.cost_usd ?? cost), label: "cost",
               note: `${usd((published.cost_usd ?? cost) / all.length)} a window` })}
    </div>

    ${drift ? html`<div class="panel"><div class="panel-b note">
      The published scoreboard says ${n3(published.statistic)} and the windows in the database
      add up to ${n3(mine.skill)}. One of the two is stale — most likely the run was re-scored
      without being re-published.</div></div>` : ""}

    <section class="panel">
      <div class="panel-h">
        <h2>${run.accepted ? "Promoted" : "Dropped"}</h2>
        ${pill(run.accepted ? "kept" : "rejected", run.accepted ? "up" : "down")}
        <span class="spacer"></span>
        <a href="${raw(href.compare(id))}">read both harnesses side by side</a>
      </div>
      <div class="panel-b">
        <div class="prose">${(run.reasons || []).map(x => html`<p>${x}</p>`)}
          ${(run.reasons || []).length ? "" : html`<p class="note">No reasons were recorded.</p>`}</div>
        <nav class="btn-row" aria-label="Which harness">
          <a class="btn" href="${raw(href.run(id, "baseline"))}"
             ${raw(side === "baseline" ? 'aria-current="page"' : "")}>incumbent</a>
          <a class="btn" href="${raw(href.run(id, "candidate"))}"
             ${raw(side === "candidate" ? 'aria-current="page"' : "")}>candidate</a>
        </nav>
      </div>
    </section>

    <div class="grid-2 even">
      <section class="panel">
        <div class="panel-h"><h2>What it said against what printed</h2></div>
        <div class="panel-b">
          <div class="legend">
            <span><i class="dot" style="background:var(--c-cand)"></i> beat no change</span>
            <span><i class="dot" style="background:transparent;border:1.5px solid var(--down)"></i>
              lost to no change</span>
            <span><i style="background:var(--c-cand);opacity:.35;height:12px;width:12px;border-radius:3px"></i>
              the wedge where a forecast removes error</span>
          </div>
          <figure class="chart">
            <div id="scatter"></div>
            <figcaption>Both axes are the five-minute move in cents. On the diagonal the
              harness called the move exactly; on the horizontal it said nothing, which is what
              the benchmark says everywhere. Inside the wedge between them it removed error;
              outside it added some.</figcaption>
          </figure>
        </div>
      </section>

      <section class="panel">
        <div class="panel-h"><h2>Where the skill actually sits</h2></div>
        <div class="panel-b">
          <figure class="chart">
            <div id="hist"></div>
            <figcaption>${mine.quiet
              ? html`${plural(mine.quiet, "window")} never moved a tick. They are still scored —
                  the benchmark's error is floored at one tick so a dead market is scored rather
                  than excused — but the best a forecast can do on one is zero.`
              : html`Every window here moved at least a tick.`}</figcaption>
          </figure>
        </div>
      </section>
    </div>

    <section class="panel">
      <div class="panel-h"><h2>Where it lost</h2>
        <span class="pill">worst first — the only thing a rewrite can aim at</span></div>
      <div class="scroll">${windowTable(worst)}</div>
    </section>

    <section class="panel">
      <div class="panel-h"><h2>Where it won</h2>
        <span class="pill">best of all ${plural(all.length, "held-out window")}</span></div>
      <div class="scroll">${windowTable(best)}</div>
    </section>`;

  return {
    title: id,
    heading: html`${id} <span class="crumb">· ${side === "baseline" ? "incumbent" : "candidate"}</span>`,
    lead: html`Held out on ${plural(new Set(all.map(r => r.fixture)).size, "match")},
      ${plural(all.length, "window")} scored. Everything on this page is held-out only.`,
    crumbs: [["generations", href.runs()], [id]],
    body,
    ready: root => {
      predictedVsRealised(root.querySelector("#scatter"), all);
      skillHistogram(root.querySelector("#hist"), all);
    },
  };
}

function windowTable(rows) {
  if (!rows.length) return empty("nothing scored");
  return html`<table>
    <thead><tr>
      <th scope="col">window</th><th scope="col" class="n">mid</th>
      <th scope="col" class="n">said</th><th scope="col" class="n">printed</th>
      <th scope="col">skill</th><th scope="col">why it said so</th>
    </tr></thead>
    <tbody>${rows.map(r => html`<tr class="tap">
      <th scope="row">
        <a class="cell-link" href="${raw(href.window(r.id))}">
          <span class="mono ticker">${r.ticker}</span>
          <span class="crumb mono">${clock(r.at)}
            ${r.echoed ? pill("echo") : ""}${r.unmeasurable ? pill("quiet", "warn") : ""}</span>
        </a>
      </th>
      <td class="n">${price(r.mid_now)}</td>
      <td class="n">${price(r.predicted)}</td>
      <td class="n">${price(r.realised)}</td>
      <td class="bar-cell">${skillBar(r.skill)}
        <span class="crumb mono ${dir(r.skill)}">${n3(r.skill)}</span></td>
      <td class="why-cell">${((r.output && r.output.driver) || "").slice(0, 160)
        || html`<span class="crumb">not recorded</span>`}</td>
    </tr>`)}</tbody>
  </table>`;
}
