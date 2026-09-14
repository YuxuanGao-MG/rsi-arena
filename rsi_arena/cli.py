"""rsi-arena: bench a harness, run one generation of the loop, show a lineage.

    rsi-arena windows                                             # build the question set, no key needed
    rsi-arena bench --split holdout
    rsi-arena optimize --run-dir runs/gen1
    rsi-arena optimize --harness runs/gen1 --run-dir runs/gen2     # continue from a run
    rsi-arena show runs/gen2
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from .harness import Harness, OpenRouter, SyncLLM
from .loop import (Generation, Rollout, Settings, TaskAdapter, accept, evaluate, lineage,
                   reflection_templates, render_lineage, split_by_group, summarise)
from .loop.generation import BEST, fingerprint, resolve_harness
from .topics import TOPICS, load_topic


def log(message: str) -> None:
    print(message, file=sys.stderr)


def _settings_args(ap: argparse.ArgumentParser) -> None:
    d = Settings()
    ap.add_argument("--topic", default=d.topic, choices=sorted(TOPICS))
    ap.add_argument("--harness", default=d.harness, help="a harness file, or a run directory to continue from")
    ap.add_argument("--benchmark", default=d.benchmark)
    ap.add_argument("--windows-dir", default=d.windows_dir)
    ap.add_argument("--holdout", type=int, default=d.holdout, help="fixtures the optimizer never sees")
    ap.add_argument("--seed", type=int, default=d.seed)
    ap.add_argument("--every", type=int, default=d.every, help="minutes between windows")
    ap.add_argument("--model", default=d.model, help="override the harness model")
    ap.add_argument("--cache-dir", default=d.cache_dir)
    ap.add_argument("--no-llm-cache", action="store_true")
    ap.add_argument("--concurrency", type=int, default=d.concurrency)


def _settings(args: argparse.Namespace) -> Settings:
    s = Settings(topic=args.topic, harness=args.harness, benchmark=args.benchmark,
                 windows_dir=args.windows_dir, holdout=args.holdout,
                 seed=args.seed, every=args.every, model=args.model, cache_dir=args.cache_dir,
                 llm_cache=not args.no_llm_cache, concurrency=args.concurrency)
    for name in ("reflection_model", "max_metric_calls", "minibatch", "max_cost_ratio", "run_dir"):
        if hasattr(args, name):
            setattr(s, name, getattr(args, name))
    return s


def _llm(s: Settings) -> OpenRouter:
    return OpenRouter(cache_dir=f"{s.cache_dir}/llm", cache=s.llm_cache, concurrency=s.concurrency)


def _bench(task, harness, instances, llm, s: Settings) -> list[Rollout]:
    return asyncio.run(evaluate(task, harness, instances, llm, concurrency=s.concurrency))


def _closing(llm: OpenRouter):
    """Close the client inside the loop that opened it.

    ``asyncio.run`` creates a loop, runs, and closes it. A second
    ``asyncio.run(llm.close())`` therefore tries to shut down an httpx pool whose
    sockets belong to a loop that no longer exists, and every real run ended in
    an ``Event loop is closed`` traceback after printing its results — exit code
    1 on a run that worked, which on CI is indistinguishable from one that did
    not.
    """

    async def _run(coro):
        try:
            return await coro
        finally:
            await llm.close()

    return _run


def _dump_rollouts(path: Path, rollouts: list[Rollout]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([r.to_dict() for r in rollouts], indent=1, default=str))


# -- windows ----------------------------------------------------------------

def cmd_windows(args: argparse.Namespace) -> int:
    """Build the question set from the exchange and the fixture feed. Needs no model key."""
    s = _settings(args)
    task = load_topic(s)
    instances = task.instances()
    train, hold = split_by_group(instances, s.holdout, s.seed)
    groups: dict[str, list[Any]] = {}
    for i in instances:
        groups.setdefault(i.group, []).append(i)
    rows = []
    for group, items in sorted(groups.items()):
        moved = sum(1 for w in items if abs(w.realised - w.mid_now) >= 0.01)
        rows.append({"group": group, "windows": len(items), "moved": moved,
                     "split": "holdout" if items[0] in hold else "train"})
    report = {"topic": task.name, "windows_dir": s.windows_dir, "instances": len(instances),
              "train": len(train), "holdout": len(hold), "groups": rows}
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"{task.name}: {len(instances)} windows ({len(train)} train, {len(hold)} held out) "
              f"in {s.windows_dir}")
        for r in rows:
            print(f"  {r['group']:34} {r['windows']:>4} windows  {r['moved']:>4} moved >=1c  {r['split']}")
    return 0 if instances else 1


# -- bench ------------------------------------------------------------------

def cmd_bench(args: argparse.Namespace) -> int:
    s = _settings(args)
    task = load_topic(s)
    harness, source, _ = resolve_harness(s.harness)
    if s.model:
        harness.config.model = s.model
    train, hold = split_by_group(task.instances(), s.holdout, s.seed)
    chosen = {"train": train, "holdout": hold, "all": train + hold}[args.split]
    if not chosen:
        print(json.dumps({"error": "no instances"}))
        return 1
    llm = _llm(s)
    rollouts = asyncio.run(_closing(llm)(
        evaluate(task, harness, chosen, llm, concurrency=s.concurrency)))
    summary = {"harness": harness.name, "source": source, "fingerprint": fingerprint(harness),
               "split": args.split, **summarise(task, rollouts)}
    if args.out:
        _dump_rollouts(Path(args.out), rollouts)
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(f"{harness.name} ({fingerprint(harness)}) on {args.split}")
        for k, v in summary.items():
            if k not in ("harness", "source", "fingerprint", "split"):
                print(f"  {k:18} {v}")
    return 0


# -- optimize ---------------------------------------------------------------

def cmd_optimize(args: argparse.Namespace) -> int:
    import gepa

    s = _settings(args)
    task = load_topic(s)
    incumbent, source, parent = resolve_harness(s.harness)
    if s.model:
        incumbent.config.model = s.model
    run_dir = Path(s.run_dir)
    gen = Generation(run_dir=str(run_dir), topic=task.name, parent=parent, incumbent=source,
                     incumbent_fingerprint=fingerprint(incumbent), settings=s.to_dict())

    train, hold = split_by_group(task.instances(), s.holdout, s.seed)
    gen.split = {"train_groups": sorted({i.group for i in train}), "holdout_groups": sorted({i.group for i in hold}),
                 "train": len(train), "holdout": len(hold)}
    log(f"train {len(train)} instances in {len(gen.split['train_groups'])} groups; "
        f"held out {len(hold)} in {len(gen.split['holdout_groups'])}")
    if not train:
        log("nothing to optimize on")
        return 1

    llm = _llm(s)
    log(f"baseline: {incumbent.name} ({gen.incumbent_fingerprint})")
    base_train, base_hold = _bench(task, incumbent, train, llm, s), _bench(task, incumbent, hold, llm, s)
    gen.baseline = {"train": summarise(task, base_train), "holdout": summarise(task, base_hold)}
    _dump_rollouts(run_dir / "rollouts" / "baseline.train.json", base_train)
    _dump_rollouts(run_dir / "rollouts" / "baseline.holdout.json", base_hold)
    gen.save()
    log(f"  train {gen.baseline['train']['statistic']:+.3f}   held-out {gen.baseline['holdout']['statistic']:+.3f}")

    adapter = TaskAdapter(task, incumbent, llm, concurrency=s.concurrency)
    result = gepa.optimize(
        seed_candidate=incumbent.to_components(), trainset=train, valset=train, adapter=adapter,
        reflection_lm=SyncLLM(llm, s.reflection_model),
        reflection_prompt_template=reflection_templates(task, incumbent),
        reflection_minibatch_size=s.minibatch, max_metric_calls=s.max_metric_calls,
        run_dir=str(run_dir / "gepa"), seed=s.seed, display_progress_bar=True, raise_on_exception=False)
    candidate = incumbent.from_components(result.best_candidate)
    candidate.name = f"{incumbent.name.split('+')[0]}+{run_dir.name}"
    candidate.save(run_dir / BEST)
    gen.candidate_fingerprint = fingerprint(candidate)
    gen.search = {"candidates": len(result.candidates), "metric_calls": result.total_metric_calls,
                  "best_idx": result.best_idx,
                  "best_train_value": round(result.val_aggregate_scores[result.best_idx], 4)}
    log(f"search: {gen.search['candidates']} candidates, best mean value {gen.search['best_train_value']:.3f}")

    cand_train, cand_hold = _bench(task, candidate, train, llm, s), _bench(task, candidate, hold, llm, s)
    gen.candidate = {"train": summarise(task, cand_train), "holdout": summarise(task, cand_hold)}
    _dump_rollouts(run_dir / "rollouts" / "candidate.train.json", cand_train)
    _dump_rollouts(run_dir / "rollouts" / "candidate.holdout.json", cand_hold)
    decision = accept(task, candidate_train=cand_train, incumbent_train=base_train,
                      candidate_holdout=cand_hold, incumbent_holdout=base_hold,
                      max_cost_ratio=s.max_cost_ratio, seed=s.seed,
                      unchanged=gen.candidate_fingerprint == gen.incumbent_fingerprint)
    gen.decision = decision.to_dict()
    gen.llm = {"calls": llm.calls, "cache_hits": llm.cache_hits, "spent_usd": round(llm.spent_usd, 4)}
    gen.save()
    asyncio.run(llm.close())

    print(render_lineage(lineage(run_dir)))
    print()
    print(("ACCEPTED " if decision.accepted else "REJECTED ") + "; ".join(decision.reasons))
    print(f"next: rsi-arena optimize --harness {run_dir} --run-dir <new run dir>")
    return 0


# -- show -------------------------------------------------------------------

def cmd_show(args: argparse.Namespace) -> int:
    chain = lineage(args.run_dir)
    if not chain:
        print(f"{args.run_dir} is not a run directory")
        return 1
    print(render_lineage(chain))
    if args.json:
        print(json.dumps([g.__dict__ for g in chain], indent=2, default=str))
    return 0


# -- entry ------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="rsi-arena", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)

    w = sub.add_parser("windows", help="build the question set; needs no model key")
    _settings_args(w)
    w.add_argument("--json", action="store_true")
    w.set_defaults(fn=cmd_windows)

    b = sub.add_parser("bench", help="score one harness on the benchmark")
    _settings_args(b)
    b.add_argument("--split", choices=["train", "holdout", "all"], default="all")
    b.add_argument("--json", action="store_true")
    b.add_argument("--out", default=None, help="write every rollout to this JSON file")
    b.set_defaults(fn=cmd_bench)

    d = Settings()
    o = sub.add_parser("optimize", help="one generation: baseline, GEPA search, gate")
    _settings_args(o)
    o.add_argument("--run-dir", default=d.run_dir)
    o.add_argument("--max-metric-calls", type=int, default=d.max_metric_calls)
    o.add_argument("--reflection-model", default=d.reflection_model)
    o.add_argument("--minibatch", type=int, default=d.minibatch)
    o.add_argument("--max-cost-ratio", type=float, default=d.max_cost_ratio)
    o.set_defaults(fn=cmd_optimize)

    sh = sub.add_parser("show", help="the lineage that leads to a run directory")
    sh.add_argument("run_dir")
    sh.add_argument("--json", action="store_true")
    sh.set_defaults(fn=cmd_show)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
