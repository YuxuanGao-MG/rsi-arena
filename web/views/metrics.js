/* The record: every generation, with the banners and the drift panel.
 *
 * This is the old front page's table half, one level down. The chart moved to
 * the overview; the sub-nav below fans out to the deeper sections — archive,
 * cost, votes, live — which keep their own routes because a deep link is
 * someone's bookmark.
 */

import { href } from "../routes.js";
import {
  html, raw, stat, pill, n3, usd, dir, empty, day, plural,
} from "../dom.js";
import { sparkline } from "../charts.js";
import { windowSkill } from "../stats.js";
import { rewriteLabel, fateOf, genName } from "../labels.js";
import { loadGenerations } from "../generations.js";

export async function metricsView({ signal, topic }) {
  const g = await loadGenerations({ signal, topic });
  if (!g.runs.length) {
    return {
      title: "Metrics", heading: "No generations published yet",
      body: empty(html`Nothing has been pushed to the database yet. A generation lands here
        when someone runs <code>python scripts/publish_runs.py runs/&lt;name&gt;</code>.`),
    };
  }

  const kept = g.runs.filter(r => r.accepted).length;
  const spent = g.runs.reduce((a, r) => a + (r.llm?.spent_usd || 0), 0);
  const tried = g.runs.reduce((a, r) => a + (r.search?.candidates || 0), 0);

  const body = html`
    <div class="cards">
      ${stat({ value: g.runs.length, label: "generations",
               note: kept ? `${kept} promoted` : "none promoted" })}
      ${stat({ value: g.best == null ? "—" : n3(g.best),
               tone: g.best == null ? "" : dir(g.best),
               label: "best held-out skill",
               note: g.best == null ? "no generation has published one"
                 : g.measured.length < g.runs.length
                   ? `over the ${plural(g.measured.length, "generation")} that measured anything`
                   : "on one metric, recomputed" })}
      ${stat({ value: tried, label: "candidates tried", note: "across every search" })}
      ${stat({ value: usd(spent), label: "spent on models",
               note: `${usd(spent / g.runs.length)} a generation` })}
    </div>

    ${g.exhausted.length || g.incomplete.length ? html`<section class="panel warnband">
      <div class="panel-b prose">
      <p>${[...g.exhausted.map(r => `${genName(r.id, g.runs)} ran out of money`),
            ...g.incomplete.map(r => `${genName(r.id, g.runs)} crashed`)].join("; ")} — those runs
      measured nothing, so they appear as gaps, not results.</p>
      <details class="more"><summary>why a gap and not a zero</summary>
        <p>A run that stopped mid-way records refusals — forecasts that never happened, stored
        as "no change". Scored, they would look like a harness that broke even. Excluded and
        labelled, they look like what they are.
        ${g.exhausted.map(r => html`${genName(r.id, g.runs)} spent ${usd(r.llm.spent_usd)}${
          r.llm.budget_usd ? html` of ${usd(r.llm.budget_usd)}` : ""}. `)}
        <a href="${raw(href.cost())}">What the loop costs</a>.</p>
      </details>
    </div></section>` : ""}

    ${g.drifted.length ? html`<section class="panel"><div class="panel-b prose">
      <p>Some early scores were computed under older versions of the metric, so this page
      recomputes every number on today's formula. Where the original differs, the generation's
      own page shows both.</p>
      <details class="more"><summary>which ones, and by how much</summary>
        <p>${g.drifted.map(d => `${d.run.id}'s ${d.side === "baseline" ? "incumbent" : "rewrite"}
          published ${n3(d.published)} and recomputes to ${n3(d.here)}`).join("; ")}.
        Nothing was re-scored — the formula changed underneath them, and plotting old and new on
        one axis would make a change of metric look like a result.</p>
      </details>
    </div></section>` : ""}

    <section class="panel">
      <div class="panel-h"><h2>Every generation</h2><span class="pill">newest first</span></div>
      <ul class="rows">${g.runs.map(r => row(r, g.byRunSide.get(`${r.id}|candidate`) || [],
                                            g.level.get(r.id), g.statusOf.get(r.id), g.runs))}</ul>
    </section>

    <p class="sub">Skill is measured against no change. Predicting the price stays put is free
    and nearly always nearly right, so zero is worth exactly as much as silence — and a
    promotion needs a held-out interval clear of it.</p>`;

  return {
    title: "Metrics",
    heading: html`Every generation, and how it did`,
    lead: html`One row per rewrite, newest first. Click any of them for the full story —
      the moments it lost, the moments it won, and what it said.
      ${kept === 0 ? "None has been promoted yet." : `${plural(kept, "rewrite")} survived.`}`,
    body,
  };
}

function row(r, windows, level, status, runs) {
  const hold = r.decision?.holdout || {};
  const skills = windows.map(windowSkill).filter(v => v != null);
  const inc = level?.baseline?.skill ?? r.baseline?.holdout?.statistic;
  const cand = level?.candidate?.skill ?? r.candidate?.holdout?.statistic;
  const verdict = status === "incomplete" ? pill("incomplete", "warn")
    : status === "exhausted" ? pill("ran out of money", "warn")
    : pill(r.accepted ? "promoted" : "dropped", r.accepted ? "up" : "down");
  return html`<li>
    <a class="row" href="${raw(href.run(r.id))}" data-ok="${r.accepted ? 1 : 0}">
      <span class="mark ${raw(status !== "complete" ? "warn-mark" : "")}" aria-hidden="true"></span>
      <span>
        <span class="name">${genName(r.id, runs)}
          ${verdict}
          ${status === "complete" && hold.underpowered ? pill("underpowered", "warn") : ""}
          ${status === "complete" && hold.usable === false ? pill("no interval", "warn") : ""}
          ${r.candidate_fp && r.candidate_fp === r.incumbent_fp ? pill("unchanged") : ""}</span>
        <span class="why">${(r.reasons || [])[0] || ""}</span>
        <span class="why crumb">${r.id} · ${day(r.created)}</span>
      </span>
      <span class="spark">${raw(sparkline(skills, {
        label: `${skills.length} held-out windows, worst to best`,
      }))}</span>
      <span class="right">
        ${status === "complete" ? html`
          <span class="delta ${dir(hold.diff)}">${n3(hold.diff)}</span>
          <span class="crumb mono">${n3(inc)} → ${n3(cand)}</span>`
        : html`<span class="crumb">no measurement</span>`}
      </span>
    </a>
  </li>`;
}
