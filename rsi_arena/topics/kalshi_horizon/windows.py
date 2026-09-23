"""The question set: instants of finished matches, with the answer attached.

A window is a contract, an instant, the mid then, the game state then, and the
mid five minutes later. Building one needs the network; once built it is written
per fixture under a windows directory and never fetched again. That directory
is versioned with the benchmark: it is the question set, not a cache.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from ...kalshi._history import History
from ...kalshi.replay import HORIZON_MINUTES, MatchTimeline, fresh_quote, match_timeline


@dataclass(frozen=True)
class Fixture:
    league: str
    game: str
    event: str
    tickers: tuple[str, ...]


@dataclass(frozen=True)
class Window:
    ticker: str
    at: datetime
    mid_now: float
    realised: float
    game: dict[str, Any] = field(default_factory=dict)
    event: str = ""
    #: The touch the candle printed at the instant and at the horizon, for
    #: the paper book to cross; None on a window built before they were
    #: kept, which the book fills with the venue's proxy spread.
    yes_bid: float | None = None
    yes_ask: float | None = None
    yes_bid_h: float | None = None
    yes_ask_h: float | None = None

    @property
    def id(self) -> str:
        return f"{self.ticker}@{self.at.isoformat()}"

    @property
    def group(self) -> str:
        return self.event

    def to_dict(self) -> dict[str, Any]:
        # "group" beside "event": the loop and the publisher read the split's
        # unit by that name, and another topic's instance has no event.
        return {"ticker": self.ticker, "at": self.at.isoformat(), "mid_now": self.mid_now,
                "realised": self.realised, "game": self.game, "event": self.event,
                "group": self.group, "yes_bid": self.yes_bid, "yes_ask": self.yes_ask,
                "yes_bid_h": self.yes_bid_h, "yes_ask_h": self.yes_ask_h}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Window":
        return cls(ticker=d["ticker"], at=datetime.fromisoformat(d["at"]), mid_now=d["mid_now"],
                   realised=d["realised"], game=d.get("game", {}), event=d.get("event", ""),
                   yes_bid=_opt(d.get("yes_bid")), yes_ask=_opt(d.get("yes_ask")),
                   yes_bid_h=_opt(d.get("yes_bid_h")), yes_ask_h=_opt(d.get("yes_ask_h")))


def _opt(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def load_fixtures(path: str | Path) -> list[Fixture]:
    raw = json.loads(Path(path).read_text())
    return [Fixture(league=f["league"], game=str(f["game"]), event=f["event"], tickers=tuple(f["tickers"]))
            for f in raw]


def build_windows(fixtures: list[Fixture], *, history: History, every_minutes: int = 5,
                  horizon: int = HORIZON_MINUTES, windows_dir: str | Path | None = None,
                  timeline_for: Callable[[str, str], MatchTimeline | None] = match_timeline,
                  log: Callable[[str], None] = lambda m: None) -> list[Window]:
    """Every scoreable window of every fixture, stored per fixture under ``windows_dir``."""
    root = Path(windows_dir) if windows_dir else None
    out: list[Window] = []
    for fixture in fixtures:
        path = root / f"{fixture.event}.every{every_minutes}.h{horizon}.json" if root else None
        if path is not None and path.exists():
            out.extend(Window.from_dict(w) for w in json.loads(path.read_text()))
            continue
        # One fixture's feed dropping its connection used to take the whole
        # build with it: 67 of 177 matches built, then a RemoteDisconnected and
        # nothing else. Each fixture is written as it finishes, so a re-run
        # resumes — but losing the remaining hundred to one transient socket is
        # not a failure worth propagating.
        try:
            line = timeline_for(fixture.league, fixture.game)
        except Exception as exc:
            log(f"skipped {fixture.event}: {type(exc).__name__}: {exc}")
            continue
        if line is None:
            log(f"skipped {fixture.event}: no timeline")
            continue
        try:
            built = _windows_for(fixture, line, history, every_minutes, horizon)
        except Exception as exc:
            log(f"skipped {fixture.event}: {type(exc).__name__}: {exc}")
            continue
        log(f"{fixture.event}: {len(built)} windows")
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps([w.to_dict() for w in built]))
        out.extend(built)
    return out


def _windows_for(fixture: Fixture, line: MatchTimeline, hist: History,
                 every_minutes: int, horizon: int) -> list[Window]:
    built: list[Window] = []
    for ticker in fixture.tickers:
        for at in line.windows(every_minutes=every_minutes):
            candle = fresh_quote(hist, ticker, at)
            if candle is None:
                continue
            # The same candle ``realised_mid`` reads, kept whole so the
            # horizon's touch is on the window and not only its mid.
            later = fresh_quote(hist, ticker, at + timedelta(minutes=horizon))
            if later is None or later.mid is None:
                continue
            built.append(Window(ticker=ticker, at=at, mid_now=candle.mid, realised=later.mid,
                                game=line.state_at(at), event=fixture.event,
                                yes_bid=candle.yes_bid_close, yes_ask=candle.yes_ask_close,
                                yes_bid_h=later.yes_bid_close, yes_ask_h=later.yes_ask_close))
    return built
