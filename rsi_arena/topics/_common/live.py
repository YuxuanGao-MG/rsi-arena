"""Grade a live forecast once its horizon has printed, whatever the venue.

The replay benchmark scores a harness against a past it cannot see; a live
collector puts the same harness on a market being traded now and scores it a
few minutes later, when the price prints. The part that is the same for every
venue is here: a row is pending until its horizon has passed, is scored
against the price the venue printed then, and is written once. What the venue
is — how a realised price is looked up, how an output is scored — comes in as
two functions.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

#: ``realised_fn(ticker, at) -> float | None``: the price the venue printed at
#: the horizon, or None if it printed nothing usable.
RealisedFn = Callable[[str, datetime], "float | None"]
#: ``score_fn(output, mid_now, realised) -> score | None``, where a score has
#: ``skill``, ``value``, ``error`` and ``naive_error``.
ScoreFn = Callable[[Any, float, float], Any]
#: ``quote_fn(ticker, at) -> {"bid", "ask", "mid"} | None``: the touch the
#: venue showed at the horizon, for the paper book to cross on the way out.
#: None when the venue keeps no touch (a bar feed) or showed none.
QuoteFn = Callable[[str, datetime], "dict[str, float] | None"]


def _quote(raw: Any) -> dict[str, float] | None:
    """A touch as the book reads it, or None when what came back is not one."""
    if not isinstance(raw, dict):
        return None
    try:
        bid, ask = float(raw["bid"]), float(raw["ask"])
    except (KeyError, TypeError, ValueError):
        return None
    if ask < bid:
        return None
    mid = raw.get("mid")
    try:
        mid = float(mid) if mid is not None else (bid + ask) / 2
    except (TypeError, ValueError):
        mid = (bid + ask) / 2
    return {"bid": bid, "ask": ask, "mid": mid}


def resolve(row: dict, *, realised_fn: RealisedFn, score_fn: ScoreFn,
            horizon_minutes: int, grace_minutes: int = 1,
            now: datetime | None = None, quote_fn: QuoteFn | None = None) -> bool:
    """Score a forecast once the horizon has printed. False while it has not.

    A live forecast is only worth keeping if it eventually meets a number, and
    the number arrives ``horizon_minutes`` after the fact. Until then the row
    stays pending; it is never scored against the price it was given. With a
    ``quote_fn`` the touch at the horizon rides along as ``realised_quote``,
    so the paper book closes against what was showing rather than a proxy.
    """
    at = datetime.fromisoformat(row["at"])
    if (now or datetime.now(timezone.utc)) < at + timedelta(minutes=horizon_minutes + grace_minutes):
        return False
    realised = realised_fn(row["ticker"], at)
    if realised is None:
        row["scored"] = None
        row["unscored_because"] = "no quote printed at the horizon"
        return True
    score = score_fn(row.get("output"), row["mid_now"], realised)
    row["realised"] = realised
    if quote_fn is not None:
        try:
            quote = _quote(quote_fn(row["ticker"], at))
        except Exception:  # noqa: BLE001 - the exit touch is a bonus; the grade is the point
            quote = None
        if quote is not None:
            row["realised_quote"] = quote
    if score is None:
        # The harness never answered - the first live in-play window hit the
        # twenty-cent ledger with a dollar-sixty prompt (in-match tool payloads
        # are an order larger than pre-match ones) and this line read .skill off
        # None, crashing the sweep and taking every still-pending grading with
        # it. An unanswered quote is a recorded fact, not an exception.
        row["scored"] = None
        row["unscored_because"] = "the harness produced no forecast"
        return True
    row["scored"] = {"skill": round(score.skill, 4), "value": round(score.value, 4),
                     "error": round(score.error, 4), "naive_error": round(score.naive_error, 4)}
    return True


def write_resolved(pending: list[dict], out: Path, *, realised_fn: RealisedFn,
                   score_fn: ScoreFn, horizon_minutes: int,
                   now: datetime | None = None, quote_fn: QuoteFn | None = None) -> list[dict]:
    """Write every forecast whose horizon has printed; keep the rest waiting.

    ``now`` is for tests; a collector leaves it to the clock.
    """
    still: list[dict] = []
    with out.open("a") as fh:
        for row in pending:
            if resolve(row, realised_fn=realised_fn, score_fn=score_fn,
                       horizon_minutes=horizon_minutes, now=now, quote_fn=quote_fn):
                fh.write(json.dumps(row, default=str) + "\n")
            else:
                still.append(row)
    return still


def report(lines: list[str]) -> None:
    """Where a GitHub Actions job writes what a person will read. Empty
    elsewhere, and everything here still goes to stdout either way."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with open(path, "a") as fh:
        fh.write("\n".join(lines) + "\n")


__all__ = ["resolve", "write_resolved", "report", "RealisedFn", "ScoreFn", "QuoteFn"]
