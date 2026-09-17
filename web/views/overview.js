/* The "now" page: the running system first, then the record it stands on.
 *
 * Modelled on Xiaomi's MiMo RL page, where the exhibit is the run itself —
 * the chrome carries its state, the hero says what phase it is in, and the
 * archive is one click deeper rather than the front door. The honesty rules
 * do not relax for the redesign: a dead run says dead, an absent heartbeat
 * says absent, and the training curve draws its gaps as gaps. The countdowns
 * are the one liveness that needs no fetch — a cron is a fact, and the
 * arithmetic is local.
 */

import { href } from "../routes.js";
import {
  html, raw, toHTML, stat, pill, n3, usd, dir, empty, day, plural,
} from "../dom.js";
import { generationSkill } from "../charts.js";
import { loadGenerations } from "../generations.js";
import { onStatus, setOverviewVisible, relative } from "../status.js";
import { tickAll } from "../clock.js";
import { loadGuess, renderGuess, wireGuess } from "../guess.js";
import { addActions } from "../actions.js";

const PHASES = ["baseline", "search", "cascade", "holdout", "audit", "done"];

export async function overviewView({ signal }) {
  const g = await loadGenerations({ signal });
  const latest = g.runs[0] || null;
  const latestStatus = latest ? g.statusOf.get(latest.id) : null;
  // The crowd is a garnish; its absence must never cost the page.
  const guessState = await loadGuess({ signal, runs: g.runs }).catch(() => ({ available: false }));

  const body = html`
    <section class="panel hero-panel">
      <div class="panel-b" id="status-hero" aria-live="off">${raw(heroSkeleton())}</div>
      <div class="panel-b guess" id="guess-panel">${raw(renderGuess(guessState))}</div>
    </section>

    ${g.runs.length ? html`<section class="panel">
      <div class="panel-h">
        <h2>Has any rewrite beaten its incumbent?</h2>
        <span class="pill">held out</span>
        <span class="spacer"></span>
        <a href="${raw(href.metrics())}">every generation</a>
      </div>
      <div class="panel-b">
        <div class="legend">
          <span><i class="dot" style="background:var(--c-inc)"></i> incumbent</span>
          <span><i class="dot" style="background:var(--c-cand)"></i> candidate</span>
          <span><i class="wash"></i> 95% interval</span>
        </div>
        <figure class="chart">
          <div id="gen-skill"></div>
          <figcaption>Zero is silence — saying nothing. A band touching it cannot be promoted.
            A gap measured nothing.</figcaption>
        </figure>
        <details class="table-view">
          <summary>The same numbers as a table</summary>
          <div class="scroll">
            <table>
              <caption>Held-out pooled skill, recomputed on today's metric; interval and
                resolution as the gate recorded them.</caption>
              <thead><tr>
                <th scope="col">generation</th><th scope="col" class="n">incumbent</th>
                <th scope="col" class="n">candidate</th><th scope="col" class="n">difference</th>
                <th scope="col" class="n">95% interval</th>
                <th scope="col" class="n">resolves</th><th scope="col">verdict</th>
              </tr></thead>
              <tbody>${g.points.map(p => html`<tr>
                <th scope="row" class="mono">${p.id}</th>
                ${p.gap ? html`<td colspan="5" class="quiet">no measurement — ${p.gap}</td>
                  <td>${p.gap === "crashed" ? "incomplete" : "ran out of money"}</td>`
                : p.unchanged ? html`<td class="n">${n3(p.inc)}</td>
                  <td colspan="4" class="quiet">both sides forecast identically on every
                    window, so there is no difference to measure</td>
                  <td>dropped</td>`
                : html`<td class="n">${n3(p.inc)}</td>
                  <td class="n">${n3(p.cand)}</td>
                  <td class="n">${n3(p.diff)}</td>
                  <td class="n">${p.low == null ? "—" : `${n3(p.low)} to ${n3(p.high)}`}</td>
                  <td class="n">${p.detectable ? `±${p.detectable.toFixed(3)}` : "—"}</td>
                  <td>${p.accepted ? "promoted" : "dropped"}</td>`}
              </tr>`)}</tbody>
            </table>
          </div>
        </details>
      </div>
    </section>` : empty("No generations published yet.")}

    <section class="panel"><div class="panel-b prose">
      <p>An LLM harness forecasts where a Kalshi soccer price will be five minutes later.
      Each generation, another LLM rewrites it from its failures. A rewrite is promoted only if
      it beats its parent on matches neither ever saw — and after
      ${plural(g.runs.length, "generation")}, none has been.</p>
      <details class="more">
        <summary>the full story</summary>
        <p><strong>Skill</strong> is the fraction of the no-change baseline's error a forecast
        removed. Predicting "no change" is free and nearly always nearly right, so saying
        nothing scores exactly zero — <strong>silence</strong> — and a promotion needs a
        held-out interval clear of it. <strong>Held-out</strong> means matches neither the
        rewrite nor its optimizer saw; splits respect matches, never windows, because fifty
        windows on one match are fifty correlated looks at one game.</p>
        <p>Zero promotions is the finding, not the failure mode. Part of it is now measured:
        the early gate was too small to resolve the effects it was judging.
        <a href="${raw(href.about())}">Methodology and vocabulary</a>.</p>
      </details>
    </div></section>

    ${latest ? verdictCard(latest, latestStatus, g) : ""}`;

  return {
    title: "Overview",
    heading: "A harness that rewrites itself, measured",
    lead: html`What the loop is doing now, and whether any rewrite has ever beaten the harness
      it came from.`,
    body,
    ready: root => {
      if (g.points.length) generationSkill(root.querySelector("#gen-skill"), g.points);
      const hero = root.querySelector("#status-hero");
      // The hero re-renders on every status emit; the unsubscribe rides a
      // teardown event, so the route change that replaces the DOM also stops
      // the feed and hands the GitHub budget back.
      const off = onStatus(s => {
        if (hero.isConnected === false) return off();
        hero.innerHTML = toHTML(heroFor(s, g));
        try { tickAll(hero); } catch (e) { /* stub DOM */ }
      });
      setOverviewVisible(true);
      const guessEl = root.querySelector("#guess-panel");
      if (guessEl) wireGuess(guessEl, addActions, guessState);
      // The crowd refreshes gently while the overview is on screen; the
      // teardown stops it with everything else.
      const refreshGuess = async () => {
        if (typeof document !== "undefined" && document.hidden) return;
        try {
          const next = await loadGuess({ runs: g.runs });
          if (guessEl && guessEl.isConnected !== false) {
            guessEl.innerHTML = renderGuess(next);
            wireGuess(guessEl, addActions, next);
          }
        } catch (e) { /* the next cycle retries */ }
      };
      const guessTimer = setInterval(refreshGuess, 90_000);
      root.addEventListener("view-teardown", () => {
        off(); setOverviewVisible(false); clearInterval(guessTimer);
      }, { once: true });
    },
  };
}

