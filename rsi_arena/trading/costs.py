"""What a fill costs on each venue, behind one interface the book never looks past.

A forecast becomes money only through a fill, and a fill crosses a spread and
pays a fee that is different on every venue: Kalshi charges per contract on a
curve that peaks at fifty cents, a perpetual charges basis points of notional
and walks a ladder, and an equity sale pays two federal taxes that a purchase
does not. The book must not know any of that — it holds cash and positions
and asks a :class:`VenueCosts` what a side is worth and what crossing costs —
so every venue-specific branch lives here and nowhere else.

The three venues are also asymmetric in what "short" means. On Kalshi a short
is a *long NO*: money goes out on entry, the position is worth ``qty * (1 -
mid)``, and its cash flows have the sign of a purchase. On a perpetual or an
equity a short is a sale: cash comes in, the position is worth ``-qty * mid``.
``cash_flow_open`` and ``cash_flow_close`` carry that sign so the book's
equity is ``cash + sum(value)`` on every venue and its round-trip P&L is the
sum of two cash flows, never a per-venue formula.

A resting order is the other way to trade. The book posts a bid and an ask
and the venue fills them when a print goes through, so ``rest`` prices a
fill *at the posted price* - no spread crossed - and charges the maker fee:
a quarter of the taker curve on Kalshi, two basis points on the perpetual,
nothing on an equity (commission free either way; the SEC and TAF charges
on a sale stay). The book walks the price path and asks ``rest`` what each
touch was worth.

Stdlib only. ``rsi_arena.alpaca.replay`` and ``rsi_arena.crypto.replay`` own
the equity taxes and the tick sizes, but importing either package imports its
HTTP client through the package ``__init__``, so the numbers are copied here
with their source named; keep them in step by hand.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from ..kalshi._fees import maker_fee, taker_fee

#: A proxy quote is what the engine invents when a venue printed a mid and no
#: book. Kalshi soccer quotes are typically two cents wide; a large-cap
#: perpetual one or two basis points; a US large cap one to five (the alpaca
#: replay's ``costs`` tool assumes three, and so does :class:`EquityCosts`).
KALSHI_PROXY_SPREAD = 0.02          # dollars, full width
CRYPTO_PROXY_SPREAD_BPS = 2.0       # basis points, full width
EQUITY_HALF_SPREAD_BPS = 3.0        # basis points, each side

#: Copied from ``rsi_arena.alpaca.replay`` (Section 31 and FINRA TAF as
#: published for the fiscal year). Sells only.
SEC_FEE_PER_DOLLAR = 27.80 / 1_000_000
TAF_PER_SHARE, TAF_MAX = 0.000166, 8.30

#: Copied from ``rsi_arena.crypto.replay`` (``TICK_BPS``) and
#: ``rsi_arena.alpaca.replay`` (``TICK_BPS``); the Kalshi tick is one cent.
KALSHI_TICK = 1.0
CRYPTO_TICK_BPS = 2.0
EQUITY_TICK_BPS = 5.0

#: A perpetual's taker fee per side. The spot replay charges Binance spot
#: VIP0 (ten bps); a perpetual on the same exchange is half that, and a
#: paper book that charged spot fees for a perpetual would refuse every
#: one-minute forecast there is.
TAKER_BPS_PER_SIDE = 5.0
#: A perpetual's maker fee per side (Binance USD-M VIP0: 0.02%). A resting
#: quote on the coin pays this, never the spread.
MAKER_BPS_PER_SIDE = 2.0

Levels = tuple[tuple[float, float], ...]


@dataclass(frozen=True)
class Quote:
    """What the venue was showing at ``at``: touch, optional ladder, and whether
    the touch was seen or invented from a mid (``proxy``)."""

    at: datetime
    bid: float
    ask: float
    mid: float
    bids: Levels = ()
    asks: Levels = ()
    proxy: bool = False

    @classmethod
    def from_mid(cls, at: datetime, mid: float, half_spread_abs: float) -> "Quote":
        h = max(0.0, float(half_spread_abs))
        return cls(at=at, bid=mid - h, ask=mid + h, mid=mid, proxy=True)

    @property
    def spread(self) -> float:
        return self.ask - self.bid

    def to_dict(self) -> dict[str, Any]:
        return {"at": self.at.isoformat(), "bid": self.bid, "ask": self.ask, "mid": self.mid,
                "bids": [list(l) for l in self.bids], "asks": [list(l) for l in self.asks],
                "proxy": self.proxy}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Quote":
        return cls(at=datetime.fromisoformat(d["at"]), bid=d["bid"], ask=d["ask"], mid=d["mid"],
                   bids=tuple((float(p), float(q)) for p, q in d.get("bids") or ()),
                   asks=tuple((float(p), float(q)) for p, q in d.get("asks") or ()),
                   proxy=bool(d.get("proxy", False)))


@dataclass(frozen=True)
class Fill:
    """One crossing. ``px`` is the price paid per unit in the venue's own terms
    (a NO price for a Kalshi short), ``notional_usd`` is ``px * qty``, and
    ``partial`` says the ladder ran out before the size was met."""

    px: float
    qty: float
    notional_usd: float
    fees_usd: float
    levels: int = 1
    partial: bool = False


@runtime_checkable
class HasPosition(Protocol):
    """The three fields a venue needs to price an open position; the book's
    ``Position`` has them, and so does anything a test hands in."""

    @property
    def side(self) -> str: ...
    @property
    def qty(self) -> float: ...
    @property
    def entry_px(self) -> float: ...


class VenueCosts(Protocol):
    name: str
    unit: str       # "cents" | "bps": the unit round_trip_cost speaks

    def fill(self, side: str, size_usd: float, quote: Quote, *,
             closing: bool | str = False, qty: float | None = None) -> Fill: ...
    def rest(self, side: str, px: float, size_usd: float, *,
             closing: bool = False, qty: float | None = None) -> Fill: ...
    def round_trip_cost(self, quote: Quote, size_usd: float) -> float: ...
    def exposure(self, position: HasPosition, mid: float) -> float: ...
    def value(self, position: HasPosition, mid: float) -> float: ...
    def cash_flow_open(self, fill: Fill, side: str) -> float: ...
    def cash_flow_close(self, fill: Fill, side: str) -> float: ...
    def proxy_quote(self, at: datetime, mid: float) -> Quote: ...


def _side(side: str) -> str:
    if side not in ("long", "short"):
        raise ValueError(f"side must be long or short, not {side!r}")
    return side


# -- Kalshi -----------------------------------------------------------------

class KalshiCosts:
    """Binary contracts in dollars. Long buys YES at the ask; short buys NO at
    ``1 - bid``, which is the same crossing seen from the other side of the
    book. Every crossing is a taker, capped at 3.5c a contract, and a
    contract held to settlement pays nothing on the way out."""

    name = "kalshi"
    unit = "cents"

    def fill(self, side: str, size_usd: float, quote: Quote, *,
             closing: bool | str = False, qty: float | None = None) -> Fill:
        _side(side)
        if not closing:
            px = quote.ask if side == "long" else 1.0 - quote.bid
        else:
            px = quote.bid if side == "long" else 1.0 - quote.ask
        px = min(max(px, 0.0), 1.0)
        if qty is None:
            qty = float(math.floor(size_usd / px)) if px > 0 else 0.0
        notional = qty * px
        # The fee curve is symmetric in p(1-p), so the YES-equivalent price
        # and the NO price charge the same; settlement is free.
        fees = 0.0 if closing == "settled" else taker_fee(px, qty)
        return Fill(px=px, qty=qty, notional_usd=notional, fees_usd=fees)

    def rest(self, side: str, px: float, size_usd: float, *,
             closing: bool = False, qty: float | None = None) -> Fill:
        """A resting order filled at ``px``, a YES price. A filled bid is a
        long (YES at the bid); a filled ask is a short (NO at one minus the
        ask), and the same two prices take a position of that side off. The
        maker fee is a quarter of the taker curve at the YES price."""
        _side(side)
        px = min(max(px, 0.0), 1.0)
        price = px if side == "long" else 1.0 - px
        if qty is None:
            qty = float(math.floor(size_usd / price)) if price > 0 else 0.0
        notional = qty * price
        return Fill(px=price, qty=qty, notional_usd=notional, fees_usd=maker_fee(px, qty))

    def round_trip_cost(self, quote: Quote, size_usd: float = 0.0) -> float:
        """Cents per contract to get in and out at the touch."""
        return 100.0 * (quote.spread + taker_fee(quote.ask) + taker_fee(quote.bid))

    def exposure(self, position: HasPosition, mid: float) -> float:
        return self.value(position, mid)

    def value(self, position: HasPosition, mid: float) -> float:
        mid = min(max(mid, 0.0), 1.0)
        return position.qty * (mid if position.side == "long" else 1.0 - mid)

    def cash_flow_open(self, fill: Fill, side: str) -> float:
        return -(fill.notional_usd + fill.fees_usd)     # YES or NO, both are purchases

    def cash_flow_close(self, fill: Fill, side: str) -> float:
        return fill.notional_usd - fill.fees_usd

    def proxy_quote(self, at: datetime, mid: float) -> Quote:
        return Quote.from_mid(at, mid, KALSHI_PROXY_SPREAD / 2)


# -- Perpetuals -------------------------------------------------------------

def walk_book(levels: Levels, size_usd: float) -> tuple[float, float, int]:
    """Average price, notional actually filled, and levels consumed, eating a
    ladder of ``(px, qty)`` from the touch outward until ``size_usd`` is met.

    A million-dollar book sizing at ten percent moves a hundred thousand
    dollars, which on a thin altcoin ladder is several levels deep; charging
    the touch for all of it is the kind of optimism a paper book exists to
    remove. Runs out gracefully: the caller reads ``filled < size_usd`` as a
    partial.
    """
    remaining = max(0.0, float(size_usd))
    spent, got, used = 0.0, 0.0, 0
    for px, qty in levels:
        if remaining <= 0 or px <= 0 or qty <= 0:
            break
        take_usd = min(remaining, px * qty)
        spent += take_usd
        got += take_usd / px
        remaining -= take_usd
        used += 1
    if got <= 0:
        return 0.0, 0.0, 0
    return spent / got, spent, used


class PerpCosts:
    """A perpetual: fractional size, a ladder when the collector saw one,
    basis-point taker fees both ways, and a short that is a real sale."""

    name = "perp"
    unit = "bps"
    taker_bps = TAKER_BPS_PER_SIDE
    maker_bps = MAKER_BPS_PER_SIDE

    def fill(self, side: str, size_usd: float, quote: Quote, *,
             closing: bool | str = False, qty: float | None = None) -> Fill:
        _side(side)
        buying = (side == "long") != bool(closing)
        ladder, touch = (quote.asks, quote.ask) if buying else (quote.bids, quote.bid)
        if qty is not None:
            size_usd = qty * touch
        avg, filled, used = walk_book(ladder, size_usd) if ladder else (0.0, 0.0, 0)
        partial = False
        if used == 0:
            avg, filled, used = touch, size_usd, 1
        elif filled < size_usd - 1e-9:
            if qty is not None:
                # A close takes the whole position out, at the last price seen.
                last_px = ladder[used - 1][0]
                rest = size_usd - filled
                avg = size_usd / (filled / avg + rest / last_px)
                filled = size_usd
            else:
                partial = True
        if qty is None:
            qty = filled / avg if avg > 0 else 0.0
        notional = qty * avg
        fees = notional * self.taker_bps / 1e4
        return Fill(px=avg, qty=qty, notional_usd=notional, fees_usd=fees, levels=max(1, used),
                    partial=partial)

    def rest(self, side: str, px: float, size_usd: float, *,
             closing: bool = False, qty: float | None = None) -> Fill:
        """A resting order filled whole at ``px``, the maker fee on the notional."""
        _side(side)
        if qty is None:
            qty = size_usd / px if px > 0 else 0.0
        notional = qty * px
        return Fill(px=px, qty=qty, notional_usd=notional, fees_usd=notional * self.maker_bps / 1e4)

    def round_trip_cost(self, quote: Quote, size_usd: float = 0.0) -> float:
        spread_bps = quote.spread / quote.mid * 1e4 if quote.mid else 0.0
        return spread_bps + 2 * self.taker_bps

    def exposure(self, position: HasPosition, mid: float) -> float:
        return position.qty * mid

    def value(self, position: HasPosition, mid: float) -> float:
        return position.qty * mid * (1.0 if position.side == "long" else -1.0)

    def cash_flow_open(self, fill: Fill, side: str) -> float:
        return (-fill.notional_usd if side == "long" else fill.notional_usd) - fill.fees_usd

    def cash_flow_close(self, fill: Fill, side: str) -> float:
        return (fill.notional_usd if side == "long" else -fill.notional_usd) - fill.fees_usd

    def proxy_quote(self, at: datetime, mid: float) -> Quote:
        return Quote.from_mid(at, mid, mid * CRYPTO_PROXY_SPREAD_BPS / 2e4)


# -- Equities ---------------------------------------------------------------

class EquityCosts:
    """Whole shares against a fixed three-basis-point half spread around the
    caller's mid (a vwap or a close; the bar replay has no book), commission
    free, with the SEC and TAF charges on every sale and nothing on a buy."""

    name = "equity"
    unit = "bps"
    half_spread_bps = EQUITY_HALF_SPREAD_BPS
    maker_bps = 0.0

    def fill(self, side: str, size_usd: float, quote: Quote, *,
             closing: bool | str = False, qty: float | None = None) -> Fill:
        _side(side)
        buying = (side == "long") != bool(closing)
        px = quote.mid * (1.0 + (self.half_spread_bps if buying else -self.half_spread_bps) / 1e4)
        if qty is None:
            qty = float(math.floor(size_usd / px)) if px > 0 else 0.0
        notional = qty * px
        fees = 0.0 if buying else notional * SEC_FEE_PER_DOLLAR + min(qty * TAF_PER_SHARE, TAF_MAX)
        return Fill(px=px, qty=qty, notional_usd=notional, fees_usd=fees)

    def rest(self, side: str, px: float, size_usd: float, *,
             closing: bool = False, qty: float | None = None) -> Fill:
        """Whole shares at the posted price; no commission either way, the
        federal charges on a sale as on any sale."""
        _side(side)
        buying = (side == "long") != bool(closing)
        if qty is None:
            qty = float(math.floor(size_usd / px)) if px > 0 else 0.0
        notional = qty * px
        fees = 0.0 if buying else notional * SEC_FEE_PER_DOLLAR + min(qty * TAF_PER_SHARE, TAF_MAX)
        return Fill(px=px, qty=qty, notional_usd=notional, fees_usd=fees)

    def round_trip_cost(self, quote: Quote, size_usd: float) -> float:
        shares = size_usd / quote.mid if quote.mid else 0.0
        taf_bps = (min(shares * TAF_PER_SHARE, TAF_MAX) / size_usd * 1e4) if size_usd > 0 else 0.0
        return 2 * self.half_spread_bps + SEC_FEE_PER_DOLLAR * 1e4 + taf_bps

    def exposure(self, position: HasPosition, mid: float) -> float:
        return position.qty * mid

    def value(self, position: HasPosition, mid: float) -> float:
        return position.qty * mid * (1.0 if position.side == "long" else -1.0)

    def cash_flow_open(self, fill: Fill, side: str) -> float:
        return (-fill.notional_usd if side == "long" else fill.notional_usd) - fill.fees_usd

    def cash_flow_close(self, fill: Fill, side: str) -> float:
        return (fill.notional_usd if side == "long" else -fill.notional_usd) - fill.fees_usd

    def proxy_quote(self, at: datetime, mid: float) -> Quote:
        return Quote.from_mid(at, mid, mid * self.half_spread_bps / 1e4)


VENUES: dict[str, type] = {"kalshi": KalshiCosts, "perp": PerpCosts, "equity": EquityCosts}
TICKS: dict[str, float] = {"kalshi": KALSHI_TICK, "perp": CRYPTO_TICK_BPS, "equity": EQUITY_TICK_BPS}


def default_tick(costs: VenueCosts) -> float:
    """The smallest move the venue's metric counts, in ``costs.unit``."""
    return TICKS[costs.name]


def costs_named(name: str) -> VenueCosts:
    """The venue a saved book names, so state can be reloaded without the
    caller remembering which engine wrote it."""
    try:
        return VENUES[name]()
    except KeyError:
        raise ValueError(f"unknown venue {name!r}; one of {sorted(VENUES)}") from None


__all__ = ["Quote", "Fill", "VenueCosts", "HasPosition", "KalshiCosts", "PerpCosts", "EquityCosts",
           "walk_book", "costs_named", "default_tick", "VENUES", "TICKS", "KALSHI_PROXY_SPREAD", "CRYPTO_PROXY_SPREAD_BPS",
           "EQUITY_HALF_SPREAD_BPS", "TAKER_BPS_PER_SIDE", "MAKER_BPS_PER_SIDE", "SEC_FEE_PER_DOLLAR",
           "TAF_PER_SHARE",
           "TAF_MAX", "KALSHI_TICK", "CRYPTO_TICK_BPS", "EQUITY_TICK_BPS"]
