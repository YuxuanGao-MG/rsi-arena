"""Restate the Sharpe of every stored book, which was never a Sharpe.

A replay book's marks are the question set's windows, and the question set is a
sample: one crypto holdout book's 960 marks are spread over 120 days of a year,
median gap three hours, not one pair of them consecutive minutes. Annualising
that by the square root of the topic's cycles a year treated a scattered sample
as a continuous series and produced -395 per cycle, -85 daily, for a book that
lost two percent. `rsi_arena/trading/stats.py` no longer computes one for a
sampled book, but every row written before 4 October carries the old number and
the reader has no way to know it is meaningless.

So this recomputes the two fields from what is already in the database - the
marks say whether the book is a series, the trades give a t that needs no clock -
and leaves every other statistic alone. No rollouts are read and nothing is
re-traded: this is arithmetic on rows that are already there.

    python scripts/restate_sharpe.py --dry-run
    python scripts/restate_sharpe.py
"""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import psycopg2                                                    # noqa: E402

from publish_runs import db_url                                    # noqa: E402
from rsi_arena.trading.stats import CYCLES_PER_YEAR                # noqa: E402


class _Mark:
    """Enough of a Mark for ``is_contiguous``."""

    def __init__(self, at):
        self.at = at


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db-url", default="")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    from rsi_arena.trading.stats import is_contiguous

    conn = psycopg2.connect(db_url(args.db_url), connect_timeout=30)
    conn.autocommit = False
    changed = kept = 0
    with conn, conn.cursor() as cur:
        cur.execute("select topic, book_id, kind, stats from rsi.books order by topic, book_id")
        books = cur.fetchall()
        for topic, book_id, kind, stats in books:
            if not isinstance(stats, dict):
                continue
            cur.execute("""select at from rsi.book_marks where topic=%s and book_id=%s
                            order by at""", (topic, book_id))
            marks = [_Mark(r[0]) for r in cur.fetchall()]
            per_year = CYCLES_PER_YEAR.get(topic, 0)
            gap = (365 * 24 * 3600) / per_year if per_year else 0
            contiguous = is_contiguous(marks, gap)

            cur.execute("""select pnl_usd from rsi.trades where topic=%s and book_id=%s""",
                        (topic, book_id))
            pnls = [float(r[0]) for r in cur.fetchall() if r[0] is not None]
            t = None
            if len(pnls) >= 3:
                sd = statistics.stdev(pnls)
                if sd:
                    t = statistics.mean(pnls) / (sd / math.sqrt(len(pnls)))

            fresh = dict(stats)
            fresh["contiguous"] = contiguous
            fresh["trade_t"] = t
            if not contiguous:
                fresh["sharpe"] = None
                fresh["daily_sharpe"] = None
            if fresh == stats:
                kept += 1
                continue
            changed += 1
            was = stats.get("daily_sharpe")
            print(f"  {topic:20} {book_id[:38]:38} {kind:6} "
                  f"daily Sharpe {'' if was is None else f'{was:+.1f}'} -> "
                  f"{'null' if not contiguous else 'kept'}"
                  f"   t {'—' if t is None else f'{t:+.2f}'}")
            if not args.dry_run:
                cur.execute("update rsi.books set stats = %s where topic=%s and book_id=%s",
                            (json.dumps(fresh), topic, book_id))
        if args.dry_run:
            conn.rollback()
    conn.close()
    print(f"\n{changed} book(s) restated, {kept} already right"
          + (" (dry run; nothing written)" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
