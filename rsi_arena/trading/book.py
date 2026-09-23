"""One harness's simulated million dollars: cash, positions, the marks and the trades.

The benchmark scores forecasts; this is what those forecasts would have been
worth if someone had traded them, with the spread crossed and the fee paid.
The book is deliberately dumb. It never decides anything (``policy.py``
does), never knows what venue it is on (``costs.py`` does), and its one
transition, :meth:`Book.step`, applies a decision to a cycle and returns
what happened. Everything a report needs is on the object afterwards:
``marks`` for the equity curve, ``trades`` for the round trips, ``refusals``
for the sizes the caps threw out.

Three caps, all as a share of current equity, so a book that has lost half
its money trades half as big: ten percent in any one instrument, fifty
percent gross across all of them, and nothing under half a percent, because
a hundred-dollar position on a million-dollar book is noise wearing a fee.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from .costs import Fill, Quote, VenueCosts, costs_named

START_EQUITY = 1_000_000.0
MAX_POSITION = 0.10      # of equity, one instrument
MAX_GROSS = 0.50         # of equity, all instruments
MIN_SIZE = 0.005         # of equity; below this the open is refused

CLOSE_REASONS = ("agent", "horizon", "force_close", "settled", "handover", "cap_refused")


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
    source: str = ""             # "harness" | "default": who sized it

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
    """A closed round trip. ``pnl_usd`` is the sum of its two cash flows, fees
    included, so it is the number the cash column actually moved by."""

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
class Mark:
    at: datetime
    equity_usd: float
    cash_usd: float
    gross_exposure_usd: float
    open_positions: int
    drawdown: float              # (peak - equity) / peak, at this mark
    event: str | None = None     # "handover", "horizon", ... or None for a plain cycle

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

    @property
    def pnl_usd(self) -> float:
        return sum(t.pnl_usd for t in self.trades)

    @property
    def fees_usd(self) -> float:
        return sum(t.fees_usd for t in self.trades) + (self.fill.fees_usd if self.fill else 0.0)


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
        self.last_quote: dict[str, Quote] = {}
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

    # -- transitions -----------------------------------------------------------

    def open(self, instrument: str, side: str, size_frac: float, quote: Quote, at: datetime,
             deadline: datetime, *, run_id: str = "", instance_id: str = "", source: str = "",
             ) -> Fill | None:
        """A fill, or None with the refusal on record. Sized off equity as it
        is now, then cut to what the gross cap leaves; one position per
        instrument, so an open against an open closes the old one first."""
        if instrument in self.positions:
            self.close(instrument, quote, at, "agent")
        self.last_quote[instrument] = quote
        eq = self.equity()
        wanted = min(max(0.0, size_frac), MAX_POSITION) * eq
        room = MAX_GROSS * eq - self.gross()
        size_usd = min(wanted, room)
        if size_usd < MIN_SIZE * eq:
            self.refusals.append({"at": at.isoformat(), "instrument": instrument, "side": side,
                                  "wanted_usd": round(wanted, 2), "room_usd": round(room, 2),
                                  "reason": "below MIN_SIZE" if wanted >= MIN_SIZE * eq else "dust"})
            return None
        fill = self.costs.fill(side, size_usd, quote)
        if fill.qty <= 0:
            self.refusals.append({"at": at.isoformat(), "instrument": instrument, "side": side,
                                  "wanted_usd": round(wanted, 2), "room_usd": round(room, 2),
                                  "reason": "no fill"})
            return None
        self.cash += self.costs.cash_flow_open(fill, side)
        self.positions[instrument] = Position(
            instrument=instrument, side=side, qty=fill.qty, entry_px=fill.px,
            notional_usd=fill.notional_usd, fees_usd=fill.fees_usd, opened_at=at,
            deadline=deadline, run_id=run_id, instance_id=instance_id, source=source)
        return fill

    def close(self, instrument: str, quote: Quote, at: datetime, reason: str) -> Trade:
        if reason not in CLOSE_REASONS:
            raise ValueError(f"unknown close reason {reason!r}")
        pos = self.positions.pop(instrument)
        self.last_quote[instrument] = quote
        entry = Fill(px=pos.entry_px, qty=pos.qty, notional_usd=pos.notional_usd, fees_usd=pos.fees_usd)
        exit_ = self.costs.fill(pos.side, pos.notional_usd, quote,
                                closing="settled" if reason == "settled" else True, qty=pos.qty)
        flow_in, flow_out = self.costs.cash_flow_open(entry, pos.side), self.costs.cash_flow_close(exit_, pos.side)
        self.cash += flow_out
        trade = Trade(instrument=instrument, side=pos.side, opened_at=pos.opened_at, closed_at=at,
                      entry_px=pos.entry_px, exit_px=exit_.px, qty=pos.qty, size_usd=pos.notional_usd,
                      fees_usd=pos.fees_usd + exit_.fees_usd, pnl_usd=flow_in + flow_out, reason=reason,
                      run_id=pos.run_id, instance_id=pos.instance_id, source=pos.source)
        self.trades.append(trade)
        return trade

    def expire(self, at: datetime) -> list[Trade]:
        """Every position past its deadline, closed at the last quote seen.
        Sorted by instrument so two books fed the same cycles agree."""
        out = []
        for instrument in sorted(self.positions):
            pos = self.positions[instrument]
            if pos.deadline <= at:
                out.append(self.close(instrument, self.last_quote[instrument], at, "force_close"))
        return out

    def step(self, cycle: Any, decision: Any) -> StepResult:
        """Apply one decision to one cycle. ``cycle`` carries ``at``,
        ``instrument``, ``entry`` (a Quote), ``deadline`` and the ids;
        ``decision`` carries ``action``, ``size`` and ``source``."""
        before = len(self.trades)
        fill, refused = None, False
        action = decision.action
        if action in ("open_long", "open_short"):
            fill = self.open(cycle.instrument, "long" if action == "open_long" else "short",
                             decision.size, cycle.entry, cycle.at, cycle.deadline,
                             run_id=getattr(cycle, "run_id", ""), instance_id=getattr(cycle, "instance_id", ""),
                             source=decision.source)
            refused = fill is None
        elif action == "close":
            if cycle.instrument in self.positions:
                self.close(cycle.instrument, cycle.entry, cycle.at, "agent")
        elif action != "hold":
            raise ValueError(f"unknown action {action!r}")
        return StepResult(action=action, size=decision.size, source=decision.source, fill=fill,
                          trades=tuple(self.trades[before:]), refused=refused)

    # -- persistence -----------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {"book_id": self.book_id, "topic": self.topic, "harness_fp": self.harness_fp,
                "costs": self.costs.name, "start": self.start, "cash": self.cash, "peak": self.peak,
                "positions": {k: self.positions[k].to_dict() for k in sorted(self.positions)},
                "trades": [t.to_dict() for t in self.trades],
                "marks": [m.to_dict() for m in self.marks],
                "last_quote": {k: self.last_quote[k].to_dict() for k in sorted(self.last_quote)},
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
        book.last_quote = {k: Quote.from_dict(v) for k, v in d["last_quote"].items()}
        book.handovers, book.refusals = list(d.get("handovers", ())), list(d.get("refusals", ()))
        book.last_at = datetime.fromisoformat(d["last_at"]) if d.get("last_at") else None
        book.processed = int(d.get("processed", 0))
        book.harness_name = d.get("harness_name", "")
        return book


__all__ = ["Book", "Position", "Trade", "Mark", "StepResult", "START_EQUITY", "MAX_POSITION",
           "MAX_GROSS", "MIN_SIZE", "CLOSE_REASONS"]
