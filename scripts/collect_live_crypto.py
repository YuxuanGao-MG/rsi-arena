"""Ask the harness about the spot market as it trades, and keep every trace.

The replay benchmark scores a harness against a past it cannot see. This does
the other thing: it puts the same harness on BTC, ETH and SOL as they trade
now, keeps what it said and how it got there, and scores it a minute later
when the bar prints. Nothing here feeds the gate - a live forecast is
evidence about the harness, not a promotion - but it is the only place the
arena sees a price it has not already read the end of, and the only place it
sees the order book at all, which has no history and so no replay form.

Cadence-driven, because the market has no fixtures: a tick every ``--every``
seconds for ``--minutes``, and at each tick one forecast per symbol at
``at = now``, through :func:`rsi_arena.crypto.replay.live_tools` - the same
box the benchmark grades against, never cached, plus ``order_book`` and
``mempool_now``. The book at depth 20 and the mempool are written beside the
forecast, so what the harness saw is what a reader sees.

Every invocation carries a spend ceiling. On the Jev seed a forecast costs
about two thousandths of a cent, so the ceiling is about the runner's time
rather than the key; three consecutive provider errors stop the sweep green,
because a 402 answers every call the same way.

    python scripts/collect_live_crypto.py --minutes 25 --every 60 --max-usd 0.10
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rsi_arena.crypto._binance import BinanceSpot, KlineStore, close_at   # noqa: E402
from rsi_arena.crypto._futures import (FuturesStore, fetch_funding,     # noqa: E402
                                        fetch_open_interest, fetch_perp_candles)
from rsi_arena.crypto._onchain import SERIES, OnchainStore               # noqa: E402
from rsi_arena.crypto.replay import SYMBOLS, live_tools                 # noqa: E402
from rsi_arena.harness.llm import OpenRouter                             # noqa: E402
from rsi_arena.harness.runner import Runner                              # noqa: E402
from rsi_arena.harness.spec import Harness                               # noqa: E402
from rsi_arena.topics._common.live import report                         # noqa: E402
from rsi_arena.topics._common.live import resolve as _resolve            # noqa: E402
from rsi_arena.topics._common.live import write_resolved as _write_resolved  # noqa: E402
from rsi_arena.topics.crypto_horizon import CryptoHorizon, score_output  # noqa: E402
from rsi_arena.topics.crypto_horizon.windows import context_at           # noqa: E402

UTC = timezone.utc
TOPIC = CryptoHorizon.name
VENUE = "binance-spot"
BOOK_DEPTH = 20


class Sources:
    """The three feeds a live box reads, and how each is kept current.

    Bars come from the committed store where it has the day and from the
    exchange where it does not (``fetch_missing``), never written back. The
    perp series and the chain series are refreshed into a scratch store under
    ``runs/live`` at the start of a sweep, so the committed benchmark data is
    never touched by a collection.
    """

    def __init__(self, data_dir: Path, scratch: Path, symbols: tuple[str, ...]) -> None:
        self.symbols = symbols
        self.spot = BinanceSpot(KlineStore(data_dir / "klines"), fetch_missing=True)
        self.futures = FuturesStore(scratch)
        self.onchain = OnchainStore(scratch / "onchain")
        self._seed = data_dir

    def refresh(self, now: datetime) -> list[str]:
        """Bring the perp and chain series up to ``now``. Problems, as sentences."""
        problems: list[str] = []
        since = now - timedelta(days=2)
        for symbol in self.symbols:
            for kind, fetch in (("funding", fetch_funding), ("perp", fetch_perp_candles),
                                ("oi", lambda s, b, u: fetch_open_interest(s, b, u, period="5m"))):
                try:
                    self.futures.append(kind, symbol, fetch(symbol, since, None))
                except Exception as exc:  # noqa: BLE001 - one feed down is not the sweep down
                    problems.append(f"{symbol} {kind}: {type(exc).__name__}: {exc}")
        for name in SERIES:
            try:
                self.onchain.refresh(name)
            except Exception as exc:  # noqa: BLE001
                problems.append(f"onchain {name}: {type(exc).__name__}: {exc}")
        return problems

    def realised(self, ticker: str, at: datetime, horizon: int) -> float | None:
        """The close the exchange printed ``horizon`` minutes after ``at``, fetched fresh."""
        symbol = ticker.split("@", 1)[0]
        when = at + timedelta(minutes=horizon)
        try:
            bars = self.spot.fetch_klines(symbol, when - timedelta(minutes=4), when + timedelta(minutes=1))
        except Exception:  # noqa: BLE001 - reported as no quote, retried next pass
            return None
        k = close_at(bars, when)
        return None if k is None else k.close


def book_summary(book: dict[str, Any] | None) -> dict[str, Any] | None:
    """The part of a book worth a column: the touch, the spread, the lean."""
    if not book:
        return None
    return {k: book.get(k) for k in ("bid", "ask", "mid", "spread_bps", "bid_qty", "ask_qty",
                                     "imbalance", "levels")}


async def one(harness: Harness, llm: Any, symbol: str, at: datetime, sources: Any,
              horizon: int, mempool: dict[str, Any] | None = None) -> tuple[dict, dict | None]:
    """One forecast on one coin at ``at``, with the trace and the book it saw.

    Returns the forecast row and the book row, or a row with ``skipped`` set
    and no book when there was no complete bar to anchor the forecast on.
    """
    # live_tools, not replay_tools: the disk cache behind the frozen tools has
    # no TTL because settled history cannot change, which is not true of a
    # market that is still trading.
    box = live_tools(at, sources.spot, None, symbols=sources.symbols, futures=sources.futures,
                     onchain=sources.onchain, horizon=horizon)
    quote = box["market_quote"].safe_call(symbol=symbol)
    if not quote.ok:
        return {"symbol": symbol, "skipped": quote.error}, None
    depth = box["order_book"].safe_call(symbol=symbol, depth=BOOK_DEPTH)
    book = depth.data if depth.ok else None
    context = {**context_at(at), "book": book_summary(book),
               "mempool": mempool if mempool else None}
    inputs = {"question": symbol, "context": json.dumps(context_at(at))[:1200]}
    run = await Runner(llm, box).run(harness, **inputs)
    ticker = f"{symbol}@{at.isoformat()}"
    row = {"at": at.isoformat(), "topic": TOPIC, "symbol": symbol, "venue": VENUE,
           "ticker": ticker, "unit": CryptoHorizon.metric.unit, "horizon_minutes": horizon,
           "mid_now": quote.data["price"], "context": context, "harness": harness.name,
           "output": run.output, "run": run.to_dict(), "ok": run.ok, "error": run.error}
    snapshot = {"topic": TOPIC, "symbol": symbol, "at": at.isoformat(),
                "book": {"order_book": book, "mempool": mempool,
                         "last_close": quote.data["price"], "bar_close": quote.data["bar_close"],
                         "error": None if depth.ok else depth.error}}
    return row, snapshot


def _realised_for(sources: Any, horizon: int) -> Callable[[str, datetime], float | None]:
    return lambda ticker, at: sources.realised(ticker, at, horizon)


#: How far past the horizon a snapshot may be and still stand for its touch.
QUOTE_SLACK = timedelta(minutes=3)


def _quote_from_snapshots(snapshots: list[dict] | None, horizon: int
                          ) -> Callable[[str, datetime], dict | None] | None:
    """The touch nearest the horizon among the books this sweep saw, within
    three minutes after it; None when there is no such book. The order book
    has no history, so a sweep's own snapshots are the only place it is."""
    if not snapshots:
        return None

    def read(ticker: str, at: datetime) -> dict | None:
        symbol = ticker.split("@", 1)[0]
        target = at + timedelta(minutes=horizon)
        best, best_gap = None, None
        for snap in snapshots:
            if snap.get("symbol") != symbol:
                continue
            try:
                when = datetime.fromisoformat(str(snap["at"]))
            except (KeyError, ValueError):
                continue
            gap = when - target
            if gap < timedelta(0) or gap > QUOTE_SLACK:
                continue
            if best_gap is None or gap < best_gap:
                best, best_gap = snap, gap
        book = ((best or {}).get("book") or {}).get("order_book") or {}
        if not book or book.get("bid") is None or book.get("ask") is None:
            return None
        return {"bid": book["bid"], "ask": book["ask"], "mid": book.get("mid")}
    return read


