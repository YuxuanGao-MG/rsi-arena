"""Roll a topic's question set forward, so the search always sees recent markets.

A question set built once and committed is an exam that ages: the Kalshi set
ended on the newest settled fixture the day it was discovered, the crypto set
on a Tuesday in September, the news set on the fifteenth. Every generation
after that is asked about the same weeks, and the weeks it never sees are the
ones the incumbent will be traded on. This script is run weekly by
``.github/workflows/roll.yml`` and appends what the venues have settled since,
then prunes the oldest so the set keeps its size.

    python scripts/roll_question_set.py --topic kalshi-horizon-5m --dry-run
    python scripts/roll_question_set.py --topic crypto-horizon-1m --keep 365
    python scripts/roll_question_set.py --topic news-equity-5m --until 2026-09-19
    python scripts/roll_question_set.py --topic all --validate --summary roll.json

``--topic all`` rolls every topic in turn, each with its own time budget and
its own row in the summary, and one topic's failure does not stop the next.
The workflow does not use it: it runs a step per topic so a red topic is a red
step, and the job's verdict can be "all three failed" rather than "the first
one did".

How each topic rolls, as a procedure
------------------------------------

**kalshi-horizon-5m** (``benchmarks/soccer-2026.json``, ``benchmarks/windows/``)

1. *Newest* is the latest date encoded in an event ticker already in the set
   (``KXEPLGAME-26SEP14LEENEW`` is dated 2026-09-14). Kalshi dates an event by
   the day it listed the contract, which is the kickoff day give or take
   ``discover_fixtures.DATE_SPREAD`` (three days), so the search window starts
   ``DATE_SPREAD`` days before newest and ends at ``--until`` (yesterday).
2. For every league already in the set, in alphabetical order, the settled
   events of its match-winner series (``KX<LEAGUE>GAME``) are read newest
   first. An event is a candidate when its ticker date is inside the window
   and its ticker is not already in the set. Reading stops once
   twenty-five consecutive events are older than the window.
3. A candidate becomes a fixture exactly as ``discover_fixtures.resolve``
   decides: at least two finalised markets, linked to an ESPN fixture with
   enough confidence, with a timeline that yields at least ten five-minute
   windows. Anything else is reported by reason and left for next week.
4. Dedupe is by event ticker and then by ESPN game id: Kalshi sometimes lists
   one match under two tickers, and two rows for one match would put the same
   game on both sides of a split.
5. The new fixtures are appended to the benchmark (written whole to a
   temporary file and renamed into place), their window files are built by
   ``kalshi_horizon.windows.build_windows`` (only fixtures without a file are
   built), ``quote_windows.quote_file`` puts the touch on those files, and
   ``path_windows.fill_paths`` puts the minute prints between each instant and
   its horizon on them. The path script is imported defensively: a checkout
   without it logs a notice and rolls on, leaving the new windows pathless,
   which the book reads as a quote nothing ever filled.
6. Prune: fixtures are ordered by ticker date, the newest ``--keep`` (485)
   stay, the benchmark is rewritten without the rest, and then their window
   files are deleted. Nothing else under ``benchmarks/windows`` is touched.

**crypto-horizon-1m** (``benchmarks/crypto-2026-09.json``, ``benchmarks/windows-crypto/``)

1. *Newest* is the benchmark's ``to``. The new days are ``to + 1`` through
   ``--until`` (yesterday, so every day is complete on the exchange).
2. For each new day and each of the three symbols, the day's 1,440 minute
   bars come from the Binance spot mirror into the kline store; a day already
   in the store is counted, not fetched. Then the OKX perp series (funding,
   open interest, candles) are extended from their last recorded point and
   the on-chain series are refreshed. Discovery is a store-level check, so a
   fill that stopped halfway is resumed by the next run.
3. ``to`` moves to the last day filled and the benchmark is rewritten; then
   ``crypto_horizon.windows.build_windows`` builds the new days' files (a day
   without bars is not written, so it can be built later).
4. Prune: ``from`` moves to ``to - keep + 1`` (``--keep`` 365 days) and the
   ``D<date>`` window files of the days before it are deleted. The kline,
   perp and on-chain stores are never pruned: they are cheap and the live
   collector reads them.

**news-equity-5m** (``benchmarks/news-2026-09.json``, ``benchmarks/windows-news/``)

1. *Newest* is the latest New York session date of an item in the set. The
   new sessions are the weekdays from the day after through ``--until``.
2. For each new session, ``discover_news.discover`` runs over the universe
   file for that one day: the symbol must clear the dollar-volume screen on
   the twenty sessions before it, the Benzinga item must break in regular
   hours far enough from the open and the close, its headline must not
   repeat one on the name within thirty minutes, it must be ten minutes from
   the last item kept on the name, and a name gives at most four items a day.
3. Dedupe is by ``(symbol, news_id)``: one story on two names is two rows by
   design, so the id alone would drop the second name.
4. The new items are appended and the benchmark rewritten; the bars each
   item's replay reads (its day and ten sessions before it, the index ETFs,
   twenty daily bars) are fetched into the bar store; then
   ``news_equity.windows.build_windows`` builds the new symbol-days' files.
5. Prune: the oldest session dates are dropped whole, one at a time, while
   the remainder still holds at least ``--keep`` symbol-days (2,350, the
   size the set had when the roll began); the benchmark is rewritten and the
   dropped groups' window files deleted. The bar store is not pruned.

What a roll never does
----------------------

- It never rewrites an existing window file. New files are created, pruned
  files are deleted, and every file that survives is checked byte for byte
  against its state before the roll; a difference aborts the roll with an
  error. A window's ``id``, ``mid_now`` and ``realised`` are the scoreboard's
  key, and a remembered answer must stay attached to its question.
- It never touches ``runs/``, the archive or the scoreboard.
- It never shrinks a set below what the topic's spec needs: audit + held-out
  + the probe. ``--validate`` runs ``rsi-arena windows --json`` and
  ``scripts/preflight.py`` after the roll and reverts the topic's paths in
  git when either fails.

A roll reshuffles held-out membership: ``three_way_split`` shuffles the sorted
group ids on a fixed seed, so adding or removing a group moves others between
train, held-out and audit. That is acceptable because the scoreboard keys on
instance ids, so every remembered answer survives, and because the audit set
is meant to be a fresh cut rather than a monument.

Idempotent: a second run on the same day finds nothing newer than the set and
nothing older than ``--keep``, and writes nothing. ``--dry-run`` does the
discovery in memory, prints what would change, and writes nothing at all.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import inspect
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rsi_arena.topics import TOPICS, spec_of                                 # noqa: E402

UTC = timezone.utc
KALSHI, CRYPTO, NEWS = "kalshi-horizon-5m", "crypto-horizon-1m", "news-equity-5m"
#: ``--topic all``: every topic in turn, each with its own budget and its own row.
ALL = "all"

#: Groups a set keeps after a roll: the size each set had when the roll began,
#: so the split the specs were sized for keeps its power.
#:
#: Crypto was 92 UTC days, which is a quarter, which held out thirty and could
#: resolve a gap of about 0.04 pooled skill - wider than any rewrite has ever
#: produced, and wider than the +0.025 that gen5 was promoted on. Its bars are
#: free and unlimited from the Binance mirror, so the set is a year: 365 days
#: hold out 120 and resolve about 0.02. This is the number the weekly roll
#: maintains; shrinking it silently shrinks the gate's eyesight.
KEEP = {KALSHI: 485, CRYPTO: 365, NEWS: 2350}
#: Where each topic's discovery leaves venue data, beside the question set.
DATA_DIRS = {CRYPTO: "benchmarks/crypto-data", NEWS: "benchmarks/news-data"}
#: Minutes a topic may spend discovering before it keeps what it has.
BUDGET_MINUTES = 25.0
#: Upstream calls the roll adds are tried this many times, backing off geometrically.
RETRY_ATTEMPTS = 3
#: Settled events older than the window, in a row, before a league's listing
#: is taken to have run past it. Kalshi lists newest first, but not by contract.
STOP_AFTER_OLD = 25
#: Cadence and horizon of the Kalshi set, which name its window files.
KALSHI_EVERY, KALSHI_HORIZON = 5, 5
NEWS_HORIZON = 5

Log = Callable[[str], None]


class RollError(RuntimeError):
    """The roll cannot go on without leaving the set in a state it should not be in."""


# -- small tools -----------------------------------------------------------------

def yesterday(today: date | None = None) -> date:
    return (today or datetime.now(UTC).date()) - timedelta(days=1)


def days_between(start: date, end: date) -> list[date]:
    out, d = [], start
    while d <= end:
        out.append(d)
        d += timedelta(days=1)
    return out


def write_atomic(path: Path, text: str) -> None:
    """Whole file or nothing: a roll interrupted mid-write leaves the old file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def load_script(name: str) -> Any:
    """A sibling script as a module. ``scripts/`` is not a package on purpose."""
    if name in sys.modules:
        return sys.modules[name]
    path = Path(__file__).resolve().parent / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # Registered before it runs, for two reasons: a dataclass in the script
    # resolves its annotations through ``sys.modules``, and loading the same
    # script twice in one process would run its imports twice.
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return mod


