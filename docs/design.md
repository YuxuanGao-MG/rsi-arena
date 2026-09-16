# Design: the alternative

## Status, 2026-09-15

**The loop has run end to end against the real API.** Everything below is what
that cost to learn, because none of it was visible against fakes.

Built and tested: the harness contract and runner (`rsi_arena/harness`), the
frozen-tool replay and match timeline (`rsi_arena/kalshi/replay.py`), the
topic-agnostic loop (`rsi_arena/loop`), the Kalshi horizon topic
(`rsi_arena/topics/kalshi_horizon`), and one CLI. Decisions A and D are settled.

### What the first real runs found

**The measurement was not self-consistent.** GEPA selects on the mean of
per-window scores; the gate promotes on pooled skill. On the quarter of windows
where the market did not move, the first said the window did not matter and the
second said it could only cost — so a candidate could climb one while sinking
the other, and the first thousand-call run did exactly that: mean +0.020, pooled
-0.044, on the same 102 windows. GEPA had followed its own correct reflection
("you are rewarded only for anticipating moves") and gone from predicting
movement on 6 of 29 dead markets to 27 of 29, which the objective it could see
scored as free. Fixed by flooring the benchmark's error at one tick in both
places, so saying nothing on a dead market is right and saying something is
wrong, visibly, to both halves.

**The interval was drawn over the wrong unit.** Thirty-four windows of one match
are one match seen thirty-four times. Resampling them independently reported
±6.8 points on a baseline whose skill is 4.3 — an interval wider than the
quantity, so nothing could ever be promoted — while also overstating the
evidence. It resamples by match now.

**Two held-out matches cannot support any inference.** A cluster bootstrap over
two groups draws only {A,A}, {A,B}, {B,B}. Below eight groups the gate reports
the interval as unusable and refuses to promote at all.

**Silence has to score zero, and twice it did not.** Flooring the benchmark put
the floor in the numerator too, so a harness that said nothing scored +0.046 —
caught when two models that echoed the mid on every window posted the best
number in a model comparison. Then, with that fixed, clipping let the mean rise
while the pooled sum fell. The answer was to stop averaging skill, which divides
by a per-window benchmark, and average the numerator the pooled statistic sums
instead: over a fixed set of windows its denominator is constant, so the two are
monotone by construction rather than by hope.

**The noise band is about ±1 point.** Three runs of the same harness over the
same windows scored -1.18%, +1.18%, +1.18%. That is the same order as the effect
the loop exists to detect, and it belongs beside every gain ever reported.

**Cost is $0.013 a window**, and a candidate that grew the context roughly
doubled it.

### The question set

485 matches across nine leagues, 11,146 windows on disk, built by
`scripts/discover_fixtures.py` from settled Kalshi events that link to a fixture
with a usable timeline. A generation thins to eight windows a match — about
3,900 — because power comes from matches, not from windows within one.

Up from five matches and 170 windows. The last jump was 168 matches recovered in
one change: Kalshi dates an event by the day it listed the contract and the
fixture feed by the day it kicked off, so Sevilla against Valencia trades as
26SEP13 and was played on the 11th. Looking one day either side of the ticker's
date lost a quarter of the settled events; three days catches them.

### What the gate can see

Asked, finally, by `scripts/power.py`, from the real paired rollouts rather than
an assumed variance. On a thirty-five match held-out set the smallest pooled
gap the gate can resolve at 80% power is about **0.027**. The best rewrite
anyone has found moved held-out skill by **under 0.01**.

So the gate could not see its own search's output, and three generations of "no
improvement" were statements about the sample size rather than about the
candidates. The held-out set is a hundred matches now — also above the forty
clusters below which a pairs cluster bootstrap over-rejects — and every interval
the gate draws reports `detectable` beside `diff`, so a rejection that was never
winnable says so rather than looking like a verdict.

The honest reading is that this is a resolution problem the benchmark cannot
fully solve: even all 485 matches as held-out would only resolve 0.007. What
closes the gap is evidence accumulated across generations, which is what the
archive is for, not a larger single experiment.

### The model is a bigger lever than the harness, so far

Measured on the same sixty-eight held-out windows, under the corrected metric:

| model | skill | $/window | echoed the mid |
| --- | --- | --- | --- |
| **Opus 5** | **+0.106** | 0.0353 | 11/68 |
| gpt-5-mini | -0.008 | 0.0025 | 7/68 |
| Sonnet 4.5 | -0.011 | 0.0130 | 37/68 |
| Astra 6 | -0.026 | 0.0543 | 11/68 |
| Haiku 4.5 | -0.100 | 0.0046 | 37/68 |

Every model but one is at or below silence. Opus 5 clears it by ten points with
no failed runs, an MAE of 0.0388 against no-change's 0.0437, and +0.174 on the
forty-eight windows that actually moved — it is more willing to speak and right
when it does.

Two readings of that, and both matter. Swapping the model moved skill by twelve
points while the best rewrite so far moved it by less than one, which is a
warning about the premise: the variance may not be in the harness. But every
generation until now optimised a harness whose baseline could not beat saying
nothing, and there is not much for a rewrite to find on a negative baseline.
Opus 5 is the first starting point with something to improve on.

The harness and the reflection both run on Opus 5 now. The reflection model is
under three per cent of a generation's bill — tens of calls against thousands —
and rewriting a harness from its own failures is the part that most rewards
reasoning.

### Still open

- No generation has been accepted. Two have been rejected honestly.
- Transfer is unmeasured: every number is soccer at a five-minute horizon.
- Nothing re-scores an old winner, so persistence is unchecked.

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
