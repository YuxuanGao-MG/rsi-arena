"""The same book on the live collectors' rows, resumed from a file each sweep.

A live collector writes one row per forecast and grades it a few minutes
later; this reads the graded rows, turns each into a cycle, and applies the
ones the book has not seen. A position carries until the agent closes it or
its deadline expires, swept at every cycle, exactly as in replay; and the
harness behind a topic's book changes when a promotion lands, so the book
records a handover and keeps its positions - the new harness inherits the
old one's exposure the way a desk inherits a departing trader's, because
closing everything on a promotion would charge the new harness a round trip
it never asked for.

The quote comes from whatever the collector kept: Kalshi rows carry
``yes_bid``/``yes_ask``, crypto rows carry the touch under ``context.book``
and sometimes a ladder alongside, and an equity row carries only a mid. A
row with none of these gets a proxy quote from the venue's own spread
constant, flagged so a report can say how much of a book's P&L was earned
against invented spreads. Two more optional keys drive the posted quote and
the deadline: ``path``, the bars the price walked after the instant, and
``settlement``, what a resolved market pays.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .book import Book, Mark, PathBar, Trade
from .costs import Quote, default_tick
from .replay import Cycle, apply_cycle

_DELTA_KEYS = {"cents": ("delta_cents", "half_width_cents"), "bps": ("delta_bps", "half_width_bps")}

#: What a collector may write a binary settlement as.
_SETTLEMENT = {"yes": 1.0, "no": 0.0, "1": 1.0, "0": 0.0, "true": 1.0, "false": 0.0}


def _at(value: Any) -> datetime:
    at = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return at if at.tzinfo is not None else at.replace(tzinfo=timezone.utc)


def _levels(raw: Any) -> tuple[tuple[float, float], ...]:
    try:
        return tuple((float(p), float(q)) for p, q in (raw or ()))
    except (TypeError, ValueError):
        return ()


def _touch(at: datetime, bid: Any, ask: Any, ladder: dict | None) -> Quote | None:
    try:
        b, a = float(bid), float(ask)
    except (TypeError, ValueError):
        return None
    if a < b:
        return None
    return Quote(at=at, bid=b, ask=a, mid=(a + b) / 2,
                 bids=_levels(ladder.get("bids")) if ladder else (),
                 asks=_levels(ladder.get("asks")) if ladder else ())


def _entry_quote(row: dict, at: datetime, mid: float, costs: Any, ladder: dict | None) -> Quote:
    q = _touch(at, row.get("yes_bid"), row.get("yes_ask"), ladder)
    if q is None:
        book = (row.get("context") or {}).get("book") or {}
        q = _touch(at, book.get("bid"), book.get("ask"), ladder)
    return q if q is not None else costs.proxy_quote(at, mid)


def _exit_quote(row: dict, at: datetime, realised: float, costs: Any) -> Quote:
    rq = row.get("realised_quote") or {}
    q = _touch(at, rq.get("bid"), rq.get("ask"), None)
    return q if q is not None else costs.proxy_quote(at, realised)


def _delta(row: dict, mid: float, unit: str) -> tuple[float, float]:
    """The forecast in ``unit``, from the row's own numbers when the grader
    wrote them and from the output's keys otherwise."""
    if row.get("predicted") is not None:
        p, h = float(row["predicted"]), float(row.get("half_width") or 0.0)
        if unit == "cents":
            return (p - mid) * 100.0, h * 100.0
        return ((p / mid - 1.0) * 1e4 if mid else 0.0), h
    out = row.get("output")
    if not isinstance(out, dict):
        return 0.0, 0.0
    dk, hk = _DELTA_KEYS[unit]
    try:
        return float(out.get(dk) or 0.0), float(out.get(hk) or 0.0)
    except (TypeError, ValueError):
        return 0.0, 0.0


