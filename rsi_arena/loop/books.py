"""A generation's paper books: the rollouts a benchmark scored, replayed
through ``rsi_arena.trading`` and written beside them.

The loop scores forecasts; this is what they would have been worth as
money. One book per (side, split) of a run, written to
``<run_dir>/books/<side>.<split>.json`` in the shape ``publish_trading.py``
publishes, and each rollout's outcome gets the book's record of its cycle
under ``details["trade"]`` so the rewriter reads the fill beside the skill.

Topic-agnostic: a task that exposes ``trading`` (a ``TradingSpec``) gets a
book, and a task that does not gets nothing. Nothing here imports a topic.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..trading import Book, book_stats, cycles_of, replay_book, with_trade
from .generation import qualified
from .task import Rollout

BOOKS_DIR = "books"


def book_id_for(run_dir: str | Path | None, topic: str, side: str, split: str) -> str:
    """``<qualified run id>:<side>:<split>``; ``bench:<side>:<split>`` off a run."""
    run_id = qualified(run_dir, topic) if run_dir is not None else None
    return f"{run_id or 'bench'}:{side}:{split}"


def book_file(book: Book, *, run_id: str | None, side: str, split: str, stats: dict[str, Any],
              harness_name: str = "") -> dict[str, Any]:
    """The file ``publish_book_file`` reads: the row's columns, then the
    trades and marks whole."""
    started = book.marks[0].at if book.marks else datetime.now(timezone.utc)
    return {"book_id": book.book_id, "topic": book.topic, "harness_fp": book.harness_fp,
            "harness_name": harness_name or book.harness_name, "kind": "replay",
            "run_id": run_id, "side": side, "split": split, "started_at": started.isoformat(),
            "stats": stats, "trades": [t.to_dict() for t in book.trades],
            "marks": [m.to_dict() for m in book.marks],
            "refusals": list(book.refusals)}


def replay_books(task: Any, rollouts: list[Rollout], *, run_dir: str | Path | None, side: str,
                 split: str, harness_fp: str, harness_name: str, write: bool = True,
                 ) -> tuple[dict[str, Any], Path | None]:
    """Replay the rollouts through the task's book; attach each cycle's record
    to its rollout in place; write the book file. Returns the stats and the
    path written (None when not writing or off a run).

    Raises whatever the replay raises: the caller decides whether a book
    failure matters (the loop says it never does).
    """
    spec = task.trading
    cycles = cycles_of(rollouts, spec)
    book_id = book_id_for(run_dir, task.name, side, split)
    book, records = replay_book(cycles, spec, book_id=book_id, harness_fp=harness_fp)
    book.harness_name = harness_name
    stats = book_stats(book, spec.cycles_per_year)
    for i, r in enumerate(rollouts):
        record = records.get(r.instance.id)
        if record is not None:
            rollouts[i] = replace(r, outcome=with_trade(r.outcome, record))
    path = None
    if write and run_dir is not None:
        run_id = qualified(run_dir, task.name)
        path = Path(run_dir) / BOOKS_DIR / f"{side}.{split}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(book_file(book, run_id=run_id, side=side, split=split,
                                             stats=stats, harness_name=harness_name),
                                   indent=1, default=str))
    return stats, path


__all__ = ["BOOKS_DIR", "book_id_for", "book_file", "replay_books"]