def load_optional(name: str, attr: str, *, log: Log = print) -> Any:
    """``name.attr`` if the sibling script is there, else None and a notice.

    ``scripts/path_windows.py`` is written on its own branch and may not be in
    a checkout this script runs from. A roll without it is a roll whose new
    windows carry no path, which the book reads as a quote that never filled -
    thinner than it should be, but not wrong, and next week's roll picks the
    paths up once the script lands.
    """
    try:
        mod = load_script(name)
    except (ImportError, OSError, SyntaxError) as exc:
        log(f"  scripts/{name}.py not available ({type(exc).__name__}: {exc}); "
            f"skipping {attr}")
        return None
    fn = getattr(mod, attr, None)
    if fn is None:
        log(f"  scripts/{name}.py has no {attr}(); skipping it")
    return fn


class Budget:
    """Wall-clock allowance for the part of a roll that talks to a venue."""

    def __init__(self, minutes: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self.deadline = clock() + minutes * 60.0

    def expired(self) -> bool:
        return self.clock() >= self.deadline

    def left(self) -> float:
        return max(0.0, self.deadline - self.clock())


def with_retries(fn: Callable[..., Any], *args: Any, attempts: int = RETRY_ATTEMPTS, base: float = 2.0,
                 log: Log = lambda m: None, what: str = "", sleep: Callable[[float], None] = time.sleep,
                 **kwargs: Any) -> Any:
    """``fn(*args, **kwargs)``, tried ``attempts`` times with a pause of 2, 4, 8... seconds."""
    for attempt in range(attempts):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - the venue's failure, whatever it was
            if attempt + 1 == attempts:
                raise
            pause = base * (2 ** attempt)
            log(f"  {what or getattr(fn, '__name__', 'call')}: {type(exc).__name__}: {exc}; "
                f"retrying in {pause:.0f}s")
            sleep(pause)
    raise RuntimeError("unreachable")


class Untouched:
    """The files a roll must leave alone, fingerprinted before and checked after."""

    def __init__(self, paths: Iterable[Path]) -> None:
        self.before = {p: self._digest(p) for p in paths if p.exists()}

    @staticmethod
    def _digest(p: Path) -> str:
        return hashlib.sha256(p.read_bytes()).hexdigest()

    def check(self) -> None:
        changed = [p for p, d in self.before.items() if not p.exists() or self._digest(p) != d]
        if changed:
            raise RollError("a surviving window file changed during the roll: "
                            + ", ".join(p.name for p in changed[:5]))


@dataclass
class Roll:
    """What one topic's roll did, for the log, the step summary and the tests."""

    topic: str
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    groups: int = 0
    status: str = "ok"          # ok | nothing | budget | skipped | reverted | failed
    reason: str = ""
    dry_run: bool = False
    windows: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"topic": self.topic, "status": self.status, "reason": self.reason,
                "added": len(self.added), "removed": len(self.removed), "groups": self.groups,
                "added_ids": self.added, "removed_ids": self.removed, "dry_run": self.dry_run,
                "windows": self.windows}

    def line(self) -> str:
        verb = "would add" if self.dry_run else "added"
        gone = "would remove" if self.dry_run else "removed"
        s = (f"{self.topic}: {verb} {len(self.added)}, {gone} {len(self.removed)}, "
             f"{self.groups} groups; {self.status}")
        return s + (f" ({self.reason})" if self.reason else "")


