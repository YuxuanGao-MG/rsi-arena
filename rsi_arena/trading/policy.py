"""From a forecast to an order: the quote every cycle must post, and the take
the harness may add on top.

The forecast contract already asks for a move and a width, and those two
numbers *are* a market: a harness that says "up four cents, half a cent
either side" has said it would buy at three and a half and sell at four and
a half. So the quote is not an extra thing to ask a harness for - it is the
forecast read in the venue's own price space, and :func:`read_quote` does
that conversion exactly the way ``Metric.price_from`` does it (cents on a
price that is a dollar fraction, basis points on a price that is a level).
Every cycle posts one, whether the harness volunteered anything or not, at a
default two percent of equity a side; the market fills a side when the
realised path crosses it, and pays the maker fee rather than the spread.

Taking is the optional half. A harness that has an opinion about direction
and size may say so in ``action`` and ``size``; most do not, and the house
rule below turns its delta and width into a take the way a careful person
would: no trade unless the move clears the round trip and the width with
something left over, and a quarter-Kelly size on what is left, because the
width is a confidence interval and not a variance and full Kelly on a guess
is a way to go broke correctly. The rule closes on a reversal it would have
traded, and holds through noise, so a book is not churned by a forecast
wobbling around zero.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, NamedTuple

from .book import MAX_POSITION, MIN_SIZE

ACTIONS = ("open_long", "open_short", "close", "hold")
KELLY_FRACTION = 0.25

#: What a side of the quote is worth when nobody said: two percent of equity,
#: four times the dust floor and a fifth of the position cap, so a harness
#: that only forecasts still leaves a real order on the book.
DEFAULT_QUOTE_SIZE = 0.02

#: The two output fields a forecast arrives in, per unit. The same names
#: ``Metric.output_keys`` carries; copied rather than imported, because this
#: package must not import a topic.
QUOTE_KEYS: dict[str, tuple[str, str]] = {
    "cents": ("delta_cents", "half_width_cents"),
    "bps": ("delta_bps", "half_width_bps"),
}

TRADING_CONTRACT = (
    "Every cycle your forecast is posted as a two-sided quote on the instrument, and the "
    "quote IS the forecast: the market you named, bid at (predicted - half width) and ask at "
    "(predicted + half width), converted into the venue's prices. A half width under one tick "
    "is widened to a tick, so the two sides are never the same price. The size on each side is "
    "`quote_size` if you give one, else `size`, else 0.02 of equity, capped at 0.10. The market "
    "fills a side when the realised price path crosses it - the bid at the first bar whose low "
    "reaches it, the ask at the first bar whose high does, each at most once a cycle - and a "
    "resting fill pays the maker fee, not the spread: a quarter of the taker curve on Kalshi, "
    "two basis points on a perpetual, nothing on an equity beyond the SEC and TAF charges every "
    "sale pays. Both sides filling in one cycle is a round trip worth (ask - bid) a contract. "
    "A quote narrower than the market is how you get filled and how you get run over; a wide one "
    "rarely trades. On Kalshi a filled bid is long YES at the bid and a filled ask is long NO at "
    "one minus the ask. "
    "Nothing rewards trading for its own sake. A quote wide enough that the path never reaches "
    "it scores exactly what standing aside scores, and standing aside is the right answer "
    "whenever you cannot name a width the market will not run through: the book is charged for "
    "the fills it takes, never for the fills it misses. Half width is the number that decides "
    "this, and it is yours to choose - it is not a by-product of the forecast's dispersion "
    "unless you make it one. "
    "Two further optional fields add a *take*, at the touch and the taker fee. `action` is one of "
    "open_long, open_short, close, hold; `size` is a fraction of equity from 0 to 0.10 (anything "
    "larger is clamped, the gross cap is 0.50 across all instruments, and an order under 0.005 of "
    "equity is refused). A harness that omits both takes on the default rule: edge = |delta| - "
    "(round-trip cost + half width); no take unless edge is positive; size = min(0.10, 0.25 * edge "
    "/ max(half_width, tick)); an open position is closed when the forecast reverses by more than "
    "half the round-trip cost and held otherwise. "
    "Positions carry across cycles. Nothing closes at the forecast's horizon: a position stays on "
    "until you close it or its deadline arrives, and at the deadline a resolved Kalshi market "
    "settles at 0 or 1 and every other venue force-closes at the last mid. Every book starts at "
    "$1,000,000 and is marked each cycle."
)

#: Two sentences kept verbatim in ``Plan.GRAMMAR`` (rsi_arena/harness/spec.py);
#: the test that they match is what keeps a rewriter's grammar and this engine
#: in step.
GRAMMAR_NOTE = (
    "A decisions plan always quotes - the move and half width it forecasts are posted as a "
    "two-sided market every cycle - and may also take: a choice question over open_long, "
    "open_short, close, hold mapped with {\"as\": \"choice\"} under the output field \"action\", "
    "and a score question with \"values\" between 0 and 0.10 mapped with {\"as\": \"mean\"} under "
    "\"size\", set the paper book's take and the size of each side of the quote. Omitting both "
    "fields is allowed: the quote is still posted, two percent a side, and the take follows the "
    "default rule."
)


class Quoted(NamedTuple):
    """The two-sided market a cycle posts: prices in the venue's own space, and
    the size of each side as a fraction of equity."""

    bid: float
    ask: float
    size: float


@dataclass(frozen=True)
class Decision:
    action: str
    size: float
    source: str          # "harness" | "default"
    edge: float | None = None
    quote: Quoted | None = None
    #: "harness" when the quote came off the output's own move and width,
    #: "default" when it came off the forecast the outcome was scored on.
    quote_source: str = ""


def _number(x: Any) -> float | None:
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    return float(x)


def read_decision(output: Any) -> Decision | None:
    """The harness's own take, or None when it did not give a usable one.

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


