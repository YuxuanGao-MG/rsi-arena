# web

A reader for what the harnesses actually did: the generations, the windows each
one was scored on, the trace of any window, everything the search has proposed,
what the loop costs, and forecasts on markets that were still trading.

No build step and no framework. One HTML shell, a stylesheet, and a dozen ES
modules the browser loads directly; the server fills three placeholders into the
shell and serves the rest as files. That constraint is why a deploy is a
restart, and it is worth keeping.

## Running it

```bash
export SUPABASE_URL=https://<project>.supabase.co
export SUPABASE_ANON_KEY=<anon key>
python web/server.py            # http://localhost:8000
```

The anon key is injected at serve time rather than committed, so rotating it
does not mean editing HTML. It can only read: the tables carry row-level
security with a select-only policy, and the one write a visitor can make goes
through a `security definer` function that computes what it stores.

Files are read once at start, so a change to a module needs a restart.

### Environment

| Variable | What it does |
|---|---|
| `SUPABASE_URL`, `SUPABASE_ANON_KEY` | Where the page reads from. Unset is survivable: the page says so in words rather than fetching itself and reporting `Unexpected token '<'`. |
| `PORT` | Defaults to 8000. |
| `OPENROUTER_CREDIT_REMAINING`, `OPENROUTER_CREDIT_TOTAL`, `OPENROUTER_CREDIT_AS_OF` | What is left on the model account. The one number on the cost page that is in no manifest — a generation records what it spent, and nothing records what there is left to spend — so it is set by hand and shown with the date it was set. Unset shows nothing rather than zero. |
| `ARCHIVE_PATH` | Where the Kalshi `archive.json` lives, if not `runs/archive.json`. Other topics' archives are `runs/archive.<topic>.json`, served at `/archive/<topic>.json`. |

## Topics

One service, several loops. The header's switcher moves every page between
`kalshi-horizon-5m` (cents of a 0-1 price, tick 0.01), `news-equity-5m` and
`crypto-horizon-1m` (basis points of a relative move, tick 5). A route may start
with `t/<topic>/`; without it the page is on the topic the browser last chose,
else Kalshi. Runs, progress, live forecasts and guesses filter on their `topic`
column; rollouts, traces and votes reach a topic through the run ids of a
filtered run list. A rollout row without a `topic` is a Kalshi row, and every
per-row floor goes through `tickOf`, so the Kalshi numbers are unchanged.
`supabase/migrations/008_topics.sql` adds the columns; before it is applied the
status dot degrades to the unfiltered read and the other topics are empty.

## What is where

| File | What it holds |
|---|---|
| `index.html` | The shell: head, header (with the topic switcher), `<main>`, the live region, and the config the server substitutes. |
| `app.js` | Router, theme toggle, focus and announcements, the skeleton and its slow-fetch notice. |
| `topics.js` | The topics table: unit, tick, words, crons, archive path and grouping per topic, plus the arithmetic that differs between them (`moveOf`, `predictedOf`, `fmtMove`, `tickOf`). Imports nothing. |
| `routes.js` | Every href, with the optional `#/t/<topic>/` prefix — omitted for the default topic, so old links and bookmarks still mean Kalshi. |
| `data.js` | Every request. Timeouts, cancellation on route change, `Range` pagination, an in-memory cache, and typed errors. |
| `dom.js` | The `html` tagged template, which escapes every interpolation unless it is explicitly `raw`, plus the fragments every view reuses. |
| `charts.js` | Seven pictures, as inline SVG, re-rendered at the width they are actually given. |
| `stats.js` | Pooled skill, recomputed from `err` and `naive_error` — `topics/kalshi_horizon/score.py` in JavaScript. |
| `archive.js` | The candidate archive: frontier, wins, lineage — `loop/archive.py` in JavaScript. |
| `views/` | One module per page. |

## Two things the page recomputes rather than reads

**Pooled skill.** The three published generations were scored under three
versions of the metric: `gen1` and `gen1-1k` divided by an unfloored benchmark,
and `gen1-floored` was scored while a per-window skill of `1 - error/benchmark`
still paid a full point for saying nothing on a market that did not move. Each
was the gate's statistic on the day, so none is wrong — but on one axis they
would make a change of metric look like a result. Every level on the site is
recomputed from the windows' own errors; each generation's page shows the
published figure beside it and says why they differ.

**Which windows were the worst and the best.** Ordering by the stored `skill`
column put six echoes of the mid at the top of "Where it won". The ranking is
done on the recomputed number, and only the eighteen rows that are shown have
their `output` fetched.

## Why worst-first

The window list is ordered by skill ascending, always. A generation that scored
+0.04 tells you nothing you can act on. The windows it lost tell you what the
harness does when it is wrong, which is the only thing a rewrite can be aimed
at — and it is the same material GEPA reflects over.

## Where the data comes from

`scripts/publish_runs.py` pushes a run directory into Supabase. Tables live in
an `rsi` schema and are read through views in `public` (`rsi_runs`,
`rsi_rollouts`, `rsi_traces`, `rsi_votes`, `rsi_live_forecasts`). The Supabase
project is shared with another arena, so exposing a second schema through the
API would have changed that arena's API too; views reach the same rows without
touching anything anyone else depends on.

`runs/archive.json` is not in Supabase and is served as a file. It records what
the search *found*, which carries no claim; the database holds what the gate
*decided*. The `Dockerfile` copies it into the image.

**`supabase/migrations/002_votes_rpc.sql` has to be run by hand** before the
vote, the live page, the audit block or audit rollouts will work. Until then
those parts of the page say so, by name, rather than failing quietly.
