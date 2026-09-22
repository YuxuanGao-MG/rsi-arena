"""Everything that has to be true before a generation is worth paying for.

A generation is about thirty-five dollars and an hour, and it runs unattended
twice a day. Most of what can go wrong is quiet: a metric that rewards silence,
a held-out split too small to draw an interval from, a harness whose plan reads
a name nobody supplies. Each of those has happened here, and each was found
after a run rather than before one.

    python scripts/preflight.py --benchmark benchmarks/soccer-2026.json --holdout 35

Takes the same flags the run takes, and parses them with the run's own parser.
It used to build its own ``Settings`` instead, which is how it came to assert
that no match contributed more than sixteen windows while the generation beside
it kept all thirty-four of them: the flag that would have thinned the set was
being dropped in ``_settings``, and the one check that could have caught it was
asking a different object. A guard that does not read what it guards is
decoration.

Asks the topic, not Kalshi, for everything it checks: the box a harness may
draw on, the inputs a plan may read, the unit a move is measured in and what
counts as one. ``--topic`` therefore preflights any topic the CLI knows.

Needs no key and no network beyond the question set already on disk.
"""
import os, sys, collections
sys.path.insert(0, '.')

ok, bad = [], []
def check(name, cond, detail=""):
    (ok if cond else bad).append(f"{name}: {detail}")
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))

from rsi_arena.harness.spec import Harness
from rsi_arena.loop.task import three_way_split
from rsi_arena.cli import _metric_of, _moved_by, _settings, build_parser
from rsi_arena.topics import load_topic, spec_of
from rsi_arena.topics._common.metric import pooled_skill, score_output

# The run's own parser, so a flag the run honours is a flag this sees.
s = _settings(build_parser().parse_args(["optimize", *sys.argv[1:]]))
h = Harness.load(s.harness)
task = load_topic(s)
spec = spec_of(s.topic)
metric = _metric_of(task)
moved_by = _moved_by(task)

print(f"\n— {task.name}: moves in {metric.unit}, a tick is {metric.tick:g} —")

print("\n— the harness —")
check("model is set", bool(h.config.model), h.config.model)
_box = set(task.tools())
check("every declared tool exists in the frozen box", set(h.tools) <= _box,
      f"{len(h.tools)} declared of {len(_box)} available: {sorted(set(h.tools) - _box) or 'all present'}")
check("the rewriter has room to compose", len(_box) >= 10, f"{len(_box)} tools in the box")
check("plan inputs are supplied", h.plan.required_inputs() <= set(task.inputs),
      f"{sorted(h.plan.required_inputs())} of {sorted(task.inputs)}")
check("components round-trip", h.from_components(h.to_components()).to_components() == h.to_components())

print("\n— the metric —")
# Stated in the topic's own output keys and unit, so the three properties the
# whole loop rests on are asserted of the metric a run will actually score by.
_delta, _width = metric.output_keys
_move = metric.price_from(0.5, 7 * metric.tick)              # a seven-tick move
quiet = [score_output({_delta: 0}, 0.5, 0.5, metric) for _ in range(9)]
mover = [score_output({_delta: 0}, 0.5, _move, metric) for _ in range(20)]
check("silence pools to exactly zero", abs(pooled_skill(quiet + mover)) < 1e-12, f"{pooled_skill(quiet+mover):+.2e}")
check("silence is 0.5 to the optimizer", quiet[0].value == 0.5)
loud = [score_output({_delta: 4 * metric.tick}, 0.5, 0.5, metric) for _ in range(9)] + mover
check("noise on a dead market costs", pooled_skill(loud) < 0, f"{pooled_skill(loud):+.4f}")

