# rsi-arena

Harnesses that compete, and a loop that rewrites the losers. First application:
five-minute price forecasts on Kalshi soccer contracts, scored by replay against
finished matches.

A **harness** is JSON: a context, a model config, the tools it may call, and a
plan of prompt, tool and loop steps. The runner executes it against a toolbox
the host supplies. The **benchmark** puts a harness back at fixed instants of
finished matches with tools frozen there and scores what it predicted against
what the market printed. The **optimizer** is [GEPA](https://github.com/gepa-ai/gepa):
it reads the traces of the windows a harness lost and rewrites the harness. A
**gate** promotes a rewrite only if it beats the incumbent on fixtures the
optimizer never saw.

```
rsi_arena/
  harness/    the JSON contract, the runner, the model client
  kalshi/     Kalshi and fixture data (stdlib only), tools frozen at an instant
  bench/      fixed windows, the skill score, the evaluator
  optimize/   the GEPA adapter, the acceptance gate, the loop CLI
harnesses/    horizon-5m.json, the base harness the loop exists to beat
benchmarks/   epl-2026-09.json, five finished EPL fixtures
docs/         design, framework survey, notes on the prior codebase
tests/        offline: no key, no network
```

## Run

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest                                            # offline, a second or two

export OPENROUTER_API_KEY=sk-or-...
python -m rsi_arena.bench --split holdout         # baseline numbers, pooled
python -m rsi_arena.optimize --max-metric-calls 300 --run-dir runs/gen1
python -m rsi_arena.optimize --harness runs/gen1/best.json --run-dir runs/gen2
```

Kalshi reads need no credentials. The first run of a fixture fetches its
windows and caches them under `.cache/`; frozen tool answers and model calls
are cached too, so re-scoring a harness costs nothing.

## The score

Benchmark is no change. Per window, `skill = 1 - |predicted - realised| /
|mid_now - realised|`, pooled by summing errors across windows. Reported to the
optimizer as `0.5 + skill/2`. Windows where the market did not move carry no
information and are flagged. The base harness sits at skill about zero and
echoes the current mid most of the time; that is the number to beat.

## The gate

Fixtures are split into train and held-out by fixture, never by window. GEPA
sees only train. A candidate is accepted when the paired bootstrap of its
pooled-skill difference against the incumbent on held-out windows sits above
zero, it does not regress on train, and it costs at most twice as much.

## Docs

- [`docs/design.md`](docs/design.md): the loop, the components, the first experiment
- [`docs/frameworks.md`](docs/frameworks.md): the open-source options and why GEPA
- [`docs/sean-runtime-notes.md`](docs/sean-runtime-notes.md): what the earlier codebase contains
