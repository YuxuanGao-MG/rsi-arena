/* What the search found, as opposed to what the gate kept.
 *
 * A generation used to keep one harness and throw away the rest: GEPA proposed
 * seven candidates, the best mean scorer was gated, and the other six — each
 * paid for, and some of them the best thing anyone had found on some particular
 * match — were left in the run directory and never read again. Three
 * generations ran that way and none was accepted, so the loop's whole memory of
 * three days of search was one JSON file identical to the one it started from.
 *
 * This page is that memory, read back. It is a file the server holds rather
 * than a table Supabase holds, because it records what was found, which carries
 * no claim, and the moment an archive is ranked it stops preserving the losers
 * that make it worth having.
 */

import { ApiError } from "../data.js";
import { href } from "../routes.js";
import { html, raw, stat, pill, n2, pct, empty, plural } from "../dom.js";
import { archiveFrontier } from "../charts.js";
import { loadArchive, contested, instanceBest, wins, frontier, byFixture, SILENCE } from "../archive.js";

export async function archiveView({ signal }) {
  let entries;
  try {
    entries = await loadArchive({ signal });
  } catch (err) {
    if (err instanceof ApiError && err.kind === "notfound") return notShipped();
    throw err;
  }
  if (!entries.length) return notShipped();

  const disputed = contested(entries);
  const best = instanceBest(entries, disputed);
  const won = wins(entries, best);
  const front = frontier(entries, won, disputed);
  const generations = [...new Set(entries.map(e => e.generation))];
  // The seed carries no scores — it predates the archive keeping any — so it
  // has no mean to plot; it appears in the table, not the scatter.
  const points = entries.filter(e => e.n > 0).map(e => ({
    id: e.id, generation: e.generation, mean: e.mean ?? 0, n: e.n,
    wins: (won.get(e.id) || []).length, onFrontier: front.has(e.id),
    // `promoted` on an archive entry means "the standing incumbent", which is
    // the seed today — not a gate acceptance, which has never happened. The
    // green treatment is reserved for the second claim.
    seed: !!e.isSeed, flat: !!e.flat,
  })).sort((a, b) => b.wins - a.wins);

  const topMean = [...points].sort((a, b) => b.mean - a.mean)[0];
  // The candidates the whole design is for: they would be thrown away by a
  // best-mean rule, and they are the only thing that works somewhere.
  const stepping = points.filter(p => p.onFrontier && p.wins > 0 && p.mean < (topMean?.mean ?? 0));
  const owned = byFixture(entries);

  const body = html`
    <div class="cards">
      ${stat({ value: entries.length, label: "candidates remembered",
               note: `across ${plural(generations.length, "generation")}` })}
      ${stat({ value: front.size, label: "on the frontier",
               note: "best at something, dominated by nothing" })}
      ${stat({ value: best.size, label: "instances scored",
               note: "the space the archive has an opinion about" })}
      ${stat({ value: stepping.length, label: "kept in spite of the mean",
               note: "a best-mean rule would have deleted these" })}
    </div>

    <section class="panel">
      <div class="panel-h"><h2>Everything the search has proposed</h2>
        <span class="pill">frontier filled</span></div>
      <div class="panel-b">
        <div class="legend">
          <span><i class="dot" style="background:var(--c-cand)"></i> on the frontier</span>
          <span><i class="dot" style="background:transparent;border:2px solid var(--ghost)"></i>
            dominated by something</span>
          <span><i class="dot" style="background:transparent;border:1.5px solid var(--brand)"></i>
            the seed — the standing incumbent</span>
        </div>
        <figure class="chart">
          <div id="frontier"></div>
          <figcaption>Right of the line beat silence on average. The interesting dots are low
            and to the right of others' left: candidates that lose on average but are the only
            thing that ever worked on some match.</figcaption>
        </figure>
        <details class="table-view">
          <summary>Every candidate as a table</summary>
          <div class="scroll"><table>
            <caption>Sorted by how many instances the candidate is the best anyone has found on.</caption>
            <thead><tr>
              <th scope="col">candidate</th><th scope="col">generation</th>
              <th scope="col" class="n">mean</th><th scope="col" class="n">best on</th>
              <th scope="col" class="n">beat silence</th><th scope="col">standing</th>
            </tr></thead>
            <tbody>${entries.map(e => ({
              id: e.id, generation: e.generation, mean: e.mean, n: e.n,
              wins: (won.get(e.id) || []).length, onFrontier: front.has(e.id),
              seed: !!e.isSeed, flat: !!e.flat,
            })).sort((a, b) => b.wins - a.wins).map(p => {
              const e = entries.find(x => x.id === p.id);
              return html`<tr>
                <th scope="row" class="mono ticker">${p.id}</th>
                <td class="crumb">${p.generation}</td>
                <td class="n">${n2(p.mean)}</td>
                <td class="n">${p.wins} of ${p.n}</td>
                <td class="n">${e ? pct(e.beatSilence / Math.max(1, e.n)) : "—"}</td>
                <td>${p.onFrontier ? pill("frontier", "brand") : pill("dominated")}
                  ${p.seed ? pill("seed / incumbent", "brand") : ""}
                  ${p.flat ? pill("flat", "warn") : ""}</td>
              </tr>`;
            })}</tbody>
          </table></div>
        </details>
      </div>
    </section>

    ${stepping.length ? html`<section class="panel">
      <div class="panel-h"><h2>Kept in spite of the average</h2></div>
      <div class="panel-b prose">
        <p>${plural(stepping.length, "candidate")} lose on average and are still the best thing
        anyone has found on some match. They stay.</p>
        <details class="more"><summary>which ones, and why keep losers</summary>
          <p>${stepping.map(p => `${p.id} (mean ${n2(p.mean)}, best on ${p.wins})`).join("; ")}.
          Keeping only the best-scoring agent deletes the stepping stones back to solid ground;
          in GEPA's own ablation, selecting from this frontier is worth about twice selecting
          the best mean.</p>
        </details>
      </div></section>` : ""}

    <section class="panel">
      <div class="panel-h"><h2>Who owns which match</h2></div>
      <div class="scroll"><table>
        <caption>For each match in the archive, the candidate with the highest mean score on its
          windows. A match whose owner is not the best overall candidate is a match the frontier
          is paying for.</caption>
        <thead><tr><th scope="col">match</th><th scope="col">best candidate</th>
          <th scope="col" class="n">its mean there</th><th scope="col" class="n">windows</th></tr></thead>
        <tbody>${[...owned.entries()].slice(0, 40).map(([fixture, per]) => {
          let bestId = null, bestMean = -Infinity, windows = 0;
          for (const [id, cell] of per) {
            const mean = cell.sum / cell.n;
            if (mean > bestMean) { bestMean = mean; bestId = id; windows = cell.n; }
          }
          return html`<tr>
            <th scope="row" class="mono ticker">${fixture}</th>
            <td class="mono ticker">${bestId}
              ${bestId === topMean.id ? "" : pill("not the best overall", "warn")}</td>
            <td class="n">${n2(bestMean)}</td>
            <td class="n">${windows}</td>
          </tr>`;
        })}</tbody>
      </table></div>
    </section>

    <p class="sub">The archive is not the lineage. <a href="${raw(href.lineage())}">Lineage</a>
    records what the gate promoted, which is a claim about held-out evidence; this records what
    was proposed, which is cheaper, larger, and claims nothing.</p>`;

  return {
    title: "Archive",
    heading: "Everything the search has ever found",
    lead: html`Every harness the search has ever written — ${plural(entries.length, "candidate")},
      ${front.size} still worth mutating. Nothing here is a result; it is the gene pool the next
      generation starts from.`,
    body,
    ready: root => archiveFrontier(root.querySelector("#frontier"), points),
  };
}

function notShipped() {
  return {
    title: "Archive", heading: "The archive is not deployed here",
    body: html`<section class="panel"><div class="panel-b prose">
      <p>This page reads <code>/archive.json</code>, which the server copies out of
      <code>runs/archive.json</code>. This deployment does not have it: either the loop has not
      written an archive yet, or the image was built without copying it in.</p>
      <p class="note">It is a file rather than a table on purpose — it records what the search
      found, which carries no claim, and Supabase holds only what the gate decided.</p>
    </div></section>`,
  };
}
