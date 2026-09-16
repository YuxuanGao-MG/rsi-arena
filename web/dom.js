/* Markup, escaping, and the shapes every view reuses.
 *
 * The page this replaces escaped `& < > "` but not `'`, and interpolated
 * values into seventeen single-quoted `onclick` attributes — with `fixture`
 * derived from Kalshi and ESPN ids, which are externally sourced. Two things
 * changed. There are no inline handlers at all any more (see `bind` in app.js:
 * `data-action` plus a delegated listener), and interpolation goes through the
 * `html` tag below, which escapes every value unless it is explicitly `raw`.
 */

const RAW = Symbol("raw");

export const esc = s => String(s ?? "").replace(/[&<>"'`]/g, c => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;", "`": "&#96;",
}[c]));

/** Mark a string as already-safe markup. Charts and nested templates use it. */
export const raw = s => ({ [RAW]: String(s) });

function render(v) {
  if (v == null || v === false || v === true) return "";
  if (Array.isArray(v)) return v.map(render).join("");
  if (typeof v === "object" && RAW in v) return v[RAW];
  return esc(v);
}

/** Tagged template that escapes by default. Anything unescaped says so. */
export function html(strings, ...values) {
  let out = strings[0];
  for (let i = 0; i < values.length; i++) out += render(values[i]) + strings[i + 1];
  return raw(out);
}

export const toHTML = render;

export function mount(el, node) {
  el.innerHTML = render(node);
  return el;
}

/* ---------- numbers ------------------------------------------------------- */

export const n3 = v => v == null || Number.isNaN(v) ? "—" : (v > 0 ? "+" : "") + Number(v).toFixed(3);
export const n2 = v => v == null || Number.isNaN(v) ? "—" : Number(v).toFixed(2);
export const usd = v => v == null ? "—" : "$" + Number(v).toFixed(Number(v) < 1 ? 3 : 2);
export const cents = v => v == null ? "—" : (v > 0 ? "+" : "") + Number(v).toFixed(1) + "c";
export const pct = v => v == null ? "—" : Math.round(Number(v) * 100) + "%";
export const price = v => v == null ? "—" : Number(v).toFixed(3);
export const dir = v => v == null ? "quiet" : v > 0 ? "up" : v < 0 ? "down" : "quiet";
export const sgn = v => html`<span class="${dir(v)}">${n3(v)}</span>`;
export const clock = t => String(t || "").slice(11, 16);
export const day = t => String(t || "").slice(0, 10);
export const stamp = t => String(t || "").replace("T", " ").slice(0, 16);
export const plural = (n, one, many) => `${n} ${n === 1 ? one : many || one + "s"}`;

/** Held-out pooled skill, as the gate computes it, or null. */
export const holdout = (run, side) => (run && run[side] && run[side].holdout) || null;

/* ---------- shared fragments ---------------------------------------------- */

export const panel = (head, body, cls = "") => html`
  <section class="panel ${raw(cls)}">
    ${head ? html`<div class="panel-h">${head}</div>` : ""}
    <div class="panel-b">${body}</div>
  </section>`;

export const stat = ({ value, label, note, tone = "", hero = false }) => html`
  <div class="stat ${raw(hero ? "hero" : "")}">
    <b class="${raw(tone)}">${value}</b>
    <span class="label">${label}</span>
    ${note ? html`<small>${note}</small>` : ""}
  </div>`;

export const pill = (text, cls = "") => html`<span class="pill ${raw(cls)}">${text}</span>`;

export const skeleton = (lines = 3) => html`
  <div class="panel"><div class="panel-b" aria-hidden="true">
    ${raw(Array.from({ length: lines },
      (_, i) => `<div class="skel" style="width:${[40, 70, 55, 62][i % 4]}%"></div>`).join(""))}
  </div></div>
  <p class="visually-hidden">Loading.</p>`;

export const empty = text => html`<div class="panel"><p class="msg">${text}</p></div>`;

/** A failure a reader can act on: what happened, and a button to try again. */
export const errorPanel = (err, retryAction) => html`
  <section class="panel error">
    <div class="panel-h"><h2>${err.kind === "config" ? "This page is misconfigured"
                               : "The data did not load"}</h2></div>
    <div class="panel-b">
      <p class="prose">${err.message}</p>
      ${err.kind === "config" ? html`<p class="note prose">Set SUPABASE_URL and
        SUPABASE_ANON_KEY on the service and redeploy. Until then there is nothing
        to show, and a retry will not help.</p>` : ""}
      ${err.retryable && retryAction
        ? html`<button class="btn" type="button" data-action="${retryAction}">Try again</button>`
        : ""}
    </div>
  </section>`;

/** A skill bar centred on zero: right of the line is better than silence. */
export function skillBar(v) {
  if (v == null) return "";
  const w = Math.min(50, Math.abs(v) * 50);          // ±1.0 fills the half
  const pos = v >= 0;
  return html`<div class="sk" aria-hidden="true"><div class="mid"></div>
    <i style="left:${raw(pos ? 50 : 50 - w)}%;width:${raw(w)}%;background:var(--${raw(pos ? "up" : "down")})"></i>
  </div>`;
}
