"""Run one harness over the fixed benchmark and print the pooled numbers.

    python -m rsi_arena.bench --harness harnesses/horizon-5m.json
    python -m rsi_arena.bench --harness runs/latest/best.json --holdout 2 --json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from ..harness import Harness, OpenRouter
from ..kalshi import History, ToolCache
from .evaluate import evaluate_windows, summarise
from .windows import build_windows, load_fixtures, split_fixtures


def add_common(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--benchmark", default="benchmarks/epl-2026-09.json")
    ap.add_argument("--harness", default="harnesses/horizon-5m.json")
    ap.add_argument("--holdout", type=int, default=2, help="fixtures kept out of the optimizer's sight")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--every", type=int, default=5, help="minutes between windows")
    ap.add_argument("--model", default=None, help="override the harness model")
    ap.add_argument("--cache-dir", default=".cache")
    ap.add_argument("--no-llm-cache", action="store_true")
    ap.add_argument("--concurrency", type=int, default=4)


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    add_common(ap)
    ap.add_argument("--split", choices=["train", "holdout", "all"], default="all")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    harness = Harness.load(args.harness)
    if args.model:
        harness.config.model = args.model
    fixtures = load_fixtures(args.benchmark)
    train, holdout = split_fixtures(fixtures, args.holdout, args.seed)
    chosen = {"train": train, "holdout": holdout, "all": fixtures}[args.split]
    history = History()
    windows = build_windows(chosen, history=history, every_minutes=args.every,
                            cache_dir=args.cache_dir, log=lambda m: print(m, file=sys.stderr))
    if not windows:
        print(json.dumps({"error": "no scoreable windows"}))
        return 1

    async with OpenRouter(cache_dir=f"{args.cache_dir}/llm", cache=not args.no_llm_cache) as llm:
        results = await evaluate_windows(harness, windows, llm, history=history,
                                         tool_cache=ToolCache(f"{args.cache_dir}/tools"),
                                         concurrency=args.concurrency)
    summary = {"harness": harness.name, "split": args.split, "fixtures": len(chosen),
               **summarise(results)}
    if args.json:
        print(json.dumps({**summary, "windows_detail": [
            {**r.window.to_dict(), "value": r.value, "cost_usd": r.cost_usd,
             "score": r.score.to_dict() if r.score else None, "error": r.error}
            for r in results]}, indent=2, default=str))
    else:
        print(f"{harness.name} on {args.split} ({len(chosen)} fixtures)")
        for k, v in summary.items():
            if k not in ("harness", "split", "fixtures"):
                print(f"  {k:16} {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
