"""How a crypto window becomes a paper trade on the perpetual.

The replay has bars and no book, so every quote here is a proxy: the last
close with a two-basis-point spread around it, at the instant and again at
the horizon. A live row carries the touch and sometimes a ladder, and the
engine's ``rows_to_cycles`` reads those itself. A position is out an hour
after it was asked about: a one-minute forecast that is still open at the
sixtieth minute is a position nobody is watching.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from ...trading import PerpCosts, Quote, TradingSpec, delta_from_details

#: One mark a minute, around the clock.
CYCLES_PER_YEAR = 525_600
HORIZON = timedelta(minutes=1)
DEADLINE = timedelta(minutes=60)


def trading_spec(task: Any | None = None) -> TradingSpec:
    costs = PerpCosts()
    horizon = timedelta(minutes=int(getattr(task, "horizon", 1) or 1)) if task is not None else HORIZON

    def quote_of(window: Any) -> tuple[Quote, Quote]:
        return (costs.proxy_quote(window.at, float(window.mid_now)),
                costs.proxy_quote(window.at + horizon, float(window.realised)))

    return TradingSpec(costs=costs, unit="bps", tick=2.0, cycles_per_year=CYCLES_PER_YEAR,
                       quote_of=quote_of, deadline_of=lambda w: w.at + DEADLINE,
                       instrument_of=lambda w: w.symbol,
                       delta_of=lambda o: delta_from_details(o.details, "bps"),
                       topic="crypto-horizon-1m", horizon=horizon,
                       path_of=lambda w: getattr(w, "path", ()) or (),
                       settlement_of=lambda w: getattr(w, "settlement", None))


__all__ = ["trading_spec", "CYCLES_PER_YEAR", "HORIZON", "DEADLINE"]
