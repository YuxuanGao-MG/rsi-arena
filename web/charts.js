/* The pictures.
 *
 * The page this replaces had two: a six-pixel bar and a forty-six-pixel price
 * track. Everything else about a measurable quantity over time was told in
 * `toFixed(3)`. These four are the ones that carry the argument:
 *
 *   generationSkill   did any rewrite beat its incumbent, and by more than the
 *                     interval the gate draws around the difference
 *   predictedVsRealised  what the harness said against what printed, with the
 *                     wedge inside which it beat saying nothing
 *   skillHistogram    where the mass sits — and it sits on exactly zero
 *   sparkline         the same shape, small enough for a list row
 *   equityCurve       a paper book's equity against its own running peak, so
 *                     every drawdown is a shaded gap rather than a number
 *
 * Marks are deliberately thin and the grid is a hairline: the data is the only
 * thing allowed to be loud. The colours are two validated categorical steps
 * (--c-inc, --c-cand), not the brand teal, which is too low-chroma to separate
 * from a graphite under deuteranopia.
 *
 * Charts re-render at the width they are actually given rather than scaling a
 * fixed viewBox, because a viewBox that fits a phone renders 5px axis labels.
 */

import { esc, n3, clock, day, stamp } from "./dom.js";
import { topicOf, moveOf, saidOf, fmtMove, fmtPrice, fmtUsd, fmtPct } from "./topics.js";

const observers = [];

/** Every observer a route created, dropped when the route changes. */
export function clearCharts() {
  for (const o of observers) o.disconnect();
  observers.length = 0;
}

function host(el, render) {
  if (!el) return;
  el.classList.add("chart-host");
  const tip = document.createElement("div");
  tip.className = "chart-tip";
  tip.hidden = true;
  let last = -1;
  const paint = () => {
    const w = Math.max(260, Math.round(el.clientWidth || 640));
    if (w === last) return;
    last = w;
    el.innerHTML = render(w);
    el.appendChild(tip);
  };
  paint();
  if (typeof ResizeObserver === "function") {
    const ro = new ResizeObserver(paint);
    ro.observe(el);
    observers.push(ro);
  }

  // Hover and focus show the same thing, so a keyboard reaches every value —
  // and every value is in the table view underneath regardless.
  const show = ev => {
    const target = ev.target.closest && ev.target.closest("[data-tip]");
    if (!target) return;
    tip.textContent = target.getAttribute("data-tip");
    tip.hidden = false;
    const box = target.getBoundingClientRect(), frame = el.getBoundingClientRect();
    const left = box.left - frame.left + box.width / 2;
    tip.style.left = `${Math.max(8, Math.min(frame.width - 8, left))}px`;
    tip.style.top = `${Math.max(0, box.top - frame.top - 10)}px`;
  };
  const hide = () => { tip.hidden = true; };
  el.addEventListener("pointerover", show);
  el.addEventListener("pointerout", hide);
  el.addEventListener("focusin", show);
  el.addEventListener("focusout", hide);
}

/* ---------- small maths --------------------------------------------------- */

function niceTicks(lo, hi, count = 5) {
  if (!(hi > lo)) { hi = lo + 1; }
  const span = hi - lo;
  const step0 = span / Math.max(1, count);
  const mag = 10 ** Math.floor(Math.log10(step0));
  const step = [1, 2, 2.5, 5, 10].map(m => m * mag).find(s => s >= step0) || mag * 10;
  const out = [];
  for (let t = Math.ceil(lo / step) * step; t <= hi + step / 1e6; t += step)
    out.push(Math.abs(t) < step / 1e6 ? 0 : t);
  return out;
}

const pad = (lo, hi, frac = 0.12) => {
  const span = Math.max(hi - lo, 1e-6);
  return [lo - span * frac, hi + span * frac];
};

/* ---------- 1. skill across generations ----------------------------------- */

/**
 * One dumbbell per generation: incumbent, candidate, and the bootstrap
 * interval the gate draws around the *difference* — which is why the band is
 * anchored on the incumbent rather than on the candidate. A band that crosses
 * the silence line is a generation that cannot be promoted, and so far every
 * band does.
 *
 * The whiskers, where a generation has them, are what this test could resolve
 * at all: on a two-fixture held-out set the smallest gap the bootstrap can
 * separate from noise is larger than any gain anyone has found, so the
 * rejection was a statement about the sample size rather than about the
 * candidate. Drawing it is the difference between "the rewrite failed" and
 * "nobody could have told".
 *
 * Takes points rather than run records because the levels are recomputed on
 * one metric first — see stats.js:recompute.
 */
