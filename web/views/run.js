/* One generation: what it scored on the matches it was held out on, where it
 * lost, where it won, and what its forecasts looked like against the prices.
 *
 * Three queries, all of them held-out only. The page this replaces filtered on
 * `run_id` and `side` but not `split`, so the cards said "held-out skill" and
 * the tables underneath were train and held-out pooled together. It also took
 * the best six rows of a 500-row ascending slice and labelled them "Where it
 * won", which on any run with more than 500 windows is the best of the worst.
 */

import { q, qAll, qs } from "../data.js";
import { href } from "../routes.js";
import {
  html, raw, stat, pill, n3, usd, price, dir, empty, clock, plural, holdout, skillBar,
} from "../dom.js";
import { predictedVsRealised, skillHistogram } from "../charts.js";
import { pooled, pooledOnMoves, windowSkill, refused, runStatus } from "../stats.js";
import { genName, rewriteLabel, pointsText } from "../labels.js";

const NARROW = "id,fixture,ticker,at,mid_now,realised,predicted,half_width," +
               "err,naive_error,skill,echoed,unmeasurable,cost_usd,ok,scored";

export async function runView({ params, query, signal }) {
  const id = params.id;
  const side = query.side === "baseline" ? "baseline" : "candidate";
  if (!id) return { title: "Generation", heading: "No generation named", body: empty("Pick one from the list.") };

  // `eq.` is the operator, not decoration. Without it PostgREST answers 400
  // "failed to parse filter" — and because the test fixtures accepted the
  // malformed filter, every generation link on the live site was an error page
  // while the whole suite stayed green. The fixture server now rejects
  // operator-less filters the way PostgREST does; see tests/harness.mjs.
  const base = `rollouts?run_id=eq.${encodeURIComponent(id)}&side=eq.${side}&split=eq.holdout`;
  // The run record and the windows do not depend on one another; serial awaits
  // here used to cost a round trip each.
  const [[run], allRaw, allRuns] = await Promise.all([
    q(`runs?id=eq.${encodeURIComponent(id)}&select=*`, { signal }),
    qAll(`${base}&select=${NARROW}&order=skill.asc`, { signal, max: 8000 }),
    q("runs?select=id,created&order=created.asc", { signal }).catch(() => []),
  ]);

  if (!run) {
    return { title: "Generation", heading: "No such generation",
             body: empty(html`Nothing published under <code>${id}</code>.`) };
  }
  if (!allRaw.length) {
    const why = runStatus(run);
    return {
      title: id, heading: id,
      crumbs: [["generations", href.runs()], [id]],
      body: empty(why === "exhausted"
        ? html`The ${side} of this generation has no held-out windows because the money ran out
            before any were paid for. That is the whole record: nothing was measured here.`
        : why === "incomplete"
        ? html`This generation crashed before the ${side} was scored on held-out. An incomplete
            record, not a rejection.`
        : html`This generation has no held-out windows for the ${side}. It may have been
            published before the split was recorded, or scored on train only.`),
    };
  }

  // Every skill on this page is recomputed from the window's own errors, so a
  // generation scored under an older metric does not read as a better one — and
  // so "worst first" and "best first" are actually worst and best. Ordering by
  // the stored column would put six echoes of the mid, each worth a full point
  // under the metric of the week, at the top of "Where it won".
  const everything = allRaw.map(r => ({ ...r, published_skill: r.skill, skill: windowSkill(r) }));
  // A refusal is a window the harness never answered — gen5 stored 715 of them
  // with `predicted == mid_now`, so left in they rank as confident echoes.
  // They are counted, said out loud below, and in nothing else on this page.
  const refusals = everything.filter(refused);
  const all = everything.filter(r => !refused(r));
  const ranked = [...all].sort((a, b) => (a.skill ?? 0) - (b.skill ?? 0));
  const worst = ranked.slice(0, 12);
  const best = ranked.slice(-6).reverse();
  const status = runStatus(run);

  // The drivers, for those eighteen rows only. `output` is the largest column
  // on the table and there is no reason to drag it across every window to print
  // eighteen sentences.
  const wanted = [...worst, ...best].map(r => r.id);
  const drivers = new Map();
  if (wanted.length) {
    const rows = await qAll(`rollouts?select=id,output&id=${qs.inList(wanted)}`,
                            { signal, max: 100 });
    for (const row of rows) drivers.set(row.id, row.output);
  }
  for (const row of [...worst, ...best]) row.output = drivers.get(row.id) || null;

  const published = holdout(run, side) || {};
  const mine = pooled(everything);
  const moved = pooledOnMoves(all);
  const cost = everything.reduce((a, r) => a + (r.cost_usd || 0), 0);
  // The manifest's statistic pooled the refusals; below a centipoint the two
  // are the same claim and the panel would be flagging rounding noise.
  const drift = status === "complete" && published.statistic != null && mine.skill != null
    && Math.abs(published.statistic - mine.skill) > 0.01;

  const body = html`
    <div class="cards">
      ${stat({ value: n3(mine.skill), tone: dir(mine.skill),
               label: "held-out pooled skill",
               note: `${plural(mine.scored, "scored window")} · zero is silence` })}
      ${stat({ value: n3(moved.skill), tone: dir(moved.skill),
               label: "on the windows that moved",
               note: `${moved.instances} of ${all.length} moved a tick or more` })}
      ${stat({ value: all.filter(r => r.echoed).length,
               label: "echoed the mid", note: "worth exactly zero, by construction" })}
      ${stat({ value: usd(published.cost_usd ?? cost), label: "cost",
               note: mine.scored
                 ? `${usd((published.cost_usd ?? cost) / mine.scored)} a scored window` : "" })}
    </div>

    ${refusals.length ? html`<section class="panel warnband"><div class="panel-b prose">
      <p>${refusals.length} of ${everything.length} windows here are budget-exhaustion
      refusals, not forecasts. Every number on this page excludes them.</p>
      <details class="more"><summary>what a refusal is</summary>
        <p>The generation's money ran out mid-run, so every later call was refused before the
        model was asked. Refusals are stored looking like "no change" forecasts — pooled
        naively they would score as confident echoes of the market, flattering a harness that
        never spoke.</p>
      </details>
    </div></section>` : ""}

    ${power(run, all)}

    ${drift ? html`<section class="panel"><div class="panel-b prose">
      <p>Two numbers, one generation: the gate recorded ${n3(published.statistic)} on the day;
      the same windows recompute to ${n3(mine.skill)} on today's formula.</p>
      <details class="more"><summary>why they differ</summary>
        <p>Nothing was re-scored — the metric changed after this run. The published figure is
        what decided the verdict; the recomputed one is what can be compared across
        generations, and it is the one this site plots.</p>
      </details>
    </div></section>` : ""}

    ${audit(run)}

    <section class="panel">
      <div class="panel-h">
        <h2>${status === "incomplete" ? "Incomplete record"
            : status === "exhausted" ? "Ran out of money"
            : run.accepted ? "Promoted" : "Dropped"}</h2>
        ${status === "incomplete" ? pill("crashed before a verdict", "warn")
        : status === "exhausted" ? pill("budget spent mid-run", "warn")
        : pill(run.accepted ? "kept" : "rejected", run.accepted ? "up" : "down")}
        <span class="spacer"></span>
        <a href="${raw(href.compare(id))}">read both harnesses side by side</a>
      </div>
      <div class="panel-b">
        <div class="prose">${(run.reasons || []).map(x => html`<p>${x}</p>`)}
          ${(run.reasons || []).length ? "" : html`<p class="note">No reasons were recorded.</p>`}</div>
        ${provenance(run)}
        <nav class="btn-row" aria-label="Which harness">
          <a class="btn" href="${raw(href.run(id, "baseline"))}"
             ${raw(side === "baseline" ? 'aria-current="page"' : "")}>the original harness</a>
          <a class="btn" href="${raw(href.run(id, "candidate"))}"
             ${raw(side === "candidate" ? 'aria-current="page"' : "")}>the rewrite</a>
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
      <div class="scroll">${windowTable(worst,
        html`The held-out windows this harness lost the most error on, worst first.${restated(worst)}`)}</div>
    </section>

    <section class="panel">
      <div class="panel-h"><h2>Where it won</h2>
        <span class="pill">best of all ${plural(all.length, "held-out window")}</span></div>
      <div class="scroll">${windowTable(best,
        html`The held-out windows it removed the most error on, best first.${restated(best)}`)}</div>
    </section>`;

  return {
    title: id,
    heading: side === "candidate"
      ? html`Did ${genName(id, allRuns)}'s rewrite forecast better than what it replaced?`
      : html`How the original harness did in ${genName(id, allRuns)}`,
    lead: html`${side === "candidate" ? "The rewrite" : "It"}
      ${pointsText(mine.skill)} across ${plural(all.length, "scored moment")}
      on ${plural(new Set(all.map(r => r.fixture)).size, "match", "matches")} it had never
      seen${refusals.length ? html` (${plural(refusals.length, "refusal")} excluded)` : ""}.
      <span class="crumb mono">${id} · ${side}</span>`,
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
      ${h.underpowered
        ? html`<p>Too close to call — literally. On ${plural(groups, "match", "matches")} this
            test cannot see a difference smaller than ${n3(h.detectable)}, and it measured
            ${n3(h.diff)}.</p>
            <details class="more"><summary>what that means</summary>
              <p>The rejection is a fact about the number of matches, not about the rewrite:
              no candidate this size could have been visible to the test judging it
              ${h.se ? html`(standard error ${h.se.toFixed(4)})` : ""}. More matches, not a
              better rewrite, is what would change it.</p>
            </details>`
        : html`<p>The measurement (${n3(h.diff)}) was large enough for this test to see —
            its floor on ${plural(groups, "match", "matches")} is ${n3(h.detectable)}.</p>`}
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
    bits.push(html`<p>The search started from an earlier candidate
      (<code>${s.seed}</code>) instead of the current harness — kept around because it was the
      best anyone had found on some matches, even though it lost on average.
      <a href="${raw(href.archive())}">The archive it came from</a>.</p>`);
  else if (s.seed)
    bits.push(html`<p>The search started from the incumbent.</p>`);
  if (archive)
    bits.push(html`<p class="note">${plural(archive.candidates, "candidate")} remembered,
      ${archive.frontier} on the frontier, across ${plural(archive.generations, "generation")}.</p>`);
  if (llm.spent_usd != null)
    bits.push(html`<p class="note">Spent ${usd(llm.spent_usd)}${llm.budget_usd
      ? ` of a ${usd(llm.budget_usd)} ceiling` : ""}${llm.exhausted
      ? " — and stopped because that ran out, not because the search was finished" : ""}.
      ${llm.cache_hits ? `${llm.cache_hits} of ${(llm.calls || 0) + llm.cache_hits} model calls came from the cache.` : ""}</p>`);
  return bits.length ? html`<div class="prose provenance">${bits}</div>` : "";
}

/** Says so when a table's numbers are not the ones the database stored. */
function restated(rows) {
  const moved = rows.filter(r => r.published_skill != null
    && Math.abs(r.published_skill - r.skill) > 0.002).length;
  return moved
    ? html` ${plural(moved, "row")} here scored differently under the metric in force when this
        generation ran; the skill shown is recomputed from the window's own errors.`
    : "";
}

function windowTable(rows, caption) {
  if (!rows.length) return empty("nothing scored");
  return html`<table>
    <caption>${caption}</caption>
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
            ${r.echoed ? pill("echo") : ""} ${r.unmeasurable ? pill("quiet", "warn") : ""}</span>
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
