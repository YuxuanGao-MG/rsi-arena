"""The question set: the second a story broke on a US stock, with the answer attached.

A window is a symbol, the instant a Benzinga item on it was published, the
last IEX minute close known at that instant, and the close five minutes on.
Building one needs bars, which after discovery are on disk; once built it is
written per symbol-day under a windows directory and never computed again.
That directory is versioned with the benchmark: it is the question set, not
a cache.

The group is the symbol-day, because the gate resamples by group and the
same name on the same afternoon is one thing seen several times. Its prefix
is the symbol, so the probe sample stratifies by name rather than by date.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from ...alpaca._bars import HORIZON_MINUTES, parse_instant
from ...alpaca._news import NewsItem
from ...alpaca._session import in_window, ny_date
from ...trading import PathBar
from .._common.store import write_json_atomic

#: Where discovery leaves the bars a replay reads, next to the benchmark.
DATA_DIR = "benchmarks/news-data"


@dataclass(frozen=True)
class BenchmarkItem:
    """One row of the benchmark file: a story paired with one of its symbols."""

    symbol: str
    news_id: str
    at: datetime
    headline: str
    summary: str = ""
    source: str = ""
    symbols: tuple[str, ...] = ()
    updated_at: datetime | None = None

    @property
    def edited_after(self) -> bool:
        return self.updated_at is not None and self.updated_at > self.at

    @property
    def group(self) -> str:
        return f"{self.symbol}-{ny_date(self.at):%Y%m%d}"

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "BenchmarkItem":
        return cls(symbol=str(d["symbol"]).upper(), news_id=str(d["news_id"]),
                   at=parse_instant(d["at"]), headline=d.get("headline", ""),
                   summary=d.get("summary", "") or "", source=d.get("source", "") or "",
                   symbols=tuple(str(s).upper() for s in (d.get("symbols") or [])),
                   updated_at=parse_instant(d["updated_at"]) if d.get("updated_at") else None)

    def to_dict(self) -> dict[str, Any]:
        return {"symbol": self.symbol, "news_id": self.news_id, "at": self.at.isoformat(),
                "headline": self.headline, "summary": self.summary, "source": self.source,
                "symbols": list(self.symbols),
                "updated_at": self.updated_at.isoformat() if self.updated_at else None}


@dataclass(frozen=True)
class NewsWindow:
    symbol: str
    at: datetime
    mid_now: float                  # the last complete IEX minute close at the instant
    realised: float                 # the same, five minutes on
    news_id: str
    headline: str
    summary: str = ""
    source: str = ""
    symbols: tuple[str, ...] = ()
    edited_after: bool = False
    #: The vwap of the bar behind ``mid_now`` and of the bar at the horizon,
    #: for the paper book to cross; None on a window built before they were
    #: kept, or through a bar source that serves closes only.
    vwap_now: float | None = None
    vwap_h: float | None = None
    #: The minute bars between the instant and the horizon, for the quote the
    #: book posts to be filled by; empty through a source that serves closes only.
    path: tuple[PathBar, ...] = ()
    #: A share never settles, so this stays None; the field is here because
    #: every topic's window answers the same two questions for the book.
    settlement: float | None = None

    @property
    def id(self) -> str:
        return f"{self.symbol}@{self.at.isoformat()}#{self.news_id}"

    @property
    def group(self) -> str:
        return f"{self.symbol}-{ny_date(self.at):%Y%m%d}"

    #: The name the thinner and the live grader read the instrument by.
    @property
    def ticker(self) -> str:
        return self.symbol

    def item(self) -> NewsItem:
        """The story as the frozen toolbox is handed it."""
        return NewsItem(id=self.news_id, created_at=self.at, updated_at=self.at,
                        headline=self.headline, summary=self.summary, source=self.source,
                        symbols=self.symbols or (self.symbol,), edited_after=self.edited_after)

    def to_dict(self) -> dict[str, Any]:
        return {"symbol": self.symbol, "at": self.at.isoformat(), "mid_now": self.mid_now,
                "realised": self.realised, "news_id": self.news_id, "headline": self.headline,
                "summary": self.summary, "source": self.source, "symbols": list(self.symbols),
                "edited_after": self.edited_after, "group": self.group,
                "vwap_now": self.vwap_now, "vwap_h": self.vwap_h,
                "path": [b.to_dict() for b in self.path], "settlement": self.settlement}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "NewsWindow":
        return cls(symbol=d["symbol"], at=parse_instant(d["at"]), mid_now=float(d["mid_now"]),
                   realised=float(d["realised"]), news_id=str(d["news_id"]),
                   headline=d.get("headline", ""), summary=d.get("summary", "") or "",
                   source=d.get("source", "") or "",
                   symbols=tuple(d.get("symbols") or ()), edited_after=bool(d.get("edited_after", False)),
                   vwap_now=_opt(d.get("vwap_now")), vwap_h=_opt(d.get("vwap_h")),
                   path=tuple(PathBar.from_dict(b) for b in d.get("path") or ()),
                   settlement=_opt(d.get("settlement")))


def _opt(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _vwap(bars: Any, symbol: str, at: datetime) -> float | None:
    """The vwap of the bar ``price_at`` would read at ``at``, through a source
    that can hand the bar over; None through one that serves closes only."""
    bar_at = getattr(bars, "bar_at", None)
    if bar_at is None:
        return None
    try:
        bar = bar_at(symbol, at)
    except Exception:  # noqa: BLE001 - the close was already read; the vwap is a bonus
        return None
    return None if bar is None else float(getattr(bar, "vwap", 0.0) or 0.0) or None


def _path(bars: Any, symbol: str, at: datetime, horizon: int) -> tuple[PathBar, ...]:
    """The minute bars closing inside ``(at, at + horizon]``, as the path a
    resting quote is filled by; empty through a source that serves closes
    only, which is a quote that never trades rather than a window lost."""
    read = getattr(bars, "bars", None)
    if read is None:
        return ()
    end = at + timedelta(minutes=horizon)
    try:
        got = read(symbol, at, end)
    except Exception:  # noqa: BLE001 - the closes were already read; the path is a bonus
        return ()
    return tuple(PathBar(ts=b.ts_close, high=float(b.h), low=float(b.l), close=float(b.c))
                 for b in got if at < b.ts_close <= end)


def load_benchmark(path: str | Path) -> list[BenchmarkItem]:
    raw = json.loads(Path(path).read_text())
    return [BenchmarkItem.from_dict(r) for r in raw]


def group_path(root: Path, group: str, horizon: int = HORIZON_MINUTES) -> Path:
    return root / f"{group}.h{horizon}.json"


def build_windows(items: list[BenchmarkItem], *, bars: Any, horizon: int = HORIZON_MINUTES,
                  windows_dir: str | Path | None = None,
                  log: Callable[[str], None] = lambda m: None) -> list[NewsWindow]:
    """Every scoreable window of every item, one file per symbol-day.

    An item is dropped when its instant is outside the window a forecast may
    be asked in, when no bar had closed within three minutes of it, or when
    none had five minutes on. A group already on disk is read, never rebuilt,
    so a run that stopped resumes without touching the bars again.
    """
    root = Path(windows_dir) if windows_dir else None
    by_group: dict[str, list[BenchmarkItem]] = {}
    for it in items:
        by_group.setdefault(it.group, []).append(it)
    out: list[NewsWindow] = []
    for group in sorted(by_group):
        path = group_path(root, group, horizon) if root else None
        if path is not None and path.exists():
            out.extend(NewsWindow.from_dict(w) for w in json.loads(path.read_text()))
            continue
        built: list[NewsWindow] = []
        for it in sorted(by_group[group], key=lambda i: i.at):
            if not in_window(it.at, horizon):
                log(f"skipped {it.symbol} {it.at.isoformat()}: outside regular hours")
                continue
            try:
                now = bars.price_at(it.symbol, it.at)
                later = bars.realised_price(it.symbol, it.at, horizon)
            except Exception as exc:
                log(f"skipped {it.symbol} {it.at.isoformat()}: {type(exc).__name__}: {exc}")
                continue
            if now is None:
                log(f"skipped {it.symbol} {it.at.isoformat()}: no fresh bar at the instant")
                continue
            if later is None:
                log(f"skipped {it.symbol} {it.at.isoformat()}: no fresh bar at the horizon")
                continue
            built.append(NewsWindow(symbol=it.symbol, at=it.at, mid_now=now, realised=later,
                                    news_id=it.news_id, headline=it.headline, summary=it.summary,
                                    source=it.source, symbols=it.symbols or (it.symbol,),
                                    edited_after=it.edited_after,
                                    vwap_now=_vwap(bars, it.symbol, it.at),
                                    vwap_h=_vwap(bars, it.symbol, it.at + timedelta(minutes=horizon)),
                                    path=_path(bars, it.symbol, it.at, horizon)))
        log(f"{group}: {len(built)} windows")
        if path is not None:
            write_json_atomic(path, [w.to_dict() for w in built])
        out.extend(built)
    return out


__all__ = ["BenchmarkItem", "NewsWindow", "load_benchmark", "build_windows", "group_path", "DATA_DIR"]
