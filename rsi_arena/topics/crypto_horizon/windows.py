"""The question set: instants of the spot market at a fixed cadence, with the answer attached.

A window is a symbol, an instant, the price then, a little calendar context,
and the price ``horizon`` minutes later. The price at an instant is the close
of the last complete one-minute bar (``crypto/_binance.close_at``), so a
window at 12:15:00 asks about the move from the 12:14 close to the close at
12:15 + horizon. Building one needs the minute bars, which discovery has put
on disk; once built the windows are written per UTC day under a windows
directory and never computed again. That directory is versioned with the
benchmark: it is the question set, not a cache.

**One group per UTC day, across the three symbols.** The loop splits and
resamples by group, and the gate's cluster bootstrap needs the groups to be
independent. Three coins on the same day are not independent - the majors
move together minute to minute - and two instants an hour apart on one day
share the day's regime. A day is the smallest unit that is close to
independent of the next, so it is the unit.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from ...crypto._binance import MAX_STALE_S, close_at
from ...crypto._futures import next_funding
from ...trading import PathBar

UTC = timezone.utc

#: Where discovery puts the bars, the perp series and the chain series when
#: the benchmark file does not say.
DEFAULT_DATA_DIR = "benchmarks/crypto-data"

#: The first instant of a day worth asking about. Midnight itself has the
#: previous day's last bar as its price, and the first minutes of a UTC day
#: are the funding settlement's own noise.
SKIP_FIRST_MINUTES = 5


@dataclass(frozen=True)
class Benchmark:
    symbols: tuple[str, ...]
    start: date                  # first UTC day, inclusive
    end: date                    # last UTC day, inclusive
    every: int                   # minutes between instants
    horizon: int                 # minutes ahead the window asks about
    data_dir: str = DEFAULT_DATA_DIR

    def days(self) -> list[date]:
        out, d = [], self.start
        while d <= self.end:
            out.append(d)
            d += timedelta(days=1)
        return out


def load_benchmark(path: str | Path) -> Benchmark:
    raw = json.loads(Path(path).read_text())
    return Benchmark(symbols=tuple(s.upper() for s in raw["symbols"]),
                     start=date.fromisoformat(raw["from"]), end=date.fromisoformat(raw["to"]),
                     every=int(raw.get("every", 5)), horizon=int(raw.get("horizon", 1)),
                     data_dir=str(raw.get("data_dir") or DEFAULT_DATA_DIR))


@dataclass(frozen=True)
class CryptoWindow:
    symbol: str
    at: datetime
    mid_now: float
    realised: float
    context: dict[str, Any] = field(default_factory=dict)
    #: The minute bars between the instant and the horizon, for the quote the
    #: book posts to be filled by; empty on a window built before they were kept.
    path: tuple[PathBar, ...] = ()
    #: A perpetual never settles, so this stays None; the field is here because
    #: every topic's window answers the same two questions for the book.
    settlement: float | None = None

    @property
    def id(self) -> str:
        return f"{self.symbol}@{self.at.isoformat()}"

    @property
    def group(self) -> str:
        return f"D{self.at.astimezone(UTC):%Y%m%d}"

    @property
    def ticker(self) -> str:
        """The symbol, under the name the shared thinning orders instances by."""
        return self.symbol

    def to_dict(self) -> dict[str, Any]:
        return {"symbol": self.symbol, "at": self.at.isoformat(), "mid_now": self.mid_now,
                "realised": self.realised, "context": self.context, "group": self.group,
                "path": [b.to_dict() for b in self.path], "settlement": self.settlement}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "CryptoWindow":
        return cls(symbol=d["symbol"], at=datetime.fromisoformat(d["at"]), mid_now=d["mid_now"],
                   realised=d["realised"], context=d.get("context", {}),
                   path=tuple(PathBar.from_dict(b) for b in d.get("path") or ()),
                   settlement=None if d.get("settlement") is None else float(d["settlement"]))


def context_at(at: datetime) -> dict[str, Any]:
    """The calendar as the harness is shown it: what an instant is, not what happened at it."""
    at = at.astimezone(UTC)
    return {"hour_utc": at.hour, "weekday": at.strftime("%A"),
            "minutes_to_funding": int((next_funding(at) - at).total_seconds() // 60)}


def instants(day: date, every_minutes: int, horizon: int,
             skip_first: int = SKIP_FIRST_MINUTES) -> list[datetime]:
    """Instants of ``day`` worth forecasting from, the horizon still inside the day."""
    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    end = start + timedelta(days=1)
    out, at = [], start + timedelta(minutes=skip_first)
    while at + timedelta(minutes=horizon) <= end:
        out.append(at)
        at += timedelta(minutes=every_minutes)
    return out


def price_at(spot: Any, symbol: str, at: datetime) -> float | None:
    """The close of the last complete, fresh bar at ``at``, through any ``klines`` source."""
    k = close_at(spot.klines(symbol, at - timedelta(seconds=MAX_STALE_S), at), at)
    return None if k is None else k.close


def path_at(spot: Any, symbol: str, at: datetime, horizon: int) -> tuple[PathBar, ...]:
    """The bars that close inside ``(at, at + horizon]``, as the path a resting
    quote is filled by. The bar opening at ``at`` is the first one the forecast
    could not already see, which is exactly where the quote starts working."""
    end = at + timedelta(minutes=horizon)
    try:
        bars = spot.klines(symbol, at, end)
    except Exception:  # noqa: BLE001 - no path is a quote that never fills, not a failed day
        return ()
    return tuple(PathBar(ts=k.ts_close, high=float(k.high), low=float(k.low), close=float(k.close))
                 for k in bars if at < k.ts_close <= end)


def build_windows(benchmark: Benchmark, spot: Any, *, every_minutes: int | None = None,
                  horizon: int | None = None, windows_dir: str | Path | None = None,
                  log: Callable[[str], None] = lambda m: None) -> list[CryptoWindow]:
    """Every scoreable window of every day, stored per day under ``windows_dir``.

    Resumable: a day whose file exists is read, not rebuilt, so a build that
    stopped halfway continues from where it was and a committed question set
    is never recomputed under a harness that has already been scored on it.
    """
    every = int(every_minutes or benchmark.every)
    ahead = int(horizon or benchmark.horizon)
    root = Path(windows_dir) if windows_dir else None
    out: list[CryptoWindow] = []
    for day in benchmark.days():
        path = root / f"D{day:%Y%m%d}.every{every}.h{ahead}.json" if root else None
        if path is not None and path.exists():
            out.extend(CryptoWindow.from_dict(w) for w in json.loads(path.read_text()))
            continue
        built: list[CryptoWindow] = []
        try:
            for symbol in benchmark.symbols:
                for at in instants(day, every, ahead):
                    now = price_at(spot, symbol, at)
                    if now is None:
                        continue
                    later = price_at(spot, symbol, at + timedelta(minutes=ahead))
                    if later is None:
                        continue
                    built.append(CryptoWindow(symbol=symbol, at=at, mid_now=now, realised=later,
                                              context=context_at(at),
                                              path=path_at(spot, symbol, at, ahead)))
        except Exception as exc:  # noqa: BLE001 - one day's failure is not the build's
            log(f"skipped D{day:%Y%m%d}: {type(exc).__name__}: {exc}")
            continue
        if not built:
            # Not written: an empty file would be read back as a day with no
            # windows forever, where the truth is that its bars are not on disk yet.
            log(f"skipped D{day:%Y%m%d}: no bars")
            continue
        log(f"D{day:%Y%m%d}: {len(built)} windows")
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps([w.to_dict() for w in built]))
        out.extend(built)
    return out


__all__ = ["Benchmark", "CryptoWindow", "load_benchmark", "build_windows", "instants", "context_at",
           "price_at", "path_at", "DEFAULT_DATA_DIR"]
