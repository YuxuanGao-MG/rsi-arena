/* "Will the next generation be promoted?" — a forecast, on a forecasting site.
 *
 * One open guess per browser; switching sides replaces it. Grading is
 * client-side and literal: a guess is judged against the first *measured* run
 * whose `created` is after the guess was cast — a run that crashed or ran out
 * of money measured nothing, so the guess rides to the one after, the same
 * rule the rest of the site applies to itself. Nothing of another visitor is
 * shown except the aggregate yes/no split and the crowd's hit rate.
 */

import { q, qAll, rpc, ApiError, topicFilter } from "./data.js";
import { html, raw, toHTML, pct, pill, plural } from "./dom.js";
import { runStatus } from "./stats.js";
import { VOTER } from "./voter.js";
import { DEFAULT } from "./topics.js";

/** One open guess per browser per topic: each loop is its own question. */
export async function loadGuess({ signal, runs, topic = DEFAULT }) {
  // qAll, not q: a single page cuts off at PostgREST's row limit, and a voter
  // past row one thousand would be told they never guessed. "Mine" is fetched
  // by its own filter as well, so it cannot fall off any page at all.
  const [guesses, mineRows] = await Promise.all([
    qAll(`guesses?select=created,guess,voter${topicFilter(topic)}&order=created.desc`,
         { signal, fresh: true, max: 50_000 }).catch(() => null),
    q(`guesses?select=created,guess,voter${topicFilter(topic)}` +
      `&voter=eq.${encodeURIComponent(VOTER)}`,
      { signal, fresh: true }).catch(() => []),
  ]);
  return { ...build(guesses, runs, VOTER, mineRows && mineRows[0]), topic };
}

/** Pure, for the tests: guesses + runs (+ who is asking) → the widget's state. */
export function build(guesses, runs, voter = VOTER, mineExact = undefined) {
  if (guesses == null) return { available: false };
  const crowd = { yes: 0, no: 0 };
  let mine = mineExact ?? null;
  for (const g of guesses) {
    (g.guess ? crowd.yes += 1 : crowd.no += 1);
    if (mineExact === undefined && g.voter === voter) mine = g;
  }
  // Runs oldest-first by created, with only the measured ones able to grade.
  const measured = [...(runs || [])]
    .filter(r => runStatus(r) === "complete")
    .sort((a, b) => new Date(a.created) - new Date(b.created));

  const gradeOf = g => {
    const judge = measured.find(r => new Date(r.created) > new Date(g.created));
    return judge ? { run: judge.id, promoted: !!judge.accepted,
                     hit: g.guess === !!judge.accepted } : null;
  };
  const myGrade = mine ? gradeOf(mine) : null;

  // The crowd's record: every current guess old enough to have met a verdict.
  let graded = 0, hits = 0;
  for (const g of guesses) {
    const v = gradeOf(g);
    if (v) { graded += 1; if (v.hit) hits += 1; }
  }
  return { available: true, crowd, mine, myGrade,
           hitRate: graded ? hits / graded : null, graded };
}

export function renderGuess(state) {
  if (!state.available)
    return toHTML(html`<p class="note">The crowd cannot be read right now.</p>`);
  const { crowd, mine, myGrade, hitRate, graded } = state;
  const total = crowd.yes + crowd.no;
  const yesShare = total ? Math.round((crowd.yes / total) * 100) : 50;
  return toHTML(html`
    <p class="guess-q">Will the next generation be promoted?</p>
    <div class="btn-row">
      <button class="btn btn-sm" type="button" data-action="guess" data-guess="true"
        aria-pressed="${String(!!(mine && mine.guess === true))}">yes</button>
      <button class="btn btn-sm" type="button" data-action="guess" data-guess="false"
        aria-pressed="${String(!!(mine && mine.guess === false))}">no</button>
      ${total ? html`<span class="crowd-bar" role="img"
          aria-label="${crowd.yes} yes, ${crowd.no} no">
        <i style="width:${raw(yesShare)}%"></i></span>
        <span class="note tnum">${crowd.yes} yes · ${crowd.no} no</span>` : ""}
    </div>
    ${myGrade ? html`<p class="note">You said ${mine.guess ? "yes" : "no"}; the gate said
      ${myGrade.promoted ? "yes" : "no"} on ${myGrade.run} —
      ${myGrade.hit ? pill("you were right", "up") : pill("you were wrong", "down")}.
      Guess again for the next one.</p>`
    : mine ? html`<p class="note">Your ${mine.guess ? "yes" : "no"} stands until a generation
      measures something. Switching sides replaces it.</p>` : ""}
    ${hitRate != null ? html`<p class="note">The crowd has been right ${pct(hitRate)} of the
      time over ${plural(graded, "graded guess", "graded guesses")}.</p>` : ""}
    <p class="note" id="guess-msg"></p>`);
}

/** Wire the buttons inside `panel`; re-render from each response. */
export function wireGuess(panel, addActions, state) {
  let current = state;
  addActions({
    guess: async el => {
      const buttons = [...panel.querySelectorAll("button")];
      buttons.forEach(b => { b.disabled = true; });
      try {
        // The three-argument overload; the two-argument one still exists for
        // pages published before topics and means Kalshi.
        const out = await rpc("cast_guess",
          { guess: el.dataset.guess === "true", voter: VOTER,
            topic: current.topic || DEFAULT });
        current = { ...current, crowd: { yes: 0, no: 0, ...(out && out.crowd || {}) },
                    mine: { guess: el.dataset.guess === "true",
                            created: new Date().toISOString(), voter: VOTER },
                    myGrade: null };
        panel.innerHTML = renderGuess(current);
      } catch (err) {
        const msg = err instanceof ApiError && err.status === 404
          ? "guessing is not installed on this database yet"
          : rpcMessage(err) || err.message;
        const slot = panel.querySelector("#guess-msg");
        if (slot) slot.textContent = msg;
        buttons.forEach(b => { b.disabled = false; });
      }
    },
  });
}

function rpcMessage(err) {
  try { return JSON.parse(err.detail).message || null; } catch (e) { return null; }
}
