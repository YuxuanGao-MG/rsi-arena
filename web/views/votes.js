/* What the crowd said, against what the arithmetic said.
 *
 * `rsi.votes` has had a read policy and a select grant since the table was
 * written, and nothing in the interface had ever read a vote back — the data
 * was write-only. This is the page that makes a vote worth casting, and the
 * input a Bradley–Terry model over votes would eventually be fitted on.
 */

import { qAll } from "../data.js";
import { href } from "../routes.js";
import { html, raw, stat, pill, n3, pct, dir, empty, day, plural } from "../dom.js";

export async function votesView({ signal }) {
  const [votes, runs] = await Promise.all([
    qAll("votes?select=*&order=created.desc", { signal, max: 20_000 }),
    qAll("runs?select=id,accepted&order=created.desc", { signal, pageSize: 200, max: 2000 }),
  ]);

  if (!votes.length) {
    return {
      title: "Votes", heading: "Nobody has voted yet",
      lead: html`When someone reads a pair of harnesses on the
        <a href="${raw(href.compare())}">compare page</a> and says which they would rather have
        had, the vote lands here beside the pooled skill of both sides.`,
      body: empty("No votes recorded."),
    };
  }

  // Votes cast before the database computed the two skills carry whatever the
  // browser posted, which is exactly the number the comparison is about. They
  // are counted separately rather than trusted or hidden.
  const hasFlag = votes.some(v => Object.prototype.hasOwnProperty.call(v, "server_computed"));
  const trusted = votes.filter(v => !hasFlag || v.server_computed);
  const decided = trusted.filter(v =>
    v.chose !== "neither" && v.baseline_skill != null && v.candidate_skill != null);
  const agreed = decided.filter(v => v.chose === better(v)).length;
  const runIds = new Set(runs.map(r => r.id));

  const byRun = new Map();
  for (const v of decided) {
    if (!byRun.has(v.run_id)) byRun.set(v.run_id, { agree: 0, disagree: 0 });
    byRun.get(v.run_id)[v.chose === better(v) ? "agree" : "disagree"] += 1;
  }

  const body = html`
    <div class="cards">
      ${stat({ value: votes.length, label: "votes cast",
               note: `${new Set(votes.map(v => v.voter)).size} distinct browsers` })}
      ${stat({ value: decided.length ? pct(agreed / decided.length) : "—",
               label: "reader agrees with the score",
               note: decided.length ? `${agreed} of ${decided.length} decided votes`
                                    : "no decided votes yet" })}
      ${stat({ value: votes.filter(v => v.chose === "neither").length, label: "chose neither",
               note: "a real answer, and kept as one" })}
      ${stat({ value: votes.filter(v => v.left_side === "candidate").length,
               label: "rewrite shown left", note: "the coin, for the bias correction" })}
    </div>

    ${hasFlag && trusted.length < votes.length ? html`<section class="panel"><div class="panel-b note">
      ${plural(votes.length - trusted.length, "vote")} were recorded before the database computed
      the two skills for itself. Those rows carry numbers the browser sent, which is the very
      thing this page is comparing against, so they are excluded from the agreement rate.
    </div></section>` : ""}
    ${!hasFlag ? html`<section class="panel"><div class="panel-b note">
      Every vote here was recorded with skills the browser supplied, because
      <code>supabase/migrations/002_votes_rpc.sql</code> has not been run against this database
      yet. Read the agreement rate as provisional.
    </div></section>` : ""}

    ${byRun.size ? html`<section class="panel">
      <div class="panel-h"><h2>By generation</h2></div>
      <div class="scroll"><table>
        <caption>How often a reader's preference matched the pooled held-out skill.</caption>
        <thead><tr><th scope="col">generation</th><th scope="col" class="n">votes</th>
          <th scope="col" class="n">agreed</th><th scope="col">rate</th></tr></thead>
        <tbody>${[...byRun.entries()].map(([id, c]) => {
          const n = c.agree + c.disagree;
          return html`<tr>
            <th scope="row"><a class="mono" href="${raw(href.compare(id))}">${id}</a>
              ${runIds.has(id) ? "" : pill("run not published", "warn")}</th>
            <td class="n">${n}</td>
            <td class="n">${c.agree}</td>
            <td><div class="meter" role="img"
                     aria-label="${pct(c.agree / n)} of ${plural(n, "vote")} agreed">
                  <i style="width:${raw(Math.round((c.agree / n) * 100))}%"></i></div>
              <span class="crumb tnum">${pct(c.agree / n)}</span></td>
          </tr>`;
        })}</tbody>
      </table></div>
    </section>` : ""}

    <section class="panel">
      <div class="panel-h"><h2>Every vote</h2><span class="pill">newest first</span></div>
      <div class="scroll"><table>
        <caption>One row per vote. The two skills are the pooled held-out figures for that
          match, which the reader could not see when they chose.</caption>
        <thead><tr>
          <th scope="col">when</th><th scope="col">match</th><th scope="col">chose</th>
          <th scope="col" class="n">incumbent</th><th scope="col" class="n">rewrite</th>
          <th scope="col">verdict</th>
        </tr></thead>
        <tbody>${votes.slice(0, 200).map(v => {
          const call = better(v);
          const known = call != null && v.chose !== "neither";
          return html`<tr>
            <td class="crumb">${day(v.created)}</td>
            <th scope="row"><a class="mono ticker"
              href="${raw(href.compare(v.run_id, v.fixture))}">${v.fixture}</a></th>
            <td>${v.chose === "baseline" ? "incumbent" : v.chose === "candidate" ? "rewrite" : "neither"}
              ${v.left_side === v.chose ? pill("left") : ""}</td>
            <td class="n ${dir(v.baseline_skill)}">${n3(v.baseline_skill)}</td>
            <td class="n ${dir(v.candidate_skill)}">${n3(v.candidate_skill)}</td>
            <td>${!known ? html`<span class="crumb">—</span>`
              : v.chose === call ? pill("agreed", "up") : pill("disagreed", "down")}</td>
          </tr>`;
        })}</tbody>
      </table></div>
    </section>`;

  return {
    title: "Votes",
    heading: "How often a reader and the arithmetic disagree",
    lead: html`Not who wins. A harness that reads well and scores badly is the thing most likely
      to fool the rewriter, which is judging text too — so the disagreements are the rows worth
      reading.`,
    body,
  };
}

const better = v => v.baseline_skill == null || v.candidate_skill == null ? null
  : v.baseline_skill === v.candidate_skill ? "neither"
  : v.baseline_skill > v.candidate_skill ? "baseline" : "candidate";
