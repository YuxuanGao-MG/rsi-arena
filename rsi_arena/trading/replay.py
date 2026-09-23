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
and at its horizon, which instrument it is, when a position in it must be
out, and how to read the forecast off an outcome.

The exit rule is the one thing here that is not a plain fill. A forecast is
for one horizon; a position taken on it is closed *at that horizon* unless
the same instrument is asked again soon enough - within one more horizon and
before the deadline - in which case the next forecast decides. That is what
lets a harness ride a trend it keeps calling, and what stops a position from
sitting silently in a market that stopped being asked about.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable

from .book import MAX_POSITION, Book
from .costs import Quote, VenueCosts
from .policy import decide


@dataclass(frozen=True)
class Cycle:
    """One forecast, ready to trade: the quote it was made against, the quote
    printed at its horizon, and what it said."""

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


@dataclass(frozen=True)
class TradingSpec:
    """How a topic's instances become cycles. ``delta_of`` reads
    ``(delta, half_width)`` in ``unit`` from an outcome, or None when it
    said nothing; the other three read an instance."""

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
                         run_id=getattr(run, "run_id", "") or "", output=run.output if run else None,
                         delta=delta, half_width=half, entry=entry, exit=exit_,
                         horizon_at=exit_.at, deadline=spec.deadline_of(inst)))
    return sorted(out, key=lambda c: (c.at, c.instrument, c.instance_id))


def _record(res: Any, closed: list[Any]) -> dict[str, Any]:
    trades = list(res.trades) + closed
    return {"action": res.action, "size": res.size, "source": res.source,
            "pnl_usd": sum(t.pnl_usd for t in trades),
            "fees_usd": sum(t.fees_usd for t in trades) + (res.fill.fees_usd if res.fill else 0.0),
            "reason": trades[-1].reason if trades else None, "refused": res.refused}


def _credit(record: dict[str, Any], trade: Any) -> None:
    record["pnl_usd"] += trade.pnl_usd
    record["fees_usd"] += trade.fees_usd
    record["reason"] = trade.reason


def apply_cycle(book: Book, cycle: Cycle, tick: float) -> Any:
    """The per-cycle sequence every driver shares: expire, quote, mark,
    decide, step. Returns the book's StepResult. The round-trip cost the
    default rule sees is priced at the position cap, which is the size the
    rule will ask for whenever the edge is worth having."""
    book.expire(cycle.at)
    book.last_quote[cycle.instrument] = cycle.entry
    book.mark(cycle.at)
    open_pos = book.positions.get(cycle.instrument)
    cost = book.costs.round_trip_cost(cycle.entry, book.equity() * MAX_POSITION)
    decision = decide(cycle.output, cycle.delta, cycle.half_width, cost, tick,
                      open_pos.side if open_pos else None)
    res = book.step(cycle, decision)
    book.last_at, book.processed = cycle.at, book.processed + 1
    return res


def replay_book(cycles: list[Cycle], spec: TradingSpec, *, book_id: str, harness_fp: str,
                ) -> tuple[Book, dict[str, dict[str, Any]]]:
    """A book after every cycle, and what each cycle did to it by instance id.

    A cycle's ``pnl_usd`` is what was realised on *its* instrument during it -
    a close the agent asked for, a close at the horizon, or a forced close at
    the deadline - and zero while the position carries, so the sum over records
    is the sum over trades.
    """
    cycles = sorted(cycles, key=lambda c: (c.at, c.instrument, c.instance_id))
    next_at: dict[int, datetime | None] = {}
    seen: dict[str, int] = {}
    for i, c in enumerate(cycles):
        if c.instrument in seen:
            next_at[seen[c.instrument]] = c.at
        seen[c.instrument] = i
    book = Book(book_id, spec.topic, harness_fp, spec.costs)
    records: dict[str, dict[str, Any]] = {}
    latest_record: dict[str, dict[str, Any]] = {}
    for i, c in enumerate(cycles):
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
        pos = book.positions.get(c.instrument)
        if pos is not None:
            latest = min(pos.deadline, c.horizon_at + (c.horizon_at - c.at))
            nxt = next_at.get(i)
            if nxt is None or nxt > latest:
                closed.append(book.close(c.instrument, c.exit, c.horizon_at, "horizon"))
                book.mark(c.horizon_at, event="horizon")
        records[c.instance_id] = latest_record[c.instrument] = _record(res, closed)
    return book, records


__all__ = ["Cycle", "TradingSpec", "cycles_of", "replay_book", "apply_cycle", "delta_from_details"]
