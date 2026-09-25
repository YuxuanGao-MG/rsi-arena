# Design: the alternative

## Status, 2026-09-24: the seeds quote a width on purpose, and reach for the whole box

The book's finding was about the harnesses, so the harnesses are what changed.
Two things were wrong with every seed and neither was a bug.

**The width was not a decision.** Each Jev seed derived `half_width` from the
move distribution with `{"as": "half_range", "coverage": 0.6}`, which is a
statement about how unsure the model is rather than about what market it would
stand behind — and a distribution piled on one level reads as zero, which the
engine widens to the venue tick. Measured over the committed sets, the Kalshi
half-width landed at 1.00c on essentially every window against a five-minute
path that reaches 2.5c from the mid at the median and 17.5c at the ninetieth.
Each Jev seed now asks a `score` question of its own, `width`, over five
venue-appropriate levels from the tick to well past the median path — Kalshi
1/2/4/8/16 cents, crypto 2/5/10/20/40 bps, news 5/12/25/50/100 bps — mapped
`{"from": "width", "as": "mean", "min": <tick>}`. The instructions say the thing
plainly: this is the half-width of the market you post, the path will travel
through one that is too narrow and take both your sides at prices the market has
already left, and a width nobody reaches costs exactly nothing. The three chat
seeds keep their schema keys and say the same thing in the property description.

`scripts/width_fills.py` is what says whether those five levels bracket the
range the answer changes over. It reads the realised path the sets now carry and
fills a quote centred on the mid the way `Book.post` does:

| half width | Kalshi both / one | crypto both / one | news both / one |
| --- | --- | --- | --- |
| tick | 34% / 45% | 18% / 61% | 30% / 61% |
| 2× | 16% / 44% | 4% / 42% | 9% / 51% |
| 4× | 6% / 33% | 1% / 16% | 2% / 24% |
| 8× | 1% / 21% | 0% / 4% | 0% / 8% |
| 16× | 0% / 11% | 0% / 1% | 0% / 1% |

"one" is the adversely selected half — the path went through one side and kept
going — and it is what the Kalshi book lost ninety-six per cent to. The levels
span a range from "run over four windows in five" to "never trades", which is
the range a choice has to cover.

The metric does not fight this. `half_width` reaches exactly two places: the
`covered` flag, and the feedback sentence that reports it. `skill`, `value`,
`mae` and the gate read the error against the benchmark and never the width, so
a wider quote changes `coverage` and the PnL objective and changes the promotion
number not at all. The one cost is that "realised price inside the quote" gets
less informative as the width grows; PnL is the channel that charges for the
width now, and it is the honest one.

**The box was three tools wide.** Every seed listed `market_quote`,
`candlesticks`, `previous_trades` out of the twenty each box offers, and a
`tools` rewrite almost never survives its minibatch, so the derived tools built
for exactly this job were never reached. Every seed now runs `state_summary`,
`move_base_rate` and `tape_imbalance` as fixed steps before the prompt — they
are cached, so they cost tool time and not model tokens — plus the topic's clock
(`settlement_countdown` on Kalshi, `session_clock` on crypto and news), and the
prompt text reads their output. `move_base_rate` is the one that matters most:
its p50 and p90 are the numbers the width question asks to be read. `ask_opus`
stays out of the seeds because it costs real money per window, and each seed's
description now says it is there.

Not `game_clock` or `goal_absorption`, though `KalshiHorizon.tools()` advertises
them: `replay_tools` omits them when a window has no timeline, and
`Harness.check` fails **every** instance of a generation when the first
instance's box lacks a listed tool. A seed that names them is one ESPN outage
away from scoring a whole generation as silence. `state_summary` folds the clock
and the goal in when a timeline exists and degrades quietly when it does not, so
the substance is kept without the failure mode. Making them safe to name is a
four-line change in `replay_tools` — both functions already return
`failed("no timeline for this window")` — but it contradicts a tested design
decision (`tests/test_replay.py`) and is its own edit.

## Status, 2026-09-23: the books trade, and the first thing they say is "quote wider"

The book became a market maker: the forecast is the quote. Every cycle the
harness posts its predicted mid plus and minus the half-width it already
states, with a size; the realised five-minute path decides the rest. A bar
whose low crosses the bid fills the buy, a bar whose high crosses the ask
fills the sell, both in one window is a round trip worth the spread, and
neither is a quote nobody wanted. Fills pay the maker fee. Taking is still
available and still optional. Positions carry until the harness closes them
or the deadline: a settled Kalshi market pays zero or one, everything else
closes at the mid.