print("\n— the question set —")
inst = task.instances()
groups = collections.Counter(i.group for i in inst)
train, hold, audit = three_way_split(inst, s.audit, s.holdout, s.seed,
                                     s.generation // max(1, s.holdout_rotate_every))
tg, hg, ag = ({i.group for i in train}, {i.group for i in hold}, {i.group for i in audit})
# A hundred was the Kalshi number: what the power analysis said a held-out set
# needs, with an audit slice and a probe beside it, out of 485 matches. A topic
# whose group is a day has ninety-odd of them in a quarter, and its spec asks
# for the split it can afford; the floor here is that the three splits fit. An
# empty question set is the most common way a new topic arrives, and it used
# to crash the arithmetic below rather than fail this line.
_need = s.audit + s.holdout + max(s.cascade, 8)
check("groups fill the split", len(groups) >= _need,
      f"{len(groups)} groups, {len(inst)} windows; audit {s.audit} + held-out {s.holdout} + probe "
      f"{max(s.cascade, 8)} = {_need}"
      + ("; the question set is empty: build it (a discover script, then rsi-arena windows) "
         "before a generation" if not inst else ""))
check("no match on both sides", not (tg & hg), f"{len(tg)} train / {len(hg)} held out")
check("the audit set is shown to nothing else", not (ag & (tg | hg)),
      f"{len(ag)} matches held back for confirmation")
check("held-out clears the bootstrap floor", len(hg) >= 8, f"{len(hg)} matches, floor is 8")
# The pairs cluster bootstrap over-rejects below roughly forty clusters, and at
# thirty-five the smallest gap it can resolve is larger than any rewrite has
# ever produced. A gate that cannot see its own search rejects everything and
# calls it evidence.
# Forty is a property of the bootstrap, not of Kalshi, so it stays; but a
# topic whose spec asks for fewer has accepted the over-rejection knowingly,
# and the check says so rather than failing a split the topic chose.
_forty = len(hg) >= 40
check("held-out is large enough for the test to mean something", _forty or len(hg) >= spec.holdout,
      f"{len(hg)} groups; below 40 the bootstrap over-rejects"
      + ("" if _forty else f" - the {s.topic} spec asks for {spec.holdout} and takes that on"))
_cap = s.per_fixture or 10 ** 6
_most = max(groups.values(), default=0)
check("windows per match respect --per-fixture", _most <= _cap,
      f"max {_most}, cap {s.per_fixture or 'none'}")
# Measured, not assumed: gen5 paid $12.08 for 357 windows that reached the model.
# The docs said $0.013 a window for a year, which was a different model. One
# number, on the settings object, so the run's reserve and this prediction
# cannot drift apart again.
PER_WINDOW = s.window_usd
_probe = min(s.cascade or len(tg), len(tg)) * (s.per_fixture or 8)
# Priced against what is already owned. The incumbent's half of a generation is
# the same harness on the same windows every time until something is promoted,
# and the topic's scoreboard remembers what that already cost.
from rsi_arena.loop import Scoreboard
from rsi_arena.loop.generation import fingerprint
_board = Scoreboard.load(Scoreboard.path_for(s.runs_dir, task.name)) if s.reuse_scores else Scoreboard()
_inc = fingerprint(h)
_owned = sum(1 for w in hold if _board.get(_inc, w) is not None) if len(_board) else 0
# Priced the way the run spends it, which is in three shares.
#
# The judgment - probe and held-out on the candidate - is reserved before the
# search at the most the gate lets a candidate cost, because a candidate that
# costs more is rejected on cost at the probe and never reaches held-out. The
# search gets max_metric_calls plus one valset pass at the incumbent's rate,
# and the run stops it on dollars, so this share is a cap on it rather than a
# guess about it. The incumbent's own half is whatever the scoreboard does not
# already own. gen11 priced all three at the incumbent's rate, in one pot,
# and the pot ran dry $6.50 into a $22 held-out set.
# The judgment is priced at the same floored rate the run reserves at: the gate
# lets a candidate cost the ratio times max(incumbent, floor), and the reserve
# is that times the windows judged. Pricing it at the raw rate said $0.33 for a
# generation whose reserve alone was $24, and the ceiling derived from the
# prediction could not pay for a verdict.
_floor = max(PER_WINDOW, s.cost_floor_usd or 0.0)
_judge = (_probe + len(hold)) * _floor * s.max_cost_ratio
_search = (s.max_metric_calls + s.valset) * PER_WINDOW
# The rewriter's own bill. On a topic whose windows cost a fraction of a cent it
# is most of the generation: one reflection per minibatch of proposals, at what
# a Sonnet rewrite over eight traces has cost.
# The search stops on dollars (loop/budget.py::SpendStopper), so what it may
# spend on rewrites is a cap, not a forecast: enough proposals to matter,
# priced at what a Sonnet rewrite over eight traces has cost.
REFLECTION_USD = 0.08
MAX_PRICED_PROPOSALS = 75
_proposals = min(MAX_PRICED_PROPOSALS, max(0, s.max_metric_calls - s.valset) // max(1, s.minibatch))
_reflect = _proposals * REFLECTION_USD
_cold = (_probe + len(hold)) * PER_WINDOW + _judge + _search + _reflect
_next = _cold - _owned * PER_WINDOW
check("a generation has a ceiling", s.max_generation_usd > 0, f"${s.max_generation_usd:.2f}")
check("the judgment fits under the ceiling", _judge < s.max_generation_usd,
      f"reserve ${_judge:.0f} (x{s.max_cost_ratio:.1f}) under ${s.max_generation_usd:.0f}; "
      f"the workflow raises the ceiling to what is predicted")
# Reported, not asserted. Whether the money is there is the caller's question and
# it can answer it better than this can — it knows the balance and what the day
# has already spent. What this knows is what the split costs, and it is the only
# thing that does.
print(f"  COST  about ${_next:.0f} to run: ${_judge:.0f} reserved to judge, ${_search:.0f} "
      f"for the search, ${_reflect:.0f} for {_proposals} rewrites, "
      f"${(_probe + len(hold) - _owned) * PER_WINDOW:.0f} for the baseline "
      f"({len(_board)} answers on file save ${_owned * PER_WINDOW:.0f} of a ${_cold:.0f} "
      f"cold generation)")
_emit = os.environ.get("GITHUB_OUTPUT")
if _emit:
    with open(_emit, "a") as fh:
        fh.write(f"predicted_usd={_next:.2f}\n")
        fh.write(f"cold_usd={_cold:.2f}\n")
check("the question set is not itself the runaway", len(inst) * PER_WINDOW < 600,
      f"{len(inst)} windows is about ${len(inst) * PER_WINDOW:.0f} per full pass")
moved = sum(1 for i in inst if moved_by(i))
check("enough windows actually move", bool(inst) and moved / len(inst) > 0.4,
      f"{moved}/{len(inst)} moved >= {metric.tick:g} {metric.unit}")

print("\n— the gate —")
check("cascade is on", s.cascade > 0, f"{s.cascade} matches, floor {s.cascade_floor:+.3f}")
check("search budget buys several rewrites", s.max_metric_calls >= 400, str(s.max_metric_calls))
# The assertion that was missing for four generations. GEPA scores the seed
# across the whole valset before it consults the stop condition, so a valset at
# or over the budget spends everything on the seed and proposes nothing - and
# the run still gates result.best_candidate and prints a verdict about it.
check("the search can actually iterate", 0 < s.valset < s.max_metric_calls * 0.7,
      f"valset {s.valset} against {s.max_metric_calls} calls leaves "
      f"{s.max_metric_calls - s.valset} for proposals")
check("cost ceiling is set", s.max_cost_ratio > 1, f"{s.max_cost_ratio}x")

print(f"\n{len(ok)} pass, {len(bad)} fail")
for b in bad:
    print(f"  ! {b}")
sys.exit(1 if bad else 0)
