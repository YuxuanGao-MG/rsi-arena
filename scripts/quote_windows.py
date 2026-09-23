"""Put the touch on a question set that was built before the touch was kept.

A window built before ``yes_bid``/``yes_ask`` existed carries only mids, and
the paper book crosses the venue's two-cent proxy in their place. The candle
the mid came from is still on the exchange, so the touch can be read back
without rebuilding anything: for every window, the last minute candle at or
before its instant (fresh and two-sided, exactly as ``fresh_quote`` chose it)
gives ``yes_bid``/``yes_ask``, and the one at or before its horizon gives
``yes_bid_h``/``yes_ask_h``. A window with no such candle keeps None there,
and the book keeps its proxy for that one.

The question itself is never touched. ``ticker``, ``at``, ``mid_now``,
``realised``, ``game`` and ``event`` are written back byte for byte, and the
script refuses to write a file where the reloaded window's id, mid or
realised mid differs from what it read. That is what keeps the scoreboard's
memory: its key is the id and those two numbers.

    python scripts/quote_windows.py                       # every file, resumable
    python scripts/quote_windows.py --limit 3 --dry-run   # look first
    python scripts/quote_windows.py --report              # spreads on disk, no network

One ``KalshiClient`` and its rate limiter are shared by every worker; the
client already retries 429s and 5xxs with backoff, and a ticker whose fetch
still fails is retried here a few times more before its file is left alone
for the next run. Kalshi's candle reads are public, so no key is needed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rsi_arena.kalshi._history import Candle, History, MINUTE          # noqa: E402
from rsi_arena.kalshi.replay import HORIZON_MINUTES, MAX_STALE_S        # noqa: E402
from rsi_arena.topics.kalshi_horizon.windows import Window              # noqa: E402

FIELDS = ("yes_bid", "yes_ask", "yes_bid_h", "yes_ask_h")
#: Never rewritten: the question, and what the scoreboard keys its memory on.
FROZEN = ("ticker", "at", "mid_now", "realised", "game", "event", "group")
#: Either side of a ticker's span of instants, so the candle before the first
#: window and the one at the last horizon are inside the request.
PAD = timedelta(minutes=5)
#: Kalshi's proxy, in cents: the width the book assumed before the touch was kept.
PROXY_CENTS = 2.0
FETCH_ATTEMPTS = 3


def touch_at(candles: list[Candle], at: datetime, max_stale_s: int = MAX_STALE_S,
             ) -> tuple[float, float] | None:
    """``(bid, ask)`` of the last candle at or before ``at``, or None when that
    candle is missing, older than ``max_stale_s``, or not a two-sided book.

    The same choice ``fresh_quote`` makes when the set is built, so the touch
    written here is the touch of the candle the window's mid came from - not
    an earlier two-sided candle standing in for a dead one.
    """
    last: Candle | None = None
    for c in candles:
        if c.ts > at:
            break
        last = c
    if last is None or not last.two_sided:
        return None
    if (at - last.ts).total_seconds() > max_stale_s:
        return None
    return float(last.yes_bid_close), float(last.yes_ask_close)


def is_quoted(raw: dict[str, Any]) -> bool:
    return all(raw.get(f) is not None for f in FIELDS)


def _fetch(history: Any, ticker: str, start: datetime, end: datetime,
           log: Callable[[str], None]) -> list[Candle]:
    """Minute candles over ``[start, end]``, once per ticker. ``History``
    chunks anything past its 5000-period cap itself; a fetch that fails after
    the client's own retries is tried again here with a longer pause."""
    for attempt in range(FETCH_ATTEMPTS):
        try:
            return list(history.price_path(ticker, start, end, MINUTE))
        except Exception as exc:  # noqa: BLE001 - the venue's failure, whatever it was
            if attempt + 1 == FETCH_ATTEMPTS:
                raise
            pause = 5.0 * (attempt + 1)
            log(f"  {ticker}: {type(exc).__name__}: {exc}; retrying in {pause:.0f}s")
            time.sleep(pause)
    raise RuntimeError("unreachable")


