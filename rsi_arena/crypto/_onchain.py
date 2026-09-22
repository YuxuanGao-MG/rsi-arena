"""On-chain and flow series, with the rule for when a point is known.

Two cadences, two rules. A **daily** series - transactions a day, the
mempool's size, hash rate, stablecoin supply, DEX volume - is published as a
number for day D, and that number exists only once D is over: a chart read
during D shows D-1 as its last point. So a daily series serves the latest
point whose date is **strictly before** ``at.date()``. A **per-block**
series - fee rates - is stamped with the block's time and is known from that
second, so it serves the latest point with ``ts <= at``. Serving a daily
point for the day of the instant would hand a harness the day's own
aggregate, which is the future summarised.

Sources, all public and keyless, verified 2026-09-21: mempool.space for
per-block fee rates and hash rate, blockchain.com's charts for daily
transactions, mempool size and hash rate, DefiLlama for stablecoin supply
and DEX volume. Discovery fetches them into
``benchmarks/crypto-data/onchain/<series>.json``; the replay tools slice by
``at``. mempool.space's ``/3m`` windows reach back three months from the
day discovery ran, so a window before that is told "not recorded before
<date>" rather than served the earliest point there is.
"""

from __future__ import annotations

import json
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable

import httpx

UTC = timezone.utc

MEMPOOL = "https://mempool.space/api"
BLOCKCHAIN = "https://api.blockchain.info/charts"
LLAMA_STABLES = "https://stablecoins.llama.fi/stablecoincharts/all"
LLAMA_DEXS = "https://api.llama.fi/overview/dexs"


def _get(url: str, params: dict[str, Any] | None = None, *, tries: int = 4) -> Any:
    last: Exception | None = None
    for attempt in range(tries):
        try:
            r = httpx.get(url, params=params, timeout=60.0, follow_redirects=True)
            if r.status_code == 429 or r.status_code >= 500:
                raise httpx.HTTPStatusError(f"{r.status_code} from {url}", request=r.request,
                                            response=r)
            r.raise_for_status()
            return r.json()
        except (httpx.HTTPError, ValueError) as exc:
            last = exc
            time.sleep(2.0 * (attempt + 1))
    raise RuntimeError(f"{url}: {type(last).__name__}: {last}")


# -- fetchers: each returns rows with an integer ``ts`` in seconds -----------

def _fee_rates() -> list[dict[str, Any]]:
    rows = _get(f"{MEMPOOL}/v1/mining/blocks/fee-rates/3m")
    return [{"ts": int(r["timestamp"]), "height": int(r.get("avgHeight", 0)),
             "fee_10": float(r.get("avgFee_10", 0)), "fee_50": float(r.get("avgFee_50", 0)),
             "fee_90": float(r.get("avgFee_90", 0))} for r in rows]


def _hashrate_mempool() -> list[dict[str, Any]]:
    body = _get(f"{MEMPOOL}/v1/mining/hashrate/3m")
    return [{"ts": int(r["timestamp"]), "value": float(r["avgHashrate"])}
            for r in body.get("hashrates", [])]


def _blockchain_chart(name: str) -> Callable[[], list[dict[str, Any]]]:
    def fetch() -> list[dict[str, Any]]:
        body = _get(f"{BLOCKCHAIN}/{name}", {"timespan": "1year", "format": "json"})
        return [{"ts": int(p["x"]), "value": float(p["y"])} for p in body.get("values", [])]
    return fetch


def _stablecoins() -> list[dict[str, Any]]:
    rows = _get(LLAMA_STABLES)
    out = []
    for r in rows:
        total = (r.get("totalCirculatingUSD") or {}).get("peggedUSD")
        if total is None:
            continue
        out.append({"ts": int(r["date"]), "value": float(total)})
    return out


def _dex_volume() -> list[dict[str, Any]]:
    body = _get(LLAMA_DEXS)
    return [{"ts": int(ts), "value": float(v)} for ts, v in body.get("totalDataChart", [])]


