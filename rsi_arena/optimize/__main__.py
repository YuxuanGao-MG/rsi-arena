"""One generation of the loop: baseline, GEPA search on the train fixtures, gate on held-out.

    export OPENROUTER_API_KEY=sk-or-...
    python -m rsi_arena.optimize --holdout 2 --max-metric-calls 300 --run-dir runs/gen1
    python -m rsi_arena.optimize --harness runs/gen1/best.json --run-dir runs/gen2   # next generation

Writes to the run directory: ``baseline.json`` and ``candidate.json`` (scores on
both splits), ``best.json`` (the harness GEPA chose), ``decision.json`` (the
gate's verdict and why), and GEPA's own state.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import gepa

from ..bench.__main__ import add_common
from ..bench.evaluate import evaluate_windows, summarise
from ..bench.windows import build_windows, load_fixtures, split_fixtures
from ..harness import Harness, OpenRouter
from ..harness.llm import SyncLLM
from ..kalshi import History, ToolCache
from .adapter import HorizonAdapter
from .gate import accept


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_common(ap)
    ap.add_argument("--run-dir", default="runs/latest")
    ap.add_argument("--max-metric-calls", type=int, default=300,
                    help="window evaluations GEPA may spend, in total")
    ap.add_argument("--reflection-model", default="anthropic/claude-sonnet-4.5")
    ap.add_argument("--minibatch", type=int, default=8, help="windows per reflection step")
    ap.add_argument("--max-cost-ratio", type=float, default=2.0)
    args = ap.parse_args()

    run_dir = Path(args.run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    log = lambda m: print(m, file=sys.stderr)  # noqa: E731

    base = Harness.load(args.harness)
    if args.model:
        base.config.model = args.model
    fixtures = load_fixtures(args.benchmark)
    train_fx, hold_fx = split_fixtures(fixtures, args.holdout, args.seed)
    history = History()
    train = build_windows(train_fx, history=history, every_minutes=args.every,
                          cache_dir=args.cache_dir, log=log)
    hold = build_windows(hold_fx, history=history, every_minutes=args.every,
                         cache_dir=args.cache_dir, log=log)
    log(f"train: {len(train_fx)} fixtures, {len(train)} windows; "
        f"held out: {len(hold_fx)} fixtures, {len(hold)} windows")
    if not train:
        log("no train windows; nothing to optimize")
        return 1

    llm = OpenRouter(cache_dir=f"{args.cache_dir}/llm", cache=not args.no_llm_cache)
    tools = ToolCache(f"{args.cache_dir}/tools")

    def bench(harness: Harness, windows):
        return asyncio.run(evaluate_windows(harness, windows, llm, history=history,
                                            tool_cache=tools, concurrency=args.concurrency))

    log("baseline on train and held-out")
    base_train, base_hold = bench(base, train), bench(base, hold)
    baseline = {"train": summarise(base_train), "holdout": summarise(base_hold)}
    (run_dir / "baseline.json").write_text(json.dumps(baseline, indent=2))
    log(f"baseline train skill {baseline['train']['skill']:+.3f}, "
        f"held-out {baseline['holdout']['skill']:+.3f}")

    adapter = HorizonAdapter(base, llm, history=history, tool_cache=tools,
                             concurrency=args.concurrency)
    result = gepa.optimize(
        seed_candidate=base.to_components(), trainset=train, valset=train, adapter=adapter,
        reflection_lm=SyncLLM(llm, args.reflection_model),
        reflection_minibatch_size=args.minibatch, max_metric_calls=args.max_metric_calls,
        run_dir=str(run_dir / "gepa"), seed=args.seed, display_progress_bar=True,
        raise_on_exception=False)
    best = base.from_components(result.best_candidate)
    best.name = f"{base.name}+gen"
    best.save(run_dir / "best.json")
    log(f"GEPA proposed {len(result.candidates)} candidates over {result.total_metric_calls} "
        f"window evaluations; best candidate {result.best_idx} scored "
        f"{result.val_aggregate_scores[result.best_idx]:.4f} on train")

    cand_train, cand_hold = bench(best, train), bench(best, hold)
    candidate = {"train": summarise(cand_train), "holdout": summarise(cand_hold)}
    (run_dir / "candidate.json").write_text(json.dumps(candidate, indent=2))

    decision = accept(
        candidate_train=[r.score for r in cand_train if r.score], incumbent_train=[r.score for r in base_train if r.score],
        candidate_holdout=[r.score for r in cand_hold if r.score], incumbent_holdout=[r.score for r in base_hold if r.score],
        candidate_cost=candidate["holdout"]["cost_per_window"], incumbent_cost=baseline["holdout"]["cost_per_window"],
        max_cost_ratio=args.max_cost_ratio, seed=args.seed)
    (run_dir / "decision.json").write_text(json.dumps(decision.__dict__, indent=2))
    print(json.dumps({"accepted": decision.accepted, "reasons": decision.reasons,
                      "baseline": baseline, "candidate": candidate,
                      "llm_calls": llm.calls, "llm_cache_hits": llm.cache_hits,
                      "spent_usd": round(llm.spent_usd, 4)}, indent=2))
    asyncio.run(llm.close())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
