# Design: the alternative

## Status, 2026-09-23: the first promotion

The Kalshi lineage restarted on Jev on the 22nd (`runs/kalshi-jev/`, held-out
300 matches, audit 60, eight windows a match) and its first generation was
**accepted** - the first promotion in the arena's history, after eleven Opus
generations that could not be told from noise:

| | held-out skill | echoed the mid | on moves |
| --- | --- | --- | --- |
| Jev seed | -0.0145 | 206 / 2,400 | -0.0001 |
| gen1 candidate | -0.0095 | 571 / 2,400 | +0.0002 |

Gain +0.005, interval +0.003 to +0.007 on a test that resolves 0.0024; the
audit set the search never saw agreed (+0.004, +0.000 to +0.008). Eight
candidates, 5,376 calls, $5.10. What the rewrite changed was the context
alone: the tools, plan and model are the seed's. What the context bought is
visible in the echo column - the candidate says "no move" on a quarter of
held-out windows where the seed guessed, and on the windows that moved it is
no better. So this is a real, confirmed improvement in the metric, and an
honest reading of it is that the harness learned when to stay quiet, not yet
how to see a move coming. Both numbers are still below zero.

The gate could see it because the price of a window fell two hundred-fold:
a held-out set of 300 matches resolves 0.0024 where a hundred resolved 0.050.
Twenty of the twenty-two generations before this one had rejections that
were statements about sample size; this one is a statement about the harness.

## Status, 2026-09-22: three topics, and what the price bought

Two new topics run beside Kalshi, on the same loop, gate and reader, both on
TypeSafe's Jev as the incumbent. Their first generations, on GitHub Actions:

| topic | question set | held-out groups | resolves | baseline | candidate | spent |
| --- | --- | --- | --- | --- | --- | --- |
| crypto-horizon-1m | 92 UTC days, 2,208 windows | 30 days | 0.050 | -0.016 | -0.013 | $5.09 |
| news-equity-5m | 5,102 items, 2,246 symbol-days | 300 symbol-days | **0.009** | -0.007 | -0.007 | $6.64 |

Both rejected, both correctly: neither rewrite moved held-out skill outside
its interval. The number to read is the third column. The Kalshi gate on a
hundred matches resolves about 0.050; the news gate on three hundred
symbol-days resolves 0.009, for a generation that costs a tenth as much,
because a Jev window costs two thousandths of a cent and the search judged
every candidate on the whole question set. The crypto gate is where Kalshi's
is only because its groups are days and the set has ninety-two of them; the
lever there is more days, not more windows.

What the rewriter did with the money: the news search proposed 58 times,
accepted ten candidates and stopped on its call budget, having rewritten the
context, the plan, the tool list and the model; the candidate echoed the
mid on 9 held-out windows against the incumbent's 53. The crypto search
proposed 23 times for 7 candidates. On neither topic is the incumbent above
silence yet.

Two things the first generations found, both fixed: a Jev incumbent's
judgment had to be priced at the topic's own cost floor (the reserve was
$24 against a $5 ceiling), and the reader keyed runs by directory name, so
the crypto loop's gen1 overwrote Kalshi's gen1 rollouts; other topics now
publish as ``gen1@<topic>``.

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

**Cost is $0.034 a window.** Measured, from gen5: $12.08 for 357 windows that
reached the model. The figure here said $0.013 for a month, from a run under a
different task model, and the gap is why a generation was believed to cost $50
when it costs $86 at eight windows a match. `scripts/preflight.py` now refuses
to start a split whose ceiling cannot buy it, which is the check that would have
caught it before two runs died for money rather than for evidence.

At a hundred held-out matches and four windows each: **a cold generation is
about $53, a warm one about $35** — warm meaning the incumbent's held-out
rollouts are still cached, which holds until the held-out set rotates. Two a
day is therefore $70–$106, not the $60 the workflow's own comment claimed.

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

### The judgment is paid for before the search spends (2026-09-21)