export function generationSkill(el, points) {
  host(el, width => {
    // Right margin wide enough for the zero line's own label. "silence" used to
    // be printed inside the plot at the right edge, over the newest generation -
    // the one a reader looks at first - and at the left it covered the oldest.
    const H = 330, m = { t: 34, r: 54, b: 46, l: 66 };
    const plotW = width - m.l - m.r, plotH = H - m.t - m.b;
    // The scale comes from the levels, and the whiskers are allowed off the top
    // and bottom. On the real Kalshi record the first eleven generations were
    // scored on a few dozen windows and carry intervals ten times anything
    // since, so a min-to-max scale flattened the twenty-five generations that
    // followed into a single line a pixel thick. A clipped whisker says it is
    // clipped; a squashed chart says nothing at all.
    const levels = [0];
    const spans = [];
    for (const p of points) {
      for (const v of [p.inc, p.cand]) if (v != null) levels.push(v);
      if (p.inc != null && p.low != null) spans.push(p.inc + p.low, p.inc + p.high);
    }
    // A percentile of both, not the extremes of either. The Kalshi record holds
    // two lineages: eleven Opus generations scored on a few dozen windows, whose
    // levels reach -0.057 and whose intervals reach -0.11, and then twenty-five
    // Jev ones that all sit between -0.015 and zero. Scaled to the extremes the
    // live era - the one the page is asking about - was a line one pixel thick.
    // Anything outside the frame is drawn as an arrow at the edge and carries
    // its real number in the tooltip, so nothing is hidden, only moved.
    const quantile = (xs, q) => {
      const a = [...xs].sort((u, v) => u - v);
      return a.length ? a[Math.min(a.length - 1, Math.max(0, Math.round(q * (a.length - 1))))] : 0;
    };
    const want = [0, quantile(levels, 0.05), quantile(levels, 0.95),
                  quantile(spans, 0.10), quantile(spans, 0.90)];
    const [lo, hi] = pad(Math.min(...want), Math.max(...want), 0.18);
    const y = v => m.t + plotH - ((v - lo) / (hi - lo)) * plotH;
    const clamp = v => Math.min(m.t + plotH, Math.max(m.t, y(v)));
    const band = plotW / Math.max(1, points.length);
    const cx = i => m.l + band * (i + 0.5);
    // How dense this has become. Thirty generations in a 1,700px panel leaves
    // about fifty pixels each, and `gen12@kalshi-jev` needs ninety - which is
    // how every label on this chart came to be printed on top of every other.
    const crowded = band < 80;
    const every = Math.max(1, Math.ceil(points.length / Math.max(2, Math.floor(plotW / 78))));

    // Where the lineage changes. The Kalshi topic holds two - eleven Opus
    // generations under `runs/`, then the Jev ones under `runs/kalshi-jev/` -
    // and each numbers from one, so the axis read "gen1 ... gen11, gen1, gen3
    // ...": two different generations with one name, eight columns apart. The
    // lineage is said once, where it starts, instead of on every label.
    const lineageOf = id => (String(id).split("@")[1] || "").replace(/-horizon.*|-equity.*/, "");
    const breaks = points.map((p, i) => [i, lineageOf(p.id)])
      .filter(([i, lg], n) => n > 0 && lg !== lineageOf(points[i - 1].id));
    const eras = breaks.map(([i, lg]) => {
      const x = m.l + band * i;
      return `<line x1="${x.toFixed(1)}" x2="${x.toFixed(1)}" y1="${m.t}" y2="${m.t + plotH}"
          stroke="var(--line)" stroke-width="1" stroke-dasharray="1 4"/>
        <text x="${(x + 5).toFixed(1)}" y="${(m.t + 11).toFixed(1)}" font-size="10"
          fill="var(--faint)">${esc(lg || "original")}</text>`;
    }).join("");

    // Promotion is the one event this chart exists to show, and it happens
    // twice in thirty-seven. A triangle under the axis was not enough: it is
    // the same size and nearly the same green as a candidate clipped at the
    // bottom edge. The whole column is washed instead, which nothing else does.
    const promoted = points.map((p, i) => p.accepted
      ? `<rect x="${(cx(i) - band / 2).toFixed(1)}" y="${m.t}" width="${band.toFixed(1)}"
           height="${plotH}" fill="var(--up)" fill-opacity="0.09"/>`
      : "").join("");

    const ticks = niceTicks(lo, hi, 5);
    // Enough decimals for the ticks to differ. Two was right when a skill of
    // 0.02 was a big number and wrong once the live era sat inside one
    // hundredth: the axis read "0.00, -0.00, -0.01, -0.01", four labels naming
    // two values. And -0.00 is not a number anybody writes.
    const step = Math.abs((ticks[1] ?? 0) - (ticks[0] ?? 1)) || Math.abs(hi - lo) / 5;
    const dp = Math.min(6, Math.max(2, Math.ceil(-Math.log10(step)) + 1));
    const tickText = t => {
      const out = t.toFixed(dp);
      return /^-0\.0*$/.test(out) ? out.slice(1) : out;
    };
    const grid = ticks.map(t => `
      <line x1="${m.l}" x2="${m.l + plotW}" y1="${y(t).toFixed(1)}" y2="${y(t).toFixed(1)}"
            stroke="var(--c-grid)" stroke-width="1"/>
      <text x="${m.l - 10}" y="${(y(t) + 4).toFixed(1)}" text-anchor="end"
            font-size="11" fill="var(--faint)" class="tnum">${tickText(t)}</text>`).join("");

    // "silence" sits at the left, against the axis. At the right it was printed
    // over the newest generation - the one a reader looks at first.
    const zero = `
      <line x1="${m.l}" x2="${m.l + plotW}" y1="${y(0).toFixed(1)}" y2="${y(0).toFixed(1)}"
            stroke="var(--c-zero)" stroke-width="1.5"/>
      <text x="${m.l + plotW + 6}" y="${(y(0) + 4).toFixed(1)}" text-anchor="start"
            font-size="11" font-weight="600" fill="var(--soft)">silence</text>`;

    // The incumbent is one harness carried forward, so it is a line, not thirty
    // unrelated dots. Drawn in segments: a crashed generation measured nothing,
    // and joining across it would draw a trend through a hole.
    const inFrame = v => v != null && y(v) >= m.t && y(v) <= m.t + plotH;
    const runsOf = [];
    let current = [];
    points.forEach((p, i) => {
      // Broken at a hole and at a level off the chart alike: a line drawn to a
      // clamped point is a flat stretch that never happened.
      if (p.gap || !inFrame(p.inc)) { if (current.length) runsOf.push(current); current = []; return; }
      current.push([cx(i), y(p.inc)]);
    });
    if (current.length) runsOf.push(current);
    const incLine = runsOf.map(seg => seg.length === 1
      ? `<circle cx="${seg[0][0].toFixed(1)}" cy="${seg[0][1].toFixed(1)}" r="2.5"
           fill="var(--c-inc)"/>`
      : `<polyline points="${seg.map(([a, b]) => `${a.toFixed(1)},${b.toFixed(1)}`).join(" ")}"
           fill="none" stroke="var(--c-inc)" stroke-width="2" stroke-linejoin="round"/>`).join("");

    const marks = points.map((p, i) => {
      const x = cx(i);
      const parts = [];
      const label = esc(p.id.replace(/@.*$/, ""));
      const shown = i === points.length - 1 || i % every === 0;
      if (p.gap) {
        // A hole in the record, not a data point on the silence line: drawn as
        // one it reads as "the latest rewrite broke even".
        parts.push(`<line x1="${x}" x2="${x}" y1="${m.t + 8}" y2="${m.t + plotH - 8}"
          stroke="var(--warn)" stroke-width="1.5" stroke-dasharray="2 5" stroke-linecap="round"/>`);
        if (!crowded) {
          const mid = m.t + plotH / 2;
          parts.push(`<text x="${x}" y="${(mid - 4).toFixed(1)}" text-anchor="middle"
            font-size="11" font-weight="600" fill="var(--warn)">no</text>`);
          parts.push(`<text x="${x}" y="${(mid + 10).toFixed(1)}" text-anchor="middle"
            font-size="11" font-weight="600" fill="var(--warn)">measurement</text>`);
        }
      } else {
        if (p.inc != null && p.low != null && p.high != null) {
          // The interval, as a whisker rather than a block. A block twenty-two
          // pixels wide beside a fifty-pixel slot reads as a bar chart of
          // something, and it is not a quantity - it is where the difference
          // could be.
          const rawTop = y(p.inc + p.high), rawBottom = y(p.inc + p.low);
          const top = clamp(p.inc + p.high), bottom = clamp(p.inc + p.low);
          const cutTop = rawTop < m.t + 0.5, cutBottom = rawBottom > m.t + plotH - 0.5;
          const w = Math.min(7, Math.max(3, band * 0.16));
          // Dashed when the test was too small to have resolved the difference
          // it was looking for. That used to be two orange rules per column,
          // drawn for all twenty-nine and sitting about where the whisker ends
          // already were - the loudest ink on the chart, saying roughly what
          // the whisker said. It is a property of the whisker, so it is drawn
          // as one; the number it came from is in the tooltip and the table.
          const dash = p.underpowered ? ' stroke-dasharray="3 3"' : "";
          const colour = p.underpowered ? "--warn" : "--c-cand";
          parts.push(`<line x1="${x}" x2="${x}" y1="${top.toFixed(1)}" y2="${bottom.toFixed(1)}"
            stroke="var(${colour})" stroke-opacity="0.5" stroke-width="2"
            stroke-linecap="round"${dash}/>`);
          for (const [edge, cut, dir] of [[top, cutTop, -1], [bottom, cutBottom, 1]]) {
            if (cut)
              // An arrowhead rather than a cap: this whisker leaves the chart.
              parts.push(`<path d="M ${x} ${(edge + dir * 1).toFixed(1)} l ${w} ${(dir * 6)} l ${-2 * w} 0 Z"
                fill="var(${colour})" fill-opacity="0.5"/>`);
            else
              parts.push(`<line x1="${(x - w).toFixed(1)}" x2="${(x + w).toFixed(1)}"
                y1="${edge.toFixed(1)}" y2="${edge.toFixed(1)}"
                stroke="var(${colour})" stroke-opacity="0.5" stroke-width="1.5"/>`);
          }
        }
        for (const [v, colour, r] of [[p.inc, "--c-inc", crowded ? 3 : 4],
                                      [p.cand, "--c-cand", crowded ? 3.5 : 5]]) {
          if (v == null) continue;
          if (inFrame(v)) {
            parts.push(`<circle cx="${x}" cy="${y(v).toFixed(1)}" r="${r}"
              fill="var(${colour})" stroke="var(--panel)" stroke-width="1.5"/>`);
          } else {
            // Off the top or the bottom. One rule for every mark that leaves the
            // frame, so a reader learns it once.
            const dir = y(v) < m.t ? -1 : 1;
            const edge = dir < 0 ? m.t + 1 : m.t + plotH - 1;
            parts.push(`<path d="M ${x} ${(edge + dir * 2).toFixed(1)} l 5 ${-dir * 7} l -10 0 Z"
              fill="var(${colour})" stroke="var(--panel)" stroke-width="1"/>`);
          }
        }
        // Promotion is the event worth a mark. "dropped" under all twenty-nine
        // of them was thirty words saying nothing happened; the caption says it
        // once, with a number.
        // The washed column and the triangle say it; a word over each said it
        // twice and, on two consecutive promotions, printed itself.
        if (p.accepted)
          parts.push(`<path d="M ${x} ${m.t + plotH + 4} l 5.5 9 l -11 0 Z" fill="var(--up)"/>`);
        if (points.length <= 6) {
          if (p.cand != null)
            parts.push(`<text x="${x}" y="${(y(p.cand) - 12).toFixed(1)}" text-anchor="middle"
              font-size="11" font-weight="600" fill="var(--soft)" class="tnum">${n3(p.cand)}</text>`);
          if (p.inc != null && Math.abs(y(p.inc) - y(p.cand ?? p.inc)) > 4)
            parts.push(`<text x="${x}" y="${(y(p.inc) + 19).toFixed(1)}" text-anchor="middle"
              font-size="11" fill="var(--faint)" class="tnum">${n3(p.inc)}</text>`);
        }
      }
      // Every Nth, so a label is a label rather than a smear. The rest are in
      // the tooltip and in the table under the chart, both of which name every
      // generation.
      if (shown)
        parts.push(`<text x="${x}" y="${m.t + plotH + 26}" text-anchor="middle"
          font-size="11" fill="var(--soft)">${label}</text>`);

      const tip = p.gap
        ? `${p.id}: ${p.gap} — no held-out measurement exists for this generation`
        : `${p.id}: incumbent ${n3(p.inc)}, candidate ${n3(p.cand)}` +
          (p.low != null ? `, difference ${n3(p.diff)} (${n3(p.low)} to ${n3(p.high)})` : "") +
          (p.detectable ? `. This test could only resolve ${n3(p.detectable)}` : "") +
          `. ${p.accepted ? "Promoted" : "Not promoted"}`;
      parts.push(`<g tabindex="0" data-tip="${esc(tip)}" role="img" aria-label="${esc(tip)}">
        <rect x="${(x - band / 2).toFixed(1)}" y="${m.t}" width="${band.toFixed(1)}"
              height="${plotH}" fill="transparent"/></g>`);
      return parts.join("");
    }).join("");

    return `<svg viewBox="0 0 ${width} ${H}" width="${width}" height="${H}" role="group"
      aria-label="Pooled held-out skill for each generation, incumbent against candidate">
      <text x="${m.l - 48}" y="18" font-size="11" fill="var(--faint)">pooled held-out skill</text>
      ${grid}${promoted}${zero}${eras}${incLine}${marks}
      <line x1="${m.l}" x2="${m.l + plotW}" y1="${m.t + plotH}" y2="${m.t + plotH}"
            stroke="var(--line)" stroke-width="1"/>
    </svg>`;
  });
}

