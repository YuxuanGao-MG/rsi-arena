"""Ask the harness about stories as they break, and keep every trace.

The news benchmark scores a harness against a past it cannot see: the item's
instant is on disk and so is the bar five minutes on. This is the other
thing: the same harness, the same tools, pointed at an item that broke a
moment ago on a name that is still trading, scored five minutes later when
the bar prints. Nothing here feeds the gate - a live forecast is evidence
about the harness, not a promotion - but it is the only place the arena sees
a story before the tape has answered it.

Event-driven, not sampled. Every ``--poll`` seconds the feed is asked for
items on the universe since the last poll; each new one on a name in the
universe, inside the window a forecast may be asked in, gets one forecast at
the instant it was seen, and never a second. A burst is capped per poll
rather than chased, because a five-cent ceiling is the whole budget.

The box is ``live_tools`` - the frozen replay toolbox with the cache refused
- and the bars are read fresh for the day that is still printing. The
grading is ``topics/_common/live.py`` with this venue's two answers: the
bar at the horizon, and the score in basis points.

    python scripts/collect_live_news.py --minutes 45 --max-usd 0.05
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rsi_arena.alpaca import (HORIZON_MINUTES, AlpacaBars, AlpacaData, AlpacaNews,  # noqa: E402
                              BarStore, NewsItem, in_window, live_tools, session)
from rsi_arena.harness.llm import OpenRouter                      # noqa: E402
from rsi_arena.harness.runner import Runner                       # noqa: E402
from rsi_arena.harness.spec import Harness                        # noqa: E402
from rsi_arena.topics._common.live import report                  # noqa: E402
from rsi_arena.topics._common.live import resolve as _resolve     # noqa: E402
from rsi_arena.topics._common.live import write_resolved as _write_resolved  # noqa: E402
from rsi_arena.topics.news_equity import score_output             # noqa: E402

UTC = timezone.utc
TOPIC = "news-equity-5m"
VENUE = "alpaca-iex"
UNIT = "bps"
#: Symbols per request to the news feed, the batch discovery uses.
BATCH = 40
#: The feed is asked from a little before the last poll, so an item indexed
#: late is not missed; ``seen`` keeps the overlap from forecasting twice.
OVERLAP_S = 90


def read_universe(path: str | Path) -> list[str]:
    out: list[str] = []
    for line in Path(path).read_text().splitlines():
        s = line.split("#", 1)[0].strip().upper()
        if s and s not in out:
            out.append(s)
    return out


def instance_id(symbol: str, at: datetime, news_id: str) -> str:
    """The same shape as a benchmark window's id, so a live row and a replayed
    one on the same story read alike. The publisher stores it as the ticker."""
    return f"{symbol}@{at.isoformat()}#{news_id}"


def symbol_of(ticker: str) -> str:
    return ticker.split("@", 1)[0]


class LiveBars(AlpacaBars):
    """Bars for a day that is still printing.

    ``AlpacaBars`` memoises a day once fetched, which is right for settled
    history and wrong for today: a day read at 10:00 would still end at 10:00
    at 10:05, and every later price would be stale. Today is re-read after
    ``ttl`` seconds - within one forecast the tools share a read, across polls
    they see the new bars. Past days memoise as before.
    """

    def __init__(self, client: AlpacaData, ttl_s: int = 20) -> None:
        super().__init__(client, BarStore(None))
        self.ttl_s = ttl_s
        self._today: dict[str, tuple[float, list[Any]]] = {}

    def day(self, symbol: str, day: date) -> list[Any]:
        if day < datetime.now(UTC).date():
            return super().day(symbol, day)
        key = symbol.upper()
        hit = self._today.get(key)
        if hit is not None and time.monotonic() - hit[0] < self.ttl_s:
            return hit[1]
        bars = self.fetch_day(symbol, day)
        self._today[key] = (time.monotonic(), bars)
        return bars


def new_items(news: Any, universe: list[str], since: datetime, until: datetime,
              seen: set[tuple[str, str]], batch: int = BATCH) -> list[tuple[str, NewsItem]]:
    """Every (symbol, item) pair on the universe in ``[since, until]`` not seen before.

    An item names several symbols; each one in the universe is its own
    forecast, as it is in the benchmark, and each is forecast once.
    """
    wanted = set(universe)
    out: list[tuple[str, NewsItem]] = []
    for i in range(0, len(universe), batch):
        chunk = universe[i:i + batch]
        for item in news.items(chunk, since, until, limit=50, sort="asc"):
            for symbol in item.symbols:
                if symbol in wanted and (symbol, item.id) not in seen:
                    seen.add((symbol, item.id))
                    out.append((symbol, item))
    return out


async def one(harness: Harness, llm: Any, bars: Any, news: Any, symbol: str, item: NewsItem,
              at: datetime) -> dict:
    """One forecast on a story that just broke, with the trace that produced it."""
    price = bars.price_at(symbol, at)
    if price is None:
        return {"symbol": symbol, "news_id": item.id, "skipped": "no fresh bar on the name"}
    # live_tools, not replay_tools: the disk cache behind the frozen tools has
    # no TTL because settled history cannot change, which is not true of a
    # name that is still trading.
    box = live_tools(at, bars, news, item=item, symbols_in_story=item.symbols)
    story = {"headline": item.headline, "summary": item.summary[:600], "source": item.source,
             "symbols": list(item.symbols), "at": item.created_at.isoformat()}
    run = await Runner(llm, box).run(harness, question=symbol, news=json.dumps(story)[:1200],
                                     context=json.dumps(session(at)))
    return {"at": at.isoformat(), "topic": TOPIC, "symbol": symbol, "venue": VENUE, "unit": UNIT,
            "ticker": instance_id(symbol, at, item.id), "news_id": item.id,
            "league": None, "game_id": None, "game": None,
            "mid_now": price, "context": item.to_dict(), "harness": harness.name,
            "output": run.output, "run": run.to_dict(), "ok": run.ok, "error": run.error}


async def sweep(harness: Harness, llm: Any, bars: Any, news: Any, universe: list[str],
                since: datetime, now: datetime, seen: set[tuple[str, str]], *,
                max_forecasts: int = 8, log: Any = print) -> list[dict]:
    """Forecast each new item once. Returns the rows made, skips included as sentences."""
    rows: list[dict] = []
    for symbol, item in new_items(news, universe, since, now, seen):
        if not in_window(item.created_at) or not in_window(now):
            log(f"  {symbol} {item.id}: outside the window a forecast may be asked in")
            continue
        if len(rows) >= max_forecasts:
            log(f"  {symbol} {item.id}: over this poll's cap of {max_forecasts}; not chased")
            continue
        if getattr(llm, "over_budget", False):
            break
        row = await one(harness, llm, bars, news, symbol, item, now)
        if row.get("skipped"):
            log(f"  {symbol} {item.id}: {row['skipped']}")
            continue
        rows.append(row)
        said = (row.get("output") or {}).get("delta_bps")
        log(f"  {symbol} at {row['mid_now']:.2f} -> {said:+.1f} bps  {item.headline[:60]}"
            if said is not None else f"  {symbol}: no forecast ({row.get('error')})")
    return rows


def realised_for(bars: Any):
    """This venue's answer to "what did the name print at the horizon"."""
    return lambda ticker, at: bars.realised_price(symbol_of(ticker), at, HORIZON_MINUTES)


