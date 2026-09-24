"""One harness's simulated million dollars: cash, positions, the marks and the trades.

The benchmark scores forecasts; this is what those forecasts would have been
worth if someone had traded them, with the spread crossed and the fee paid.
The book is deliberately dumb. It never decides anything (``policy.py``
does), never knows what venue it is on (``costs.py`` does), and its one
transition, :meth:`Book.step`, applies a decision to a cycle and returns
what happened. Everything a report needs is on the object afterwards:
``marks`` for the equity curve, ``trades`` for the round trips, ``fills``
for the quotes the market hit, ``refusals`` for the sizes the caps threw
out.

A book trades two ways. Every cycle it *posts* a two-sided quote - a bid, an
ask and a size a side - and the market fills a side when the realised price
path crosses it (:meth:`Book.post` walks the bars); it *may* also take, at
the touch and the taker fee (:meth:`Book.open` / :meth:`Book.close`). A
position carries until the agent closes it or its deadline arrives, when it
settles at the venue's settlement value if one is known and is force-closed
at the last quote otherwise. Nothing closes at a forecast's horizon.

Three caps, all as a share of current equity, so a book that has lost half
its money trades half as big: ten percent in any one instrument, fifty
percent gross across all of them, and nothing under half a percent, because
a hundred-dollar position on a million-dollar book is noise wearing a fee.
A fill that would breach a cap is cut to what the cap leaves, on record.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime
from typing import Any

from .costs import Fill, Quote, VenueCosts, costs_named

START_EQUITY = 1_000_000.0
MAX_POSITION = 0.10      # of equity, one instrument
MAX_GROSS = 0.50         # of equity, all instruments
MIN_SIZE = 0.005         # of equity; below this the open is refused

CLOSE_REASONS = ("agent", "quote", "force_close", "settled", "handover", "cap_refused")


@dataclass(frozen=True)
class PathBar:
    """One bar of the realised price path after a forecast, in the
    instrument's own price space (a YES price, a coin, a share)."""

    ts: datetime
    high: float
    low: float
    close: float

    def to_dict(self) -> dict[str, Any]:
        return {"ts": self.ts.isoformat(), "high": self.high, "low": self.low, "close": self.close}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PathBar":
        ts = d["ts"]
        return cls(ts=ts if isinstance(ts, datetime) else datetime.fromisoformat(str(ts)),
                   high=float(d["high"]), low=float(d["low"]), close=float(d["close"]))


@dataclass(frozen=True)
class Position:
    instrument: str
    side: str                    # "long" | "short"
    qty: float
    entry_px: float              # the venue's own price: YES, NO, coin or share
    notional_usd: float
    fees_usd: float
    opened_at: datetime
    deadline: datetime
    run_id: str = ""
    instance_id: str = ""
    source: str = ""             # "harness" | "default" | "quote": who sized it

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["opened_at"], d["deadline"] = self.opened_at.isoformat(), self.deadline.isoformat()
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Position":
        d = dict(d)
        d["opened_at"] = datetime.fromisoformat(d["opened_at"])
        d["deadline"] = datetime.fromisoformat(d["deadline"])
        return cls(**d)


@dataclass(frozen=True)
class Trade:
    """A closed round trip, or the closed part of one. ``pnl_usd`` is the sum
    of its two cash flows, fees included, so it is the number the cash column
    actually moved by."""

    instrument: str
    side: str
    opened_at: datetime
    closed_at: datetime
    entry_px: float
    exit_px: float
    qty: float
    size_usd: float
    fees_usd: float
    pnl_usd: float
    reason: str
    run_id: str = ""
    instance_id: str = ""
    source: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["opened_at"], d["closed_at"] = self.opened_at.isoformat(), self.closed_at.isoformat()
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Trade":
        d = dict(d)
        d["opened_at"] = datetime.fromisoformat(d["opened_at"])
        d["closed_at"] = datetime.fromisoformat(d["closed_at"])
        return cls(**d)


