/* What a generation costs, and how close it ran to its ceiling.
 *
 * The bill is not a footnote here. The search is the largest recurring line —
 * one generation of a thousand metric calls spent $18 — and the reason the loop
 * grew a per-generation USD ceiling at all. A page that shows skill and hides
 * spend makes a rewrite look free.
 */

import { qAll } from "../data.js";
import { href } from "../routes.js";
import { html, raw, stat, pill, n3, usd, pct, dir, empty, day, plural } from "../dom.js";
import { spendByGeneration } from "../charts.js";
import { ceilingOf } from "../stats.js";

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

export async function costView({ signal }) {
  const runs = await qAll(
    "runs?select=id,created,accepted,baseline,candidate,decision,search,llm&order=created.asc",
    { signal, pageSize: 200, max: 2000 });
  if (!runs.length)
    return { title: "Cost", heading: "Nothing has been spent yet",
             body: empty("No generation has been published.") };

  const rows = runs.map(r => {
    const spent = r.llm?.spent_usd || 0;
    const ceiling = ceilingOf(r);
    const windows = (r.candidate?.holdout?.instances || 0) + (r.candidate?.train?.instances || 0);
    return {
      id: r.id, created: r.created, accepted: r.accepted, spent, ceiling, windows,
      calls: r.llm?.calls ?? null, hits: r.llm?.cache_hits ?? null,
      candidates: r.search?.candidates ?? null,
      metricCalls: r.search?.metric_calls ?? null,
      gain: r.decision?.holdout?.diff ?? null,
    };
  });

  const credit = account();
  const total = rows.reduce((a, r) => a + r.spent, 0);
  const dearest = rows.reduce((a, r) => (r.spent > a.spent ? r : a), rows[0]);
  const ceilings = [...new Set(rows.map(r => r.ceiling).filter(v => v != null))];
  const ceiling = ceilings.length ? ceilings[ceilings.length - 1] : null;
  const over = rows.filter(r => r.ceiling != null && r.spent > r.ceiling);
  const totalWindows = rows.reduce((a, r) => a + r.windows, 0);

  const perGeneration = total / rows.length;
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
      ${stat({ value: usd(total), label: "spent on models",
               note: `across ${plural(rows.length, "generation")}` })}
      ${stat({ value: usd(perGeneration), label: "a generation",
               note: ceiling ? `against a ${usd(ceiling)} ceiling` : "no ceiling recorded" })}
      ${stat({ value: usd(dearest.spent), label: "the dearest one", note: dearest.id })}
      ${stat({ value: totalWindows ? usd(total / totalWindows) : "—", label: "a scored window",
               note: totalWindows ? `${totalWindows} windows scored` : "no window counts published" })}
    </div>

    <section class="panel">
      <div class="panel-h"><h2>Spend by generation</h2>
        ${ceiling ? pill(`ceiling ${usd(ceiling)}`, "warn") : pill("no ceiling recorded", "warn")}</div>
      <div class="panel-b">
        <figure class="chart">
          <div id="spend"></div>
          <figcaption>${ceiling
            ? html`The line is the per-generation ceiling as it was recorded with the run.
                ${ceilings.length > 1
                  ? html`It has changed ${plural(ceilings.length - 1, "time")}; the line shows the
                      most recent value, and each generation is measured against its own in the
                      table below.` : ""}`
            : html`No generation carries a ceiling. The loop writes what it spent into
                <code>manifest["llm"]</code>, and this page reads a ceiling from the same object
                under <code>ceiling_usd</code>, <code>budget_usd</code> or <code>max_usd</code> —
                until one of those is written, there is nothing honest to draw a line at.`}
          </figcaption>
        </figure>
      </div>
    </section>

    ${over.length ? html`<section class="panel"><div class="panel-b note">
      ${plural(over.length, "generation")} finished over the ceiling recorded with it
      (${over.map(r => `${r.id} at ${usd(r.spent)} of ${usd(r.ceiling)}`).join("; ")}). A ceiling
      that refuses rather than queues should make this impossible, so a row here is a bug in the
      accounting or a ceiling raised mid-run.
    </div></section>` : ""}

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
          <td class="n">${usd(r.spent)}</td>
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
          <td class="n ${dir(r.gain)}">${n3(r.gain)}</td>
        </tr>`)}</tbody>
      </table></div>
    </section>

    <p class="sub">Cache hits are free and counted separately from calls, so a generation that
    re-scored the same windows at temperature zero costs nothing the second time. That is also
    why a noise measurement has to be run with <code>--no-llm-cache</code>: the cheap number and
    the honest number are not the same number.</p>`;

  return {
    title: "Cost",
    heading: "What the loop costs to run",
    lead: html`Model spend per generation, against the per-generation ceiling.
      ${runs.some(r => r.accepted)
        ? html`${plural(runs.filter(r => r.accepted).length, "generation")} was promoted, so some
            of this bought a better harness.`
        : html`Nothing has been promoted, so every dollar on this page bought evidence rather than
            a better harness — which is the result, but it is worth knowing the price of it.`}`,
    body,
    ready: root => spendByGeneration(root.querySelector("#spend"), runs, ceiling),
  };
}
