/* Everything that talks to Supabase.
 *
 * The tables live in an `rsi` schema, reached through views in `public`: the
 * project is shared with another arena, and exposing a second schema through
 * the API would have changed that arena's API too.
 *
 * This file exists because the six-line fetch wrapper it replaces had four
 * failure modes that all looked the same on screen:
 *
 *   - a hash change mid-flight rendered whichever response landed last, so
 *     clicking Lineage then Compare could leave you on Compare reading Lineage;
 *   - a stalled connection left the skeleton up forever, with no timeout;
 *   - an unset SUPABASE_URL made every request relative, so the page fetched
 *     itself, got HTML with a 200, and the reader saw "Unexpected token '<'";
 *   - a PostgREST error body — hints, column names, the failing SQL — was
 *     written into the page with innerHTML.
 */

const TIMEOUT_MS = 15_000;
const TTL_MS = 60_000;
const PAGE = 1000;              // PostgREST's own default max is 1000 rows

const cache = new Map();        // key -> { at, data }

// The difference between this machine's clock and the database's, estimated
// from response Date headers. A client five minutes fast would otherwise mark
// every live run stale — "the run died" is a serious accusation to make on
// the strength of a wrong wristwatch. HTTP dates have one-second resolution,
// so half a second is added as the expected midpoint.
let skewMs = 0;
export const serverNow = () => Date.now() + skewMs;

export class ApiError extends Error {
  /** @param kind config|network|timeout|aborted|http|parse|notfound */
  constructor(kind, message, { status = 0, detail = "" } = {}) {
    super(message);
    this.name = "ApiError";
    this.kind = kind;
    this.status = status;
    this.detail = detail;                 // never rendered; logged for us
    this.retryable = kind !== "config" && kind !== "notfound";
  }
}

