# Design: the alternative

## Status, 2026-09-13

Built and tested offline: the harness contract and runner (`rsi_arena/harness`),
the frozen-tool replay and match timeline (`rsi_arena/kalshi/replay.py`), the
topic-agnostic loop (`rsi_arena/loop`: Task protocol, GEPA adapter, gate,
generation records), the Kalshi horizon topic (`rsi_arena/topics/kalshi_horizon`)
and one CLI (`rsi-arena bench | optimize | show`). Decision A below
is settled: a slim runtime with the same JSON contract, no dependency on the
earlier package. Decision D is settled: this repository.

The live data path is verified on GitHub Actions: the `loop` workflow built
the question set from Kalshi and the fixture feed and committed it under
`benchmarks/windows/`, 170 windows over five EPL fixtures, 34 per fixture, 115
of them moving a cent or more over the horizon. `bench` and `optimize` wait on
the `OPENROUTER_API_KEY` repository secret; their numbers go in `runs/`.

## Goal

A recursive self-improvement loop that runs end to end in days, scored by a
verifiable benchmark rather than votes. The existing repo has the runtime and
the UI and none of the loop. This one starts from the loop.

## The loop

```
population (harness JSON, generation N)
   │
   ▼
benchmark each harness on a fixed, cached window set  → pooled skill, cost, echo rate
   │
   ▼
select top-K, paired on identical windows (Bradley–Terry over pairs, not Elo over votes)
   │
   ▼
optimizer: an LLM is handed a parent harness, its worst windows with traces,
           the tool catalogue and the scoring rule, and writes a child
   │
   ▼
promotion gate: child beats parent on the same windows by more than noise,
                at no more than 2× the cost; else discarded
   │
   ▼
generation N+1, appended to the population store. Repeat.
```

## Framework

The optimizer is not written here. `docs/frameworks.md` surveys the open-source
options; the choice is **GEPA** (MIT, reflective text evolution with a
per-instance Pareto frontier) for the search, with the held-in/held-out
acceptance rule from Self-Harness and Meta-Harness layered on top. ShinkaEvolve
is the fallback engine if the harness moves from JSON to Python. What we write
is the evaluator, the harness runner, and the gate.

## Components

| Package | Does | Size guess |
|---|---|---|
| `harness/` | The JSON contract (same shape as the earlier `Agent.to_dict()`, so configs move both ways) and a slim runner: prompt, tool and loop steps, flat state, templating, cost ledger | built |
| `loop/task.py` | The `Task` protocol: instances, toolbox per instance, run inputs, score, pooled statistic. `evaluate()` and `split_by_group()` written against it | built |
| `loop/adapter.py` | `TaskAdapter` for GEPA, and one reflection prompt per component so a JSON plan is not rewritten as prose | built |
| `loop/gate.py` | Paired bootstrap over shared instances; accept only on held-out gain, no held-in regression, bounded cost | built |
| `loop/generation.py` | One run directory per generation with a manifest; lineage walks parents back to the seed | built |
| `topics/kalshi_horizon/` | Windows from finished fixtures, the skill score, the task's feedback and background | built |
| `ratings/` | Later. Bradley–Terry over paired outcomes for non-verifiable topics | — |
| `arena/` | Later. Battles and votes; the earlier server and web app could be reused | — |

## Decisions that change the work

**A. Runtime: rewrite slim, or depend on `rsi_arena`?**
Recommend rewrite, keeping the JSON contract byte-compatible. A dependency on
Sean's package means every runtime change goes through his repo; a slim rewrite
is a few hundred lines and the Kalshi tools bind by name either way.

**B. Model access: OpenRouter or a direct provider SDK?**
Recommend OpenRouter to start, because the population-diversity argument in the
README needs several model families and one client. Revisit if structured
output reliability stays a problem.

**C. First fitness function.**
Recommend the Kalshi five-minute horizon replay. It is verifiable, cheap, gives
hundreds of labels per fixture, and the base harness sits at skill ≈ 0, so any
improvement is visible. Web research with an LLM judge is the fallback and is
much noisier.

**D. Where this lives.**
A standalone private repo, once created. The GitHub app available to this
session cannot create repositories, so that is a manual step.

## Experiment 1

Can an LLM rewriter beat skill ≈ 0 on the fixed benchmark without overfitting it?

- Split the five benchmark fixtures three train, two held out.
- Baseline: the current `kalshi-horizon-5m` harness, run three times on train
  to measure noise.
- Each generation: 8 children from the top 2 parents, 3 generations, one
  model for the rewriter and one for the harness to keep the variable count at
  one.
- Report per generation: pooled skill on train and on held-out, skill on moved
  windows, echo rate, cost per window, and how many children passed the gate.
- Success is a held-out skill improvement larger than the measured noise band.
  A train-only improvement is the overfitting result and is worth reporting
  too.

Budget: at roughly $0.005 per window and ~200 windows per fixture, one harness
over three train fixtures is about $3; 24 children plus baselines is under $100.
