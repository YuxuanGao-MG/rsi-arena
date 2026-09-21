"""Recover every candidate three generations of search already paid for.

Each run wrote its whole GEPA state to ``runs/*/gepa/gepa_state.bin`` — the
candidates, the candidates-by-instances score matrix, and the ancestry within
the search — and then read back exactly one field, ``best_candidate``, and
deleted the rest by never looking at it again. Three generations, twenty
candidates, about thirty-eight dollars of evaluation, all of it still on disk.

This reads it back. Nothing is re-evaluated and nothing is paid for twice.

The one thing the state does not hold is which instance each column refers to;
the subscore lists are positional against the valset as GEPA received it. New
runs write ``valset.json`` for exactly this reason. The three old runs predate
that, so their order is recovered from ``rollouts/baseline.train.json``, which
was produced by a ``gather`` over the same list and is therefore in the same
order.

    python scripts/backfill_archive.py            # writes runs/archive.json
    python scripts/backfill_archive.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rsi_arena.harness import Harness                                  # noqa: E402
from rsi_arena.loop import ARCHIVE, Archive, Entry, from_gepa_state    # noqa: E402
from rsi_arena.loop.generation import Generation, fingerprint_components  # noqa: E402


def valset_ids(run_dir: Path) -> list[str]:
    """The instance ids in the order GEPA was handed them."""
    explicit = run_dir / "valset.json"
    if explicit.exists():
        return json.loads(explicit.read_text())
    rollouts = run_dir / "rollouts" / "baseline.train.json"
    if not rollouts.exists():
        return []
    rows = json.loads(rollouts.read_text())
    out = []
    for r in rows:
        inst = r.get("instance") or {}
        ticker, at = inst.get("ticker"), inst.get("at")
        if ticker and at:
            out.append(f"{ticker}@{at}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runs-dir", default="runs")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    root = Path(args.runs_dir)
    archive = Archive.load(root / ARCHIVE)
    before = len(archive)

    for run_dir in sorted(root.glob("*")):
        if not Generation.is_run_dir(run_dir):
            continue
        try:
            gen = Generation.load(run_dir)
        except Exception as exc:
            print(f"  {run_dir.name}: unreadable manifest ({type(exc).__name__}), skipped")
            continue
        ids = valset_ids(run_dir)
        if not ids:
            print(f"  {run_dir.name}: no valset order recoverable, skipped")
            continue
        model = (gen.settings or {}).get("model")
        if not model:
            best = run_dir / "best.json"
            model = Harness.load(best).config.model if best.exists() else None
        found = from_gepa_state(
            run_dir, run_dir.name, ids,
            lambda c, m=model: fingerprint_components(c, c.get("model") or m),
            promoted_id=gen.candidate_fingerprint if (gen.decision or {}).get("accepted") else None)
        scored = sum(1 for e in found if e.scores)
        print(f"  {run_dir.name}: {len(found)} candidates, {scored} with per-instance scores, "
              f"{len(ids)} instances")
        for entry in found:
            archive.add(entry)

    summary = archive.summary()
    print(f"\n{before} -> {len(archive)} candidates; {summary['frontier']} on the frontier, "
          f"{summary['instances']} instances remembered across {summary['generations']} generations")
    widest = max(((len(v), k) for k, v in archive.wins().items()), default=(0, ""))
    if widest[0]:
        entry = archive.get(widest[1])
        print(f"broadest: {widest[1]} from {entry.generation}, best on {widest[0]} instances")
    if args.dry_run:
        print("(dry run; nothing written)")
        return 0
    archive.save(root / ARCHIVE)
    print(f"wrote {root / ARCHIVE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