def quote_windows(raws: list[dict[str, Any]], history: Any, *, horizon: int = HORIZON_MINUTES,
                  force: bool = False, log: Callable[[str], None] = lambda m: None,
                  ) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """A copy of one file's windows with the touch on them, and what was done.

    Fetches each ticker once over the span of its windows; a window already
    carrying all four fields is left as it is unless ``force``.
    """
    stale = timedelta(minutes=horizon)
    todo = [i for i, r in enumerate(raws) if force or not is_quoted(r)]
    stats = {"windows": len(raws), "skipped": len(raws) - len(todo), "entry": 0, "horizon": 0,
             "both": 0, "no_entry": 0, "no_horizon": 0, "mid_disagrees": 0}
    by_ticker: dict[str, list[int]] = defaultdict(list)
    for i in todo:
        by_ticker[raws[i]["ticker"]].append(i)
    out = [dict(r) for r in raws]
    for ticker, idxs in by_ticker.items():
        ats = [datetime.fromisoformat(raws[i]["at"]) for i in idxs]
        candles = _fetch(history, ticker, min(ats) - PAD, max(ats) + stale + PAD, log)
        for i, at in zip(idxs, ats):
            entry = touch_at(candles, at)
            later = touch_at(candles, at + stale)
            row = out[i]
            row["yes_bid"], row["yes_ask"] = entry if entry else (None, None)
            row["yes_bid_h"], row["yes_ask_h"] = later if later else (None, None)
            stats["entry"] += entry is not None
            stats["horizon"] += later is not None
            stats["both"] += entry is not None and later is not None
            stats["no_entry"] += entry is None
            stats["no_horizon"] += later is None
            if entry is not None and abs((entry[0] + entry[1]) / 2 - float(raws[i]["mid_now"])) > 1e-9:
                stats["mid_disagrees"] += 1
    return out, stats


def check_unchanged(before: list[dict[str, Any]], after: list[dict[str, Any]]) -> None:
    """Raise unless every window's question survived: same count, same frozen
    fields byte for byte, and the reloaded ``Window`` has the same id, mid
    and realised mid (the scoreboard's key)."""
    if len(before) != len(after):
        raise AssertionError(f"window count changed: {len(before)} -> {len(after)}")
    for old, new in zip(before, after):
        for f in FROZEN:
            if old.get(f, None) != new.get(f, None) or (f in old) != (f in new):
                raise AssertionError(f"{f} changed on {old.get('ticker')}@{old.get('at')}")
        a, b = Window.from_dict(old), Window.from_dict(new)
        if a.id != b.id or a.mid_now != b.mid_now or a.realised != b.realised:
            raise AssertionError(f"question changed: {a.id}")


def write_atomic(path: Path, raws: list[dict[str, Any]]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(raws))
    os.replace(tmp, path)


def quote_file(path: Path, history: Any, *, horizon: int, force: bool, dry_run: bool,
               log: Callable[[str], None]) -> dict[str, int]:
    before = json.loads(path.read_text())
    if not force and all(is_quoted(r) for r in before):
        log(f"{path.name}: {len(before)} windows already quoted")
        return {"windows": len(before), "skipped": len(before), "entry": 0, "horizon": 0,
                "both": 0, "no_entry": 0, "no_horizon": 0, "mid_disagrees": 0, "files_skipped": 1}
    after, stats = quote_windows(before, history, horizon=horizon, force=force, log=log)
    check_unchanged(before, after)
    if not dry_run:
        write_atomic(path, after)
    quoted = sum(1 for r in after if is_quoted(r))
    log(f"{path.name}: {quoted}/{len(after)} windows quoted, {len(after) - quoted} left without "
        f"({stats['no_entry']} no entry, {stats['no_horizon']} no horizon"
        + (f", {stats['mid_disagrees']} mid disagrees" if stats["mid_disagrees"] else "")
        + f"){'  [dry run]' if dry_run else ''}")
    return stats


def quote_dir(windows_dir: str | Path, history: Any, *, horizon: int = HORIZON_MINUTES,
              workers: int = 4, limit: int = 0, force: bool = False, dry_run: bool = False,
              log: Callable[[str], None] = print) -> dict[str, int]:
    """Every window file under ``windows_dir``, ``workers`` files at a time
    (each file fetches its tickers in turn, so that many tickers are in flight
    against the one shared rate limiter)."""
    files = sorted(Path(windows_dir).glob(f"*.h{horizon}.json"))
    if limit:
        files = files[:limit]
    total: dict[str, int] = defaultdict(int)
    lock = threading.Lock()

    def say(msg: str) -> None:
        with lock:
            log(msg)

    def one(path: Path) -> None:
        try:
            stats = quote_file(path, history, horizon=horizon, force=force, dry_run=dry_run, log=say)
        except Exception as exc:  # noqa: BLE001 - one file's failure is not the sweep's
            say(f"{path.name}: FAILED {type(exc).__name__}: {exc}")
            with lock:
                total["files_failed"] += 1
            return
        with lock:
            total["files"] += 1
            for k, v in stats.items():
                total[k] += v

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        list(pool.map(one, files))
    log(f"\n{total['files']} files ({total.get('files_skipped', 0)} already quoted, "
        f"{total.get('files_failed', 0)} failed): {total['windows']} windows, "
        f"{total['skipped']} skipped, {total['entry']} quoted at entry, {total['horizon']} at horizon, "
        f"{total['both']} both; {total['no_entry']} without a fresh two-sided candle at entry, "
        f"{total['no_horizon']} at horizon"
        + (f"; {total['mid_disagrees']} entry mids disagree with mid_now" if total["mid_disagrees"] else "")
        + ("  [dry run: nothing written]" if dry_run else ""))
    return dict(total)


