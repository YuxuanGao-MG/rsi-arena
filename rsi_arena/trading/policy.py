"""From a forecast to an order: the harness's own call when it makes one, and
the house rule when it does not.

The forecast contract asks for a move and a width. Trading it needs two more
things - which way, and how big - and a harness that has an opinion about
those may say so in two optional output fields. Most do not, and a harness
that only forecasts still gets a book, so the default rule below turns its
delta and width into an order the way a careful person would: no trade
unless the move clears the round trip and the width with something left
over, and a quarter-Kelly size on what is left, because the width is a
confidence interval and not a variance and full Kelly on a guess is a way to
go broke correctly. The rule closes on a reversal it would have traded, and
holds through noise, so a book is not churned by a forecast wobbling around
zero.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .book import MAX_POSITION, MIN_SIZE

ACTIONS = ("open_long", "open_short", "close", "hold")
KELLY_FRACTION = 0.25

TRADING_CONTRACT = (
    "Two optional output fields turn a forecast into a paper trade. `action` is one of "
    "open_long, open_short, close, hold; `size` is a fraction of the book's equity from 0 to 0.10 "
    "(anything larger is clamped, the gross cap is 0.50 across all instruments, and an order "
    "under 0.005 of equity is refused). On Kalshi open_long buys YES at the ask and open_short "
    "buys NO at one minus the bid, each paying the taker fee, and a position held to settlement "
    "pays no exit fee; on a crypto perpetual the sides are a long or short in the coin, filled "
    "down the book at five basis points a side; on an equity they are a long or short in shares "
    "at a three-basis-point half spread, with SEC and TAF charges on every sale. One position per "
    "instrument: an open against an open closes the old one first. A harness that omits both "
    "fields trades the default rule: edge = |delta| - (round-trip cost + half width); no trade "
    "unless edge is positive; size = min(0.10, 0.25 * edge / max(half_width, tick)); an open "
    "position is closed when the forecast reverses by more than half the round-trip cost and "
    "held otherwise. Every book starts at $1,000,000, fills cross the spread, and equity is "
    "marked each cycle."
)

#: Two sentences kept verbatim in ``Plan.GRAMMAR`` (rsi_arena/harness/spec.py);
#: the test that they match is what keeps a rewriter's grammar and this engine
#: in step.
GRAMMAR_NOTE = (
    "A decisions plan may also trade: a choice question over open_long, open_short, close, hold "
    "mapped with {\"as\": \"choice\"} under the output field \"action\", and a score question with "
    "\"values\" between 0 and 0.10 mapped with {\"as\": \"mean\"} under \"size\", set the paper "
    "book's order for the cycle. Omitting both fields is allowed and trades the default rule."
)


@dataclass(frozen=True)
class Decision:
    action: str
    size: float
    source: str          # "harness" | "default"
    edge: float | None = None


def _number(x: Any) -> float | None:
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    return float(x)


def read_decision(output: Any) -> Decision | None:
    """The harness's own order, or None when it did not give a usable one.

    Strict on purpose: an action outside the four or a size that is not a
    number falls through to the default rule rather than being repaired,
    because a rewriter that learns "size": "big" works would keep writing it.
    """
    if not isinstance(output, dict):
        return None
    action = output.get("action")
    if action not in ACTIONS:
        return None
    raw = output.get("size", 0.0 if action in ("close", "hold") else None)
    size = _number(raw)
    if size is None:
        return None
    return Decision(action=action, size=min(max(size, 0.0), MAX_POSITION), source="harness")


def default_decision(*, delta: float, half_width: float, round_trip_cost: float, tick: float,
                     open_side: str | None) -> Decision:
    """The house rule, in the forecast's own unit (cents or basis points)."""
    width = max(half_width, 0.0)
    edge = abs(delta) - (round_trip_cost + width)
    want = "long" if delta > 0 else "short" if delta < 0 else None
    if open_side is not None:
        if want is not None and want != open_side and abs(delta) > round_trip_cost / 2:
            return Decision("close", 0.0, "default", edge)
        return Decision("hold", 0.0, "default", edge)
    if want is None or edge <= 0:
        return Decision("hold", 0.0, "default", edge)
    size = min(MAX_POSITION, KELLY_FRACTION * edge / max(width, tick))
    if size < MIN_SIZE:
        return Decision("hold", 0.0, "default", edge)
    return Decision("open_long" if want == "long" else "open_short", size, "default", edge)


def decide(output: Any, delta: float, half_width: float, round_trip_cost: float, tick: float,
           open_side: str | None) -> Decision:
    own = read_decision(output)
    if own is not None:
        return own
    return default_decision(delta=delta, half_width=half_width, round_trip_cost=round_trip_cost,
                            tick=tick, open_side=open_side)


__all__ = ["ACTIONS", "KELLY_FRACTION", "TRADING_CONTRACT", "GRAMMAR_NOTE", "Decision",
           "read_decision", "default_decision", "decide"]
