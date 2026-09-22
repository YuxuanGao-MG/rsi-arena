"""Find news items on liquid US names that can be replayed, and write a benchmark.

An item qualifies when every piece a replay needs exists: the symbol clears a
dollar-volume screen on the sessions before the period, the item broke in
regular hours far enough from the open and the close, its headline is not a
repeat of one on the same name in the last half hour, it is at least ten
minutes from the last item kept on that name, and the name has not already
given its share of the day. Anything rejected is counted by reason, because
a question set that quietly shrinks is a moving exam.

The bars each kept item needs are fetched into the bar store as part of
discovery, so that ``rsi-arena windows`` and every replay after it run with
no key and no network: the item's day and the ten sessions before it for
the name, the day for SPY and QQQ, and twenty daily bars for the day's
context.

    python scripts/discover_news.py --universe benchmarks/universe-us.txt \\
        --from 2026-06-01 --to 2026-09-15 --min-dollar-volume 5e7 \\
        --per-symbol-day 4 --out benchmarks/news-2026-09.json

``--dry-run`` runs the same pipeline against a synthetic tape and news feed,
writes to a scratch directory, and prints the command above. It is how this
script was known to work before a key existed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import sys
import tempfile
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rsi_arena.alpaca import (AlpacaBars, AlpacaData, AlpacaNews, BarStore, NewsItem,  # noqa: E402
                              in_window, is_weekday, ny_date, prior_weekdays, session_close,
                              session_open)
from rsi_arena.topics.news_equity.windows import BenchmarkItem, build_windows  # noqa: E402

UTC = timezone.utc
INDEX = ("SPY", "QQQ")
DEDUPE_MINUTES = 30
GAP_MINUTES = 10
LIQUIDITY_SESSIONS = 20
LOOKBACK_SESSIONS = 10
#: Weekdays fetched to be sure of ten sessions past any holiday run.
LOOKBACK_WEEKDAYS = 14


def read_universe(path: str | Path) -> list[str]:
    out: list[str] = []
    for line in Path(path).read_text().splitlines():
        s = line.split("#", 1)[0].strip().upper()
        if s and s not in out:
            out.append(s)
    return out


def normalise(headline: str) -> str:
    """The headline as a repeat is recognised: lower case, digits stripped,
    whitespace collapsed. "Apple rises 3%" and "Apple rises 4%" are one story."""
    return re.sub(r"\s+", " ", re.sub(r"\d+", "", headline.lower())).strip()


def liquidity(bars: Any, symbol: str, on: date, sessions: int = LIQUIDITY_SESSIONS) -> float:
    """Median dollar volume over the sessions before ``on``. Zero without bars."""
    rows = bars.daily(symbol, before_date=on, days=sessions)
    dollars = sorted(b.c * b.v for b in rows)
    return dollars[len(dollars) // 2] if dollars else 0.0


def select(candidates: list[tuple[str, NewsItem]], per_symbol_day: int,
           rejects: Counter, examples: dict[str, str]) -> list[BenchmarkItem]:
    """The items kept, and why the others were not."""
    by_symbol: dict[str, list[NewsItem]] = {}
    for symbol, item in candidates:
        by_symbol.setdefault(symbol, []).append(item)
    rows: list[BenchmarkItem] = []
    for symbol in sorted(by_symbol):
        seen: list[tuple[str, datetime]] = []
        last_kept: datetime | None = None
        kept_day: dict[date, list[BenchmarkItem]] = {}
        for item in sorted(by_symbol[symbol], key=lambda i: i.created_at):
            def reject(why: str) -> None:
                rejects[why] += 1
                examples.setdefault(why, f"{symbol} {item.created_at.isoformat()} {item.headline[:60]}")
            if not in_window(item.created_at):
                reject("outside regular hours")
                continue
            key = normalise(item.headline)
            if any(k == key and item.created_at - when <= timedelta(minutes=DEDUPE_MINUTES)
                   for k, when in seen):
                reject("duplicate headline within 30 minutes")
                continue
            seen.append((key, item.created_at))
            if last_kept is not None and item.created_at - last_kept < timedelta(minutes=GAP_MINUTES):
                reject("within 10 minutes of a kept item on the name")
                continue
            last_kept = item.created_at
            kept_day.setdefault(ny_date(item.created_at), []).append(BenchmarkItem(
                symbol=symbol, news_id=item.id, at=item.created_at, headline=item.headline,
                summary=item.summary, source=item.source, symbols=item.symbols,
                updated_at=item.updated_at))
        for day in sorted(kept_day):
            day_rows = kept_day[day]
            if per_symbol_day > 0 and len(day_rows) > per_symbol_day:
                step = len(day_rows) / per_symbol_day
                chosen = [day_rows[int(i * step)] for i in range(per_symbol_day)]
                rejects["over the per-symbol-day cap"] += len(day_rows) - len(chosen)
                examples.setdefault("over the per-symbol-day cap",
                                    f"{symbol} {day.isoformat()}: {len(day_rows)} items")
                day_rows = chosen
            rows.extend(day_rows)
    rows.sort(key=lambda r: (r.at, r.symbol))
    return rows


def fill_bars(bars: AlpacaBars, rows: list[BenchmarkItem], log: Any = print) -> int:
    """Everything a replay of these rows reads, into the store. Returns day fetches."""
    fetched = 0
    days_by_symbol: dict[str, set[date]] = {}
    for r in rows:
        days_by_symbol.setdefault(r.symbol, set()).add(ny_date(r.at))
    index_days: set[date] = set()
    for symbol in sorted(days_by_symbol):
        wanted: set[date] = set()
        for day in days_by_symbol[symbol]:
            wanted.add(day)
            wanted.update(prior_weekdays(day, LOOKBACK_WEEKDAYS))
            index_days.add(day)
        n = bars.ensure_days(symbol, sorted(wanted))
        for day in sorted(days_by_symbol[symbol]):
            bars.daily(symbol, before_date=day, days=LIQUIDITY_SESSIONS)
        fetched += n
        log(f"  bars  {symbol:6} {len(wanted):>3} days wanted, {n:>3} fetched")
    for symbol in INDEX:
        n = bars.ensure_days(symbol, sorted(index_days))
        fetched += n
        log(f"  bars  {symbol:6} {len(index_days):>3} days wanted, {n:>3} fetched")
    return fetched


def discover(universe: list[str], bars: AlpacaBars, news: AlpacaNews, start: date, end: date, *,
             min_dollar_volume: float, per_symbol_day: int, batch: int = 25,
             log: Any = print) -> tuple[list[BenchmarkItem], Counter, dict[str, str]]:
    rejects: Counter = Counter()
    examples: dict[str, str] = {}
    kept_symbols: list[str] = []
    for symbol in universe:
        try:
            median = liquidity(bars, symbol, start)
        except Exception as exc:
            rejects["no daily bars"] += 1
            examples.setdefault("no daily bars", f"{symbol}: {type(exc).__name__}: {exc}")
            continue
        if median < min_dollar_volume:
            rejects["below the dollar-volume screen"] += 1
            examples.setdefault("below the dollar-volume screen", f"{symbol}: ${median:,.0f} a session")
            continue
        kept_symbols.append(symbol)
    log(f"{len(kept_symbols)} of {len(universe)} symbols clear ${min_dollar_volume:,.0f} a session")

    wanted = set(kept_symbols)
    since = datetime.combine(start, time(0), tzinfo=UTC)
    until = datetime.combine(end, time(23, 59, 59), tzinfo=UTC)
    candidates: list[tuple[str, NewsItem]] = []
    seen_ids: set[tuple[str, str]] = set()
    for i in range(0, len(kept_symbols), batch):
        chunk = kept_symbols[i:i + batch]
        items = news.items(chunk, since, until, limit=50, sort="asc")
        for item in items:
            for symbol in item.symbols:
                if symbol in wanted and (symbol, item.id) not in seen_ids:
                    seen_ids.add((symbol, item.id))
                    candidates.append((symbol, item))
        log(f"  news  {chunk[0]}..{chunk[-1]}: {len(items)} items")
    log(f"{len(candidates)} symbol-item pairs on {len(kept_symbols)} names")
    rows = select(candidates, per_symbol_day, rejects, examples)
    return rows, rejects, examples


# -- a synthetic venue, for the dry run --------------------------------------------

class _DryRunClient:
    """Answers the three endpoints from a tape it makes up, in the API's own
    shapes, so the real ``AlpacaBars`` and ``AlpacaNews`` run over it."""

    def __init__(self, symbols: list[str], seed: int = 0) -> None:
        self.symbols, self.seed = symbols, seed
        self.requests = 0

    def _rng(self, *key: Any) -> random.Random:
        digest = hashlib.sha256(f"{self.seed}:{':'.join(map(str, key))}".encode()).hexdigest()
        return random.Random(int(digest[:12], 16))

    def _price(self, symbol: str) -> float:
        return 50 + (sum(map(ord, symbol)) % 400)

    def _day_bars(self, symbol: str, day: date) -> list[dict[str, Any]]:
        if not is_weekday(day):
            return []
        rng = self._rng(symbol, day)
        price = self._price(symbol) * (1 + 0.02 * (rng.random() - 0.5))
        out = []
        t = session_open(day)
        close = session_close(day)
        while t < close:
            price *= 1 + rng.gauss(0, 0.0004)
            out.append({"t": t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "o": round(price, 2), "h": round(price * 1.0003, 2), "l": round(price * 0.9997, 2),
                        "c": round(price, 2), "v": rng.randint(500, 5000), "n": rng.randint(5, 50),
                        "vw": round(price, 3)})
            t += timedelta(minutes=1)
        return out

    def collect(self, path: str, key: str, params: dict[str, Any], *, max_items: int | None = None) -> Any:
        self.requests += 1
        start = datetime.fromisoformat(params["start"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(params["end"].replace("Z", "+00:00"))
        if path == "/v2/stocks/bars":
            symbol = params["symbols"]
            rows: list[dict[str, Any]] = []
            d = start.date()
            while d <= end.date():
                if params.get("timeframe") == "1Day":
                    bars = self._day_bars(symbol, d)
                    if bars:
                        rows.append({"t": datetime.combine(d, time(4), tzinfo=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                     "o": bars[0]["o"], "h": max(b["h"] for b in bars),
                                     "l": min(b["l"] for b in bars), "c": bars[-1]["c"],
                                     "v": sum(b["v"] for b in bars), "n": sum(b["n"] for b in bars),
                                     "vw": bars[-1]["vw"]})
                else:
                    rows.extend(b for b in self._day_bars(symbol, d)
                                if start <= datetime.fromisoformat(b["t"].replace("Z", "+00:00")) <= end)
                d += timedelta(days=1)
            return {symbol: rows}
        if path == "/v1beta1/news":
            wanted = params["symbols"].split(",")
            items: list[dict[str, Any]] = []
            d = start.date()
            while d <= end.date():
                for symbol in wanted:
                    rng = self._rng("news", symbol, d)
                    for k in range(rng.randint(2, 5)):
                        # Some in hours, some out, and one repeat in the batch.
                        hour, minute = rng.choice([8, 9, 10, 11, 13, 14, 15, 17]), rng.randint(0, 59)
                        when = datetime.combine(d, time(hour, minute, rng.randint(0, 59)),
                                                tzinfo=session_open(d).tzinfo).astimezone(UTC)
                        if not (start <= when <= end):
                            continue
                        headline = (f"{symbol} shares move {rng.randint(1, 9)}% as analyst weighs in"
                                    if k % 2 else f"{symbol} announces update {rng.randint(1, 99)}")
                        items.append({"id": f"{symbol}-{d.isoformat()}-{k}", "headline": headline,
                                      "summary": "synthetic", "created_at": when.isoformat(),
                                      "updated_at": (when + timedelta(minutes=rng.choice([0, 0, 45]))).isoformat(),
                                      "symbols": [symbol] + ([wanted[0]] if symbol != wanted[0] and rng.random() < 0.3 else []),
                                      "source": "benzinga", "url": ""})
                d += timedelta(days=1)
            items.sort(key=lambda i: i["created_at"], reverse=(params.get("sort") == "desc"))
            return items[:max_items] if max_items else items
        if path.startswith("/v2/stocks/") and path.endswith("/trades"):
            return []
        raise ValueError(f"the dry run answers no such endpoint: {path}")


def real_command(args: argparse.Namespace) -> str:
    return (f"python scripts/discover_news.py --universe {args.universe} --from {args.start} "
            f"--to {args.end} --min-dollar-volume {args.min_dollar_volume:g} "
            f"--per-symbol-day {args.per_symbol_day} --out {args.out or 'benchmarks/news-2026-09.json'}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--universe", default="benchmarks/universe-us.txt")
    ap.add_argument("--from", dest="start", default="2026-06-01", help="first day, ISO")
    ap.add_argument("--to", dest="end", default="2026-09-15", help="last day, ISO")
    ap.add_argument("--min-dollar-volume", type=float, default=5e7,
                    help="median dollar volume over the twenty sessions before --from")
    ap.add_argument("--per-symbol-day", type=int, default=4, help="items kept per name per day; 0 keeps all")
    ap.add_argument("--out", default="", help="write the benchmark here")
    ap.add_argument("--data-dir", default="benchmarks/news-data",
                    help="where the bar store lives; the replay reads it")
    ap.add_argument("--dry-run", action="store_true",
                    help="run against a synthetic tape in a scratch directory; no key, no network")
    args = ap.parse_args(argv)
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)

    if args.dry_run:
        scratch = Path(tempfile.mkdtemp(prefix="discover-news-"))
        out = Path(args.out) if args.out else scratch / "news.json"
        data_dir = Path(args.data_dir) if args.data_dir != "benchmarks/news-data" else scratch / "news-data"
        universe = ["AAA", "BBB", "CCC", "DDD", *INDEX]
        client: Any = _DryRunClient(universe)
        if end - start > timedelta(days=6):
            end = start + timedelta(days=6)
        print(f"DRY RUN: a synthetic tape for {', '.join(universe)} from {start} to {end}, "
              f"written under {scratch}")
    else:
        out = Path(args.out) if args.out else None
        data_dir = Path(args.data_dir)
        universe = read_universe(args.universe)
        client = AlpacaData()
        if not client.has_credentials:
            print("no Alpaca keys in the environment: set APCA_API_KEY_ID and APCA_API_SECRET_KEY "
                  "(a free account gives the IEX feed and the news feed), then run:")
            print(f"  {real_command(args)}")
            return 2
    bars = AlpacaBars(client, BarStore(data_dir / "bars"))
    news = AlpacaNews(client)

    rows, rejects, examples = discover(universe, bars, news, start, end,
                                       min_dollar_volume=args.min_dollar_volume,
                                       per_symbol_day=args.per_symbol_day)
    print(f"\n{len(rows)} items kept on {len({r.symbol for r in rows})} names, "
          f"{len({r.group for r in rows})} symbol-days")
    for why, n in rejects.most_common():
        print(f"  reject  {n:>5}  {why}  e.g. {examples.get(why, '')}")

    print("\nfilling the bar store")
    fetched = fill_bars(bars, rows)
    print(f"  {fetched} days fetched, {client.requests} requests in all")

    # Built here only to say how many will build; `rsi-arena windows` writes
    # the question set itself, per group, under the topic's windows directory.
    windows = build_windows(rows, bars=bars, windows_dir=None, log=lambda m: None)
    print(f"\n{len(windows)} of {len(rows)} items build a window offline "
          f"({len({w.group for w in windows})} groups)")

    if out is not None and rows:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps([r.to_dict() for r in rows], indent=1) + "\n")
        print(f"wrote {out}")
    if args.dry_run:
        print("\nonce APCA_API_KEY_ID and APCA_API_SECRET_KEY are set, run:")
        print(f"  {real_command(args)}")
        print("then `python -m rsi_arena.cli windows --topic news-equity-5m` to build the question set.")
    return 0 if rows else 1


if __name__ == "__main__":
    raise SystemExit(main())
