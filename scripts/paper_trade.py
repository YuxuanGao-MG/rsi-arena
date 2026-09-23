"""Trade a live sweep's forecasts on the topic's paper book, resumed from a file.

Every live workflow runs this after its collector: the graded rows past the
book's last cycle become cycles, the book applies them, and what it traded
and marked is appended to ``<out-dir>/<topic>-trades.jsonl`` and
``<out-dir>/<topic>-marks.jsonl`` for ``publish_trading.py`` to push. The
state itself - cash, positions, every trade and mark - is rewritten to
``--state`` after each run, so the book runs on across sweeps; the workflow
caches that file by prefix and the database is the durable copy.

A re-run over the same forecasts appends nothing: a row at or before the
book's ``last_at`` has been seen. ``--restore-from-db`` rebuilds a state
from ``rsi.books`` and ``rsi.trades`` when the cache has been evicted -
best effort, positions marked at their entry until the next quote.

    python scripts/paper_trade.py --topic kalshi-horizon-5m --forecasts runs/live/forecasts.jsonl \\
        --harness harnesses/horizon-5m-jev.json --state runs/live/books/kalshi-horizon-5m.json
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from publish_live import db_url, read_rows                        # noqa: E402
from rsi_arena.harness import Harness                             # noqa: E402
from rsi_arena.loop.generation import fingerprint                 # noqa: E402
from rsi_arena.topics import spec_of                              # noqa: E402
from rsi_arena.trading import (Book, Position, Trade, TradingSpec, book_stats,   # noqa: E402
                               load_state, rows_to_cycles, step_live)


def trading_spec_for(topic: str) -> TradingSpec:
    """The topic's spec without its task: the topic package's module-level
    ``trading_spec()``, so no benchmark is read to trade a live row."""
    factory = spec_of(topic).factory
    cls = getattr(factory, "__self__", None)
    module = (cls or factory).__module__.rsplit(".", 1)[0]
    return importlib.import_module(f"{module}.trading").trading_spec()


def read_forecasts(path: Path, topic: str) -> list[dict]:
    """Every whole row of the file, on this topic (a row that names none is
    taken to be the first topic's, which is what the Kalshi collector writes)."""
    rows, _ = read_rows(path, 0)
    return [r for r in rows if not r.get("topic") or r["topic"] == topic]


def read_ladders(path: Path | None) -> dict[tuple[str, str], dict[str, Any]]:
    """The crypto collector's books, keyed ``(symbol, at)`` for ``rows_to_cycles``."""
    if path is None or not path.exists():
        return {}
    out: dict[tuple[str, str], dict[str, Any]] = {}
    rows, _ = read_rows(path, 0)
    for snap in rows:
        book = (snap.get("book") or {}).get("order_book") or {}
        if snap.get("symbol") and snap.get("at") and (book.get("bids") or book.get("asks")):
            out[(snap["symbol"], snap["at"])] = {"bids": book.get("bids") or [],
                                                 "asks": book.get("asks") or []}
    return out


def harness_name_of(rows: list[dict], harness: Harness) -> str:
    """The name the rows carry (the collector's harness), else the file's."""
    for r in reversed(rows):
        if r.get("harness"):
            return str(r["harness"])
    return harness.name


def trade_line(topic: str, book: Book, t: Trade) -> dict[str, Any]:
    return {**t.to_dict(), "topic": topic, "book_id": book.book_id,
            "harness_fp": book.harness_fp, "harness_name": book.harness_name}


def open_line(topic: str, book: Book, p: Position) -> dict[str, Any]:
    """An open position as a trade line with no close: the publisher keys on
    (topic, book_id, instrument, opened_at), and the close fills it in."""
    return {"instrument": p.instrument, "side": p.side, "opened_at": p.opened_at.isoformat(),
            "closed_at": None, "entry_px": p.entry_px, "exit_px": None, "qty": p.qty,
            "size_usd": p.notional_usd, "fees_usd": p.fees_usd, "pnl_usd": None, "reason": None,
            "run_id": p.run_id, "instance_id": p.instance_id, "source": p.source,
            "topic": topic, "book_id": book.book_id, "harness_fp": book.harness_fp,
            "harness_name": book.harness_name}


def mark_line(topic: str, book: Book, m: Any) -> dict[str, Any]:
    return {**m.to_dict(), "topic": topic, "book_id": book.book_id}


def append(path: Path, lines: list[dict]) -> None:
    if not lines:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        for line in lines:
            fh.write(json.dumps(line, default=str) + "\n")


def state_of(book: Book, spec: TradingSpec, started_at: str | None) -> dict[str, Any]:
    """The book's dict plus what the publisher reads off a state file."""
    d = book.to_dict()
    d["stats"] = book_stats(book, spec.cycles_per_year)
    d["started_at"] = started_at or (book.marks[0].at.isoformat() if book.marks
                                     else datetime.now(timezone.utc).isoformat())
    return d


def save(path: Path, book: Book, spec: TradingSpec, started_at: str | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state_of(book, spec, started_at), indent=1, sort_keys=True, default=str))
    tmp.replace(path)


