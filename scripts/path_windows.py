"""Put the realised price path on a question set that was built before it was kept.

The book became a market maker: every cycle posts a two-sided quote, and the
bars the price walked through after the instant decide whether it fills - the
bid at the first bar whose low reaches it, the ask at the first whose high
does. ``Cycle.path`` comes from the instance's ``path`` field, and a window
built before that field existed carries None there, so every one of those
windows posts a quote that cannot trade. The bars are still where they always
were, so the path can be read back without rebuilding anything.

For each window lacking a path, the bars strictly after ``at`` and through
``at + horizon``, in the instrument's own price space: the Kalshi contract's
traded YES price a minute at a time (from ``History.price_path``, one fetch
per ticker over the whole file's span), the coin's one-minute klines off the
local store, the share's one-minute bars off the local bar store. A window
whose bars are missing keeps ``path: null`` - a quote that never fills is the
honest answer for a minute nobody traded in, and not a window to throw away.

Kalshi windows also take the market's ``settlement``, 1.0 or 0.0, once per
ticker, so a position carried to the final whistle settles at what the
contract paid instead of being force-closed at the last mid. A ticker whose
settlement the history layer cannot give keeps None, and the run carries on.

The question itself is never touched. Every field but ``path`` and
``settlement`` is written back byte for byte, and the script refuses to write
a file where a reloaded window's id, mid or realised mid differs from what it
read. That is what keeps the scoreboard's memory: its key is the id and those
two numbers.

    python scripts/path_windows.py                             # all three topics, resumable
    python scripts/path_windows.py --topic crypto-horizon-1m   # one of them
    python scripts/path_windows.py --limit 3 --dry-run         # look first
    python scripts/path_windows.py --report                    # what is on disk, no network

Kalshi is the only topic that needs the network. One ``KalshiClient`` and its
rate limiter are shared by every worker; the client already retries 429s and
5xxs with backoff, and a ticker whose fetch still fails is retried here a few
times more, every worker pausing together while it does, before the file is
left alone for the next run. Crypto and news read what discovery put on disk
and open no socket at all.
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
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rsi_arena.alpaca._bars import BarStore                              # noqa: E402
from rsi_arena.crypto._binance import BinanceSpot, KlineStore            # noqa: E402
from rsi_arena.kalshi._history import History, MINUTE                    # noqa: E402
from rsi_arena.topics import spec_of                                     # noqa: E402
from rsi_arena.topics.crypto_horizon.windows import CryptoWindow         # noqa: E402
from rsi_arena.topics.crypto_horizon.windows import DEFAULT_DATA_DIR as CRYPTO_DATA_DIR  # noqa: E402
from rsi_arena.topics.kalshi_horizon.windows import SETTLES, Window      # noqa: E402
from rsi_arena.topics.news_equity.windows import DATA_DIR as NEWS_DATA_DIR  # noqa: E402
from rsi_arena.topics.news_equity.windows import NewsWindow             # noqa: E402
from rsi_arena.trading import PathBar                                    # noqa: E402

UTC = timezone.utc

#: The only two fields this script may write. Everything else on a window is
#: the question, and the question is not ours to edit.
WRITABLE = ("path", "settlement")
#: Named for the assertion's message; the check itself covers every other key.
FROZEN = ("ticker", "symbol", "at", "mid_now", "realised", "id", "game", "event", "group",
          "yes_bid", "yes_ask", "yes_bid_h", "yes_ask_h", "vwap_now", "vwap_h")
FETCH_ATTEMPTS = 3
#: The first backoff between attempts; each one waits a multiple more.
RETRY_PAUSE_S = 5.0


# -- where the bars come from ---------------------------------------------------

class KalshiPaths:
    """Traded YES prices a minute at a time, from the exchange.

    Trade prices and not quotes, exactly as ``_windows_for`` writes them when
    a set is built today: a resting bid is filled when somebody sells into it,
    and a minute in which nobody traded filled nobody. A candle with no print
    carries no price and is not a bar.

    ``price_path`` is asked first, because that is what the builder asks and
    it clips the span at the market's close. It needs the market object, and
    a handful of last July's markets 404 on it while their candles are still
    served, so a failure falls through to the candle call itself. Nothing is
    lost by that: the span asked for is inside the match, and the clip only
    ever matters after the close.
    """

    settles = True

    def __init__(self, history: Any) -> None:
        self.history = history

    def bars(self, ticker: str, start: datetime, end: datetime) -> list[PathBar]:
        try:
            candles = self.history.price_path(ticker, start, end, MINUTE)
        except Exception:  # noqa: BLE001 - a market that is gone still has its candles
            candles = self.history.candles(ticker, start, end, MINUTE)
        return [PathBar(ts=c.ts, high=float(c.price_high), low=float(c.price_low),
                        close=float(c.price_close))
                for c in candles
                if c.price_high is not None and c.price_low is not None and c.price_close is not None]

    def settlement(self, ticker: str) -> float | None:
        """1.0, 0.0, or None for a market that has not resolved."""
        read = getattr(self.history, "settlement", None)
        if read is None:
            return None
        return SETTLES.get(str(read(ticker) or "").lower())


class CryptoPaths:
    """One-minute klines off the local store; a perpetual never settles."""

    settles = False

    def __init__(self, spot: Any) -> None:
        self.spot = spot

    def bars(self, symbol: str, start: datetime, end: datetime) -> list[PathBar]:
        return [PathBar(ts=k.ts_close, high=float(k.high), low=float(k.low), close=float(k.close))
                for k in self.spot.klines(symbol, start, end)]

    def settlement(self, symbol: str) -> float | None:
        return None


class NewsPaths:
    """One-minute IEX bars off the local store; a share never settles.

    The store keeps a UTC day per file, so a span is read a day at a time and
    a day nobody fetched is simply no bars rather than a failure.
    """

    settles = False

    def __init__(self, store: BarStore) -> None:
        self.store = store

    def bars(self, symbol: str, start: datetime, end: datetime) -> list[PathBar]:
        out: list[PathBar] = []
        day, last = start.astimezone(UTC).date(), end.astimezone(UTC).date()
        while day <= last:
            for b in self.store.load(symbol, day) or ():
                out.append(PathBar(ts=b.ts_close, high=float(b.h), low=float(b.l), close=float(b.c)))
            day += timedelta(days=1)
        out.sort(key=lambda b: b.ts)
        return out

    def settlement(self, symbol: str) -> float | None:
        return None


# -- what each topic is ----------------------------------------------------------

@dataclass(frozen=True)
class TopicPaths:
    """A topic's question set, and how to read the path of one of its windows."""

    name: str
    field: str                              # the window field naming the instrument
    window_cls: type                        # for the reload check before writing
    source: Callable[[str | None], Any]     # (data_dir) -> a paths source
    data_dir: str | None = None

    @property
    def windows_dir(self) -> str:
        return spec_of(self.name).windows_dir


