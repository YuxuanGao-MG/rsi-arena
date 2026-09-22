"""One-minute bars, the point-in-time rule, and a store that makes replay offline.

**A bar is known once it has closed.** Alpaca stamps a bar with its open, so
the bar stamped 14:03:00 describes 14:03:00 to 14:04:00 and exists, as a
fact, at 14:04:00 and not before. Every read of a price at an instant goes
through :func:`known_bars`, which keeps a bar only when ``ts_open + 60s`` is
at or before the instant. That is the whole point-in-time contract for this
venue, and it is written here once so that a tool cannot get it differently.

A price at an instant is the close of the last known bar, and only if that
bar is recent: IEX prints a bar for a minute that traded, so a name that has
not traded for three minutes has no price, not an old one. ``MAX_STALE_S``
is the same three minutes Kalshi's replay uses.

:class:`BarStore` keeps fetched minute bars per symbol and UTC day, gzipped,
under ``benchmarks/news-data/bars/<SYMBOL>/<YYYY-MM-DD>.json.gz``. A day is
fetched whole, once; after discovery has run, every read a replay makes is a
file on disk and no key is needed.
"""

from __future__ import annotations

import gzip
import json
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ._client import AlpacaData
from ._session import ny_date

UTC = timezone.utc

BAR_S = 60
#: A known bar older than this before the instant is a name that stopped
#: trading, not a price.
MAX_STALE_S = 180
HORIZON_MINUTES = 5


@dataclass(frozen=True)
class Bar:
    symbol: str
    ts_open: datetime
    o: float
    h: float
    l: float
    c: float
    v: float
    n: int
    vwap: float

    @property
    def ts_close(self) -> datetime:
        return self.ts_open + timedelta(seconds=BAR_S)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["ts_open"] = self.ts_open.astimezone(UTC).isoformat()
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Bar":
        return cls(symbol=d["symbol"], ts_open=parse_instant(d["ts_open"]), o=float(d["o"]),
                   h=float(d["h"]), l=float(d["l"]), c=float(d["c"]), v=float(d["v"]),
                   n=int(d.get("n") or 0), vwap=float(d.get("vwap") or d["c"]))

    @classmethod
    def from_api(cls, symbol: str, raw: dict[str, Any]) -> "Bar":
        close = float(raw["c"])
        return cls(symbol=symbol, ts_open=parse_instant(raw["t"]), o=float(raw["o"]),
                   h=float(raw["h"]), l=float(raw["l"]), c=close, v=float(raw.get("v") or 0),
                   n=int(raw.get("n") or 0), vwap=float(raw.get("vw") or close))


def parse_instant(raw: str) -> datetime:
    when = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def rfc3339(at: datetime) -> str:
    return at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# -- the rule ----------------------------------------------------------------

def known_bars(bars: list[Bar], at: datetime) -> list[Bar]:
    """The bars that had closed by ``at``. A bar open at ``at - 30s`` is not one."""
    return [b for b in bars if b.ts_close <= at]


def last_complete_bar(bars: list[Bar], at: datetime, max_stale_s: int = MAX_STALE_S) -> Bar | None:
    """The last known bar, if it opened within ``max_stale_s`` of the instant."""
    usable = [b for b in known_bars(bars, at) if b.ts_open >= at - timedelta(seconds=max_stale_s)]
    return usable[-1] if usable else None


def stale_seconds(bar: Bar, at: datetime) -> int:
    return int((at - bar.ts_close).total_seconds())


def bar_at_or_before(bars: list[Bar], when: datetime) -> Bar | None:
    """The last bar in a known list that opened at or before ``when``."""
    hit = None
    for b in bars:
        if b.ts_open <= when:
            hit = b
        else:
            break
    return hit


# -- the store ---------------------------------------------------------------