def _path(raw: Any) -> tuple[PathBar, ...]:
    """``row["path"]``: the bars the price walked after the instant, each
    ``{ts, high, low, close}``. A row without one simply never fills a quote,
    so a malformed path is dropped rather than raised on."""
    out = []
    for bar in raw or ():
        try:
            out.append(PathBar.from_dict(bar))
        except (KeyError, TypeError, ValueError):
            continue
    return tuple(sorted(out, key=lambda b: b.ts))


def _settlement(raw: Any) -> float | None:
    """``row["settlement"]``: a number, or the ``yes``/``no`` a Kalshi market
    resolves to. None when the market has not resolved."""
    if raw is None or isinstance(raw, bool):
        return None if raw is None else float(raw)
    if isinstance(raw, (int, float)):
        return float(raw)
    return _SETTLEMENT.get(str(raw).strip().lower())


def rows_to_cycles(rows: list[dict], spec_like: Any, books_by_key: dict | None = None) -> list[Cycle]:
    """Graded rows to cycles, skipping the ungraded. ``spec_like`` needs
    ``costs``, ``unit`` and ``horizon``; ``books_by_key`` is the crypto
    collector's ladders keyed ``(symbol, at_iso)``."""
    out = []
    for row in rows:
        realised = row.get("realised")
        if realised is None or row.get("mid_now") is None:
            continue
        # The symbol first: a crypto or news row's ``ticker`` is the row's own
        # id (``BTCUSDT@<at>``), and a book keyed on it would never carry a
        # position from one cycle to the next. A Kalshi row has only a ticker.
        instrument = row.get("symbol") or row.get("ticker")
        if not instrument:
            continue
        at = _at(row["at"])
        mid, realised = float(row["mid_now"]), float(realised)
        ladder = None
        if books_by_key:
            ladder = books_by_key.get((instrument, at.isoformat())) or books_by_key.get((instrument, row["at"]))
        horizon_at = _at(row["horizon_at"]) if row.get("horizon_at") else at + spec_like.horizon
        deadline = _at(row["deadline"]) if row.get("deadline") else horizon_at + (horizon_at - at)
        delta, half = _delta(row, mid, spec_like.unit)
        out.append(Cycle(at=at, instrument=instrument,
                         instance_id=str(row.get("id") or f"{instrument}@{at.isoformat()}"),
                         run_id=str(row.get("run_id") or ""), output=row.get("output"),
                         delta=delta, half_width=half,
                         entry=_entry_quote(row, at, mid, spec_like.costs, ladder),
                         exit=_exit_quote(row, horizon_at, realised, spec_like.costs),
                         horizon_at=horizon_at, deadline=deadline,
                         path=_path(row.get("path")), settlement=_settlement(row.get("settlement"))))
    return sorted(out, key=lambda c: (c.at, c.instrument, c.instance_id))


def load_state(path: str | Path) -> Book | None:
    p = Path(path)
    if not p.exists():
        return None
    return Book.from_dict(json.loads(p.read_text()))


def save_state(path: str | Path, book: Book) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(book.to_dict(), indent=1, sort_keys=True))
    tmp.replace(p)


def step_live(book: Book, cycles: list[Cycle], *, harness_fp: str, harness_name: str,
              tick: float | None = None) -> tuple[list[Trade], list[Mark]]:
    """Apply the cycles the book has not seen; what it traded and marked."""
    tick = default_tick(book.costs) if tick is None else tick
    trades0, marks0 = len(book.trades), len(book.marks)
    fresh = sorted((c for c in cycles if book.last_at is None or c.at > book.last_at),
                   key=lambda c: (c.at, c.instrument, c.instance_id))
    if fresh and harness_name != book.harness_name:
        if book.harness_name:
            book.handovers.append({"at": fresh[0].at.isoformat(), "from": book.harness_name,
                                   "to": harness_name})
            book.mark(fresh[0].at, event="handover")
        book.harness_name, book.harness_fp = harness_name, harness_fp
    for c in fresh:
        apply_cycle(book, c, tick)
    return book.trades[trades0:], book.marks[marks0:]


__all__ = ["rows_to_cycles", "load_state", "save_state", "step_live"]
