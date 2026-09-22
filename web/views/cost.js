/* What a generation costs, and how close it ran to its ceiling.
 *
 * The bill is not a footnote here. The search is the largest recurring line —
 * one generation of a thousand metric calls spent $18 — and the reason the loop
 * grew a per-generation USD ceiling at all. A page that shows skill and hides
 * spend makes a rewrite look free.
 */

import { qAll, topicFilter } from "../data.js";
import { href } from "../routes.js";
import { html, raw, stat, pill, n3, usd, pct, dir, empty, day, plural } from "../dom.js";
import { spendByGeneration } from "../charts.js";
import { ceilingOf, runStatus } from "../stats.js";
import { DEFAULT } from "../topics.js";

/**
 * The model account's balance, which no manifest holds.
 *
 * Injected by the server from the environment, because the only place this
 * number exists is the OpenRouter dashboard. Unset is the normal state and
 * shows nothing at all — a balance nobody has updated is worse than no balance,
 * so it carries the date it was set and says so when it has none.
 */
function account() {
  const c = (window.RSI && window.RSI.credit) || {};
  const num = v => {
    const n = Number(String(v || "").replace(/[^0-9.\-]/g, ""));
    return Number.isFinite(n) && n > 0 ? n : null;
  };
  return { remaining: num(c.remaining), total: num(c.total), asOf: (c.asOf || "").trim() || null };
}