class BarStore:
    """Minute bars on disk, per symbol and UTC day; daily bars per symbol.

    Regular hours sit inside one UTC day whichever way the clocks are set,
    so a day here is a session. A day is written only after the API answered
    for it, so an empty file is a day with no prints (a holiday), not a
    failed fetch.
    """

    def __init__(self, root: str | Path | None) -> None:
        self.root = Path(root) if root else None

    def path(self, symbol: str, day: date) -> Path | None:
        return None if self.root is None else self.root / symbol.upper() / f"{day.isoformat()}.json.gz"

    def has(self, symbol: str, day: date) -> bool:
        p = self.path(symbol, day)
        return p is not None and p.exists()

    def load(self, symbol: str, day: date) -> list[Bar] | None:
        p = self.path(symbol, day)
        if p is None or not p.exists():
            return None
        with gzip.open(p, "rt") as fh:
            return [Bar.from_dict(d) for d in json.load(fh)]

    def save(self, symbol: str, day: date, bars: list[Bar]) -> None:
        p = self.path(symbol, day)
        if p is None:
            return
        p.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(p, "wt") as fh:
            json.dump([b.to_dict() for b in bars], fh)

    def daily_path(self, symbol: str) -> Path | None:
        return None if self.root is None else self.root / symbol.upper() / "daily.json.gz"

    def load_daily(self, symbol: str) -> list[Bar]:
        p = self.daily_path(symbol)
        if p is None or not p.exists():
            return []
        with gzip.open(p, "rt") as fh:
            return [Bar.from_dict(d) for d in json.load(fh)]

    def save_daily(self, symbol: str, bars: list[Bar]) -> list[Bar]:
        """Merge by date and write; returns what is now on disk."""
        p = self.daily_path(symbol)
        by_day = {b.ts_open: b for b in self.load_daily(symbol)}
        for b in bars:
            by_day[b.ts_open] = b
        merged = [by_day[k] for k in sorted(by_day)]
        if p is not None:
            p.parent.mkdir(parents=True, exist_ok=True)
            with gzip.open(p, "wt") as fh:
                json.dump([b.to_dict() for b in merged], fh)
        return merged

    def days(self, symbol: str) -> list[date]:
        if self.root is None or not (self.root / symbol.upper()).exists():
            return []
        out = []
        for p in (self.root / symbol.upper()).glob("*.json.gz"):
            try:
                out.append(date.fromisoformat(p.name[: -len(".json.gz")]))
            except ValueError:
                continue
        return sorted(out)


# -- the source ---------------------------------------------------------------

