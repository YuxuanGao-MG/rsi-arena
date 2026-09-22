"""Push the order books live forecasts were made against into Supabase.

``collect_live_crypto.py`` writes one JSON object per forecast to
``runs/live/crypto-books.jsonl``: the book at depth twenty, the mempool, and
the last close, at the instant the harness was asked. The forecast itself
goes through ``publish_live.py``; this carries the book, into
``rsi.book_snapshots(topic, symbol, at, book)``, keyed the way migration 008
keys it. A snapshot is a fact about an instant, so a row already there is
left alone: on conflict, do nothing.

Same append-only file, same sidecar, same half-line rule as the forecasts:
``read_rows`` and ``offset_of`` are ``publish_live.py``'s own.

    python scripts/publish_books.py                      # since the last run
    python scripts/publish_books.py --all --dry-run      # everything, no database
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from publish_live import db_url, offset_of, read_rows          # noqa: E402

try:
    import psycopg2
    from psycopg2.extras import Json, execute_values
except ImportError:                                            # pragma: no cover
    sys.exit("pip install psycopg2-binary")


def book_row(row: dict) -> tuple:
    """One snapshot, in the column order ``rsi.book_snapshots`` declares."""
    return (row["topic"], row["symbol"], row["at"], Json(row.get("book")))


def publish(cur, rows: list[dict]) -> int:
    execute_values(cur, """
        insert into rsi.book_snapshots (topic, symbol, at, book)
        values %s
        on conflict (topic, symbol, at) do nothing
    """, [book_row(r) for r in rows])
    return len(rows)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("path", nargs="?", default="runs/live/crypto-books.jsonl")
    ap.add_argument("--db-url", default="")
    ap.add_argument("--all", action="store_true", help="ignore the sidecar and republish the file")
    ap.add_argument("--dry-run", action="store_true", help="print the rows; touch no database")
    args = ap.parse_args()

    path = Path(args.path)
    if not path.exists():
        print(f"no {path}; nothing collected")
        return 0
    sidecar = path.with_suffix(path.suffix + ".published")
    start = 0 if args.all else offset_of(sidecar, path)
    rows, end = read_rows(path, start)
    if not rows:
        print(f"nothing new in {path} (read to byte {start} of {path.stat().st_size})")
        return 0

    if args.dry_run:
        for row in rows:
            topic, symbol, at, book = book_row(row)
            ob = (book.adapted or {}).get("order_book") or {}
            print(f"  {at}  {topic:18} {symbol:8} bid {ob.get('bid')} ask {ob.get('ask')} "
                  f"spread {ob.get('spread_bps')} bps  lean {ob.get('imbalance')}")
        print(f"\n{len(rows)} rows would be written to rsi.book_snapshots")
        return 0

    conn = psycopg2.connect(db_url(args.db_url), connect_timeout=30)
    conn.autocommit = False
    with conn, conn.cursor() as cur:
        written = publish(cur, rows)
    conn.close()
    # Only after the commit, for the same reason publish_live.py waits.
    sidecar.write_text(json.dumps({"bytes": end, "rows": len(rows)}))
    print(f"\n{written} book snapshots published")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
