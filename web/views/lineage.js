/* Where each generation came from, and where each candidate came from.
 *
 * Two trees, because they are two different claims. The first is the promotion
 * lineage the gate wrote: a generation, its parent, and whether it survived.
 * The second is what the search actually walked — every candidate GEPA proposed
 * and the candidate it was mutated from — which is larger, cheaper, and claims
 * nothing at all.
 *
 * The tree this replaces drew only what was reachable from a synthetic
 * `"__seed__"` root, so a run whose parent had not been published vanished from
 * the page without a word, and a run that pointed at itself recursed until the
 * tab died. Both are visible states now: an unreachable run is drawn at the top
 * level and labelled with the parent nobody can see, and a cycle stops at the
 * repeat and says so.
 */

import { qAll } from "../data.js";
import { href } from "../routes.js";
import { html, raw, pill, n3, n2, dir, empty, day, plural } from "../dom.js";
import { loadArchive, wins, frontier, tree } from "../archive.js";
import { sparkline } from "../charts.js";

const COLUMNS = "id,created,parent,accepted,reasons,incumbent_fp,candidate_fp," +
                "baseline,candidate,decision,search";

export async function lineageView({ signal }) {
  const [runs, entries] = await Promise.all([
    qAll(`runs?select=${COLUMNS}&order=created.asc`, { signal, pageSize: 200, max: 2000 }),
    // The archive is a file this server holds. Its absence is a deployment
    // state, not a failure of this page.
    loadArchive({ signal }).catch(() => null),
  ]);
  if (!runs.length && !entries) {
    return { title: "Lineage", heading: "Nothing to draw yet",
             body: empty("No generation has been published and no archive is deployed.") };
  }

  const kept = runs.filter(r => r.accepted).length;
  const { byId, children, roots, orphans } = promotions(runs);
  const selfish = runs.filter(r => r.parent === r.id);
  const seen = new Set();
  const draw = list => html`<ul class="tree">${list.map(r => {
    if (seen.has(r.id))
      return html`<li><p class="note">${r.id} appears again here — its parent chain loops.
        Drawn once, above.</p></li>`;
    seen.add(r.id);
    const kids = children.get(r.id) || [];
    return html`<li>${node(r, byId)}${kids.length ? draw(kids) : ""}</li>`;
  })}</ul>`;

  return {
    title: "Lineage",
    heading: "Lineage",
    lead: html`Every generation from the seed, kept and dropped alike. A tree showing only what
      survived is a tree nobody can learn from — and ${kept === 0
        ? html`nothing has been promoted yet, which is itself the result`
        : html`${plural(kept, "generation")} of ${runs.length} survived`}.`,
    body: html`
      <section class="panel"><div class="panel-b">
        <p class="eyebrow">the seed</p>
        <p class="mono ticker">${(runs[0] && runs[0].incumbent_fp) || "not recorded"}</p>
        <p class="note">A fingerprint covers the components and the model — what the loop can
        rewrite — so a generation that changed nothing carries the same one.</p>
      </div></section>

      ${orphans.length ? html`<section class="panel"><div class="panel-b note">
        ${plural(orphans.length, "generation")} below name a parent that is not in the database
        (${orphans.map(r => r.parent).join(", ")}). They are drawn at the top level rather than
        dropped, because a missing parent is a publishing gap, not a missing run.
      </div></section>` : ""}
      ${selfish.length ? html`<section class="panel"><div class="panel-b note">
        ${plural(selfish.length, "generation")} name themselves as their own parent
        (${selfish.map(r => r.id).join(", ")}). That is a bad record rather than a loop in the
        experiment, and it is drawn once at the top level.
      </div></section>` : ""}

      <h2>What the gate promoted</h2>
      ${runs.length ? draw(roots) : empty("No generation has been published.")}

      ${entries ? candidateTree(entries) : html`<p class="sub">The candidate archive is not
        deployed here, so only the promotion lineage can be drawn.
        <a href="${raw(href.archive())}">The archive</a>.</p>`}`,
  };
}

