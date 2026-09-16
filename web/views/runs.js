/* Every generation, newest first, and the one chart the site exists to show. */

import { qAll, qs } from "../data.js";
import { href } from "../routes.js";
import {
  html, raw, stat, pill, n3, usd, dir, empty, day,
} from "../dom.js";
import { generationSkill, sparkline } from "../charts.js";
import { bestHoldout, groupBy } from "../stats.js";

const RUN_COLUMNS = "id,created,parent,accepted,reasons,incumbent_fp,candidate_fp," +
                    "baseline,candidate,decision,search,llm";

export async function runsView({ signal }) {
  const runs = await qAll(`runs?select=${RUN_COLUMNS}&order=created.desc`,
                          { signal, pageSize: 200, max: 2000 });
  if (!runs.length) {
    return {
      title: "Generations", heading: "No generations published yet",
      body: empty(html`Nothing has been pushed to the database yet. A generation lands here
        when someone runs <code>python scripts/publish_runs.py runs/&lt;name&gt;</code>.`),
    };
  }

  // Sparklines want per-window skill, which is a second query — narrow, held-out
  // only, and paginated. It runs beside nothing else, so it is awaited alone.
  let byRun = new Map();
  if (runs.length <= 24) {
    const ids = runs.map(r => r.id);
    const rows = await qAll(
      `rollouts?select=run_id,skill&side=eq.candidate&split=eq.holdout` +
      `&run_id=${qs.inList(ids)}`, { signal, max: 12_000 });
    byRun = groupBy(rows, "run_id");
  }

  const kept = runs.filter(r => r.accepted).length;
  const best = bestHoldout(runs);
  const spent = runs.reduce((a, r) => a + (r.llm?.spent_usd || 0), 0);
  const tried = runs.reduce((a, r) => a + (r.search?.candidates || 0), 0);
  const ordered = [...runs].reverse();           // oldest first, as time reads

  const body = html`
    <div class="cards">
      ${stat({ value: runs.length, label: "generations",
               note: kept ? `${kept} promoted` : "none promoted" })}
      ${stat({ value: best == null ? "—" : n3(best), tone: best == null ? "" : dir(best),
               label: "best held-out skill",
               note: best == null ? "no generation has published one" : "zero is silence" })}
      ${stat({ value: tried, label: "candidates tried", note: "across every search" })}
      ${stat({ value: usd(spent), label: "spent on models",
               note: `${usd(spent / runs.length)} a generation` })}
    </div>

    <section class="panel">
      <div class="panel-h">
        <h2>Did any rewrite beat its incumbent?</h2>
        <span class="pill">held out</span>
      </div>
      <div class="panel-b">
        <div class="legend">
          <span><i class="dot" style="background:var(--c-inc)"></i> incumbent</span>
          <span><i class="dot" style="background:var(--c-cand)"></i> candidate</span>
          <span><i style="background:var(--c-cand);opacity:.35;height:12px;width:12px;border-radius:3px"></i>
            95% interval on the difference</span>
        </div>
        <figure class="chart">
          <div id="gen-skill"></div>
          <figcaption>Pooled skill on the held-out matches, one dumbbell per generation.
            The band is the gate's paired cluster bootstrap on the <em>difference</em>, so it is
            anchored on the incumbent: a band that touches the silence line is a candidate the
            gate cannot promote.</figcaption>
        </figure>
        <details class="table-view">
          <summary>The same numbers as a table</summary>
          <div class="scroll">
            <table>
              <caption>Held-out pooled skill by generation.</caption>
              <thead><tr>
                <th scope="col">generation</th><th scope="col" class="n">incumbent</th>
                <th scope="col" class="n">candidate</th><th scope="col" class="n">difference</th>
                <th scope="col" class="n">95% interval</th><th scope="col">verdict</th>
              </tr></thead>
              <tbody>${ordered.map(r => html`<tr>
                <th scope="row" class="mono">${r.id}</th>
                <td class="n">${n3(r.baseline?.holdout?.statistic)}</td>
                <td class="n">${n3(r.candidate?.holdout?.statistic)}</td>
                <td class="n">${n3(r.decision?.holdout?.diff)}</td>
                <td class="n">${r.decision?.holdout?.low == null ? "—"
                  : `${n3(r.decision.holdout.low)} to ${n3(r.decision.holdout.high)}`}</td>
                <td>${r.accepted ? "promoted" : "dropped"}</td>
              </tr>`)}</tbody>
            </table>
          </div>
        </details>
      </div>
    </section>

    <section class="panel">
      <div class="panel-h"><h2>Every generation</h2><span class="pill">newest first</span></div>
      <ul class="rows">${runs.map(r => row(r, byRun.get(r.id) || []))}</ul>
    </section>

    <p class="sub">Skill is measured against no change. Predicting the price stays put is free
    and nearly always nearly right, so zero is worth exactly as much as silence — and a
    promotion needs a held-out interval clear of it.</p>`;

  return {
    title: "Generations",
    heading: "Three generations, and whether any of them helped",
    lead: html`Each generation rewrites the harness and scores the rewrite against the harness
      it came from, on matches the optimizer never saw. The rewrite is promoted only if a paired
      cluster bootstrap over those matches puts the difference clear of zero.`,
    body,
    ready: root => generationSkill(root.querySelector("#gen-skill"), ordered),
  };
}

function row(r, windows) {
  const d = r.decision?.holdout?.diff;
  const unusable = r.decision?.holdout?.usable === false;
  const skills = windows.map(w => w.skill).filter(v => v != null);
  return html`<li>
    <a class="row" href="${raw(href.run(r.id))}" data-ok="${r.accepted ? 1 : 0}">
      <span class="mark" aria-hidden="true"></span>
      <span>
        <span class="name">${r.id}
          ${pill(r.accepted ? "promoted" : "dropped", r.accepted ? "up" : "down")}
          ${unusable ? pill("no interval", "warn") : ""}
          ${r.candidate_fp === r.incumbent_fp ? pill("unchanged") : ""}</span>
        <span class="why">${(r.reasons || [])[0] || ""}</span>
        <span class="why crumb">${day(r.created)}</span>
      </span>
      <span class="spark">${raw(sparkline(skills, {
        label: `${skills.length} held-out windows, worst to best`,
      }))}</span>
      <span class="right">
        <span class="delta ${dir(d)}">${n3(d)}</span>
        <span class="crumb mono">${n3(r.baseline?.holdout?.statistic)} →
          ${n3(r.candidate?.holdout?.statistic)}</span>
      </span>
    </a>
  </li>`;
}
