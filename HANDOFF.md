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

## Read this first (2026-09-22)

**Three topics now, one loop, one reader.** `rsi-arena topic --topic <name> --json`
prints what each runs on; the workflows read the same spec with `--shell`.

| topic | instance | unit / tick | seed harness | question set | keys |
|---|---|---|---|---|---|
| `kalshi-horizon-5m` | a Kalshi soccer contract at an instant, 5 min out | cents / 1c | `harnesses/horizon-5m-jev.json` (Jev; the Opus lineage's 11 generations stay under `runs/`, the Jev lineage is `runs/kalshi-jev/`) | 485 matches, `benchmarks/windows/`; held-out 300 | OpenRouter |
| `crypto-horizon-1m` | BTC/ETH/SOL spot at an instant, 1 min out, every 5 min | bps / 2 | `harnesses/crypto-horizon-1m-jev.json` (Jev) | 92 UTC days, 2,208 windows, `benchmarks/windows-crypto/` + `benchmarks/crypto-data/` | none for data |
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

What is not yet done: a 30-second crypto horizon (needs 1 s klines from a live
collector) and more crypto days (the gate resolves 0.050 on 30 held-out days;
news resolves 0.009 on 300 symbol-days). Every topic has run at least one
generation on Actions; see `docs/design.md` Status 2026-09-22.

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
| Question set: 11,146 windows over 485 matches in nine leagues, thinned to 8 a match | `benchmarks/windows/` | committed, built by the workflow |
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
on that and not on the optimizer.

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
results are committed to `main` and summarised on the run page.

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