# -- what is on disk ----------------------------------------------------------

def _quantile(values: list[float], q: float) -> float:
    if not values:
        return float("nan")
    s = sorted(values)
    return s[min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))]


def report(windows_dir: str | Path, horizon: int = HORIZON_MINUTES, log: Callable[[str], None] = print,
           ) -> dict[str, Any]:
    """Spreads of the touch already written: overall and by league prefix, in
    cents, with the share wider than the two-cent proxy. Offline."""
    files = sorted(Path(windows_dir).glob(f"*.h{horizon}.json"))
    spreads: dict[str, list[float]] = defaultdict(list)
    spreads_h: list[float] = []
    n = entry = at_horizon = both = 0
    for path in files:
        for r in json.loads(path.read_text()):
            n += 1
            e = r.get("yes_bid") is not None and r.get("yes_ask") is not None
            h = r.get("yes_bid_h") is not None and r.get("yes_ask_h") is not None
            entry += e
            at_horizon += h
            both += e and h
            if e:
                spreads[r["ticker"].split("-", 1)[0]].append(100.0 * (r["yes_ask"] - r["yes_bid"]))
            if h:
                spreads_h.append(100.0 * (r["yes_ask_h"] - r["yes_bid_h"]))
    every = [s for v in spreads.values() for s in v]
    wide = sum(1 for s in every if s > PROXY_CENTS + 1e-9)
    log(f"{len(files)} files, {n} windows: {entry} quoted at entry, {at_horizon} at horizon, {both} both; "
        f"{n - entry} without an entry touch, {n - at_horizon} without a horizon touch")
    if every:
        log(f"entry spread: median {_quantile(every, 0.5):.1f}c, p90 {_quantile(every, 0.9):.1f}c, "
            f"mean {sum(every) / len(every):.2f}c; {wide}/{len(every)} ({100.0 * wide / len(every):.1f}%) "
            f"wider than the {PROXY_CENTS:.0f}c proxy")
    if spreads_h:
        log(f"horizon spread: median {_quantile(spreads_h, 0.5):.1f}c, p90 {_quantile(spreads_h, 0.9):.1f}c")
    rows = {}
    for prefix in sorted(spreads):
        v = spreads[prefix]
        w = sum(1 for s in v if s > PROXY_CENTS + 1e-9)
        rows[prefix] = {"n": len(v), "median": _quantile(v, 0.5), "p90": _quantile(v, 0.9),
                        "wider_than_proxy": w / len(v)}
        log(f"  {prefix:22} {len(v):>6} quoted  median {rows[prefix]['median']:4.1f}c  "
            f"p90 {rows[prefix]['p90']:4.1f}c  >{PROXY_CENTS:.0f}c {100.0 * w / len(v):5.1f}%")
    return {"files": len(files), "windows": n, "entry": entry, "horizon": at_horizon, "both": both,
            "median": _quantile(every, 0.5), "p90": _quantile(every, 0.9),
            "wider_than_proxy": (wide / len(every)) if every else float("nan"), "by_prefix": rows}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--windows-dir", default="benchmarks/windows")
    ap.add_argument("--horizon", type=int, default=HORIZON_MINUTES,
                    help="minutes to the horizon; picks the *.h<N>.json files and the horizon candle")
    ap.add_argument("--workers", type=int, default=4, help="files in flight at once")
    ap.add_argument("--limit", type=int, default=0, help="only the first N files (sorted)")
    ap.add_argument("--force", action="store_true", help="requote windows that already carry the touch")
    ap.add_argument("--dry-run", action="store_true", help="fetch and report; write nothing")
    ap.add_argument("--report", action="store_true", help="summarise the touch already on disk; no network")
    args = ap.parse_args(argv)
    if args.report:
        report(args.windows_dir, args.horizon)
        return 0
    history = History()  # one client, one limiter, shared by every worker
    total = quote_dir(args.windows_dir, history, horizon=args.horizon, workers=args.workers,
                      limit=args.limit, force=args.force, dry_run=args.dry_run)
    return 1 if total.get("files_failed") else 0


if __name__ == "__main__":
    raise SystemExit(main())