/* ---------- 2. predicted against realised --------------------------------- */

/**
 * Both axes are the move in the topic's unit — cents of a contract price,
 * basis points of a quote — not the price, so the two lines that matter are
 * drawable: the diagonal is the move called exactly, and the horizontal is
 * saying nothing. Between them is the wedge where the forecast removed error
 * from the no-change benchmark — which is the definition of skill, drawn.
 */
export function predictedVsRealised(el, rows, topic) {
  const t = topicOf(topic);
  const unit = t.unit === "bps" ? "basis points" : "cents";
  const pts = rows
    .filter(r => r.mid_now != null && r.realised != null && r.predicted != null)
    .slice(0, 1500)
    .map(r => ({
      x: moveOf(r, t.id),
      y: saidOf(r, t.id),
      skill: r.skill, ticker: r.symbol || r.ticker, at: r.at,
    }));

  host(el, width => {
    const H = Math.min(420, Math.max(280, width * 0.62));
    const m = { t: 22, r: 22, b: 46, l: 52 };
    const plotW = width - m.l - m.r, plotH = H - m.t - m.b;
    const reach = Math.max(2, ...pts.map(p => Math.max(Math.abs(p.x), Math.abs(p.y)))) * 1.08;
    const x = v => m.l + ((v + reach) / (2 * reach)) * plotW;
    const y = v => m.t + plotH - ((v + reach) / (2 * reach)) * plotH;

    const ticks = niceTicks(-reach, reach, 5);
    const grid = ticks.map(t => `
      <line x1="${x(t).toFixed(1)}" x2="${x(t).toFixed(1)}" y1="${m.t}" y2="${m.t + plotH}"
            stroke="var(--c-grid)" stroke-width="1"/>
      <line x1="${m.l}" x2="${m.l + plotW}" y1="${y(t).toFixed(1)}" y2="${y(t).toFixed(1)}"
            stroke="var(--c-grid)" stroke-width="1"/>
      <text x="${x(t).toFixed(1)}" y="${m.t + plotH + 18}" text-anchor="middle"
            font-size="11" fill="var(--faint)" class="tnum">${t}</text>
      <text x="${m.l - 8}" y="${(y(t) + 4).toFixed(1)}" text-anchor="end"
            font-size="11" fill="var(--faint)" class="tnum">${t}</text>`).join("");

    // The wedge: every (x, y) with y between 0 and 2x beats no change.
    const wedge = `<path d="M ${x(0)} ${y(0)} L ${x(reach)} ${y(0)} L ${x(reach)} ${y(2 * reach)} Z
                            M ${x(0)} ${y(0)} L ${x(-reach)} ${y(0)} L ${x(-reach)} ${y(-2 * reach)} Z"
      fill="var(--c-cand)" fill-opacity="var(--c-wash)"/>`;

    const dots = pts.map(p => {
      const better = (p.skill ?? 0) > 0;
      const tip = `${p.ticker} ${clock(p.at)} · printed ${fmtMove(p.x, t.id)}, ` +
        `said ${fmtMove(p.y, t.id)}, skill ${n3(p.skill)}`;
      return `<g tabindex="0" data-tip="${esc(tip)}" role="img" aria-label="${esc(tip)}">
        <circle cx="${x(p.x).toFixed(1)}" cy="${y(p.y).toFixed(1)}" r="11" fill="transparent"/>
        <circle cx="${x(p.x).toFixed(1)}" cy="${y(p.y).toFixed(1)}" r="3.5"
          fill="${better ? "var(--c-cand)" : "none"}" fill-opacity="${better ? ".85" : "0"}"
          stroke="${better ? "var(--panel)" : "var(--down)"}" stroke-width="${better ? 1 : 1.5}"/>
      </g>`;
    }).join("");

    return `<svg viewBox="0 0 ${width} ${H}" width="${width}" height="${H}" role="group"
      aria-label="What the harness said against what printed, in ${unit} of move">
      ${grid}${wedge}
      <line x1="${x(-reach)}" x2="${x(reach)}" y1="${y(-reach)}" y2="${y(reach)}"
            stroke="var(--soft)" stroke-width="1.5"/>
      <line x1="${m.l}" x2="${m.l + plotW}" y1="${y(0).toFixed(1)}" y2="${y(0).toFixed(1)}"
            stroke="var(--c-zero)" stroke-width="1.5"/>
      <text x="${x(reach) - 4}" y="${y(reach) + 14}" text-anchor="end" font-size="11"
            font-weight="600" fill="var(--soft)">called it exactly</text>
      <text x="${m.l + 4}" y="${(y(0) - 8).toFixed(1)}" font-size="11" font-weight="600"
            fill="var(--soft)">silence</text>
      ${dots}
      <text x="${m.l + plotW}" y="${H - 6}" text-anchor="end" font-size="11"
            fill="var(--faint)">what printed, ${unit}</text>
      <text x="${m.l - 40}" y="${m.t - 8}" font-size="11" fill="var(--faint)">what it said, ${unit}</text>
    </svg>`;
  });
}