# -- kalshi ------------------------------------------------------------------------

def event_date(event: str) -> date | None:
    """The date a Kalshi fixture ticker carries, or None for a ticker that has none."""
    from rsi_arena.kalshi._linking import parse_event_ticker
    fx = parse_event_ticker(event)
    return fx.date if fx else None


def newest_kickoff(rows: list[dict[str, Any]]) -> date | None:
    dates = [d for d in (event_date(r["event"]) for r in rows) if d is not None]
    return max(dates) if dates else None


def kalshi_window_path(windows_dir: Path, event: str, every: int = KALSHI_EVERY,
                       horizon: int = KALSHI_HORIZON) -> Path:
    return windows_dir / f"{event}.every{every}.h{horizon}.json"


def discover_kalshi(rows: list[dict[str, Any]], since: date, until: date, *,
                    events_of: Callable[[str], Iterable[dict[str, Any]]],
                    resolve: Callable[[str, str], tuple[dict[str, Any] | None, str]],
                    budget: Budget, log: Log = print, leagues: list[str] | None = None,
                    date_spread: int | None = None) -> tuple[list[dict[str, Any]], bool]:
    """New fixtures dated in ``[since - DATE_SPREAD, until]`` that are not in ``rows``,
    and whether the budget stopped the search before the listing ran out."""
    if date_spread is None:
        date_spread = load_script("discover_fixtures").DATE_SPREAD
    known_events = {r["event"] for r in rows}
    known_games = {str(r["game"]) for r in rows}
    leagues = leagues or sorted({r["league"] for r in rows})
    if not leagues:
        raise RollError("the set names no league; build it with scripts/discover_fixtures.py first")
    floor = since - timedelta(days=date_spread)
    new: list[dict[str, Any]] = []
    for league in leagues:
        if budget.expired():
            log(f"  time budget spent before {league}; keeping what was found")
            return new, True
        old_in_a_row = 0
        try:
            listing = iter(events_of(league))
        except Exception as exc:  # noqa: BLE001 - one league's venue failure is not the roll's
            log(f"  {league}: listing failed ({type(exc).__name__}: {exc}); skipped this week")
            continue
        while True:
            try:
                event = next(listing)
            except StopIteration:
                break
            except Exception as exc:  # noqa: BLE001
                log(f"  {league}: listing broke ({type(exc).__name__}: {exc}); moving on")
                break
            ticker = event.get("event_ticker", "")
            when = event_date(ticker) if ticker else None
            if when is None:
                continue
            if when < floor:
                old_in_a_row += 1
                if old_in_a_row >= STOP_AFTER_OLD:
                    break
                continue
            old_in_a_row = 0
            if when > until or ticker in known_events:
                continue
            if budget.expired():
                log(f"  time budget spent in {league}; keeping what was found")
                return new, True
            try:
                row, why = resolve(league, ticker)
            except Exception as exc:  # noqa: BLE001
                row, why = None, f"{type(exc).__name__}: {exc}"
            if row is None:
                log(f"  skip  {ticker:34} {why}")
                continue
            if str(row["game"]) in known_games:
                log(f"  dup   {ticker:34} same match as one already in the set")
                continue
            log(f"  ok    {ticker:34} game {row['game']}")
            new.append({k: v for k, v in row.items() if k != "windows"})
            known_events.add(ticker)
            known_games.add(str(row["game"]))
    return new, False