def _at(value: Any) -> datetime:
    at = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return at if at.tzinfo is not None else at.replace(tzinfo=timezone.utc)


def restore_from_db(topic: str, spec: TradingSpec, url: str) -> tuple[Book, str | None] | None:
    """A book rebuilt from ``rsi.books`` and ``rsi.trades``: the closed trades
    whole, the open ones as positions marked at their entry, cash backed out
    of the published end equity. Marks are not reloaded (a season of them is
    a lot of rows for a curve the database already has), so the cycle count
    resumes from the stats. None when the database has no such book."""
    import psycopg2

    book_id = f"live:{topic}"
    conn = psycopg2.connect(url, connect_timeout=30)
    try:
        with conn.cursor() as cur:
            cur.execute("select harness_fp, harness_name, started_at, stats from rsi.books "
                        "where topic = %s and book_id = %s", (topic, book_id))
            row = cur.fetchone()
            if row is None:
                return None
            fp, name, started, stats = row[0] or "", row[1] or "", row[2], row[3] or {}
            cur.execute("select instrument, side, opened_at, closed_at, entry_px, exit_px, qty, "
                        "size_usd, fees_usd, pnl_usd, reason, run_id, instance_id, source "
                        "from rsi.trades where topic = %s and book_id = %s order by opened_at",
                        (topic, book_id))
            trades = cur.fetchall()
            cur.execute("select max(at) from rsi.book_marks where topic = %s and book_id = %s",
                        (topic, book_id))
            last_mark = (cur.fetchone() or [None])[0]
    finally:
        conn.close()
    if isinstance(stats, str):
        stats = json.loads(stats)
    book = Book(book_id, topic, fp, spec.costs, start=float(stats.get("start_equity") or 1_000_000.0))
    book.harness_name = name
    last_at = _at(last_mark) if last_mark else None
    for (instrument, side, opened, closed, entry, exit_, qty, size, fees, pnl, reason, run_id,
         instance_id, source) in trades:
        opened_at = _at(opened)
        if closed is None:
            book.positions[instrument] = Position(
                instrument=instrument, side=side, qty=float(qty or 0), entry_px=float(entry or 0),
                notional_usd=float(size or 0), fees_usd=float(fees or 0), opened_at=opened_at,
                deadline=opened_at + 2 * spec.horizon, run_id=run_id or "",
                instance_id=instance_id or "", source=source or "")
            when = opened_at
        else:
            closed_at = _at(closed)
            book.trades.append(Trade(
                instrument=instrument, side=side, opened_at=opened_at, closed_at=closed_at,
                entry_px=float(entry or 0), exit_px=float(exit_ or 0), qty=float(qty or 0),
                size_usd=float(size or 0), fees_usd=float(fees or 0), pnl_usd=float(pnl or 0),
                reason=reason or "horizon", run_id=run_id or "", instance_id=instance_id or "",
                source=source or ""))
            when = closed_at
        last_at = when if last_at is None or when > last_at else last_at
    end = float(stats.get("end_equity") or book.start)
    book.cash = end - sum(book.costs.value(p, p.entry_px) for p in book.positions.values())
    book.peak = max(book.start, end)
    book.last_at, book.processed = last_at, int(stats.get("cycles") or 0)
    return book, (started.isoformat() if isinstance(started, datetime) else started)