def _kalshi_source(_: str | None) -> KalshiPaths:
    return KalshiPaths(History())  # one client, one limiter, shared by every worker


def _crypto_source(data_dir: str | None) -> CryptoPaths:
    root = Path(data_dir or CRYPTO_DATA_DIR) / "klines"
    return CryptoPaths(BinanceSpot(KlineStore(root), fetch_missing=False))


def _news_source(data_dir: str | None) -> NewsPaths:
    return NewsPaths(BarStore(Path(data_dir or NEWS_DATA_DIR) / "bars"))


TOPICS: dict[str, TopicPaths] = {
    "kalshi-horizon-5m": TopicPaths("kalshi-horizon-5m", "ticker", Window,
                                    _kalshi_source),
    "crypto-horizon-1m": TopicPaths("crypto-horizon-1m", "symbol", CryptoWindow, _crypto_source,
                                    data_dir=CRYPTO_DATA_DIR),
    "news-equity-5m": TopicPaths("news-equity-5m", "symbol", NewsWindow, _news_source,
                                 data_dir=NEWS_DATA_DIR),
}


# -- being polite ----------------------------------------------------------------

class Throttle:
    """A pause every worker respects, so a burst of 429s slows the whole run
    down rather than each thread discovering the limit on its own."""

    def __init__(self) -> None:
        self._until = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        while True:
            with self._lock:
                left = self._until - time.monotonic()
            if left <= 0:
                return
            time.sleep(min(left, 1.0))

    def pause(self, seconds: float) -> None:
        with self._lock:
            self._until = max(self._until, time.monotonic() + seconds)


#: What the venue saying "slow down" looks like once it has become an exception.
BURST_MARKERS = ("429", "Too Many", "500", "502", "503", "504")


def _rate_limited(exc: Exception) -> bool:
    text = f"{type(exc).__name__}: {exc}"
    return any(m in text for m in BURST_MARKERS)


