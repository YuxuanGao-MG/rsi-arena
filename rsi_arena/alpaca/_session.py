"""The US equity session, in New York time.

Regular trading hours are 09:30 to 16:00 America/New_York on weekdays. A
holiday is not modelled: on one there are no bars, so no instance builds and
no window opens, which is the same outcome with nothing to keep current.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
OPEN = time(9, 30)
CLOSE = time(16, 0)

#: A forecast instant needs a settled open behind it and a printed close
#: ahead of it: not before five minutes into the session, and the horizon
#: has to land at least a minute before the bell.
SETTLE_MINUTES = 5
BEFORE_CLOSE_MINUTES = 1


def to_ny(at: datetime) -> datetime:
    return at.astimezone(NY)


def ny_date(at: datetime) -> date:
    return to_ny(at).date()


def session_open(day: date) -> datetime:
    return datetime.combine(day, OPEN, tzinfo=NY)


def session_close(day: date) -> datetime:
    return datetime.combine(day, CLOSE, tzinfo=NY)


def is_weekday(day: date) -> bool:
    return day.weekday() < 5


def session(at: datetime) -> dict[str, Any]:
    """Where the instant sits in the day: ``pre``, ``open`` or ``closed``,
    with minutes since the open and to the close (negative before the open,
    and to the close negative after it)."""
    local = to_ny(at)
    day = local.date()
    opens, closes = session_open(day), session_close(day)
    since_open = (local - opens).total_seconds() / 60
    to_close = (closes - local).total_seconds() / 60
    if not is_weekday(day):
        phase = "closed"
    elif local < opens:
        phase = "pre"
    elif local < closes:
        phase = "open"
    else:
        phase = "closed"
    return {"date": day.isoformat(), "clock_et": local.strftime("%H:%M:%S"),
            "phase": phase, "minutes_since_open": round(since_open, 1),
            "minutes_to_close": round(to_close, 1), "weekday": local.strftime("%A")}


def in_window(at: datetime, horizon: int = 5) -> bool:
    """May a ``horizon``-minute forecast be asked at ``at``?

    Open plus five minutes at the earliest, so the first bar has printed and
    the opening auction is behind it; and ``at + horizon`` no later than a
    minute before the close, so the answer is a regular-session print and not
    the closing cross.
    """
    local = to_ny(at)
    day = local.date()
    if not is_weekday(day):
        return False
    earliest = session_open(day) + timedelta(minutes=SETTLE_MINUTES)
    latest_end = session_close(day) - timedelta(minutes=BEFORE_CLOSE_MINUTES)
    return earliest <= local and local + timedelta(minutes=horizon) <= latest_end


def prior_weekdays(day: date, n: int) -> list[date]:
    """The ``n`` weekdays before ``day``, nearest first. Holidays are among
    them and carry no bars, which the caller skips."""
    out: list[date] = []
    d = day
    while len(out) < n:
        d -= timedelta(days=1)
        if is_weekday(d):
            out.append(d)
    return out


__all__ = ["NY", "OPEN", "CLOSE", "to_ny", "ny_date", "session_open", "session_close",
           "is_weekday", "session", "in_window", "prior_weekdays"]
