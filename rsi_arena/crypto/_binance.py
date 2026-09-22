"""Binance spot: minute bars, the print tape, and the one point-in-time rule.

The public market-data host is ``data-api.binance.vision``, which answers
from the US and from a GitHub runner; ``api.binance.com`` returns 451 from
both and is kept only as a fallback for wherever it is the one that works.
No key, no signing: every endpoint here is public.

**A bar is known only when it is complete.** The exchange prints a 1-minute
kline with ``ts_open``; its close is the price at ``ts_open + 60s`` and not a
second earlier. So the price *at* an instant is the close of the last bar
that had closed by then, and :func:`close_at` is that rule written once. It
is what a window's ``mid_now`` is, what ``realised`` is at the horizon, and
what every replay tool means by "now"; a second copy of it would be a second
place to get it wrong.

The bars themselves live on disk once discovery has fetched them: one gzip
JSON per symbol and UTC day under :class:`KlineStore`. :meth:`BinanceSpot.klines`
reads the store first and asks the network only for days it does not hold,
so a replay over the committed benchmark never opens a socket.

``klines`` also accepts ``interval="1s"``, which the exchange serves for the
last few days only and which always goes to the network: nothing at one
second is stored or committed. A thirty-second horizon is a later step, once
a live collector is recording one-second bars as they print; the client is
ready for it and the store is deliberately not.
"""

from __future__ import annotations

import gzip
import json
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import httpx

SPOT_HOSTS: tuple[str, ...] = ("https://data-api.binance.vision", "https://api.binance.com")

#: A quote older than this is a dead feed, not a price. Three bars: the
#: exchange prints one a minute without fail, so three missing means the
#: store has a hole, and the honest answer is no price rather than a stale one.
MAX_STALE_S = 180

MINUTE = timedelta(minutes=1)
UTC = timezone.utc

#: Bar widths the client will ask for. Only 1m is stored.
INTERVALS: dict[str, timedelta] = {"1s": timedelta(seconds=1), "1m": MINUTE, "5m": timedelta(minutes=5),
                                  "1h": timedelta(hours=1)}


@dataclass(frozen=True)
class Kline:
    ts_open: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float            # base asset
    quote_volume: float      # quote asset (USDT)
    trades: int
    taker_buy_volume: float  # base asset bought by takers: the aggressor side of the tape
    width: timedelta = MINUTE

    @property
    def ts_close(self) -> datetime:
        return self.ts_open + self.width

    @property
    def taker_sell_volume(self) -> float:
        return max(0.0, self.volume - self.taker_buy_volume)

    def to_row(self) -> list[Any]:
        return [int(self.ts_open.timestamp() * 1000), self.open, self.high, self.low, self.close,
                self.volume, self.quote_volume, self.trades, self.taker_buy_volume]

    @classmethod
    def from_row(cls, row: list[Any], width: timedelta = MINUTE) -> "Kline":
        """A row as the exchange prints it (12 fields) or as the store keeps it (9)."""
        return cls(ts_open=datetime.fromtimestamp(int(row[0]) / 1000, tz=UTC),
                   open=float(row[1]), high=float(row[2]), low=float(row[3]), close=float(row[4]),
                   volume=float(row[5]),
                   quote_volume=float(row[7] if len(row) >= 12 else row[6]),
                   trades=int(row[8] if len(row) >= 12 else row[7]),
                   taker_buy_volume=float(row[9] if len(row) >= 12 else row[8]),
                   width=width)


@dataclass(frozen=True)
class AggTrade:
    ts: datetime
    price: float
    qty: float
    buyer_maker: bool        # True: the taker sold. False: the taker bought.

    @property
    def taker_buy(self) -> bool:
        return not self.buyer_maker


# -- the point-in-time rule ----------------------------------------------------

def close_at(bars: Iterable[Kline], at: datetime, max_stale_s: int = MAX_STALE_S) -> Kline | None:
    """The last bar complete by ``at``, if it closed within ``max_stale_s`` of it.

    Complete means ``ts_open + 60s <= at``: a bar still being printed has no
    close yet, whatever the feed shows for it. Fresh means ``ts_open >= at -
    max_stale_s``, so three missing minutes are a hole and not a price.
    """
    best: Kline | None = None
    floor = at - timedelta(seconds=max_stale_s)
    for k in bars:
        if k.ts_close <= at and k.ts_open >= floor and (best is None or k.ts_open > best.ts_open):
            best = k
    return best


def complete_before(bars: Iterable[Kline], at: datetime) -> list[Kline]:
    """Every bar whose close is known at ``at``, oldest first."""
    return sorted((k for k in bars if k.ts_close <= at), key=lambda k: k.ts_open)


# -- the store -----------------------------------------------------------------

