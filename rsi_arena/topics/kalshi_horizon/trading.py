"""How a Kalshi window becomes a paper trade: the touch it was quoted at,
the touch at its horizon, and when a position in the contract must be out.

The engine in ``rsi_arena.trading`` knows nothing about football; this is
the whole of what it needs to know. A window built since the touch was kept
carries ``yes_bid``/``yes_ask`` at the instant and ``yes_bid_h``/``yes_ask_h``
at the horizon, and the book crosses those. An older window carries only
mids, and the book crosses the venue's two-cent proxy spread instead,
flagged as such on the quote. The deadline is the final whistle: kickoff
plus a hundred and fifteen minutes when the window knows its kickoff, else
five minutes past the last window the question set has on that contract.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Callable

from ...trading import KalshiCosts, Quote, TradingSpec, delta_from_details

#: Cadence of the marks a year: one every five minutes, around the clock.
CYCLES_PER_YEAR = 105_120
HORIZON = timedelta(minutes=5)
#: Ninety minutes, half-time, stoppage: a position is out before the whistle.
MATCH_MINUTES = 115
#: Past the last window a contract was asked about, without a kickoff to go by.
AFTER_LAST_WINDOW = timedelta(minutes=5)


def _touch(at: datetime, bid: Any, ask: Any) -> Quote | None:
    try:
        b, a = float(bid), float(ask)
    except (TypeError, ValueError):
        return None
    if a < b or not (0.0 <= b <= 1.0 and 0.0 <= a <= 1.0):
        return None
    return Quote(at=at, bid=b, ask=a, mid=(a + b) / 2)


def quote_of(costs: KalshiCosts) -> Callable[[Any], tuple[Quote, Quote]]:
    def read(window: Any) -> tuple[Quote, Quote]:
        at = window.at
        entry = _touch(at, getattr(window, "yes_bid", None), getattr(window, "yes_ask", None))
        if entry is None:
            entry = costs.proxy_quote(at, float(window.mid_now))
        later = at + HORIZON
        exit_ = _touch(later, getattr(window, "yes_bid_h", None), getattr(window, "yes_ask_h", None))
        if exit_ is None:
            exit_ = costs.proxy_quote(later, float(window.realised))
        return entry, exit_
    return read


def _kickoff(window: Any, task: Any) -> datetime | None:
    """The match's kickoff, from the window's own state (a pre-match window
    says it) or from a timeline the task has already built. Never fetched:
    a deadline is not worth a network call per window."""
    game = window.game if isinstance(getattr(window, "game", None), dict) else {}
    raw = game.get("kickoff")
    if raw:
        try:
            return datetime.fromisoformat(str(raw))
        except ValueError:
            pass
    lines = getattr(task, "_timelines", None) or {}
    line = lines.get(game.get("game_id")) if game.get("game_id") else None
    return getattr(line, "kickoff", None)


def deadline_of(task: Any | None) -> Callable[[Any], datetime]:
    last_by_ticker: dict[str, datetime] = {}
    scanned = False

    def read(window: Any) -> datetime:
        nonlocal scanned
        kickoff = _kickoff(window, task)
        if kickoff is not None:
            return max(kickoff + timedelta(minutes=MATCH_MINUTES), window.at + HORIZON)
        if task is not None and not scanned:
            # Once: every window of the question set, so a contract's deadline
            # is the same whichever split asked about it.
            scanned = True
            try:
                for w in task.instances():
                    t = getattr(w, "ticker", None)
                    if t and (t not in last_by_ticker or w.at > last_by_ticker[t]):
                        last_by_ticker[t] = w.at
            except Exception:  # noqa: BLE001 - a task that cannot list is a task without a set
                pass
        last = last_by_ticker.get(window.ticker, window.at)
        return max(last, window.at) + AFTER_LAST_WINDOW
    return read


def trading_spec(task: Any | None = None) -> TradingSpec:
    """The spec, with or without a task. Without one the deadline falls back
    to five minutes past the window, which is what a live row gets anyway
    (``rows_to_cycles`` sets its own)."""
    costs = KalshiCosts()
    return TradingSpec(costs=costs, unit="cents", tick=1.0, cycles_per_year=CYCLES_PER_YEAR,
                       quote_of=quote_of(costs), deadline_of=deadline_of(task),
                       instrument_of=lambda w: w.ticker,
                       delta_of=lambda o: delta_from_details(o.details, "cents"),
                       topic="kalshi-horizon-5m", horizon=HORIZON,
                       path_of=lambda w: getattr(w, "path", ()) or (),
                       settlement_of=lambda w: getattr(w, "settlement", None))


__all__ = ["trading_spec", "quote_of", "deadline_of", "CYCLES_PER_YEAR", "HORIZON", "MATCH_MINUTES"]
