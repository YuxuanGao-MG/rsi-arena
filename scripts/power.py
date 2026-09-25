"""How big a held-out set has to be before the gate can see anything.

The gate accepts when the lower end of a 95% cluster-bootstrap interval clears
zero. Nobody had ever asked what effect that test can actually detect at the
size we run it. It matters more than it sounds: if the answer is larger than the
held-out set, then the loop cannot accept a real improvement no matter how many
generations it runs, and every rejection so far has been uninformative rather
than evidence that the rewrites are no good.

Estimated from the real paired rollouts rather than an assumed variance,
because the pairing is the whole reason the test has any power at all — the two
harnesses see the same matches, so the match-level noise that dominates the
marginal variance cancels, and what is left is the disagreement between them.
Published estimates put that correlation near 0.9, worth about a tenfold
variance reduction; this measures ours rather than borrowing theirs.

    python scripts/power.py
    python scripts/power.py --effect 0.02 --runs runs/gen1-floored
    python scripts/power.py --topic crypto-horizon-1m --was 30 \
        --runs runs/crypto-horizon-1m/gen7

``--topic`` reads the held-out size out of that topic's ``TopicSpec`` and the
group count out of its question set, so every line printed is about the split
the topic actually runs rather than the Kalshi numbers this was written for.
``--was`` prints a second row for the size the topic used to hold out, which is
how a deeper question set is shown to have bought something.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

#: One-sided 2.5% and 80% power: the usual 2.8 standard errors.
Z = 1.96 + 0.84


def load(path: Path) -> dict[str, list[dict]]:
    """Rollouts grouped by match. The match is the unit the bootstrap resamples."""
    if not path.exists():
        return {}
    by_group: dict[str, list[dict]] = {}
    for row in json.loads(path.read_text()):
        inst = row.get("instance") or {}
        key = inst.get("event") or inst.get("group") or ""
        details = (row.get("outcome") or {}).get("details") or {}
        if key and "error" in details and "naive_error" in details:
            by_group.setdefault(key, []).append(details)
    return by_group


def pooled(groups: dict[str, list[dict]], keys) -> float:
    removed = benchmark = 0.0
    for k in keys:
        for d in groups.get(k, []):
            bench = max(d["naive_error"], 0.01)
            removed += d["naive_error"] - d["error"]
            benchmark += bench
    return removed / benchmark if benchmark else 0.0


def paired_se(base: dict, cand: dict, *, draws: int = 4000, seed: int = 0) -> tuple[float, int]:
    """Standard error of the pooled difference, resampling matches with replacement."""
    keys = sorted(set(base) & set(cand))
    if len(keys) < 2:
        return float("nan"), len(keys)
    rng = random.Random(seed)
    diffs = []
    for _ in range(draws):
        pick = [keys[rng.randrange(len(keys))] for _ in keys]
        diffs.append(pooled(cand, pick) - pooled(base, pick))
    return statistics.pstdev(diffs), len(keys)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runs", default="runs", help="a run directory, or the root to scan")
    ap.add_argument("--effect", type=float, default=0.02,
                    help="the pooled-skill gap worth detecting")
    ap.add_argument("--topic", default=None,
                    help="take the held-out size and the set's group count from this topic's "
                         "spec and question set, instead of the Kalshi numbers")
    ap.add_argument("--was", type=int, default=None,
                    help="a held-out size to report beside the current one, so the gain from a "
                         "deeper question set is a number rather than a claim")
    args = ap.parse_args()

    # The two numbers every verdict below is about: how many groups the set
    # holds, and how many of them the gate is handed. They were Kalshi's
    # literals for the run of this script's life, which meant that pointing it
    # at another topic produced a correct measurement under a false caption.
    total, gate_at, noun = 485, 35, "matches"
    if args.topic:
        from rsi_arena.cli import _settings, build_parser                   # noqa: PLC0415
        from rsi_arena.topics import load_topic, spec_of                   # noqa: PLC0415
        gate_at = spec_of(args.topic).holdout
        s = _settings(build_parser().parse_args(["optimize", "--topic", args.topic]))
        total = len({i.group for i in load_topic(s).instances()})
        noun = "UTC days" if args.topic == "crypto-horizon-1m" else "groups"

    root = Path(args.runs)
    run_dirs = [root] if (root / "manifest.json").exists() else sorted(
        d for d in root.glob("*") if (d / "manifest.json").exists())

    rows = []
    for run_dir in run_dirs:
        base = load(run_dir / "rollouts" / "baseline.holdout.json")
        cand = load(run_dir / "rollouts" / "candidate.holdout.json")
        if not base or not cand:
            continue
        se, n = paired_se(base, cand)
        if math.isnan(se) or se <= 0:
            continue
        rows.append((run_dir.name, n, se))
        print(f"{run_dir.name}: {n} matches, paired SE {se:.4f}")

    if not rows:
        print("no run has both a baseline and a candidate held-out set to pair")
        return 1

    # SE scales as 1/sqrt(n) in the number of matches, so one measurement gives
    # the whole curve — but only if the measurement is worth anything. Every run
    # so far used a two-match held-out set, which is below the bootstrap's own
    # floor of eight, and the two available estimates differ by a factor of four.
    # Take the pessimistic one and say so; an underpowered gate that believes it
    # is powered is the failure this script exists to prevent.
    name, n, se = max(rows, key=lambda r: r[2])
    per_match = se * math.sqrt(n)
    thin = n < 8
    print(f"\nmeasured on {name}: SE = {per_match:.4f} / sqrt(matches)")
    if len(rows) > 1:
        spread = max(r[2] for r in rows) / min(r[2] for r in rows)
        print(f"the {len(rows)} available estimates differ by {spread:.1f}x; "
              f"this is the pessimistic one")
    if thin:
        print(f"WARNING: extrapolated from {n} matches, which is below the bootstrap's "
              f"floor of 8. Treat the table as an order of magnitude, not a number. "
              f"The first honest measurement arrives with the next generation.")
    print(f"minimum detectable pooled-skill gap, one-sided 2.5% at 80% power:\n")
    print(f"  {noun:>8}  {'SE':>8}  {'detectable':>11}")
    sizes = {8, 20, 60, 100, 200, 350, gate_at, total} | ({args.was} if args.was else set())
    for matches in sorted(n for n in sizes if 0 < n <= total):
        s = per_match / math.sqrt(matches)
        mark = "  <- the gate" if matches == gate_at else ("  <- was" if matches == args.was else "")
        print(f"  {matches:>8}  {s:>8.4f}  {Z * s:>11.4f}{mark}")

    need = math.ceil((Z * per_match / args.effect) ** 2)
    at_now = Z * per_match / math.sqrt(gate_at)
    print(f"\nTo see a gap of {args.effect:+.3f} you need about {need} held-out {noun}.")
    print(f"The benchmark holds {total} {noun}. The gate runs on {gate_at}, which can see "
          f"{at_now:+.3f} and nothing smaller.")
    if args.was:
        at_was = Z * per_match / math.sqrt(args.was)
        print(f"At {args.was} it could see {at_was:+.3f}, so the deeper set resolves a gap "
              f"{at_was / at_now:.1f}x smaller.")
    print("\nThe best rewrite anyone has found moved held-out skill by under 0.01, and")
    print("swapping the task model moved it by 0.12. If the first of those is the size")
    print(f"of effect the search produces, then a gate on {gate_at} {noun} cannot see the")
    print("search's output at all, and every rejection is uninformative rather than")
    print("evidence that the rewrites are no good.")
    if at_now > 0.01:
        print(f"\nThat is the case here: {at_now:+.3f} is larger than the effect. Widen the")
        print("held-out set rather than loosening the gate — a gate that cannot see is not")
        print("fixed by lowering the bar it cannot measure.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
