/* One window: one ticker at one instant, which is the unit that gets scored.
 *
 * The forecast, what it was worth, and — when the generation was scored with
 * `--trace` — every step the harness took to produce it.
 */

import { q } from "../data.js";
import { href } from "../routes.js";
import {
  html, raw, pill, n3, n2, usd, cents, dir, empty, stamp, plural,
} from "../dom.js";
import { priceTrack } from "../charts.js";

export async function windowView({ params, signal }) {
  const rid = String(params.id || "");
  if (!/^\d+$/.test(rid))
    return { title: "Window", heading: "No such window",
             body: empty("A window id is a number; this address does not carry one.") };

  // Both keyed on the same id and neither needs the other's answer.
  const [[r], traces] = await Promise.all([
    q(`rollouts?id=eq.${encodeURIComponent(rid)}&select=*`, { signal }),
    q(`traces?rollout_id=eq.${encodeURIComponent(rid)}&select=spans`, { signal }),
  ]);
  if (!r) return { title: "Window", heading: "No such window",
                   body: empty(html`Nothing published with id <code>${rid}</code>.`) };

  const spans = (traces[0] && traces[0].spans) || [];
  const o = r.output || {};
  const covered = r.realised != null && r.predicted != null && r.half_width != null
    && Math.abs(r.realised - r.predicted) <= r.half_width;

  const body = html`
    <div class="grid-2 split">
      <section class="panel">
        <div class="panel-h">
          <h2 class="mono ticker">${r.ticker}</h2>
          ${pill(stamp(r.at))}
          ${pill(r.split, r.split === "holdout" ? "brand" : "")}
          ${pill(r.side === "baseline" ? "incumbent" : "candidate")}
        </div>
        <div class="panel-b">
          ${raw(priceTrack(r))}
          <div class="fc">
            <div class="box"><div class="k">it said</div>
              <div class="big">${cents(o.delta_cents)}</div>
              <p>quoting ±${o.half_width_cents ?? "?"}c${o.confidence != null
                ? `, confidence ${o.confidence}` : ""}${o.reconstructed
                ? " · rebuilt from the score, so no reasoning was kept" : ""}.
                ${r.realised != null && r.half_width
                  ? covered ? "The price printed inside that quote."
                            : "The price printed outside that quote." : ""}</p></div>
            <div class="box"><div class="k">it was worth</div>
              <div class="big ${dir(r.skill)}">${n3(r.skill)}</div>
              <p>against no change${r.unmeasurable
                ? " — but the market did not move, so there was no error to remove" : ""}.
                ${r.err != null && r.naive_error != null
                  ? `Missed by ${n2(r.err * 100)}c where saying nothing would have missed by
                     ${n2(r.naive_error * 100)}c.` : ""}</p></div>
          </div>
          ${r.cost_usd != null ? html`<p class="note">This window cost ${usd(r.cost_usd)}.</p>` : ""}
          ${r.ok === false ? html`<div class="box"><div class="k">the run failed</div>
            <p>${r.error_text || "no reason recorded"} — a failed run is scored as silence,
            never dropped, because dropping it would reward failing on hard windows.</p></div>` : ""}
        </div>
      </section>

      <section class="panel">
        <div class="panel-h"><h2>Its reasoning</h2></div>
        <div class="panel-b">
          ${o.driver ? html`<div class="box"><div class="k">driver</div><p>${o.driver}</p></div>` : ""}
          ${o.falsifier ? html`<div class="box"><div class="k">what would prove it wrong</div>
            <p>${o.falsifier}</p></div>` : ""}
          ${!o.driver && !o.falsifier ? html`<p class="note">This generation was scored without
            <code>--trace</code>, so only the numbers were kept.</p>` : ""}
          ${r.feedback ? html`<div class="box"><div class="k">how it was scored</div>
            <p>${r.feedback}</p></div>` : ""}
        </div>
      </section>
    </div>

    <section class="panel">
      <div class="panel-h"><h2>What it did</h2>${pill(plural(spans.length, "step"))}</div>
      <div class="panel-b">
        ${spans.length ? spans.map(s => html`
          <details class="span" data-k="${s.kind}" ${raw(s.kind === "llm" ? "open" : "")}>
            <summary>
              ${pill(s.kind, s.kind === "llm" ? "brand" : "")}
              <span class="nm">${s.name}</span>
              ${s.error ? pill("error", "down") : ""}
              <span class="meta">${s.duration_s != null ? s.duration_s + "s" : ""}${
                s.cost_usd ? " · $" + s.cost_usd.toFixed(5) : ""}${s.cached ? " · cached" : ""}</span>
            </summary>
            ${s.input != null ? html`<pre>${s.input}</pre>` : ""}
            ${s.output != null ? html`<pre>${s.output}</pre>` : ""}
            ${s.error ? html`<pre class="down">${s.error}</pre>` : ""}
          </details>`)
          : html`<p class="note">No trace was kept for this generation. Traces are written only
            when a run is scored with <code>--trace</code>; they are two orders larger than the
            row they hang off.</p>`}
      </div>
    </section>`;

  return {
    title: r.ticker,
    heading: html`${r.ticker} <span class="crumb">· ${stamp(r.at)}</span>`,
    lead: html`Where this contract's mid price went in the five minutes after that instant,
      and what the harness said it would do.`,
    crumbs: [["generations", href.runs()], [r.run_id, href.run(r.run_id, r.side)], ["window"]],
    body,
  };
}
