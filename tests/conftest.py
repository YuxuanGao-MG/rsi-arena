"""Fakes: a model that answers from a script, and a history built from a price series."""

from __future__ import annotations

import json
from datetime import datetime, time, timedelta, timezone
from typing import Any, Callable

import pytest

from rsi_arena.alpaca._bars import MAX_STALE_S, Bar, last_complete_bar
from rsi_arena.alpaca._news import NewsItem
from rsi_arena.alpaca._session import NY
from rsi_arena.harness import Completion, Decision
from rsi_arena.kalshi._history import Candle

UTC = timezone.utc


class FakeLLM:
    """Answers by calling ``script(messages, schema, tools)``; counts calls."""

    def __init__(self, script: Callable[..., Any] | None = None, cost: float = 0.001,
                 decisions: Callable[..., Any] | None = None) -> None:
        self.script = script or (lambda messages, schema, tools: "ok")
        self.cost = cost
        self.calls: list[dict[str, Any]] = []
        #: ``decisions(state, questions) -> answers`` for a decisions model.
        #: The default answers every score question with all its mass on the
        #: middle level, every noul with 0.5 and every choice with its first label.
        self.decisions = decisions
        self.decided: list[dict[str, Any]] = []

    async def decide(self, state, questions, *, model) -> Decision:
        self.decided.append({"state": state, "questions": questions, "model": model})
        if self.decisions is not None:
            answers = self.decisions(state, questions)
        else:
            answers = {}
            for key, q in questions.items():
                if q["type"] == "score":
                    n = len(q["criteria"]); mid = (n - 1) // 2
                    answers[key] = {"type": "score", "score": float(mid),
                                    "probabilities": {str(i): (1.0 if i == mid else 0.0) for i in range(n)},
                                    "confidence": 1.0}
                elif q["type"] == "noul":
                    answers[key] = {"type": "noul", "noul": 0.5}
                else:
                    first = next(iter(q["criteria"]))
                    answers[key] = {"type": "choice", "choice": first,
                                    "probabilities": {k: (1.0 if k == first else 0.0) for k in q["criteria"]},
                                    "confidence": 1.0}
        return Decision(answers=answers, cost_usd=self.cost / 1000, model=model)

    async def complete(self, messages, *, model, system=None, schema=None, tools=None,
                       temperature=None, max_tokens=None) -> Completion:
        self.calls.append({"messages": messages, "model": model, "system": system,
                           "schema": schema, "tools": tools})
        answer = self.script(messages, schema, tools)
        if isinstance(answer, dict) and "tool_calls" in answer:
            return Completion(text="", message={"role": "assistant", "content": None,
                                                "tool_calls": answer["tool_calls"]},
                              tool_calls=answer["tool_calls"], cost_usd=self.cost)
        text = answer if isinstance(answer, str) else json.dumps(answer)
        return Completion(text=text, message={"role": "assistant", "content": text},
                          cost_usd=self.cost)


def candle(ticker: str, ts: datetime, mid: float, spread: float = 0.02, volume: float = 1.0,
           two_sided: bool = True) -> Candle:
    bid, ask = (mid - spread / 2, mid + spread / 2) if two_sided else (0.0, 1.0)
    return Candle(ticker=ticker, ts=ts, yes_bid_open=bid, yes_bid_close=bid, yes_bid_high=bid,
                  yes_bid_low=bid, yes_ask_open=ask, yes_ask_close=ask, yes_ask_high=ask,
                  yes_ask_low=ask, price_open=mid, price_close=mid, price_high=mid, price_low=mid,
                  price_mean=mid, price_previous=mid, volume=volume, open_interest=100.0)


class FakeHistory:
    """Minute candles from ``series[ticker] = {minute_offset: mid}`` starting at ``t0``."""

    def __init__(self, t0: datetime, series: dict[str, dict[int, float]],
                 dead: set[tuple[str, int]] | None = None) -> None:
        self.t0, self.series, self.dead = t0, series, dead or set()
        self.trade_calls: list[dict[str, Any]] = []

    def _candles(self, ticker: str) -> list[Candle]:
        out = []
        for minute, mid in sorted(self.series.get(ticker, {}).items()):
            ts = self.t0 + timedelta(minutes=minute)
            out.append(candle(ticker, ts, mid, two_sided=(ticker, minute) not in self.dead))
        return out

    def quote_at(self, ticker: str, when: datetime, interval: int = 1) -> Candle | None:
        usable = [c for c in self._candles(ticker) if c.ts <= when]
        return usable[-1] if usable else None

    def price_path(self, ticker: str, start=None, end=None, interval: int = 1,
                   clip_to_close: bool = True) -> list[Candle]:
        return [c for c in self._candles(ticker)
                if (start is None or c.ts >= start) and (end is None or c.ts <= end)]

    def trades(self, ticker: str, start=None, end=None, max_trades=None) -> list[dict]:
        self.trade_calls.append({"ticker": ticker, "start": start, "end": end, "max": max_trades})
        return [{"created_time": (end or self.t0).isoformat(), "yes_price_dollars": "0.50",
                 "count_fp": "3", "taker_side": "yes"}]


