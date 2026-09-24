"""Trade a finished evaluation: the rollouts a benchmark already scored, run
through a book as if each forecast had been an order.

Nothing is fetched and nothing is re-run. A rollout is a question, the
harness's answer, and the price the venue printed at the horizon; the
engine reads the forecast from the *outcome's* details rather than the run's
output because a memoised rollout has no run at all (``Rollout.run`` is None
when the answer was remembered off the scoreboard), and the details are
where the topic wrote what it scored.

The book is topic-agnostic; what a topic has to say is small and arrives as
a :class:`TradingSpec` of functions: how to quote an instance at its instant
and at its horizon, the price path the venue printed after it, which
instrument it is, what it settles at if it settles, when a position in it
must be out, and how to read the forecast off an outcome.

Nothing closes at a horizon. A forecast's horizon is where the *score* is
taken, not where a position ends: the quote a cycle posts rests, the take a
cycle crosses stays on, and both carry until the agent closes them or the
deadline arrives - a resolved Kalshi market settling at zero or one, every
other venue force-closing at the last mid. A cycle's record is therefore
what it *realised*; what it is still carrying sits in the marks.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, Callable

from .book import MAX_POSITION, Book, PathBar
from .costs import Quote, VenueCosts
from .policy import decide


@dataclass(frozen=True)
class Cycle:
    """One forecast, ready to trade: the quote it was made against, the quote
    printed at its horizon, the bars the price walked through afterwards, and
    what it said."""

    at: datetime
    instrument: str
    instance_id: str
    run_id: str
    output: Any
    delta: float
    half_width: float
    entry: Quote
    exit: Quote
    horizon_at: datetime
    deadline: datetime
    #: The realised path over ``(at, horizon_at]``, in the instrument's own
    #: price space; what the posted quote is filled by. Empty on a window
    #: built before paths were kept, which simply never fills a quote.
    path: tuple[PathBar, ...] = ()
    #: What the instrument pays at its deadline when that is already known
    #: (a Kalshi contract that resolved: 0 or 1); None for everything else.
    settlement: float | None = None


def _no_path(instance: Any) -> tuple[PathBar, ...]:
    return ()


def _no_settlement(instance: Any) -> float | None:
    return None


@dataclass(frozen=True)
class TradingSpec:
    """How a topic's instances become cycles. ``delta_of`` reads
    ``(delta, half_width)`` in ``unit`` from an outcome, or None when it
    said nothing; the others read an instance."""

    costs: VenueCosts
    unit: str
    tick: float
    cycles_per_year: float
    quote_of: Callable[[Any], tuple[Quote, Quote]]
    deadline_of: Callable[[Any], datetime]
    instrument_of: Callable[[Any], str]
    delta_of: Callable[[Any], "tuple[float, float] | None"]
    topic: str = ""
    #: What live rows fall back on when they carry no horizon of their own.
    horizon: timedelta = timedelta(minutes=5)
    #: The bars the price walked after the instant, and the settlement the
    #: instrument is already known to pay. Both default to nothing, so a topic
    #: that keeps neither still builds cycles.
    path_of: Callable[[Any], "tuple[PathBar, ...]"] = field(default=_no_path)
    settlement_of: Callable[[Any], "float | None"] = field(default=_no_settlement)


def delta_from_details(details: dict[str, Any], unit: str) -> tuple[float, float] | None:
    """``(delta, half_width)`` in ``unit`` from a MoveScore's ``to_dict``,
    which every five-minute topic writes into ``Outcome.details``.

    ``half_width`` there is in the metric's working unit - dollars for an
    absolute metric, basis points for a relative one - so cents needs the
    hundred and bps does not.
    """
    if not details.get("scored"):
        return None
    try:
        mid, predicted, half = float(details["mid_now"]), float(details["predicted"]), float(details["half_width"])
    except (KeyError, TypeError, ValueError):
        return None
    if unit == "cents":
        return (predicted - mid) * 100.0, half * 100.0
    if not mid:
        return None
    return (predicted / mid - 1.0) * 1e4, half


def cycles_of(rollouts: list[Any], spec: TradingSpec) -> list[Cycle]:
    """Sorted so a shuffled input replays to the same book."""
    out = []
    for r in rollouts:
        inst = r.instance
        entry, exit_ = spec.quote_of(inst)
        read = spec.delta_of(r.outcome)
        delta, half = read if read is not None else (0.0, 0.0)
        run = getattr(r, "run", None)
        out.append(Cycle(at=inst.at, instrument=spec.instrument_of(inst), instance_id=inst.id,
                         run_id=getattr(run, "run_id", "") or "",
                         output=run.output if run else _remembered_order(r.outcome),
                         delta=delta, half_width=half, entry=entry, exit=exit_,
                         horizon_at=exit_.at, deadline=spec.deadline_of(inst),
                         path=tuple(spec.path_of(inst) or ()),
                         settlement=spec.settlement_of(inst)))
    return sorted(out, key=lambda c: (c.at, c.instrument, c.instance_id))


def _remembered_order(outcome: Any) -> dict[str, Any] | None:
    """The order a rollout with no run gave, if an earlier book recorded one.

    A memoised rollout has no output, so its cycle used to trade the default
    rule whatever the harness had said - which judged a remembered candidate
    by a rule and a fresh one by its own hand. The record a generation's book
    left under ``details["trade"]`` keeps the harness's action and size when
    the harness gave them (``source == "harness"``); a default-rule record is
    not replayed as an order, because it was not one.
    """
    trade = (getattr(outcome, "details", None) or {}).get("trade")
    if not isinstance(trade, dict) or trade.get("source") != "harness":
        return None
    return {"action": trade.get("action"), "size": trade.get("size")}


def book_line(record: dict[str, Any]) -> str:
    """``Book: quote 0.49/0.51 2% bid hit, open_long 5% -> +120 USD (quote)``,
    from a replay record. The quote clause is dropped from a record that has
    none, which is what a hand-built record and an older book file are."""
    action, size = record.get("action", "hold"), float(record.get("size") or 0.0)
    pnl = float(record.get("pnl_usd") or 0.0)
    reason = record.get("reason") or ("refused" if record.get("refused") else "no trade")
    quote, posted = "", record.get("quote")
    if posted:
        sides = {f["side"] for f in record.get("fills") or ()}
        hit = "both" if len(sides) == 2 else next(iter(sides), "")
        quote = (f"quote {posted['bid']:.4g}/{posted['ask']:.4g} "
                 f"{float(posted.get('size_frac') or 0.0):.0%} "
                 f"{hit + ' hit' if hit else 'unhit'}, ")
    return f"Book: {quote}{action} {size:.0%} -> {pnl:+.0f} USD ({reason})"


def with_trade(outcome: Any, record: dict[str, Any]) -> Any:
    """The outcome (a frozen dataclass with ``details`` and ``feedback``) with
    the record under ``details["trade"]`` and the line on the end of its
    feedback, so a rewriter reads what the forecast was worth as money beside
    what it was worth as skill.

    A rollout is scored before any book exists, so the line cannot be written
    at scoring time; it is appended when a replay attaches its record. An
    outcome that already carries a line - one remembered off a scoreboard
    whose generation wrote its book - gets that line replaced, not a second
    one, because the record being attached is the book it is now in.
    """
    details = {**outcome.details, "trade": dict(record)}
    feedback = outcome.feedback.rstrip()
    if " Book: " in feedback:
        feedback = feedback.rsplit(" Book: ", 1)[0]
    elif feedback.startswith("Book: "):
        feedback = ""
    return replace(outcome, details=details, feedback=f"{feedback} {book_line(record)}".strip())


def _path_summary(path: "tuple[PathBar, ...]", posted: Any) -> dict[str, Any] | None:
    """What the market actually did between the instant and the horizon, and
    whether it reached either side of the quote.

    ``crossed`` is not ``filled``: a cap can cut a side that the tape reached
    down to nothing, and the difference between "nobody traded there" and "we
    could not afford it" is the difference between a bad quote and a full book.
    """
    if not path:
        return {"bars": 0, "high": None, "low": None, "close": None,
                "crossed_bid": False, "crossed_ask": False}
    return {"bars": len(path),
            "high": max(b.high for b in path), "low": min(b.low for b in path),
            "close": path[-1].close,
            "crossed_bid": posted is not None and any(b.low <= posted.bid for b in path),
            "crossed_ask": posted is not None and any(b.high >= posted.ask for b in path)}


def _fill_row(f: Any) -> dict[str, Any]:
    return {"side": f.side, "px": f.px, "qty": f.qty, "notional_usd": f.notional_usd,
            "fees_usd": f.fees_usd, "at": f.at.isoformat(), "bar_ts": f.bar_ts.isoformat(),
            "effect": f.effect}


def _record(res: Any, closed: list[Any], cycle: Cycle, unit: str) -> dict[str, Any]:
    """One cycle, whole: what was quoted, what the path did, what crossed, what
    filled and what it cost. A reader should never have to re-derive a fill."""
    trades = list(res.trades) + closed
    posted = res.quote
    return {"action": res.action, "size": res.size, "source": res.source,
            "quote": None if posted is None else {
                "bid": posted.bid, "ask": posted.ask, "size_frac": posted.size_frac,
                "size_usd": posted.size_usd, "source": posted.source,
                "mid_now": cycle.entry.mid, "unit": unit},
            "fills": [_fill_row(f) for f in res.fills],
            "path_summary": _path_summary(cycle.path, posted),
            "pnl_usd": sum(t.pnl_usd for t in trades),
            "fees_usd": res.fees_usd + sum(t.fees_usd for t in closed),
            "reason": trades[-1].reason if trades else None, "refused": res.refused}


def _credit(record: dict[str, Any], trade: Any) -> None:
    record["pnl_usd"] += trade.pnl_usd
    record["fees_usd"] += trade.fees_usd
    record["reason"] = trade.reason


def apply_cycle(book: Book, cycle: Cycle, tick: float) -> Any:
    """The per-cycle sequence every driver shares: learn the settlement, sweep
    the deadlines, take the entry quote, mark, decide, step. Returns the
    book's StepResult. The round-trip cost the default rule sees is priced at
    the position cap, which is the size the rule will ask for whenever the
    edge is worth having; the quote is posted off the entry's mid, in the
    venue's own unit, whatever the take turns out to be."""
    if cycle.settlement is not None:
        book.settlements[cycle.instrument] = float(cycle.settlement)
    book.expire(cycle.at)
    book.last_quote[cycle.instrument] = cycle.entry
    book.mark(cycle.at)
    open_pos = book.positions.get(cycle.instrument)
    cost = book.costs.round_trip_cost(cycle.entry, book.equity() * MAX_POSITION)
    decision = decide(cycle.output, cycle.delta, cycle.half_width, cost, tick,
                      open_pos.side if open_pos else None,
                      mid_now=cycle.entry.mid, unit=book.costs.unit)
    res = book.step(cycle, decision)
    book.last_at, book.processed = cycle.at, book.processed + 1
    return res


