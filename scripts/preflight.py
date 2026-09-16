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

Needs no key and no network beyond the question set already on disk.
"""
import json, sys, glob, collections
from datetime import datetime, timezone
sys.path.insert(0, '.')
from pathlib import Path

ok, bad = [], []
def check(name, cond, detail=""):
    (ok if cond else bad).append(f"{name}: {detail}")
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))

from rsi_arena.harness.spec import Harness
from rsi_arena.loop.settings import Settings
from rsi_arena.topics.kalshi_horizon import score_output, pooled_skill
from rsi_arena.topics.kalshi_horizon.task import KalshiHorizon
from rsi_arena.loop.task import three_way_split
from rsi_arena.cli import _settings, build_parser
from rsi_arena.kalshi.replay import replay_tools

# The run's own parser, so a flag the run honours is a flag this sees.
s = _settings(build_parser().parse_args(["optimize", *sys.argv[1:]]))
h = Harness.load(s.harness)

print("\n— the harness —")
check("model is set", bool(h.config.model), h.config.model)
_box = set(replay_tools(datetime(2026, 1, 1, tzinfo=timezone.utc)))
check("every declared tool exists in the frozen box", set(h.tools) <= _box,
      f"{len(h.tools)} declared of {len(_box)} available: {sorted(set(h.tools) - _box) or 'all present'}")
check("the rewriter has room to compose", len(_box) >= 10, f"{len(_box)} tools in the box")
check("plan inputs are supplied", h.plan.required_inputs() <= {"game"}, str(h.plan.required_inputs()))
check("components round-trip", h.from_components(h.to_components()).to_components() == h.to_components())

print("\n— the metric —")
quiet = [score_output({"delta_cents": 0}, 0.5, 0.5) for _ in range(9)]
mover = [score_output({"delta_cents": 0}, 0.5, 0.57) for _ in range(20)]
check("silence pools to exactly zero", abs(pooled_skill(quiet + mover)) < 1e-12, f"{pooled_skill(quiet+mover):+.2e}")
check("silence is 0.5 to the optimizer", quiet[0].value == 0.5)
loud = [score_output({"delta_cents": 4}, 0.5, 0.5) for _ in range(9)] + mover
check("noise on a dead market costs", pooled_skill(loud) < 0, f"{pooled_skill(loud):+.4f}")

print("\n— the question set —")
task = KalshiHorizon.from_settings(s)
inst = task.instances()
groups = collections.Counter(i.group for i in inst)
train, hold, audit = three_way_split(inst, s.audit, s.holdout, s.seed,
                                     s.generation // max(1, s.holdout_rotate_every))
tg, hg, ag = ({i.group for i in train}, {i.group for i in hold}, {i.group for i in audit})
check("matches", len(groups) >= 100, f"{len(groups)} matches, {len(inst)} windows")
check("no match on both sides", not (tg & hg), f"{len(tg)} train / {len(hg)} held out")
check("the audit set is shown to nothing else", not (ag & (tg | hg)),
      f"{len(ag)} matches held back for confirmation")
check("held-out clears the bootstrap floor", len(hg) >= 8, f"{len(hg)} matches, floor is 8")
# The pairs cluster bootstrap over-rejects below roughly forty clusters, and at
# thirty-five the smallest gap it can resolve is larger than any rewrite has
# ever produced. A gate that cannot see its own search rejects everything and
# calls it evidence.
check("held-out is large enough for the test to mean something", len(hg) >= 40,
      f"{len(hg)} matches; below 40 the bootstrap over-rejects")
_cap = s.per_fixture or 10 ** 6
check("windows per match respect --per-fixture", max(groups.values()) <= _cap,
      f"max {max(groups.values())}, cap {s.per_fixture or 'none'}")
_cost = len(inst) * 0.025
check("a generation fits its budget", s.max_generation_usd > 0, f"ceiling ${s.max_generation_usd:.0f}")
check("the question set is not itself the runaway", _cost < 400,
      f"{len(inst)} windows is about ${_cost:.0f} per full pass")
moved = sum(1 for i in inst if abs(i.realised - i.mid_now) >= 0.01)
check("enough windows actually move", moved / len(inst) > 0.4, f"{moved}/{len(inst)} moved >= 1c")

print("\n— the gate —")
check("cascade is on", s.cascade > 0, f"{s.cascade} matches, floor {s.cascade_floor:+.3f}")
check("search budget buys several rewrites", s.max_metric_calls >= 400, str(s.max_metric_calls))
check("cost ceiling is set", s.max_cost_ratio > 1, f"{s.max_cost_ratio}x")

print(f"\n{len(ok)} pass, {len(bad)} fail")
for b in bad:
    print(f"  ! {b}")
sys.exit(1 if bad else 0)