/* ---------- 3. distribution of per-window skill --------------------------- */

/**
 * Exactly zero gets its own column. Half of all forecasts echo the current mid,
 * and a histogram that bins them with "nearly zero" hides the single loudest
 * fact about this harness.
 */
export function skillHistogram(el, rows) {
  const scored = rows.filter(r => r.skill != null);
  const silent = scored.filter(r => Math.abs(r.skill) < 1e-9).length;
  const BINS = 10, LIMIT = 1;
  const bins = Array.from({ length: BINS }, () => 0);
  for (const r of scored) {
    if (Math.abs(r.skill) < 1e-9) continue;
    const clipped = Math.max(-LIMIT, Math.min(LIMIT, r.skill));
    const idx = Math.min(BINS - 1, Math.floor(((clipped + LIMIT) / (2 * LIMIT)) * BINS));
    bins[idx] += 1;
  }
  const peak = Math.max(silent, ...bins, 1);

  host(el, width => {
    const H = 260, m = { t: 20, r: 18, b: 54, l: 46 };
    const plotW = width - m.l - m.r, plotH = H - m.t - m.b;
    const slots = BINS + 1;                       // the bins, plus the zero column
    const band = plotW / slots;
    const barW = Math.min(24, band - 6);
    const y = v => m.t + plotH - (v / peak) * plotH;

    const column = (i, count, colour, label, tip) => {
      const cx = m.l + band * (i + 0.5);
      const h = Math.max(count > 0 ? 2 : 0, plotH - (y(count) - m.t));
      return `<g tabindex="0" data-tip="${esc(tip)}" role="img" aria-label="${esc(tip)}">
        <rect x="${(cx - band / 2).toFixed(1)}" y="${m.t}" width="${band.toFixed(1)}"
              height="${plotH}" fill="transparent"/>
        <rect x="${(cx - barW / 2).toFixed(1)}" y="${(m.t + plotH - h).toFixed(1)}"
              width="${barW.toFixed(1)}" height="${h.toFixed(1)}" rx="4" fill="${colour}"/>
        ${count ? `<text x="${cx.toFixed(1)}" y="${(m.t + plotH - h - 6).toFixed(1)}"
          text-anchor="middle" font-size="11" fill="var(--faint)" class="tnum">${count}</text>` : ""}
        <text x="${cx.toFixed(1)}" y="${m.t + plotH + 18}" text-anchor="middle" font-size="10"
          fill="var(--faint)">${esc(label)}</text>
      </g>`;
    };

    const zeroCol = column(0, silent, "var(--c-zero)", "0",
      `${silent} windows scored exactly zero — the forecast was the mid, so it removed no error`);
    const rest = bins.map((count, i) => {
      const from = -LIMIT + (2 * LIMIT * i) / BINS, to = from + (2 * LIMIT) / BINS;
      const label = i === 0 ? "≤ -0.8" : i === BINS - 1 ? "≥ +0.8" : (from + 0.1).toFixed(1);
      return column(i + 1, count, "var(--c-cand)", label,
        `${count} windows with skill between ${from.toFixed(2)} and ${to.toFixed(2)}`);
    }).join("");

    return `<svg viewBox="0 0 ${width} ${H}" width="${width}" height="${H}" role="group"
      aria-label="How many windows scored what skill">
      <text x="${m.l - 38}" y="14" font-size="11" fill="var(--faint)">windows</text>
      ${zeroCol}${rest}
      <line x1="${m.l}" x2="${m.l + plotW}" y1="${m.t + plotH}" y2="${m.t + plotH}"
            stroke="var(--line)" stroke-width="1"/>
      <text x="${(m.l + band / 2).toFixed(1)}" y="${H - 20}" text-anchor="middle" font-size="11"
            font-weight="600" fill="var(--soft)">said nothing</text>
      <text x="${m.l + plotW}" y="${H - 6}" text-anchor="end" font-size="11"
            fill="var(--faint)">skill per window, worse ← → better</text>
    </svg>`;
  });
}