def replay_book(cycles: list[Cycle], spec: TradingSpec, *, book_id: str, harness_fp: str,
                ) -> tuple[Book, dict[str, dict[str, Any]]]:
    """A book after every cycle, and what each cycle did to it by instance id.

    A cycle's ``pnl_usd`` is what was realised on *its* instrument during it -
    a side of its quote closing a position, a close the agent asked for, or
    the deadline settling or forcing one out - and zero while the position
    carries, so the sum over records is the sum over trades. What is still
    open when the cycles run out is wound down at its own deadline, credited
    to the last cycle that chose to carry it.
    """
    cycles = sorted(cycles, key=lambda c: (c.at, c.instrument, c.instance_id))
    book = Book(book_id, spec.topic, harness_fp, spec.costs)
    records: dict[str, dict[str, Any]] = {}
    latest_record: dict[str, dict[str, Any]] = {}
    for c in cycles:
        before = len(book.trades)
        res = apply_cycle(book, c, spec.tick)
        swept = book.trades[before:len(book.trades) - len(res.trades)]
        # The deadline sweep runs at every cycle, whatever instrument the
        # cycle is on. A sweep of this instrument is this cycle's; a sweep of
        # another instrument belongs to the last cycle that chose to carry it,
        # so that the records still sum to the trades.
        closed = [t for t in swept if t.instrument == c.instrument]
        for t in swept:
            if t.instrument != c.instrument and t.instrument in latest_record:
                _credit(latest_record[t.instrument], t)
        records[c.instance_id] = latest_record[c.instrument] = _record(res, closed, c, spec.unit)
    for t in book.wind_down():
        if t.instrument in latest_record:
            _credit(latest_record[t.instrument], t)
    return book, records


__all__ = ["Cycle", "TradingSpec", "PathBar", "cycles_of", "replay_book", "apply_cycle",
           "delta_from_details", "book_line", "with_trade"]