class FakeBars:
    """IEX minute bars from ``series[symbol] = {minute_offset: close}`` starting
    at ``t0``; a value may be ``(close, volume)``. ``daily[symbol] = {date: close}``
    stands in for the daily endpoint and ``prints`` for the trades one.

    Point in time is the shared rule, not a fake of it: ``price_at`` goes
    through :func:`last_complete_bar` exactly as the real source does.
    ``leaky`` makes ``trades`` and ``bars`` ignore ``end``, the way a gateway
    that does not honour its bound would, so a tool's own check is what a
    test then exercises.
    """

    def __init__(self, t0: datetime, series: dict[str, dict[int, Any]],
                 daily: dict[str, dict[Any, float]] | None = None,
                 prints: list[dict[str, Any]] | None = None, volume: float = 100.0,
                 leaky: bool = False) -> None:
        self.t0, self.series = t0, series
        self.daily_rows = daily or {}
        self.prints = prints or []
        self.volume, self.leaky = volume, leaky
        self.bar_calls: list[dict[str, Any]] = []
        self.trade_calls: list[dict[str, Any]] = []
        self.daily_calls: list[dict[str, Any]] = []

    def _bars(self, symbol: str) -> list[Bar]:
        out = []
        for minute, value in sorted(self.series.get(symbol, {}).items()):
            close, vol = (value if isinstance(value, tuple) else (value, self.volume))
            ts = self.t0 + timedelta(minutes=minute)
            out.append(Bar(symbol=symbol, ts_open=ts, o=close, h=close * 1.0002, l=close * 0.9998,
                           c=close, v=vol, n=10, vwap=close))
        return out

    def bars(self, symbol: str, start: datetime, end: datetime, timeframe: str = "1Min") -> list[Bar]:
        self.bar_calls.append({"symbol": symbol, "start": start, "end": end, "timeframe": timeframe})
        return [b for b in self._bars(symbol)
                if b.ts_open >= start and (self.leaky or b.ts_open <= end)]

    def price_at(self, symbol: str, at: datetime, max_stale_s: int = MAX_STALE_S) -> float | None:
        bar = last_complete_bar(self.bars(symbol, at - timedelta(seconds=max_stale_s + 60), at),
                                at, max_stale_s)
        return None if bar is None else bar.c

    def realised_price(self, symbol: str, at: datetime, horizon: int = 5) -> float | None:
        return self.price_at(symbol, at + timedelta(minutes=horizon))

    def daily(self, symbol: str, before_date, days: int = 20) -> list[Bar]:
        self.daily_calls.append({"symbol": symbol, "before": before_date, "days": days})
        rows = []
        for day, close in sorted(self.daily_rows.get(symbol, {}).items()):
            if day >= before_date:
                continue
            ts = datetime.combine(day, time(0), tzinfo=NY).astimezone(UTC)
            rows.append(Bar(symbol=symbol, ts_open=ts, o=close, h=close * 1.01, l=close * 0.99,
                            c=close, v=1e6, n=1000, vwap=close))
        return rows[-days:]

    def trades(self, symbol: str, start: datetime, end: datetime, limit: int = 50) -> list[dict]:
        self.trade_calls.append({"symbol": symbol, "start": start, "end": end, "limit": limit})
        rows = [{"t": p["t"].isoformat(), "p": p["p"], "s": p["s"]}
                for p in self.prints
                if p.get("symbol", symbol) == symbol and p["t"] >= start
                and (self.leaky or p["t"] <= end)]
        rows.sort(key=lambda r: r["t"], reverse=True)
        return rows[:limit]


class FakeNews:
    """News items in memory. Honours ``start``/``end`` like the API unless ``leaky``."""

    def __init__(self, items: list[NewsItem], leaky: bool = False) -> None:
        self.items_all, self.leaky = list(items), leaky
        self.calls: list[dict[str, Any]] = []

    def items(self, symbols, start: datetime, end: datetime, limit: int = 50, sort: str = "asc",
              max_items: int | None = None) -> list[NewsItem]:
        self.calls.append({"symbols": list(symbols), "start": start, "end": end, "limit": limit,
                           "sort": sort, "max_items": max_items})
        wanted = {s.upper() for s in symbols}
        rows = [i for i in self.items_all if wanted & set(i.symbols)
                and (self.leaky or start <= i.created_at <= end)]
        rows.sort(key=lambda i: i.created_at, reverse=(sort == "desc"))
        return rows[:max_items] if max_items else rows


@pytest.fixture
def t0() -> datetime:
    return datetime(2026, 8, 23, 15, 0, tzinfo=UTC)