/* ---------- 4. sparkline --------------------------------------------------- */

/**
 * Every held-out window of one generation, worst to best, against the silence
 * line. Small enough for a list row and shaped enough to tell two generations
 * apart at a glance.
 */
export function sparkline(values, { label = "" } = {}) {
  const v = values.filter(x => x != null).sort((a, b) => a - b);
  if (v.length < 2) return "";
  const W = 120, H = 28, reach = Math.max(0.2, ...v.map(Math.abs));
  const x = i => (i / (v.length - 1)) * W;
  const y = s => H / 2 - (s / reach) * (H / 2 - 2);
  const line = v.map((s, i) => `${i ? "L" : "M"}${x(i).toFixed(1)} ${y(s).toFixed(1)}`).join(" ");
  const area = `${line} L${W} ${(H / 2).toFixed(1)} L0 ${(H / 2).toFixed(1)} Z`;
  return `<span class="spark" role="img" aria-label="${esc(label)}">
    <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" focusable="false">
      <path d="${area}" fill="var(--c-cand)" fill-opacity="var(--c-wash)"/>
      <line x1="0" x2="${W}" y1="${H / 2}" y2="${H / 2}" stroke="var(--c-zero)" stroke-width="1"
            vector-effect="non-scaling-stroke"/>
      <path d="${line}" fill="none" stroke="var(--c-cand)" stroke-width="2"
            stroke-linejoin="round" vector-effect="non-scaling-stroke"/>
    </svg></span>`;
}

/* ---------- 5. one window's prices ---------------------------------------- */

/** mid, what it said with its quote, and what printed — on one line, to scale.
 * Prices are printed in the topic's precision: three decimals for a 0-1
 * contract, two for a quote, none for one in the thousands. */
