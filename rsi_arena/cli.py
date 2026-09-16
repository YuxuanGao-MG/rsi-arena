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
from dataclasses import fields
from pathlib import Path
from typing import Any

from .harness import Harness, OpenRouter, SyncLLM
from .loop import (Generation, Rollout, Settings, TaskAdapter, accept, evaluate, lineage,
                   probe_sample, reflection_templates, render_lineage, split_by_group,
                   summarise)
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
    ap.add_argument("--per-fixture", type=int, default=d.per_fixture,
                    help="cap windows kept per match; 0 keeps all. Power comes from "
                         "matches, not windows within one")
    ap.add_argument("--model", default=d.model, help="override the harness model")
    ap.add_argument("--cache-dir", default=d.cache_dir)
    ap.add_argument("--no-llm-cache", action="store_true")
    ap.add_argument("--concurrency", type=int, default=d.concurrency)
    ap.add_argument("--cascade", type=int, default=d.cascade,
                    help="train matches to probe before paying for the full evaluation; 0 disables")
    ap.add_argument("--cascade-floor", type=float, default=d.cascade_floor,
                    help="a probe below this is rejected without confirming")
    ap.add_argument("--max-generation-usd", type=float, default=d.max_generation_usd,
                    help="a backstop on the whole generation; 0 removes it")


def _settings(args: argparse.Namespace) -> Settings:
    """Every parsed flag, on the settings object the run will actually use.

    Written as a sweep over the dataclass rather than a list of names because
    the list of names is what went wrong. It enumerated five of eleven, so
    ``--per-fixture 8`` parsed, printed in the workflow log, and was discarded:
    the scheduled generation kept all thirty-four windows of all four hundred
    and fifty matches and was killed by the job timeout five and a half hours
    later. ``--cascade`` and ``--cascade-floor`` went the same way. A flag added
    to the parser now reaches the run by construction, and ``test_cli.py`` holds
    the parser and the dataclass to the same set of names.
    """
    s = Settings()
    for f in fields(Settings):
        if hasattr(args, f.name):
            setattr(s, f.name, getattr(args, f.name))
    # The one flag whose name is not its setting: argparse has no --llm-cache to
    # turn back on, so the switch is phrased as the negative.
    if getattr(args, "no_llm_cache", False):
        s.llm_cache = False
    return s


def _llm(s: Settings) -> OpenRouter:
    return OpenRouter(cache_dir=f"{s.cache_dir}/llm", cache=s.llm_cache, concurrency=s.concurrency,
                      budget_usd=s.max_generation_usd or None)


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