def roll_kalshi(benchmark: Path, windows_dir: Path, *, keep: int, until: date, dry_run: bool,
                discover: Callable[[list[dict[str, Any]], date, date], tuple[list[dict[str, Any]], bool]],
                build: Callable[[list[dict[str, Any]]], None],
                quote: Callable[[list[Path]], None],
                fill_paths: Callable[[list[Path]], None] | None = None,
                log: Log = print) -> Roll:
    rows: list[dict[str, Any]] = json.loads(benchmark.read_text())
    since = newest_kickoff(rows) or (until - timedelta(days=7))
    log(f"{KALSHI}: {len(rows)} fixtures, newest dated {since}; looking through {until}")
    new, cut_short = discover(rows, since, until)
    # ``windows`` is how many a fixture yielded, which is discovery's diagnostic
    # and not part of the benchmark's contract - the other four keys are. Dropped
    # here as well as in ``discover_kalshi`` so the file's shape does not depend
    # on which discovery produced the row.
    new = [{k: v for k, v in r.items() if k != "windows"} for r in new]

    merged = rows + new
    ordered = sorted(merged, key=lambda r: (event_date(r["event"]) or date.min, r["event"]))
    drop = {r["event"] for r in ordered[:-keep]} if 0 < keep < len(ordered) else set()
    kept = [r for r in merged if r["event"] not in drop]
    roll = Roll(KALSHI, added=[r["event"] for r in new if r["event"] not in drop],
                removed=sorted(drop), groups=len(kept), dry_run=dry_run)
    if cut_short:
        roll.status, roll.reason = "budget", "time budget spent; kept what was found"
    elif not new and not drop:
        roll.status, roll.reason = "nothing", "nothing to add"
    if dry_run:
        return roll

    survivors = Untouched(kalshi_window_path(windows_dir, r["event"]) for r in kept
                          if r["event"] not in {n["event"] for n in new})
    # 1. Append first, so a roll that dies in the build leaves fixtures
    #    without windows - which `rsi-arena windows` builds next time.
    if new:
        write_atomic(benchmark, json.dumps(merged, indent=2) + "\n")
        fresh = [r for r in new if r["event"] not in drop]
        build(fresh)
        fresh_paths = [kalshi_window_path(windows_dir, r["event"]) for r in fresh]
        quote(fresh_paths)
        # The quote says where the book may post; the path says whether the
        # tape ever went there. Both are written onto the new files only -
        # `Untouched` covers everything else.
        if fill_paths is not None:
            fill_paths(fresh_paths)
    # 2. Prune: the benchmark first, then the files it no longer names.
    if drop:
        write_atomic(benchmark, json.dumps(kept, indent=2) + "\n")
        for event in sorted(drop):
            p = kalshi_window_path(windows_dir, event)
            if p.exists():
                p.unlink()
    survivors.check()
    return roll


# -- crypto ------------------------------------------------------------------------

def crypto_window_path(windows_dir: Path, day: date, every: int, horizon: int) -> Path:
    return windows_dir / f"D{day:%Y%m%d}.every{every}.h{horizon}.json"


