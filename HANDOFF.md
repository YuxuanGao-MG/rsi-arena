# Handoff: rsi-arena

Written 2026-09-14, substantially revised 2026-09-16. Read this
first, then `README.md`, then `docs/design.md`. Everything below is the state
of the repository at commit time; `git log` is the record after that.

## What this is, in three sentences

A harness (a model wired to tools by a JSON plan) is put back at fixed instants
of finished Kalshi soccer matches, with its tools frozen at that instant, and
asked where the contract's mid price goes in five minutes. The answer is in the
candle history, so hundreds of windows score in seconds and cost only model
calls. GEPA rewrites the harness from the traces of the windows it lost, and a
gate promotes a rewrite only if it beats the incumbent on fixtures the
optimizer never saw.

## Where it came from

`seantao97/rsi-arena` is the earlier codebase (Sean wrote the runtime, backend
and web app in three PRs in August 2026; Yuxuan wrote the topics catalogue, the
Kalshi data layer, the tools and the replay benchmark). This repository is the
independent alternative: same idea, loop-first, built on GEPA instead of a
hand-written optimizer. It reuses only Yuxuan's stdlib-only Kalshi data layer,
copied into `rsi_arena/kalshi/`. `docs/sean-runtime-notes.md` describes the
old codebase; `docs/frameworks.md` is the survey that chose GEPA. Treat the old
repo as reference only; do not depend on it.

## The sweep covers every competition Kalshi prices (2026-09-27)

It covered eight. A Thursday full of UEFA Nations League fixtures produced no
forecasts and no error, for two reasons that are the same reason twice:

1. `collect_live.py` and `discover_fixtures.py` derived the series ticker as
   `f"KX{league}GAME"`. The Nations League trades as `KXUEFANLGAME`, so the
   sweep asked for `KXUEFANATIONSGAME`, got an empty listing, and reported a
   quiet evening — an empty listing is what Kalshi answers for a competition
   with no matches on, so there was nothing to notice.
2. `resolve_league` stripped underscores *before* looking a code up, so
   `UEFA_NATIONS` resolved to nothing and `espn_scoreboard` raised on it. Seven
   league codes were in that state. Even with the right ticker, the Nations
   League could not have reached its fixture feed.

**`rsi_arena/kalshi/_series.py` is the mapping now.** Seventy-three
`league -> series` pairs, each verified on 2026-09-27 against the live exchange
*and* the live fixture feed, with a comment per line saying which ESPN
competition answered and how many sampled fixtures linked. `series_for()` is a
lookup; `KX{LEAGUE}GAME` is only its fallback and says so when it is used. The
residue is recorded too, with reasons: `UNMAPPED` (49 real per-match series ESPN
cannot grade — a flat 400 for Poland, Korea, Croatia, Egypt; an empty card every
day of the year for Switzerland, Thailand, the Czech top flight), `SEASONAL`
(8 out of season), `NOT_FIXTURES` (10 series ending in GAME that price something
else, or nothing).

Two things worth keeping in mind before editing any of it:

- **A cup's qualifying rounds are a different ESPN competition from the cup.**
  September's FA Cup rounds are `eng.fa_qual`, November's are `eng.fa`.
  `_taxonomy.EXTRA_SCOREBOARD_SLUGS` unions the extra feeds in
  `espn_scoreboard`. Only the scoreboard needs it — ESPN's `summary?event=`
  endpoint is slug-agnostic, so one canonical slug still serves every timeline.
- **An ESPN slug that answers 200 is not a slug that serves football.** That is
  how the old table came to claim `sui.1`, `tha.1`, `pol.1` and `egy.1`.

**Run `python scripts/check_leagues.py` occasionally** — monthly is plenty, or
The `leagues` workflow runs it at 06:20 UTC on the first of each month and opens
an issue only when something has drifted; the table itself is never edited
automatically, because a new competition wants a verified slug and a dead one
wants a decision.
whenever a sweep looks thinner than the fixture list. It re-asks both venues and
prints the drift: series that have died, mapped leagues that stopped linking,
unmapped ones that started working, and soccer series the table has never had an
opinion about. `--quick` skips the fixture feed and takes about a minute; the
full run takes five. It is deliberately not a test: Kalshi listing a new
competition is news, not a regression, and the table is edited by a person who
can say why in the commit. What *is* a test is that the table is self-consistent
and that every league in it reaches a fixture feed (`tests/test_leagues.py`),
including a named regression test for `UEFA_NATIONS -> KXUEFANLGAME`.