def resolve(row: dict, sources: Any, horizon: int, snapshots: list[dict] | None = None) -> bool:
    """Score a forecast once its minute has printed. False while it has not."""
    return _resolve(row, realised_fn=_realised_for(sources, horizon), score_fn=score_output,
                    horizon_minutes=horizon, quote_fn=_quote_from_snapshots(snapshots, horizon))


def write_resolved(pending: list[dict], sources: Any, out: Path, horizon: int,
                   snapshots: list[dict] | None = None) -> list[dict]:
    """Write every forecast whose horizon has printed; keep the rest waiting.
    ``snapshots`` are this sweep's books, for the touch at the horizon."""
    return _write_resolved(pending, out, realised_fn=_realised_for(sources, horizon),
                           score_fn=score_output, horizon_minutes=horizon,
                           quote_fn=_quote_from_snapshots(snapshots, horizon))


def append_books(books: list[dict], out: Path) -> None:
    if not books:
        return
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a") as fh:
        for b in books:
            fh.write(json.dumps(b, default=str) + "\n")


async def sweep(harness: Harness, llm: Any, sources: Any, *, minutes: float, every: float,
                out: Path, books_out: Path, horizon: int, max_usd: float = 0.0,
                now: Callable[[], datetime] = lambda: datetime.now(UTC),
                sleep: Callable[[float], Any] = asyncio.sleep,
                mempool_fn: Callable[[], dict[str, Any] | None] | None = None,
                log: Callable[[str], None] = print) -> dict[str, Any]:
    """Ticks until ``minutes`` are up, then waits out the last forecasts' horizon."""
    deadline = now() + timedelta(minutes=minutes)
    pending: list[dict] = []
    #: Every book this sweep saw, so a forecast's exit can be priced at the
    #: touch showing a minute later rather than at a proxy spread.
    snapshots: list[dict] = []
    made, ticks = 0, 0
    #: Consecutive runs that died in the provider. Three is the signal that
    #: the key, not this invocation, is out of money.
    provider_failures = 0
    starved = False

    while now() < deadline:
        ticks += 1
        at = now()
        mempool = None
        if mempool_fn is not None:
            try:
                mempool = mempool_fn()
            except Exception:  # noqa: BLE001 - the mempool is a note, not the forecast
                mempool = None
        books: list[dict] = []
        for symbol in sources.symbols:
            if getattr(llm, "over_budget", False):
                break
            row, book = await one(harness, llm, symbol, at, sources, horizon, mempool)
            if row.get("skipped"):
                log(f"  {symbol}: skipped ({row['skipped']})")
                continue
            kind = (row.get("run") or {}).get("error_kind")
            provider_failures = provider_failures + 1 if kind == "provider" else 0
            pending.append(row)
            if book is not None:
                books.append(book)
            made += 1
            said = (row.get("output") or {}).get("delta_bps")
            log(f"  {symbol} at {row['mid_now']:,.2f} -> {said:+.1f} bps"
                if said is not None else f"  {symbol}: no forecast ({row.get('error')})")
            if provider_failures >= 3:
                log(f"::notice title=nothing to collect::three model calls in a row failed in "
                    f"the provider ({row.get('error')}); stopping")
                starved = True
                break
        append_books(books, books_out)
        snapshots.extend(books)
        pending = write_resolved(pending, sources, out, horizon, snapshots)
        if getattr(llm, "over_budget", False):
            log(f"  ${llm.spent_usd:.4f} spent of ${max_usd:.2f}; stopping early")
            break
        if starved:
            break
        # The next tick on the cadence, not ``every`` after this one finished.
        wait = (at + timedelta(seconds=every) - now()).total_seconds()
        if wait > 0:
            await sleep(wait)

    # The last tick's forecasts have not met their horizon yet. Wait it out
    # rather than throw away work already paid for.
    waited = 0
    while pending and waited < 20:
        await sleep(15)
        waited += 1
        pending = write_resolved(pending, sources, out, horizon, snapshots)
    return {"forecasts": made, "ticks": ticks, "starved": starved,
            "over_budget": bool(getattr(llm, "over_budget", False)),
            "unresolved": len(pending), "spent_usd": float(getattr(llm, "spent_usd", 0.0))}


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbols", default=",".join(SYMBOLS))
    ap.add_argument("--harness", default="harnesses/crypto-horizon-1m-jev.json")
    ap.add_argument("--minutes", type=float, default=25, help="how long to keep collecting")
    ap.add_argument("--every", type=float, default=60, help="seconds between ticks")
    ap.add_argument("--horizon", type=int, default=0,
                    help="minutes ahead; 0 reads it off the benchmark file")
    ap.add_argument("--benchmark", default="benchmarks/crypto-2026-09.json")
    ap.add_argument("--data-dir", default="benchmarks/crypto-data")
    ap.add_argument("--window-usd", type=float, default=0.01, help="per-forecast ceiling")
    ap.add_argument("--max-usd", type=float, default=0.10,
                    help="ceiling on this invocation's model spend; 0 removes it")
    ap.add_argument("--out", default="runs/live/crypto-forecasts.jsonl")
    ap.add_argument("--books", default="runs/live/crypto-books.jsonl")
    args = ap.parse_args()

    symbols = tuple(s.strip().upper() for s in args.symbols.split(",") if s.strip())
    horizon = args.horizon
    if not horizon:
        try:
            horizon = int(json.loads(Path(args.benchmark).read_text()).get("horizon", 1))
        except (OSError, ValueError):
            horizon = 1
    harness = Harness.load(args.harness)
    if args.window_usd:
        harness.config.max_usd = args.window_usd
    llm = OpenRouter(budget_usd=args.max_usd or None)
    out, books_out = Path(args.out), Path(args.books)
    out.parent.mkdir(parents=True, exist_ok=True)

    sources = Sources(Path(args.data_dir), out.parent / "crypto-data", symbols)
    problems = sources.refresh(datetime.now(UTC))
    for p in problems:
        print(f"::warning title=feed::{p}")

    from rsi_arena.crypto._onchain import mempool_now
    started = time.time()
    try:
        summary = await sweep(harness, llm, sources, minutes=args.minutes, every=args.every,
                              out=out, books_out=books_out, horizon=horizon, max_usd=args.max_usd,
                              mempool_fn=mempool_now)
    finally:
        await llm.close()

    print(f"\n{summary['forecasts']} forecasts over {summary['ticks']} ticks written to {out}, "
          f"${summary['spent_usd']:.4f} spent, {int(time.time() - started)}s")
    lines = ["### Live crypto collection",
             f"- {summary['forecasts']} forecasts on {', '.join(symbols)} over {summary['ticks']} ticks, "
             f"horizon {horizon}m",
             f"- ${summary['spent_usd']:.4f} spent" + (f" of ${args.max_usd:.2f}" if args.max_usd else "")]
    if summary["starved"]:
        lines.append("- stopped early: the model refused three calls in a row, which is what an "
                     "exhausted key looks like from here")
    elif summary["over_budget"]:
        lines.append(f"- stopped early: this invocation's ${args.max_usd:.2f} was spent")
    if summary["unresolved"]:
        lines.append(f"- {summary['unresolved']} forecasts never met a printed bar and were dropped")
    for p in problems:
        lines.append(f"- feed: {p}")
    report(lines)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
