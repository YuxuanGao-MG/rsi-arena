"""Put a harness back at an instant of the spot market and let it forecast into a known future.

The same discipline as ``kalshi/replay.py``, on a market that never closes.
Every read here is bounded at ``at``: a bar is known only once it has closed
(:func:`~._binance.close_at`), a funding rate once it has settled, an
open-interest point once its period has printed, a daily on-chain figure
only after the day it describes. **A leak - the bar still being printed, the
day's own transaction count - silently turns the benchmark into a measure of
hindsight**, and it is the kind of leak that makes a harness look good.

Tool names follow the Kalshi box where the meaning carries over
(``market_quote``, ``candlesticks``, ``market_at_time``, ``price_velocity``,
``market_shock``, ``move_base_rate``, ``tape_imbalance``, ``state_summary``),
so a plan written for one reads naturally on the other. The tools that read
the perp, the chain and the clock are this market's own.

Three sources, all duck-typed so the tests can fake them: ``spot`` has
``klines(symbol, start, end)`` (and ``depth`` for live); ``tape`` has
``agg_trades(symbol, start, end, limit)``; ``futures`` has ``funding``,
``oi`` and ``perp`` by symbol; ``onchain`` has ``known_at(series, at)`` and
``first_recorded(series)``. A source left out makes its tools answer
"unavailable" rather than disappear: a harness that names them still loads,
and the absence is in the trace.
"""

from __future__ import annotations

import json
import statistics
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from ..harness.toolcache import NO_CACHE, ToolCache
from ..harness.toolcache import cached as _cached
from ..harness.tools import FunctionTool, Toolbox, ToolResult
from ._binance import MAX_STALE_S, AggTrade, Kline, close_at, complete_before, resample
from ._futures import next_funding, period_ms

#: The default horizon. The topic passes its own; a tool that speaks of "the
#: next N minutes" reads N from the box it is in, never from here.
HORIZON_MINUTES = 1
UTC = timezone.utc

SYMBOLS: tuple[str, ...] = ("BTCUSDT", "ETHUSDT", "SOLUSDT")

#: Binance spot VIP0, no BNB discount: ten basis points either side.
TAKER_BPS = 10.0
MAKER_BPS = 10.0

#: A move over the horizon smaller than this did not happen, for the base rate
#: and the drift tests. The same tick the metric floors at: two basis points,
#: about the median one-minute move on BTC.
TICK_BPS = 2.0

#: A one-minute close-to-close move this large is a shock.
SHOCK_BPS = 25.0

INTERVALS = {"1m": 1, "5m": 5, "1h": 60}


def bps(from_price: float, to_price: float) -> float:
    return (to_price / from_price - 1.0) * 1e4 if from_price else 0.0


def _px(p: float | None) -> str:
    return "?" if p is None else f"{p:,.2f}"


def _instant(raw: Any) -> datetime | None:
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=UTC)
    if not raw:
        return None
    try:
        when = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def _quantile(sorted_values: list[float], q: float) -> float:
    if not sorted_values:
        return 0.0
    pos = q * (len(sorted_values) - 1)
    lo, hi = int(pos), min(int(pos) + 1, len(sorted_values) - 1)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo)


def us_equity_hours(day: datetime) -> tuple[datetime, datetime]:
    """Cash open and close on ``day`` in UTC, DST applied. 09:30-16:00 New York."""
    d = day.astimezone(UTC).date()
    # Second Sunday of March to the first Sunday of November.
    march = datetime(d.year, 3, 1, tzinfo=UTC)
    dst_start = march + timedelta(days=(6 - march.weekday()) % 7 + 7)
    november = datetime(d.year, 11, 1, tzinfo=UTC)
    dst_end = november + timedelta(days=(6 - november.weekday()) % 7)
    midnight = datetime(d.year, d.month, d.day, tzinfo=UTC)
    offset = 4 if dst_start <= midnight < dst_end else 5
    return (midnight + timedelta(hours=9 + offset, minutes=30), midnight + timedelta(hours=16 + offset))


def live_tools(at: datetime, spot: Any, tape: Any = None, *,
               symbols: tuple[str, ...] = SYMBOLS, futures: Any = None,
               onchain: Any = None, horizon: int = HORIZON_MINUTES) -> Toolbox:
    """:func:`replay_tools` at an instant that is happening, never cached, plus
    the two reads that exist only live: the order book and the mempool."""
    box = replay_tools(at, spot, tape, NO_CACHE, symbols=symbols, futures=futures, onchain=onchain,
                       horizon=horizon)

    def order_book(symbol: str, depth: int = 20) -> ToolResult:
        try:
            book = spot.depth(symbol, limit=int(depth))
        except Exception as exc:  # noqa: BLE001 - surfaced to the model
            return ToolResult.failed(f"no book for {symbol}: {type(exc).__name__}: {exc}")
        bids, asks = book.get("bids") or [], book.get("asks") or []
        if not bids or not asks:
            return ToolResult.failed(f"{symbol} has an empty book")
        bid, ask = bids[0][0], asks[0][0]
        mid = (bid + ask) / 2
        bid_qty = sum(q for _, q in bids[:int(depth)])
        ask_qty = sum(q for _, q in asks[:int(depth)])
        out = {"symbol": symbol.upper(), "bid": bid, "ask": ask, "mid": mid,
               "spread_bps": round(bps(mid, ask) - bps(mid, bid), 3),
               "bid_qty": round(bid_qty, 4), "ask_qty": round(ask_qty, 4),
               "imbalance": round((bid_qty - ask_qty) / (bid_qty + ask_qty), 3) if bid_qty + ask_qty else 0.0,
               "levels": int(depth)}
        return ToolResult(ok=True, text=json.dumps(out), data=out)

    def mempool() -> ToolResult:
        from ._onchain import mempool_now
        try:
            out = mempool_now()
        except Exception as exc:  # noqa: BLE001
            return ToolResult.failed(f"mempool unreachable: {type(exc).__name__}: {exc}")
        return ToolResult(ok=True, text=json.dumps(out), data=out)

    box["order_book"] = FunctionTool(
        name="order_book",
        description="The live order book: best bid and ask, spread in bps, resting size either "
                    "side over the top levels. Live only; it has no history.",
        parameters={"type": "object", "properties": {"symbol": {"type": "string"},
                                                     "depth": {"type": "integer"}},
                    "required": ["symbol"]},
        fn=order_book)
    box["mempool_now"] = FunctionTool(
        name="mempool_now",
        description="Bitcoin's mempool this second: queued transactions, size, recommended fees. Live only.",
        parameters={"type": "object", "properties": {}},
        fn=mempool)
    return box