export function priceTrack(r, topic) {
  const t = topicOf(topic ?? r);
  const price = v => fmtPrice(v, t.id);
  const pts = [["mid", r.mid_now, "var(--ghost)"],
               ["said", r.predicted, "var(--c-cand)"],
               ["printed", r.realised, "var(--ink)"]].filter(p => p[1] != null);
  if (pts.length < 2) return "";
  const W = 620, H = 76;
  const vals = pts.map(p => p[1]);
  const half = r.half_width || 0;
  let lo = Math.min(...vals, (r.predicted ?? vals[0]) - half);
  let hi = Math.max(...vals, (r.predicted ?? vals[0]) + half);
  const room = Math.max(0.008, (hi - lo) * 0.35);
  lo -= room; hi += room;
  const at = v => 40 + ((v - lo) / (hi - lo)) * (W - 80);

  const quote = r.predicted != null && half
    ? `<rect x="${at(r.predicted - half).toFixed(1)}" y="30"
             width="${(at(r.predicted + half) - at(r.predicted - half)).toFixed(1)}" height="16"
             rx="4" fill="var(--c-cand)" fill-opacity="var(--c-wash)"/>` : "";

  return `<svg class="track" viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet"
    role="img" aria-label="Mid ${price(r.mid_now)}, forecast ${price(r.predicted)}, printed ${price(r.realised)}">
    <line x1="24" x2="${W - 24}" y1="38" y2="38" stroke="var(--line)" stroke-width="1"/>
    ${quote}
    ${pts.map(([k, v, colour]) => `
      <line x1="${at(v).toFixed(1)}" x2="${at(v).toFixed(1)}" y1="28" y2="48"
            stroke="${colour}" stroke-width="2.5" stroke-linecap="round"/>
      <text x="${at(v).toFixed(1)}" y="18" text-anchor="middle" font-size="12"
            font-weight="600" fill="var(--soft)">${k}</text>
      <text x="${at(v).toFixed(1)}" y="66" text-anchor="middle" font-size="12"
            fill="var(--faint)" class="tnum">${price(v)}</text>`).join("")}
  </svg>`;
}

/* ---------- 6. spend against a ceiling ------------------------------------- */

/** One column per generation, with the ceiling as the one reference line. */
export function spendByGeneration(el, runs, ceiling) {
  host(el, width => {
    const H = 260, m = { t: 24, r: 20, b: 52, l: 56 };
    const plotW = width - m.l - m.r, plotH = H - m.t - m.b;
    const spends = runs.map(r => r.llm?.spent_usd || 0);
    const top = Math.max(...spends, ceiling || 0, 1) * 1.12;
    const band = plotW / Math.max(1, runs.length);
    const barW = Math.min(46, band - 18);
    const y = v => m.t + plotH - (v / top) * plotH;

    const ticks = niceTicks(0, top, 4);
    const grid = ticks.map(t => `
      <line x1="${m.l}" x2="${m.l + plotW}" y1="${y(t).toFixed(1)}" y2="${y(t).toFixed(1)}"
            stroke="var(--c-grid)" stroke-width="1"/>
      <text x="${m.l - 10}" y="${(y(t) + 4).toFixed(1)}" text-anchor="end" font-size="11"
            fill="var(--faint)" class="tnum">$${t.toFixed(t < 10 ? 1 : 0)}</text>`).join("");

    const bars = runs.map((r, i) => {
      const spent = r.llm?.spent_usd || 0;
      const own = r._ceiling ?? ceiling;         // each run against its own, when it has one
      const cx = m.l + band * (i + 0.5);
      const h = Math.max(2, plotH - (y(spent) - m.t));
      const over = own && spent > own;
      const tip = `${r.id}: $${spent.toFixed(2)}` +
        (own ? ` of a $${own.toFixed(2)} ceiling` : " (no ceiling recorded)");
      return `<g tabindex="0" data-tip="${esc(tip)}" role="img" aria-label="${esc(tip)}">
        <rect x="${(cx - band / 2).toFixed(1)}" y="${m.t}" width="${band.toFixed(1)}"
              height="${plotH}" fill="transparent"/>
        <rect x="${(cx - barW / 2).toFixed(1)}" y="${(m.t + plotH - h).toFixed(1)}"
              width="${barW.toFixed(1)}" height="${h.toFixed(1)}" rx="4"
              fill="var(${over ? "--down" : "--c-cand"})"/>
        <text x="${cx.toFixed(1)}" y="${(m.t + plotH - h - 6).toFixed(1)}" text-anchor="middle"
              font-size="11" fill="var(--faint)" class="tnum">$${spent.toFixed(2)}</text>
        <text x="${cx.toFixed(1)}" y="${m.t + plotH + 20}" text-anchor="middle" font-size="11"
              fill="var(--soft)">${esc(r.id.length > 13 ? r.id.slice(0, 12) + "…" : r.id)}</text>
      </g>`;
    }).join("");

    const limit = ceiling ? `
      <line x1="${m.l}" x2="${m.l + plotW}" y1="${y(ceiling).toFixed(1)}" y2="${y(ceiling).toFixed(1)}"
            stroke="var(--warn)" stroke-width="1.5"/>
      <text x="${m.l + plotW}" y="${(y(ceiling) - 7).toFixed(1)}" text-anchor="end" font-size="11"
            font-weight="600" fill="var(--warn)">ceiling $${ceiling.toFixed(2)}</text>` : "";

    return `<svg viewBox="0 0 ${width} ${H}" width="${width}" height="${H}" role="group"
      aria-label="What each generation spent on model calls">
      ${grid}${bars}${limit}
      <line x1="${m.l}" x2="${m.l + plotW}" y1="${m.t + plotH}" y2="${m.t + plotH}"
            stroke="var(--line)" stroke-width="1"/>
    </svg>`;
  });
}

/* ---------- 7. the archive's frontier -------------------------------------- */

/**
 * Every candidate the search has ever proposed: how well it did on average
 * against how much of the instance space it is the best thing anyone has found
 * on.
 *
 * The point of the picture is the bottom right — a candidate that loses on the
 * mean while being the only thing that ever worked on some match. Keeping those
 * is worth roughly twice keeping the best mean scorer in GEPA's own ablation,
 * and a page that ranks candidates by their average erases exactly them.
 */
