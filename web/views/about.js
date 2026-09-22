/* Methodology, vocabulary, and the honest history.
 *
 * The one page with no numbers to fetch. It exists because the finding — zero
 * promotions across every generation — only means something if a reader can
 * check what would have counted as one, and because the site's own mistakes
 * (a metric that drifted, a gate too small to see what it judged) are part of
 * the record, not blemishes on it.
 */

import { href } from "../routes.js";
import { html, raw } from "../dom.js";
import { TOPICS, TOPIC_IDS, topicOf, DEFAULT, wordsOf } from "../topics.js";

export async function aboutView({ topic = DEFAULT } = {}) {
  const t = topicOf(topic);
  const w = wordsOf(t.id);
  const kalshi = t.id === DEFAULT;
  const body = html`
    <section class="panel"><div class="panel-b prose">
      <h2>What is being measured</h2>
      ${kalshi ? html`<p>A <strong>harness</strong> — an LLM wired to market-data tools by a JSON plan — is put
      back at fixed instants of Kalshi soccer matches, its tools frozen at that instant, and
      asked where the contract's mid price goes in five minutes. The answer is already in the
      candle history, so hundreds of windows score in seconds and cost only model calls.</p>`
      : html`<p>${t.blurb}</p>
      <p>A <strong>harness</strong> — an LLM wired to market-data tools by a JSON plan — is put
      back at a fixed instant, its tools frozen at that instant, and asked where the price goes
      in five minutes, in ${t.unit === "bps" ? "basis points" : "cents"}. The answer is
      already in the price history, so hundreds of ${w.instance}s score in seconds and cost
      only model calls.</p>`}
      <p>Each generation, GEPA rewrites the harness from the traces of the ${w.instance}s it
      lost. The rewrite is promoted only if it beats the incumbent on
      <strong>held-out</strong> ${w.groups} — ${w.groups} neither the rewrite nor its optimizer
      ever saw — with a paired cluster bootstrap putting the difference clear of zero. The gate
      is the only promotion path; the search's own best-on-train is not a result. A new
      generation starts on the ${t.loopWhen} cron and reports its phase as it moves, which is
      what the dot in the header reads.</p>
      <p><strong>Skill</strong> is the fraction of the no-change baseline's error a forecast
      removed. Predicting the price stays put is free and nearly always nearly right, so saying
      nothing scores exactly zero — <strong>silence</strong> — which makes zero a meaningful
      line, not an axis default. The gated statistic is <strong>pooled</strong>: sum the error
      removed, sum the baseline's error, divide${kalshi ? ""
        : html`, with the baseline's error floored at one tick of ${t.tick}
          ${t.unit === "bps" ? "basis points" : "cents"}`}. Splits respect
      <strong>${kalshi ? "fixtures" : w.groups}</strong>, never ${w.instance}s, because fifty
      ${w.instance}s on one ${w.group} are fifty correlated observations of one
      ${w.group === "match" ? "game" : w.group}. A failed run scores as silence rather than
      being dropped, since dropping it would reward failing on the hard ${w.instance}s.</p>
    </div></section>

    <section class="panel"><div class="panel-b prose">
      <h2>The topics</h2>
      <p>Three loops run the same machinery on three questions. Each has its own generations,
      its own archive and its own live collector, and the switcher in the header moves every
      page between them. Skill is comparable across them — it is a fraction of the no-change
      error either way — but the unit is not: a cent of a 0-1 contract and a basis point of a
      quote are different things, and the pages say which they are showing.</p>
      <dl class="glossary-list">
        ${TOPIC_IDS.map(id => html`<dt>${TOPICS[id].title}
            ${id === t.id ? html`<span class="pill brand">this page</span>` : ""}</dt>
          <dd>${TOPICS[id].blurb} Unit: ${TOPICS[id].unit}, tick ${TOPICS[id].tick}.
            <a href="${raw(href.topic(id, "#/about"))}">Read the ${TOPICS[id].title} pages</a>.</dd>`)}
      </dl>
    </div></section>

    <section class="panel"><div class="panel-b prose">
      <h2>The honest history</h2>
      <p><strong>The metric drifted.</strong> The earliest generations were scored under earlier
      versions of the skill formula — one divided by an unfloored benchmark, one paid a full
      point for saying nothing on a market that did not move. Each published figure was the
      gate's statistic on the day it ran, so none is wrong; but they are different questions,
      and this site recomputes every level from each generation's own stored errors on today's
      formula, showing the published figure beside it wherever they differ.</p>
      <p><strong>The early gate could not see what it judged.</strong> On a two-fixture held-out
      set, the smallest difference the bootstrap can separate from noise was larger than any
      gain a rewrite had produced — so those rejections were statements about the sample size,
      not about the candidates. The gate now records its own resolution
      (<strong>underpowered</strong>), and the pages draw it.</p>
      <p><strong>Some generations measured nothing.</strong> One crashed before a verdict
      existed; one ran out of money mid-baseline and recorded hundreds of budget refusals stored
      with the mid echoed back. Refusals are excluded from every pooled number here, and both
      runs render as labelled gaps rather than data points — a nothing drawn at zero reads as a
      break-even.</p>
      <p><strong>Zero promotions is the finding.</strong> The question the arena exists to
      answer is whether an LLM rewriting its own harness from failure traces actually improves
      it under a gate that cannot be argued with. So far the answer is no, and every page of
      <a href="${raw(href.metrics())}">the record</a> is the evidence — including the live
      forecasts, the one place the harness meets a market whose end it has not already read.</p>
    </div></section>

    <section class="panel"><div class="panel-b prose">
      <h2>Vocabulary</h2>
      <dl class="glossary-list">
        <dt>${w.instance}</dt><dd>One ${w.subject} at one instant — the unit that gets scored.</dd>
        <dt>${kalshi ? "fixture" : "group"}</dt><dd>The ${w.group} a ${w.instance} belongs to;
          the unit every split respects.</dd>
        <dt>generation</dt><dd>One run of the loop: baseline, search, gate, verdict.</dd>
        <dt>incumbent / candidate</dt><dd>The harness being defended, and the rewrite
          challenging it.</dd>
        <dt>pooled skill</dt><dd>Sum the error every forecast removed, sum the baseline's
          error, then divide — the gated statistic.</dd>
        <dt>echoed</dt><dd>The forecast repeated the current mid. Worth exactly zero, by
          construction.</dd>
        <dt>probe / cascade</dt><dd>A cheap first pass on a few train matches; a candidate far
          behind the incumbent there is dropped without paying for the full evaluation.</dd>
        <dt>underpowered</dt><dd>The gate's interval could not have resolved a gain of the size
          it was judging.</dd>
        <dt>frontier</dt><dd>Candidates best at something and dominated by nothing — the pool
          the next generation samples its parent from.</dd>
        <dt>fingerprint</dt><dd>A short id computed from a harness's prompt, plan, tools and
          model. Identical harnesses carry identical fingerprints, whatever they are named.</dd>
        <dt>refusal</dt><dd>A window the harness never answered because the budget was already
          spent. Counted, shown, and excluded from every statistic.</dd>
      </dl>
    </div></section>

    <p class="sub">Everything on this site is a select against the same tables the loop writes,
    plus one archive file the search keeps. There is no editorial layer: where a number and a
    manifest disagree, the page says so and shows both.</p>`;

  return {
    title: "About",
    heading: "How the arena works, and what it has actually found",
    lead: html`The methodology, the vocabulary, and the mistakes that are part of the record.`,
    body,
  };
}