class AlpacaBars:
    """Bars, prices and trades from the data API, through the store.

    ``bars`` on a one-minute timeframe is served a UTC day at a time: a day in
    the store is read from disk, a day not in it is fetched whole and written.
    So a replay on a discovered question set never touches the network, and
    a machine without keys fails only on a day nothing fetched.
    """

    def __init__(self, client: AlpacaData | None = None, store: BarStore | None = None,
                 feed: str = "iex", memo_days: int = 512) -> None:
        self.client = client or AlpacaData()
        self.store = store or BarStore(None)
        self.feed = feed
        self._memo: dict[tuple[str, date], list[Bar]] = {}
        self._memo_days = memo_days
        self.fetches = 0                             # day fetches, for tests and reports

    # -- days --

    def fetch_day(self, symbol: str, day: date) -> list[Bar]:
        """One UTC day of minute bars from the API, written to the store."""
        start = datetime.combine(day, datetime.min.time(), tzinfo=UTC)
        end = start + timedelta(days=1) - timedelta(seconds=1)
        raw = self.client.collect("/v2/stocks/bars", "bars", {
            "symbols": symbol, "timeframe": "1Min", "start": rfc3339(start), "end": rfc3339(end),
            "limit": 10000, "feed": self.feed, "adjustment": "raw", "sort": "asc"})
        self.fetches += 1
        bars = sorted((Bar.from_api(symbol, r) for r in (raw.get(symbol) or [])),
                      key=lambda b: b.ts_open)
        self.store.save(symbol, day, bars)
        return bars

    def day(self, symbol: str, day: date) -> list[Bar]:
        key = (symbol.upper(), day)
        hit = self._memo.get(key)
        if hit is not None:
            return hit
        bars = self.store.load(symbol, day)
        if bars is None:
            bars = self.fetch_day(symbol, day)
        if len(self._memo) >= self._memo_days:
            self._memo.pop(next(iter(self._memo)))
        self._memo[key] = bars
        return bars

    def ensure_days(self, symbol: str, days: list[date]) -> int:
        """Fetch what the store lacks. Returns how many days were fetched."""
        n = 0
        for d in days:
            if not self.store.has(symbol, d):
                self.fetch_day(symbol, d)
                n += 1
        return n

    # -- the protocol the tools read --

    def bars(self, symbol: str, start: datetime, end: datetime, timeframe: str = "1Min") -> list[Bar]:
        """Bars whose open is in ``[start, end]``. Nothing about completeness:
        that is :func:`known_bars`, applied by the caller at its instant."""
        if timeframe != "1Min":
            raw = self.client.collect("/v2/stocks/bars", "bars", {
                "symbols": symbol, "timeframe": timeframe, "start": rfc3339(start),
                "end": rfc3339(end), "limit": 10000, "feed": self.feed, "adjustment": "raw",
                "sort": "asc"})
            return sorted((Bar.from_api(symbol, r) for r in (raw.get(symbol) or [])),
                          key=lambda b: b.ts_open)
        out: list[Bar] = []
        d = start.astimezone(UTC).date()
        last = end.astimezone(UTC).date()
        while d <= last:
            out.extend(b for b in self.day(symbol, d) if start <= b.ts_open <= end)
            d += timedelta(days=1)
        return out

    def price_at(self, symbol: str, at: datetime, max_stale_s: int = MAX_STALE_S) -> float | None:
        """Close of the last bar that had closed by ``at`` and opened within
        ``max_stale_s`` of it; None when the name had gone quiet."""
        bar = last_complete_bar(self.bars(symbol, at - timedelta(seconds=max_stale_s + BAR_S), at),
                                at, max_stale_s)
        return None if bar is None else bar.c

    def realised_price(self, symbol: str, at: datetime, horizon: int = HORIZON_MINUTES) -> float | None:
        return self.price_at(symbol, at + timedelta(minutes=horizon))

    def daily(self, symbol: str, before_date: date, days: int = 20) -> list[Bar]:
        """The last ``days`` daily bars dated strictly before ``before_date``,
        oldest first. Served from the store's daily file when it covers them,
        fetched and merged in when it does not."""
        have = [b for b in self.store.load_daily(symbol) if ny_date(b.ts_open) < before_date]
        if len(have) < days:
            start = datetime.combine(before_date - timedelta(days=days * 2 + 10),
                                     datetime.min.time(), tzinfo=UTC)
            end = datetime.combine(before_date, datetime.min.time(), tzinfo=UTC) - timedelta(seconds=1)
            fetched = self.bars(symbol, start, end, timeframe="1Day")
            merged = self.store.save_daily(symbol, fetched) if self.store.root else fetched
            have = [b for b in merged if ny_date(b.ts_open) < before_date]
        return have[-days:]

    def trades(self, symbol: str, start: datetime, end: datetime, limit: int = 50) -> list[dict[str, Any]]:
        """Prints between ``start`` and ``end``, newest first, bounded by the
        API's ``end`` and never by trimming a longer list afterwards."""
        rows = self.client.collect(f"/v2/stocks/{symbol}/trades", "trades", {
            "start": rfc3339(start), "end": rfc3339(end), "limit": max(1, min(int(limit), 10000)),
            "feed": self.feed, "sort": "desc"}, max_items=int(limit))
        return [{"t": r.get("t"), "p": float(r.get("p") or 0), "s": float(r.get("s") or 0)}
                for r in rows]


__all__ = ["Bar", "BarStore", "AlpacaBars", "known_bars", "last_complete_bar", "stale_seconds",
           "bar_at_or_before", "parse_instant", "rfc3339", "BAR_S", "MAX_STALE_S",
           "HORIZON_MINUTES"]