def _fetch(source: Any, instrument: str, start: datetime, end: datetime, *,
           throttle: Throttle, log: Callable[[str], None]) -> list[PathBar]:
    """The bars over ``[start, end]``, once per instrument, retried a few times
    past whatever the client already did. A failure that smells like a rate
    limit holds every worker back, not just this one."""
    for attempt in range(FETCH_ATTEMPTS):
        throttle.wait()
        try:
            return source.bars(instrument, start, end)
        except Exception as exc:  # noqa: BLE001 - the venue's failure, whatever it was
            if attempt + 1 == FETCH_ATTEMPTS:
                raise
            pause = RETRY_PAUSE_S * (attempt + 1)
            if _rate_limited(exc):
                throttle.pause(pause)
            log(f"  {instrument}: {type(exc).__name__}: {exc}; retrying in {pause:.0f}s")
            time.sleep(pause)
    raise RuntimeError("unreachable")


# -- one file ---------------------------------------------------------------------

def horizon_of(path: Path) -> int | None:
    """The minutes a file's windows look ahead, off its ``.h<N>.json`` suffix."""
    stem = path.name[: -len(".json")] if path.name.endswith(".json") else path.name
    tail = stem.rsplit(".", 1)[-1]
    return int(tail[1:]) if tail.startswith("h") and tail[1:].isdigit() else None


def bars_in(bars: list[PathBar], at: datetime, horizon: timedelta) -> list[PathBar]:
    """The bars of ``(at, at + horizon]``, in time order. The bar stamped at
    the instant itself is the one the forecast could already see."""
    end = at + horizon
    return [b for b in bars if at < b.ts <= end]


def _blank_stats() -> dict[str, int]:
    return {"windows": 0, "skipped": 0, "pathed": 0, "no_bars": 0, "bars": 0,
            "settled": 0, "unsettled": 0, "unreachable": 0}


def path_windows(raws: list[dict[str, Any]], source: Any, *, horizon: int, field: str = "ticker",
                 force: bool = False, throttle: Throttle | None = None,
                 log: Callable[[str], None] = lambda m: None,
                 ) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """A copy of one file's windows with the path on them, and what was done.

    Fetches each instrument once over the span of its windows; a window that
    already carries a path is left as it is unless ``force``.
    """
    ahead = timedelta(minutes=horizon)
    thr = throttle or Throttle()
    settles = bool(getattr(source, "settles", False))
    need_path = [i for i, r in enumerate(raws) if force or r.get("path") is None]
    need_settle = ([i for i, r in enumerate(raws) if force or r.get("settlement") is None]
                   if settles else [])
    stats = _blank_stats()
    stats["windows"] = len(raws)
    stats["skipped"] = len(raws) - len(set(need_path) | set(need_settle))
    out = [dict(r) for r in raws]

    by_instrument: dict[str, list[int]] = defaultdict(list)
    for i in need_path:
        by_instrument[raws[i][field]].append(i)
    for instrument, idxs in by_instrument.items():
        ats = [datetime.fromisoformat(raws[i]["at"]) for i in idxs]
        try:
            bars = _fetch(source, instrument, min(ats), max(ats) + ahead, throttle=thr, log=log)
        except Exception as exc:  # noqa: BLE001 - an instrument the venue no longer serves
            # A few of last July's markets are gone from the exchange
            # altogether. Their windows keep the null they had, which is the
            # same answer as a minute nobody traded in, and the file's other
            # contracts still get their paths.
            log(f"  {instrument}: no bars ({type(exc).__name__}: {exc})")
            for i in idxs:
                out[i]["path"] = None
            stats["no_bars"] += len(idxs)
            stats["unreachable"] += len(idxs)
            continue
        for i, at in zip(idxs, ats):
            got = bars_in(bars, at, ahead)
            out[i]["path"] = [b.to_dict() for b in got] if got else None
            stats["pathed" if got else "no_bars"] += 1
            stats["bars"] += len(got)

    settled: dict[str, float | None] = {}
    for i in need_settle:
        instrument = raws[i][field]
        if instrument not in settled:
            thr.wait()
            try:
                settled[instrument] = source.settlement(instrument)
            except Exception as exc:  # noqa: BLE001 - a settlement is a bonus, not the run
                log(f"  {instrument}: no settlement ({type(exc).__name__}: {exc})")
                settled[instrument] = None
        out[i]["settlement"] = settled[instrument]
        stats["settled" if settled[instrument] is not None else "unsettled"] += 1
    return out, stats


