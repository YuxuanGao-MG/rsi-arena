/* Every generation, newest first, and the one chart the site exists to show. */

import { qAll, qs } from "../data.js";
import { href } from "../routes.js";
import {
  html, raw, stat, pill, n3, usd, dir, empty, day, plural,
} from "../dom.js";
import { generationSkill, sparkline } from "../charts.js";
import { groupBy, recompute, metricGap, windowSkill, runStatus } from "../stats.js";

const RUN_COLUMNS = "id,created,parent,accepted,reasons,incumbent_fp,candidate_fp," +
                    "baseline,candidate,decision,search,llm,split";

/** What a broken record's slot on the chart says instead of a number. */
const GAP_LABEL = { exhausted: "ran out of money", incomplete: "crashed" };

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
  // metric, and the count of windows that never moved. `ok` and `scored` ride
  // along so a budget refusal is never pooled as a forecast.
  let windows = [];
  if (runs.length <= 24) {
    windows = await qAll(
      `rollouts?select=run_id,side,skill,err,naive_error,unmeasurable,ok,scored` +
      `&split=eq.holdout&run_id=${qs.inList(runs.map(r => r.id))}`,
      { signal, max: 24_000 });
  }
  const level = recompute(windows);
  const byRunSide = groupBy(windows, r => `${r.run_id}|${r.side}`);
  const statusOf = new Map(runs.map(r => [r.id, runStatus(r)]));

  const kept = runs.filter(r => r.accepted).length;
  const spent = runs.reduce((a, r) => a + (r.llm?.spent_usd || 0), 0);
  const tried = runs.reduce((a, r) => a + (r.search?.candidates || 0), 0);
  const exhausted = runs.filter(r => statusOf.get(r.id) === "exhausted");
  const incomplete = runs.filter(r => statusOf.get(r.id) === "incomplete");
  const ordered = [...runs].reverse();           // oldest first, as time reads

  const points = ordered.map(r => {
    const status = statusOf.get(r.id);
    if (status !== "complete") {
      // A run that ran out of money or crashed measured nothing on held-out.
      // Its manifest still carries zero-shaped scoreboards, and falling
      // through to them is how a generation whose candidate never answered a
      // window briefly posted "0.000" as the best number on this page.
      return { id: r.id, accepted: false, gap: GAP_LABEL[status] };
    }
    const mine = level.get(r.id) || {};
    const hold = r.decision?.holdout || {};
    return {
      id: r.id, accepted: !!r.accepted,
      inc: mine.baseline?.skill ?? r.baseline?.holdout?.statistic ?? null,
      cand: mine.candidate?.skill ?? r.candidate?.holdout?.statistic ?? null,
      diff: hold.diff ?? null,
      low: hold.usable === false ? null : hold.low ?? null,
      high: hold.usable === false ? null : hold.high ?? null,
      usable: hold.usable !== false,
      detectable: hold.detectable ?? null, underpowered: !!hold.underpowered,
    };
  });
  const measured = points.filter(p => !p.gap);
  const best = measured.reduce((a, p) => Math.max(a, p.cand ?? -Infinity, p.inc ?? -Infinity), -Infinity);
  // The metric drifted under both sides — the loudest case is an *incumbent*
  // (+0.043 published, -0.011 recomputed) — so both are checked.
  const drifted = runs.flatMap(r => statusOf.get(r.id) !== "complete" ? [] :
    ["baseline", "candidate"].flatMap(side => {
      const g = metricGap(r, side, level.get(r.id));
      return g && g.differs ? [{ run: r, ...g }] : [];
    }));
  const underpowered = runs.filter(r => r.decision?.holdout?.underpowered
                                        && statusOf.get(r.id) === "complete");

  const body = html`
    <section class="panel"><div class="panel-b prose">
      <p>A <strong>harness</strong> is an LLM wired to market-data tools by a JSON plan. It is
      dropped into a moment of a Kalshi soccer match and asked where the contract's price will be
      five minutes later. Each generation, another LLM rewrites the harness from traces of the
      windows it lost, and the rewrite is promoted only if it beats the harness it came from on
      <strong>held-out</strong> matches — matches neither the rewrite nor its optimizer ever
      saw. <strong>Skill</strong> is the fraction of the no-change baseline's error a forecast
      removed: predicting "no change" is free and nearly always nearly right, so saying nothing
      scores exactly zero — <strong>silence</strong> — and a promotion needs an interval clear
      of it.</p>
      <p>${plural(runs.length, "generation")} in, nothing has been promoted. The pages here show
      exactly why — including that the early gate was judging effects it was too small to see.</p>
      <details class="glossary">
        <summary>The rest of the vocabulary</summary>
        <dl>
          <dt>window</dt><dd>One contract at one instant — the unit that gets scored.</dd>
          <dt>fixture</dt><dd>The match a window belongs to. Splits respect fixtures, never
            windows: fifty windows on one match are fifty correlated observations of one game.</dd>
          <dt>pooled skill</dt><dd>Sum the error every forecast removed, sum the baseline's
            error, then divide. Not the mean of per-window skill — a dead market and a wild one
            carry very different weight, and the gate reads the pooled number.</dd>
          <dt>echoed</dt><dd>The forecast repeated the current mid. Worth exactly zero, by
            construction.</dd>
          <dt>underpowered</dt><dd>The gate's own interval could not have resolved a gain of the
            size it was judging, so the rejection is about the sample, not the candidate.</dd>
          <dt>probe / cascade</dt><dd>A cheap first pass on a few train matches; a candidate far
            behind the incumbent there is dropped without paying for the full evaluation.</dd>
          <dt>frontier</dt><dd>Candidates that are the best anyone has found on some window and
            beaten everywhere by nothing — the pool the next generation starts from.</dd>
        </dl>
      </details>
    </div></section>

    <div class="cards">
      ${stat({ value: runs.length, label: "generations",
               note: kept ? `${kept} promoted` : "none promoted" })}
      ${stat({ value: Number.isFinite(best) ? n3(best) : "—",
               tone: Number.isFinite(best) ? dir(best) : "",
               label: "best held-out skill",
               note: Number.isFinite(best)
                 ? measured.length < runs.length
                   ? `over the ${plural(measured.length, "generation")} that measured anything`
                   : "on one metric, recomputed"
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
      Everything such a run recorded past that point is refusals scored as silence, so it appears
      on this page as a gap, not a measurement.
      <a href="${raw(href.cost())}">What the loop costs</a>.</p>
    </div></section>` : ""}
    ${incomplete.length ? html`<section class="panel warnband"><div class="panel-b">
      <p class="eyebrow warn">incomplete records</p>
      <p class="prose">${incomplete.map(r => r.id).join(", ")} crashed before a verdict existed.
      An incomplete record is not a rejection — nothing was measured, so there is nothing to
      reject — and it is drawn as a gap rather than a data point.</p>
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
            about the number of matches rather than about the rewrite.` : ""}${
            points.some(p => p.gap) ? html` A generation that ran out of money or crashed is a
            labelled gap: it measured nothing, and a nothing drawn at zero would read as a
            break-even.` : ""}</figcaption>
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
                ${p.gap ? html`<td colspan="5" class="quiet">no measurement — ${p.gap}</td>
                  <td>${p.gap === "crashed" ? "incomplete" : "ran out of money"}</td>`
                : html`<td class="n">${n3(p.inc)}</td>
                  <td class="n">${n3(p.cand)}</td>
                  <td class="n">${n3(p.diff)}</td>
                  <td class="n">${p.low == null ? "—" : `${n3(p.low)} to ${n3(p.high)}`}</td>
                  <td class="n">${p.detectable ? `±${p.detectable.toFixed(3)}` : "—"}</td>
                  <td>${p.accepted ? "promoted" : "dropped"}</td>`}
              </tr>`)}</tbody>
            </table>
          </div>
        </details>
      </div>
    </section>

    ${drifted.length ? html`<section class="panel"><div class="panel-b prose">
      <p class="eyebrow">why these are not the numbers in the manifests</p>
      <p>${plural(new Set(drifted.map(d => d.run.id)).size, "generation")} published a held-out
      figure that its own windows no longer produce:
      ${drifted.map(d => `${d.run.id}'s ${d.side === "baseline" ? "incumbent" : "candidate"}
        published ${n3(d.published)} and recomputes to ${n3(d.here)}`).join("; ")}.
      Nothing was re-scored. The metric changed underneath them — the earliest runs divided by
      an unfloored benchmark, and one was scored while a per-window skill of
      <code>1 - error/benchmark</code> still handed a full point to a harness that said nothing
      on a market that did not move.</p>
      <p class="note">Each of those was the gate's statistic on the day it ran, so none of them
      is wrong; they are different questions, and plotting them on one axis would make a change
      of metric look like a result. The chart above recomputes every generation from its own
      windows on today's formula; each generation's page shows both.</p>
    </div></section>` : ""}

    <section class="panel">
      <div class="panel-h"><h2>Every generation</h2><span class="pill">newest first</span></div>
      <ul class="rows">${runs.map(r => row(r, byRunSide.get(`${r.id}|candidate`) || [],
                                          level.get(r.id), statusOf.get(r.id)))}</ul>
    </section>`;

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

function row(r, windows, level, status) {
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
        <span class="name">${r.id}
          ${verdict}
          ${status === "complete" && hold.underpowered ? pill("underpowered", "warn") : ""}
          ${status === "complete" && hold.usable === false ? pill("no interval", "warn") : ""}
          ${r.candidate_fp && r.candidate_fp === r.incumbent_fp ? pill("unchanged") : ""}</span>
        <span class="why">${(r.reasons || [])[0] || ""}</span>
        <span class="why crumb">${day(r.created)}</span>
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