gen11 spent $64.48 of a $64.26 ceiling and produced no verdict. The ceiling
was one pot priced at the incumbent's 3.3 cents a window; the search ran on
candidates that carried a second prompt step at 5.5 cents, the cascade paid at
that price too, and held-out got $6.50 of the $22 it needed. 339 of 400
windows scored as silence and the gate refused to read the comparison. The
money was short by about $8, and the order of payment turned $8 into $64.

The pot is now split before the search sees it (`loop/budget.py`). Once the
baseline is scored, judging a candidate is priced at the incumbent's measured
rate over probe and held-out, at the most the gate lets a candidate cost
(`max_cost_ratio`, 2x) - and the cascade now rejects on cost at the probe, so
nothing that reaches held-out costs more than was kept back for it. The search
gets the remainder and stops on dollars, one accepted candidate early, at the
rate it is actually paying. Held-out is bought whole or not at all. Preflight
prices the same three shares; the workflow's margin drops from 1.35 to 1.1
because two of the three are now caps rather than guesses.

### A model that answers with distributions (2026-09-21)

TypeSafe's Jev (`typesafe/jev-1.13`, on OpenRouter through the alpha
Decisions API, and on the OpenMesh API at `POST /v1/decisions`) writes no
text. It takes a state and typed questions and returns calibrated
probabilities; a `score` question over ordered levels comes back as a
distribution and its expectation. It answers in a few hundred milliseconds at
$0.042 a million input tokens, output free: about two thousandths of a cent a
window, against 3.3 cents for Opus 5.

The harness carries it without a new contract. The plan already runs its
tools as fixed steps, which is the only way a model that calls nothing can
have them; the last prompt step carries `questions` in place of an output
schema - seven levels of cent moves with a value each - and `answers` maps
the distribution onto the output fields: the mean for `delta_cents`, the
tightest band holding sixty per cent of the mass for `half_width_cents`.
Both live in the plan, so the optimizer rewrites the levels and the mapping
like any other part of it. `harnesses/horizon-5m-jev.json` is the base;
`rsi_arena/harness/decisions.py` has the arithmetic; the spec refuses a
decisions model on a text step and a chat model on a questions step at load.

Measured, paired on gen11's four hundred held-out windows against the
archived Opus 5 answers (`scripts/compare_on_rollouts.py`):

| harness | skill | on moves | echoed | $/window |
| --- | --- | --- | --- | --- |
| Opus 5, incumbent | -0.002 | +0.063 | 88/400 | 0.0329 |
| Jev, first seven levels | -0.013 | +0.002 | 41/400 | 0.0002 |

Difference -0.011, 95% interval -0.049 to +0.022, on a test that resolves
0.050: not distinguishable, on an untuned question. What is different in kind
is the price. At two thousandths of a cent a window the whole question set -
eleven thousand windows - is about two dollars, which is the resolution
problem the gate has never been able to buy its way out of: every generation
can be judged on every match, and the interval that could not see a 0.01
gain at a hundred matches can see it at four hundred and eighty-five.