/** A plain sentence when the page cannot possibly work, or null. */
export function configProblem() {
  const { url, key } = window.RSI || {};
  if (!url || url === "__SUPA" + "BASE_URL__")
    return "This page has no SUPABASE_URL. It is substituted when web/server.py " +
           "serves the file, so opening index.html directly will always show this; " +
           "on the deployed service it means the environment variable is unset.";
  if (!/^https?:\/\//.test(url))
    return `SUPABASE_URL is set to "${url}", which is not an absolute http(s) URL. ` +
           "Every request would go to this server instead of the database.";
  if (!key || key === "__SUPA" + "BASE_ANON_KEY__")
    return "This page has no SUPABASE_ANON_KEY, so every request would come back 401.";
  return null;
}

function base() {
  return String(window.RSI.url).replace(/\/$/, "");
}

/** One controller that gives up on a timeout and gives up when the view does. */
function linked(outer) {
  const ctl = new AbortController();
  const state = { timedOut: false };
  const timer = setTimeout(() => { state.timedOut = true; ctl.abort(); }, TIMEOUT_MS);
  const relay = () => ctl.abort();
  if (outer) {
    if (outer.aborted) ctl.abort();
    else outer.addEventListener("abort", relay, { once: true });
  }
  state.signal = ctl.signal;
  state.done = () => {
    clearTimeout(timer);
    if (outer) outer.removeEventListener("abort", relay);
  };
  return state;
}

async function request(url, { signal, headers = {}, method = "GET", body } = {}) {
  const problem = configProblem();
  if (problem) throw new ApiError("config", problem);

  const link = linked(signal);
  let res;
  try {
    res = await fetch(url, {
      method, body, signal: link.signal,
      headers: {
        apikey: window.RSI.key,
        Authorization: `Bearer ${window.RSI.key}`,
        ...(body ? { "Content-Type": "application/json" } : {}),
        ...headers,
      },
    });
  } catch (err) {
    if (link.timedOut)
      throw new ApiError("timeout", `The database did not answer within ${TIMEOUT_MS / 1000}s.`);
    if (err && err.name === "AbortError")
      throw new ApiError("aborted", "superseded");
    throw new ApiError("network", "Could not reach the database.", { detail: String(err) });
  } finally {
    link.done();
  }

  const stamped = res.headers.get("date");
  if (stamped) {
    const t = new Date(stamped).getTime();
    if (Number.isFinite(t)) skewMs = t + 500 - Date.now();
  }
  const type = res.headers.get("content-type") || "";
  if (!res.ok) {
    const detail = await res.text().catch(() => "");
    // The body here is PostgREST's: hints, column names, sometimes the SQL.
    // It goes to the console, never to the page.
    if (detail) console.error(`${res.status} ${url}\n${detail}`);
    throw new ApiError("http", `The database refused the request (${res.status}).`,
                       { status: res.status, detail });
  }
  if (res.status === 204) return { rows: null, range: null };
  if (!type.includes("json")) {
    throw new ApiError("parse",
      "The database answered with something that is not JSON — usually a sign that " +
      "SUPABASE_URL points at this page rather than at Supabase.");
  }
  return { rows: await res.json(), range: res.headers.get("content-range") };
}

/**
 * One select. `path` is everything after `rsi_`, e.g. `runs?select=*`.
 *
 * `ttl` is honoured per path: hashchange used to refetch every run with six
 * jsonb blobs on every press of the back button.
 */
export async function q(path, { signal, ttl = TTL_MS, fresh = false } = {}) {
  const key = `GET ${path}`;
  const hit = cache.get(key);
  if (!fresh && hit && Date.now() - hit.at < ttl) return hit.data;
  const { rows } = await request(`${base()}/rest/v1/rsi_${path}`, { signal });
  cache.set(key, { at: Date.now(), data: rows });
  return rows;
}

/**
 * Every row of a select, a page at a time.
 *
 * Three queries on the old page had no limit at all and pulled every run with
 * six jsonb blobs each; one pulled four thousand rollouts including `output`
 * to count distinct fixtures. Nothing here is unbounded: `max` is a wall, and
 * hitting it is reported rather than silently truncating a count.
 */
export async function qAll(path, { signal, ttl = TTL_MS, fresh = false,
                                   pageSize = PAGE, max = 20_000 } = {}) {
  const key = `ALL ${path}`;
  const hit = cache.get(key);
  if (!fresh && hit && Date.now() - hit.at < ttl) return hit.data;

  const out = [];
  let from = 0, total = null;
  for (;;) {
    const to = from + pageSize - 1;
    const { rows, range } = await request(`${base()}/rest/v1/rsi_${path}`, {
      signal, headers: { "Range-Unit": "items", Range: `${from}-${to}`, Prefer: "count=exact" },
    });
    out.push(...(rows || []));
    const slash = (range || "").split("/")[1];
    if (slash && slash !== "*") total = Number(slash);
    if (!rows || rows.length < pageSize) break;
    from += pageSize;
    if (from >= max) break;
  }
  const data = Object.assign(out, { total: total ?? out.length, truncated: out.length < (total ?? 0) });
  cache.set(key, { at: Date.now(), data });
  return data;
}

/**
 * A file this server holds rather than a row Supabase holds.
 *
 * The candidate archive is committed to the repository — it is the record of
 * what the search proposed, which is not a published result — so it arrives
 * from the same origin as the page, with no key and no PostgREST.
 */
export async function local(path, { signal, ttl = 5 * TTL_MS } = {}) {
  const key = `LOCAL ${path}`;
  const hit = cache.get(key);
  if (hit && Date.now() - hit.at < ttl) return hit.data;

  const link = linked(signal);
  let res;
  try {
    res = await fetch(path, { signal: link.signal });
  } catch (err) {
    if (link.timedOut) throw new ApiError("timeout", "This server did not answer in time.");
    if (err && err.name === "AbortError") throw new ApiError("aborted", "superseded");
    throw new ApiError("network", "Could not read a file this server holds.",
                       { detail: String(err) });
  } finally {
    link.done();
  }
  if (res.status === 404) throw new ApiError("notfound", `${path} is not deployed here.`, { status: 404 });
  if (!res.ok) throw new ApiError("http", `This server refused ${path} (${res.status}).`,
                                  { status: res.status });
  const data = await res.json();
  cache.set(key, { at: Date.now(), data });
  return data;
}

/**
 * A JSON endpoint that is not Supabase — today only GitHub's Actions API,
 * the cross-check for a run that predates the heartbeat.
 *
 * Same abort and timeout machinery as everything else, none of the headers:
 * sending the Supabase key to a third party would be wrong even though the key
 * is public, and GitHub rate-limits by IP either way. A 403 here is the rate
 * limit, and the caller degrades rather than retries — 60 requests an hour is
 * a budget, not a suggestion.
 */
export async function external(url, { signal, ttl = 0 } = {}) {
  const key = `EXT ${url}`;
  const hit = cache.get(key);
  if (ttl && hit && Date.now() - hit.at < ttl) return hit.data;

  const link = linked(signal);
  let res;
  try {
    res = await fetch(url, { signal: link.signal,
                             headers: { Accept: "application/vnd.github+json" } });
  } catch (err) {
    if (link.timedOut) throw new ApiError("timeout", "The endpoint did not answer in time.");
    if (err && err.name === "AbortError") throw new ApiError("aborted", "superseded");
    throw new ApiError("network", "Could not reach the endpoint.", { detail: String(err) });
  } finally {
    link.done();
  }
  if (!res.ok) {
    throw new ApiError("http", `The endpoint refused the request (${res.status}).`,
                       { status: res.status });
  }
  const data = await res.json();
  if (ttl) cache.set(key, { at: Date.now(), data });
  return data;
}

/** A `security definer` function. The only write the anon key can make. */
export async function rpc(name, args, { signal } = {}) {
  const { rows } = await request(`${base()}/rest/v1/rpc/rsi_${name}`, {
    signal, method: "POST", body: JSON.stringify(args),
  });
  return rows;
}

/**
 * How many rows match, without fetching them.
 *
 * The compare page used to count votes by pulling rows through the default
 * limit — accurate up to a thousand and silently wrong after. One row plus
 * `Prefer: count=exact` gets the total from the Content-Range header instead.
 */
export async function count(path, { signal } = {}) {
  const { range } = await request(`${base()}/rest/v1/rsi_${path}`, {
    signal, headers: { "Range-Unit": "items", Range: "0-0", Prefer: "count=exact" },
  });
  const total = (range || "").split("/")[1];
  return total && total !== "*" ? Number(total) : 0;
}

/** Forget what is cached — after a write, or on an explicit retry. */
export function invalidate(prefix = "") {
  for (const key of [...cache.keys()])
    if (!prefix || key.includes(prefix)) cache.delete(key);
}

export const qs = {
  /** `in.(a,b,c)` with each value quoted, because fixtures carry dashes. */
  inList(values) {
    // Quoted because fixtures carry dashes, encoded because PostgREST decodes
    // the query string before it parses the list.
    const body = values.map(v => `"${String(v).replace(/"/g, '""')}"`).join(",");
    return `in.(${encodeURIComponent(body)})`;
  },
  eq: v => `eq.${encodeURIComponent(v)}`,
};
