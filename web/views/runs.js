/* Every generation, newest first, and the one chart the site exists to show. */

import { qAll, qs } from "../data.js";
import { href } from "../routes.js";
import {
  html, raw, stat, pill, n3, usd, dir, empty, day, plural,
} from "../dom.js";
import { generationSkill, sparkline } from "../charts.js";
import { groupBy, recompute, metricGap, windowSkill } from "../stats.js";

const RUN_COLUMNS = "id,created,parent,accepted,reasons,incumbent_fp,candidate_fp," +
                    "baseline,candidate,decision,search,llm,split";

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

  // Every held-out window of every generation, both sides, narrow. It pays for
  // three things at once: the sparklines, the pooled levels recomputed on one
  // metric, and the count of windows that never moved.
  let windows = [];
  if (runs.length <= 24) {
    windows = await qAll(
      `rollouts?select=run_id,side,skill,err,naive_error,unmeasurable` +
      `&split=eq.holdout&run_id=${qs.inList(runs.map(r => r.id))}`,
      { signal, max: 24_000 });
  }
  const level = recompute(windows);
  const byRunSide = groupBy(windows, r => `${r.run_id}|${r.side}`);

  const kept = runs.filter(r => r.accepted).length;
  const spent = runs.reduce((a, r) => a + (r.llm?.spent_usd || 0), 0);
  const tried = runs.reduce((a, r) => a + (r.search?.candidates || 0), 0);
  const exhausted = runs.filter(r => r.llm?.exhausted);
  const ordered = [...runs].reverse();           // oldest first, as time reads

  const points = ordered.map(r => {
    const mine = level.get(r.id) || {};
    const hold = r.decision?.holdout || {};
    return {
      id: r.id, accepted: !!r.accepted,
      inc: mine.baseline?.skill ?? r.baseline?.holdout?.statistic ?? null,
      cand: mine.candidate?.skill ?? r.candidate?.holdout?.statistic ?? null,
      diff: hold.diff ?? null, low: hold.low ?? null, high: hold.high ?? null,
      detectable: hold.detectable ?? null, underpowered: !!hold.underpowered,
    };
  });
  const best = points.reduce((a, p) => Math.max(a, p.cand ?? -Infinity, p.inc ?? -Infinity), -Infinity);
  const drifted = runs.filter(r => {
    const g = metricGap(r, "candidate", level.get(r.id));
    return g && g.differs;
  });
  const underpowered = runs.filter(r => r.decision?.holdout?.underpowered);

  const body = html`
    <div class="cards">
      ${stat({ value: runs.length, label: "generations",
               note: kept ? `${kept} promoted` : "none promoted" })}
      ${stat({ value: Number.isFinite(best) ? n3(best) : "—",
               tone: Number.isFinite(best) ? dir(best) : "",
               label: "best held-out skill",
               note: Number.isFinite(best) ? "on one metric, recomputed"
                                           : "no generation has published one" })}
      ${stat({ value: tried, label: "candidates tried", note: "across every search" })}
      ${stat({ value: usd(spent), label: "spent on models",
               note: `${usd(spent / runs.length)} a generation` })}
    </div>

    ${exhausted.length ? html`<section class="panel warnband"><div class="panel-b">
      <p class="eyebrow warn">the money ran out</p>
      <p class="prose">${plural(exhausted.length, "generation")} stopped because the budget was
      spent, not because the search was finished
      (${exhausted.map(r => `${r.id} at ${usd(r.llm.spent_usd)}` +
          (r.llm.budget_usd ? ` of ${usd(r.llm.budget_usd)}` : "")).join("; ")}).
      A generation that ran out of money and one that ran to completion produce the same shaped
      record, and the difference is the whole meaning of the result.
      <a href="${raw(href.cost())}">What the loop costs</a>.</p>
    </div></section>` : ""}

    <section class="panel">
      <div class="panel-h">
        <h2>Did any rewrite beat its incumbent?</h2>
        <span class="pill">held out</span>
      </div>
      <div class="panel-b">
        <div class="legend">
          <span><i class="dot" style="background:var(--c-inc)"></i> incumbent</span>
          <span><i class="dot" style="background:var(--c-cand)"></i> candidate</span>
          <span><i class="wash"></i> 95% interval on the difference</span>
          ${underpowered.length ? html`<span><i style="background:var(--warn)"></i>
            what this test could resolve at all</span>` : ""}
        </div>
        <figure class="chart">
          <div id="gen-skill"></div>
          <figcaption>Pooled skill on the held-out matches, one dumbbell per generation. The band
            is the gate's paired cluster bootstrap on the <em>difference</em>, so it is anchored
            on the incumbent: a band that touches the silence line is a candidate the gate cannot
            promote.${underpowered.length ? html` The amber marks are the smallest gap this test
            could have resolved — where they sit outside the band, the rejection was a statement
            about the number of matches rather than about the rewrite.` : ""}</figcaption>
        </figure>
        <details class="table-view">
          <summary>The same numbers as a table</summary>
          <div class="scroll">
            <table>
              <caption>Held-out pooled skill by generation, recomputed on today's metric.
                The interval and the resolution are the gate's own, as recorded.</caption>
              <thead><tr>
                <th scope="col">generation</th><th scope="col" class="n">incumbent</th>
                <th scope="col" class="n">candidate</th><th scope="col" class="n">difference</th>
                <th scope="col" class="n">95% interval</th>
                <th scope="col" class="n">resolves</th><th scope="col">verdict</th>
              </tr></thead>
              <tbody>${points.map(p => html`<tr>
                <th scope="row" class="mono">${p.id}</th>
                <td class="n">${n3(p.inc)}</td>
                <td class="n">${n3(p.cand)}</td>
                <td class="n">${n3(p.diff)}</td>
                <td class="n">${p.low == null ? "—" : `${n3(p.low)} to ${n3(p.high)}`}</td>
                <td class="n">${p.detectable ? `±${p.detectable.toFixed(3)}` : "—"}</td>
                <td>${p.accepted ? "promoted" : "dropped"}</td>
              </tr>`)}</tbody>
            </table>
          </div>
        </details>
      </div>
    </section>

    ${drifted.length ? html`<section class="panel"><div class="panel-b prose">
      <p class="eyebrow">why these are not the numbers in the manifests</p>
      <p>${plural(drifted.length, "generation")} published a held-out figure that its own windows
      no longer produce: ${drifted.map(r => {
        const g = metricGap(r, "candidate", level.get(r.id));
        return `${r.id} published ${n3(g.published)} and recomputes to ${n3(g.here)}`;
      }).join("; ")}. Nothing was re-scored. The metric changed underneath them — the earliest
      runs divided by an unfloored benchmark, and one of them was scored while a per-window skill
      of <code>1 - error/benchmark</code> still handed a full point to a harness that said nothing
      on a market that did not move.</p>
      <p class="note">Each of those was the gate's statistic on the day it ran, so none of them is
      wrong. They are three different questions, and plotting them on one axis would make a change
      of metric look like a result. The chart above recomputes every generation from its own
      windows on today's formula; each generation's page shows both.</p>
    </div></section>` : ""}

    <section class="panel">
      <div class="panel-h"><h2>Every generation</h2><span class="pill">newest first</span></div>
      <ul class="rows">${runs.map(r => row(r, byRunSide.get(`${r.id}|candidate`) || [], level.get(r.id)))}</ul>
    </section>

    <p class="sub">Skill is measured against no change. Predicting the price stays put is free
    and nearly always nearly right, so zero is worth exactly as much as silence — and a
    promotion needs a held-out interval clear of it.</p>`;

  return {
    title: "Generations",
    heading: html`${plural(runs.length, "generation")}, and whether any of them helped`,
    lead: html`Each generation rewrites the harness and scores the rewrite against the harness it
      came from, on matches the optimizer never saw. The rewrite is promoted only if a paired
      cluster bootstrap over those matches puts the difference clear of zero.
      ${kept === 0 ? "None has been." : `${plural(kept, "rewrite")} survived that.`}`,
    body,
    ready: root => generationSkill(root.querySelector("#gen-skill"), points),
  };
}

