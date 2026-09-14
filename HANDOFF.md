# Handoff: rsi-arena

Written 2026-09-14 for a Claude Code desktop session picking this up. Read this
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
| Question set: 170 windows, 5 EPL fixtures, 34 each, 115 moved >= 1c | `benchmarks/windows/` | committed, built by the workflow |
| Tests, 32, offline, fake model and fake history | `tests/` | passing |

The one thing that has not happened: **no model call has been made yet.**
`bench` and `optimize` have run only against fakes.

## What to do next, in order

1. **Get a key in.** Locally: `export OPENROUTER_API_KEY=sk-or-...`. On GitHub:
   repository secret `OPENROUTER_API_KEY`. OpenRouter bills against prepaid
   credits; a 402 means no credits and is not retried.
2. **Baseline.** `rsi-arena bench --split holdout` then `--split train`.
   Expected from the old repo's measurements: pooled skill about zero, run to
   run between -5% and +1%; 70 to 80% of forecasts echo the current mid. If the
   first real call fails, the likely causes are listed under Risks.
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

- **More fixtures.** `benchmarks/epl-2026-09.json` has five. A fixture needs
  the league, the ESPN event id (`game`), the Kalshi event ticker and its
  market tickers. Other leagues in `kalshi/_taxonomy.py:COMPETITIONS` work the
  same way; `rsi-arena windows` builds and commits the windows.
- **Cascade evaluation.** Score a candidate on a cheap subset first and only
  run the full set if it clears the incumbent there. OpenEvolve does this;
  GEPA's `val_evaluation_policy` may be the hook.
- **Noise band.** Run the baseline several times uncached and report the
  spread next to every gain.
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