@pytest.fixture
def history(t0) -> FakeHistory:
    # Contract A drifts up a cent a minute; contract B is flat.
    return FakeHistory(t0, {"A": {m: 0.40 + 0.01 * m for m in range(0, 40)},
                            "B": {m: 0.50 for m in range(0, 40)}})


# -- the crypto topic's sources, faked -------------------------------------------

from rsi_arena.crypto._binance import AggTrade, Kline  # noqa: E402


def ramp(minutes: int, start: float = 100.0, bps_per_minute: float = 1.0) -> dict[int, float]:
    """A close series rising ``bps_per_minute`` basis points a minute, compounded, from ``start``."""
    return {m: start * (1 + bps_per_minute / 1e4) ** m for m in range(minutes)}


class FakeKlines:
    """Minute bars from ``series[symbol] = {minute_offset: close}`` starting at ``t0``.

    Each bar opens at its offset and closes a minute later, like the exchange's.
    ``calls`` counts reads, so a test can see whether a cache or a windows file
    answered instead.
    """

    def __init__(self, t0: datetime, series: dict[str, dict[int, float]]) -> None:
        self.t0, self.series = t0, series
        self.calls: list[tuple[str, datetime, datetime]] = []

    def klines(self, symbol: str, start: datetime, end: datetime, interval: str = "1m") -> list[Kline]:
        self.calls.append((symbol, start, end))
        out = []
        for m, close in sorted(self.series.get(symbol, {}).items()):
            ts = self.t0 + timedelta(minutes=m)
            if start <= ts < end:
                out.append(Kline(ts_open=ts, open=close, high=close * 1.0001, low=close * 0.9999,
                                 close=close, volume=1.0, quote_volume=close, trades=10,
                                 taker_buy_volume=0.6))
        return out


class FakeAggTrades:
    """A tape of ``(ts, price, qty, buyer_maker)`` that honours ``end`` like the API's endTime."""

    def __init__(self, prints: list[tuple[datetime, float, float, bool]]) -> None:
        self.prints = prints
        self.calls = 0

    def agg_trades(self, symbol: str, start: datetime, end: datetime, limit: int = 1000) -> list[AggTrade]:
        self.calls += 1
        return [AggTrade(ts=ts, price=p, qty=q, buyer_maker=m) for ts, p, q, m in self.prints
                if start <= ts <= end][:limit]


class FakeFutures:
    """Funding, open interest and perp rows as the store hands them back: oldest first, ``ts`` in ms."""

    def __init__(self, funding: list[dict] | None = None, oi: list[dict] | None = None,
                 perp: list[dict] | None = None) -> None:
        self._funding, self._oi, self._perp = funding or [], oi or [], perp or []

    def funding(self, symbol: str) -> list[dict]:
        return sorted(self._funding, key=lambda r: r["ts"])

    def oi(self, symbol: str) -> list[dict]:
        return sorted(self._oi, key=lambda r: r["ts"])

    def perp(self, symbol: str) -> list[dict]:
        return sorted(self._perp, key=lambda r: r["ts"])


def FakeFunding(rows: list[dict]) -> FakeFutures:  # noqa: N802 - a fake by the name the tests use
    return FakeFutures(funding=rows)


def FakeOI(rows: list[dict]) -> FakeFutures:  # noqa: N802
    return FakeFutures(oi=rows)


class FakeDaily:
    """On-chain series: ``series[name] = (kind, rows)`` with ``ts`` in seconds, sliced by the store's rules."""

    def __init__(self, series: dict[str, tuple[str, list[dict]]]) -> None:
        self.series = series

    def kind(self, name: str) -> str:
        return self.series[name][0]

    def rows(self, name: str) -> list[dict]:
        return sorted(self.series.get(name, ("daily", []))[1], key=lambda r: r["ts"])

    def first_recorded(self, name: str) -> datetime | None:
        rows = self.rows(name)
        return datetime.fromtimestamp(int(rows[0]["ts"]), tz=UTC) if rows else None

    def known_at(self, name: str, at: datetime) -> list[dict]:
        from rsi_arena.crypto._onchain import block_at, daily_before
        rows = self.rows(name)
        kind = self.series.get(name, ("daily", []))[0]
        return daily_before(rows, at) if kind == "daily" else block_at(rows, at)


@pytest.fixture
def c0() -> datetime:
    """Noon UTC on a Sunday: no US session, funding at 16:00."""
    return datetime(2026, 8, 23, 12, 0, tzinfo=UTC)


@pytest.fixture
def spot(c0) -> FakeKlines:
    # BTC climbs a basis point a minute for two days; ETH is flat; SOL climbs twice as fast.
    return FakeKlines(c0, {"BTCUSDT": ramp(2 * 1440, 100000.0, 1.0),
                           "ETHUSDT": {m: 4000.0 for m in range(2 * 1440)},
                           "SOLUSDT": ramp(2 * 1440, 200.0, 2.0)})