function row(r, windows, level) {
  const hold = r.decision?.holdout || {};
  const skills = windows.map(windowSkill).filter(v => v != null);
  const inc = level?.baseline?.skill ?? r.baseline?.holdout?.statistic;
  const cand = level?.candidate?.skill ?? r.candidate?.holdout?.statistic;
  return html`<li>
    <a class="row" href="${raw(href.run(r.id))}" data-ok="${r.accepted ? 1 : 0}">
      <span class="mark" aria-hidden="true"></span>
      <span>
        <span class="name">${r.id}
          ${pill(r.accepted ? "promoted" : "dropped", r.accepted ? "up" : "down")}
          ${hold.underpowered ? pill("underpowered", "warn") : ""}
          ${hold.usable === false ? pill("no interval", "warn") : ""}
          ${r.llm?.exhausted ? pill("budget spent", "warn") : ""}
          ${r.candidate_fp === r.incumbent_fp ? pill("unchanged") : ""}</span>
        <span class="why">${(r.reasons || [])[0] || ""}</span>
        <span class="why crumb">${day(r.created)}</span>
      </span>
      <span class="spark">${raw(sparkline(skills, {
        label: `${skills.length} held-out windows, worst to best`,
      }))}</span>
      <span class="right">
        <span class="delta ${dir(hold.diff)}">${n3(hold.diff)}</span>
        <span class="crumb mono">${n3(inc)} → ${n3(cand)}</span>
      </span>
    </a>
  </li>`;
}
