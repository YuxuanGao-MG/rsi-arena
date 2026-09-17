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

    ${power(run, all)}

    ${drift ? html`<section class="panel"><div class="panel-b prose">
      <p class="eyebrow">two numbers for one generation</p>
      <p>The gate recorded ${n3(published.statistic)} here. The same windows recompute to
      ${n3(mine.skill)} on today's formula.</p>
      <p class="note">Nothing was re-scored: the metric changed after this run. The earliest runs
      divided by an unfloored benchmark, and <code>gen1-floored</code> was scored while a
      per-window skill of <code>1 - error/benchmark</code> still paid a full point for saying
      nothing on a market that did not move. The published figure is what the gate read on the
      day and is the one that decided the promotion; the recomputed figure is the one that can be
      compared with another generation.</p>
    </div></section>` : ""}

    ${audit(run)}

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
        ${provenance(run)}
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

/**
 * What this test could see at all.
 *
 * An interval that straddles zero says "not proven", and "not proven" means two
 * different things depending on whether the test could resolve the effect. On a
 * two-fixture held-out set the smallest gap the bootstrap separates from noise
 * is larger than any gain anyone has found — so the rejection was a statement
 * about the sample, and saying so is the difference between a result and an
 * absence of one.
 */
function power(run, rows) {
  const h = run.decision?.holdout || {};
  const groups = h.groups ?? new Set(rows.map(r => r.fixture)).size;
  if (h.usable === false) {
    return html`<section class="panel warnband"><div class="panel-b prose">
      <p class="eyebrow warn">no interval could be drawn</p>
      <p>${plural(groups, "held-out fixture")} is too few for the gate to resample from, so no
      gain could have been promoted here no matter how large it was. The interval narrows with
      the number of <em>matches</em>, not the number of windows: fifty windows on one match are
      fifty correlated observations of one game.</p>
    </div></section>`;
  }
  if (!h.detectable) return "";
  return html`<section class="panel ${raw(h.underpowered ? "warnband" : "")}">
    <div class="panel-b prose">
      <p class="eyebrow ${raw(h.underpowered ? "warn" : "")}">${h.underpowered
        ? "this test could not have seen it" : "what this test could resolve"}</p>
      <p>On ${plural(groups, "held-out fixture")} the smallest difference this bootstrap can
      separate from noise is ${n3(h.detectable)}${h.se ? html` (a standard error of
        ${h.se.toFixed(4)})` : ""}. The difference it measured was ${n3(h.diff)}.
      ${h.underpowered
        ? html`That is inside the noise floor, so this generation's rejection is a fact about the
            number of matches it was judged on and not about the rewrite. More fixtures, not a
            better candidate, is what would change it.`
        : html`The measurement was large enough to be visible to the test that judged it.`}</p>
    </div></section>`;
}

/** The confirmation pass, when there was one. */
function audit(run) {
  const a = run.audit;
  if (!a || !a.candidate) return "";
  const confirmed = a.decision?.accepted;
  return html`<section class="panel">
    <div class="panel-h"><h2>The audit set</h2>
      ${pill(confirmed ? "confirmed" : "not confirmed", confirmed ? "up" : "down")}</div>
    <div class="panel-b prose">
      <p>Cut away before anything else and shown to nothing until the gate had already said yes:
      ${plural(a.candidate.instances ?? 0, "window")} the search has never seen.
      The candidate scored ${n3(a.candidate.statistic)} there against the incumbent's
      ${n3(a.baseline?.statistic)}.</p>
      <p class="note">${confirmed
        ? "A promotion that survives this is a promotion."
        : "A promotion that does not survive this is the winner's curse caught in the act, and it was withdrawn."}
        ${(a.decision?.reasons || []).join(" ")}</p>
    </div></section>`;
}

/** Where this generation's search started, and what it cost. */
function provenance(run) {
  const s = run.search || {}, llm = run.llm || {};
  const archive = s.archive_after || s.archive;
  const bits = [];
  if (s.seed && s.seed_is_incumbent === false)
    bits.push(html`<p>The search started from archived candidate <code>${s.seed}</code> rather
      than from the incumbent — the frontier is sampled in proportion to how much of the instance
      space a candidate uniquely owns, so a stepping stone that lost on average can still be the
      thing worth mutating. <a href="${raw(href.archive())}">The archive</a>.</p>`);
  else if (s.seed)
    bits.push(html`<p>The search started from the incumbent.</p>`);
  if (archive)
    bits.push(html`<p class="note">${archive.candidates} candidates remembered,
      ${archive.frontier} on the frontier, across ${plural(archive.generations, "generation")}.</p>`);
  if (llm.spent_usd != null)
    bits.push(html`<p class="note">Spent ${usd(llm.spent_usd)}${llm.budget_usd
      ? ` of a ${usd(llm.budget_usd)} ceiling` : ""}${llm.exhausted
      ? " — and stopped because that ran out, not because the search was finished" : ""}.
      ${llm.cache_hits ? `${llm.cache_hits} of ${(llm.calls || 0) + llm.cache_hits} model calls came from the cache.` : ""}</p>`);
  return bits.length ? html`<div class="prose provenance">${bits}</div>` : "";
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
