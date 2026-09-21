"""Score a harness on the exact windows another run already scored, paired.

    python scripts/compare_on_rollouts.py runs/gen11/rollouts/baseline.holdout.json \
        --harness harnesses/horizon-5m-jev.json

The incumbent's answers are read back from the rollout dump - already paid
for, remembered cost and all - so the comparison costs only the challenger's
windows, and the gate's own paired bootstrap draws the interval by match.
Written for the first question anyone asks of a new model: on the windows we
have, does it beat what we run.
"""
import argparse, asyncio, json, sys
from datetime import datetime
from pathlib import Path
sys.path.insert(0, ".")
from rsi_arena.harness import Harness, OpenRouter
from rsi_arena.loop import Settings, evaluate, paired_bootstrap, summarise
from rsi_arena.loop.task import Outcome, Rollout
from rsi_arena.topics.kalshi_horizon import KalshiHorizon, Window

ap = argparse.ArgumentParser()
ap.add_argument("rollouts", nargs="?", default="")
ap.add_argument("--scoreboard", default="",
                help="instead of a dump: a harness fingerprint whose answers runs/scoreboard.json remembers; "
                     "every window of the question set it has an answer for is compared")
ap.add_argument("--harness", required=True)
ap.add_argument("--benchmark", default="benchmarks/soccer-2026.json")
ap.add_argument("--limit", type=int, default=0)
ap.add_argument("--concurrency", type=int, default=8)
ap.add_argument("--max-usd", type=float, default=5.0)
ap.add_argument("--out", default="")
a = ap.parse_args()

s = Settings(benchmark=a.benchmark)
task = KalshiHorizon.from_settings(s)
windows, incumbent = [], []
if a.scoreboard:
    from rsi_arena.loop import SCOREBOARD, Scoreboard
    board = Scoreboard.load(Path("runs") / SCOREBOARD)
    for w in task.instances():
        known = board.get(a.scoreboard, w)
        if known is None:
            continue
        windows.append(w)
        incumbent.append(Rollout(instance=w, run=None, outcome=known,
                                 remembered_cost=board.cost_of(a.scoreboard, w)))
    print(f"scoreboard remembers {len(windows)} answers of {a.scoreboard}")
else:
    dump = json.load(open(a.rollouts))
    for r in dump:
        i = r["instance"]
        w = Window(ticker=i["ticker"], at=datetime.fromisoformat(i["at"]), mid_now=i["mid_now"],
                   realised=i["realised"], game=i.get("game") or {}, event=i.get("event", ""))
        o = r["outcome"]
        windows.append(w)
        incumbent.append(Rollout(instance=w, run=None,
                                 outcome=Outcome(value=o["value"], feedback=o.get("feedback", ""),
                                                 objectives=o.get("objectives") or {}, details=o.get("details") or {}),
                                 remembered_cost=r.get("cost_usd")))
if a.limit:
    windows, incumbent = windows[: a.limit], incumbent[: a.limit]
task._windows = windows
harness = Harness.load(a.harness)
llm = OpenRouter(cache_dir=f"{s.cache_dir}/llm", budget_usd=a.max_usd, concurrency=a.concurrency)

async def main():
    try:
        return await evaluate(task, harness, windows, llm, concurrency=a.concurrency)
    finally:
        await llm.close()

challenger = asyncio.run(main())
if a.out:
    Path(a.out).write_text(json.dumps([r.to_dict() for r in challenger], indent=1))

def show(name, rollouts):
    d = summarise(task, rollouts)
    print(f"{name:12} skill {d['statistic']:+.4f}  on-moves {d.get('skill_on_moves', 0):+.4f}  "
          f"mae {d.get('mae', 0):.4f} (naive {d.get('naive_mae', 0):.4f})  echoed {d.get('echoed')}/{d['instances']}  "
          f"unscored {d.get('unscored')}  ${d['cost_usd']:.4f} (${d['cost_per_instance']:.5f}/window)")

print(f"{len(windows)} windows over {len({w.group for w in windows})} matches")
show("incumbent", incumbent)
show(harness.name, challenger)
b = paired_bootstrap(task, challenger, incumbent, seed=0)
print(f"paired: diff {b['diff']:+.4f}  95% [{b['low']:+.4f}, {b['high']:+.4f}]  detectable {b.get('detectable', 0):+.4f}  "
      f"{'UNDERPOWERED' if b.get('underpowered') else ''}")
failed = [r for r in challenger if r.run is not None and not r.run.ok]
if failed:
    print(f"{len(failed)} failed runs; first: {failed[0].run.error}")
print(f"llm: {llm.calls} calls, {llm.cache_hits} cached, ${llm.spent_usd:.4f}")