function heroSkeleton() {
  return `<div class="skel" style="width:38%"></div>
          <div class="skel" style="width:60%;margin-top:.7rem"></div>`;
}

/** The status hero. Four honest states plus "reconnecting" laid over any.
 *  Exported so the tests can hold each state without driving the poller. */
export function heroFor(s, g) {
  const note = s.reconnecting
    ? html`<p class="note reconnect">reconnecting — showing the last state that loaded</p>` : "";
  const collecting = s.collection && s.collection.running
    ? html`<p class="note"><span class="status-dot live inline-dot" aria-hidden="true"></span>
        a live collection sweep is running${s.collection.startedAt
          ? html`, started <time data-tick="rel" datetime="${s.collection.startedAt}">${
              relative(s.collection.startedAt)}</time>` : ""}.</p>` : "";

  if (s.kind === "loading") return html`${raw(heroSkeleton())}`;

  if (s.kind === "live") {
    const over = s.budgetUsd && s.spentUsd != null ? Math.min(100,
      Math.round((s.spentUsd / s.budgetUsd) * 100)) : null;
    return html`
      <div class="hero-row">
        <span class="status-dot live" aria-hidden="true"></span>
        <div>
          <p class="eyebrow">running now</p>
          <h2 class="hero-title">${s.run} · ${s.phase} ·
            <time data-tick="elapsed" datetime="${s.startedAt}">…</time> elapsed</h2>
        </div>
      </div>
      <div class="phase-rail" role="img" aria-label="Phase ${s.phase} of ${PHASES.join(", ")}">
        ${PHASES.map(p => html`<span class="phase ${raw(p === s.phase ? "on"
          : PHASES.indexOf(p) < PHASES.indexOf(s.phase) ? "past" : "")}">${p}</span>`)}
      </div>
      <div class="cards hero-cards">
        ${stat({ value: s.spentUsd != null ? usd(s.spentUsd) : "—", label: "spent",
                 note: s.budgetUsd ? `of a ${usd(s.budgetUsd)} ceiling` : "" })}
        ${stat({ value: s.evaluations ?? "—", label: "evaluations",
                 note: "windows the search has paid for" })}
        ${stat({ value: html`<time data-tick="rel" datetime="${s.updatedAt}">${
                   relative(s.updatedAt)}</time>`, label: "last heartbeat" })}
      </div>
      ${over != null ? html`<div class="meter budget-meter" role="img"
          aria-label="${over}% of the budget spent">
        <i style="width:${raw(over)}%"></i></div>` : ""}
      ${collecting}${note}`;
  }

  if (s.kind === "stale") {
    return html`
      <div class="hero-row">
        <span class="status-dot stale" aria-hidden="true"></span>
        <div>
          <p class="eyebrow warn">stale</p>
          <h2 class="hero-title">${s.run} stopped mid-${s.phase}</h2>
        </div>
      </div>
      <p class="prose">Last heartbeat <time data-tick="rel" datetime="${s.lastSeen}">${
        relative(s.lastSeen)}</time>. The run died or lost its database — either way "live"
      would be a lie. Any verdict will land in <a href="${raw(href.metrics())}">the record</a>.</p>
      ${collecting}${note}`;
  }

  if (s.kind === "running-blind") {
    return html`
      <div class="hero-row">
        <span class="status-dot blind" aria-hidden="true"></span>
        <div>
          <p class="eyebrow">running, no heartbeat</p>
          <h2 class="hero-title">a generation is executing</h2>
        </div>
      </div>
      <p class="prose">GitHub reports workflow ${s.ghRun} ${s.ghStatus === "queued"
        ? "queued" : "in progress"}${s.startedAt ? html`, started
        <time data-tick="rel" datetime="${s.startedAt}">${relative(s.startedAt)}</time>` : ""} —
      and no progress row exists, so phase and spend are unknown until it reports or finishes.
      This run predates the heartbeat; nothing here is pretending.</p>
      ${collecting}${note}`;
  }

  // idle
  const latest = g.runs[0];
  const verdict = s.conclusion
    || (latest ? (g.statusOf.get(latest.id) === "complete"
        ? (latest.accepted ? "accepted" : "rejected") : g.statusOf.get(latest.id)) : null);
  return html`
    <div class="hero-row">
      <span class="status-dot idle" aria-hidden="true"></span>
      <div>
        <p class="eyebrow">idle</p>
        <h2 class="hero-title">between generations</h2>
      </div>
    </div>
    <p class="prose">${s.lastRun || latest
      ? html`Last word: <strong>${s.lastRun || latest.id}</strong> —
          ${verdict || "no verdict recorded"}${s.reason ? html`, ${s.reason}` : ""}.`
      : html`No run has reported yet.`}</p>
    <div class="cards hero-cards">
      ${stat({ value: html`<time data-tick="until" data-target="loop">…</time>`,
               label: "next generation", note: "daily at 03:17 UTC, retried 05:47" })}
      ${stat({ value: html`<time data-tick="until" data-target="live">…</time>`,
               label: "next live collection",
               note: "19:05 weekdays · 15:05 weekends · 01:05 daily, UTC" })}
    </div>
    ${collecting}${note}`;
}

/** The latest generation's verdict, as a card rather than a table row. */
function verdictCard(r, status, g) {
  const hold = r.decision?.holdout || {};
  const mine = g.level.get(r.id) || {};
  const inc = mine.baseline?.skill ?? r.baseline?.holdout?.statistic;
  const cand = mine.candidate?.skill ?? r.candidate?.holdout?.statistic;
  return html`<section class="panel">
    <div class="panel-h"><h2>The latest generation</h2>
      ${status === "incomplete" ? pill("incomplete", "warn")
        : status === "exhausted" ? pill("ran out of money", "warn")
        : pill(r.accepted ? "promoted" : "dropped", r.accepted ? "up" : "down")}
      <span class="spacer"></span>
      <a href="${raw(href.run(r.id))}">read it in full</a></div>
    <div class="panel-b">
      <p class="prose"><strong>${r.id}</strong> · ${day(r.created)} —
        ${(r.reasons || [])[0] || "no reasons recorded"}</p>
      ${status === "complete" ? html`<div class="cards">
        ${stat({ value: n3(inc), tone: dir(inc), label: "incumbent, held out" })}
        ${stat({ value: n3(cand), tone: dir(cand), label: "candidate, held out" })}
        ${stat({ value: n3(hold.diff), tone: dir(hold.diff), label: "difference",
                 note: hold.low != null && hold.usable !== false
                   ? `${n3(hold.low)} to ${n3(hold.high)}` : "no usable interval" })}
      </div>` : html`<p class="note">No held-out measurement exists for this generation.</p>`}
    </div></section>`;
}
