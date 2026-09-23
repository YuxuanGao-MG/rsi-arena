"""Push paper books - the trades, the marks, the running stats - into Supabase.

Two sources feed the same three tables (``rsi.books``, ``rsi.trades``,
``rsi.book_marks``, migration 009):

* A generation's replay books. ``paper_trade.py`` writes one JSON file per
  (side, split) under ``<run_dir>/books/`` with the whole book in it - stats,
  every trade, every mark. ``publish_runs.py`` calls ``publish_book_file`` on
  each after the rollouts, and a book is replaced whole, the way rollouts
  are, so a record is never half one run and half another.

* The live book. Every live sweep appends to ``runs/live/<topic>-trades.jsonl``
  and ``runs/live/<topic>-marks.jsonl`` and rewrites its state to
  ``runs/live/books/<topic>.json``. Those files never leave the runner - the
  directory is gitignored - so this runs after every sweep, with
  ``publish_live.py``'s sidecar: a ``.published`` file beside each jsonl
  remembers how far it was read, written only after the commit.

A trade is appended once when it opens and again when it closes, and the
table keys it on (topic, book_id, instrument, opened_at), so the second line
fills in ``closed_at``, ``exit_px`` and ``pnl_usd`` on the row the first made.
A mark is a fact about an instant: on conflict, the row already there stands.
Losing a sidecar therefore republishes the file rather than duplicating it.

The tables arrive with a migration someone applies by hand, and the publisher
runs on a cron beside it. If they are not there yet this says so and exits 0;
the files stay on disk and in the run artifact, and the next invocation after
the migration publishes them.

    python scripts/publish_trading.py --topic kalshi-horizon-5m
    python scripts/publish_trading.py --topic crypto-horizon-1m --all --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from publish_live import db_url, env_from, offset_of, read_rows     # noqa: E402,F401

try:
    import psycopg2
    from psycopg2.extras import Json, execute_values
except ImportError:                                                # pragma: no cover
    sys.exit("pip install psycopg2-binary")

#: The topic a book file that names none belongs to.
DEFAULT_TOPIC = "kalshi-horizon-5m"

#: What migration 009 creates. All three or nothing: a database with some of
#: them is mid-migration and gets the same notice as one with none.
TABLES = ("books", "trades", "book_marks")
NOT_APPLIED = ("rsi.books / rsi.trades / rsi.book_marks are not in this database; "
               "apply supabase/migrations/009_trading.sql. Nothing published.")

BOOK_COLUMNS = ("topic", "book_id", "harness_fp", "harness_name", "kind", "run_id",
                "side", "split", "started_at", "stats")
TRADE_COLUMNS = ("topic", "book_id", "harness_fp", "run_id", "instance_id", "side",
                 "instrument", "opened_at", "closed_at", "entry_px", "exit_px", "qty",
                 "size_usd", "fees_usd", "pnl_usd", "reason", "source")
TRADE_KEY = ("topic", "book_id", "instrument", "opened_at")
MARK_COLUMNS = ("topic", "book_id", "at", "equity_usd", "cash_usd", "gross_exposure_usd",
                "open_positions", "drawdown", "event")
MARK_KEY = ("topic", "book_id", "at")


# ---------------------------------------------------------------------------
# The probe.

def trading_tables(cur) -> set[str]:
    """Which of 009's tables this database has, or none if it will not say."""
    try:
        cur.execute("select table_name from information_schema.tables "
                    "where table_schema = 'rsi' and table_name in ('books', 'trades', 'book_marks')")
        return {r[0] for r in cur.fetchall()}
    except Exception:  # noqa: BLE001 - a probe that fails is a database without them
        return set()


def has_trading_tables(cur) -> bool:
    return trading_tables(cur) >= set(TABLES)


# ---------------------------------------------------------------------------
# Rows.

def book_row(topic: str, book: dict, *, kind: str, book_id: str) -> dict[str, Any]:
    """One ``rsi.books`` row from a book file or a live state file."""
    return {"topic": topic, "book_id": book_id,
            "harness_fp": book.get("harness_fp"), "harness_name": book.get("harness_name"),
            "kind": kind, "run_id": book.get("run_id"), "side": book.get("side"),
            "split": book.get("split"), "started_at": book.get("started_at"),
            "stats": Json(book.get("stats") or {})}