function promotions(runs) {
  const byId = new Map(runs.map(r => [r.id, r]));
  const children = new Map();
  const roots = [];
  for (const r of runs) {
    const parent = r.parent && r.parent !== r.id && byId.has(r.parent) ? r.parent : null;
    if (parent) {
      if (!children.has(parent)) children.set(parent, []);
      children.get(parent).push(r);
    } else {
      roots.push(r);
    }
  }
  return { byId, children, roots,
           orphans: roots.filter(r => r.parent && r.parent !== r.id && !byId.has(r.parent)) };
}

function node(r, byId) {
  const d = r.decision?.holdout?.diff;
  const missingParent = r.parent && r.parent !== r.id && !byId.has(r.parent);
  return html`<a class="row card-row" href="${raw(href.run(r.id))}" data-ok="${r.accepted ? 1 : 0}">
    <span class="mark" aria-hidden="true"></span>
    <span>
      <span class="name">${r.id}
        ${pill(r.accepted ? "kept" : "dropped", r.accepted ? "up" : "down")}
        ${r.decision?.holdout?.underpowered ? pill("underpowered", "warn") : ""}
        ${r.candidate_fp === r.incumbent_fp ? pill("unchanged") : ""}
        ${missingParent ? pill("parent not published", "warn") : ""}</span>
      <span class="why mono">held out ${n3(r.baseline?.holdout?.statistic)} →
        ${n3(r.candidate?.holdout?.statistic)} · ${(r.search?.candidates) ?? "?"} candidates
        · ${day(r.created)}</span>
      <span class="why">${(r.reasons || [])[0] || ""}</span>
    </span>
    <span class="spark"></span>
    <span class="right"><span class="delta ${dir(d)}">${n3(d)}</span></span>
  </a>`;
}

/** The lineage the search walked: every candidate, and what it was mutated from. */
function candidateTree(entries) {
  const won = wins(entries);
  const front = frontier(entries, won);
  const { byId, children, roots, orphans } = tree(entries);
  const seen = new Set();

  const drawn = list => html`<ul class="tree">${list.map(e => {
    if (seen.has(e.id)) return html`<li><p class="note">${e.id} loops; drawn once above.</p></li>`;
    seen.add(e.id);
    const kids = (children.get(e.id) || []).sort((a, b) => (b.mean ?? 0) - (a.mean ?? 0));
    const mine = won.get(e.id) || [];
    const scores = Object.values(e.scores || {}).map(v => v - 0.5);
    return html`<li>
      <div class="row card-row" data-ok="${front.has(e.id) ? 1 : 0}">
        <span class="mark" aria-hidden="true"></span>
        <span>
          <span class="name mono ticker">${e.id}
            ${front.has(e.id) ? pill("frontier", "brand") : pill("dominated")}
            ${e.promoted ? pill("promoted", "up") : ""}
            ${e.parent && !byId.has(e.parent) ? pill("parent not archived", "warn") : ""}</span>
          <span class="why">${e.generation} · mean ${n2(e.mean)} · best on
            ${mine.length} of ${e.n} instances${e.discovered_after_calls
              ? ` · found after ${e.discovered_after_calls} evaluations` : ""}</span>
          ${e.note ? html`<span class="why">${e.note}</span>` : ""}
        </span>
        <span class="spark">${raw(sparkline(scores, {
          label: `${scores.length} instances, worst to best, against silence`,
        }))}</span>
        <span class="right crumb">${plural(e.children || 0, "child")}</span>
      </div>
      ${kids.length ? drawn(kids) : ""}
    </li>`;
  })}</ul>`;

  return html`
    <h2>What the search proposed</h2>
    <p class="sub">${plural(entries.length, "candidate")}, ${front.size} of them on the frontier.
    A dominated candidate is still drawn: it is the stepping stone the next generation may be
    sampled from, and deleting it is how a search forgets the way back to solid ground.
    ${orphans.length ? html`${plural(orphans.length, "candidate")} name a parent the archive does
      not hold and are drawn at the top level.` : ""}</p>
    ${drawn(roots.sort((a, b) => (b.mean ?? 0) - (a.mean ?? 0)))}`;
}