def run(args: argparse.Namespace, *, log=print) -> dict[str, Any]:
    topic = args.topic
    spec = trading_spec_for(topic)
    harness = Harness.load(args.harness)
    harness_fp = fingerprint(harness)
    state_path, out_dir = Path(args.state), Path(args.out_dir)
    forecasts = Path(args.forecasts)

    started_at: str | None = None
    book = load_state(state_path)
    if book is not None:
        try:
            started_at = json.loads(state_path.read_text()).get("started_at")
        except (OSError, ValueError):
            started_at = None
    elif args.restore_from_db:
        restored = restore_from_db(topic, spec, db_url(args.db_url))
        if restored is not None:
            book, started_at = restored
            log(f"restored live:{topic} from the database: {len(book.trades)} trades, "
                f"{len(book.positions)} open, last cycle {book.last_at}")
        else:
            log(f"no live:{topic} in the database; starting a fresh book")
    if book is None:
        book = Book(f"live:{topic}", topic, harness_fp, spec.costs)

    rows = read_forecasts(forecasts, topic) if forecasts.exists() else []
    ladders = read_ladders(Path(args.books) if args.books else None)
    cycles = rows_to_cycles(rows, spec, ladders)
    fresh = [c for c in cycles if book.last_at is None or c.at > book.last_at]
    harness_name = harness_name_of(rows, harness)
    before = {k: p.opened_at for k, p in book.positions.items()}
    trades, marks = step_live(book, cycles, harness_fp=harness_fp, harness_name=harness_name,
                              tick=spec.tick)
    opened = [p for k, p in book.positions.items() if before.get(k) != p.opened_at]

    trade_lines = [trade_line(topic, book, t) for t in trades] + [open_line(topic, book, p) for p in opened]
    mark_lines = [mark_line(topic, book, m) for m in marks]
    stats = book_stats(book, spec.cycles_per_year)
    summary = {"topic": topic, "rows": len(rows), "cycles": len(cycles), "applied": len(fresh),
               "trades_closed": len(trades), "opened": len(opened), "marks": len(marks),
               "equity": stats["end_equity"], "total_return": stats["total_return"],
               "open_positions": len(book.positions), "last_at": book.last_at.isoformat() if book.last_at else None,
               "harness": harness_name, "dry_run": bool(args.dry_run)}
    if args.dry_run:
        log(f"dry run: {summary['applied']} of {summary['cycles']} cycles would be applied; "
            f"{len(trade_lines)} trade lines, {len(mark_lines)} marks; nothing written")
        return summary
    append(out_dir / f"{topic}-trades.jsonl", trade_lines)
    append(out_dir / f"{topic}-marks.jsonl", mark_lines)
    save(state_path, book, spec, started_at)
    log(f"live:{topic}: {summary['applied']} cycles applied of {summary['cycles']} "
        f"({summary['rows']} rows), {len(trades)} closed, {len(opened)} opened, "
        f"{len(marks)} marks; equity ${stats['end_equity']:,.2f} ({stats['total_return']:+.2%}), "
        f"{len(book.positions)} open -> {state_path}")
    return summary


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--topic", required=True)
    ap.add_argument("--forecasts", required=True, help="the collector's resolved rows")
    ap.add_argument("--books", default=None, help="the crypto collector's order-book snapshots")
    ap.add_argument("--harness", required=True, help="the harness that forecast, for its fingerprint")
    ap.add_argument("--state", required=True, help="the book's state file, read and rewritten")
    ap.add_argument("--out-dir", default="runs/live", help="where the trades and marks jsonl go")
    ap.add_argument("--restore-from-db", action="store_true",
                    help="with no state file, rebuild one from rsi.books and rsi.trades")
    ap.add_argument("--db-url", default="")
    ap.add_argument("--dry-run", action="store_true", help="say what would happen; write nothing")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    run(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