class KlineStore:
    """One gzip JSON of 1-minute bars per (symbol, UTC day).

    ``benchmarks/crypto-data/klines/<SYMBOL>/<YYYY-MM-DD>.json.gz``. Filled by
    discovery, read by everything else. A day is either whole or absent: a
    partial day is never written, so a file's presence means its 1,440 bars
    (or however many the exchange printed) are all there.
    """

    #: Days held in memory once read. A box reads the same day for every tool
    #: and a build walks a day a hundred times; a gzip decode each time is the
    #: whole cost of the replay.
    MEMO = 96

    def __init__(self, root: str | Path | None) -> None:
        self.root = Path(root) if root else None
        self._memo: dict[tuple[str, date], list[Kline]] = {}

    def path(self, symbol: str, day: date) -> Path | None:
        if self.root is None:
            return None
        return self.root / symbol.upper() / f"{day.isoformat()}.json.gz"

    def has(self, symbol: str, day: date) -> bool:
        p = self.path(symbol, day)
        return p is not None and p.exists()

    def read(self, symbol: str, day: date) -> list[Kline] | None:
        key = (symbol.upper(), day)
        if key in self._memo:
            return self._memo[key]
        p = self.path(symbol, day)
        if p is None or not p.exists():
            return None
        with gzip.open(p, "rt") as fh:
            rows = json.load(fh)
        bars = [Kline.from_row(r) for r in rows]
        if len(self._memo) >= self.MEMO:
            self._memo.pop(next(iter(self._memo)))
        self._memo[key] = bars
        return bars

    def write(self, symbol: str, day: date, bars: list[Kline]) -> None:
        p = self.path(symbol, day)
        if p is None:
            return
        p.parent.mkdir(parents=True, exist_ok=True)
        rows = [k.to_row() for k in sorted(bars, key=lambda k: k.ts_open) if k.ts_open.date() == day]
        with gzip.open(p, "wt") as fh:
            json.dump(rows, fh, separators=(",", ":"))
        self._memo.pop((symbol.upper(), day), None)

    def days(self, symbol: str) -> list[date]:
        if self.root is None or not (self.root / symbol.upper()).exists():
            return []
        return sorted(date.fromisoformat(p.name[:10])
                      for p in (self.root / symbol.upper()).glob("*.json.gz"))


# -- the client ----------------------------------------------------------------

def _get(url: str, params: dict[str, Any], *, timeout: float = 30.0, tries: int = 4) -> Any:
    last: Exception | None = None
    for attempt in range(tries):
        try:
            r = httpx.get(url, params=params, timeout=timeout)
            if r.status_code == 429 or r.status_code >= 500:
                raise httpx.HTTPStatusError(f"{r.status_code} from {url}", request=r.request,
                                            response=r)
            r.raise_for_status()
            return r.json()
        except (httpx.HTTPError, ValueError) as exc:
            last = exc
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"{url}: {type(last).__name__}: {last}")