That needed the realised path, which the question sets did not carry.
`scripts/path_windows.py` filled it: 16,234 of 16,469 Kalshi windows (the
rest are five-minute spans nobody traded in, or five markets that now 404),
every crypto window, every news window. Replaying the three first
generations with it:

| book | quote sides filled | trades | return | fees |
| --- | --- | --- | --- | --- |
| kalshi held-out | 2,598 of 4,800 | 1,561 | **-96.2%** | $186,523 |
| crypto held-out | 737 of 1,440 | 660 | -0.5% | $7,418 |
| news held-out | 878 of 1,422 | 532 | +0.1% | $316 |

The Kalshi number is not a bug; it is the answer to a question nobody had
asked yet. The seeds state a half-width below the venue tick, so the engine
widens it to the tick and the harness ends up quoting a **one-cent** market
on a contract whose five-minute path routinely travels four cents (p50 4c,
p90 18c; 71% of windows move at least twice the quoted half-width). Both
sides get taken, every fill is adversely selected, and the maker fee alone
is eighteen per cent of the book over 1,561 trades. Crypto is the same shape
at a fifth of the intensity; news, whose five-basis-point half-width sits
against an eighteen-basis-point path, is the one topic near break-even.

So the book's first finding is about the harnesses, not the venues: they
quote far too tight. Half-width has until now been a number nobody was
scored on - the metric only floors the error at a tick - and the paper book
is the first thing that charges for it. That is exactly the lever the
rewriter can pull, and PnL is on the search's frontier as of this morning.

## Status, 2026-09-23: a paper book beside the metric

Every harness now keeps a simulated book (`rsi_arena/trading/`): a million
dollars, a decision each cycle - open long, open short, close, hold, and a
size up to a tenth of equity, half of equity gross - fills that cross the
recorded spread and pay the venue's fees (Kalshi taker fees on YES or NO,
a perpetual at five basis points a side for crypto, vwap plus a three-basis-
point half-spread and SEC/TAF for equities), marks at every cycle, force-
closes at the topic's horizon (match end, sixty minutes, the session close).
The same engine runs over a generation's rollouts in instance-time order
(one book per harness per split, published as `<run>:<side>:<split>`) and
over the live forecasts (one book per topic, `live:<topic>`, carried across
incumbent changes with a handover mark). The reader's Trading page shows
the equity curve with drawdown, a leaderboard of every book, Sharpe (per
cycle, with a daily figure beside it), max drawdown, hit rate, turnover,
fees, open positions, recent trades, winners and losers.

PnL is a parallel metric. The gate still promotes on skill, on purpose: a
PnL objective would send the optimizer after the fill and fee model's
edges rather than after the price, and its noise would cost the gate the
resolution the cheap window just bought.