def trade_row(topic: str, book_id: str, t: dict, *, harness_fp: str | None = None) -> dict[str, Any]:
    """One ``rsi.trades`` row. A live line carries its own book and harness;
    a replay book's trades take the book's."""
    return {"topic": topic, "book_id": t.get("book_id") or book_id,
            "harness_fp": t.get("harness_fp") or harness_fp,
            "run_id": t.get("run_id"), "instance_id": t.get("instance_id"),
            "side": t.get("side"), "instrument": t.get("instrument"),
            "opened_at": t.get("opened_at"), "closed_at": t.get("closed_at"),
            "entry_px": t.get("entry_px"), "exit_px": t.get("exit_px"), "qty": t.get("qty"),
            "size_usd": t.get("size_usd"), "fees_usd": t.get("fees_usd"),
            "pnl_usd": t.get("pnl_usd"), "reason": t.get("reason"), "source": t.get("source")}


def mark_row(topic: str, book_id: str, m: dict) -> dict[str, Any]:
    return {"topic": topic, "book_id": m.get("book_id") or book_id, "at": m.get("at"),
            "equity_usd": m.get("equity_usd"), "cash_usd": m.get("cash_usd"),
            "gross_exposure_usd": m.get("gross_exposure_usd"),
            "open_positions": m.get("open_positions"), "drawdown": m.get("drawdown"),
            "event": m.get("event")}


def latest_by(rows: list[dict[str, Any]], key: tuple[str, ...]) -> list[dict[str, Any]]:
    """One row per key, the last one written winning, in first-seen order.

    ``on conflict do update`` refuses to touch the same row twice in one
    statement, and a trade that opened and closed inside one sweep is two
    lines with one key. The close is the later line and the one that counts.
    Rows missing part of the key would fail the insert's not-null checks, and
    are dropped rather than failing the batch.
    """
    kept: dict[tuple, dict[str, Any]] = {}
    for row in rows:
        k = tuple(row.get(c) for c in key)
        if any(v is None for v in k):
            continue
        kept[k] = row
    return list(kept.values())


# ---------------------------------------------------------------------------
# Writes.

def upsert_book(cur, row: dict[str, Any]) -> None:
    cur.execute(f"""
        insert into rsi.books ({", ".join(BOOK_COLUMNS)})
        values ({", ".join("%s" for _ in BOOK_COLUMNS)})
        on conflict (topic, book_id) do update set
            harness_fp = excluded.harness_fp, harness_name = excluded.harness_name,
            stats = excluded.stats, updated_at = now()
    """, tuple(row[c] for c in BOOK_COLUMNS))


def upsert_trades(cur, rows: list[dict[str, Any]]) -> int:
    rows = latest_by(rows, TRADE_KEY)
    if not rows:
        return 0
    updates = ", ".join(f"{c} = excluded.{c}" for c in TRADE_COLUMNS if c not in TRADE_KEY)
    execute_values(cur, f"""
        insert into rsi.trades ({", ".join(TRADE_COLUMNS)})
        values %s
        on conflict (topic, book_id, instrument, opened_at) do update set {updates}
    """, [tuple(r[c] for c in TRADE_COLUMNS) for r in rows])
    return len(rows)


def insert_marks(cur, rows: list[dict[str, Any]]) -> int:
    rows = latest_by(rows, MARK_KEY)
    if not rows:
        return 0
    execute_values(cur, f"""
        insert into rsi.book_marks ({", ".join(MARK_COLUMNS)})
        values %s
        on conflict (topic, book_id, at) do nothing
    """, [tuple(r[c] for c in MARK_COLUMNS) for r in rows])
    return len(rows)


def delete_book_rows(cur, topic: str, book_id: str) -> None:
    cur.execute("delete from rsi.trades where topic = %s and book_id = %s", (topic, book_id))
    cur.execute("delete from rsi.book_marks where topic = %s and book_id = %s", (topic, book_id))


# ---------------------------------------------------------------------------
# A replay book, whole.

def topic_of(path: Path, book: dict) -> str:
    """The topic a book file belongs to: its own say, else the run's manifest
    two directories up, else the first topic's."""
    if book.get("topic"):
        return book["topic"]
    manifest = path.resolve().parent.parent / "manifest.json"
    if manifest.exists():
        try:
            found = json.loads(manifest.read_text()).get("topic")
        except (ValueError, OSError):
            found = None
        if found:
            return found
    return DEFAULT_TOPIC


def publish_book_file(cur, path: str | Path, *, topic: str | None = None) -> tuple[int, int]:
    """One ``<run_dir>/books/<side>.<split>.json``, replaced whole.

    The books row is upserted, the book's trades and marks are deleted and
    inserted again. Returns (trades, marks) written.
    """
    path = Path(path)
    book = json.loads(path.read_text())
    topic = topic or topic_of(path, book)
    book_id = book["book_id"]
    upsert_book(cur, book_row(topic, book, kind=book.get("kind") or "replay", book_id=book_id))
    delete_book_rows(cur, topic, book_id)
    fp = book.get("harness_fp")
    trades = upsert_trades(cur, [trade_row(topic, book_id, t, harness_fp=fp)
                                 for t in book.get("trades") or []])
    marks = insert_marks(cur, [mark_row(topic, book_id, m) for m in book.get("marks") or []])
    return trades, marks


