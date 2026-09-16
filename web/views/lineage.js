/* Where each generation came from.
 *
 * The tree this replaces drew only what was reachable from a synthetic
 * `"__seed__"` root, so a run whose parent had not been published vanished
 * from the page without a word — and a run that pointed at itself recursed
 * until the tab died. Both are now visible states rather than missing ones:
 * an unreachable run is drawn at the top level and labelled with the parent
 * nobody can see, and a cycle stops at the repeat and says so.
 */

import { qAll } from "../data.js";
import { href } from "../routes.js";
import { html, raw, pill, n3, dir, empty, day, plural } from "../dom.js";

const COLUMNS = "id,created,parent,accepted,reasons,incumbent_fp,candidate_fp," +
                "baseline,candidate,decision,search";

export async function lineageView({ signal }) {
  const runs = await qAll(`runs?select=${COLUMNS}&order=created.asc`,
                          { signal, pageSize: 200, max: 2000 });
  if (!runs.length) {
    return { title: "Lineage", heading: "Nothing to draw yet",
             body: empty("No generation has been published.") };
  }

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

  const seen = new Set();
  const draw = list => html`<ul class="tree">${list.map(r => {
    if (seen.has(r.id))
      return html`<li><p class="note">${r.id} appears again here — its parent chain loops.
        Drawn once, above.</p></li>`;
    seen.add(r.id);
    const kids = children.get(r.id) || [];
    return html`<li>${node(r, byId)}${kids.length ? draw(kids) : ""}</li>`;
  })}</ul>`;

  const kept = runs.filter(r => r.accepted).length;
  const orphans = roots.filter(r => r.parent && r.parent !== r.id && !byId.has(r.parent));
  const selfish = runs.filter(r => r.parent === r.id);

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

      ${draw(roots)}`,
  };
}

function node(r, byId) {
  const d = r.decision?.holdout?.diff;
  const missingParent = r.parent && r.parent !== r.id && !byId.has(r.parent);
  return html`<a class="row card-row" href="${raw(href.run(r.id))}" data-ok="${r.accepted ? 1 : 0}">
    <span class="mark" aria-hidden="true"></span>
    <span>
      <span class="name">${r.id}
        ${pill(r.accepted ? "kept" : "dropped", r.accepted ? "up" : "down")}
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