PnL is now on the search's frontier, though. `TaskAdapter.evaluate`
replays every batch it scores - a reflection minibatch of eight or the
valset - through a book of its own in instance-time order (the honest unit:
a position carried across batches would credit one candidate's fill to
another's exit), attaches each cycle's record and its `Book:` line to the
outcome the rewriter reads, and reports `objectives["pnl"]` beside `skill`
and `cost`: 0.5 for a cycle that holds, a percent of the book made or lost
the whole [0, 1] range (`PNL_SCALE_USD`, symmetric with how `value` scales
skill). GEPA runs with `frontier_type="hybrid"` (gepa 0.1.4: instance,
objective, hybrid, cartesian; the default was instance): the per-instance
frontier that selection was measured on, plus one key per objective held
by the candidate with the best valset mean of it. The candidate that makes
the most paper money keeps a seat on the frontier and one extra draw as a
parent; with hundreds of instance keys against three objective keys, the
instance frontier stays primary, which is why not `cartesian`. The frontier
only chooses which candidate is rewritten next; GEPA's best candidate is
still the best mean skill, and `loop/gate.py` is untouched - promotion is
by skill.

A harness may say what to do (`action`, `size`; the seeds now ask Jev a
choice question and a score question for them); a harness that does not
trades a default rule - open in the forecast's direction when the move
clears the round trip plus the quoted half-width, quarter-Kelly, capped.
Backfilled over the three first generations that rule made **no trade at
all**: the forecasts' expected moves (1.2c, 4.7 bps, 20 bps at most) never
clear the round trip (about 6c, 12 bps, 6 bps) once the half-width is
added, which is the honest reading of harnesses whose skill is still below
zero. Books with trades will come from harnesses that choose to trade, and
the leaderboard will say what that costs them.

Limitations stated: Kalshi and equity fills are at the touch with no depth;
Kalshi replay quotes are a two-cent proxy until the question set is rebuilt
with the new bid/ask fields; per-cycle Sharpe is inflated by idle cycles;
funding and borrow are ignored.

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

### How the question sets roll (2026-09-23)

A question set built once and committed is an exam that ages. The Kalshi set
ends on the newest event Kalshi had settled the day it was discovered, the
crypto set on a Tuesday in September, the news set on the fifteenth. Every
generation after that is asked about the same weeks, and the weeks it never
sees are the ones the incumbent will actually be traded on. `roll.yml` runs
`scripts/roll_question_set.py` every Sunday at 02:00 UTC, one step per topic,
to append what the venues settled since and prune the oldest back to size.

**Where new markets come from, and what "newest" means.**

- *kalshi-horizon-5m.* Newest is the latest date encoded in an event ticker
  already in `benchmarks/soccer-2026.json` — `KXEPLGAME-26SEP14LEENEW` is
  dated the 14th. Kalshi dates an event by the day it listed the contract,
  which is the kickoff day give or take `discover_fixtures.DATE_SPREAD`
  (three days), so the search runs from three days before newest through
  `--until` (yesterday). For each league already in the set, the settled
  events of its match-winner series are read newest first and stop after
  twenty-five in a row older than the window; each candidate has to clear the
  same bar `discover_fixtures.resolve` sets — two finalised markets, an ESPN
  link with enough confidence, ten windows out of the timeline. New fixtures
  are built, quoted by `quote_windows`, and given their minute prints by
  `path_windows.fill_paths` when that script is in the checkout.
- *crypto-horizon-1m.* Newest is the benchmark's `to`. The new days are `to +
  1` through `--until`, which is yesterday, so every day added is complete on
  the exchange. Filling a day is a store-level check — a day already in the
  kline store is counted, not refetched — which is what makes a roll that
  died halfway resumable by the next one.
- *news-equity-5m.* Newest is the latest New York session date in the set.
  `discover_news.discover` runs over the universe for each new weekday, with
  the same liquidity screen, session window, headline-repeat and per-name
  caps that built the set.

**Dedupe.** Kalshi by event ticker *and* by ESPN game id: one match is
sometimes listed under two tickers, and two rows for one match would put the
same game on both sides of the train/held-out split. Crypto by day, which is
the group. News by `(symbol, news_id)`, not by `news_id` alone — one story on
two names is two rows by design.

**Prune, and in what order.** Kalshi orders fixtures by ticker date and keeps
the newest `--keep` (485, the size the set had when the roll began); crypto
moves `from` to `to - keep + 1` (92 days); news drops whole session dates from
the oldest while the remainder still holds `--keep` symbol-days (2,350).
In every topic the benchmark is rewritten first, *then* the window files it no
longer names are deleted, so a roll killed between the two leaves orphan files
rather than fixtures without questions. Venue stores — klines, perps, on-chain,
bars — are never pruned: they are cheap and the live collectors read them.

**What a roll must never do.**

- Never change an existing window's `id`, `mid_now`, `realised`, quotes or
  path. Every file that survives is fingerprinted before the roll and checked
  after; a difference aborts with `RollError`. Those fields are the
  scoreboard's key, and a remembered answer has to stay attached to its
  question.
- Never touch `runs/`, the archive or the scoreboard. `roll.yml` stages
  `benchmarks/` paths by name and nothing else: a roll that committed a run
  directory would be writing the loop's record from outside the loop.
- Never shrink a set below what its spec needs — audit + held-out + the probe.
  `--validate` runs `rsi-arena windows --topic <t> --json` and
  `scripts/preflight.py` after each topic and, when either fails, restores that
  topic's paths from the checkout and reports `roll reverted: <reason>` instead
  of failing the job.
- Never roll under a running generation. The job polls `gh run list --workflow
  loop.yml` every two minutes for up to thirty and then skips the week green:
  a set changing mid-run would leave a generation half-scored on one exam and
  half on another.

A roll does reshuffle held-out membership — `three_way_split` shuffles sorted
group ids on a fixed seed, so adding or removing a group moves others between
train, held-out and audit. That is fine, and deliberate: the scoreboard keys on
instance ids, so every remembered answer survives the reshuffle, and the audit
set is meant to be a fresh cut rather than a monument.

The whole thing is built to be quiet. Per-topic `continue-on-error`, so one
venue being down does not stop the other two; a 25-minute discovery budget a
topic, so a slow venue costs a partial append and not the job; three-attempt
exponential backoff on every upstream call; atomic writes everywhere, including
`topics/_common/store.py` under `build_windows`, because a half-written window
file is a group that will never be rebuilt. Nothing new is success. The job is
red only when every topic attempted failed, or when the push failed, and an
issue is opened only in the first case.

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
