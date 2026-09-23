/* The paper books: what the forecasts would have been worth as positions.
 *
 * A forecast is scored on error; a book is scored on money. `paper_trade`
 * turns each forecast into a position — sized by the harness or by a default
 * rule when the harness declines — holds it to the horizon, charges fees, and
 * marks the book every cycle. A live sweep keeps one book per topic
 * (`live:<topic>`), and a generation's publish writes one per side it scored
 * (`<run>:<side>:<split>`), so a rewrite can be read as a P&L as well as a
 * skill.
 *
 * Nothing here feeds the gate either. A book is evidence about what the
 * forecasts were worth, at one sizing rule and one fee schedule; the gate
 * decides on skill, on matches neither side saw.
 */

import { q, qAll, ApiError, qs } from "../data.js";
import { href } from "../routes.js";
import { html, raw, stat, pill, empty, stamp, plural, n2, pct, dir } from "../dom.js";
import { equityCurve } from "../charts.js";
import { topicOf, DEFAULT, fmtTradePrice, fmtUsd, fmtPct, positionWord, wordsOf } from "../topics.js";

const MARK_COLUMNS = "at,equity_usd,cash_usd,gross_exposure_usd,open_positions,drawdown,event";
const TRADE_COLUMNS = "id,book_id,run_id,instance_id,side,instrument,opened_at,closed_at," +
                      "entry_px,exit_px,qty,size_usd,fees_usd,pnl_usd,reason,source";

const num = v => (v == null || Number.isNaN(Number(v)) ? null : Number(v));

/** Books by total return, best first; a book with no figure goes last. */
function ranked(books) {
  return [...books].sort((a, b) => {
    const x = num(a.stats.total_return), y = num(b.stats.total_return);
    if (x == null && y == null) return 0;
    if (x == null) return 1;
    if (y == null) return -1;
    return y - x;
  });
}

/** The deepest peak-to-trough fall in a time-ordered list of marks. */
function drawdownOf(marks) {
  let peak = -Infinity, worst = 0;
  for (const m of marks) {
    const e = num(m.equity_usd);
    if (e == null) continue;
    peak = Math.max(peak, e);
    if (peak > 0) worst = Math.max(worst, 1 - e / peak);
  }
  return Number.isFinite(peak) ? worst : null;
}

/** What to call a book: the harness's name, else its fingerprint, shortened. */
const nameOf = b => b.harness_name || (b.harness_fp ? String(b.harness_fp).slice(0, 12) : b.book_id);

const kindPill = b => b.kind === "live" ? pill("live", "up") : pill("replay");
const sourcePill = s => s === "harness" ? pill("harness", "brand")
  : s === "default" ? pill("default", "warn") : s ? pill(s) : "";
const reasonWord = r => String(r || "").replace(/_/g, " ") || "—";
const money = v => fmtUsd(v, { sign: false });
const pnlCell = v => v == null ? html`<span class="crumb">—</span>`
  : html`<span class="${dir(v)}">${fmtUsd(v)}</span>`;

