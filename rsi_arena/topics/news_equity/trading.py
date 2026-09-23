"""How a news window becomes a paper trade in the shares.

The bar replay has no book, so the quote is a three-basis-point half spread
around a mid: the bar's vwap where the window kept one (a bar's volume-
weighted price is nearer what a taker paid than its close), the close
otherwise. A position is out at the bell, 16:00 New York on the item's own
date, because an overnight gap is not what a five-minute forecast claimed.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from ...alpaca._session import ny_date, session_close
from ...trading import EquityCosts, Quote, TradingSpec, delta_from_details

#: 78 five-minute bars a session, 252 sessions.
CYCLES_PER_YEAR = 19_656
HORIZON = timedelta(minutes=5)


def _mid(preferred: Any, fallback: float) -> float:
    try:
        v = float(preferred)
        return v if v > 0 else float(fallback)
    except (TypeError, ValueError):
        return float(fallback)


def deadline_of(window: Any) -> datetime:
    """The close of the session the item broke in, never before the horizon."""
    bell = session_close(ny_date(window.at))
    return max(bell, window.at + HORIZON)


def trading_spec(task: Any | None = None) -> TradingSpec:
    costs = EquityCosts()

    def quote_of(window: Any) -> tuple[Quote, Quote]:
        now = _mid(getattr(window, "vwap_now", None), window.mid_now)
        later = _mid(getattr(window, "vwap_h", None), window.realised)
        return costs.proxy_quote(window.at, now), costs.proxy_quote(window.at + HORIZON, later)

    return TradingSpec(costs=costs, unit="bps", tick=5.0, cycles_per_year=CYCLES_PER_YEAR,
                       quote_of=quote_of, deadline_of=deadline_of,
                       instrument_of=lambda w: w.symbol,
                       delta_of=lambda o: delta_from_details(o.details, "bps"),
                       topic="news-equity-5m", horizon=HORIZON)


__all__ = ["trading_spec", "deadline_of", "CYCLES_PER_YEAR", "HORIZON"]
