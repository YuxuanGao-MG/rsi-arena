"""Fetch everything the crypto topic replays from, and write its benchmark file.

The question set is the benchmark, and it is built from data that has to be on
disk before a replay can be honest: the minute bars every window's price comes
from, the perp's funding and open interest, the chain's daily figures. None of
it needs a key. All of it is fetched here, once, append-only, and committed
under ``benchmarks/crypto-data``; the replay tools read files and never open a
socket for a bar.

    python scripts/discover_crypto.py --symbols BTCUSDT,ETHUSDT,SOLUSDT \\
        --from 2026-06-20 --to 2026-09-19 --every 5 --horizon 1 \\
        --out benchmarks/crypto-2026-09.json

Re-runnable and resumable. A day of bars already in the store is not fetched
again; a perp series is extended from its last recorded point; a chain series
is refreshed and merged by timestamp. Days whose bar count is short are
reported rather than skipped silently, because a question set that quietly
shrinks is a moving exam. The benchmark file records what was found, so the
gaps are in the commit beside the data.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rsi_arena.crypto._binance import BinanceSpot, KlineStore          # noqa: E402
from rsi_arena.crypto._futures import (FuturesStore, fetch_funding,   # noqa: E402
                                        fetch_open_interest, fetch_perp_candles)
from rsi_arena.crypto._onchain import SERIES, OnchainStore             # noqa: E402

UTC = timezone.utc

#: A UTC day has 1,440 minute bars. The exchange has printed every one of them
#: for years; fewer is a hole in the feed on the day, or a hole in the fetch.
FULL_DAY = 1440


def _days(start: date, end: date) -> list[date]:
    out, d = [], start
    while d <= end:
        out.append(d)
        d += timedelta(days=1)
    return out


def fill_klines(spot: BinanceSpot, symbols: list[str], days: list[date]) -> dict:
    report: dict = {}
    for symbol in symbols:
        held, fetched, short = 0, 0, []
        for day in days:
            if spot.store.has(symbol, day):
                n = len(spot.store.read(symbol, day) or [])
                held += 1
            else:
                n = spot.fill_day(symbol, day)
                fetched += 1
                if fetched % 10 == 0:
                    print(f"  {symbol}: {fetched} days fetched ({spot.requests} requests)", flush=True)
                time.sleep(0.1)
            if n < FULL_DAY:
                short.append({"day": day.isoformat(), "bars": n})
        report[symbol] = {"days": len(days), "held": held, "fetched": fetched, "short_days": short}
        print(f"{symbol}: {len(days)} days, {held} already held, {fetched} fetched, "
              f"{len(short)} short" + (f" ({short[0]['day']}: {short[0]['bars']} bars ...)" if short else ""))
    return report


def fill_futures(store: FuturesStore, symbols: list[str], start: date, end: date) -> dict:
    report: dict = {}
    since = datetime.combine(start - timedelta(days=1), datetime.min.time(), UTC)
    until = datetime.combine(end + timedelta(days=1), datetime.min.time(), UTC)
    # Open interest twice: daily over the whole span (OKX keeps a hundred days
    # of it) and five-minute over the few days it keeps at that resolution.
    # Both land in one file, each row carrying its period.
    fetchers = {"funding": fetch_funding,
                "oi": lambda sym, b, u: (fetch_open_interest(sym, b, u, period="1D")
                                         + fetch_open_interest(sym, b, u, period="5m")),
                "perp": fetch_perp_candles}
    for symbol in symbols:
        report[symbol] = {}
        for kind, fetch in fetchers.items():
            # Extend forward from the last point, unless the series does not
            # yet reach back to ``since`` - then fetch the whole span, so a
            # short first run (a smoke test over two days) cannot pin the
            # series' start forever.
            first, last = store.first_recorded(kind, symbol), store.last_recorded(kind, symbol)
            begin = last if (first is not None and first <= since + timedelta(days=1)) else since
            try:
                rows = fetch(symbol, begin, until)
            except Exception as exc:  # noqa: BLE001 - reported, not fatal
                print(f"  {symbol} {kind}: {type(exc).__name__}: {exc}", file=sys.stderr)
                rows = []
            added = store.append(kind, symbol, rows)
            first, final = store.first_recorded(kind, symbol), store.last_recorded(kind, symbol)
            report[symbol][kind] = {"rows": len(store.read(kind, symbol)), "added": added,
                                    "first": first.isoformat() if first else None,
                                    "last": final.isoformat() if final else None}
            print(f"{symbol} {kind}: {added} rows added, {report[symbol][kind]['rows']} held, "
                  f"from {report[symbol][kind]['first']} to {report[symbol][kind]['last']}")
    return report


def fill_onchain(store: OnchainStore) -> dict:
    report: dict = {}
    for name in SERIES:
        try:
            added = store.refresh(name)
        except Exception as exc:  # noqa: BLE001
            print(f"  onchain {name}: {type(exc).__name__}: {exc}", file=sys.stderr)
            added = 0
        rows = store.rows(name)
        first = store.first_recorded(name)
        report[name] = {"kind": store.kind(name), "rows": len(rows), "added": added,
                        "first": first.isoformat() if first else None,
                        "last": datetime.fromtimestamp(int(rows[-1]["ts"]), tz=UTC).isoformat() if rows else None}
        print(f"onchain {name}: {added} added, {len(rows)} held, from {report[name]['first']}")
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbols", default="BTCUSDT,ETHUSDT,SOLUSDT")
    ap.add_argument("--from", dest="start", required=True, help="first UTC day, inclusive")
    ap.add_argument("--to", dest="end", required=True, help="last UTC day, inclusive")
    ap.add_argument("--every", type=int, default=5, help="minutes between instants")
    ap.add_argument("--horizon", type=int, default=1, help="minutes ahead a window asks about")
    ap.add_argument("--lead-in-days", type=int, default=30,
                    help="days of bars before --from, so hourly_seasonality has its month "
                         "at the first window rather than nothing")
    ap.add_argument("--data-dir", default="benchmarks/crypto-data")
    ap.add_argument("--out", default="benchmarks/crypto-2026-09.json")
    ap.add_argument("--skip-futures", action="store_true")
    ap.add_argument("--skip-onchain", action="store_true")
    args = ap.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    data = Path(args.data_dir)
    days = _days(start - timedelta(days=max(0, args.lead_in_days)), end)

    print(f"— minute bars: {len(symbols)} symbols x {len(days)} days ({args.lead_in_days} lead-in) —")
    spot = BinanceSpot(KlineStore(data / "klines"))
    klines = fill_klines(spot, symbols, days)

    futures: dict = {}
    if not args.skip_futures:
        print("— the perp: funding, open interest, candles (OKX) —")
        futures = fill_futures(FuturesStore(data), symbols, start, end)

    onchain: dict = {}
    if not args.skip_onchain:
        print("— the chain —")
        onchain = fill_onchain(OnchainStore(data / "onchain"))

    out = {"symbols": symbols, "from": start.isoformat(), "to": end.isoformat(),
           "every": args.every, "horizon": args.horizon, "data_dir": str(data),
           "lead_in_days": args.lead_in_days,
           "built": datetime.now(UTC).isoformat(timespec="seconds"),
           "klines": klines, "futures": futures, "onchain": onchain}
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, indent=1))
    short = sum(len(v["short_days"]) for v in klines.values())
    print(f"wrote {args.out}: {len(days)} days, {len(symbols)} symbols, every {args.every}m, "
          f"horizon {args.horizon}m, {short} short days")
    return 0


if __name__ == "__main__":
    sys.exit(main())