def replay_tools(at: datetime, spot: Any, tape: Any = None, cache: ToolCache | None = None, *,
                 symbols: tuple[str, ...] = SYMBOLS, futures: Any = None,
                 onchain: Any = None, horizon: int = HORIZON_MINUTES) -> Toolbox:
    """Every tool that can be replayed honestly, bounded at ``at``.

    Reads of history are cached through ``cache`` keyed on the instant and the
    arguments; arithmetic with no clock in it is not. ``tape`` defaults to
    ``spot`` when that has ``agg_trades``, which the real client does.
    ``horizon`` is the minutes ahead the harness is asked about, and every
    tool that describes a move "over the horizon" measures that many minutes.
    """
    horizon = max(1, int(horizon))
    at = at if at.tzinfo else at.replace(tzinfo=UTC)
    cache = cache or ToolCache(None)
    stamp = at.astimezone(UTC).isoformat()
    if tape is None and hasattr(spot, "agg_trades"):
        tape = spot
    symbols = tuple(s.upper() for s in symbols)

    def cached(tool: str, args: dict[str, Any], compute: Callable[[], ToolResult]) -> ToolResult:
        # The horizon is in the key: a base rate over one-minute moves is not
        # a base rate over five-minute moves, and both may be built at one instant.
        return _cached(cache, stamp, tool, {**args, "h": horizon}, compute)

    # -- frozen reads, memoised within the box so the summary re-reads nothing --

    _bars: dict[tuple[str, datetime, datetime], list[Kline]] = {}

    def bars_between(symbol: str, start: datetime, end: datetime) -> list[Kline]:
        """Bars with ``ts_open`` in [start, end) whose close is known at ``at``."""
        key = (symbol.upper(), start, end)
        if key not in _bars:
            _bars[key] = complete_before(spot.klines(symbol.upper(), start, min(end, at)), at)
        return _bars[key]

    def bars_back(symbol: str, minutes: float) -> list[Kline]:
        return bars_between(symbol, at - timedelta(minutes=minutes), at)

    def quote_bar(symbol: str, when: datetime | None = None) -> Kline | None:
        when = when or at
        return close_at(bars_between(symbol, when - timedelta(seconds=MAX_STALE_S), when), when)

    def price_then(symbol: str, when: datetime) -> float | None:
        k = quote_bar(symbol, when)
        return None if k is None else k.close

    _prints: dict[tuple[str, int], list[AggTrade]] = {}

    def prints(symbol: str, minutes_back: int) -> list[AggTrade] | None:
        """Every aggregate trade in the last ``minutes_back`` minutes, oldest first, none after ``at``.

        The exchange serves a span oldest-first up to a thousand at a time, so
        a busy ten minutes is paged forward from its start rather than asked
        for once and silently truncated at the far end from the instant.
        """
        if tape is None:
            return None
        key = (symbol.upper(), int(minutes_back))
        if key in _prints:
            return _prints[key]
        start = at - timedelta(minutes=int(minutes_back))
        out: list[AggTrade] = []
        cursor = start
        for _ in range(40):
            page = tape.agg_trades(symbol.upper(), cursor, at, 1000)
            page = [t for t in page if start <= t.ts <= at]
            out.extend(page)
            if len(page) < 1000:
                break
            cursor = page[-1].ts + timedelta(milliseconds=1)
        out.sort(key=lambda t: t.ts)
        _prints[key] = out
        return out

    # -- the book, as the bars recorded it -----------------------------------

    def quote(symbol: str) -> ToolResult:
        def compute() -> ToolResult:
            k = quote_bar(symbol)
            if k is None:
                return ToolResult.failed(f"no complete bar on {symbol} within three minutes of {stamp}")
            before = bars_back(symbol, 6)
            ago_1 = [b for b in before if b.ts_open <= k.ts_open - timedelta(minutes=1)]
            ago_5 = [b for b in before if b.ts_open <= k.ts_open - timedelta(minutes=5)]
            out = {"symbol": symbol.upper(), "price": k.close, "bar_open": k.ts_open.isoformat(),
                   "bar_close": k.ts_close.isoformat(), "age_s": int((at - k.ts_close).total_seconds()),
                   "high": k.high, "low": k.low, "volume": k.volume,
                   "quote_volume": round(k.quote_volume, 2), "trades": k.trades,
                   "taker_buy_share": round(k.taker_buy_volume / k.volume, 3) if k.volume else None,
                   "change_1m_bps": round(bps(ago_1[-1].close, k.close), 2) if ago_1 else None,
                   "change_5m_bps": round(bps(ago_5[-1].close, k.close), 2) if ago_5 else None,
                   "note": "the close of the last complete one-minute bar; there is no book history"}
            return ToolResult(ok=True, text=json.dumps(out), data=out)
        return cached("market_quote", {"symbol": symbol.upper()}, compute)

    def path(symbol: str, hours_back: float = 1.0, interval: str = "1m") -> ToolResult:
        hours = max(0.1, float(hours_back))
        step = INTERVALS.get(str(interval), None)
        def compute() -> ToolResult:
            if step is None:
                return ToolResult.failed(f"interval {interval!r}; one of {', '.join(INTERVALS)}")
            rows = resample(bars_back(symbol, hours * 60), step)[-300:]
            bars = [{"ts": k.ts_open.isoformat(), "o": k.open, "h": k.high, "l": k.low, "c": k.close,
                     "v": round(k.volume, 4), "trades": k.trades,
                     "taker_buy_share": round(k.taker_buy_volume / k.volume, 3) if k.volume else None}
                    for k in rows]
            out = {"symbol": symbol.upper(), "interval": interval, "bars": bars,
                   "net_bps": round(bps(bars[0]["o"], bars[-1]["c"]), 2) if bars else None}
            return ToolResult(ok=True, text=json.dumps(out), data=out)
        return cached("candlesticks", {"symbol": symbol.upper(), "hours_back": hours,
                                       "interval": str(interval)}, compute)

    def trades(symbol: str, minutes_back: int = 5, limit: int = 20) -> ToolResult:
        back, n = max(1, int(minutes_back)), max(1, int(limit))
        def compute() -> ToolResult:
            rows = prints(symbol, back)
            if rows is None:
                return ToolResult.failed("no trade tape in this box")
            recent = [{"ts": t.ts.isoformat(timespec="milliseconds"), "price": t.price, "qty": t.qty,
                       "side": "buy" if t.taker_buy else "sell"} for t in rows[-n:]][::-1]
            out = {"symbol": symbol.upper(), "minutes_back": back, "prints_in_window": len(rows),
                   "trades": recent}
            return ToolResult(ok=True, text=json.dumps(out), data=out)
        return cached("recent_trades", {"symbol": symbol.upper(), "minutes_back": back, "limit": n},
                      compute)

    def at_time(symbol: str, when: str) -> ToolResult:
        def compute() -> ToolResult:
            asked = _instant(when)
            if asked is None:
                return ToolResult.failed(f"{when!r} is not an ISO instant")
            # The one place a harness could reach forward by asking: clamped
            # and told, not refused.
            capped = min(asked, at)
            k = quote_bar(symbol, capped)
            if k is None:
                return ToolResult.failed(f"no complete bar on {symbol} at {capped.isoformat()}")
            out = {"symbol": symbol.upper(), "asked_for": asked.isoformat(),
                   "answered_at": capped.isoformat(), "clamped": capped < asked,
                   "price": k.close, "bar_open": k.ts_open.isoformat(), "high": k.high, "low": k.low,
                   "volume": k.volume}
            return ToolResult(ok=True, text=json.dumps(out), data=out)
        return cached("market_at_time", {"symbol": symbol.upper(), "when": str(when)}, compute)

    def volume(symbol: str, hours_back: float = 3.0) -> ToolResult:
        hours = max(0.1, float(hours_back))
        def compute() -> ToolResult:
            rows = [k for k in bars_back(symbol, hours * 60) if k.volume]
            if not rows:
                return ToolResult.failed(f"nothing traded on {symbol} in that window")
            ref = rows[-1].close
            step = ref * 0.001                     # ten basis points of the last price
            buckets: dict[float, float] = {}
            for k in rows:
                key = round(round(k.close / step) * step, 2)
                buckets[key] = buckets.get(key, 0.0) + k.quote_volume
            total = sum(buckets.values())
            heaviest = max(buckets.items(), key=lambda kv: kv[1])
            vwap = sum(k.close * k.quote_volume for k in rows) / sum(k.quote_volume for k in rows)
            out = {"symbol": symbol.upper(), "hours_back": hours, "quote_volume": round(total, 2),
                   "bars_traded": len(rows), "bucket_bps": 10,
                   "by_price": {f"{k:,.2f}": round(v, 2) for k, v in sorted(buckets.items())},
                   "heaviest_price": heaviest[0], "heaviest_share": round(heaviest[1] / total, 3),
                   "vwap": round(vwap, 2), "price_vs_vwap_bps": round(bps(vwap, ref), 2)}
            return ToolResult(ok=True, text=json.dumps(out), data=out)
        return cached("volume_profile", {"symbol": symbol.upper(), "hours_back": hours}, compute)

    # -- arithmetic: no clock, no I/O ----------------------------------------

    def fees(notional: float = 10000.0, maker: bool = False) -> ToolResult:
        size = max(0.0, float(notional))
        side = MAKER_BPS if maker else TAKER_BPS
        out = {"notional_usd": size, "maker": bool(maker), "fee_bps": side,
               "fee_usd": round(size * side / 1e4, 4),
               "round_trip_bps": 2 * side, "round_trip_usd": round(size * 2 * side / 1e4, 4),
               "breakeven_move_bps": 2 * side,
               "note": "Binance spot VIP0 without a BNB discount: 10 bps taker, 10 bps maker"}
        return ToolResult(ok=True, text=json.dumps(out), data=out)

    def edge(expected_bps: float, half_width_bps: float = 0.0, fee_bps: float = 2 * TAKER_BPS) -> ToolResult:
        try:
            exp, half, fee = float(expected_bps), max(0.0, float(half_width_bps)), max(0.0, float(fee_bps))
        except (TypeError, ValueError):
            return ToolResult.failed("expected_bps, half_width_bps and fee_bps are numbers")
        net = abs(exp) - fee
        out = {"expected_bps": exp, "half_width_bps": half, "fee_bps": fee,
               "net_edge_bps": round(net, 2), "worth_taking": net > 0,
               "edge_to_width": round(abs(exp) / half, 3) if half > 0 else None,
               "verdict": (f"{abs(exp):.0f} bps expected less {fee:.0f} bps of fees leaves "
                           f"{net:+.0f} bps: " + ("worth trading" if net > 0 else "not worth trading"))}
        return ToolResult(ok=True, text=json.dumps(out), data=out)

    def session() -> ToolResult:
        nxt = next_funding(at)
        day_close = at.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        open_, close_ = us_equity_hours(at)
        weekday = at.astimezone(UTC).weekday()
        weekend = weekday >= 5
        if at >= close_ or weekend:
            # The next open is the next weekday's.
            probe = at + timedelta(days=1)
            while probe.weekday() >= 5:
                probe += timedelta(days=1)
            open_, close_ = us_equity_hours(probe)
        out = {"at": stamp, "weekday": at.strftime("%A"), "hour_utc": at.hour,
               "weekend": weekend,
               "minutes_to_funding": round((nxt - at).total_seconds() / 60, 1),
               "next_funding": nxt.isoformat(),
               "minutes_to_utc_day_close": round((day_close - at).total_seconds() / 60, 1),
               "us_equity_open": open_ <= at < close_ and not weekend,
               "minutes_to_us_open": round((open_ - at).total_seconds() / 60, 1) if at < open_ else None,
               "minutes_to_us_close": round((close_ - at).total_seconds() / 60, 1)
               if open_ <= at < close_ else None}
        return ToolResult(ok=True, text=json.dumps(out), data=out)

    # -- more of the path ------------------------------------------------------

    def velocity(symbol: str) -> ToolResult:
        def compute() -> ToolResult:
            rows = bars_back(symbol, 60)
            if len(rows) < 6:
                return ToolResult.failed(f"too few bars on {symbol} in the last hour")
            closes = [k.close for k in rows]
            steps = sorted(abs(bps(a, b)) for a, b in zip(closes, closes[1:]))
            typical = steps[len(steps) // 2]
            def move(n: int) -> float:
                return bps(closes[-min(n + 1, len(closes))], closes[-1])
            m5 = move(5)
            out = {"symbol": symbol.upper(), "price": closes[-1], "bars": len(rows),
                   "move_1m_bps": round(move(1), 2), "move_3m_bps": round(move(3), 2),
                   "move_5m_bps": round(m5, 2), "typical_minute_move_bps": round(typical, 2),
                   "verdict": ("running" if abs(m5) > 4 * typical + TICK_BPS
                               else "moving" if abs(m5) > 2 * typical + TICK_BPS / 2 else "still")}
            return ToolResult(ok=True, text=json.dumps(out), data=out)
        return cached("price_velocity", {"symbol": symbol.upper()}, compute)

    def jumps_in(rows: list[Kline], threshold: float) -> list[dict[str, Any]]:
        latest = rows[-1].close
        out = []
        for before, after in zip(rows, rows[1:]):
            move = bps(before.close, after.close)
            if abs(move) < threshold:
                continue
            retraced = max(0.0, min(1.0, (after.close - latest) / (after.close - before.close))) \
                if after.close != before.close else 0.0
            out.append({"at": after.ts_close.isoformat(), "move_bps": round(move, 1),
                        "from": before.close, "to": after.close, "retraced": round(retraced, 3)})
        return out

    def shock(symbol: str, minutes_back: int = 30, threshold_bps: float = SHOCK_BPS) -> ToolResult:
        back, threshold = max(2, int(minutes_back)), float(threshold_bps)
        def compute() -> ToolResult:
            rows = bars_back(symbol, back)
            if len(rows) < 3:
                return ToolResult.failed(f"too few bars on {symbol} in that window")
            found = jumps_in(rows, threshold)
            if not found:
                return ToolResult(ok=True, text=f"no one-minute move over {threshold:g} bps on {symbol}",
                                  data={"symbol": symbol.upper(), "jumps": [], "price": rows[-1].close})
            found.sort(key=lambda j: -abs(j["move_bps"]))
            out = {"symbol": symbol.upper(), "price": rows[-1].close, "jumps": found[:6],
                   "largest": found[0]}
            return ToolResult(ok=True, text=json.dumps(out), data=out)
        return cached("market_shock", {"symbol": symbol.upper(), "minutes_back": back,
                                       "threshold_bps": threshold}, compute)

    # -- the perp -----------------------------------------------------------------

    def not_before(kind: str, symbol: str, rows: list[dict[str, Any]]) -> ToolResult:
        if not rows:
            return ToolResult.failed(f"no {kind} history for {symbol}")
        first = datetime.fromtimestamp(int(rows[0]["ts"]) / 1000, tz=UTC)
        return ToolResult.failed(f"{kind} for {symbol} not recorded before {first.date().isoformat()}")

    def funding(symbol: str) -> ToolResult:
        def compute() -> ToolResult:
            if futures is None:
                return ToolResult.failed("no futures data in this box")
            rows = futures.funding(symbol.upper())
            cut = int(at.timestamp() * 1000)
            known = [r for r in rows if int(r["ts"]) <= cut]
            if not known:
                return not_before("funding", symbol, rows)
            last = known[-1]
            day = [r for r in known if int(r["ts"]) > cut - 24 * 3600 * 1000]
            settled = datetime.fromtimestamp(int(last["ts"]) / 1000, tz=UTC)
            nxt = next_funding(at)
            rate_bps = float(last["rate"]) * 1e4
            out = {"symbol": symbol.upper(), "rate": float(last["rate"]), "rate_bps": round(rate_bps, 3),
                   "annualised_pct": round(float(last["rate"]) * 3 * 365 * 100, 2),
                   "settled_at": settled.isoformat(), "next_funding": nxt.isoformat(),
                   "minutes_to_next": round((nxt - at).total_seconds() / 60, 1),
                   "last_24h_bps": [round(float(r["rate"]) * 1e4, 3) for r in day],
                   "mean_24h_bps": round(sum(float(r["rate"]) for r in day) / len(day) * 1e4, 3) if day else None,
                   "verdict": (f"{'longs pay' if rate_bps >= 0 else 'shorts pay'} {abs(rate_bps):.1f} bps "
                               f"per 8h, next reset in {(nxt - at).total_seconds() / 60:.0f} min")}
            return ToolResult(ok=True, text=json.dumps(out), data=out)
        return cached("funding_rate", {"symbol": symbol.upper()}, compute)

    def open_interest(symbol: str) -> ToolResult:
        def compute() -> ToolResult:
            if futures is None:
                return ToolResult.failed("no futures data in this box")
            rows = futures.oi(symbol.upper())
            cut = int(at.timestamp() * 1000)
            # A period's figure is its aggregate: known when the period closes.
            known = sorted((r for r in rows if int(r["ts"]) + period_ms(r) <= cut),
                           key=lambda r: int(r["ts"]) + period_ms(r))
            if not known:
                return not_before("open interest", symbol, rows)
            last = known[-1]
            as_of = int(last["ts"]) + period_ms(last)

            def change(hours: float, slack_hours: float) -> float | None:
                """Against the point closed ``hours`` ago, if one closed within ``slack`` of that."""
                target = as_of - hours * 3600 * 1000
                earlier = [r for r in known
                           if target - slack_hours * 3600 * 1000 <= int(r["ts"]) + period_ms(r) <= target]
                if not earlier or not earlier[-1]["oi"]:
                    return None
                return round((float(last["oi"]) / float(earlier[-1]["oi"]) - 1) * 100, 3)

            c1, c4, c24 = change(1, 0.25), change(4, 0.5), change(24, 6)
            lead = c1 if c1 is not None else c24
            out = {"symbol": symbol.upper(), "oi": float(last["oi"]), "oi_usd": round(float(last["oi_usd"]), 0),
                   "as_of": datetime.fromtimestamp(as_of / 1000, tz=UTC).isoformat(),
                   "resolution": str(last.get("period") or "5m"),
                   "change_1h_pct": c1, "change_4h_pct": c4, "change_24h_pct": c24,
                   "verdict": (("open interest flushed" if lead is not None and lead <= -2 else
                                "open interest building" if lead is not None and lead >= 2 else
                                "open interest steady")
                               + (f", {c1:+.1f}% in 1h" if c1 is not None else "")
                               + (f", {c4:+.1f}% in 4h" if c4 is not None else "")
                               + (f", {c24:+.1f}% in 24h" if c24 is not None else "")
                               + (f" (daily points only)" if c1 is None and c24 is not None else ""))}
            return ToolResult(ok=True, text=json.dumps(out), data=out)
        return cached("open_interest", {"symbol": symbol.upper()}, compute)

    def basis(symbol: str) -> ToolResult:
        def compute() -> ToolResult:
            if futures is None:
                return ToolResult.failed("no futures data in this box")
            rows = futures.perp(symbol.upper())
            # A five-minute perp bar is known when it closes, five minutes after its open.
            cut = int((at - timedelta(minutes=5)).timestamp() * 1000)
            known = [r for r in rows if int(r["ts"]) <= cut]
            if not known:
                return not_before("perp candles", symbol, rows)
            last = known[-1]
            closed = datetime.fromtimestamp(int(last["ts"]) / 1000, tz=UTC) + timedelta(minutes=5)
            spot_then = price_then(symbol, closed)
            if spot_then is None:
                return ToolResult.failed(f"no spot bar on {symbol} at {closed.isoformat()}")
            premium = bps(spot_then, float(last["c"]))
            out = {"symbol": symbol.upper(), "perp": float(last["c"]), "spot": spot_then,
                   "as_of": closed.isoformat(), "basis_bps": round(premium, 2),
                   "annualised_pct": round(premium / 1e4 * 3 * 365 * 100, 2),
                   "verdict": (f"perp {'rich' if premium > 0 else 'cheap'} to spot by {abs(premium):.1f} bps"
                               + (", a stretched premium" if abs(premium) > 10 else ""))}
            return ToolResult(ok=True, text=json.dumps(out), data=out)
        return cached("basis", {"symbol": symbol.upper()}, compute)

    # -- across the three coins ------------------------------------------------

    def cross(symbol: str) -> ToolResult:
        def compute() -> ToolResult:
            rows = []
            for other in symbols:
                if other == symbol.upper():
                    continue
                now = price_then(other, at)
                if now is None:
                    rows.append({"symbol": other, "price": None})
                    continue
                def move(minutes: int) -> float | None:
                    then = price_then(other, at - timedelta(minutes=minutes))
                    return None if then is None else round(bps(then, now), 2)
                rows.append({"symbol": other, "price": now, "move_5m_bps": move(5),
                             "move_1h_bps": move(60), "move_24h_bps": move(24 * 60)})
            if not rows:
                return ToolResult.failed("no other symbol in this box")
            out = {"symbol": symbol.upper(), "others": rows,
                   "verdict": "; ".join(f"{r['symbol'][:-4]} {r['move_5m_bps']:+.0f} bps/5m, "
                                        f"{r['move_1h_bps']:+.0f}/1h"
                                        for r in rows if r.get("move_5m_bps") is not None
                                        and r.get("move_1h_bps") is not None) or "no prices"}
            return ToolResult(ok=True, text=json.dumps(out), data=out)
        return cached("cross_asset", {"symbol": symbol.upper()}, compute)

    # -- the calendar in the price ---------------------------------------------

    def horizon_moves(rows: list[Kline]) -> list[tuple[Kline, float]]:
        """(bar, move to the bar ``horizon`` minutes on) for every bar that has one, in bps."""
        by_open = {k.ts_open: k for k in rows}
        out = []
        for k in rows:
            later = by_open.get(k.ts_open + timedelta(minutes=horizon))
            if later is not None:
                out.append((k, bps(k.close, later.close)))
        return out

    def seasonality(symbol: str) -> ToolResult:
        def compute() -> ToolResult:
            rows = bars_back(symbol, 30 * 24 * 60)
            moves = horizon_moves(rows)
            if len(moves) < 12 * 60:
                return ToolResult.failed(f"under half a day of {horizon}-minute history on {symbol}")
            slots: dict[int, list[float]] = {}
            for k, m in moves:
                slots.setdefault(k.ts_open.weekday() * 24 + k.ts_open.hour, []).append(m)
            by_hour: dict[int, list[float]] = {}
            for k, m in moves:
                by_hour.setdefault(k.ts_open.hour, []).append(m)
            this_slot = at.weekday() * 24 + at.hour
            here = slots.get(this_slot, [])
            overall = sum(abs(m) for _, m in moves) / len(moves)
            hours = sorted(((sum(abs(m) for m in ms) / len(ms), h) for h, ms in by_hour.items()),
                           reverse=True)
            out = {"symbol": symbol.upper(), "horizon_minutes": horizon,
                   "days": round(len(moves) / (24 * 60), 1),
                   "weekday": at.strftime("%A"), "hour_utc": at.hour,
                   "this_slot": {"samples": len(here),
                                 "mean_abs_bps": round(sum(abs(m) for m in here) / len(here), 2) if here else None,
                                 "drift_bps": round(sum(here) / len(here), 2) if here else None},
                   "overall_mean_abs_bps": round(overall, 2),
                   "busiest_hours_utc": [h for _, h in hours[:3]],
                   "quietest_hours_utc": [h for _, h in hours[-3:]],
                   "verdict": (f"{at.strftime('%A')} {at.hour:02d}h moves "
                               f"{(sum(abs(m) for m in here) / len(here)) / overall:.1f}x the month's "
                               f"average {horizon}-minute size" if here and overall else "no sample for this hour")}
            return ToolResult(ok=True, text=json.dumps(out), data=out)
        return cached("hourly_seasonality", {"symbol": symbol.upper()}, compute)

    def base_rate(symbol: str) -> ToolResult:
        def compute() -> ToolResult:
            rows = bars_back(symbol, 3 * 24 * 60)
            moves = sorted(m for k, m in horizon_moves(rows) if k.ts_open.hour == at.hour)
            if not moves:
                out = {"symbol": symbol.upper(), "hour_utc": at.hour, "horizon_minutes": horizon,
                       "samples": 0, "mean_abs_bps": None,
                       "p10": None, "p50": None, "p90": None, "moved_share": None,
                       "verdict": f"no {horizon}-minute history at {at.hour:02d}h on {symbol}"}
                return ToolResult(ok=True, text=json.dumps(out), data=out)
            moved = sum(1 for m in moves if abs(m) >= TICK_BPS) / len(moves)
            out = {"symbol": symbol.upper(), "hour_utc": at.hour, "horizon_minutes": horizon,
                   "samples": len(moves),
                   "mean_abs_bps": round(sum(abs(m) for m in moves) / len(moves), 2),
                   "mean_bps": round(sum(moves) / len(moves), 2),
                   "p10": round(_quantile(moves, 0.1), 2), "p50": round(_quantile(moves, 0.5), 2),
                   "p90": round(_quantile(moves, 0.9), 2), "moved_share": round(moved, 3),
                   "verdict": (f"{'active' if moved >= 0.5 else 'quiet'}: {moved:.0%} of {len(moves)} "
                               f"{horizon}-minute windows at {at.hour:02d}h over three days moved "
                               f"{TICK_BPS:g} bps or more")}
            return ToolResult(ok=True, text=json.dumps(out), data=out)
        return cached("move_base_rate", {"symbol": symbol.upper()}, compute)

    # -- the tape, and what the price has absorbed -------------------------------

    def imbalance(symbol: str, minutes_back: int = 10) -> ToolResult:
        back = max(1, int(minutes_back))
        def compute() -> ToolResult:
            rows = prints(symbol, back)
            if rows is not None:
                # Bounded by the request's end, and checked again here: a print
                # from after the instant is the exact leak this box refuses.
                rows = [t for t in rows if t.ts <= at]
                buy = sum(t.qty for t in rows if t.taker_buy)
                sell = sum(t.qty for t in rows if not t.taker_buy)
                largest = max((t.qty * t.price for t in rows), default=0.0)
                source, n = "aggTrades", len(rows)
            else:
                # No tape in the box: the bars carry the exchange's own count
                # of taker-bought volume, which is the same quantity a minute
                # at a time.
                bars = bars_back(symbol, back)
                buy = sum(k.taker_buy_volume for k in bars)
                sell = sum(k.taker_sell_volume for k in bars)
                largest, source, n = 0.0, "klines", sum(k.trades for k in bars)
            total = buy + sell
            ratio = (buy - sell) / total if total else 0.0
            verdict = "buyers" if ratio > 0.2 else "sellers" if ratio < -0.2 else "balanced"
            out = {"symbol": symbol.upper(), "minutes_back": back, "prints": n, "source": source,
                   "taker_bought": round(buy, 4), "taker_sold": round(sell, 4),
                   "imbalance": round(ratio, 3), "largest_print_usd": round(largest, 2),
                   "verdict": f"{verdict}: {n} prints in {back}m, {buy:.2f} bought against {sell:.2f} sold"}
            return ToolResult(ok=True, text=json.dumps(out), data=out)
        return cached("tape_imbalance", {"symbol": symbol.upper(), "minutes_back": back}, compute)

    def absorption(symbol: str) -> ToolResult:
        def compute() -> ToolResult:
            rows = bars_back(symbol, 60)
            if len(rows) < 4:
                return ToolResult.failed(f"too few bars on {symbol} in the last hour")
            found = jumps_in(rows, SHOCK_BPS)
            if not found:
                out = {"symbol": symbol.upper(), "note": f"no one-minute move over {SHOCK_BPS:g} bps in the last hour"}
                return ToolResult(ok=True, text=json.dumps(out), data=out)
            last = found[-1]                      # the most recent, in time order
            since = bps(last["to"], rows[-1].close)
            recent = rows[-4:]
            drift = bps(recent[0].close, recent[-1].close)
            minutes = int((at - _instant(last["at"])).total_seconds() // 60)
            out = {"symbol": symbol.upper(), "shock_at": last["at"], "shock_bps": last["move_bps"],
                   "minutes_since": minutes, "price_after_shock": last["to"], "price": rows[-1].close,
                   "move_since_bps": round(since, 2), "retraced": last["retraced"],
                   "drift_last_3_bars_bps": round(drift, 2), "still_drifting": abs(drift) >= TICK_BPS,
                   "verdict": (f"{last['move_bps']:+.0f} bps jump {minutes}m ago, {since:+.0f} bps since"
                               + (", still drifting" if abs(drift) >= TICK_BPS else ", settled"))}
            return ToolResult(ok=True, text=json.dumps(out), data=out)
        return cached("shock_absorption", {"symbol": symbol.upper()}, compute)

    # -- the chain ----------------------------------------------------------------

    def chain_not_before(name: str, label: str) -> ToolResult:
        first = onchain.first_recorded(name)
        if first is None:
            return ToolResult.failed(f"no {label} on file")
        return ToolResult.failed(f"{label} not recorded before {first.date().isoformat()}")

    def chain_fees(asset: str = "BTC") -> ToolResult:
        def compute() -> ToolResult:
            if onchain is None:
                return ToolResult.failed("no on-chain data in this box")
            if str(asset).upper() != "BTC":
                return ToolResult.failed("only BTC has a fee-rate series")
            rows = onchain.known_at("fee_rates", at)
            if not rows:
                return chain_not_before("fee_rates", "fee rates")
            last = rows[-1]
            cut = int(at.timestamp()) - 24 * 3600
            day = [float(r["fee_50"]) for r in rows if int(r["ts"]) >= cut]
            when = datetime.fromtimestamp(int(last["ts"]), tz=UTC)
            out = {"asset": "BTC", "as_of": when.isoformat(), "height": last.get("height"),
                   "age_min": round((at - when).total_seconds() / 60, 1),
                   "fee_10_sat_vb": last.get("fee_10"), "fee_50_sat_vb": last.get("fee_50"),
                   "fee_90_sat_vb": last.get("fee_90"),
                   "median_fee_50_24h": round(statistics.median(day), 2) if day else None,
                   "verdict": (f"median fee {last.get('fee_50')} sat/vB"
                               + (f", {'above' if day and float(last.get('fee_50', 0)) > statistics.median(day) else 'at or below'} the day's median"
                                  if day else ""))}
            return ToolResult(ok=True, text=json.dumps(out), data=out)
        return cached("chain_fees", {"asset": str(asset).upper()}, compute)

    def daily_read(name: str) -> dict[str, Any] | None:
        rows = onchain.known_at(name, at)
        if not rows:
            return None
        last = rows[-1]
        def ago(days: int) -> float | None:
            cut = int(last["ts"]) - days * 86400
            earlier = [r for r in rows if int(r["ts"]) <= cut]
            return float(earlier[-1]["value"]) if earlier else None
        w, m = ago(7), ago(30)
        v = float(last["value"])
        return {"value": v, "date": datetime.fromtimestamp(int(last["ts"]), tz=UTC).date().isoformat(),
                "change_7d_pct": round((v / w - 1) * 100, 2) if w else None,
                "change_30d_pct": round((v / m - 1) * 100, 2) if m else None}

    def chain_activity(asset: str = "BTC") -> ToolResult:
        def compute() -> ToolResult:
            if onchain is None:
                return ToolResult.failed("no on-chain data in this box")
            if str(asset).upper() != "BTC":
                return ToolResult.failed("only BTC has activity series")
            reads = {name: daily_read(name) for name in ("n_transactions", "mempool_size", "hash_rate")}
            if all(v is None for v in reads.values()):
                return chain_not_before("n_transactions", "chain activity")
            out = {"asset": "BTC", **{k: v for k, v in reads.items()},
                   "note": "daily figures; a day's number is known only after the day closes"}
            tx = reads.get("n_transactions")
            out["verdict"] = (f"{tx['value']:,.0f} transactions on {tx['date']}, "
                              f"{tx['change_7d_pct']:+.1f}% on the week" if tx and tx["change_7d_pct"] is not None
                              else "no transaction count known yet")
            return ToolResult(ok=True, text=json.dumps(out), data=out)
        return cached("chain_activity", {"asset": str(asset).upper()}, compute)

    def stables() -> ToolResult:
        def compute() -> ToolResult:
            if onchain is None:
                return ToolResult.failed("no on-chain data in this box")
            read = daily_read("stablecoin_supply")
            if read is None:
                return chain_not_before("stablecoin_supply", "stablecoin supply")
            out = {"total_usd": read["value"], "date": read["date"],
                   "change_7d_pct": read["change_7d_pct"], "change_30d_pct": read["change_30d_pct"],
                   "verdict": f"${read['value'] / 1e9:,.1f}bn of stablecoins on {read['date']}"
                              + (f", {read['change_7d_pct']:+.2f}% on the week" if read["change_7d_pct"] is not None else "")}
            return ToolResult(ok=True, text=json.dumps(out), data=out)
        return cached("stablecoin_supply", {}, compute)

    def dex() -> ToolResult:
        def compute() -> ToolResult:
            if onchain is None:
                return ToolResult.failed("no on-chain data in this box")
            rows = onchain.known_at("dex_volume", at)
            if not rows:
                return chain_not_before("dex_volume", "DEX volume")
            vals = [float(r["value"]) for r in rows]
            last, week, prior = vals[-1], vals[-7:], vals[-14:-7]
            avg_w = sum(week) / len(week)
            avg_p = sum(prior) / len(prior) if prior else None
            out = {"volume_usd": last, "date": datetime.fromtimestamp(int(rows[-1]["ts"]), tz=UTC).date().isoformat(),
                   "avg_7d_usd": round(avg_w, 0),
                   "week_on_week_pct": round((avg_w / avg_p - 1) * 100, 2) if avg_p else None,
                   "vs_7d_avg_pct": round((last / avg_w - 1) * 100, 2) if avg_w else None,
                   "verdict": f"${last / 1e9:,.2f}bn traded on DEXs, {((last / avg_w - 1) * 100):+.0f}% against the week's average"
                              if avg_w else "no DEX volume known yet"}
            return ToolResult(ok=True, text=json.dumps(out), data=out)
        return cached("dex_volume", {}, compute)

    # -- one paragraph -------------------------------------------------------------

    def summary(symbol: str) -> ToolResult:
        def compute() -> ToolResult:
            q = quote(symbol)
            if not q.ok:
                return ToolResult.failed(q.error or "no quote")
            book = q.data
            parts = [f"{symbol.upper()} {_px(book['price'])}"
                     + (f" ({book['change_1m_bps']:+.0f} bps/1m, {book['change_5m_bps']:+.0f} bps/5m)"
                        if book.get("change_1m_bps") is not None and book.get("change_5m_bps") is not None
                        else "") + "."]
            rows = bars_back(symbol, 15)[-5:]
            if len(rows) >= 2:
                parts.append(f"Last {len(rows)} bars {'/'.join(_px(k.close) for k in rows)}, "
                             f"net {bps(rows[0].close, rows[-1].close):+.0f} bps.")
            tape_now = imbalance(symbol)
            if tape_now.ok:
                parts.append(f"Tape: {tape_now.data['verdict']}.")
            fund = funding(symbol)
            if fund.ok:
                parts.append(f"Funding: {fund.data['verdict']}.")
            others = cross(symbol)
            if others.ok:
                parts.append(f"Others: {others.data['verdict']}.")
            rate = base_rate(symbol)
            if rate.ok:
                parts.append(f"Base rate: {rate.data['verdict']}.")
            clk = session()
            if clk.ok:
                parts.append(f"Clock: {clk.data['minutes_to_funding']:.0f} min to funding"
                             + (", US cash open" if clk.data["us_equity_open"] else "")
                             + (", weekend" if clk.data["weekend"] else "") + ".")
            text = " ".join(parts)
            if len(text) > 600:
                text = text[:597].rstrip() + "..."
            out = {"symbol": symbol.upper(), "summary": text, "price": book["price"]}
            return ToolResult(ok=True, text=text, data=out)
        return cached("state_summary", {"symbol": symbol.upper()}, compute)

    sym = {"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]}

    def with_symbol(**extra: Any) -> dict[str, Any]:
        return {"type": "object", "properties": {"symbol": {"type": "string"}, **extra},
                "required": ["symbol"]}

    return Toolbox([
        FunctionTool(name="market_quote",
                     description="The price as of now: the close of the last complete one-minute bar, "
                                 "with its range, volume, taker-buy share and the 1m and 5m change in bps.",
                     parameters=sym, fn=quote),
        FunctionTool(name="candlesticks",
                     description="Bars up to now. hours_back defaults to 1; interval is 1m, 5m or 1h. "
                                 "Each bar carries its taker-buy share.",
                     parameters=with_symbol(hours_back={"type": "number"},
                                            interval={"type": "string", "enum": list(INTERVALS)}),
                     fn=path),
        FunctionTool(name="recent_trades",
                     description="The print tape, newest first, from just before now. minutes_back "
                                 "defaults to 5, limit to 20.",
                     parameters=with_symbol(minutes_back={"type": "integer"}, limit={"type": "integer"}),
                     fn=trades),
        FunctionTool(name="market_at_time",
                     description="The price at an earlier ISO instant. Asking past now answers now and says so.",
                     parameters=with_symbol(when={"type": "string"}), fn=at_time),
        FunctionTool(name="volume_profile",
                     description="Where it traded, by price in ten-bps buckets, over a window ending now; "
                                 "VWAP and the heaviest level.",
                     parameters=with_symbol(hours_back={"type": "number"}), fn=volume),
        FunctionTool(name="trading_fees",
                     description="Spot fees on a notional: 10 bps taker and 10 bps maker (VIP0), the "
                                 "round trip, and the move in bps that breaks even.",
                     parameters={"type": "object", "properties": {"notional": {"type": "number"},
                                                                  "maker": {"type": "boolean"}}},
                     fn=fees),
        FunctionTool(name="price_the_edge",
                     description="Whether an expected move in bps clears fees, and how it compares "
                                 "with the half-width quoted.",
                     parameters={"type": "object",
                                 "properties": {"expected_bps": {"type": "number"},
                                                "half_width_bps": {"type": "number"},
                                                "fee_bps": {"type": "number"}},
                                 "required": ["expected_bps"]},
                     fn=edge),
        FunctionTool(name="price_velocity",
                     description="How fast it is moving against its own last hour: 1m, 3m and 5m "
                                 "moves in bps, the typical minute, and a verdict.",
                     parameters=sym, fn=velocity),
        FunctionTool(name="market_shock",
                     description="One-minute jumps over threshold_bps (default 25) in the last "
                                 "minutes_back (default 30), and how much of each has come back.",
                     parameters=with_symbol(minutes_back={"type": "integer"},
                                            threshold_bps={"type": "number"}),
                     fn=shock),
        FunctionTool(name="funding_rate",
                     description="The perp's last settled funding rate, when the next reset is, and "
                                 "the last 24 hours of rates, in bps per 8h.",
                     parameters=sym, fn=funding),
        FunctionTool(name="open_interest",
                     description="The perp's open interest as of the last closed period, with the "
                                 "change over 1h, 4h and 24h where the series has the points. A "
                                 "drop is a liquidation cascade or a de-risking.",
                     parameters=sym, fn=open_interest),
        FunctionTool(name="basis",
                     description="The perp's premium to spot in bps as of the last closed perp bar, "
                                 "and the same annualised as if it were funding.",
                     parameters=sym, fn=basis),
        FunctionTool(name="cross_asset",
                     description="The other symbols' moves over 5m, 1h and 24h in bps. Lead and lag "
                                 "across the majors is the one cross-sectional read there is.",
                     parameters=sym, fn=cross),
        FunctionTool(name="hourly_seasonality",
                     description=f"Mean absolute {horizon}-minute move and drift by hour of week over "
                                 "the thirty days before now, and where this hour sits.",
                     parameters=sym, fn=seasonality),
        FunctionTool(name="session_clock",
                     description="Minutes to the next funding reset, to the UTC day close, to the US "
                                 "cash open or close; weekend flag. Date maths for a model that cannot.",
                     parameters={"type": "object", "properties": {}}, fn=session),
        FunctionTool(name="chain_fees",
                     description="Bitcoin fee rates from the last blocks known at now: the 10th, 50th "
                                 "and 90th percentile sat/vB, against the day's median.",
                     parameters={"type": "object", "properties": {"asset": {"type": "string"}}},
                     fn=chain_fees),
        FunctionTool(name="chain_activity",
                     description="Bitcoin's daily transactions, mempool size and hash rate as last "
                                 "known: yesterday's figures, with week and month changes.",
                     parameters={"type": "object", "properties": {"asset": {"type": "string"}}},
                     fn=chain_activity),
        FunctionTool(name="stablecoin_supply",
                     description="Total stablecoin supply in dollars as of the last closed day, with "
                                 "week and month changes.",
                     parameters={"type": "object", "properties": {}}, fn=stables),
        FunctionTool(name="dex_volume",
                     description="DEX volume on the last closed day against the week's average.",
                     parameters={"type": "object", "properties": {}}, fn=dex),
        FunctionTool(name="move_base_rate",
                     description=f"What {horizon}-minute moves have looked like at this hour of day over "
                                 "the three days before now: p10/p50/p90 in bps and the share that moved.",
                     parameters=sym, fn=base_rate),
        FunctionTool(name="tape_imbalance",
                     description="Taker buying against taker selling over the last minutes_back "
                                 "(default 10) minutes, from the aggregate trade tape.",
                     parameters=with_symbol(minutes_back={"type": "integer"}), fn=imbalance),
        FunctionTool(name="shock_absorption",
                     description="How far the price has moved since the last one-minute jump over "
                                 "25 bps in the last hour, and whether it is still drifting.",
                     parameters=sym, fn=absorption),
        FunctionTool(name="state_summary",
                     description="One paragraph under 600 characters: price and its last five bars, "
                                 "tape, funding, the other coins, the base rate at this hour, the clock. "
                                 "Built from the other tools, so it cannot disagree with them.",
                     parameters=sym, fn=summary),
    ])


__all__ = ["replay_tools", "live_tools", "SYMBOLS", "TICK_BPS", "SHOCK_BPS", "TAKER_BPS",
           "MAKER_BPS", "HORIZON_MINUTES", "bps", "us_equity_hours"]
