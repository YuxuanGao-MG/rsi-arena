"""Live crypto collection: a tick forecasts every coin, keeps the book, and never caches."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from rsi_arena.harness import Harness
from rsi_arena.harness.toolcache import ToolCache
from tests.conftest import FakeKlines, FakeLLM, ramp
from tests.test_live import load_script

UTC = timezone.utc


@pytest.fixture
def no_cache_allowed(monkeypatch):
    """Any read or write of the disk tool cache is an error. ``NO_CACHE`` overrides both."""
    def boom(*args, **kwargs):
        raise AssertionError("a live market must not go through the tool cache")

    monkeypatch.setattr(ToolCache, "get", boom)
    monkeypatch.setattr(ToolCache, "put", boom)


class FakeBook(FakeKlines):
    """Bars plus a live book, the two things a live spot source answers."""

    def __init__(self, t0, series):
        super().__init__(t0, series)
        self.depth_calls = 0

    def depth(self, symbol: str, limit: int = 20) -> dict:
        self.depth_calls += 1
        px = max(self.series[symbol].values())
        return {"bids": [[px - i, 1.0] for i in range(1, limit + 1)],
                "asks": [[px + i, 2.0] for i in range(1, limit + 1)], "last_update_id": 7}


class FakeSources:
    def __init__(self, spot: FakeBook, symbols=("BTCUSDT", "ETHUSDT")) -> None:
        self.spot, self.symbols, self.futures, self.onchain = spot, symbols, None, None

    def realised(self, ticker: str, at: datetime, horizon: int) -> float | None:
        from rsi_arena.crypto._binance import close_at
        symbol = ticker.split("@", 1)[0]
        when = at + timedelta(minutes=horizon)
        k = close_at(self.spot.klines(symbol, when - timedelta(minutes=4), when), when)
        return None if k is None else k.close


def harness() -> Harness:
    return Harness.from_dict({
        "name": "live-crypto-test", "context": "forecast", "tools": ["market_quote", "cross_asset"],
        "config": {"max_usd": 1.0},
        "plan": {"steps": [
            {"type": "tool", "name": "q", "tool": "market_quote",
             "args": {"symbol": "{{question}}"}, "output_key": "q"},
            {"type": "tool", "name": "x", "tool": "cross_asset",
             "args": {"symbol": "{{question}}"}, "output_key": "x", "fail_ok": True},
            {"type": "prompt", "name": "p", "prompt": "price {{q.price}} calendar {{context}}",
             "output_schema": {"type": "object"}, "output_key": "out"},
        ]}})


class Clock:
    """A clock the sweep reads and its sleeps advance, so a two-tick sweep takes no time."""

    def __init__(self, start: datetime) -> None:
        self.t = start
        self.slept: list[float] = []

    def now(self) -> datetime:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += timedelta(seconds=seconds)


async def test_a_sweep_forecasts_each_symbol_once_a_tick_and_keeps_the_book(tmp_path, no_cache_allowed):
    live = load_script("collect_live_crypto")
    # Bars from a fortnight ago, so every forecast's minute has long since printed
    # and the row resolves in the same pass it was written: the grader reads the
    # wall clock, and the fake clock only drives the cadence.
    t0 = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(days=14)
    spot = FakeBook(t0, {"BTCUSDT": ramp(120, 100000.0, 1.0), "ETHUSDT": {m: 4000.0 for m in range(120)}})
    sources = FakeSources(spot)
    llm = FakeLLM(lambda messages, schema, tools: {"delta_bps": 1.0, "half_width_bps": 3.0})
    clock = Clock(t0 + timedelta(minutes=30))
    out, books = tmp_path / "f.jsonl", tmp_path / "b.jsonl"

    summary = await live.sweep(harness(), llm, sources, minutes=1.5, every=60, out=out,
                               books_out=books, horizon=1, now=clock.now, sleep=clock.sleep,
                               mempool_fn=lambda: {"tx_count": 42})

    assert summary["ticks"] == 2 and summary["forecasts"] == 4 and summary["unresolved"] == 0
    assert not summary["starved"] and not summary["over_budget"]
    assert len(llm.calls) == 4, "one model call per coin per tick"
    rows = [json.loads(line) for line in out.read_text().splitlines()]
    assert len(rows) == 4
    ats = sorted({r["at"] for r in rows})
    assert len(ats) == 2 and datetime.fromisoformat(ats[1]) - datetime.fromisoformat(ats[0]) == timedelta(seconds=60)
    for r in rows:
        assert r["topic"] == "crypto-horizon-1m" and r["venue"] == "binance-spot" and r["unit"] == "bps"
        assert r["ticker"] == f"{r['symbol']}@{r['at']}" and r["horizon_minutes"] == 1
        assert r["ok"] and r["output"]["delta_bps"] == 1.0
        assert r["scored"]["skill"] is not None and r["realised"] is not None, "resolved after the horizon"
        assert r["context"]["book"]["bid"] < r["context"]["book"]["ask"]
        assert r["context"]["mempool"] == {"tx_count": 42}
        assert any(s["kind"] == "tool" for s in r["run"]["trace"]), "a tool really ran, through no cache"
    btc = [r for r in rows if r["symbol"] == "BTCUSDT"][0]
    assert abs((btc["realised"] / btc["mid_now"] - 1) * 1e4 - 1.0) < 0.05, "a basis point a minute"
    assert btc["scored"]["skill"] == pytest.approx(0.5, abs=0.01), \
        "the basis point called exactly: one removed of a benchmark floored at the two-bps tick"
    # One book per forecast, keyed the way rsi.book_snapshots is.
    snaps = [json.loads(line) for line in books.read_text().splitlines()]
    assert len(snaps) == 4 and spot.depth_calls == 4
    assert {(s["topic"], s["symbol"], s["at"]) for s in snaps} == {(r["topic"], r["symbol"], r["at"]) for r in rows}
    assert snaps[0]["book"]["order_book"]["levels"] == 20 and snaps[0]["book"]["mempool"] == {"tx_count": 42}
    assert snaps[0]["book"]["last_close"] == rows[0]["mid_now"]


async def test_a_forecast_is_pending_until_its_minute_prints(tmp_path):
    live = load_script("collect_live_crypto")
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    spot = FakeBook(now - timedelta(minutes=10), {"BTCUSDT": ramp(30, 100000.0, 1.0)})
    sources = FakeSources(spot, symbols=("BTCUSDT",))
    out = tmp_path / "f.jsonl"

    fresh = {"at": now.isoformat(), "ticker": f"BTCUSDT@{now.isoformat()}", "mid_now": 100000.0,
             "output": {"delta_bps": 1.0}}
    assert live.write_resolved([fresh], sources, out, 1) == [fresh]
    assert not out.exists() or not out.read_text()

    old = dict(fresh, at=(now - timedelta(minutes=5)).isoformat(),
               ticker=f"BTCUSDT@{(now - timedelta(minutes=5)).isoformat()}")
    assert live.write_resolved([old], sources, out, 1) == []
    written = json.loads(out.read_text())
    assert written["realised"] is not None and written["scored"]["skill"] is not None
    # No forecast is a recorded fact, not an exception.
    silent = dict(old, output=None)
    assert live.resolve(silent, sources, 1) is True and silent["scored"] is None
    assert "no forecast" in silent["unscored_because"]


async def test_three_provider_failures_stop_the_sweep_green(tmp_path):
    live = load_script("collect_live_crypto")
    from rsi_arena.harness.llm import LLMError

    class Refusing(FakeLLM):
        async def complete(self, *a, **k):
            raise LLMError(402, "insufficient credits")

    t0 = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(days=14)
    spot = FakeBook(t0, {"BTCUSDT": ramp(120), "ETHUSDT": ramp(120), "SOLUSDT": ramp(120)})
    sources = FakeSources(spot, symbols=("BTCUSDT", "ETHUSDT", "SOLUSDT"))
    clock = Clock(t0 + timedelta(minutes=30))
    summary = await live.sweep(harness(), Refusing(), sources, minutes=10, every=60,
                               out=tmp_path / "f.jsonl", books_out=tmp_path / "b.jsonl", horizon=1,
                               now=clock.now, sleep=clock.sleep)
    assert summary["starved"] and summary["ticks"] == 1 and summary["forecasts"] == 3


def test_the_publish_rows_carry_the_topic_columns_and_a_ticker():
    pytest.importorskip("psycopg2")
    pub = load_script("publish_live")
    books = load_script("publish_books")
    at = "2026-09-21T14:05:00+00:00"
    row = {"at": at, "topic": "crypto-horizon-1m", "symbol": "BTCUSDT", "venue": "binance-spot",
           "ticker": f"BTCUSDT@{at}", "unit": "bps", "mid_now": 81586.01, "realised": 81594.1,
           "harness": "crypto-horizon-1m-jev", "output": {"delta_bps": 0.4, "half_width_bps": 2.0},
           "context": {"hour_utc": 14, "book": {"bid": 81585.0, "ask": 81586.0}},
           "run": {"trace": []}, "ok": True, "error": None, "scored": {"skill": 0.1}}
    values = pub.live_row(row, pub.TOPIC_COLUMNS)
    assert len(values) == len(pub.BASE_COLUMNS) + len(pub.TOPIC_COLUMNS)
    named = dict(zip(pub.BASE_COLUMNS + pub.TOPIC_COLUMNS, values))
    assert named["ticker"] == f"BTCUSDT@{at}" and named["league"] is None and named["game_id"] is None
    assert (named["topic"], named["symbol"], named["venue"], named["unit"]) == \
        ("crypto-horizon-1m", "BTCUSDT", "binance-spot", "bps")
    assert named["context"].adapted["book"]["bid"] == 81585.0 and named["skill"] == 0.1

    snap = {"topic": "crypto-horizon-1m", "symbol": "BTCUSDT", "at": at,
            "book": {"order_book": {"bid": 81585.0, "ask": 81586.0}, "mempool": None}}
    topic, symbol, at_, book = books.book_row(snap)
    assert (topic, symbol, at_) == ("crypto-horizon-1m", "BTCUSDT", at)
    assert book.adapted["order_book"]["ask"] == 81586.0