def _merge_klines(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    out = {k: dict(v) for k, v in (old or {}).items()}
    for symbol, rep in (new or {}).items():
        cur = out.setdefault(symbol, {"days": 0, "held": 0, "fetched": 0, "short_days": []})
        for k in ("days", "held", "fetched"):
            cur[k] = int(cur.get(k, 0)) + int(rep.get(k, 0))
        cur["short_days"] = list(cur.get("short_days", [])) + list(rep.get("short_days", []))
    return out


def roll_crypto(benchmark: Path, windows_dir: Path, *, keep: int, until: date, dry_run: bool,
                fill: Callable[[list[str], list[date], str], dict[str, Any]],
                build: Callable[[Any], None], log: Log = print) -> Roll:
    """``fill(symbols, days, data_dir)`` fetches the days into the stores and returns
    ``{"to": <last day filled or None>, "klines": ..., "futures": ..., "onchain": ...}``;
    ``build(benchmark)`` writes the window files of a :class:`Benchmark` span."""
    from rsi_arena.topics.crypto_horizon.windows import Benchmark

    raw = json.loads(benchmark.read_text())
    symbols = [str(s).upper() for s in raw["symbols"]]
    every, horizon = int(raw.get("every", 5)), int(raw.get("horizon", 1))
    data_dir = str(raw.get("data_dir") or DATA_DIRS[CRYPTO])
    old_from, old_to = date.fromisoformat(raw["from"]), date.fromisoformat(raw["to"])
    new_days = days_between(old_to + timedelta(days=1), until) if until > old_to else []
    log(f"{CRYPTO}: {old_from} to {old_to}; {len(new_days)} new days through {until}")

    roll = Roll(CRYPTO, dry_run=dry_run)
    new_to = old_to
    if new_days and not dry_run:
        report = fill(symbols, new_days, data_dir)
        filled_to = report.get("to")
        if filled_to is None:
            roll.status, roll.reason = "budget", "no new day could be filled; kept what was there"
        else:
            new_to = filled_to
            if filled_to < new_days[-1]:
                roll.status, roll.reason = "budget", f"time budget spent after {filled_to}"
            raw.update({"to": new_to.isoformat(), "klines": _merge_klines(raw.get("klines"), report.get("klines")),
                        "futures": report.get("futures") or raw.get("futures"),
                        "onchain": report.get("onchain") or raw.get("onchain"),
                        "built": datetime.now(UTC).isoformat(timespec="seconds")})
            write_atomic(benchmark, json.dumps(raw, indent=1))
    elif new_days:
        new_to = new_days[-1]
    added = days_between(old_to + timedelta(days=1), new_to) if new_to > old_to else []

    new_from = max(old_from, new_to - timedelta(days=keep - 1)) if keep > 0 else old_from
    dropped = days_between(old_from, new_from - timedelta(days=1)) if new_from > old_from else []
    roll.added = [f"D{d:%Y%m%d}" for d in added]
    roll.removed = [f"D{d:%Y%m%d}" for d in dropped]
    if roll.status == "ok" and not added and not dropped:
        roll.status, roll.reason = "nothing", "nothing to add"
    if dry_run:
        roll.groups = sum(1 for d in days_between(new_from, new_to)
                          if crypto_window_path(windows_dir, d, every, horizon).exists() or d in added)
        return roll

    survivors = Untouched(crypto_window_path(windows_dir, d, every, horizon)
                          for d in days_between(new_from, old_to))
    if added:
        build(Benchmark(symbols=tuple(symbols), start=added[0], end=new_to, every=every,
                        horizon=horizon, data_dir=data_dir))
    if dropped:
        raw["from"] = new_from.isoformat()
        write_atomic(benchmark, json.dumps(raw, indent=1))
        for d in dropped:
            p = crypto_window_path(windows_dir, d, every, horizon)
            if p.exists():
                p.unlink()
    survivors.check()
    roll.groups = sum(1 for d in days_between(new_from, new_to)
                      if crypto_window_path(windows_dir, d, every, horizon).exists())
    return roll


# -- news --------------------------------------------------------------------------

def roll_news(benchmark: Path, windows_dir: Path, *, keep: int, until: date, dry_run: bool,
              discover: Callable[[date, date], tuple[list[Any], bool]],
              fill: Callable[[list[Any]], None], build: Callable[[list[Any]], None],
              log: Log = print) -> Roll:
    """``discover(start, end)`` returns the items kept over those sessions and whether
    the budget cut it short; ``fill(items)`` puts their bars in the store; ``build(items)``
    writes their symbol-days' window files."""
    from rsi_arena.alpaca._session import ny_date
    from rsi_arena.topics.news_equity.windows import BenchmarkItem, group_path

    items = [BenchmarkItem.from_dict(d) for d in json.loads(benchmark.read_text())]
    newest = max((ny_date(i.at) for i in items), default=until - timedelta(days=7))
    start = newest + timedelta(days=1)
    log(f"{NEWS}: {len(items)} items on {len({i.group for i in items})} symbol-days, newest session "
        f"{newest}; looking through {until}")
    cut_short = False
    found: list[Any] = []
    if start <= until:
        found, cut_short = discover(start, until)
    known = {(i.symbol, i.news_id) for i in items}
    new: list[Any] = []
    for it in found:
        key = (it.symbol, it.news_id)
        if key in known:
            continue
        known.add(key)
        new.append(it)
    merged = items + new

    by_date: dict[date, set[str]] = {}
    for it in merged:
        by_date.setdefault(ny_date(it.at), set()).add(it.group)
    total = sum(len(g) for g in by_date.values())
    drop_dates: list[date] = []
    for d in sorted(by_date):
        if keep > 0 and total - len(by_date[d]) >= keep:
            drop_dates.append(d)
            total -= len(by_date[d])
        else:
            break
    dropped_groups = sorted(g for d in drop_dates for g in by_date[d])
    kept = [it for it in merged if ny_date(it.at) not in set(drop_dates)]
    roll = Roll(NEWS, added=sorted({it.group for it in new if ny_date(it.at) not in set(drop_dates)}),
                removed=dropped_groups, groups=len({it.group for it in kept}), dry_run=dry_run)
    if cut_short:
        roll.status, roll.reason = "budget", "time budget spent; kept what was found"
    elif not new and not drop_dates:
        roll.status, roll.reason = "nothing", "nothing to add"
    if dry_run:
        return roll

    new_groups = {it.group for it in new}
    survivors = Untouched(group_path(windows_dir, g, NEWS_HORIZON)
                          for g in {it.group for it in kept} - new_groups)
    if new:
        write_atomic(benchmark, json.dumps([it.to_dict() for it in merged], indent=1) + "\n")
        fresh = [it for it in new if ny_date(it.at) not in set(drop_dates)]
        fill(fresh)
        build(fresh)
    if drop_dates:
        write_atomic(benchmark, json.dumps([it.to_dict() for it in kept], indent=1) + "\n")
        for g in dropped_groups:
            p = group_path(windows_dir, g, NEWS_HORIZON)
            if p.exists():
                p.unlink()
    survivors.check()
    return roll


# -- the real sources ----------------------------------------------------------------

def kalshi_sources(args: argparse.Namespace, budget: Budget, log: Log) -> dict[str, Callable[..., Any]]:
    df = load_script("discover_fixtures")
    qw = load_script("quote_windows")
    from rsi_arena.kalshi._client import KalshiClient
    from rsi_arena.kalshi._history import History
    from rsi_arena.topics.kalshi_horizon.windows import Fixture, build_windows

    client = KalshiClient()
    context: dict[str, tuple[dict[str, str], set[str]]] = {}

    def league_context(league: str) -> tuple[dict[str, str], set[str]]:
        if league not in context:
            series = df.series_for(league)
            codes = with_retries(df.harvest_team_codes, client, series, log=log, what=f"{league} codes")
            markets = with_retries(lambda: list(client.paginate("/markets", "markets",
                                                                {"series_ticker": series}, max_items=4000)),
                                   log=log, what=f"{league} markets")
            context[league] = (df.names_from_markets(markets), codes)
            log(f"{league}: {len(codes)} team codes, {len(context[league][0])} names")
        return context[league]

    def events_of(league: str) -> Iterable[dict[str, Any]]:
        return client.paginate("/events", "events",
                               {"series_ticker": df.series_for(league), "status": "settled"},
                               max_items=args.limit)

    def resolve(league: str, ticker: str) -> tuple[dict[str, Any] | None, str]:
        names, codes = league_context(league)
        return with_retries(df.resolve, client, league, ticker, names, codes, log=log, what=ticker)

    def discover(rows: list[dict[str, Any]], since: date, until: date) -> tuple[list[dict[str, Any]], bool]:
        return discover_kalshi(rows, since, until, events_of=events_of, resolve=resolve,
                               budget=budget, log=log)

    history = History(client)

    def build(new_rows: list[dict[str, Any]]) -> None:
        fixtures = [Fixture(league=r["league"], game=str(r["game"]), event=r["event"],
                            tickers=tuple(r["tickers"])) for r in new_rows]
        build_windows(fixtures, history=history, every_minutes=KALSHI_EVERY, horizon=KALSHI_HORIZON,
                      windows_dir=args.windows_dir, log=log)

    def quote(paths: list[Path]) -> None:
        for p in paths:
            if not p.exists():
                continue
            try:
                qw.quote_file(p, history, horizon=KALSHI_HORIZON, force=False, dry_run=False, log=log)
            except Exception as exc:  # noqa: BLE001 - the touch is a bonus; the question is built
                log(f"  {p.name}: touch not written ({type(exc).__name__}: {exc})")

    filler = load_optional("path_windows", "fill_paths", log=log)

    def fill_paths(paths: list[Path]) -> None:
        """The minute prints between each new window's instant and its horizon.

        Called once per file, with whatever keywords the script's own
        ``fill_paths`` declares: it is being written in parallel and its
        signature is not this script's to fix. A file that cannot be given a
        path keeps its quote and is logged, because a window without a path is
        a resting quote nothing fills, not a broken question.
        """
        if filler is None:
            return
        try:
            allowed = set(inspect.signature(filler).parameters)
        except (TypeError, ValueError):
            allowed = set()
        extra = {k: v for k, v in {"horizon": KALSHI_HORIZON, "force": False,
                                   "dry_run": False, "log": log}.items() if k in allowed}
        for p in paths:
            if not p.exists():
                continue
            try:
                filler(p, history, **extra)
            except Exception as exc:  # noqa: BLE001 - the path is a bonus; the question is built
                log(f"  {p.name}: path not written ({type(exc).__name__}: {exc})")

    return {"discover": discover, "build": build, "quote": quote, "fill_paths": fill_paths}


def crypto_sources(args: argparse.Namespace, budget: Budget, log: Log) -> dict[str, Callable[..., Any]]:
    dc = load_script("discover_crypto")
    from rsi_arena.crypto._binance import BinanceSpot, KlineStore
    from rsi_arena.crypto._futures import FuturesStore
    from rsi_arena.crypto._onchain import OnchainStore
    from rsi_arena.topics.crypto_horizon.windows import build_windows

    def fill(symbols: list[str], days: list[date], data_dir: str) -> dict[str, Any]:
        data = Path(data_dir)
        spot = BinanceSpot(KlineStore(data / "klines"))
        klines = {s: {"days": 0, "held": 0, "fetched": 0, "short_days": []} for s in symbols}
        done: list[date] = []
        for day in days:
            if budget.expired():
                log(f"  time budget spent before {day}; keeping what was fetched")
                break
            try:
                for symbol in symbols:
                    rep = klines[symbol]
                    if spot.store.has(symbol, day):
                        n = len(spot.store.read(symbol, day) or [])
                        rep["held"] += 1
                    else:
                        n = with_retries(spot.fill_day, symbol, day, log=log, what=f"{symbol} {day}")
                        rep["fetched"] += 1
                    rep["days"] += 1
                    if n < dc.FULL_DAY:
                        rep["short_days"].append({"day": day.isoformat(), "bars": n})
            except Exception as exc:  # noqa: BLE001 - stop at the last whole day
                log(f"  {day}: {type(exc).__name__}: {exc}; stopping at the last whole day")
                break
            done.append(day)
            log(f"  {day}: " + ", ".join(f"{s} {'held' if spot.store.has(s, day) else 'missing'}" for s in symbols))
        if not done:
            return {"to": None}
        futures: dict[str, Any] = {}
        onchain: dict[str, Any] = {}
        if not budget.expired():
            try:
                futures = with_retries(dc.fill_futures, FuturesStore(data), symbols, done[0], done[-1],
                                       log=log, what="perp series")
            except Exception as exc:  # noqa: BLE001 - the perp is context, not the question
                log(f"  perp series not extended ({type(exc).__name__}: {exc})")
        if not budget.expired():
            onchain = dc.fill_onchain(OnchainStore(data / "onchain"))
        return {"to": done[-1], "klines": klines, "futures": futures, "onchain": onchain}

    def build(bench: Any) -> None:
        spot = BinanceSpot(KlineStore(Path(bench.data_dir) / "klines"), fetch_missing=False)
        build_windows(bench, spot, windows_dir=args.windows_dir, log=log)

    return {"fill": fill, "build": build}


def news_sources(args: argparse.Namespace, budget: Budget, log: Log) -> dict[str, Callable[..., Any]]:
    dn = load_script("discover_news")
    from rsi_arena.alpaca import AlpacaBars, AlpacaData, AlpacaNews, BarStore, is_weekday
    from rsi_arena.topics.news_equity.windows import build_windows

    client = AlpacaData()
    if not client.has_credentials:
        raise RollError("no Alpaca keys in the environment (APCA_API_KEY_ID, APCA_API_SECRET_KEY)")
    # A dry run reads through a store that writes nothing, so the liquidity
    # screen's daily bars do not land in benchmarks/ on a run that promised
    # not to touch it.
    store = BarStore(None if args.dry_run else Path(args.data_dir) / "bars")
    bars = AlpacaBars(client, store)
    news = AlpacaNews(client)
    universe = dn.read_universe(args.universe)

    def discover(start: date, end: date) -> tuple[list[Any], bool]:
        found: list[Any] = []
        for day in days_between(start, end):
            if not is_weekday(day):
                continue
            if budget.expired():
                log(f"  time budget spent before {day}; keeping what was found")
                return found, True
            rows, rejects, _ = with_retries(
                lambda: dn.discover(universe, bars, news, day, day,
                                    min_dollar_volume=args.min_dollar_volume,
                                    per_symbol_day=args.per_symbol_day, log=lambda m: None),
                log=log, what=f"news {day}")
            log(f"  {day}: {len(rows)} items kept on {len({r.symbol for r in rows})} names; "
                + ", ".join(f"{n} {why}" for why, n in rejects.most_common(3)))
            found.extend(rows)
        return found, False

    def fill(items: list[Any]) -> None:
        with_retries(dn.fill_bars, bars, items, log=log, what="bars")

    def build(items: list[Any]) -> None:
        build_windows(items, bars=bars, horizon=NEWS_HORIZON, windows_dir=args.windows_dir, log=log)

    return {"discover": discover, "fill": fill, "build": build}


#: How each topic's roll reaches its venue. A test replaces an entry with fakes.
SOURCES: dict[str, Callable[[argparse.Namespace, Budget, Log], dict[str, Callable[..., Any]]]] = {
    KALSHI: kalshi_sources, CRYPTO: crypto_sources, NEWS: news_sources}


def roll_topic(args: argparse.Namespace, budget: Budget, log: Log) -> Roll:
    src = SOURCES[args.topic](args, budget, log)
    if args.topic == KALSHI:
        return roll_kalshi(Path(args.benchmark), Path(args.windows_dir), keep=args.keep, until=args.until,
                           dry_run=args.dry_run, discover=src["discover"], build=src["build"],
                           quote=src["quote"], fill_paths=src.get("fill_paths"), log=log)
    if args.topic == CRYPTO:
        return roll_crypto(Path(args.benchmark), Path(args.windows_dir), keep=args.keep, until=args.until,
                           dry_run=args.dry_run, fill=src["fill"], build=src["build"], log=log)
    return roll_news(Path(args.benchmark), Path(args.windows_dir), keep=args.keep, until=args.until,
                     dry_run=args.dry_run, discover=src["discover"], fill=src["fill"],
                     build=src["build"], log=log)


# -- validation, reverting, and the loop guard ---------------------------------------

def topic_paths(args: argparse.Namespace) -> list[str]:
    """What a roll of this topic may have changed, and what a revert restores."""
    out = [str(args.benchmark), str(args.windows_dir)]
    if args.data_dir:
        out.append(str(args.data_dir))
    return out


def validate(topic: str, *, benchmark: str | None = None, windows_dir: str | None = None,
             run: Callable[..., Any] = subprocess.run,
             log: Log = print) -> tuple[bool, str, dict[str, Any]]:
    """The set still builds and still fits the topic's split. Both commands are
    offline; the first one also builds any window file the roll left unbuilt.

    ``preflight.py`` is given the same flags ``loop.yml`` gives it, minus the
    generation: the check that matters here is "groups fill the split", which
    is audit + held-out + probe against what the set now holds, and it does not
    move with the epoch. The rest of the flags come from the topic's spec, as
    they do in the workflow, so a roll is judged by the numbers a generation
    will be run with rather than by defaults this script invented.
    """
    here = Path(__file__).resolve().parent
    where: list[str] = []
    if benchmark:
        where += ["--benchmark", str(benchmark)]
    if windows_dir:
        where += ["--windows-dir", str(windows_dir)]
    windows_cmd = [sys.executable, "-m", "rsi_arena.cli", "windows", "--topic", topic, *where, "--json"]
    preflight_cmd = [sys.executable, str(here / "preflight.py"), "--topic", topic, *where]
    summary: dict[str, Any] = {}
    for cmd in (windows_cmd, preflight_cmd):
        name = Path(cmd[1]).name if cmd[1].endswith(".py") else cmd[3]
        p = run(cmd, capture_output=True, text=True)
        if p.returncode != 0:
            tail = "\n".join((p.stderr or p.stdout or "").strip().splitlines()[-8:])
            return False, f"{name} exited {p.returncode}: {tail}", summary
        if cmd is windows_cmd:
            try:
                report = json.loads(p.stdout)
                summary = {"instances": report.get("instances"), "train": report.get("train"),
                           "holdout": report.get("holdout"), "groups": len(report.get("groups", []))}
            except (ValueError, TypeError):
                summary = {}
        log(f"  {name}: ok")
    return True, "", summary


def revert(paths: list[str], *, run: Callable[..., Any] = subprocess.run, log: Log = print) -> None:
    """The topic's paths as the checkout had them: tracked files restored, new ones removed."""
    run(["git", "checkout", "--", *paths], capture_output=True, text=True)
    run(["git", "clean", "-fdq", "--", *paths], capture_output=True, text=True)
    log(f"  reverted {', '.join(paths)}")


def loop_runs(*, run: Callable[..., Any] = subprocess.run) -> list[str]:
    """Loop workflow runs that are queued or in progress, by id, from the GitHub API."""
    out: list[str] = []
    for status in ("in_progress", "queued"):
        p = run(["gh", "run", "list", "--workflow", "loop.yml", "--status", status,
                 "--json", "databaseId,status", "--limit", "20"], capture_output=True, text=True)
        if p.returncode != 0:
            raise RollError(f"gh run list failed: {(p.stderr or '').strip()[:200]}")
        out += [f"{r.get('databaseId')} ({r.get('status')})" for r in json.loads(p.stdout or "[]")]
    return out


def wait_for_loop(*, minutes: float, poll_s: float = 120.0, runs: Callable[[], list[str]] = loop_runs,
                  sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
                  log: Log = print) -> bool:
    """True once no loop run is queued or in progress; False after ``minutes`` of one being so.

    Any loop run counts, whichever topic it is for: the topic is resolved
    inside the job and is not visible from the run list, and the cost of
    waiting on another topic's generation is at most this wait.
    """
    deadline = clock() + minutes * 60.0
    while True:
        try:
            busy = runs()
        except RollError as exc:
            log(f"  could not ask GitHub about the loop ({exc}); going ahead")
            return True
        if not busy:
            return True
        if clock() >= deadline:
            log(f"  the loop is still running after {minutes:g} minutes: {', '.join(busy)}")
            return False
        log(f"  loop run(s) {', '.join(busy)} in progress; checking again in {poll_s:.0f}s")
        sleep(poll_s)


# -- main ----------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--topic", choices=[*sorted(TOPICS), ALL],
                    help="which question set to roll, or 'all' for every topic in turn")
    ap.add_argument("--keep", type=int, default=None,
                    help="groups kept after the roll: matches, UTC days or symbol-days; "
                         f"defaults {KEEP}")
    ap.add_argument("--until", default=None, help="last day to add, ISO; default yesterday (UTC)")
    ap.add_argument("--dry-run", action="store_true", help="discover in memory, print, write nothing")
    ap.add_argument("--budget-minutes", type=float, default=BUDGET_MINUTES,
                    help="minutes of discovery before the roll keeps what it has")
    ap.add_argument("--validate", action="store_true",
                    help="after the roll, run `rsi-arena windows --json` and scripts/preflight.py; "
                         "revert the topic's paths in git if either fails")
    ap.add_argument("--wait-for-loop", type=float, default=0.0, metavar="MINUTES",
                    help="first wait up to this long for loop.yml runs to finish (needs gh); "
                         "still busy after that, do nothing and exit 0")
    ap.add_argument("--wait-only", action="store_true",
                    help="only do the wait, and write busy=true|false to $GITHUB_OUTPUT")
    ap.add_argument("--summary", default="", help="write the roll's summary JSON here")
    ap.add_argument("--benchmark", default=None, help="override the topic's benchmark file")
    ap.add_argument("--windows-dir", default=None, help="override the topic's windows directory")
    ap.add_argument("--data-dir", default=None, help="override where venue data lives")
    ap.add_argument("--limit", type=int, default=600, help="kalshi: settled events read per league")
    ap.add_argument("--universe", default="benchmarks/universe-us.txt", help="news: the symbols screened")
    ap.add_argument("--min-dollar-volume", type=float, default=5e7, help="news: the liquidity screen")
    ap.add_argument("--per-symbol-day", type=int, default=4, help="news: items kept per name per day")
    return ap