@dataclass(frozen=True)
class QuoteFill:
    """One side of a posted quote, hit by a bar of the path. ``px`` is the
    posted price (YES, coin or share); ``at`` is when the quote was posted and
    ``bar_ts`` the bar that crossed it; ``effect`` says what it did to the
    position: opened one, extended it, reduced it or closed it."""

    instrument: str
    at: datetime
    bar_ts: datetime
    side: str                    # "bid" | "ask"
    px: float
    qty: float
    notional_usd: float
    fees_usd: float
    effect: str                  # "open" | "extend" | "reduce" | "close"
    run_id: str = ""
    instance_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["at"], d["bar_ts"] = self.at.isoformat(), self.bar_ts.isoformat()
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "QuoteFill":
        d = dict(d)
        d["at"] = datetime.fromisoformat(d["at"])
        d["bar_ts"] = datetime.fromisoformat(d["bar_ts"])
        return cls(**d)


@dataclass(frozen=True)
class Posted:
    """What one posted quote did along the path."""

    bid: float
    ask: float
    size_usd: float
    size_frac: float = 0.0       # the same size as a share of equity when posted
    source: str = ""             # "harness" | "default": whose forecast the quote is
    fills: tuple[QuoteFill, ...] = ()
    trades: tuple[Trade, ...] = ()
    scaled: bool = False         # a cap cut a fill down
    refused: bool = False        # a side that would have filled was refused

    @property
    def hit(self) -> str | None:
        """``bid``, ``ask``, ``both`` or None."""
        sides = {f.side for f in self.fills}
        if not sides:
            return None
        return "both" if len(sides) == 2 else next(iter(sides))

    def to_dict(self) -> dict[str, Any]:
        return {"bid": self.bid, "ask": self.ask, "size_usd": self.size_usd,
                "size_frac": self.size_frac, "source": self.source, "hit": self.hit,
                "fills": len(self.fills), "scaled": self.scaled, "refused": self.refused}


@dataclass(frozen=True)
class Mark:
    at: datetime
    equity_usd: float
    cash_usd: float
    gross_exposure_usd: float
    open_positions: int
    drawdown: float              # (peak - equity) / peak, at this mark
    event: str | None = None     # "handover", "settled", ... or None for a plain cycle

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["at"] = self.at.isoformat()
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Mark":
        d = dict(d)
        d["at"] = datetime.fromisoformat(d["at"])
        return cls(**d)


@dataclass(frozen=True)
class StepResult:
    action: str
    size: float
    source: str
    fill: Fill | None = None
    trades: tuple[Trade, ...] = ()
    refused: bool = False
    quote: Posted | None = None

    @property
    def fills(self) -> tuple[QuoteFill, ...]:
        return self.quote.fills if self.quote else ()

    @property
    def pnl_usd(self) -> float:
        return sum(t.pnl_usd for t in self.trades)

    @property
    def fees_usd(self) -> float:
        """Every fee this cycle paid: the closed trades', the take's entry, and
        the quote fills that opened or extended (a reducing fill's fee is in
        its trade)."""
        return (sum(t.fees_usd for t in self.trades) + (self.fill.fees_usd if self.fill else 0.0)
                + sum(f.fees_usd for f in self.fills if f.effect in ("open", "extend")))


def _iso(at: datetime | None) -> str | None:
    return at.isoformat() if at else None