export async function tradingView({ signal, topic = DEFAULT, query = {} }) {
  const t = topicOf(topic);
  const w = wordsOf(t.id);
  let books;
  try {
    books = await q(`books?select=*&topic=${qs.eq(t.id)}&order=updated_at.desc`, { signal });
  } catch (err) {
    // 404 is the honest answer while the migration has not been run: PostgREST
    // cannot see a view that does not exist, and that is a deployment state
    // rather than a failure to report as one.
    if (err instanceof ApiError && err.status === 404) return notInstalled();
    throw err;
  }
  books = (books || []).map(b => ({ ...b, stats: b.stats || {} }));
  if (!books.length) return noBooks(t);

  // Which book gets the curve and the trades: the one the query names, else
  // the live book, else the best replay. The leaderboard is always every book.
  const liveId = `live:${t.id}`;
  const wanted = query.book || null;
  const shown = books.find(b => b.book_id === wanted)
    || books.find(b => b.book_id === liveId)
    || books.find(b => b.kind === "live")
    || ranked(books)[0];
  const missing = wanted && shown.book_id !== wanted ? wanted : null;

  const filter = `&book_id=${qs.eq(shown.book_id)}&topic=${qs.eq(t.id)}`;
  const [marks, tradesRaw] = await Promise.all([
    qAll(`book_marks?select=${MARK_COLUMNS}${filter}&order=at.asc`, { signal, max: 20_000 }),
    q(`trades?select=${TRADE_COLUMNS}${filter}&order=opened_at.desc&limit=500`, { signal }),
  ]);
  const trades = tradesRaw || [];
  const open = trades.filter(r => r.closed_at == null);
  const closed = trades.filter(r => r.closed_at != null);
  const winners = closed.filter(r => num(r.pnl_usd) > 0)
    .sort((a, b) => num(b.pnl_usd) - num(a.pnl_usd)).slice(0, 5);
  const losers = closed.filter(r => num(r.pnl_usd) < 0)
    .sort((a, b) => num(a.pnl_usd) - num(b.pnl_usd)).slice(0, 5);

  // The tiles read the publisher's figures; where a figure is missing they
  // fall back to the marks, which carry the same arithmetic.
  const s = shown.stats;
  const last = marks.length ? marks[marks.length - 1] : null;
  const startEq = num(s.start_equity) ?? num(marks[0]?.equity_usd) ?? 1e6;
  const equity = num(s.end_equity) ?? num(last?.equity_usd);
  const totalReturn = num(s.total_return)
    ?? (equity != null && startEq ? equity / startEq - 1 : null);
  const maxDD = num(s.max_drawdown) ?? (marks.length ? drawdownOf(marks) : null);
  const openCount = num(s.open_positions) ?? open.length;
  const exposure = num(last?.gross_exposure_usd);

  const price = v => fmtTradePrice(v, t.id);
  const sideWord = r => positionWord(r.side, t.id);

  const tradeRow = r => html`<tr>
    <th scope="row"><span class="mono ticker">${r.instrument}</span>
      ${r.instance_id ? html`<span class="crumb">${r.instance_id}</span>` : ""}</th>
    <td>${sideWord(r)}</td>
    <td class="crumb mono">${stamp(r.opened_at)}</td>
    <td class="crumb mono">${r.closed_at ? stamp(r.closed_at) : html`<span class="crumb">open</span>`}</td>
    <td class="n">${price(r.entry_px)}</td>
    <td class="n">${r.exit_px == null ? "—" : price(r.exit_px)}</td>
    <td class="n">${money(r.size_usd)}</td>
    <td class="n">${money(r.fees_usd)}</td>
    <td class="n">${pnlCell(num(r.pnl_usd))}</td>
    <td>${reasonWord(r.reason)}</td>
    <td>${sourcePill(r.source)}</td>
  </tr>`;

  const rankRow = b => {
    const st = b.stats;
    const here = b.book_id === shown.book_id;
    return html`<tr data-book="${b.book_id}" class="${raw(here ? "shown" : "")}">
      <th scope="row">${nameOf(b)} ${kindPill(b)}
        <span class="crumb">${b.kind === "live"
          ? html`live book · since ${stamp(b.started_at)}`
          : b.run_id
            ? html`<a href="${raw(href.run(b.run_id, b.side))}">${b.run_id}</a> · ${b.side}${b.split ? ` · ${b.split}` : ""}`
            : html`${b.side || ""}${b.split ? ` · ${b.split}` : ""}`}</span></th>
      <td class="n ${raw(dir(num(st.total_return)))}">${fmtPct(st.total_return)}</td>
      <td class="n">${n2(st.sharpe)}</td>
      <td class="n">${st.max_drawdown == null ? "—" : fmtPct(-num(st.max_drawdown))}</td>
      <td class="n">${pct(st.hit_rate)}</td>
      <td class="n">${fmtUsd(st.avg_win)} / ${fmtUsd(st.avg_loss)}</td>
      <td class="n">${st.turnover == null ? "—" : `${n2(st.turnover)}×`}</td>
      <td class="n">${money(st.fees_usd)}</td>
      <td class="n">${st.trades ?? "—"}</td>
      <td>${here ? pill("shown", "brand") : html`<a href="${raw(href.trading(b.book_id))}"
        aria-label="View ${nameOf(b)}">view</a>`}</td>
    </tr>`;
  };

  const shortRow = r => html`<tr>
    <th scope="row"><span class="mono ticker">${r.instrument}</span></th>
    <td>${sideWord(r)}</td>
    <td class="crumb mono">${stamp(r.opened_at)}</td>
    <td class="n">${pnlCell(num(r.pnl_usd))}</td>
    <td>${reasonWord(r.reason)}</td>
  </tr>`;
  const shortTable = (caption, rows, none) => rows.length ? html`<div class="scroll"><table>
      <caption>${caption}</caption>
      <thead><tr><th scope="col">${w.instrument}</th><th scope="col">side</th>
        <th scope="col">opened</th><th scope="col" class="n">P&amp;L</th><th scope="col">closed by</th></tr></thead>
      <tbody>${rows.map(shortRow)}</tbody>
    </table></div>` : html`<p class="msg">${none}</p>`;

  const body = html`
    <div class="cards">
      ${stat({ value: money(equity), label: "equity",
               note: `started at ${money(startEq)} · ${shown.kind === "live" ? "live" : "replay"} book` })}
      ${stat({ value: fmtPct(totalReturn), tone: dir(totalReturn), label: "total return",
               note: s.cycles != null ? `over ${plural(num(s.cycles), "cycle")}` : "since the book opened" })}
      ${stat({ value: n2(s.sharpe), tone: dir(num(s.sharpe)), label: "Sharpe, per cycle",
               note: `daily ${n2(s.daily_sharpe)} · ${t.cyclesPerYear} cycles a year on this topic` })}
      ${stat({ value: maxDD == null ? "—" : fmtPct(-maxDD), tone: maxDD > 0 ? "down" : "",
               label: "max drawdown", note: "peak to trough, on the marks" })}
      ${stat({ value: pct(s.hit_rate), label: "hit rate",
               note: `${s.trades != null ? plural(num(s.trades), "trade") : "trades uncounted"}` +
                     (s.profit_factor != null ? ` · profit factor ${n2(s.profit_factor)}` : "") })}
      ${stat({ value: money(s.fees_usd), label: "fees paid",
               note: s.turnover != null ? `turnover ${n2(s.turnover)}× of equity` : "at the topic's fee schedule" })}
      ${stat({ value: openCount == null ? "—" : openCount, label: "open positions",
               note: exposure != null ? `exposure ${money(exposure)}` : "at the last mark" })}
    </div>

    <section class="panel"><div class="panel-b prose">
      <p>These are the same forecasts as everywhere else on this site, turned into positions.
      Each forecast becomes a ${t.id === DEFAULT ? "YES or NO" : "long or short"} in the
      ${w.instrument} it was about, sized by the harness where it said how much and by a
      default rule where it did not, held to the horizon and charged the topic's fees. The
      book is marked every cycle. Nothing here feeds the gate: a book is what the forecasts
      were worth at one sizing rule, not whether a rewrite is better than its parent.</p>
      ${missing ? html`<p class="note">There is no book called <code>${missing}</code> on
        ${t.title}; showing <b>${nameOf(shown)}</b> instead.</p>` : ""}
    </div></section>

    <section class="panel">
      <div class="panel-h"><h2>${nameOf(shown)}</h2>${kindPill(shown)}</div>
      <div class="panel-b">
        <dl class="book-meta">
          <div><dt>book</dt><dd class="mono">${shown.book_id}</dd></div>
          ${shown.run_id ? html`<div><dt>run</dt><dd><a href="${raw(href.run(shown.run_id, shown.side))}">${shown.run_id}</a>${shown.side ? ` · ${shown.side}` : ""}${shown.split ? ` · ${shown.split}` : ""}</dd></div>` : ""}
          <div><dt>started</dt><dd>${stamp(shown.started_at)}</dd></div>
          <div><dt>last mark</dt><dd>${stamp(last ? last.at : shown.updated_at)}</dd></div>
          ${s.handovers != null ? html`<div><dt>handovers</dt><dd>${s.handovers}</dd></div>` : ""}
          ${s.refusals != null ? html`<div><dt>cap refusals</dt><dd>${s.refusals}</dd></div>` : ""}
        </dl>
        ${marks.length >= 2 ? html`
        <div class="legend">
          <span><i style="background:var(--c-cand)"></i> equity</span>
          <span><i class="dash"></i> running peak</span>
          <span><i class="wash down"></i> drawdown</span>
          <span><i class="diamond"></i> event (handover, refusal)</span>
        </div>
        <figure class="chart">
          <div id="equity-curve" class="chart"></div>
          <figcaption>${plural(marks.length, "mark")}${marks.truncated ? " (truncated)" : ""},
            oldest to newest; the hairline is the starting equity and the shaded gap is how
            far below its own peak the book was.</figcaption>
        </figure>` : html`<p class="msg">${marks.length
          ? "One mark so far; a curve needs two."
          : "No marks recorded for this book yet."}</p>`}
      </div>
    </section>

    <section class="panel">
      <div class="panel-h"><h2>Leaderboard</h2><span class="pill">${plural(books.length, "book")} on ${t.title}</span></div>
      <div class="scroll"><table>
        <caption>Every book on this topic, best total return first. Avg win and avg loss are
          per closed trade; turnover is notional traded as a multiple of starting equity.</caption>
        <thead><tr>
          <th scope="col">agent</th><th scope="col" class="n">return</th>
          <th scope="col" class="n">Sharpe</th><th scope="col" class="n">max DD</th>
          <th scope="col" class="n">hit rate</th><th scope="col" class="n">avg win / loss</th>
          <th scope="col" class="n">turnover</th><th scope="col" class="n">fees</th>
          <th scope="col" class="n">trades</th><th scope="col">curve</th>
        </tr></thead>
        <tbody id="leaderboard">${ranked(books).map(rankRow)}</tbody>
      </table></div>
    </section>

    <section class="panel">
      <div class="panel-h"><h2>Open positions</h2><span class="pill">${plural(open.length, "position")}</span></div>
      ${open.length ? html`<div class="scroll"><table>
        <caption>Positions on ${nameOf(shown)} not yet closed.</caption>
        <thead><tr>
          <th scope="col">${w.instrument}</th><th scope="col">side</th><th scope="col">opened</th>
          <th scope="col" class="n">entry</th><th scope="col" class="n">size</th>
          <th scope="col" class="n">fees so far</th><th scope="col">sized by</th>
        </tr></thead>
        <tbody>${open.map(r => html`<tr>
          <th scope="row"><span class="mono ticker">${r.instrument}</span>
            ${r.instance_id ? html`<span class="crumb">${r.instance_id}</span>` : ""}</th>
          <td>${sideWord(r)}</td>
          <td class="crumb mono">${stamp(r.opened_at)}</td>
          <td class="n">${price(r.entry_px)}</td>
          <td class="n">${money(r.size_usd)}</td>
          <td class="n">${money(r.fees_usd)}</td>
          <td>${sourcePill(r.source)}</td>
        </tr>`)}</tbody>
      </table></div>` : html`<p class="msg">Nothing open on this book.</p>`}
    </section>

    <section class="panel">
      <div class="panel-h"><h2>Recent trades</h2><span class="pill">newest first</span></div>
      ${trades.length ? html`<div class="scroll"><table>
        <caption>The last ${Math.min(50, trades.length)} of ${plural(trades.length, "trade")}
          on ${nameOf(shown)}${trades.length >= 500 ? " (the newest five hundred were read)" : ""}.
          A trade closed by "horizon" ran to its full horizon; "agent" means the harness closed
          it early; "handover" and "force close" are the book's doing, not the forecast's.</caption>
        <thead><tr>
          <th scope="col">${w.instrument}</th><th scope="col">side</th><th scope="col">opened</th>
          <th scope="col">closed</th><th scope="col" class="n">entry</th><th scope="col" class="n">exit</th>
          <th scope="col" class="n">size</th><th scope="col" class="n">fees</th>
          <th scope="col" class="n">P&amp;L</th><th scope="col">closed by</th><th scope="col">sized by</th>
        </tr></thead>
        <tbody>${trades.slice(0, 50).map(tradeRow)}</tbody>
      </table></div>` : html`<p class="msg">No trades on this book yet.</p>`}
    </section>

    <div class="grid-2 even">
      <section class="panel">
        <div class="panel-h"><h2>Winners</h2><span class="pill">top 5 by P&amp;L</span></div>
        ${shortTable(`The five closed trades on ${nameOf(shown)} that made the most.`, winners,
                     closed.length ? "No closed trade on this book made money." : "No closed trades yet.")}
      </section>
      <section class="panel">
        <div class="panel-h"><h2>Losers</h2><span class="pill">bottom 5 by P&amp;L</span></div>
        ${shortTable(`The five closed trades on ${nameOf(shown)} that lost the most.`, losers,
                     closed.length ? "No closed trade on this book lost money." : "No closed trades yet.")}
      </section>
    </div>`;

  return {
    title: "Trading",
    heading: "What the forecasts were worth as positions",
    lead: html`Every book here is paper: the ${t.title} forecasts sized into positions, held to
      the horizon, charged fees and marked every cycle. The live book runs with the live
      sweeps; a replay book is one side of one generation, on the split it was scored on.`,
    body,
    ready: root => {
      if (marks.length >= 2)
        equityCurve(root.querySelector("#equity-curve"), marks, { start: startEq });
    },
  };
}

function notInstalled() {
  return {
    title: "Trading", heading: "The trading tables are not installed yet",
    body: html`<section class="panel"><div class="panel-b prose">
      <p>This page reads <code>public.rsi_books</code>, <code>public.rsi_trades</code> and
      <code>public.rsi_book_marks</code>, and the database does not have them. They are
      created by migration 009 under <code>supabase/migrations/</code>, which nobody has run
      against this project yet.</p>
      <p class="note">Until then there is nothing to show here, and a retry will not change
      that.</p>
    </div></section>`,
  };
}

function noBooks(t) {
  return {
    title: "Trading", heading: "No paper books yet",
    lead: html`The tables exist and have nothing for ${t.title}: nothing has traded this
      topic's forecasts on paper.`,
    body: empty(html`A live sweep's <code>paper_trade</code> step opens the live book
      (<code>live:${t.id}</code>) and marks it every cycle, and a generation's publish
      writes one replay book per side it scored (<code>&lt;run&gt;:&lt;side&gt;:&lt;split&gt;</code>).
      Neither has happened on ${t.title} yet; the books appear here when one does.`),
  };
}