export function archiveFrontier(el, points, { silence = 0.5 } = {}) {
  host(el, width => {
    const H = Math.min(420, Math.max(300, width * 0.55));
    const m = { t: 24, r: 24, b: 52, l: 58 };
    const plotW = width - m.l - m.r, plotH = H - m.t - m.b;
    const xs = points.map(p => p.mean), ys = points.map(p => p.wins);
    const [xlo, xhi] = pad(Math.min(silence, ...xs), Math.max(silence, ...xs), 0.12);
    const yhi = Math.max(1, ...ys) * 1.12;
    const x = v => m.l + ((v - xlo) / (xhi - xlo)) * plotW;
    const y = v => m.t + plotH - (v / yhi) * plotH;

    const grid = niceTicks(0, yhi, 4).map(t => `
      <line x1="${m.l}" x2="${m.l + plotW}" y1="${y(t).toFixed(1)}" y2="${y(t).toFixed(1)}"
            stroke="var(--c-grid)" stroke-width="1"/>
      <text x="${m.l - 10}" y="${(y(t) + 4).toFixed(1)}" text-anchor="end" font-size="11"
            fill="var(--faint)" class="tnum">${Math.round(t)}</text>`).join("");
    const xticks = niceTicks(xlo, xhi, 5).map(t => `
      <text x="${x(t).toFixed(1)}" y="${m.t + plotH + 18}" text-anchor="middle" font-size="11"
            fill="var(--faint)" class="tnum">${t.toFixed(2)}</text>`).join("");

    const dots = points.map(p => {
      const tip = `${p.id} (${p.generation}): mean ${p.mean.toFixed(3)}, best on ` +
        `${p.wins} of ${p.n} instances` + (p.seed ? " — the seed, the standing incumbent"
          : p.onFrontier ? ", on the frontier" : ", dominated");
      return `<g tabindex="0" data-tip="${esc(tip)}" role="img" aria-label="${esc(tip)}">
        <circle cx="${x(p.mean).toFixed(1)}" cy="${y(p.wins).toFixed(1)}" r="13" fill="transparent"/>
        <circle cx="${x(p.mean).toFixed(1)}" cy="${y(p.wins).toFixed(1)}" r="6"
          fill="${p.onFrontier ? "var(--c-cand)" : "none"}"
          stroke="${p.onFrontier ? "var(--panel)" : "var(--ghost)"}" stroke-width="2"/>
        ${p.seed ? `<circle cx="${x(p.mean).toFixed(1)}" cy="${y(p.wins).toFixed(1)}" r="10"
          fill="none" stroke="var(--brand)" stroke-width="1.5"/>` : ""}
      </g>`;
    }).join("");

    return `<svg viewBox="0 0 ${width} ${H}" width="${width}" height="${H}" role="group"
      aria-label="Every archived candidate: mean score against how many instances it is best on">
      ${grid}${xticks}
      <line x1="${x(silence).toFixed(1)}" x2="${x(silence).toFixed(1)}" y1="${m.t}"
            y2="${m.t + plotH}" stroke="var(--c-zero)" stroke-width="1.5"/>
      <text x="${(x(silence) + 6).toFixed(1)}" y="${m.t + 12}" font-size="11" font-weight="600"
            fill="var(--soft)">silence</text>
      ${dots}
      <text x="${m.l + plotW}" y="${H - 6}" text-anchor="end" font-size="11"
            fill="var(--faint)">mean score across every instance it was asked</text>
      <text x="${m.l - 50}" y="${m.t - 8}" font-size="11" fill="var(--faint)">instances it is best on</text>
    </svg>`;
  });
}

/* ---------- 8. a paper book's equity ---------------------------------------- */

/**
 * At most this many drawn marks. A live book marks once a cycle, which on the
 * one-minute topic is fourteen hundred points a day; the browser would draw
 * them all, but a tooltip per point is a keyboard's worth of tab stops, and
 * the shape is the same at four hundred.
 */
const CURVE_POINTS = 400;

/**
 * Which marks to draw: evenly sampled, but never dropping the first, the last,
 * the trough of the deepest drawdown, or a mark that carries an event — a
 * handover or a cap refusal is the thing the reader came to see.
 */
function thinMarks(pts, trough) {
  if (pts.length <= CURVE_POINTS) return pts.map((_, i) => i);
  const keep = new Set([0, pts.length - 1]);
  if (trough >= 0) keep.add(trough);
  pts.forEach((p, i) => { if (p.event) keep.add(i); });
  const budget = Math.max(0, CURVE_POINTS - keep.size);
  const step = pts.length / Math.max(1, budget);
  for (let k = 0; k < budget; k++) keep.add(Math.min(pts.length - 1, Math.round(k * step)));
  return [...keep].sort((a, b) => a - b);
}

/**
 * Equity over time against its own running peak. The peak is a faint line
 * and the gap under it is shaded, so a drawdown is a visible area rather
 * than a percentage in a tile; a hairline marks the starting equity so a
 * book that is below where it began is below a line, not below a number.
 *
 * Marks are drawn time-ordered, never sorted by value — this is the one
 * chart here whose x axis is the clock, and `sparkline` would put the
 * trough first.
 */