**The question set went with it.** 485 matches in nine leagues became 930 in
seventy-two, 31,243 windows, over the same three-month span (2026-07-16 to
2026-09-26) — `discover_fixtures.py --league all --max-per-league 5` twice, once
over the set's own window and once over the eleven days past its end, so the new
competitions are added rather than the old ones displaced. `--keep` is 1,000 now;
the reasoning is in `roll_question_set.py`. `preflight.py` passes 22 of 22 on it.

`live.yml` sweeps `all` (the seventy-three, biggest first) at
`--max-contracts 8`, and the unidentified alarm moved from a tenth to a fifth
because a wide card always has a few competitions in a round the feed files
elsewhere. Walked live on 2026-09-27: 28 competitions had open events, 344 of 350
identified, 137 seconds of network for the whole sweep. `collect_live.py` builds a
league's market catalogue only once it has seen an open event, which is what keeps
sixty-five extra competitions free on a quiet night.

## Read this first (2026-09-22)

**Three topics now, one loop, one reader.** `rsi-arena topic --topic <name> --json`
prints what each runs on; the workflows read the same spec with `--shell`.

| topic | instance | unit / tick | seed harness | question set | keys |
|---|---|---|---|---|---|
| `kalshi-horizon-5m` | a Kalshi soccer contract at an instant, 5 min out | cents / 1c | `harnesses/horizon-5m-jev.json` (Jev; the Opus lineage's 11 generations stay under `runs/`, the Jev lineage is `runs/kalshi-jev/`) | 930 matches over 72 competitions, `benchmarks/windows/`; held-out 300 | OpenRouter |
| `crypto-horizon-1m` | BTC/ETH/SOL spot at an instant, 1 min out, every 5 min | bps / 2 | `harnesses/crypto-horizon-1m-jev.json` (Jev) | 365 UTC days, 8,760 windows, `benchmarks/windows-crypto/` + `benchmarks/crypto-data/`; held-out 120, audit 60 | none for data |
| `news-equity-5m` | a Benzinga item on a US stock/ETF, 5 min out, RTH | bps / 5 | `harnesses/news-equity-5m-jev.json` (Jev) | **empty until Alpaca keys exist**; then `scripts/discover_news.py` | `APCA_API_KEY_ID`, `APCA_API_SECRET_KEY` |

The two new topics run on TypeSafe's Jev (`typesafe/jev-1.13`, a decisions model:
typed questions in, probabilities out, no text, no tool calls, ~$0.0002 a window;
see `docs/design.md`). A plan for it ends in a prompt step with `questions` and
`answers`; `ask_opus` / `ask_sonnet` / `ask_gpt5_mini` are tools in the pool so a
plan may delegate one reasoning step. The gate applies its cost ratio to
`max(incumbent, cost_floor_usd=0.01)` so that delegation is allowed.

What is wired: `loop.yml` resolves the topic from the cron - three slots a day
per topic, kalshi 03:17/11:17/19:17 UTC, crypto 05:47/13:47/21:47, news
08:17/16:17/00:17, capped by the spec's `runs_per_day` - or `inputs.topic`; `live.yml` (Kalshi),
`live-news.yml` (weekday RTH slots), `live-crypto.yml` (55 minutes every hour). Migration
`supabase/migrations/008_topics.sql` is applied; the reader at
https://rsi.up.railway.app has a topic switcher and reads `topic`/`unit` on every row.