def _resolve_args(args: argparse.Namespace) -> argparse.Namespace:
    spec = spec_of(args.topic)
    args.benchmark = args.benchmark or spec.benchmark
    args.windows_dir = args.windows_dir or spec.windows_dir
    args.data_dir = args.data_dir or DATA_DIRS.get(args.topic)
    args.keep = KEEP[args.topic] if args.keep is None else args.keep
    args.until = date.fromisoformat(args.until) if args.until else yesterday()
    return args


def _write_summary(path: str, payload: Any) -> None:
    if path:
        write_atomic(Path(path), json.dumps(payload, indent=1) + "\n")


def roll_one(args: argparse.Namespace, log: Log = print) -> Roll:
    """One topic, from its benchmark through validation. Never raises for a
    venue's sake: a failure is a :class:`Roll` with ``status`` ``failed``, so a
    run of ``--topic all`` goes on to the next topic and the caller still gets
    a row for this one."""
    args = _resolve_args(argparse.Namespace(**vars(args)))
    budget = Budget(args.budget_minutes)
    log(f"rolling {args.topic} through {args.until}, keeping {args.keep}"
        + (" [dry run]" if args.dry_run else ""))
    try:
        roll = roll_topic(args, budget, log)
    except Exception as exc:  # noqa: BLE001 - one topic's failure is a row, not a crash
        roll = Roll(args.topic, status="failed", reason=f"{type(exc).__name__}: {exc}",
                    dry_run=args.dry_run)
        print(f"{args.topic}: FAILED {exc}", file=sys.stderr)
        # A RollError is the roll's own alarm - a window that should not have
        # changed did, or the set names no league - so the checkout goes back.
        # Anything else is the venue or the machine giving out partway, and
        # what is on disk then is a consistent prefix worth keeping: a fixture
        # whose windows are not built yet is built by the next `rsi-arena
        # windows`, and throwing the week's fetch away would only mean
        # fetching it again.
        if isinstance(exc, RollError) and args.validate and not args.dry_run:
            revert(topic_paths(args), log=log)
        return roll

    if args.validate and not args.dry_run and roll.status in ("ok", "budget"):
        ok, why, summary = validate(args.topic, benchmark=args.benchmark,
                                    windows_dir=args.windows_dir, log=log)
        if ok:
            roll.windows = summary
        else:
            revert(topic_paths(args), log=log)
            roll.status, roll.reason = "reverted", f"roll reverted: {why}"
    log(roll.line())
    if roll.added:
        log("  added:   " + ", ".join(roll.added[:12]) + (" ..." if len(roll.added) > 12 else ""))
    if roll.removed:
        log("  removed: " + ", ".join(roll.removed[:12]) + (" ..." if len(roll.removed) > 12 else ""))
    return roll


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    log = print

    if args.wait_for_loop or args.wait_only:
        free = wait_for_loop(minutes=args.wait_for_loop or 30.0, log=log)
        emit = os.environ.get("GITHUB_OUTPUT")
        if emit:
            with open(emit, "a") as fh:
                fh.write(f"busy={'false' if free else 'true'}\n")
        if args.wait_only:
            print("the loop is idle" if free else "the loop is busy")
            return 0
        if not free:
            roll = Roll(args.topic or "-", status="skipped",
                        reason="a loop run was still in progress; rolling nothing this week")
            print(roll.line())
            _write_summary(args.summary, roll.to_dict())
            return 0

    if not args.topic:
        print("--topic is required", file=sys.stderr)
        return 2
    if args.topic == ALL and (args.benchmark or args.windows_dir or args.data_dir):
        print("--benchmark, --windows-dir and --data-dir name one topic's files; "
              "they cannot be given with --topic all", file=sys.stderr)
        return 2

    topics = sorted(TOPICS) if args.topic == ALL else [args.topic]
    rolls = [roll_one(argparse.Namespace(**{**vars(args), "topic": t}), log) for t in topics]
    if len(rolls) > 1:
        log("")
        for r in rolls:
            log("  " + r.line())
    _write_summary(args.summary, rolls[0].to_dict() if len(rolls) == 1
                   else [r.to_dict() for r in rolls])
    # Nothing to add is success. Every topic failing is not; one of several is
    # the workflow's to judge, and it has a row per topic to judge it from.
    return 1 if all(r.status == "failed" for r in rolls) else 0


if __name__ == "__main__":
    raise SystemExit(main())