class Book:
    def __init__(self, book_id: str, topic: str, harness_fp: str, costs: VenueCosts, *,
                 start: float = START_EQUITY) -> None:
        self.book_id, self.topic, self.harness_fp, self.costs = book_id, topic, harness_fp, costs
        self.start = start
        self.cash = start
        self.positions: dict[str, Position] = {}
        self.trades: list[Trade] = []
        self.marks: list[Mark] = []
        self.fills: list[QuoteFill] = []
        self.quotes_posted = 0
        self.last_quote: dict[str, Quote] = {}
        #: The settlement value an instrument is known to pay (a YES contract's
        #: 0 or 1); a position in it settles there at the deadline, fee free.
        self.settlements: dict[str, float] = {}
        self.peak = start
        self.handovers: list[dict[str, Any]] = []
        self.refusals: list[dict[str, Any]] = []
        # Live bookkeeping: the last cycle applied, how many, and whose book
        # this is right now. Replay sets them too; it costs nothing.
        self.last_at: datetime | None = None
        self.processed = 0
        self.harness_name = ""

    # -- reading ---------------------------------------------------------------

    def _mid(self, instrument: str, position: Position) -> float:
        q = self.last_quote.get(instrument)
        return q.mid if q is not None else position.entry_px

    def equity(self) -> float:
        return self.cash + sum(self.costs.value(p, self._mid(i, p)) for i, p in self.positions.items())

    def gross(self) -> float:
        return sum(abs(self.costs.exposure(p, self._mid(i, p))) for i, p in self.positions.items())

    def mark(self, at: datetime, event: str | None = None) -> Mark:
        eq = self.equity()
        self.peak = max(self.peak, eq)
        m = Mark(at=at, equity_usd=eq, cash_usd=self.cash, gross_exposure_usd=self.gross(),
                 open_positions=len(self.positions),
                 drawdown=(self.peak - eq) / self.peak if self.peak > 0 else 0.0, event=event)
        self.marks.append(m)
        return m

    # -- taking ----------------------------------------------------------------

    def _refuse(self, at: datetime, instrument: str, side: str, wanted: float, room: float,
                reason: str, kind: str) -> None:
        self.refusals.append({"at": at.isoformat(), "instrument": instrument, "side": side,
                              "wanted_usd": round(wanted, 2), "room_usd": round(room, 2),
                              "reason": reason, "kind": kind})

    def open(self, instrument: str, side: str, size_frac: float, quote: Quote, at: datetime,
             deadline: datetime, *, run_id: str = "", instance_id: str = "", source: str = "",
             ) -> Fill | None:
        """A fill at the touch, or None with the refusal on record. Sized off
        equity as it is now, then cut to what the gross cap leaves; one
        position per instrument, so an open against an open closes the old
        one first."""
        if instrument in self.positions:
            self.close(instrument, quote, at, "agent")
        self.last_quote[instrument] = quote
        eq = self.equity()
        wanted = min(max(0.0, size_frac), MAX_POSITION) * eq
        room = MAX_GROSS * eq - self.gross()
        size_usd = min(wanted, room)
        if size_usd < MIN_SIZE * eq:
            self._refuse(at, instrument, side, wanted, room,
                         "below MIN_SIZE" if wanted >= MIN_SIZE * eq else "dust", "take")
            return None
        fill = self.costs.fill(side, size_usd, quote)
        if fill.qty <= 0:
            self._refuse(at, instrument, side, wanted, room, "no fill", "take")
            return None
        self.cash += self.costs.cash_flow_open(fill, side)
        self.positions[instrument] = Position(
            instrument=instrument, side=side, qty=fill.qty, entry_px=fill.px,
            notional_usd=fill.notional_usd, fees_usd=fill.fees_usd, opened_at=at,
            deadline=deadline, run_id=run_id, instance_id=instance_id, source=source)
        return fill

    def _reduce(self, instrument: str, qty: float, exit_: Fill, at: datetime, reason: str) -> Trade:
        """``qty`` of the position out at ``exit_``: the whole of it, or a part
        that leaves the rest at the same entry. The P&L is the entry's cash
        flow for that part plus the exit's."""
        if reason not in CLOSE_REASONS:
            raise ValueError(f"unknown close reason {reason!r}")
        pos = self.positions[instrument]
        qty = min(qty, pos.qty)
        frac = qty / pos.qty if pos.qty else 1.0
        entry = Fill(px=pos.entry_px, qty=qty, notional_usd=pos.notional_usd * frac,
                     fees_usd=pos.fees_usd * frac)
        flow_in = self.costs.cash_flow_open(entry, pos.side)
        flow_out = self.costs.cash_flow_close(exit_, pos.side)
        self.cash += flow_out
        trade = Trade(instrument=instrument, side=pos.side, opened_at=pos.opened_at, closed_at=at,
                      entry_px=pos.entry_px, exit_px=exit_.px, qty=qty, size_usd=entry.notional_usd,
                      fees_usd=entry.fees_usd + exit_.fees_usd, pnl_usd=flow_in + flow_out, reason=reason,
                      run_id=pos.run_id, instance_id=pos.instance_id, source=pos.source)
        if qty >= pos.qty - 1e-12:
            self.positions.pop(instrument)
        else:
            self.positions[instrument] = replace(
                pos, qty=pos.qty - qty, notional_usd=pos.notional_usd - entry.notional_usd,
                fees_usd=pos.fees_usd - entry.fees_usd)
        self.trades.append(trade)
        return trade

    def close(self, instrument: str, quote: Quote, at: datetime, reason: str) -> Trade:
        """The whole position out at the touch of ``quote`` (fee free when
        ``reason`` is ``settled``, the quote then being the settlement)."""
        if reason not in CLOSE_REASONS:
            raise ValueError(f"unknown close reason {reason!r}")
        pos = self.positions[instrument]
        self.last_quote[instrument] = quote
        exit_ = self.costs.fill(pos.side, pos.notional_usd, quote,
                                closing="settled" if reason == "settled" else True, qty=pos.qty)
        return self._reduce(instrument, pos.qty, exit_, at, reason)

    def _out(self, instrument: str, at: datetime) -> Trade:
        """A position at its deadline: settled where the settlement is known,
        force-closed at the last quote seen otherwise."""
        settled = self.settlements.get(instrument)
        if settled is not None:
            return self.close(instrument, Quote(at=at, bid=settled, ask=settled, mid=settled), at, "settled")
        return self.close(instrument, self.last_quote[instrument], at, "force_close")

    def expire(self, at: datetime) -> list[Trade]:
        """Every position past its deadline, out. Sorted by instrument so two
        books fed the same cycles agree."""
        out = []
        for instrument in sorted(self.positions):
            if self.positions[instrument].deadline <= at:
                out.append(self._out(instrument, at))
        return out

    def wind_down(self) -> list[Trade]:
        """Every open position out at its own deadline, in deadline order, with
        a mark at each: the end of a replay, whose last cycle is not the end
        of the positions it left."""
        out = []
        for instrument in sorted(self.positions, key=lambda i: (self.positions[i].deadline, i)):
            when = self.positions[instrument].deadline
            if self.last_at is not None and when < self.last_at:
                when = self.last_at
            out.append(self._out(instrument, when))
            self.mark(when, event=out[-1].reason)
        return out

    # -- quoting ---------------------------------------------------------------

    def _rest(self, instrument: str, side: str, px: float, size_usd: float, at: datetime,
              deadline: datetime, *, run_id: str, instance_id: str, posted_at: datetime,
              ) -> tuple[QuoteFill | None, bool, bool]:
        """One side of a quote hit at ``at``, the bar that crossed it, having
        rested since ``posted_at``. Against a position on the other side it
        reduces it, up to the whole; otherwise it opens or extends, cut to
        the caps. Returns ``(fill, scaled, refused)``."""
        want = "long" if side == "bid" else "short"
        pos = self.positions.get(instrument)
        if pos is not None and pos.side != want:
            probe = self.costs.rest(pos.side, px, size_usd, closing=True)
            qty = min(probe.qty, pos.qty)
            if qty <= 0:
                return None, False, False
            exit_ = self.costs.rest(pos.side, px, 0.0, closing=True, qty=qty)
            effect = "close" if qty >= pos.qty - 1e-12 else "reduce"
            self._reduce(instrument, qty, exit_, at, "quote")
            fill = QuoteFill(instrument=instrument, at=posted_at, bar_ts=at, side=side, px=px,
                             qty=qty, notional_usd=exit_.notional_usd, fees_usd=exit_.fees_usd,
                             effect=effect, run_id=run_id, instance_id=instance_id)
            self.fills.append(fill)
            return fill, False, False
        eq = self.equity()
        held = abs(self.costs.exposure(pos, self._mid(instrument, pos))) if pos is not None else 0.0
        room = min(MAX_POSITION * eq - held, MAX_GROSS * eq - self.gross())
        size = min(size_usd, room)
        if size < MIN_SIZE * eq:
            self._refuse(at, instrument, want, size_usd, room,
                         "below MIN_SIZE" if size_usd >= MIN_SIZE * eq else "dust", "quote")
            return None, False, True
        scaled = size < size_usd - 1e-9
        if scaled:
            self._refuse(at, instrument, want, size_usd, room, "cap_scaled", "quote")
        fill = self.costs.rest(want, px, size)
        if fill.qty <= 0:
            self._refuse(at, instrument, want, size_usd, room, "no fill", "quote")
            return None, scaled, True
        self.cash += self.costs.cash_flow_open(fill, want)
        if pos is None:
            effect = "open"
            self.positions[instrument] = Position(
                instrument=instrument, side=want, qty=fill.qty, entry_px=fill.px,
                notional_usd=fill.notional_usd, fees_usd=fill.fees_usd, opened_at=at,
                deadline=deadline, run_id=run_id, instance_id=instance_id, source="quote")
        else:
            effect = "extend"
            qty = pos.qty + fill.qty
            self.positions[instrument] = replace(
                pos, qty=qty, entry_px=(pos.qty * pos.entry_px + fill.qty * fill.px) / qty,
                notional_usd=pos.notional_usd + fill.notional_usd, fees_usd=pos.fees_usd + fill.fees_usd,
                deadline=max(pos.deadline, deadline))
        out = QuoteFill(instrument=instrument, at=posted_at, bar_ts=at, side=side, px=px,
                        qty=fill.qty, notional_usd=fill.notional_usd, fees_usd=fill.fees_usd,
                        effect=effect, run_id=run_id, instance_id=instance_id)
        self.fills.append(out)
        return out, scaled, False

    def post(self, instrument: str, bid: float, ask: float, size_usd: float, path: Any,
             at: datetime, horizon_at: datetime, deadline: datetime, *, run_id: str = "",
             instance_id: str = "", size_frac: float = 0.0, source: str = "") -> Posted:
        """A two-sided quote of ``size_usd`` a side, rested from ``at`` and
        filled by the bars of ``path`` in order: the bid at the first bar
        whose low reaches it, the ask at the first whose high does, each side
        at most once. Within one bar the side nearer the last price seen is
        hit first. Bars past the deadline do not fill - the position must be
        out by then. Both sides in one cycle is a round trip inside the
        window, ``(ask - bid) * qty`` less two maker fees."""
        if not ask > bid:
            raise ValueError(f"a quote must be two-sided with ask > bid, not {bid}/{ask}")
        self.quotes_posted += 1
        before = len(self.trades)
        fills: list[QuoteFill] = []
        hit = {"bid": False, "ask": False}
        scaled = refused = False
        last = self.last_quote.get(instrument)
        ref = last.mid if last is not None else (bid + ask) / 2
        for bar in path or ():
            if bar.ts <= at or bar.ts > deadline:
                continue
            order = ("bid", "ask") if abs(ref - bid) <= abs(ask - ref) else ("ask", "bid")
            for side in order:
                if hit[side]:
                    continue
                if not (bar.low <= bid if side == "bid" else bar.high >= ask):
                    continue
                hit[side] = True
                fill, s, r = self._rest(instrument, side, bid if side == "bid" else ask, size_usd,
                                        bar.ts, deadline, run_id=run_id, instance_id=instance_id,
                                        posted_at=at)
                scaled, refused = scaled or s, refused or r
                if fill is not None:
                    fills.append(fill)
            ref = bar.close
            if hit["bid"] and hit["ask"]:
                break
        return Posted(bid=bid, ask=ask, size_usd=size_usd, size_frac=size_frac, source=source,
                      fills=tuple(fills), trades=tuple(self.trades[before:]), scaled=scaled,
                      refused=refused)

    # -- the transition ----------------------------------------------------------

    def step(self, cycle: Any, decision: Any) -> StepResult:
        """Apply one decision to one cycle: the take at the touch of
        ``cycle.entry``, then the posted quote along ``cycle.path``. ``cycle``
        carries ``at``, ``instrument``, ``entry`` (a Quote), ``path``,
        ``horizon_at``, ``deadline`` and the ids; ``decision`` carries
        ``action``, ``size``, ``source`` and ``quote``.

        The take goes first because it happens first: it crosses at ``at``,
        while the quote rests from ``at`` and is filled by bars that end after
        it. Running the path first and then crossing at ``at`` would close a
        position before it was opened.
        """
        before = len(self.trades)
        fill, refused = None, False
        action = decision.action
        run_id, instance_id = getattr(cycle, "run_id", ""), getattr(cycle, "instance_id", "")
        if action in ("open_long", "open_short"):
            fill = self.open(cycle.instrument, "long" if action == "open_long" else "short",
                             decision.size, cycle.entry, cycle.at, cycle.deadline,
                             run_id=run_id, instance_id=instance_id, source=decision.source)
            refused = fill is None
        elif action == "close":
            if cycle.instrument in self.positions:
                self.close(cycle.instrument, cycle.entry, cycle.at, "agent")
        elif action != "hold":
            raise ValueError(f"unknown action {action!r}")
        posted = None
        quote = getattr(decision, "quote", None)
        if quote is not None:
            posted = self.post(cycle.instrument, quote.bid, quote.ask, quote.size * self.equity(),
                               getattr(cycle, "path", ()), cycle.at, cycle.horizon_at, cycle.deadline,
                               run_id=run_id, instance_id=instance_id, size_frac=quote.size,
                               source=getattr(decision, "quote_source", ""))
        return StepResult(action=action, size=decision.size, source=decision.source, fill=fill,
                          trades=tuple(self.trades[before:]), refused=refused, quote=posted)

    # -- persistence -----------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {"book_id": self.book_id, "topic": self.topic, "harness_fp": self.harness_fp,
                "costs": self.costs.name, "start": self.start, "cash": self.cash, "peak": self.peak,
                "positions": {k: self.positions[k].to_dict() for k in sorted(self.positions)},
                "trades": [t.to_dict() for t in self.trades],
                "marks": [m.to_dict() for m in self.marks],
                "fills": [f.to_dict() for f in self.fills],
                "quotes_posted": self.quotes_posted,
                "last_quote": {k: self.last_quote[k].to_dict() for k in sorted(self.last_quote)},
                "settlements": {k: self.settlements[k] for k in sorted(self.settlements)},
                "handovers": list(self.handovers), "refusals": list(self.refusals),
                "last_at": _iso(self.last_at), "processed": self.processed,
                "harness_name": self.harness_name}

    @classmethod
    def from_dict(cls, d: dict[str, Any], costs: VenueCosts | None = None) -> "Book":
        book = cls(d["book_id"], d["topic"], d["harness_fp"], costs or costs_named(d["costs"]),
                   start=d.get("start", START_EQUITY))
        book.cash, book.peak = d["cash"], d["peak"]
        book.positions = {k: Position.from_dict(v) for k, v in d["positions"].items()}
        book.trades = [Trade.from_dict(t) for t in d["trades"]]
        book.marks = [Mark.from_dict(m) for m in d["marks"]]
        book.fills = [QuoteFill.from_dict(f) for f in d.get("fills", ())]
        book.quotes_posted = int(d.get("quotes_posted", 0))
        book.last_quote = {k: Quote.from_dict(v) for k, v in d["last_quote"].items()}
        book.settlements = {k: float(v) for k, v in (d.get("settlements") or {}).items()}
        book.handovers, book.refusals = list(d.get("handovers", ())), list(d.get("refusals", ()))
        book.last_at = datetime.fromisoformat(d["last_at"]) if d.get("last_at") else None
        book.processed = int(d.get("processed", 0))
        book.harness_name = d.get("harness_name", "")
        return book


__all__ = ["Book", "Position", "Trade", "Mark", "PathBar", "QuoteFill", "Posted", "StepResult",
           "START_EQUITY", "MAX_POSITION", "MAX_GROSS", "MIN_SIZE", "CLOSE_REASONS"]