Paper trading: every harness keeps a simulated $1M book (`rsi_arena/trading/`,
migration 009, the reader's Trading page); PnL is reported beside skill, the
gate still promotes on skill. See `docs/design.md` Status 2026-09-23.

The crypto question set is a year deep as of 2026-09-24 (2025-09-24..2026-09-23,
365 UTC days). It was 92 days holding out 30, which `scripts/power.py` measures
against the real paired rollouts as resolving a gap of 0.041-0.070 pooled skill -
wider than anything the search produces, and wider than the +0.025 gen5 was
promoted on. Holding out 120 of the 365 resolves 0.020-0.035, twice as fine.
Binance's mirror serves a year of minute bars for nothing, so days were the only
thing ever stopping this. OKX funding history is not: it is a rolling three
months, so 268 of the 365 days answer `funding not recorded before 2026-06-19`,
which is the honest answer and not a gap to paper over. Open interest (daily) and
the perp's own 5-minute candles do go back a year and were backfilled.

What is not yet done: a 30-second crypto horizon (needs 1 s klines from a live
collector). Every topic has run at least one generation on Actions; see
`docs/design.md` Status 2026-09-22.

The section below is the state as of 2026-09-16 and is kept for its lessons.

## Read this first (2026-09-16)

**The OpenRouter account has about $12 of credit left**, of $5,626. Nothing runs
past that. Topping it up is the one thing nobody but the account owner can do,
and every item below is idle until it happens.

Six things were spending money and producing nothing, all found in one evening
by reading a scheduled run's log rather than its result:

- `_settings()` copied five of `Settings`' eleven fields, so `--per-fixture 8`
  parsed, printed in the log, and was discarded. Every scheduled generation
  evaluated all thirty-four windows of all four hundred and eighty-five matches.
  One was killed by the job timeout at exactly five and a half hours.
- The incumbent was scored on the whole train split and then all but the probe's
  hundred and sixty rollouts were thrown away, because the probe was chosen
  after the search instead of before the spending. About ninety dollars a
  generation.
- `gepa.optimize` raises `ImportError` when `display_progress_bar` is set and
  tqdm is missing, and tqdm was not a declared dependency. A run paid for its
  entire nineteen-minute baseline and died on the opening line of the search.
- A scheduled run has no `inputs`, so the question-set step called
  `--holdout ''`; argparse rejected it and the benchmark was never rebuilt. The
  step reported success because it pipes into `tee` and a pipeline's exit code
  is the last command's.
- The publish step's condition read `env.SUPABASE_DB_URL` from the step's own
  `env:` block, which is not in scope when the condition is evaluated. The
  reader had never once been fed.
- Nothing bounded a generation's spend at all. `max_usd` bounds one window.

**The gate could not see its own search.** `scripts/power.py` asks, from the real
paired rollouts, what effect the bootstrap can resolve. On thirty-five held-out
matches: about 0.027 pooled skill. The best rewrite anyone has found moved it by
under 0.01. So three generations of "no improvement" were statements about the
sample size. Held-out is a hundred matches now, a sixty-match audit set is frozen
for confirming promotions, and every interval reports `detectable` beside `diff`.

**The search now has a memory.** `runs/archive.json` keeps every candidate any
search has proposed with its per-instance score matrix, and the next generation's
parent is sampled off the frontier rather than always being the incumbent. All of
it was already in `gepa_state.bin` and never read; `scripts/backfill_archive.py`
recovered sixteen candidates from the rejected runs without paying again.

**The tool allowlist had never once been mutated** across fourteen candidates,
because `make_reflective_dataset` handed all three components byte-identical
records. Each component now sees evidence it can act on.

## State of the code

| Piece | Where | Status |
|---|---|---|
| Harness contract (JSON: context, config, tools, plan of prompt/tool/loop steps) | `rsi_arena/harness/spec.py` | done; loads the old repo's harness files unchanged |
| Runner (budget, trace, tool-calling loop, templating, restricted conditions) | `rsi_arena/harness/runner.py`, `template.py`, `tools.py` | done, tested offline |
| OpenRouter client with disk cache, retries, structured outputs | `rsi_arena/harness/llm.py` | done, **never called against the real API** |
| Kalshi data layer (stdlib only) | `rsi_arena/kalshi/_*.py` | copied verbatim, verified live on GitHub Actions |
| Frozen tools, match timeline, staleness rule | `rsi_arena/kalshi/replay.py` | done, verified live |
| Task protocol, evaluate, split by group | `rsi_arena/loop/task.py` | done |
| GEPA adapter with per-component reflection prompts | `rsi_arena/loop/adapter.py` | done offline, **never run with a real reflection model** |
| Gate: paired bootstrap on held-out, no held-in regression, cost cap | `rsi_arena/loop/gate.py` | done |
| Generation records and lineage | `rsi_arena/loop/generation.py` | done |
| Kalshi horizon topic (windows, score, background, feedback) | `rsi_arena/topics/kalshi_horizon/` | done |
| CLI `rsi-arena windows / bench / optimize / show` | `rsi_arena/cli.py` | done; `windows` verified live |
| GitHub Actions: `ci` (pytest) and `loop` (workflow_dispatch) | `.github/workflows/` | done; `loop windows` ran twice successfully |
| Question set: 31,243 windows over 930 matches in 72 competitions, thinned to 8 a match | `benchmarks/windows/` | committed, built by the workflow |
| Tests, 32, offline, fake model and fake history | `tests/` | passing |

**Model calls have now happened.** Baseline, and two full generations against
the real API. What they found is in `docs/design.md`'s Status section; the short
version is that three defects were invisible against fakes and all three were in
the measurement rather than the harness:

1. The HTTP client was bound to a loop that no longer existed, so every run
   exited 1 after printing correct results.
2. The optimizer's objective and the gate's statistic disagreed about the
   quarter of windows where the market did not move, and GEPA found the gap
   immediately.
3. The interval was resampled over windows rather than matches, and two held-out
   matches cannot support an interval at all.

The question set is 485 matches now, not five, because the gate's power turned
on that and not on the optimizer. (930 across seventy-two competitions as of
2026-09-27; see the 2026-09-27 section above.)

## The question sets roll every Sunday (2026-09-23)

`roll.yml` runs `scripts/roll_question_set.py` at 02:00 UTC on Sundays, one
step per topic, and commits only `benchmarks/` paths. Each topic appends the
markets its venue settled since the set's newest group — Kalshi fixtures newer
than the newest event-ticker date, crypto days after the benchmark's `to`, news
sessions after the latest New York date — and then prunes back to `--keep`
groups by recency: 1,000 matches, 365 days, 2,350 symbol-days - the sizes the sets
had when the roll began, except crypto, whose quarter was deepened to a year on
2026-09-24 because thirty held-out days could not resolve any gap the search
produces, and Kalshi, whose 485 became 1,000 on 2026-09-27 when the sweep went
from eight leagues to seventy-three. Raising that keep rather than displacing the
oldest matches, because pruning to 485 by recency would have dropped half the
Premier League and LaLiga history to fit the Ekstraklasa in — narrower where it
was deep, to be broader where it was empty. It costs disk (33 KB a match) and
nothing at the gate: `holdout`, `audit`, `cascade` and `valset` are absolute
counts, so a generation buys the same number of window evaluations whatever the
set's size, drawn from a wider pool. `--keep` is a count of *groups*, not of windows or instances, because
the split is by group and power comes from groups. The
procedure, the dedupe rules and the full list of what a roll must never do are
written out in the script's module docstring and in `docs/design.md`, *How the
question sets roll*.

A roll reshuffles which groups are held out. `three_way_split` shuffles the
sorted group ids on a fixed seed, so adding a match or dropping a day moves
other groups between train, held-out and audit. That is fine and intended: the
scoreboard keys on instance ids, not on split membership, so every remembered
answer stays attached to its own question and nothing is re-scored by accident;
and the audit set is supposed to be a fresh cut rather than a monument, which is
the whole point of not asking every generation about the same weeks in
September. What a roll may never do is change an existing window — `id`,
`mid_now`, `realised`, quotes, path — and it fingerprints every surviving file
before and after to prove it did not.

Failures are designed to be quiet, because a weekly job that goes red every
third week is a job nobody reads. One venue down is one table row; all three
down is the only thing that opens an issue. A topic that fails validation
(`rsi-arena windows --json`, `scripts/preflight.py`) has its paths restored from
the checkout and reports `roll reverted: <reason>`, green, with last week's set
intact. A `loop.yml` run in progress makes the roll wait up to thirty minutes
and then skip the week. Run it by hand with `--dry-run` to see what a roll would
add and remove without touching anything.

## What to do next, in order

1. **Get a key in.** Locally: `export OPENROUTER_API_KEY=sk-or-...`. On GitHub:
   repository secret `OPENROUTER_API_KEY`. OpenRouter bills against prepaid
   credits; a 402 means no credits and is not retried.
2. **Baseline.** `rsi-arena bench --split holdout` then `--split train`.
   Measured on the small set: pooled skill +0.043 held out under the floored
   metric, and a noise band of about ±1 point across repeated runs. Roughly half
   of forecasts echo the current mid. Run the baseline more than once with
   `--no-llm-cache` before believing any gain — the band is the same size as the
   effect.
3. **One generation.** `rsi-arena optimize --run-dir runs/gen1 --max-metric-calls 200`.
   Read `runs/gen1/manifest.json` (both scoreboards, the search, the verdict),
   `runs/gen1/best.json` (what GEPA wrote), `runs/gen1/rollouts/*.json` (every
   scored window), and `runs/gen1/gepa/` (GEPA's own state and candidates).
4. **Look at what GEPA actually wrote** before trusting the number. The plan
   component is JSON extracted from a ``` block; check it parsed as intended
   and that the reflection prompts in `loop/adapter.py:reflection_templates`
   produced sensible rewrites. This is the least-exercised seam.
5. **Continue or stop.** `rsi-arena optimize --harness runs/gen1 --run-dir runs/gen2`
   starts from the candidate if it was accepted, the incumbent if not.
   `rsi-arena show runs/gen2` prints the lineage. The experiment in
   `docs/design.md` is three generations; report held-out skill against the
   seed baseline.

On GitHub, the same three commands are the `loop` workflow's `command` input;
results are committed to `main` and summarised on the run page — except
`rollouts/` and `gepa/`, which are gitignored and go to
`s3://$TRACE_BUCKET/rsi-arena/<topic>/[<lineage>/]<run>/` instead. A run you did not just
produce yourself has only its light files in the checkout; `scripts/fetch_run.py
<topic> <run>` brings the rest back, `scripts/s3_usage.py` says what is up there.
`docs/design.md`, "What a later run needs from an earlier one", is the reasoning
and the story of the three generations that were lost before it was written.

## Invariants: do not break these

- **A harness is JSON and binds tools by name.** The loop mutates text
  components (`context`, `plan`, `tools`) and rebuilds through
  `Harness.from_components`, which validates. Keep every rewrite loadable and
  every failure a readable `HarnessError`.
- **Point in time is the whole benchmark.** Frozen tools stop at the instant;
  trades are bounded by the API's `max_ts`, never trimmed afterwards; the game
  state is replayed from timestamped events, never read from the final score.
  A harness that names a tool the frozen box lacks fails at load.
- **Split by fixture, never by window.** Fifty windows on one match are fifty
  correlated observations of one game.
- **A failed run counts as silence, not as missing.** Dropping failed windows
  would reward failing on hard ones.
- **The gate is the only promotion path.** GEPA's own best-on-train is not a
  result; held-out gain with a bootstrap interval above zero is.
- **`loop/` knows nothing about Kalshi.** Topic knowledge lives in `topics/`.
- **Tests stay offline.** No key, no network; use `tests/conftest.py` fakes.

## Risks and things to check on the first real run

- **Structured outputs.** The client sets `provider.require_parameters` when a
  schema is present, so a model whose OpenRouter providers do not support
  `json_schema` returns 503 rather than ignoring the schema. Sonnet 4.5 works;
  for others check the model's Providers page.
- **The reflection model returns prose around the block.** GEPA extracts the
  last ``` block. If plans come back unparseable, tighten the `plan` template
  in `reflection_templates`.
- **`raise_on_exception=False`** in `cli.py:cmd_optimize` keeps a bad
  candidate from killing a run but also hides exceptions; look in `runs/*/gepa/`
  logs if candidates all score zero.
- **Cost objective is a magic number.** `topics/kalshi_horizon/task.py`
  normalises cost as `1 - cost/0.05`; revisit once real per-window costs are
  known.
- **Concurrency.** Kalshi allows about 200 requests/s; ESPN is throttled at
  4/s inside `_gamestate.py`. The frozen-tool cache under `.cache/tools` means
  each window's three tool answers are fetched once, ever.
- **Caching hides noise.** With the LLM cache on and temperature 0, re-running
  the same harness on the same windows is free and identical. For a noise
  measurement pass `--no-llm-cache` and run the baseline more than once.
- **Staleness rule.** `kalshi/replay.py:MAX_STALE_S` (180s) drops windows
  whose last candle is older than three minutes. This is why each fixture
  yields 34 windows rather than 34 per ticker.

## Backlog, roughly in the order it would pay off

- **More fixtures, still.** `benchmarks/soccer-2026.json` has 177 across five
  leagues, built by `scripts/discover_fixtures.py`. Sixty soccer competitions
  are supported; the script takes `--league` as a comma-separated list. More
  matters because the gate resamples by match: the interval narrows with the
  number of *matches*, not windows.
- **Cascade evaluation.** Score a candidate on a cheap subset first and only
  run the full set if it clears the incumbent there. OpenEvolve does this;
  GEPA's `val_evaluation_policy` may be the hook.
- **Noise band.** Measured at about ±1 point on 68 windows; re-measure on the
  larger set, where it should be narrower, and report it beside every gain.
- **Population diversity.** GEPA runs one candidate lineage per seed; several
  seeds or several task models per generation would test the "diverse
  optimizer" claim in the original README.
- **Trading layer.** The old repo had `decide()`: turn a forecast and quote
  width into a fill against the live book net of fees. Not ported; the
  benchmark scores forecasts, not P&L. Port it only once skill is reliably
  above zero.
- **Non-verifiable topics and the arena UI.** Bradley–Terry over votes, the
  battle page. Not started; the old repo's server and web app could be reused.

## Conventions

- Python 3.10+, `httpx`, `pydantic`, `gepa`. `pip install -e ".[dev]" && pytest`.
- Docstrings say why, not what. Names are sentences: a test is named for what
  broke.
- Commit messages: a line of what, a paragraph of why.
- Keep `docs/design.md`'s Status section current when something changes state.
