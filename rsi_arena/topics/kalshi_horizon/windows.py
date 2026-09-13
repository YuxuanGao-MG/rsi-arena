"""The question set: instants of finished matches, with the answer attached.

A window is a contract, an instant, the mid then, the game state then, and the
mid five minutes later. Building one needs the network; once built it is written
per fixture under a windows directory and never fetched again. That directory
is versioned with the benchmark: it is the question set, not a cache.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from ...kalshi._history import History
from ...kalshi.replay import HORIZON_MINUTES, MatchTimeline, fresh_quote, match_timeline, realised_mid


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

    @property
    def id(self) -> str:
        return f"{self.ticker}@{self.at.isoformat()}"

    @property
    def group(self) -> str:
        return self.event

    def to_dict(self) -> dict[str, Any]:
        return {"ticker": self.ticker, "at": self.at.isoformat(), "mid_now": self.mid_now,
                "realised": self.realised, "game": self.game, "event": self.event}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Window":
        return cls(ticker=d["ticker"], at=datetime.fromisoformat(d["at"]), mid_now=d["mid_now"],
                   realised=d["realised"], game=d.get("game", {}), event=d.get("event", ""))


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
        line = timeline_for(fixture.league, fixture.game)
        if line is None:
            log(f"skipped {fixture.event}: no timeline")
            continue
        built = _windows_for(fixture, line, history, every_minutes, horizon)
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
            realised = realised_mid(ticker, at, horizon, hist)
            if realised is None:
                continue
            built.append(Window(ticker=ticker, at=at, mid_now=candle.mid, realised=realised,
                                game=line.state_at(at), event=fixture.event))
    return built
