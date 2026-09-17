"""Recover every outcome already committed to the repository.

`runs/*/rollouts/*.json` holds every window any harness has ever been scored on,
with the error and the benchmark error that produced its skill. The loop was
re-buying those answers at three and a half cents each: the incumbent alone is
four hundred and eighty windows a generation, and it has been the same harness
for five generations because nothing has ever been promoted.

Nothing here is re-evaluated and nothing is paid for twice.

    python scripts/backfill_scoreboard.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rsi_arena.loop import SCOREBOARD, Scoreboard                    # noqa: E402
from rsi_arena.loop.generation import Generation                     # noqa: E402
from rsi_arena.loop.scoreboard import key                            # noqa: E402


class Window:
    """Only the fields the key is built from."""

    def __init__(self, d: dict) -> None:
        self.id = f"{d.get('ticker')}@{d.get('at')}"
        self.mid_now = d.get("mid_now")
        self.realised = d.get("realised")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runs-dir", default="runs")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    root = Path(args.runs_dir)
    board = Scoreboard.load(root / SCOREBOARD)
    before = len(board)

    for run_dir in sorted(root.glob("*")):
        if not Generation.is_run_dir(run_dir):
            continue
        try:
            gen = Generation.load(run_dir)
        except Exception:
            continue
        sides = {"baseline": gen.incumbent_fingerprint, "candidate": gen.candidate_fingerprint}
        for side, fingerprint in sides.items():
            if not fingerprint:
                continue
            for split in ("train", "holdout", "audit"):
                path = run_dir / "rollouts" / f"{side}.{split}.json"
                if not path.exists():
                    continue
                rows = json.loads(path.read_text())
                kept = 0
                for r in rows:
                    inst = r.get("instance") or {}
                    out = r.get("outcome") or {}
                    det = out.get("details") or {}
                    # A window scored as silence because the money ran out is not
                    # an answer, and remembering it would make the shortage
                    # permanent.
                    if not det.get("scored"):
                        continue
                    w = Window(inst)
                    if w.mid_now is None or w.realised is None:
                        continue
                    before_put = board.added
                    board.entries.setdefault(key(fingerprint, w), {
                        "value": out.get("value", 0.0), "feedback": out.get("feedback", ""),
                        "objectives": out.get("objectives", {}), "details": det,
                        "cost_usd": r.get("cost_usd", 0.0)})
                    kept += 1
                if kept:
                    print(f"  {run_dir.name:16} {side:9} {split:8} {kept:>5} answers "
                          f"({fingerprint})")

    print(f"\n{before} -> {len(board)} answers remembered")
    print(f"at $0.034 a window that is about ${(len(board) - before) * 0.034:.0f} "
          f"of evaluation that never has to be bought again")
    if args.dry_run:
        print("(dry run; nothing written)")
        return 0
    board.save(root / SCOREBOARD)
    print(f"wrote {root / SCOREBOARD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