def resolve(row: dict, bars: Any, now: datetime | None = None) -> bool:
    return _resolve(row, realised_fn=realised_for(bars), score_fn=score_output,
                    horizon_minutes=HORIZON_MINUTES, now=now)


def write_resolved(pending: list[dict], bars: Any, out: Path,
                   now: datetime | None = None) -> list[dict]:
    return _write_resolved(pending, out, realised_fn=realised_for(bars), score_fn=score_output,
                           horizon_minutes=HORIZON_MINUTES, now=now)


async def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--universe", default="benchmarks/universe-us.txt")
    ap.add_argument("--harness", default="harnesses/news-equity-5m-jev.json")
    ap.add_argument("--minutes", type=int, default=45, help="how long to keep collecting")
    ap.add_argument("--poll", type=int, default=60, help="seconds between polls of the feed")
    ap.add_argument("--max-per-sweep", type=int, default=8, help="forecasts per poll")
    ap.add_argument("--window-usd", type=float, default=0.0,
                    help="per-forecast ceiling; 0 keeps the harness file's")
    ap.add_argument("--max-usd", type=float, default=0.05,
                    help="ceiling on this invocation's model spend; 0 removes it")
    ap.add_argument("--out", default="runs/live/news-forecasts.jsonl")
    args = ap.parse_args(argv)

    client = AlpacaData()
    if not client.has_credentials:
        # Not a failure: the topic is not switched on. A sentence, not a
        # traceback, because this runs on a cron and a traceback opens an issue.
        print("no Alpaca keys in the environment (APCA_API_KEY_ID, APCA_API_SECRET_KEY); "
              "nothing to collect from")
        report(["### Live news collection", "- skipped: no Alpaca keys in the environment"])
        return 0
    bars, news = LiveBars(client), AlpacaNews(client)
    universe = read_universe(args.universe)
    harness = Harness.load(args.harness)
    if args.window_usd:
        harness.config.max_usd = args.window_usd
    llm = OpenRouter(budget_usd=args.max_usd or None)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    deadline = time.time() + args.minutes * 60
    seen: set[tuple[str, str]] = set()
    pending: list[dict] = []
    made = 0
    since = datetime.now(UTC) - timedelta(seconds=OVERLAP_S)
    #: Consecutive runs that died in the provider: three is an exhausted key,
    #: and forty minutes of retrying it writes forty identical errors.
    provider_failures = 0
    starved = False
    feed_failures = 0

    try:
        while time.time() < deadline:
            now = datetime.now(UTC)
            if not in_window(now):
                print(f"  {now.isoformat()[11:19]}Z: outside regular hours; waiting")
            else:
                try:
                    rows = await sweep(harness, llm, bars, news, universe, since, now, seen,
                                       max_forecasts=args.max_per_sweep)
                except Exception as exc:  # noqa: BLE001 - one poll, not the invocation
                    feed_failures += 1
                    print(f"::warning title=poll failed::{type(exc).__name__}: {exc}")
                    rows = []
                    if feed_failures >= 3:
                        print("::notice title=nothing to collect::three polls in a row failed; stopping")
                        break
                else:
                    feed_failures = 0
                for row in rows:
                    kind = (row.get("run") or {}).get("error_kind")
                    provider_failures = provider_failures + 1 if kind == "provider" else 0
                    pending.append(row)
                    made += 1
                if provider_failures >= 3:
                    print("::notice title=nothing to collect::three model calls in a row failed in "
                          "the provider; stopping")
                    starved = True
                    break
            since = now - timedelta(seconds=OVERLAP_S)
            pending = write_resolved(pending, bars, out)
            if llm.over_budget:
                print(f"  ${llm.spent_usd:.4f} spent of ${args.max_usd:.2f}; stopping early")
                break
            await asyncio.sleep(args.poll)

        # The last poll's forecasts have not met their horizon yet. Wait it out
        # rather than throw away work already paid for.
        while pending:
            await asyncio.sleep(30)
            pending = write_resolved(pending, bars, out)
    finally:
        await llm.close()

    print(f"\n{made} forecasts written to {out}, ${llm.spent_usd:.4f} spent, "
          f"{len(seen)} symbol-items seen")
    lines = ["### Live news collection",
             f"- {made} forecasts on {len(universe)} names, {len(seen)} symbol-items seen",
             f"- ${llm.spent_usd:.4f} spent" + (f" of ${args.max_usd:.2f}" if args.max_usd else "")]
    if starved:
        lines.append("- stopped early: the model refused three calls in a row, which is what an "
                     "exhausted key looks like from here")
    elif llm.over_budget:
        lines.append(f"- stopped early: this invocation's ${args.max_usd:.2f} was spent")
    if feed_failures >= 3:
        lines.append("- stopped early: the feed refused three polls in a row")
    report(lines)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