# ---------------------------------------------------------------------------
# The live book, incrementally.

def live_paths(live_dir: Path, topic: str) -> tuple[Path, Path, Path]:
    """(trades jsonl, marks jsonl, state json) for a topic's live book."""
    return (live_dir / f"{topic}-trades.jsonl",
            live_dir / f"{topic}-marks.jsonl",
            live_dir / "books" / f"{topic}.json")


def publish_live_book(cur, topic: str, state: dict | None,
                      trades: list[dict], marks: list[dict]) -> dict[str, int]:
    """The live book's row from its state, then the new trades and marks."""
    book_id = f"live:{topic}"
    out = {"books": 0, "trades": 0, "marks": 0}
    if state is not None:
        upsert_book(cur, book_row(topic, state, kind="live", book_id=book_id))
        out["books"] = 1
    out["trades"] = upsert_trades(cur, [trade_row(topic, book_id, t) for t in trades])
    out["marks"] = insert_marks(cur, [mark_row(topic, book_id, m) for m in marks])
    return out


def pending(path: Path, *, everything: bool) -> tuple[Path, list[dict], int, int]:
    """(sidecar, rows past the mark, where reading started, where it stopped)."""
    sidecar = path.with_suffix(path.suffix + ".published")
    start = 0 if everything else offset_of(sidecar, path)
    rows, end = read_rows(path, start)
    return sidecar, rows, start, end


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--topic", required=True)
    ap.add_argument("--live-dir", default="runs/live")
    ap.add_argument("--db-url", default="")
    ap.add_argument("--all", action="store_true", help="ignore the sidecars and republish the files")
    ap.add_argument("--dry-run", action="store_true", help="print the counts; touch no database")
    args = ap.parse_args()

    live_dir = Path(args.live_dir)
    trades_path, marks_path, state_path = live_paths(live_dir, args.topic)

    files: list[tuple[Path, Path, list[dict], int]] = []     # (path, sidecar, rows, end)
    new: dict[str, list[dict]] = {"trades": [], "marks": []}
    for name, path in (("trades", trades_path), ("marks", marks_path)):
        if not path.exists():
            print(f"no {path}")
            continue
        sidecar, rows, start, end = pending(path, everything=args.all)
        if not rows:
            print(f"nothing new in {path} (read to byte {start} of {path.stat().st_size})")
            continue
        new[name] = rows
        files.append((path, sidecar, rows, end))

    state = None
    if state_path.exists():
        state = json.loads(state_path.read_text())
    else:
        print(f"no {state_path}; the book row is left as it is")

    if state is None and not files:
        print("nothing to publish")
        return 0

    if args.dry_run:
        if state is not None:
            print(f"  book   live:{args.topic}  {state.get('harness_name')}  "
                  f"{json.dumps(state.get('stats') or {}, default=str)[:200]}")
        for t in latest_by([trade_row(args.topic, f"live:{args.topic}", t) for t in new["trades"]],
                           TRADE_KEY):
            print(f"  trade  {t['opened_at']}  {t['side']:5} {t['instrument']:34} "
                  f"closed {t['closed_at']}  pnl {t['pnl_usd']}  {t['reason']}")
        print(f"\n{len(new['trades'])} trade lines, {len(new['marks'])} marks would be written")
        return 0

    conn = psycopg2.connect(db_url(args.db_url), connect_timeout=30)
    conn.autocommit = False
    written = None
    with conn, conn.cursor() as cur:
        if has_trading_tables(cur):
            written = publish_live_book(cur, args.topic, state, new["trades"], new["marks"])
    conn.close()
    if written is None:
        # The sidecars stay where they were, so the rows are published by the
        # first invocation after the migration lands.
        print(f"::notice title=migration 009 not applied::{NOT_APPLIED}")
        return 0
    # Only after the commit. A sidecar moved ahead of a failed transaction is
    # how a day's trades go missing without anything reporting a failure.
    for _path, sidecar, rows, end in files:
        sidecar.write_text(json.dumps({"bytes": end, "rows": len(rows)}))
    print(f"\nlive:{args.topic}: {written['books']} book row, {written['trades']} trades, "
          f"{written['marks']} marks published")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