def check_unchanged(before: list[dict[str, Any]], after: list[dict[str, Any]],
                    window_cls: type | None = None) -> None:
    """Raise unless every window's question survived: same count, every field
    but the path and the settlement byte for byte, and the reloaded window
    with the same id, mid and realised mid (the scoreboard's key)."""
    if len(before) != len(after):
        raise AssertionError(f"window count changed: {len(before)} -> {len(after)}")
    for old, new in zip(before, after):
        for f in sorted((set(old) | set(new)) - set(WRITABLE)):
            if (f in old) != (f in new) or old.get(f) != new.get(f):
                raise AssertionError(f"{f} changed on {old.get('ticker') or old.get('symbol')}"
                                     f"@{old.get('at')}")
        if window_cls is None:
            continue
        a, b = window_cls.from_dict(old), window_cls.from_dict(new)
        if a.id != b.id or a.mid_now != b.mid_now or a.realised != b.realised:
            raise AssertionError(f"question changed: {a.id}")


def write_atomic(path: Path, raws: list[dict[str, Any]]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(raws))
    os.replace(tmp, path)


def _is_done(raw: dict[str, Any], settles: bool) -> bool:
    return raw.get("path") is not None and (not settles or raw.get("settlement") is not None)


def path_file(path: Path, source: Any, topic: TopicPaths, *, horizon: int, force: bool,
              dry_run: bool, throttle: Throttle, log: Callable[[str], None]) -> dict[str, int]:
    before = json.loads(path.read_text())
    settles = bool(getattr(source, "settles", False))
    if not force and all(_is_done(r, settles) for r in before):
        stats = _blank_stats()
        stats.update(windows=len(before), skipped=len(before))
        stats["files_skipped"] = 1
        log(f"{path.name}: {len(before)} windows already pathed")
        return stats
    after, stats = path_windows(before, source, horizon=horizon, field=topic.field, force=force,
                                throttle=throttle, log=log)
    check_unchanged(before, after, topic.window_cls)
    if not dry_run:
        write_atomic(path, after)
    pathed = sum(1 for r in after if r.get("path"))
    log(f"{path.name}: {pathed}/{len(after)} windows with a path, {stats['bars']} bars"
        + (f", {stats['settled']} settled" if settles else "")
        + (f" ({stats['no_bars']} without bars)" if stats["no_bars"] else "")
        + ("  [dry run]" if dry_run else ""))
    return stats


def window_files(windows_dir: str | Path, limit: int = 0) -> list[Path]:
    """The question set's files, sorted, each one carrying its own horizon."""
    files = [p for p in sorted(Path(windows_dir).glob("*.json")) if horizon_of(p) is not None]
    return files[:limit] if limit else files


def path_dir(windows_dir: str | Path, source: Any, topic: TopicPaths, *, workers: int = 4,
             limit: int = 0, force: bool = False, dry_run: bool = False,
             log: Callable[[str], None] = print) -> dict[str, int]:
    """Every window file under ``windows_dir``, ``workers`` files at a time
    (each file reads its instruments in turn, so that many instruments are in
    flight against the one shared rate limiter)."""
    files = window_files(windows_dir, limit)
    total: dict[str, int] = defaultdict(int)
    lock = threading.Lock()
    throttle = Throttle()

    def say(msg: str) -> None:
        with lock:
            log(msg)

    def one(path: Path) -> None:
        try:
            stats = path_file(path, source, topic, horizon=horizon_of(path) or 5, force=force,
                              dry_run=dry_run, throttle=throttle, log=say)
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
    log(f"\n{topic.name}: {total['files']} files ({total.get('files_skipped', 0)} already pathed, "
        f"{total.get('files_failed', 0)} failed): {total['windows']} windows, "
        f"{total['skipped']} skipped, {total['pathed']} given a path over {total['bars']} bars, "
        f"{total['no_bars']} left without one"
        + (f" ({total['unreachable']} on a market the venue no longer serves)"
           if total.get("unreachable") else "")
        + (f"; {total['settled']} settled, {total['unsettled']} not"
           if total.get("settled") or total.get("unsettled") else "")
        + ("  [dry run: nothing written]" if dry_run else ""))
    return dict(total)