def _dump_rollouts(path: Path, rollouts: list[Rollout], *, trace: bool = False) -> None:
    """Write scored instances. With ``trace``, every step the harness took too.

    Off by default because a trace is two orders larger than the row it hangs
    off, and a thousand-window run does not want a hundred megabytes of prompts
    on disk for a number nobody disputed. On when something is going to be read
    — which is most of the time a human is involved.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([r.to_dict(trace=trace) for r in rollouts],
                               indent=1, default=str))


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


# -- next -------------------------------------------------------------------

def cmd_next(args: argparse.Namespace) -> int:
    """Where the next generation continues from, and what to call it.

    A schedule cannot be told "continue from gen4" by a human twice a day, and a
    schedule that always starts from the seed is not a lineage — it is the same
    experiment repeated. This reads the run directory, finds the deepest
    generation, and names the one after it.

    Prints shell assignments so a workflow can eval them, or JSON.
    """
    root = Path(args.runs_dir)

    def loadable(d: Path):
        """A directory with a manifest this version can read, or None.

        A half-written or hand-made manifest should not stop a schedule from
        finding where to continue — it should be skipped and said out loud.
        """
        try:
            return Generation.load(d)
        except Exception as exc:
            print(f"skipping {d}: {type(exc).__name__}: {exc}", file=sys.stderr)
            return None

    found = [(d, g) for d in sorted(root.glob("*")) if Generation.is_run_dir(d)
             and (g := loadable(d)) is not None]
    gens = [d for d, _ in sorted(found, key=lambda pair: pair[1].created)]
    if gens:
        parent = gens[-1]
        # The promoted harness, which is the candidate when the gate accepted it
        # and the incumbent when it did not. A rejected generation is still a
        # generation; the lineage continues from what survived it.
        harness, run_dir = str(parent), str(root / _next_name(parent.name, root))
    else:
        harness, run_dir = args.seed, str(root / "gen1")

    payload = {"harness": harness, "run_dir": run_dir,
               "generations": len(gens), "parent": gens[-1].name if gens else None}
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        for key, value in payload.items():
            print(f"{key}={value if value is not None else ''}")
    return 0


def _next_name(parent: str, root: Path) -> str:
    """``gen4`` after ``gen3``.

    Only a trailing number that is the whole tail counts. ``gen1-floored`` ends
    in a letter, and reading the ``1`` out of the middle of it produced
    ``gen1-floore2`` — a name that says nothing about which generation it is.
    Anything that is not ``<prefix><number>`` gets counted instead.
    """
    import re

    match = re.fullmatch(r"(.*?)(\d+)", parent)
    if match:
        candidate = f"{match.group(1)}{int(match.group(2)) + 1}"
    else:
        existing = sum(1 for d in root.glob("gen*") if d.is_dir())
        candidate = f"gen{existing + 1}"
    while (root / candidate).exists():
        candidate += "b"
    return candidate


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
        _dump_rollouts(Path(args.out), rollouts, trace=args.trace)
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
    # The probe is chosen before anything is paid for, not after the search.
    #
    # It used to be picked after GEPA returned, which meant the incumbent was
    # first scored on the whole train split and then all but the probe's rollouts
    # were thrown away. On the scheduled benchmark that is thirty-six hundred
    # windows bought and a hundred and sixty kept — about ninety dollars a
    # generation for a number nobody reads. The gate only ever asks of train
    # "did this get worse", and it asks it of the probe; so score the probe, and
    # score it once.
    probe_groups = (probe_sample({i.group for i in train}, s.cascade, s.seed)
                    if s.cascade > 0 else {i.group for i in train})
    probe = [i for i in train if i.group in probe_groups]
    gen.split["probe_groups"] = sorted(probe_groups)
    gen.split["probe"] = len(probe)

    log(f"baseline: {incumbent.name} ({gen.incumbent_fingerprint})")
    log(f"  scoring {len(probe)} probe windows over {len(probe_groups)} matches "
        f"and {len(hold)} held-out windows")
    base_train, base_hold = _bench(task, incumbent, probe, llm, s), _bench(task, incumbent, hold, llm, s)
    gen.baseline = {"train": summarise(task, base_train), "holdout": summarise(task, base_hold)}
    _dump_rollouts(run_dir / "rollouts" / "baseline.train.json", base_train,
                   trace=args.trace)
    _dump_rollouts(run_dir / "rollouts" / "baseline.holdout.json", base_hold,
                   trace=args.trace)
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

    # Cascade: a cheap look before the expensive one.
    #
    # Scoring a candidate on train and held-out is two thirds of a generation's
    # bill, and most candidates are not close. A sample of the train matches
    # costs a fraction and rejects the hopeless outright — the survey that chose
    # GEPA named this as the one feature OpenEvolve had and the others lacked,
    # and the one our cost profile wanted.
    #
    # On train only. Looking at held-out cheaply and then deciding whether to
    # look properly is peeking, and the held-out split exists so that nothing
    # the optimizer touches can reach it.
    stopped_early = False
    cand_train: list = []
    cand_hold: list = []
    if s.cascade > 0:
        # The probe is scored first and kept either way. It is the cheap filter
        # *and* the train scoreboard: the gate asks of train only whether the
        # candidate got worse, and twenty matches answer that as well as a
        # hundred and forty at a fifteenth of the price. Held-out is the half
        # that needs power, and held-out is scored in full.
        cand_train = _bench(task, candidate, probe, llm, s)
        gap = (task.statistic([r.outcome for r in cand_train])
               - task.statistic([r.outcome for r in base_train]))
        log(f"  cascade: {len(probe)} windows over {len(probe_groups)} matches, "
            f"{gap:+.3f} against the incumbent")
        if gap < s.cascade_floor:
            log(f"  stopping here: {gap:+.3f} is below {s.cascade_floor:+.3f}, and "
                f"held-out would only confirm it")
            stopped_early = True
    else:
        cand_train = _bench(task, candidate, train, llm, s)

    if llm.over_budget and not stopped_early:
        log("  not scoring held-out: the generation's budget is already gone, and a "
            "half-paid held-out set is worse than none")
        stopped_early = True
    if not stopped_early:
        cand_hold = _bench(task, candidate, hold, llm, s)
    gen.candidate = {"train": summarise(task, cand_train), "holdout": summarise(task, cand_hold)}
    _dump_rollouts(run_dir / "rollouts" / "candidate.train.json", cand_train,
                   trace=args.trace)
    _dump_rollouts(run_dir / "rollouts" / "candidate.holdout.json", cand_hold,
                   trace=args.trace)
    decision = accept(task, candidate_train=cand_train, incumbent_train=base_train,
                      candidate_holdout=cand_hold, incumbent_holdout=base_hold,
                      max_cost_ratio=s.max_cost_ratio, seed=s.seed,
                      unchanged=gen.candidate_fingerprint == gen.incumbent_fingerprint,
                      stopped_early=stopped_early)
    gen.decision = decision.to_dict()
    gen.llm = {"calls": llm.calls, "cache_hits": llm.cache_hits, "spent_usd": round(llm.spent_usd, 4),
               "budget_usd": s.max_generation_usd or None, "exhausted": llm.over_budget}
    gen.save()
    if llm.over_budget:
        # Said out loud rather than inferred from a number, because a generation
        # that ran out of money and one that ran to completion produce the same
        # shaped manifest, and the difference is the whole meaning of the result.
        log(f"budget exhausted: ${llm.spent_usd:.2f} of ${s.max_generation_usd:.2f}. "
            f"Everything after the line scored as silence; read this generation "
            f"as incomplete, not as evidence.")
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
    b.add_argument("--trace", action="store_true",
                   help="keep every step the harness took, not just the score")
    b.set_defaults(fn=cmd_bench)

    d = Settings()
    o = sub.add_parser("optimize", help="one generation: baseline, GEPA search, gate")
    _settings_args(o)
    o.add_argument("--run-dir", default=d.run_dir)
    o.add_argument("--max-metric-calls", type=int, default=d.max_metric_calls)
    o.add_argument("--reflection-model", default=d.reflection_model)
    o.add_argument("--minibatch", type=int, default=d.minibatch)
    o.add_argument("--max-cost-ratio", type=float, default=d.max_cost_ratio)
    o.add_argument("--trace", action="store_true",
                   help="keep every step each harness took, not just the score")
    o.set_defaults(fn=cmd_optimize)

    n = sub.add_parser("next", help="where the next generation continues from")
    n.add_argument("--runs-dir", default="runs")
    n.add_argument("--seed", default=Settings().harness)
    n.add_argument("--json", action="store_true")
    n.set_defaults(fn=cmd_next)

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
