"""The perp beside the spot: funding, open interest and the perp's own price.

Binance's futures host is geo-blocked from the US and from GitHub's runners,
so the derivatives side comes from OKX's public endpoints, which answer from
both without a key. OKX quotes the same three coins as USDT-margined
perpetual swaps (``BTC-USDT-SWAP``), and what a five-minute forecast wants
from a perp - is funding about to reset, is open interest being flushed, is
the perp trading rich or cheap to spot - reads the same off either venue.

Fetched only by discovery, into three append-only files per symbol under
``benchmarks/crypto-data/{funding,oi,perp}/<SYMBOL>.json``, merged by
timestamp. Replay reads the files and nothing else, and a window that falls
before the first recorded point is told so: **absent rather than wrong**. A
tool that quietly served the earliest point it had would be describing a
market from days later as if it were the one being forecast.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

OKX = "https://www.okx.com"
UTC = timezone.utc

#: Binance spot symbol to the OKX perpetual that tracks it.
INSTRUMENTS: dict[str, str] = {
    "BTCUSDT": "BTC-USDT-SWAP", "ETHUSDT": "ETH-USDT-SWAP", "SOLUSDT": "SOL-USDT-SWAP",
}

#: Funding settles every eight hours at 00:00, 08:00 and 16:00 UTC on OKX, as on Binance.
FUNDING_HOURS = (0, 8, 16)

KINDS = ("funding", "oi", "perp")

#: OKX period names to their length, for a reader deciding when a row is known.
PERIOD_MS: dict[str, int] = {"5m": 300_000, "15m": 900_000, "1H": 3_600_000, "4H": 14_400_000,
                             "1D": 86_400_000}


def period_ms(row: dict[str, Any], default: str = "5m") -> int:
    return PERIOD_MS.get(str(row.get("period") or default), PERIOD_MS[default])


def instrument(symbol: str) -> str:
    sym = symbol.upper()
    if sym in INSTRUMENTS:
        return INSTRUMENTS[sym]
    if sym.endswith("USDT"):
        return f"{sym[:-4]}-USDT-SWAP"
    raise KeyError(f"no OKX perpetual known for {symbol!r}")


def _ms(when: datetime) -> int:
    return int(when.timestamp() * 1000)


def _dt(ms: Any) -> datetime:
    return datetime.fromtimestamp(int(ms) / 1000, tz=UTC)


def _get(path: str, params: dict[str, Any], *, tries: int = 4) -> list[Any]:
    last: Exception | None = None
    for attempt in range(tries):
        try:
            r = httpx.get(f"{OKX}{path}", params=params, timeout=30.0)
            if r.status_code == 429 or r.status_code >= 500:
                raise httpx.HTTPStatusError(f"{r.status_code} from {path}", request=r.request,
                                            response=r)
            r.raise_for_status()
            body = r.json()
            if str(body.get("code")) != "0":
                raise RuntimeError(f"OKX {path}: {body.get('code')} {body.get('msg')}")
            return body.get("data") or []
        except (httpx.HTTPError, ValueError) as exc:
            last = exc
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"{path}: {type(last).__name__}: {last}")


# -- fetching, oldest-first, back to ``since`` --------------------------------

def fetch_funding(symbol: str, since: datetime, until: datetime | None = None) -> list[dict[str, Any]]:
    """Settled funding rates, ``{"ts", "rate", "realized"}`` per settlement, oldest first.

    ``ts`` is the settlement time: the rate is a fact from then on and a
    forecast before it. Paginated backwards with ``after`` (records earlier
    than that fundingTime) until ``since`` is passed.
    """
    inst = instrument(symbol)
    rows: list[dict[str, Any]] = []
    after: int | None = _ms(until) if until else None
    while True:
        params: dict[str, Any] = {"instId": inst, "limit": 100}
        if after is not None:
            params["after"] = after
        data = _get("/api/v5/public/funding-rate-history", params)
        if not data:
            break
        for d in data:
            rows.append({"ts": int(d["fundingTime"]), "rate": float(d["fundingRate"]),
                         "realized": float(d.get("realizedRate") or d["fundingRate"])})
        oldest = min(int(d["fundingTime"]) for d in data)
        if oldest <= _ms(since) or len(data) < 100:
            break
        after = oldest
        time.sleep(0.25)
    return sorted((r for r in rows if r["ts"] >= _ms(since)), key=lambda r: r["ts"])


def fetch_open_interest(symbol: str, since: datetime, until: datetime | None = None,
                        period: str = "5m") -> list[dict[str, Any]]:
    """Open interest history, ``{"ts", "oi", "oi_usd", "period"}`` per period, oldest first.

    Rows come as ``[ts, oi, oiCcy, oiUsd]`` (verified 2026-09-21): ``oi`` in
    contracts, ``oiCcy`` in coin, ``oiUsd`` in dollars. The coin figure is
    kept as ``oi`` because a contract is a venue-specific size.

    OKX keeps about three days at ``5m``, five at ``1H`` and a hundred at
    ``1D`` (measured 2026-09-21), so discovery stores the daily series over
    the whole span and the five-minute one over what it can reach; the row
    carries its period, and a reader treats a row as known at ``ts`` plus
    that period, since a period's figure is its aggregate.
    """
    inst = instrument(symbol)
    rows: list[dict[str, Any]] = []
    end: int | None = _ms(until) if until else None
    while True:
        params: dict[str, Any] = {"instId": inst, "period": period, "limit": 100}
        if end is not None:
            params["end"] = end
        data = _get("/api/v5/rubik/stat/contracts/open-interest-history", params)
        if not data:
            break
        for d in data:
            rows.append({"ts": int(d[0]), "oi": float(d[2]), "oi_usd": float(d[3]), "period": period})
        oldest = min(int(d[0]) for d in data)
        if oldest <= _ms(since) or len(data) < 100:
            break
        end = oldest
        time.sleep(0.25)
    return sorted((r for r in rows if r["ts"] >= _ms(since)), key=lambda r: r["ts"])


def fetch_perp_candles(symbol: str, since: datetime, until: datetime | None = None,
                       bar: str = "5m") -> list[dict[str, Any]]:
    """The perp's own candles, ``{"ts", "o", "h", "l", "c", "v"}``, only confirmed ones, oldest first.

    ``ts`` is the bar's open. A bar is known at ``ts + 5m``, and the replay
    tools apply that rule; the fetch just refuses the unconfirmed bar (the
    ninth field is 0 while it is still being printed).
    """
    inst = instrument(symbol)
    rows: list[dict[str, Any]] = []
    after: int | None = _ms(until) if until else None
    while True:
        params: dict[str, Any] = {"instId": inst, "bar": bar, "limit": 100}
        if after is not None:
            params["after"] = after
        data = _get("/api/v5/market/history-candles", params)
        if not data:
            break
        for d in data:
            if len(d) >= 9 and str(d[8]) != "1":
                continue
            rows.append({"ts": int(d[0]), "o": float(d[1]), "h": float(d[2]), "l": float(d[3]),
                         "c": float(d[4]), "v": float(d[5])})
        oldest = min(int(d[0]) for d in data)
        if oldest <= _ms(since) or len(data) < 100:
            break
        after = oldest
        time.sleep(0.15)
    return sorted((r for r in rows if r["ts"] >= _ms(since)), key=lambda r: r["ts"])


# -- the store -----------------------------------------------------------------

class FuturesStore:
    """Three append-only series per symbol, merged by timestamp, read whole.

    The files are small enough to read whole - a hundred days of 5-minute
    open interest is thirty thousand rows - and a replay tool asks for the
    rows at or before an instant, which is a slice of a sorted list.
    """

    def __init__(self, root: str | Path | None) -> None:
        self.root = Path(root) if root else None
        self._cache: dict[tuple[str, str], list[dict[str, Any]]] = {}

    def path(self, kind: str, symbol: str) -> Path | None:
        if self.root is None:
            return None
        if kind not in KINDS:
            raise KeyError(f"unknown series {kind!r}; one of {', '.join(KINDS)}")
        return self.root / kind / f"{symbol.upper()}.json"

    def read(self, kind: str, symbol: str) -> list[dict[str, Any]]:
        key = (kind, symbol.upper())
        if key in self._cache:
            return self._cache[key]
        p = self.path(kind, symbol)
        rows: list[dict[str, Any]] = []
        if p is not None and p.exists():
            rows = json.loads(p.read_text())
        rows.sort(key=lambda r: r["ts"])
        self._cache[key] = rows
        return rows

    def append(self, kind: str, symbol: str, rows: list[dict[str, Any]]) -> int:
        """Merge ``rows`` in by ``ts``; a timestamp already held keeps the newer row. Rows added."""
        p = self.path(kind, symbol)
        if p is None:
            return 0
        held = {r["ts"]: r for r in self.read(kind, symbol)}
        before = len(held)
        for r in rows:
            held[int(r["ts"])] = r
        merged = [held[k] for k in sorted(held)]
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(merged, separators=(",", ":")))
        self._cache[(kind, symbol.upper())] = merged
        return len(merged) - before

    def first_recorded(self, kind: str, symbol: str) -> datetime | None:
        rows = self.read(kind, symbol)
        return _dt(rows[0]["ts"]) if rows else None

    def last_recorded(self, kind: str, symbol: str) -> datetime | None:
        rows = self.read(kind, symbol)
        return _dt(rows[-1]["ts"]) if rows else None

    # The three reads the replay box takes. Rows as stored, oldest first.

    def funding(self, symbol: str) -> list[dict[str, Any]]:
        return self.read("funding", symbol)

    def oi(self, symbol: str) -> list[dict[str, Any]]:
        return self.read("oi", symbol)

    def perp(self, symbol: str) -> list[dict[str, Any]]:
        return self.read("perp", symbol)


def next_funding(at: datetime) -> datetime:
    """The next settlement strictly after ``at``."""
    base = at.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    for h in FUNDING_HOURS:
        candidate = base.replace(hour=h)
        if candidate > at:
            return candidate
    return base.replace(hour=0) + timedelta(days=1)


__all__ = ["INSTRUMENTS", "FUNDING_HOURS", "PERIOD_MS", "FuturesStore", "instrument", "fetch_funding",
           "fetch_open_interest", "fetch_perp_candles", "next_funding", "period_ms"]