#: Every series discovery keeps: its cadence and how to fetch it.
SERIES: dict[str, tuple[str, Callable[[], list[dict[str, Any]]]]] = {
    "fee_rates": ("block", _fee_rates),
    "hashrate_mempool": ("daily", _hashrate_mempool),
    "n_transactions": ("daily", _blockchain_chart("n-transactions")),
    "mempool_size": ("daily", _blockchain_chart("mempool-size")),
    "hash_rate": ("daily", _blockchain_chart("hash-rate")),
    "stablecoin_supply": ("daily", _stablecoins),
    "dex_volume": ("daily", _dex_volume),
}


# -- the two rules -------------------------------------------------------------

def _day(ts: int) -> date:
    return datetime.fromtimestamp(ts, tz=UTC).date()


def daily_before(rows: list[dict[str, Any]], at: datetime) -> list[dict[str, Any]]:
    """Every daily point for a day strictly before ``at``'s UTC date, oldest first."""
    today = at.astimezone(UTC).date()
    return [r for r in rows if _day(int(r["ts"])) < today]


def block_at(rows: list[dict[str, Any]], at: datetime) -> list[dict[str, Any]]:
    """Every per-block point stamped at or before ``at``, oldest first."""
    cut = int(at.timestamp())
    return [r for r in rows if int(r["ts"]) <= cut]


# -- the store -----------------------------------------------------------------

class OnchainStore:
    """One JSON per series: ``{"kind", "rows"}``, rows merged by ``ts``."""

    def __init__(self, root: str | Path | None) -> None:
        self.root = Path(root) if root else None
        self._cache: dict[str, list[dict[str, Any]]] = {}

    def path(self, name: str) -> Path | None:
        return None if self.root is None else self.root / f"{name}.json"

    def kind(self, name: str) -> str:
        return SERIES[name][0]

    def rows(self, name: str) -> list[dict[str, Any]]:
        if name in self._cache:
            return self._cache[name]
        p = self.path(name)
        out: list[dict[str, Any]] = []
        if p is not None and p.exists():
            out = json.loads(p.read_text()).get("rows", [])
        out.sort(key=lambda r: r["ts"])
        self._cache[name] = out
        return out

    def append(self, name: str, rows: list[dict[str, Any]]) -> int:
        p = self.path(name)
        if p is None:
            return 0
        # A daily series is keyed by its day: blockchain.com re-samples the
        # mempool chart's timestamps on every call, and merging by ``ts``
        # doubled the file each run. One point a day, the latest one, is
        # what "daily" means to the tools anyway.
        key = _day if self.kind(name) == "daily" else int
        held = {key(int(r["ts"])): r for r in self.rows(name)}
        before = len(held)
        for r in sorted(rows, key=lambda r: int(r["ts"])):
            held[key(int(r["ts"]))] = r
        merged = sorted(held.values(), key=lambda r: int(r["ts"]))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"kind": self.kind(name), "rows": merged}, separators=(",", ":")))
        self._cache[name] = merged
        return len(merged) - before

    def refresh(self, name: str) -> int:
        """Fetch the series from its source and merge it in. Rows added."""
        return self.append(name, SERIES[name][1]())

    def first_recorded(self, name: str) -> datetime | None:
        rows = self.rows(name)
        return datetime.fromtimestamp(int(rows[0]["ts"]), tz=UTC) if rows else None

    def known_at(self, name: str, at: datetime) -> list[dict[str, Any]]:
        """The rows a reader at ``at`` may see, by the series' own rule."""
        rows = self.rows(name)
        return daily_before(rows, at) if self.kind(name) == "daily" else block_at(rows, at)


def mempool_now() -> dict[str, Any]:
    """The mempool this second. Live only: there is no replaying a queue."""
    pool = _get(f"{MEMPOOL}/mempool")
    fees = _get(f"{MEMPOOL}/v1/fees/recommended")
    return {"tx_count": pool.get("count"), "vsize": pool.get("vsize"),
            "total_fee_btc": (pool.get("total_fee") or 0) / 1e8,
            "fastest_sat_vb": fees.get("fastestFee"), "half_hour_sat_vb": fees.get("halfHourFee"),
            "hour_sat_vb": fees.get("hourFee"), "economy_sat_vb": fees.get("economyFee")}


__all__ = ["SERIES", "OnchainStore", "daily_before", "block_at", "mempool_now"]