What Jev cannot do is choose tools or explain itself, so for it the toolbox
is the whole of what it knows and the plan is the whole of how it looks. The
next lever is therefore the frozen toolbox itself: more derived tools (price
velocity, minutes since the last goal, settlement countdown, book depth,
the tape's imbalance), and plans that branch on a cheap `noul` answer -
"will this market move at all" - before paying for the rest. Its answers
are conditions the runner already evaluates in `skip_if`.

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

### The topic seam (2026-09-21)

Two more tracks are coming — US equities on news, and crypto spot, both
forecasting a five-minute move in basis points — and the question was what
they would have to know about Kalshi to plug in. The answer used to be: the
CLI's defaults, the "model" reflection prompt, preflight, the publisher, the
live grader, and the scoreboard's file name. Now it is a package and a
`TopicSpec` in `topics/__init__.py`.

What moved: the score, with its unit made a parameter
(`topics/_common/metric.py`; `Metric.KALSHI` reproduces `kalshi_horizon/score.py`
to 1e-12 on every case that file's tests state, and the Kalshi task still
scores through its own file); thinning and live grading (`_common/thin.py`,
`_common/live.py`); the frozen-tool cache (`harness/toolcache.py`). The loop
reads a handful of optional attributes off a task with `getattr` — `metric`,
`moved`, `label`, `context_of`, `instance_from_dict`, `use_instances`,
`model_notes` — and falls back to Kalshi's behaviour when they are absent.
`rsi-arena topic --shell` prints a topic's spec for a workflow to `eval`, and
`--harness`, `--benchmark`, `--windows-dir`, `--per-fixture`, `--window-usd`,
`--runs-dir` and `--model-choices` default to the topic's rather than to
Kalshi's. Each topic gets its own archive and scoreboard, flat in `runs/`; the
first keeps the names its committed files already have. `rsi-arena windows`
prints the same groups as before, in the topic's unit.

### The second venue: a US stock at the second a story broke (2026-09-21)

`news-equity-5m` is the first topic through the seam. An instance is a
Benzinga item on a US stock or ETF, timestamped to the second by Alpaca's
news feed, paired with one symbol it names; the tools are frozen at
`created_at`; the answer is the symbol's last complete IEX minute close five
minutes on, in basis points. Regular hours only, from five minutes after the
open to a minute before the close.

The point-in-time rule is different on this venue and is written once, in
`alpaca/_bars.py`: Alpaca stamps a bar with its open, so a bar is known when
`ts_open + 60s` is at or before the instant, and a price is the close of the
last known bar within three minutes. Every tool in `alpaca/replay.py` reads
through that rule; prints and news are bounded by the API's `end` and
checked again on each timestamp. The box has twenty tools in the same five
families the Kalshi box grew into, with the derived reads a decisions model
needs (`move_base_rate` at this time of day over the prior ten sessions,
`tape_imbalance` by the tick rule, `news_absorption`, `state_summary`).

Nothing has been fetched yet: there are no Alpaca keys on the machine. The
client reads them late, every read is tested against fakes and an
`httpx.MockTransport`, and `scripts/discover_news.py --dry-run` runs the
whole discovery against a synthetic tape and then prints the command to run
once `APCA_API_KEY_ID` and `APCA_API_SECRET_KEY` exist. Discovery fills a
per-symbol-day bar store under `benchmarks/news-data/bars`, so once it has
run, replay is offline like Kalshi's. The benchmark is committed empty and
preflight says so in a sentence rather than a traceback.

The seed is the Jev harness: at two thousandths of a cent a window the
question set can be judged whole, which is the resolution the Kalshi topic
could never buy. The metric is `Metric(tick=5, scale=100, unit="bps",
relative=True)`; a large cap quotes one to five basis points wide, so a move
under five is inside the spread.

### One workflow, several topics (2026-09-21)

`loop.yml` runs any topic the CLI knows. Which one a firing is for comes
from the dispatch input or, on a schedule, from the cron string that fired
(a `case` in the `resolve` job: 03:17, 05:47 and 09:17 UTC are Kalshi, 07:17
and 08:47 crypto, 11:17 and 12:47 news). What that topic runs on comes from
`rsi-arena topic --shell`, evaluated once into the job's environment, and
every step reads `$BENCHMARK`, `$HOLDOUT` and the rest rather than repeating
a Kalshi literal; a typed dispatch input overrides the spec. The once-a-day
guard, the chain, preflight, optimize, publish and the commit all key on the
topic and its runs directory, and the concurrency group is per topic, which
is why the resolve step is its own job: a concurrency key cannot read the
environment, but it can read another job's output. A topic on the schedule
before its package is on main - the crypto crons today - ends green with a
notice rather than an issue every morning.

For that to leave a scheduled Kalshi run byte-identical, the Kalshi spec had
to be what the workflow had carried as literals for a month - soccer-2026,
a hundred held out, sixty audited, four a fixture - and the spec is read off
`Settings`, so those four dataclass defaults moved to the production values.
`epl-2026-09.json` is still there as the five-match dev set.

`live-news.yml` and `scripts/collect_live_news.py` are the news topic's
live half: four forty-five-minute sweeps a weekday inside regular hours,
polling the feed every minute and forecasting each new item on the universe
once through `live_tools` and the Jev harness, graded through
`topics/_common/live.py` when the bar five minutes on prints, published with
the topic, symbol, venue and unit that migration 008 added. Without the
Alpaca secrets it skips, green.

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