export function equityCurve(el, marks, { start = 1e6 } = {}) {
  const pts = (marks || [])
    .filter(m => m && m.at && m.equity_usd != null)
    .map(m => ({ t: new Date(m.at).getTime(), y: Number(m.equity_usd), at: m.at,
                 event: m.event || null, open: m.open_positions,
                 exposure: m.gross_exposure_usd }))
    .filter(p => Number.isFinite(p.t) && Number.isFinite(p.y))
    .sort((a, b) => a.t - b.t);
  let peak = -Infinity, trough = -1, worst = 0;
  pts.forEach((p, i) => {
    peak = Math.max(peak, p.y);
    p.peak = peak;
    p.dd = peak > 0 ? 1 - p.y / peak : 0;
    if (p.dd > worst) { worst = p.dd; trough = i; }
  });
  const drawn = thinMarks(pts, trough).map(i => pts[i]);

  host(el, width => {
    if (!pts.length) return "";
    const H = 300, m = { t: 26, r: 24, b: 42, l: 66 };
    const plotW = width - m.l - m.r, plotH = H - m.t - m.b;
    const t0 = pts[0].t, t1 = pts[pts.length - 1].t;
    const x = t => t1 > t0 ? m.l + ((t - t0) / (t1 - t0)) * plotW : m.l + plotW / 2;
    const ys = pts.map(p => p.y);
    const [lo, hi] = pad(Math.min(start, ...ys), Math.max(start, ...ys), 0.12);
    const y = v => m.t + plotH - ((v - lo) / (hi - lo)) * plotH;
    const P = (px, py) => `${px.toFixed(1)} ${py.toFixed(1)}`;

    // Axis labels compact like fmtUsd but keep enough decimals to tell the
    // ticks apart: on a million-dollar book the grid is a few thousand
    // apart, and "$1.00M" five times over is no axis.
    const ticks = niceTicks(lo, hi, 5);
    const step = ticks.length > 1 ? ticks[1] - ticks[0] : 1;
    const decimals = (unit, min, max) =>
      Math.min(max, Math.max(min, Math.ceil(-Math.log10(step / unit))));
    const axis = v => {
      const a = Math.abs(v), s = v < 0 ? "-" : "";
      if (a >= 1e6) return `${s}$${(a / 1e6).toFixed(decimals(1e6, 2, 4))}M`;
      if (a >= 1e4) return `${s}$${(a / 1e3).toFixed(decimals(1e3, 1, 3))}k`;
      return fmtUsd(v, { sign: false });
    };
    const grid = ticks.map(v => `
      <line x1="${m.l}" x2="${m.l + plotW}" y1="${y(v).toFixed(1)}" y2="${y(v).toFixed(1)}"
            stroke="var(--c-grid)" stroke-width="1"/>
      <text x="${m.l - 10}" y="${(y(v) + 4).toFixed(1)}" text-anchor="end" font-size="11"
            fill="var(--faint)" class="tnum">${esc(axis(v))}</text>`).join("");
    const xLabels = t1 > t0 ? [0, 1 / 3, 2 / 3, 1].map(f => {
      const t = t0 + f * (t1 - t0);
      const iso = new Date(t).toISOString();
      return `<text x="${x(t).toFixed(1)}" y="${m.t + plotH + 18}" font-size="11"
        text-anchor="${f === 0 ? "start" : f === 1 ? "end" : "middle"}" fill="var(--faint)"
        class="tnum">${day(iso).slice(5)} ${clock(iso)}</text>`;
    }).join("") : "";

    // The drawdown: forward along the running peak, back along the equity.
    const fwd = drawn.map((p, i) => `${i ? "L" : "M"} ${P(x(p.t), y(p.peak))}`).join(" ");
    const back = [...drawn].reverse().map(p => `L ${P(x(p.t), y(p.y))}`).join(" ");
    const shade = `<path class="dd" d="${fwd} ${back} Z" fill="var(--down)"
      fill-opacity="var(--c-wash)" stroke="none"/>`;
    const peakLine = `<polyline class="peak" fill="none" stroke="var(--ghost)" stroke-width="1"
      stroke-dasharray="3 4" points="${drawn.map(p => P(x(p.t), y(p.peak))).join(" ")}"/>`;
    const line = `<polyline class="equity" fill="none" stroke="var(--c-cand)" stroke-width="2"
      stroke-linejoin="round" stroke-linecap="round"
      points="${drawn.map(p => P(x(p.t), y(p.y))).join(" ")}"/>`;
    const startLine = `
      <line x1="${m.l}" x2="${m.l + plotW}" y1="${y(start).toFixed(1)}" y2="${y(start).toFixed(1)}"
            stroke="var(--c-zero)" stroke-width="1"/>
      <text x="${m.l + plotW}" y="${(y(start) - 6).toFixed(1)}" text-anchor="end" font-size="11"
            font-weight="600" fill="var(--soft)">start ${esc(fmtUsd(start, { sign: false }))}</text>`;

    const r = drawn.length > 150 ? 2.2 : 3.5;
    const dots = drawn.map(p => {
      const isTrough = trough >= 0 && p === pts[trough];
      const tip = `${stamp(p.at)} · equity ${fmtUsd(p.y, { sign: false, compact: false })}` +
        ` · ${fmtPct(-p.dd)} from peak` +
        (p.open != null ? ` · ${p.open} open` : "") +
        (p.exposure != null ? ` · exposure ${fmtUsd(p.exposure, { sign: false })}` : "") +
        (p.event ? ` · ${p.event}` : "") + (isTrough ? " · deepest drawdown" : "");
      const cx = x(p.t).toFixed(1), cy = y(p.y).toFixed(1);
      if (p.event) {
        const px = x(p.t), py = y(p.y), d = 6;
        return `<path class="event" tabindex="0" data-tip="${esc(tip)}" role="img"
          aria-label="${esc(tip)}" d="M ${P(px, py - d)} L ${P(px + d, py)} L ${P(px, py + d)}
          L ${P(px - d, py)} Z" fill="var(--warn)" stroke="var(--panel)" stroke-width="1.5"/>`;
      }
      return `<circle tabindex="0" data-tip="${esc(tip)}" role="img" aria-label="${esc(tip)}"
        cx="${cx}" cy="${cy}" r="${isTrough ? r + 1.5 : r}"
        fill="${isTrough ? "var(--down)" : "var(--c-cand)"}" stroke="var(--panel)" stroke-width="1.5"/>`;
    }).join("");

    return `<svg viewBox="0 0 ${width} ${H}" width="${width}" height="${H}" role="group"
      aria-label="Equity of the book over time, with its running peak and every drawdown shaded">
      <text x="${m.l - 56}" y="14" font-size="11" fill="var(--faint)">equity</text>
      ${grid}${shade}${startLine}${peakLine}${line}${dots}${xLabels}
      <line x1="${m.l}" x2="${m.l + plotW}" y1="${m.t + plotH}" y2="${m.t + plotH}"
            stroke="var(--line)" stroke-width="1"/>
    </svg>`;
  });
}
