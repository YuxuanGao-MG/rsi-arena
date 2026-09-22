"""Put a harness back at the second a story broke, with the tape frozen there.

The same discipline as ``kalshi/replay.py``, on a different venue. Every tool
here reads bars, prints or news bounded at ``at``: bars through
:func:`known_bars` (a bar is known once it has closed), prints through the
API's own ``end`` and a second check on each timestamp, news through the
API's ``end`` and the same second check. The one tool that takes a timestamp
is clamped to the instant and says so. Nothing reads the web, a quote from
now, or a bar that had not printed.

Twenty tools in five families. The tape itself (market_quote, candlesticks,
recent_trades, market_at_time, volume_profile); arithmetic with no clock in
it (trading_costs, price_the_edge); the day and the market around this name
(daily_context, relative_volume, market_tape, session_clock); the story
(the_story, news_before, sibling_moves); and the derived reads that do the
counting a decisions model cannot (price_velocity, market_shock,
move_base_rate, tape_imbalance, news_absorption, state_summary).

Moves are in basis points of the last close, because five basis points on
Apple and five on a small-cap are the same forecast, and five cents are not.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

from ..harness.toolcache import NO_CACHE, ToolCache
from ..harness.toolcache import cached as _cached
from ..harness.tools import FunctionTool, Toolbox, ToolResult
from ._bars import (BAR_S, HORIZON_MINUTES, MAX_STALE_S, Bar, bar_at_or_before, known_bars,
                    last_complete_bar, parse_instant, stale_seconds)
from ._news import NewsItem
from ._session import ny_date, prior_weekdays, session, session_close, session_open, to_ny

UTC = timezone.utc

#: The smallest move the metric counts, in basis points. A large cap quotes
#: one to five basis points wide, so a move under five is inside the spread.
TICK_BPS = 5.0

#: Sessions the base rate and the relative volume look back over.
LOOKBACK_SESSIONS = 10

#: Transaction taxes on the sell side, as published for the fiscal year.
#: Revise when the SEC resets Section 31; these are small against a spread.
SEC_FEE_PER_DOLLAR = 27.80 / 1_000_000
TAF_PER_SHARE, TAF_MAX = 0.000166, 8.30


def live_tools(at: datetime, bars: Any, news: Any = None, *, item: NewsItem | None = None,
               symbols_in_story: tuple[str, ...] = ()) -> Toolbox:
    """:func:`replay_tools` at an instant that is happening, never cached."""
    return replay_tools(at, bars, news, NO_CACHE, item=item, symbols_in_story=symbols_in_story)


def replay_tools(at: datetime, bars: Any, news: Any = None, cache: ToolCache | None = None, *,
                 item: NewsItem | None = None, symbols_in_story: tuple[str, ...] = ()) -> Toolbox:
    """Every tool that can be replayed honestly, bounded at ``at``.

    ``bars`` answers ``bars(symbol, start, end)``, ``daily(symbol, before_date,
    days)`` and ``trades(symbol, start, end, limit)``; ``news`` answers
    ``items(symbols, start, end, limit, sort, max_items)``. ``item`` is the
    story this instant belongs to, if there is one.
    """
    cache = cache or ToolCache(None)
    stamp = at.astimezone(UTC).isoformat()
    story = tuple(s.upper() for s in symbols_in_story) or (item.symbols if item else ())
    today = ny_date(at)

    def cached(tool: str, args: dict[str, Any], compute: Callable[[], ToolResult]) -> ToolResult:
        return _cached(cache, stamp, tool, args, compute)

    # -- bounded reads, shared by every tool ---------------------------------

    def known(symbol: str, start: datetime, until: datetime | None = None) -> list[Bar]:
        """Bars that had closed by ``until`` (the instant unless said), from ``start``."""
        until = until or at
        return known_bars(bars.bars(symbol.upper(), start, until), until)

    def last_bar(symbol: str, when: datetime | None = None) -> Bar | None:
        when = when or at
        return last_complete_bar(known(symbol, when - timedelta(seconds=MAX_STALE_S + BAR_S), when),
                                 when)

    def session_bars(symbol: str, day: date) -> list[Bar]:
        """One prior session's regular-hours bars, all of them known."""
        opens, closes = session_open(day), session_close(day)
        return known(symbol, opens, closes)

    def prior_sessions(symbol: str, n: int = LOOKBACK_SESSIONS) -> list[tuple[date, list[Bar]]]:
        """Up to ``n`` sessions before today that printed, nearest first."""
        out: list[tuple[date, list[Bar]]] = []
        for day in prior_weekdays(today, n * 2):
            rows = session_bars(symbol, day)
            if rows:
                out.append((day, rows))
            if len(out) >= n:
                break
        return out

    def move_bps(now: float, then: float) -> float:
        return (now / then - 1.0) * 1e4 if then else 0.0

    # -- the tape ---------------------------------------------------------------

    def quote(symbol: str) -> ToolResult:
        def compute() -> ToolResult:
            bar = last_bar(symbol)
            if bar is None:
                return ToolResult.failed(f"no complete bar on {symbol} within {MAX_STALE_S}s of "
                                         f"the instant")
            out = {"symbol": symbol.upper(), "close": bar.c, "vwap": bar.vwap, "high": bar.h,
                   "low": bar.l, "open": bar.o, "volume": bar.v, "trades": bar.n,
                   "bar_open": bar.ts_open.isoformat(), "stale_s": stale_seconds(bar, at)}
            return ToolResult(ok=True, text=json.dumps(out, default=str), data=out)
        return cached("market_quote", {"symbol": symbol.upper()}, compute)

    def path(symbol: str, hours_back: float = 0.75, timeframe: str = "1Min") -> ToolResult:
        hours = max(0.1, float(hours_back))
        if timeframe not in ("1Min", "1Hour"):
            return ToolResult.failed(f"timeframe {timeframe!r}; use '1Min' or '1Hour'")

        def compute() -> ToolResult:
            rows = known(symbol, at - timedelta(hours=hours))
            if timeframe == "1Hour":
                rows = _hourly(rows)
            out = [{"t": b.ts_open.isoformat(), "o": b.o, "h": b.h, "l": b.l, "c": b.c, "v": b.v,
                    "n": b.n, "vw": round(b.vwap, 4)} for b in rows]
            return ToolResult(ok=True, text=json.dumps(out, default=str),
                              data={"symbol": symbol.upper(), "timeframe": timeframe, "bars": out})
        return cached("candlesticks", {"symbol": symbol.upper(), "hours_back": hours,
                                       "timeframe": timeframe}, compute)

    def tape(symbol: str, minutes_back: int = 5, limit: int = 20) -> ToolResult:
        back, n = max(1, int(minutes_back)), max(1, int(limit))

        def compute() -> ToolResult:
            since = at - timedelta(minutes=back)
            prints = _prints(symbol, since, max(n, 50))
            rows = [{"t": t["ts"].isoformat(), "price": t["p"], "size": t["s"]}
                    for t in prints[:n]]
            out = {"symbol": symbol.upper(), "minutes_back": back, "count": len(rows), "trades": rows}
            return ToolResult(ok=True, text=json.dumps(out, default=str), data=out)
        return cached("recent_trades", {"symbol": symbol.upper(), "minutes_back": back, "limit": n},
                      compute)

    def _prints(symbol: str, since: datetime, limit: int) -> list[dict[str, Any]]:
        """Prints in ``[since, at]``, newest first. Bounded by the API's end and
        checked again here: a fake or a gateway may not honour the bound."""
        rows = []
        for t in bars.trades(symbol.upper(), start=since, end=at, limit=limit):
            ts = _instant(t.get("t"))
            if ts is None or ts > at or ts < since:
                continue
            rows.append({"ts": ts, "p": float(t.get("p") or 0), "s": float(t.get("s") or 0)})
        rows.sort(key=lambda r: r["ts"], reverse=True)
        return rows

    def at_time(symbol: str, when: str) -> ToolResult:
        def compute() -> ToolResult:
            asked = _instant(when)
            if asked is None:
                return ToolResult.failed(f"{when!r} is not an ISO instant")
            # The one place a harness could reach forward by asking, so the
            # answer is clamped rather than refused, and says so.
            capped = min(asked, at)
            bar = last_bar(symbol, capped)
            if bar is None:
                return ToolResult.failed(f"no complete bar on {symbol} at {capped.isoformat()}")
            out = {"symbol": symbol.upper(), "asked_for": asked.isoformat(),
                   "answered_at": capped.isoformat(), "clamped": capped < asked,
                   "close": bar.c, "vwap": bar.vwap, "volume": bar.v,
                   "bar_open": bar.ts_open.isoformat()}
            return ToolResult(ok=True, text=json.dumps(out, default=str), data=out)
        return cached("market_at_time", {"symbol": symbol.upper(), "when": str(when)}, compute)

    def volume(symbol: str, hours_back: float = 3.0) -> ToolResult:
        hours = max(0.1, float(hours_back))

        def compute() -> ToolResult:
            rows = [b for b in known(symbol, at - timedelta(hours=hours)) if b.v > 0]
            if not rows:
                return ToolResult.failed(f"nothing traded on {symbol} in that window")
            width = _bucket_width(rows[-1].c)
            buckets: dict[str, float] = {}
            for b in rows:
                key = f"{round(b.vwap / width) * width:.2f}"
                buckets[key] = buckets.get(key, 0.0) + b.v
            heaviest = max(buckets.items(), key=lambda kv: kv[1])
            total = sum(b.v for b in rows)
            out = {"symbol": symbol.upper(), "hours_back": hours, "volume": round(total, 0),
                   "bars_traded": len(rows), "bucket_width": width,
                   "by_price": {k: round(v, 0) for k, v in sorted(buckets.items(), key=lambda kv: float(kv[0]))},
                   "heaviest_price": float(heaviest[0]), "heaviest_share": round(heaviest[1] / total, 3),
                   "last": rows[-1].c}
            return ToolResult(ok=True, text=json.dumps(out, default=str), data=out)
        return cached("volume_profile", {"symbol": symbol.upper(), "hours_back": hours}, compute)

    # -- arithmetic: no clock in it ----------------------------------------------

    def costs(price: float, shares: int = 100, spread_bps: float = 3.0) -> ToolResult:
        p, n, spread = float(price), max(1, int(shares)), max(0.0, float(spread_bps))
        if p <= 0:
            return ToolResult.failed("price must be positive")
        notional = p * n
        half_spread = notional * spread / 2e4
        sec = notional * SEC_FEE_PER_DOLLAR
        taf = min(n * TAF_PER_SHARE, TAF_MAX)
        round_trip = 2 * half_spread + sec + taf
        out = {"price": p, "shares": n, "notional_usd": round(notional, 2), "spread_bps": spread,
               "half_spread_usd": round(half_spread, 4), "sec_fee_usd": round(sec, 4),
               "taf_usd": round(taf, 4), "round_trip_usd": round(round_trip, 4),
               "breakeven_bps": round(round_trip / notional * 1e4, 2)}
        return ToolResult(ok=True, text=json.dumps(out), data=out)

    def edge(expected_bps: float, half_width_bps: float, cost_bps: float = 3.0) -> ToolResult:
        exp, width, cost = float(expected_bps), max(0.0, float(half_width_bps)), max(0.0, float(cost_bps))
        net = abs(exp) - cost
        ratio = (abs(exp) / width) if width > 0 else None
        out = {"expected_bps": exp, "half_width_bps": width, "cost_bps": cost,
               "net_bps": round(net, 2), "edge_to_width": round(ratio, 3) if ratio is not None else None,
               "direction": "up" if exp > 0 else "down" if exp < 0 else "flat",
               "worth_trading": net > 0 and (ratio is None or ratio >= 0.5),
               "verdict": (f"{abs(exp):.0f} bps expected against {cost:.0f} bps of cost leaves "
                           f"{net:+.0f}" + (", inside the quoted width" if ratio is not None and ratio < 0.5
                                            else ""))}
        return ToolResult(ok=True, text=json.dumps(out), data=out)

    # -- derived reads of the tape ----------------------------------------------

    def velocity(symbol: str) -> ToolResult:
        def compute() -> ToolResult:
            rows = known(symbol, at - timedelta(hours=1))
            if len(rows) < 4:
                return ToolResult.failed(f"too few bars on {symbol} in the last hour")
            closes = [b.c for b in rows]
            steps = sorted(abs(move_bps(b, a)) for a, b in zip(closes, closes[1:]))
            typical = steps[len(steps) // 2] if steps else 0.0

            def move(n: int) -> float:
                return move_bps(closes[-1], closes[-min(n + 1, len(closes))])
            m5 = move(5)
            out = {"symbol": symbol.upper(), "last": closes[-1], "bars": len(rows),
                   "move_1m_bps": round(move(1), 1), "move_3m_bps": round(move(3), 1),
                   "move_5m_bps": round(m5, 1), "typical_minute_move_bps": round(typical, 1),
                   "verdict": ("running" if abs(m5) > 4 * typical + TICK_BPS
                               else "moving" if abs(m5) > 2 * typical + TICK_BPS / 2 else "still")}
            return ToolResult(ok=True, text=json.dumps(out, default=str), data=out)
        return cached("price_velocity", {"symbol": symbol.upper()}, compute)

    def shock(symbol: str, minutes_back: int = 30, threshold_bps: float = 20.0) -> ToolResult:
        back, threshold = max(2, int(minutes_back)), float(threshold_bps)

        def compute() -> ToolResult:
            rows = known(symbol, at - timedelta(minutes=back))
            if len(rows) < 3:
                return ToolResult.failed(f"too few bars on {symbol} in that window")
            latest = rows[-1].c
            jumps = []
            for before, after in zip(rows, rows[1:]):
                move = move_bps(after.c, before.c)
                if abs(move) < threshold:
                    continue
                retraced = max(0.0, min(1.0, (after.c - latest) / (after.c - before.c))) if after.c != before.c else 0.0
                jumps.append({"at": after.ts_open.isoformat(), "move_bps": round(move, 1),
                              "from": before.c, "to": after.c, "retraced": round(retraced, 3)})
            if not jumps:
                out = {"symbol": symbol.upper(), "jumps": [], "last": latest}
                return ToolResult(ok=True, text=f"no move over {threshold:g} bps on {symbol}", data=out)
            jumps.sort(key=lambda j: -abs(j["move_bps"]))
            out = {"symbol": symbol.upper(), "last": latest, "jumps": jumps[:6], "largest": jumps[0]}
            return ToolResult(ok=True, text=json.dumps(out, default=str), data=out)
        return cached("market_shock", {"symbol": symbol.upper(), "minutes_back": back,
                                       "threshold_bps": threshold}, compute)

    # -- the day and the market around it ----------------------------------------

    def daily(symbol: str) -> ToolResult:
        def compute() -> ToolResult:
            prior = [b for b in bars.daily(symbol.upper(), before_date=today, days=20)
                     if ny_date(b.ts_open) < today]
            rows = known(symbol, session_open(today))
            if not rows:
                return ToolResult.failed(f"no regular-hours bar on {symbol} yet today")
            prev_close = prior[-1].c if prior else None
            opened, last = rows[0].o, rows[-1].c
            high, low = max(b.h for b in rows), min(b.l for b in rows)
            ranges = [move_bps(b.h, b.l) for b in prior if b.l]
            moves = sorted(abs(move_bps(b.c, a.c)) for a, b in zip(prior, prior[1:]))
            out = {"symbol": symbol.upper(), "prev_close": prev_close, "today_open": opened,
                   "last": last,
                   "gap_bps": round(move_bps(opened, prev_close), 1) if prev_close else None,
                   "move_from_open_bps": round(move_bps(last, opened), 1),
                   "move_from_prev_close_bps": round(move_bps(last, prev_close), 1) if prev_close else None,
                   "day_high": high, "day_low": low, "range_so_far_bps": round(move_bps(high, low), 1),
                   "position_in_range": round((last - low) / (high - low), 3) if high > low else 0.5,
                   "typical_daily_range_bps": round(sum(ranges) / len(ranges), 1) if ranges else None,
                   "typical_daily_move_bps": round(moves[len(moves) // 2], 1) if moves else None,
                   "sessions": len(prior), "bars_today": len(rows)}
            return ToolResult(ok=True, text=json.dumps(out, default=str), data=out)
        return cached("daily_context", {"symbol": symbol.upper()}, compute)

    def rel_volume(symbol: str, minutes: int = 15) -> ToolResult:
        back = max(1, int(minutes))

        def compute() -> ToolResult:
            since = at - timedelta(minutes=back)
            now_rows = [b for b in known(symbol, since) if b.ts_open >= since]
            volume_now = sum(b.v for b in now_rows)
            local_since, local_at = to_ny(since).timetz(), to_ny(at).timetz()
            typical = []
            for day, rows in prior_sessions(symbol):
                s = datetime.combine(day, local_since)
                e = datetime.combine(day, local_at)
                typical.append(sum(b.v for b in rows if s <= to_ny(b.ts_open) and b.ts_close <= e))
            typical.sort()
            median = typical[len(typical) // 2] if typical else 0.0
            ratio = (volume_now / median) if median else None
            out = {"symbol": symbol.upper(), "minutes": back, "volume": round(volume_now, 0),
                   "typical_volume": round(median, 0), "sessions": len(typical),
                   "ratio": round(ratio, 2) if ratio is not None else None,
                   "verdict": ("no prior sessions to compare" if ratio is None else
                               f"{'heavy' if ratio > 2 else 'light' if ratio < 0.5 else 'normal'}: "
                               f"{ratio:.1f}x the same {back} minutes over {len(typical)} sessions")}
            return ToolResult(ok=True, text=json.dumps(out, default=str), data=out)
        return cached("relative_volume", {"symbol": symbol.upper(), "minutes": back}, compute)

    def market(symbol: str = "SPY") -> ToolResult:
        def compute() -> ToolResult:
            rows = known(symbol, at - timedelta(minutes=40))
            if not rows:
                return ToolResult.failed(f"no bars on {symbol} in the last forty minutes")
            last = rows[-1]

            # A move ending at the last known close, measured from the bar
            # that closed n minutes before it; not from the wall clock, which
            # would lose a bar to the one still open.
            def back(n: int) -> float | None:
                then = bar_at_or_before(rows, last.ts_open - timedelta(minutes=n))
                return None if then is None else round(move_bps(last.c, then.c), 1)
            m5, m15, m30 = back(5), back(15), back(30)
            lead = m15 if m15 is not None else m5 or 0.0
            out = {"symbol": symbol.upper(), "last": last.c, "move_5m_bps": m5, "move_15m_bps": m15,
                   "move_30m_bps": m30,
                   "verdict": ("risk-on" if lead > 10 else "risk-off" if lead < -10 else "flat")}
            return ToolResult(ok=True, text=json.dumps(out, default=str), data=out)
        return cached("market_tape", {"symbol": symbol.upper()}, compute)

    def clock() -> ToolResult:
        out = {"at": at.astimezone(UTC).isoformat(), **session(at)}
        return ToolResult(ok=True, text=json.dumps(out), data=out)

    # -- the story ---------------------------------------------------------------

    def the_story() -> ToolResult:
        if item is None:
            return ToolResult.failed("no story is attached to this instant")
        out = {"id": item.id, "headline": item.headline, "summary": item.summary[:600],
               "source": item.source, "symbols": list(item.symbols),
               "published_at": item.created_at.isoformat(),
               "seconds_since_publication": int((at - item.created_at).total_seconds()),
               "edited_after": item.edited_after, "url": item.url}
        return ToolResult(ok=True, text=json.dumps(out, default=str), data=out)

    def before(symbol: str, hours_back: float = 24.0, limit: int = 10) -> ToolResult:
        hours, n = max(0.1, float(hours_back)), max(1, int(limit))

        def compute() -> ToolResult:
            if news is None:
                return ToolResult.failed("no news source at this instant")
            rows = news.items([symbol.upper()], start=at - timedelta(hours=hours), end=at,
                              limit=min(50, n + 5), sort="desc", max_items=n + 5)
            kept = [r for r in rows if r.created_at <= at and not (item is not None and r.id == item.id)]
            kept.sort(key=lambda r: r.created_at, reverse=True)
            kept = kept[:n]
            items = [{"id": r.id, "published_at": r.created_at.isoformat(), "headline": r.headline,
                      "source": r.source,
                      "minutes_before": round((at - r.created_at).total_seconds() / 60, 1)}
                     for r in kept]
            out = {"symbol": symbol.upper(), "hours_back": hours, "count": len(items),
                   "count_today": sum(1 for r in kept if ny_date(r.created_at) == today),
                   "items": items}
            return ToolResult(ok=True, text=json.dumps(out, default=str), data=out)
        return cached("news_before", {"symbol": symbol.upper(), "hours_back": hours, "limit": n},
                      compute)

    def siblings(symbol: str) -> ToolResult:
        others = tuple(s for s in story if s != symbol.upper())

        def compute() -> ToolResult:
            rows_out = []
            for other in others:
                rows = known(other, at - timedelta(minutes=12))
                if not rows:
                    rows_out.append({"symbol": other, "move_5m_bps": None, "last": None})
                    continue
                then = bar_at_or_before(rows, rows[-1].ts_open - timedelta(minutes=HORIZON_MINUTES))
                rows_out.append({"symbol": other, "last": rows[-1].c,
                                 "move_5m_bps": round(move_bps(rows[-1].c, then.c), 1) if then else None})
            moves = [r["move_5m_bps"] for r in rows_out if r["move_5m_bps"] is not None]
            out = {"symbol": symbol.upper(), "siblings": rows_out, "count": len(rows_out),
                   "mean_move_bps": round(sum(moves) / len(moves), 1) if moves else None,
                   "note": "" if others else "the story names no other symbol"}
            return ToolResult(ok=True, text=json.dumps(out, default=str), data=out)
        return cached("sibling_moves", {"symbol": symbol.upper(), "story": list(others)}, compute)

    # -- derived: the arithmetic a decisions model cannot do ----------------------

    def base_rate(symbol: str) -> ToolResult:
        bucket = _clock_bucket(at)

        def compute() -> ToolResult:
            moves: list[float] = []
            sessions = prior_sessions(symbol)
            for day, rows in sessions:
                for i, b in enumerate(rows):
                    if _clock_bucket(b.ts_open) != bucket:
                        continue
                    target = b.ts_open + timedelta(minutes=HORIZON_MINUTES)
                    later = None
                    for candidate in rows[i + 1:]:
                        if candidate.ts_open > target:
                            break
                        later = candidate
                    if later is not None:
                        moves.append(move_bps(later.c, b.c))
            if not moves:
                out = {"symbol": symbol.upper(), "bucket": bucket, "sessions": len(sessions), "samples": 0,
                       "mean_bps": None, "p10": None, "p50": None, "p90": None, "abs_p50": None,
                       "moved_share": None,
                       "verdict": f"no five-minute history at {bucket} ET over prior sessions"}
                return ToolResult(ok=True, text=json.dumps(out), data=out)
            moves.sort()
            absolute = sorted(abs(m) for m in moves)
            moved = sum(1 for m in moves if abs(m) >= TICK_BPS) / len(moves)
            out = {"symbol": symbol.upper(), "bucket": bucket, "sessions": len(sessions),
                   "samples": len(moves), "mean_bps": round(sum(moves) / len(moves), 1),
                   "p10": round(_quantile(moves, 0.1), 1), "p50": round(_quantile(moves, 0.5), 1),
                   "p90": round(_quantile(moves, 0.9), 1), "abs_p50": round(_quantile(absolute, 0.5), 1),
                   "moved_share": round(moved, 3),
                   "verdict": (f"{'active' if moved >= 0.5 else 'quiet'}: {moved:.0%} of {len(moves)} "
                               f"five-minute windows at {bucket} ET moved {TICK_BPS:g} bps or more, "
                               f"typical size {_quantile(absolute, 0.5):.0f} bps")}
            return ToolResult(ok=True, text=json.dumps(out, default=str), data=out)
        return cached("move_base_rate", {"symbol": symbol.upper(), "bucket": bucket}, compute)

    def imbalance(symbol: str, minutes_back: int = 10) -> ToolResult:
        back = max(1, int(minutes_back))

        def compute() -> ToolResult:
            since = at - timedelta(minutes=back)
            prints = sorted(_prints(symbol, since, 500), key=lambda r: r["ts"])
            # No side on an equity print, so the tick rule: a print above the
            # last price was lifted, below it was hit, level inherits.
            up = down = 0.0
            last_price, direction = None, 0
            for t in prints:
                if last_price is not None:
                    if t["p"] > last_price:
                        direction = 1
                    elif t["p"] < last_price:
                        direction = -1
                    if direction > 0:
                        up += t["s"]
                    elif direction < 0:
                        down += t["s"]
                last_price = t["p"]
            total = up + down
            ratio = (up - down) / total if total else 0.0
            verdict = "buyers" if ratio > 0.2 else "sellers" if ratio < -0.2 else "balanced"
            out = {"symbol": symbol.upper(), "minutes_back": back, "prints": len(prints),
                   "uptick_volume": round(up, 0), "downtick_volume": round(down, 0),
                   "imbalance": round(ratio, 3),
                   "largest_print": round(max((t["s"] for t in prints), default=0.0), 0),
                   "verdict": f"{verdict}: {len(prints)} prints in {back}m, {up:.0f} on upticks against "
                              f"{down:.0f} on downticks"}
            return ToolResult(ok=True, text=json.dumps(out, default=str), data=out)
        return cached("tape_imbalance", {"symbol": symbol.upper(), "minutes_back": back}, compute)

    def absorption(symbol: str) -> ToolResult:
        if item is None:
            return ToolResult.failed("no story is attached to this instant")

        def compute() -> ToolResult:
            then_at = min(item.created_at, at)
            then, now = last_bar(symbol, then_at), last_bar(symbol)
            if now is None:
                return ToolResult.failed(f"no complete bar on {symbol} at the instant")
            recent = known(symbol, at - timedelta(minutes=6))[-4:]
            drift = move_bps(recent[-1].c, recent[0].c) if len(recent) >= 2 else 0.0
            move = move_bps(now.c, then.c) if then is not None else None
            since = int((at - item.created_at).total_seconds())
            out = {"symbol": symbol.upper(), "published_at": item.created_at.isoformat(),
                   "seconds_since_publication": since,
                   "price_at_publication": then.c if then else None, "last": now.c,
                   "move_since_bps": round(move, 1) if move is not None else None,
                   "drift_last_3_bars_bps": round(drift, 1), "still_drifting": abs(drift) >= TICK_BPS,
                   "verdict": ((f"moved {move:+.0f} bps in the {since}s since publication"
                                if move is not None else "no price at publication")
                               + (", still drifting" if abs(drift) >= TICK_BPS else ", settled"))}
            return ToolResult(ok=True, text=json.dumps(out, default=str), data=out)
        return cached("news_absorption", {"symbol": symbol.upper(), "story": item.id}, compute)

    def summary(symbol: str) -> ToolResult:
        """One paragraph, through the other tools so it shares their cache and
        cannot disagree with what they said."""
        def compute() -> ToolResult:
            q = quote(symbol)
            if not q.ok:
                return ToolResult.failed(q.error or "no quote")
            book = q.data
            parts = [f"{symbol.upper()} last {book['close']:.2f} (vwap {book['vwap']:.2f}, "
                     f"{book['stale_s']}s old)."]
            rows = known(symbol, at - timedelta(minutes=6))[-5:]
            if len(rows) >= 2:
                parts.append(f"Last {len(rows)} bars net {move_bps(rows[-1].c, rows[0].c):+.0f} bps.")
            if item is not None:
                parts.append(f"Story ({item.source}, {int((at - item.created_at).total_seconds())}s ago): "
                             f"{item.headline[:90]}")
            tape_now = market("SPY")
            if tape_now.ok:
                parts.append(f"SPY {tape_now.data['move_15m_bps'] if tape_now.data['move_15m_bps'] is not None else 0:+.0f} bps/15m "
                             f"({tape_now.data['verdict']}).")
            day = daily(symbol)
            if day.ok and day.data.get("gap_bps") is not None:
                parts.append(f"Day: gap {day.data['gap_bps']:+.0f}, from open {day.data['move_from_open_bps']:+.0f}, "
                             f"range {day.data['range_so_far_bps']:.0f} bps at {day.data['position_in_range']:.0%}.")
            rate = base_rate(symbol)
            if rate.ok:
                parts.append(f"Base rate: {rate.data['verdict']}.")
            text = " ".join(parts)
            if len(text) > 600:
                text = text[:597].rstrip() + "..."
            out = {"symbol": symbol.upper(), "summary": text, "last": book["close"]}
            return ToolResult(ok=True, text=text, data=out)
        return cached("state_summary", {"symbol": symbol.upper(), "story": item.id if item else None},
                      compute)

    sym = {"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]}
    return Toolbox([
        FunctionTool(name="market_quote",
                     description=("The last complete minute bar on one symbol as of now: close, vwap, "
                                  "high, low, volume, print count and how many seconds old it is."),
                     parameters=sym, fn=quote),
        FunctionTool(name="candlesticks",
                     description=("Minute bars for one symbol up to now, or hourly with timeframe "
                                  "'1Hour'. hours_back defaults to 0.75."),
                     parameters={"type": "object",
                                 "properties": {"symbol": {"type": "string"},
                                                "hours_back": {"type": "number"},
                                                "timeframe": {"type": "string"}},
                                 "required": ["symbol"]},
                     fn=path),
        FunctionTool(name="recent_trades",
                     description="The print tape for one symbol, newest first, from just before now.",
                     parameters={"type": "object",
                                 "properties": {"symbol": {"type": "string"},
                                                "minutes_back": {"type": "integer"},
                                                "limit": {"type": "integer"}},
                                 "required": ["symbol"]},
                     fn=tape),
        FunctionTool(name="market_at_time",
                     description=("The last complete bar on one symbol at an earlier instant. Times "
                                  "after now are answered with now and flagged, never refused."),
                     parameters={"type": "object",
                                 "properties": {"symbol": {"type": "string"},
                                                "when": {"type": "string", "description": "ISO 8601 UTC"}},
                                 "required": ["symbol", "when"]},
                     fn=at_time),
        FunctionTool(name="volume_profile",
                     description=("Where one symbol traded, by price, over the hours before now: "
                                  "the price the tape keeps coming back to."),
                     parameters={"type": "object",
                                 "properties": {"symbol": {"type": "string"},
                                                "hours_back": {"type": "number"}},
                                 "required": ["symbol"]},
                     fn=volume),
        FunctionTool(name="trading_costs",
                     description=("What a round trip costs at a price: half the spread each way, "
                                  "the SEC and TAF cents on the sell, and the move in basis "
                                  "points that breaks even."),
                     parameters={"type": "object",
                                 "properties": {"price": {"type": "number"},
                                                "shares": {"type": "integer"},
                                                "spread_bps": {"type": "number"}},
                                 "required": ["price"]},
                     fn=costs),
        FunctionTool(name="price_the_edge",
                     description=("An expected move, a quoted half-width and a cost, all in basis "
                                  "points, into what is left and whether it is worth trading."),
                     parameters={"type": "object",
                                 "properties": {"expected_bps": {"type": "number"},
                                                "half_width_bps": {"type": "number"},
                                                "cost_bps": {"type": "number"}},
                                 "required": ["expected_bps", "half_width_bps"]},
                     fn=edge),
        FunctionTool(name="price_velocity",
                     description=("How fast this symbol is moving against its own last hour: one, "
                                  "three and five minute moves in basis points against the typical "
                                  "minute. Says still, moving or running."),
                     parameters=sym, fn=velocity),
        FunctionTool(name="market_shock",
                     description=("Jumps in the recent path over a threshold in basis points and "
                                  "how much of each came back. A jump that holds reads as "
                                  "information; one that snaps back was a thin book."),
                     parameters={"type": "object",
                                 "properties": {"symbol": {"type": "string"},
                                                "minutes_back": {"type": "integer"},
                                                "threshold_bps": {"type": "number"}},
                                 "required": ["symbol"]},
                     fn=shock),
        FunctionTool(name="daily_context",
                     description=("The day so far against the last twenty sessions: previous close, "
                                  "today's open, the gap and the move from the open in basis points, "
                                  "the range so far and where the last price sits in it."),
                     parameters=sym, fn=daily),
        FunctionTool(name="relative_volume",
                     description=("Volume over the last minutes against the same clock window over "
                                  "the prior ten sessions. Says heavy, normal or light."),
                     parameters={"type": "object",
                                 "properties": {"symbol": {"type": "string"},
                                                "minutes": {"type": "integer"}},
                                 "required": ["symbol"]},
                     fn=rel_volume),
        FunctionTool(name="the_story",
                     description=("The news item this instant belongs to: headline, summary, source, "
                                  "the symbols it names, seconds since publication, and whether the "
                                  "text was edited after it broke."),
                     parameters={"type": "object", "properties": {}}, fn=the_story),
        FunctionTool(name="news_before",
                     description=("Earlier items on one symbol, newest first, up to now; the story "
                                  "itself excluded. Says how many there were today. A name already "
                                  "in the news has already been repriced."),
                     parameters={"type": "object",
                                 "properties": {"symbol": {"type": "string"},
                                                "hours_back": {"type": "number"},
                                                "limit": {"type": "integer"}},
                                 "required": ["symbol"]},
                     fn=before),
        FunctionTool(name="sibling_moves",
                     description=("The five-minute move of every other symbol the story names, in "
                                  "basis points, as of now. A story that moved its siblings "
                                  "is being read by the market."),
                     parameters=sym, fn=siblings),
        FunctionTool(name="market_tape",
                     description=("The index ETF's five, fifteen and thirty minute moves in basis "
                                  "points as of now, SPY by default (QQQ for the Nasdaq). What is "
                                  "market-wide is not the story's."),
                     parameters={"type": "object", "properties": {"symbol": {"type": "string"}}},
                     fn=market),
        FunctionTool(name="session_clock",
                     description=("Where the instant sits in the session: phase, minutes since the "
                                  "open and to the close, weekday, the clock in New York."),
                     parameters={"type": "object", "properties": {}}, fn=clock),
        FunctionTool(name="move_base_rate",
                     description=("What five-minute moves look like on this symbol at this time of "
                                  "day over the prior ten sessions: count, mean, p10/p50/p90 in "
                                  "basis points, the share that moved five bps or more."),
                     parameters=sym, fn=base_rate),
        FunctionTool(name="tape_imbalance",
                     description=("Who has been hitting the tape over the last minutes: volume on "
                                  "upticks against downticks, an imbalance from -1 to 1, the print "
                                  "count and the largest print. Says buyers, sellers or balanced."),
                     parameters={"type": "object",
                                 "properties": {"symbol": {"type": "string"},
                                                "minutes_back": {"type": "integer"}},
                                 "required": ["symbol"]},
                     fn=imbalance),
        FunctionTool(name="news_absorption",
                     description=("How far the price has moved since the story broke and whether "
                                  "it is still drifting over the last three bars."),
                     parameters=sym, fn=absorption),
        FunctionTool(name="state_summary",
                     description=("One paragraph of the whole situation: the last bar, the last "
                                  "five, the story, SPY, the day so far and the base rate. Under "
                                  "600 characters; the state a decisions model reads best."),
                     parameters=sym, fn=summary),
    ])


# -- helpers -------------------------------------------------------------------

def _hourly(rows: list[Bar]) -> list[Bar]:
    out: list[Bar] = []
    group: list[Bar] = []
    key = None
    for b in rows + [None]:                                  # type: ignore[list-item]
        k = None if b is None else b.ts_open.replace(minute=0, second=0, microsecond=0)
        if group and k != key:
            vol = sum(x.v for x in group)
            out.append(Bar(symbol=group[0].symbol, ts_open=key, o=group[0].o,     # type: ignore[arg-type]
                           h=max(x.h for x in group), l=min(x.l for x in group), c=group[-1].c,
                           v=vol, n=sum(x.n for x in group),
                           vwap=(sum(x.vwap * x.v for x in group) / vol) if vol else group[-1].c))
            group = []
        if b is not None:
            group.append(b)
            key = k
    return out


def _bucket_width(price: float) -> float:
    return 0.01 if price < 20 else 0.05 if price < 100 else 0.10 if price < 500 else 0.50


def _clock_bucket(at: datetime) -> str:
    """Thirty-minute buckets of the New York clock: the open is not the lunch hour."""
    local = to_ny(at)
    start = local.replace(minute=0 if local.minute < 30 else 30, second=0, microsecond=0)
    end = start + timedelta(minutes=30)
    return f"{start:%H:%M}-{end:%H:%M}"


def _quantile(sorted_values: list[float], q: float) -> float:
    if not sorted_values:
        return 0.0
    pos = q * (len(sorted_values) - 1)
    lo, hi = int(pos), min(int(pos) + 1, len(sorted_values) - 1)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo)


def _instant(raw: Any) -> datetime | None:
    if not raw:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=UTC)
    try:
        return parse_instant(str(raw))
    except ValueError:
        return None


__all__ = ["replay_tools", "live_tools", "TICK_BPS", "LOOKBACK_SESSIONS", "HORIZON_MINUTES",
           "MAX_STALE_S"]