export async function costView({ signal, topic = DEFAULT }) {
  const runs = await qAll(
    "runs?select=id,created,accepted,baseline,candidate,decision,search,llm" +
    `${topicFilter(topic)}&order=created.asc`,
    { signal, pageSize: 200, max: 2000 });
  if (!runs.length)
    return { title: "Cost", heading: "Nothing has been spent yet",
             body: empty("No generation has been published.") };

  const rows = runs.map(r => {
    const status = runStatus(r);
    const spent = r.llm?.spent_usd || 0;
    const ceiling = ceilingOf(r);
    // Windows that were actually forecast and scored — both sides, both
    // splits, refusals out. gen5's manifest counts 160 candidate "instances"
    // that are all budget refusals, and gen4 scored 440 real baseline windows
    // that a candidate-only sum missed entirely.
    let windows = 0;
    for (const side of ["baseline", "candidate"])
      for (const split of ["train", "holdout"]) {
        const board = r[side] && r[side][split];
        if (board && board.instances) windows += board.instances - (board.unscored || 0);
      }
    return {
      id: r.id, created: r.created, accepted: r.accepted, status, spent, ceiling, windows,
      calls: r.llm?.calls ?? null, hits: r.llm?.cache_hits ?? null,
      candidates: r.search?.candidates ?? null,
      metricCalls: r.search?.metric_calls ?? null,
      gain: status === "complete" ? r.decision?.holdout?.diff ?? null : null,
    };
  });

  const credit = account();
  const recorded = rows.filter(r => r.status !== "incomplete" && r.spent > 0);
  const total = recorded.reduce((a, r) => a + r.spent, 0);
  const dearest = recorded.reduce((a, r) => (r.spent > a.spent ? r : a), recorded[0] || rows[0]);
  const ceilings = [...new Set(rows.map(r => r.ceiling).filter(v => v != null))];
  // Each generation is measured against its own recorded ceiling. One line is
  // only drawn when every recorded ceiling agrees — a single run's one-off
  // budget is not a policy, and drawing it across the chart claims it is.
  const ceiling = ceilings.length === 1 ? ceilings[0] : null;
  const unrecorded = rows.filter(r => r.status === "incomplete");
  const totalWindows = rows.reduce((a, r) => a + r.windows, 0);

  const perGeneration = recorded.length ? total / recorded.length : 0;
  const left = credit.remaining == null ? null : Math.floor(credit.remaining / perGeneration);

  const body = html`
    ${credit.remaining == null ? "" : html`<section class="panel ${raw(
        left != null && left < 1 ? "warnband" : "")}">
      <div class="panel-h"><h2>What is left to spend</h2>
        ${credit.asOf ? pill(`as of ${credit.asOf}`) : pill("not dated", "warn")}</div>
      <div class="panel-b">
        <div class="cards">
          ${stat({ value: usd(credit.remaining), hero: true, label: "credit remaining",
                   tone: left != null && left < 1 ? "down" : "",
                   note: credit.total ? `of ${usd(credit.total)} bought` : "on the model account" })}
          ${stat({ value: left == null ? "—" : left, label: "generations that buys",
                   note: `at ${usd(perGeneration)} a generation so far` })}
          ${stat({ value: usd(total), label: "spent so far",
                   note: credit.total ? pct(total / credit.total) + " of the account" : "on models" })}
        </div>
        ${credit.total ? html`<div class="meter ${raw(left != null && left < 1 ? "over" : "")}"
          role="img" aria-label="${pct(1 - credit.remaining / credit.total)} of the account spent">
          <i style="width:${raw(Math.min(100, Math.round((1 - credit.remaining / credit.total) * 100)))}%"></i>
        </div>` : ""}
        <p class="prose">${left != null && left < 1
          ? html`There is not enough left for another generation at the rate the last ones ran.
              The loop stops when this reaches zero, and nothing on this site changes after that
              until the account is topped up.`
          : html`Generations stop when this reaches zero. It is the one number here that is not
              in any manifest — a generation records what it spent, and nothing records what
              there is left to spend — so it is set on the service by hand and is only as current
              as the date beside it.`}</p>
      </div>
    </section>`}

    <div class="cards">
      ${stat({ value: usd(total), label: "recorded spend",
               note: unrecorded.length
                 ? `${plural(recorded.length, "generation")}; ${unrecorded.map(r => r.id).join(", ")}
                    crashed before writing a ledger`
                 : `across ${plural(rows.length, "generation")}` })}
      ${stat({ value: usd(perGeneration), label: "a generation",
               note: "where the ledger was written" })}
      ${stat({ value: usd(dearest?.spent), label: "the dearest one", note: dearest?.id || "—" })}
      ${stat({ value: totalWindows ? usd(total / totalWindows) : "—", label: "a scored window",
               note: totalWindows
                 ? `${totalWindows} forecasts actually scored, refusals excluded`
                 : "no window counts published" })}
    </div>

    <section class="panel">
      <div class="panel-h"><h2>Spend by generation</h2>
        ${ceilings.length ? pill("each against its own ceiling") : pill("no ceiling recorded", "warn")}</div>
      <div class="panel-b">
        <figure class="chart">
          <div id="spend"></div>
          <figcaption>${ceiling
            ? html`Every recorded ceiling is the same, so one line stands for all of them.`
            : ceilings.length
            ? html`The ceilings differ from run to run — a budget is set per generation, not as
                policy — so no single line is drawn; the table below measures each generation
                against its own.`
            : html`No generation carries a recorded ceiling, so there is nothing honest to draw
                a line at.`}
          </figcaption>
        </figure>
      </div>
    </section>

    <section class="panel">
      <div class="panel-h"><h2>Every generation</h2></div>
      <div class="scroll"><table>
        <caption>What each generation spent, what it was allowed, and what it bought.</caption>
        <thead><tr>
          <th scope="col">generation</th><th scope="col" class="n">spent</th>
          <th scope="col">against its ceiling</th><th scope="col" class="n">calls</th>
          <th scope="col" class="n">cache hits</th><th scope="col" class="n">candidates</th>
          <th scope="col" class="n">held-out gain</th>
        </tr></thead>
        <tbody>${rows.map(r => html`<tr>
          <th scope="row"><a class="mono" href="${raw(href.run(r.id))}">${r.id}</a>
            <span class="crumb">${day(r.created)}</span></th>
          <td class="n">${r.status === "incomplete"
            ? html`<span class="crumb">not recorded</span>` : usd(r.spent)}</td>
          <td>${r.ceiling == null
            ? html`<span class="crumb">not recorded</span>`
            : html`<div class="meter ${r.spent > r.ceiling ? "over" : ""}" role="img"
                        aria-label="${pct(r.spent / r.ceiling)} of ${usd(r.ceiling)}">
                     <i style="width:${raw(Math.min(100, Math.round((r.spent / r.ceiling) * 100)))}%"></i>
                   </div>
                   <span class="crumb tnum">${pct(r.spent / r.ceiling)} of ${usd(r.ceiling)}</span>`}</td>
          <td class="n">${r.calls ?? "—"}</td>
          <td class="n">${r.hits ?? "—"}</td>
          <td class="n">${r.candidates ?? "—"}</td>
          <td class="n ${dir(r.gain)}">${r.status !== "complete"
            ? html`<span class="crumb">${r.status === "incomplete" ? "crashed" : "ran out of money"}</span>`
            : n3(r.gain)}</td>
        </tr>`)}</tbody>
      </table></div>
    </section>
`;

  return {
    title: "Cost",
    heading: "What the loop costs to run",
    lead: html`Model spend per generation, each against the ceiling it was given.
      ${runs.some(r => r.accepted)
        ? html`${plural(runs.filter(r => r.accepted).length, "generation")} was promoted, so some
            of this bought a better harness.`
        : html`Nothing has been promoted, so every dollar on this page bought evidence rather than
            a better harness — which is the result, but it is worth knowing the price of it.`}`,
    body,
    ready: root => spendByGeneration(root.querySelector("#spend"),
      runs.map(r => ({ ...r, _ceiling: ceilingOf(r) })), ceiling),
  };
}