def run_topic(topic: TopicPaths, *, windows_dir: str | None = None, data_dir: str | None = None,
              workers: int = 4, limit: int = 0, force: bool = False, dry_run: bool = False,
              log: Callable[[str], None] = print) -> dict[str, int]:
    source = topic.source(data_dir or topic.data_dir)
    return path_dir(windows_dir or topic.windows_dir, source, topic, workers=workers, limit=limit,
                    force=force, dry_run=dry_run, log=log)


# -- what is on disk ----------------------------------------------------------------

def _quantile(values: list[float], q: float) -> float:
    if not values:
        return float("nan")
    s = sorted(values)
    return s[min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))]


def report(windows_dir: str | Path, *, unit: str = "cents", tick: float = 1.0,
           log: Callable[[str], None] = print) -> dict[str, Any]:
    """The paths already on disk: how many windows have one, how many bars they
    carry, and how far the price travelled inside them, against the narrowest
    quote the engine will post (one tick either side of the forecast).

    The range is what decides whether a quote can be hit at all: a window whose
    whole path spans less than a tick cannot fill both sides of even the
    tightest market, and one that spans ten will fill anything. Offline.
    """
    files = window_files(windows_dir)
    ranges: list[float] = []
    bars: list[int] = []
    n = pathed = settled = 0
    for path in files:
        for r in json.loads(path.read_text()):
            n += 1
            if r.get("settlement") is not None:
                settled += 1
            p = r.get("path")
            if not p:
                continue
            pathed += 1
            bars.append(len(p))
            hi, lo = max(b["high"] for b in p), min(b["low"] for b in p)
            mid = float(r.get("mid_now") or 0.0)
            ranges.append(100.0 * (hi - lo) if unit == "cents"
                          else (1e4 * (hi - lo) / mid if mid else 0.0))
    log(f"{len(files)} files, {n} windows: {pathed} with a path, {n - pathed} without"
        + (f", {settled} settled" if settled else ""))
    if bars:
        log(f"bars per path: median {_quantile([float(b) for b in bars], 0.5):.0f}, "
            f"mean {sum(bars) / len(bars):.1f}, {sum(bars)} in all")
    out = {"files": len(files), "windows": n, "pathed": pathed, "settled": settled,
           "bars": sum(bars), "unit": unit}
    if ranges:
        wide = sum(1 for v in ranges if v >= 2 * tick)
        out.update({"range_p10": _quantile(ranges, 0.1), "range_p50": _quantile(ranges, 0.5),
                    "range_p90": _quantile(ranges, 0.9),
                    "wider_than_tick_quote": wide / len(ranges)})
        log(f"path range ({unit}): p10 {out['range_p10']:.2f}, median {out['range_p50']:.2f}, "
            f"p90 {out['range_p90']:.2f}; {wide}/{len(ranges)} "
            f"({100.0 * wide / len(ranges):.1f}%) span the {2 * tick:g}-{unit} "
            f"tick-wide quote end to end")
    return out


UNITS = {"kalshi-horizon-5m": ("cents", 1.0), "crypto-horizon-1m": ("bps", 2.0),
         "news-equity-5m": ("bps", 5.0)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--topic", default="all", choices=("all", *TOPICS),
                    help="which question set to path; 'all' does the three in turn")
    ap.add_argument("--windows-dir", default=None, help="override the topic's own directory")
    ap.add_argument("--data-dir", default=None, help="override the local store crypto/news read")
    ap.add_argument("--workers", type=int, default=4, help="files in flight at once")
    ap.add_argument("--limit", type=int, default=0, help="only the first N files (sorted)")
    ap.add_argument("--force", action="store_true", help="repath windows that already carry one")
    ap.add_argument("--dry-run", action="store_true", help="read and report; write nothing")
    ap.add_argument("--report", action="store_true", help="summarise what is on disk; no network")
    args = ap.parse_args(argv)
    names = list(TOPICS) if args.topic == "all" else [args.topic]
    if args.windows_dir and len(names) > 1:
        ap.error("--windows-dir names one directory, so it needs one --topic")
    failed = 0
    for name in names:
        topic = TOPICS[name]
        windows_dir = args.windows_dir or topic.windows_dir
        if args.report:
            unit, tick = UNITS.get(name, ("cents", 1.0))
            print(f"{name} ({windows_dir})")
            report(windows_dir, unit=unit, tick=tick)
            print()
            continue
        total = run_topic(topic, windows_dir=windows_dir, data_dir=args.data_dir,
                          workers=args.workers, limit=args.limit, force=args.force,
                          dry_run=args.dry_run)
        failed += total.get("files_failed", 0)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