class BinanceSpot:
    """Spot market data, store-first for minute bars.

    ``hosts`` are tried in order per request; the first that answers is kept
    for the rest of the session.
    """

    def __init__(self, store: KlineStore | None = None,
                 hosts: tuple[str, ...] = SPOT_HOSTS, fetch_missing: bool = True) -> None:
        self.store = store or KlineStore(None)
        self.hosts = list(hosts)
        self.requests = 0
        #: Whether a day the store lacks is fetched. Off for a replay, whose
        #: bars were all put on disk by discovery: a tool that reaches past
        #: the store then gets what is held and nothing else, and a benchmark
        #: run on a runner never opens a socket for a bar. On for live use.
        self.fetch_missing = fetch_missing

    def _get(self, path: str, params: dict[str, Any]) -> Any:
        errors = []
        for host in list(self.hosts):
            try:
                out = _get(f"{host}{path}", params)
            except RuntimeError as exc:
                errors.append(str(exc))
                continue
            self.requests += 1
            if self.hosts[0] != host:
                self.hosts.remove(host)
                self.hosts.insert(0, host)
            return out
        raise RuntimeError("; ".join(errors))

    # -- klines --

    def fetch_klines(self, symbol: str, start: datetime, end: datetime,
                     interval: str = "1m") -> list[Kline]:
        """``GET /api/v3/klines`` paginated by startTime, bars with ``ts_open`` in [start, end)."""
        if interval not in INTERVALS:
            raise ValueError(f"interval {interval!r}; one of {', '.join(INTERVALS)}")
        width = INTERVALS[interval]
        out: list[Kline] = []
        cursor = int(start.timestamp() * 1000)
        stop = int(end.timestamp() * 1000)
        while cursor < stop:
            rows = self._get("/api/v3/klines", {"symbol": symbol.upper(), "interval": interval,
                                                 "startTime": cursor, "endTime": stop - 1,
                                                 "limit": 1000})
            if not rows:
                break
            out.extend(Kline.from_row(r, width) for r in rows)
            last_open = int(rows[-1][0])
            if len(rows) < 1000:
                break
            cursor = last_open + 1
        return [k for k in out if start <= k.ts_open < end]

    def klines(self, symbol: str, start: datetime, end: datetime,
               interval: str = "1m") -> list[Kline]:
        """Bars with ``ts_open`` in [start, end), the store first for one-minute bars.

        Days the store holds are read from it; days it does not are fetched
        when ``fetch_missing`` is on, and not written - filling the store is
        discovery's job, so that a replay cannot half-fill a day and later
        read it as whole. Any other interval (``1s`` included) is fetched and
        never stored.
        """
        if interval != "1m":
            return self.fetch_klines(symbol, start, end, interval)
        out: list[Kline] = []
        day = start.astimezone(UTC).date()
        last = (end - timedelta(microseconds=1)).astimezone(UTC).date()
        missing: list[date] = []
        while day <= last:
            held = self.store.read(symbol, day)
            if held is None:
                missing.append(day)
            else:
                out.extend(held)
            day += timedelta(days=1)
        for day in _runs(missing if self.fetch_missing else []):
            first, final = day
            out.extend(self.fetch_klines(symbol, datetime.combine(first, datetime.min.time(), UTC),
                                         datetime.combine(final + timedelta(days=1),
                                                          datetime.min.time(), UTC)))
        return sorted((k for k in out if start <= k.ts_open < end), key=lambda k: k.ts_open)

    def fill_day(self, symbol: str, day: date) -> int:
        """Fetch one whole UTC day into the store, if it is not there. Bars written."""
        if self.store.has(symbol, day):
            return 0
        t0 = datetime.combine(day, datetime.min.time(), UTC)
        bars = self.fetch_klines(symbol, t0, t0 + timedelta(days=1))
        if not bars:
            return 0
        self.store.write(symbol, day, bars)
        return len(bars)

    # -- the tape --

    def agg_trades(self, symbol: str, start: datetime, end: datetime,
                   limit: int = 1000) -> list[AggTrade]:
        """``GET /api/v3/aggTrades`` bounded by ``endTime``: nothing after ``end`` is asked for.

        The exchange refuses a span over an hour, so the start is clamped to
        an hour before the end rather than the call failing.
        """
        start = max(start, end - timedelta(hours=1))
        rows = self._get("/api/v3/aggTrades", {"symbol": symbol.upper(),
                                                "startTime": int(start.timestamp() * 1000),
                                                "endTime": int(end.timestamp() * 1000),
                                                "limit": max(1, min(1000, int(limit)))})
        out = [AggTrade(ts=datetime.fromtimestamp(int(r["T"]) / 1000, tz=UTC), price=float(r["p"]),
                        qty=float(r["q"]), buyer_maker=bool(r["m"])) for r in rows]
        return [t for t in out if t.ts <= end]

    # -- live only --

    def depth(self, symbol: str, limit: int = 20) -> dict[str, Any]:
        """The order book now. There is no history of it, so it has no replay form."""
        raw = self._get("/api/v3/depth", {"symbol": symbol.upper(), "limit": max(5, min(100, int(limit)))})
        return {"bids": [[float(p), float(q)] for p, q in raw.get("bids", [])],
                "asks": [[float(p), float(q)] for p, q in raw.get("asks", [])],
                "last_update_id": raw.get("lastUpdateId")}

    # -- the rule, applied --

    def price_at(self, symbol: str, at: datetime) -> float | None:
        """The close of the last complete, fresh bar at ``at``: :func:`close_at`."""
        bars = self.klines(symbol, at - timedelta(seconds=MAX_STALE_S), at)
        k = close_at(bars, at)
        return None if k is None else k.close

    def realised_price(self, symbol: str, at: datetime, horizon: int = 1) -> float | None:
        return self.price_at(symbol, at + timedelta(minutes=horizon))


def _runs(days: list[date]) -> list[tuple[date, date]]:
    """Consecutive days grouped, so one fetch covers a gap rather than one a day."""
    out: list[tuple[date, date]] = []
    for d in sorted(days):
        if out and out[-1][1] + timedelta(days=1) == d:
            out[-1] = (out[-1][0], d)
        else:
            out.append((d, d))
    return out


def resample(bars: list[Kline], minutes: int) -> list[Kline]:
    """1-minute bars into ``minutes``-bars aligned to the UTC clock. Only whole buckets."""
    if minutes <= 1:
        return list(bars)
    width = timedelta(minutes=minutes)
    buckets: dict[datetime, list[Kline]] = {}
    for k in bars:
        floor = k.ts_open - timedelta(minutes=k.ts_open.minute % minutes, seconds=k.ts_open.second,
                                      microseconds=k.ts_open.microsecond)
        if minutes >= 60:
            floor = floor.replace(minute=0)
        buckets.setdefault(floor, []).append(k)
    out = []
    for floor, ks in sorted(buckets.items()):
        ks.sort(key=lambda k: k.ts_open)
        if len(ks) < minutes or ks[-1].ts_close != floor + width:
            continue                             # a bucket still being printed, or holed
        out.append(Kline(ts_open=floor, open=ks[0].open, high=max(k.high for k in ks),
                         low=min(k.low for k in ks), close=ks[-1].close,
                         volume=sum(k.volume for k in ks), quote_volume=sum(k.quote_volume for k in ks),
                         trades=sum(k.trades for k in ks),
                         taker_buy_volume=sum(k.taker_buy_volume for k in ks)))
    return out


__all__ = ["Kline", "AggTrade", "KlineStore", "BinanceSpot", "close_at", "complete_before",
           "resample", "MAX_STALE_S", "SPOT_HOSTS", "INTERVALS"]