# -- the quote -----------------------------------------------------------------

def _relative(unit: str, relative: bool | None) -> bool:
    return (unit != "cents") if relative is None else bool(relative)


def _price(mid_now: float, delta: float, relative: bool) -> float:
    """A move of ``delta`` in the unit, as a price. ``Metric.price_from``."""
    return mid_now * (1.0 + delta / 1e4) if relative else mid_now + delta / 100.0


def _span(mid_now: float, width: float, relative: bool) -> float:
    """A width in the unit, as a distance in price space."""
    return mid_now * width / 1e4 if relative else width / 100.0


def quote_size_of(output: Any) -> float:
    """``quote_size``, else ``size``, else the default. A harness that answers
    zero has quoted nothing, which is its right; only a missing number falls
    through to the default."""
    if isinstance(output, dict):
        for key in ("quote_size", "size"):
            value = _number(output.get(key))
            if value is not None:
                return value
    return DEFAULT_QUOTE_SIZE


def quote_from(delta: float, half_width: float, *, mid_now: float, unit: str, tick: float,
               relative: bool | None = None, size: float = DEFAULT_QUOTE_SIZE) -> Quoted | None:
    """``predicted +/- half_width`` in the venue's prices, one tick wide at the
    narrowest. None when there is no mid to anchor on."""
    mid = float(mid_now)
    if not mid:
        return None
    rel = _relative(unit, relative)
    predicted = _price(mid, float(delta), rel)
    half = max(_span(mid, abs(float(half_width)), rel), _span(mid, abs(float(tick)), rel))
    if half <= 0:
        return None
    return Quoted(bid=predicted - half, ask=predicted + half,
                  size=min(max(float(size), 0.0), MAX_POSITION))


def read_quote(output: Any, mid_now: float, unit: str, tick: float,
               relative: bool | None = None) -> Quoted | None:
    """The quote a harness's own output names, or None when it named no move.

    The forecast is the quote, so this reads the same two fields the score
    reads and converts them the way the metric does. A width under the venue's
    tick is widened to it: a market with both sides at one price is not a
    market, and a zero-width quote would be filled on both sides by any bar
    that touched it.
    """
    keys = QUOTE_KEYS.get(unit)
    if keys is None or not isinstance(output, dict):
        return None
    delta = _number(output.get(keys[0]))
    if delta is None:
        return None
    half = _number(output.get(keys[1])) or 0.0
    return quote_from(delta, half, mid_now=mid_now, unit=unit, tick=tick, relative=relative,
                      size=quote_size_of(output))


# -- the take ------------------------------------------------------------------

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
           open_side: str | None, *, mid_now: float | None = None, unit: str = "cents",
           relative: bool | None = None) -> Decision:
    """The cycle's order: the quote it posts and the take it may also cross.

    Without a ``mid_now`` there is no price space to quote in and only the take
    comes back, which is what a caller that just wants the take rule asks for.
    """
    take = read_decision(output)
    if take is None:
        take = default_decision(delta=delta, half_width=half_width,
                                round_trip_cost=round_trip_cost, tick=tick, open_side=open_side)
    if mid_now is None:
        return take
    quote, source = read_quote(output, mid_now, unit, tick, relative), "harness"
    if quote is None:
        # Nothing usable in the output - a remembered rollout has no output at
        # all - so the quote comes off the forecast the outcome was scored on.
        quote, source = quote_from(delta, half_width, mid_now=mid_now, unit=unit, tick=tick,
                                   relative=relative, size=quote_size_of(output)), "default"
    if quote is None:
        return take
    return replace(take, quote=quote, quote_source=source)


__all__ = ["ACTIONS", "KELLY_FRACTION", "DEFAULT_QUOTE_SIZE", "QUOTE_KEYS", "TRADING_CONTRACT",
           "GRAMMAR_NOTE", "Decision", "Quoted", "read_decision", "read_quote", "quote_from",
           "quote_size_of", "default_decision", "decide"]
