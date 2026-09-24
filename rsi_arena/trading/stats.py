"""The numbers a book is judged by, computed from its marks and trades alone.

Per-cycle Sharpe is annualised on the topic's own cycle count, which is why
the count is a table here: a five-minute Kalshi book prints 105,120 marks a
year, a one-minute crypto book 525,600, and an equity book only during the
session (78 five-minute bars a day over 252 days). A daily Sharpe is beside
it because per-cycle returns on a book that holds for one cycle are mostly
zeros and a few spikes, and the annualised number from that can flatter or
damn a book on a handful of trades; bucketing by UTC day is the coarser
read a person would trust first.
"""

from __future__ import annotations

import math
import statistics
from datetime import timezone
from typing import Any

from .book import START_EQUITY, Mark, Trade

CYCLES_PER_YEAR = {"kalshi-horizon-5m": 105_120,
                   "crypto-horizon-1m": 525_600,
                   "news-equity-5m": 19_656}

#: One percent of the starting book. A cycle that makes this much is a full
#: point on the search's frontier and a cycle that loses it is zero, which is
#: how ``value`` scales skill (ten cents of edge a full point): small enough
#: that a real trade registers, large enough that the fee model's rounding
#: does not.
PNL_SCALE_USD = START_EQUITY / 100


def pnl_objective(pnl_usd: float) -> float:
    """A cycle's realised P&L after fees as a [0, 1] objective, 0.5 for no
    trade. Symmetric and clipped: a percent of the book either way is the
    whole range, and nothing a single cycle does can weigh more than that."""
    return min(1.0, max(0.0, 0.5 + float(pnl_usd) / (2 * PNL_SCALE_USD)))


def _sharpe(series: list[float], per_year: float) -> float:
    returns = [b / a - 1.0 for a, b in zip(series, series[1:]) if a > 0]
    if len(returns) < 2:
        return 0.0
    sd = statistics.stdev(returns)
    if sd == 0:
        return 0.0
    return statistics.mean(returns) / sd * math.sqrt(per_year)


def _max_drawdown(series: list[float]) -> float:
    peak, worst = -math.inf, 0.0
    for e in series:
        peak = max(peak, e)
        if peak > 0:
            worst = max(worst, (peak - e) / peak)
    return worst


def _daily_closes(marks: list[Mark]) -> list[float]:
    """The last equity seen on each UTC day, in day order. A naive mark is
    taken as UTC, which is what every collector in this repo writes."""
    by_day: dict[str, float] = {}
    for m in marks:
        at = m.at if m.at.tzinfo is not None else m.at.replace(tzinfo=timezone.utc)
        by_day[at.astimezone(timezone.utc).date().isoformat()] = m.equity_usd
    return [by_day[d] for d in sorted(by_day)]


def equity_stats(marks: list[Mark], trades: list[Trade], cycles_per_year: float, *,
                 start: float = START_EQUITY, handovers: int = 0, refusals: int = 0,
                 quotes_posted: int = 0, fills: int = 0) -> dict[str, Any]:
    """Everything a scoreboard shows for one book.

    ``avg_loss`` is the mean of the losing trades' P&L and so is negative;
    ``profit_factor`` is gross wins over gross losses and None when there
    were no losses to divide by, which JSON can carry and infinity cannot.
    ``turnover`` counts each round trip's size twice, once in and once out,
    as a share of the starting equity.

    ``fill_rate`` is sides filled over quotes posted, so it runs from zero to
    two: a book that gets both sides of every quote hit is a market maker and
    a book at zero is quoting somewhere nobody trades. It is the first number
    to read when a harness makes no money - a wide quote never trades, and a
    narrow one trades every time it is wrong.
    """
    series = [start] + [m.equity_usd for m in marks]
    wins = [t.pnl_usd for t in trades if t.pnl_usd > 0]
    losses = [t.pnl_usd for t in trades if t.pnl_usd < 0]
    gross_win, gross_loss = sum(wins), -sum(losses)
    end = series[-1]
    return {
        "total_return": end / start - 1.0 if start else 0.0,
        "sharpe": _sharpe(series, cycles_per_year),
        "daily_sharpe": _sharpe([start] + _daily_closes(marks), 365),
        "max_drawdown": _max_drawdown(series),
        "hit_rate": len(wins) / len(trades) if trades else 0.0,
        "avg_win": statistics.mean(wins) if wins else 0.0,
        "avg_loss": statistics.mean(losses) if losses else 0.0,
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else None,
        "turnover": 2 * sum(t.size_usd for t in trades) / start if start else 0.0,
        "fees_usd": sum(t.fees_usd for t in trades),
        "trades": len(trades),
        "cycles": len(marks),
        "open_positions": marks[-1].open_positions if marks else 0,
        "start_equity": start,
        "end_equity": end,
        "handovers": handovers,
        "refusals": refusals,
        "quotes_posted": quotes_posted,
        "fills": fills,
        "fill_rate": fills / quotes_posted if quotes_posted else 0.0,
    }


def book_stats(book: Any, cycles_per_year: float) -> dict[str, Any]:
    return equity_stats(book.marks, book.trades, cycles_per_year, start=book.start,
                        handovers=len(book.handovers), refusals=len(book.refusals),
                        quotes_posted=getattr(book, "quotes_posted", 0),
                        fills=len(getattr(book, "fills", ())))


__all__ = ["CYCLES_PER_YEAR", "PNL_SCALE_USD", "pnl_objective", "equity_stats", "book_stats"]
