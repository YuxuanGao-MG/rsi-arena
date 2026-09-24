"""Rebuild a run's paper books from the rollouts it already scored.

A generation run before the books existed has everything a book needs on
disk - its manifest names the topic and the harnesses, its rollouts carry
each instance, each outcome's details and each run's output - and nothing
that needs a model or a network. This replays them the way ``cmd_optimize``
now does as it goes, and writes ``<run_dir>/books/<side>.<split>.json`` in
the shape ``publish_trading.py`` publishes. The rollouts files are read,
never rewritten. Each instance is the question set's copy when the set
agrees it is the same question, so a set quoted after the run was scored
(``scripts/quote_windows.py``) lends its touch to the replay.

    python scripts/backfill_books.py runs/kalshi-jev/gen1 runs/crypto-horizon-1m/gen1
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rsi_arena.harness import Harness                      # noqa: E402
from rsi_arena.loop import Outcome, Rollout, Settings       # noqa: E402
from rsi_arena.loop.books import replay_books               # noqa: E402
from rsi_arena.topics import load_topic, spec_of            # noqa: E402


class _Run:
    """What a rollout file kept of its run: enough for the book (the output
    and the id) and for ``Rollout.cost_usd``."""

    def __init__(self, d: dict[str, Any]) -> None:
        self.output = d.get("output")
        self.run_id = d.get("run_id") or ""
        self.harness = d.get("harness") or ""
        self.ok = bool(d.get("ok"))
        self.cost_usd = float(d.get("cost_usd") or 0.0)


#: What the run's own settings decide about the question set it was scored on:
#: where it lives, and how much of it the thinner kept. ``per_fixture`` is in
#: the list because ``Settings`` carries a concrete default for it (Kalshi's
#: eight), which ``TopicSpec.fill`` will not replace - so a crypto run scored
#: on twenty-four instants a day was rebuilt here against eight of them, and
#: two thirds of its rollouts found no copy of themselves in the set to take
#: a touch or a path from.
FROM_RUN = ("benchmark", "windows_dir", "cache_dir", "per_fixture", "every")


def load_task(topic: str, settings: dict[str, Any] | None = None) -> Any:
    """The topic's task, offline: the benchmark file and the stores on disk,
    with the run's own benchmark, windows paths and thinning when the manifest
    kept them."""
    s = Settings(topic=topic)
    for name in FROM_RUN:
        if settings and settings.get(name) is not None:
            setattr(s, name, settings[name])
    return load_topic(spec_of(topic).fill(s))


def load_rollouts(task: Any, path: Path) -> list[Rollout]:
    rows = json.loads(path.read_text())
    out = []
    for d in rows:
        run = _Run(d["run"]) if d.get("run") else None
        out.append(Rollout(instance=task.instance_from_dict(d["instance"]), run=run,
                           outcome=Outcome(**d["outcome"]),
                           remembered_cost=None if run else d.get("cost_usd")))
    return out


def with_set_quotes(task: Any, rollouts: list[Rollout]) -> list[Rollout]:
    """The rollouts with each instance swapped for the question set's own copy
    when that copy is the same question and carries more.

    A rollout dumped before the touch was kept has an instance with mids and
    nothing else, and a book built from it can only cross the proxy. The set
    on disk may since have been quoted (``scripts/quote_windows.py``), and it
    is the same question exactly when the id and the two numbers the
    scoreboard keys on agree - so that copy, touch and all, is what the book
    replays. Anything else about the rollout stays as dumped.
    """
    try:
        fresh = {w.id: w for w in task.instances()}
    except Exception:  # noqa: BLE001 - a set that cannot be listed leaves the rollouts as they are
        return rollouts
    out = []
    for r in rollouts:
        w = fresh.get(r.instance.id)
        if w is not None and w is not r.instance and all(
                getattr(w, f, None) == getattr(r.instance, f, None) for f in ("mid_now", "realised")):
            r = replace(r, instance=w)
        out.append(r)
    return out


def harness_of(run_dir: Path, manifest: dict[str, Any], side: str,
               rollouts: list[Rollout]) -> tuple[str, str]:
    """(fingerprint, name) of the harness behind a side, from the manifest and
    the harness files it points at; the rollouts' own run names as a fallback."""
    if side == "candidate":
        fp, source = manifest.get("candidate_fingerprint") or "", run_dir / "best.json"
    else:
        fp, source = manifest.get("incumbent_fingerprint") or "", Path(manifest.get("incumbent") or "")
    name = ""
    if source and Path(source).is_file():
        try:
            name = Harness.load(source).name
        except Exception:  # noqa: BLE001 - a name is a label; the fingerprint is the identity
            name = ""
    if not name:
        name = next((r.run.harness for r in rollouts if r.run is not None and r.run.harness), "")
    return fp, name


def backfill(run_dir: Path, *, log=print) -> dict[str, dict[str, Any]]:
    manifest = json.loads((run_dir / "manifest.json").read_text())
    topic = manifest["topic"]
    task = load_task(topic, manifest.get("settings"))
    if getattr(task, "trading", None) is None:
        log(f"{run_dir}: {topic} has no trading spec; nothing to build")
        return {}
    files = sorted((run_dir / "rollouts").glob("*.json"))
    loaded = {p: with_set_quotes(task, load_rollouts(task, p)) for p in files}
    # Every instance the run scored, so a deadline that falls back to "the
    # last window on this contract" reads the whole set and not one split.
    seen: dict[str, Any] = {}
    for rollouts in loaded.values():
        for r in rollouts:
            seen.setdefault(r.instance.id, r.instance)
    task.use_instances(list(seen.values()))
    out: dict[str, dict[str, Any]] = {}
    for path, rollouts in loaded.items():
        side, split = path.stem.split(".", 1)
        if not rollouts:
            log(f"  {side}.{split}: no rollouts")
            continue
        fp, name = harness_of(run_dir, manifest, side, rollouts)
        stats, written = replay_books(task, rollouts, run_dir=run_dir, side=side, split=split,
                                      harness_fp=fp, harness_name=name)
        out[f"{side}.{split}"] = stats
        log(f"  {side}.{split}: {len(rollouts)} rollouts -> {stats['trades']} trades, "
            f"return {stats['total_return']:+.2%}, max drawdown {stats['max_drawdown']:.2%}, "
            f"fees ${stats['fees_usd']:,.2f} -> {written}")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("run_dir", nargs="+")
    args = ap.parse_args(argv)
    failed = 0
    for raw in args.run_dir:
        run_dir = Path(raw)
        if not (run_dir / "manifest.json").exists():
            print(f"{run_dir}: not a run directory")
            failed += 1
            continue
        print(f"{run_dir}")
        try:
            backfill(run_dir)
        except Exception as exc:  # noqa: BLE001 - one run's failure is not the batch's
            print(f"  failed: {type(exc).__name__}: {exc}")
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
