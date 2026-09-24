"""``scripts/path_windows.py``: the realised path goes onto a built question
set from the bars, and nothing else about the set moves."""

from __future__ import annotations

import gzip
import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from rsi_arena.alpaca._bars import Bar, BarStore
from rsi_arena.crypto._binance import BinanceSpot, Kline, KlineStore
from rsi_arena.topics.crypto_horizon import CryptoWindow
from rsi_arena.topics.kalshi_horizon import Window
from rsi_arena.topics.news_equity.windows import NewsWindow
from tests.conftest import FakeHistory

ROOT = Path(__file__).resolve().parent.parent
UTC = timezone.utc
T0 = datetime(2026, 8, 28, 18, 0, tzinfo=UTC)
M = timedelta(minutes=1)


def load_script():
    spec = importlib.util.spec_from_file_location("path_windows", ROOT / "scripts" / "path_windows.py")
    mod = importlib.util.module_from_spec(spec)
    # Registered before it is run: its dataclass resolves its own annotations
    # through ``sys.modules``, and a module that is not there has none.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def pw():
    return load_script()


class SettlingHistory(FakeHistory):
    """A history that also knows what its markets paid, and counts the asks."""

    def __init__(self, t0, series, settled=None, dead=None):
        super().__init__(t0, series, dead)
        self.settled = settled or {}
        self.settlement_calls: list[str] = []

    def settlement(self, ticker: str) -> str | None:
        self.settlement_calls.append(ticker)
        if ticker not in self.settled:
            raise RuntimeError("no such market")
        return self.settled[ticker]

    def candles(self, ticker, start=None, end=None, interval=1):
        """The call that needs no market object, which is what the fallback uses."""
        return FakeHistory.price_path(self, ticker, start, end, interval)


def _raw(ticker: str, minute: int, mid_now: float, realised: float, **extra) -> dict:
    d = {"ticker": ticker, "at": (T0 + minute * M).isoformat(), "mid_now": mid_now,
         "realised": realised, "game": {"game_id": "1", "clock": f"{minute}'"},
         "event": ticker.rsplit("-", 1)[0], "yes_bid": mid_now - 0.01, "yes_ask": mid_now + 0.01,
         "yes_bid_h": realised - 0.01, "yes_ask_h": realised + 0.01}
    d.update(extra)
    return d


@pytest.fixture
def history():
    # A: a candle every minute from 0 to 40 at a rising mid, so a five-minute
    # path is five bars. C: nothing at all, so a window on it has no bars.
    series = {"E-A": {m: 0.50 + m / 1000 for m in range(41)}}
    return SettlingHistory(T0, series, settled={"E-A": "yes"})


@pytest.fixture
def windows_dir(tmp_path):
    d = tmp_path / "windows"
    d.mkdir()
    (d / "E.every5.h5.json").write_text(json.dumps([
        _raw("E-A", 10, 0.51, 0.515),       # bars at 11..15
        _raw("E-A", 20, 0.52, 0.525),       # bars at 21..25
        _raw("E-C", 10, 0.31, 0.31),        # a contract with no candles at all
    ]))
    return d


@pytest.fixture
def kalshi(pw):
    return pw.TOPICS["kalshi-horizon-5m"]


# -- the path itself -------------------------------------------------------------


def test_bars_come_from_strictly_after_the_instant_through_the_horizon(pw, history, windows_dir,
                                                                       kalshi):
    total = pw.path_dir(windows_dir, pw.KalshiPaths(history), kalshi, workers=2,
                        log=lambda m: None)
    rows = json.loads((windows_dir / "E.every5.h5.json").read_text())
    first = rows[0]
    assert [b["ts"] for b in first["path"]] == [(T0 + m * M).isoformat() for m in range(11, 16)]
    assert abs(first["path"][0]["close"] - 0.511) < 1e-9        # the minute after the instant
    assert abs(first["path"][-1]["close"] - 0.515) < 1e-9       # the horizon's own minute
    assert all(set(b) == {"ts", "high", "low", "close"} for b in first["path"])
    assert [b["ts"] for b in rows[1]["path"]] == [(T0 + m * M).isoformat() for m in range(21, 26)]
    assert total["pathed"] == 2 and total["bars"] == 10 and total["no_bars"] == 1
    # The window reloads with the path the book posts its quote against.
    w = Window.from_dict(first)
    assert len(w.path) == 5 and w.path[0].ts == T0 + 11 * M


def test_a_window_with_no_bars_keeps_null(pw, history, windows_dir, kalshi):
    pw.path_dir(windows_dir, pw.KalshiPaths(history), kalshi, workers=1, log=lambda m: None)
    empty = json.loads((windows_dir / "E.every5.h5.json").read_text())[2]
    assert empty["path"] is None
    w = Window.from_dict(empty)
    assert w.path == () and w.mid_now == 0.31          # still a question, just never filled


def test_settlement_is_read_once_per_ticker_and_a_failure_does_not_stop_the_run(
        pw, history, windows_dir, kalshi):
    pw.path_dir(windows_dir, pw.KalshiPaths(history), kalshi, workers=1, log=lambda m: None)
    rows = json.loads((windows_dir / "E.every5.h5.json").read_text())
    assert rows[0]["settlement"] == 1.0 and rows[1]["settlement"] == 1.0
    assert rows[2]["settlement"] is None               # the market that could not be read
    assert history.settlement_calls == ["E-A", "E-C"]  # once each, whatever the answer


def test_the_question_is_untouched(pw, history, windows_dir, kalshi):
    path = windows_dir / "E.every5.h5.json"
    before = json.loads(path.read_text())
    pw.path_dir(windows_dir, pw.KalshiPaths(history), kalshi, workers=1, log=lambda m: None)
    after = json.loads(path.read_text())
    assert len(after) == len(before)
    for old, new in zip(before, after):
        assert {k: v for k, v in new.items() if k not in pw.WRITABLE} == old
        a, b = Window.from_dict(old), Window.from_dict(new)
        assert (a.id, a.mid_now, a.realised, a.game, a.event) == (b.id, b.mid_now, b.realised,
                                                                  b.game, b.event)
        assert (a.yes_bid, a.yes_ask, a.yes_bid_h, a.yes_ask_h) == (b.yes_bid, b.yes_ask,
                                                                    b.yes_bid_h, b.yes_ask_h)
    # A set of bars that came back against a different question is refused.
    tampered = [dict(r) for r in before]
    tampered[0]["mid_now"] = 0.99
    with pytest.raises(AssertionError):
        pw.check_unchanged(before, tampered, Window)
    dropped = [dict(r) for r in before]
    dropped[0].pop("yes_bid")
    with pytest.raises(AssertionError):
        pw.check_unchanged(before, dropped, Window)


# -- doing it twice ---------------------------------------------------------------


def test_resumable_skips_windows_already_pathed(pw, history, windows_dir, kalshi):
    path = windows_dir / "E.every5.h5.json"
    rows = json.loads(path.read_text())
    rows[0].update({"path": [{"ts": T0.isoformat(), "high": 9.0, "low": 9.0, "close": 9.0}],
                    "settlement": 0.0})
    path.write_text(json.dumps(rows))
    calls: list[tuple] = []
    real = history.price_path

    def counted(ticker, start=None, end=None, interval=1, clip_to_close=True):
        calls.append((ticker, start, end))
        return real(ticker, start, end, interval, clip_to_close)

    history.price_path = counted
    total = pw.path_dir(windows_dir, pw.KalshiPaths(history), kalshi, workers=1, log=lambda m: None)
    after = json.loads(path.read_text())
    assert after[0]["path"][0]["close"] == 9.0 and after[0]["settlement"] == 0.0  # kept as written
    assert total["skipped"] == 1 and len(after[1]["path"]) == 5                   # the rest done
    # One fetch per ticker, spanning that ticker's unpathed windows to their horizon.
    assert [c[0] for c in calls] == ["E-A", "E-C"]
    assert calls[0][1] == T0 + 20 * M and calls[0][2] == T0 + 25 * M
    # A file whose windows all carry a path is not even fetched.
    for r in after:
        r.update({"path": [{"ts": T0.isoformat(), "high": 9.0, "low": 9.0, "close": 9.0}],
                  "settlement": 1.0})
    path.write_text(json.dumps(after))
    calls.clear()
    log: list[str] = []
    total = pw.path_dir(windows_dir, pw.KalshiPaths(history), kalshi, workers=1, log=log.append)
    assert calls == [] and total["files_skipped"] == 1 and "already pathed" in log[0]


def test_force_repaths_over_what_was_written(pw, history, windows_dir, kalshi):
    path = windows_dir / "E.every5.h5.json"
    rows = json.loads(path.read_text())
    for r in rows:
        r.update({"path": [{"ts": T0.isoformat(), "high": 9.0, "low": 9.0, "close": 9.0}],
                  "settlement": 1.0})
    path.write_text(json.dumps(rows))
    total = pw.path_dir(windows_dir, pw.KalshiPaths(history), kalshi, workers=1, force=True,
                        log=lambda m: None)
    forced = json.loads(path.read_text())
    assert len(forced[0]["path"]) == 5 and abs(forced[0]["path"][0]["close"] - 0.511) < 1e-9
    assert forced[2]["path"] is None and total["skipped"] == 0


def test_dry_run_writes_nothing(pw, history, windows_dir, kalshi):
    path = windows_dir / "E.every5.h5.json"
    before = path.read_bytes()
    log: list[str] = []
    total = pw.path_dir(windows_dir, pw.KalshiPaths(history), kalshi, workers=1, dry_run=True,
                        log=log.append)
    assert path.read_bytes() == before
    assert not list(windows_dir.glob("*.tmp"))
    assert total["pathed"] == 2 and any("dry run" in m for m in log)


def test_limit_and_the_horizon_suffix_pick_the_files(pw, history, windows_dir, kalshi):
    (windows_dir / "F.every5.h5.json").write_text(json.dumps([_raw("E-A", 30, 0.53, 0.535)]))
    (windows_dir / "G.every5.h1.json").write_text(json.dumps([_raw("E-A", 30, 0.53, 0.531)]))
    (windows_dir / "notes.json").write_text(json.dumps([]))
    assert [p.name for p in pw.window_files(windows_dir)] == [
        "E.every5.h5.json", "F.every5.h5.json", "G.every5.h1.json"]
    total = pw.path_dir(windows_dir, pw.KalshiPaths(history), kalshi, workers=1, limit=1,
                        log=lambda m: None)
    assert total["files"] == 1
    assert "path" not in json.loads((windows_dir / "F.every5.h5.json").read_text())[0]
    pw.path_dir(windows_dir, pw.KalshiPaths(history), kalshi, workers=1, log=lambda m: None)
    # The horizon is the file's own, so a one-minute file gets one bar.
    g = json.loads((windows_dir / "G.every5.h1.json").read_text())[0]
    assert len(g["path"]) == 1 and abs(g["path"][0]["close"] - 0.531) < 1e-9


def test_bars_in_is_open_at_the_instant_and_closed_at_the_horizon(pw):
    bars = [pw.PathBar(ts=T0 + m * M, high=1.0, low=1.0, close=1.0) for m in range(0, 8)]
    got = pw.bars_in(bars, T0 + 2 * M, timedelta(minutes=3))
    assert [b.ts for b in got] == [T0 + 3 * M, T0 + 4 * M, T0 + 5 * M]
    assert pw.horizon_of(Path("D20260620.every5.h1.json")) == 1
    assert pw.horizon_of(Path("AAPL-20260601.h5.json")) == 5
    assert pw.horizon_of(Path("benchmark.json")) is None


# -- the two topics that never open a socket ---------------------------------------


def _kline(at: datetime, close: float) -> Kline:
    return Kline(ts_open=at, open=close, high=close + 1.0, low=close - 1.0, close=close,
                 volume=1.0, quote_volume=close, trades=10, taker_buy_volume=0.6)


def test_crypto_reads_its_kline_store_and_opens_no_socket(pw, tmp_path, monkeypatch):
    from rsi_arena.crypto import _binance

    monkeypatch.setattr(_binance, "_get", lambda *a, **k: pytest.fail("crypto fetched over the network"))
    day = datetime(2026, 6, 20, tzinfo=UTC)
    store = KlineStore(tmp_path / "klines")
    store.write("BTCUSDT", day.date(), [_kline(day + m * M, 100.0 + m) for m in range(60)])
    d = tmp_path / "windows-crypto"
    d.mkdir()
    (d / "D20260620.every5.h1.json").write_text(json.dumps([
        {"symbol": "BTCUSDT", "at": (day + 5 * M).isoformat(), "mid_now": 104.0,
         "realised": 105.0, "context": {"hour_utc": 0}, "group": "D20260620"},
        {"symbol": "ETHUSDT", "at": (day + 5 * M).isoformat(), "mid_now": 1.0,
         "realised": 1.0, "context": {}, "group": "D20260620"},
    ]))
    source = pw.CryptoPaths(BinanceSpot(store, fetch_missing=False))
    total = pw.path_dir(d, source, pw.TOPICS["crypto-horizon-1m"], workers=1, log=lambda m: None)
    rows = json.loads((d / "D20260620.every5.h1.json").read_text())
    # One minute ahead is one bar, the one that closes at the horizon.
    assert len(rows[0]["path"]) == 1
    assert rows[0]["path"][0]["ts"] == (day + 6 * M).isoformat()
    assert rows[0]["path"][0]["close"] == 105.0 and rows[0]["path"][0]["high"] == 106.0
    assert rows[1]["path"] is None                     # a symbol the store does not hold
    assert "settlement" not in rows[0]                 # a perpetual never settles
    assert total["pathed"] == 1 and total["no_bars"] == 1
    assert len(CryptoWindow.from_dict(rows[0]).path) == 1


def test_news_reads_its_bar_store_and_opens_no_socket(pw, tmp_path):
    day = datetime(2026, 6, 1, 14, 0, tzinfo=UTC)
    store = BarStore(tmp_path / "bars")
    store.save("AAPL", day.date(), [Bar(symbol="AAPL", ts_open=day + m * M, o=200.0 + m,
                                        h=200.5 + m, l=199.5 + m, c=200.0 + m, v=1e4, n=20,
                                        vwap=200.0 + m) for m in range(60)])
    d = tmp_path / "windows-news"
    d.mkdir()
    at = day + timedelta(minutes=42, seconds=6)
    (d / "AAPL-20260601.h5.json").write_text(json.dumps([
        {"symbol": "AAPL", "at": at.isoformat(), "mid_now": 241.0, "realised": 246.0,
         "news_id": "1", "headline": "a thing happened", "summary": "", "source": "bz",
         "symbols": ["AAPL"], "edited_after": False, "group": "AAPL-20260601"},
        {"symbol": "MSFT", "at": at.isoformat(), "mid_now": 1.0, "realised": 1.0,
         "news_id": "2", "headline": "another", "summary": "", "source": "bz",
         "symbols": ["MSFT"], "edited_after": False, "group": "AAPL-20260601"},
    ]))
    total = pw.path_dir(d, pw.NewsPaths(store), pw.TOPICS["news-equity-5m"], workers=1,
                        log=lambda m: None)
    rows = json.loads((d / "AAPL-20260601.h5.json").read_text())
    # A story at 14:42:06 is quoted against the bars closing 14:43 through 14:47.
    assert [b["ts"] for b in rows[0]["path"]] == [
        (day + m * M).isoformat() for m in range(43, 48)]
    assert rows[0]["path"][0]["high"] == 242.5 and rows[0]["path"][0]["low"] == 241.5
    assert rows[1]["path"] is None                     # a name the store does not hold
    assert total["pathed"] == 1 and total["bars"] == 5
    assert len(NewsWindow.from_dict(rows[0]).path) == 5
    assert not list((tmp_path / "bars").glob("MSFT/*"))   # nothing was fetched or written


def test_report_reads_what_is_on_disk(pw, history, windows_dir, kalshi):
    pw.path_dir(windows_dir, pw.KalshiPaths(history), kalshi, workers=1, log=lambda m: None)
    log: list[str] = []
    out = pw.report(windows_dir, unit="cents", tick=1.0, log=log.append)
    assert out["windows"] == 3 and out["pathed"] == 2 and out["bars"] == 10
    assert out["settled"] == 2
    # The five minute-bars of the first window span 0.511 to 0.515: 0.4 cents.
    assert abs(out["range_p50"] - 0.4) < 1e-6 and out["wider_than_tick_quote"] == 0.0
    assert any("path range" in m for m in log)


def test_gzip_store_files_are_what_the_sources_read(tmp_path):
    """The two offline sources read the same gzip files discovery writes."""
    store = KlineStore(tmp_path / "klines")
    day = datetime(2026, 6, 20, tzinfo=UTC)
    store.write("BTCUSDT", day.date(), [_kline(day, 100.0)])
    with gzip.open(tmp_path / "klines" / "BTCUSDT" / "2026-06-20.json.gz", "rt") as fh:
        assert len(json.load(fh)) == 1


def test_a_market_that_is_gone_falls_back_to_its_candles(pw, history, windows_dir, kalshi):
    """Some of last July's markets 404 on the market object while their
    candles are still served; the path is read from those rather than lost."""
    def gone(ticker, start=None, end=None, interval=1, clip_to_close=True):
        raise RuntimeError("HTTP Error 404: Not Found")

    history.price_path = gone
    pw.path_dir(windows_dir, pw.KalshiPaths(history), kalshi, workers=1, log=lambda m: None)
    rows = json.loads((windows_dir / "E.every5.h5.json").read_text())
    assert len(rows[0]["path"]) == 5 and rows[2]["path"] is None


def test_a_market_the_venue_no_longer_serves_leaves_null_without_failing_the_file(
        pw, history, windows_dir, kalshi, monkeypatch):
    """A contract that 404s everywhere costs its own windows their path and
    nothing else: the file's other contracts are still written."""
    def gone(ticker, start=None, end=None, interval=1, clip_to_close=True):
        raise RuntimeError("HTTP Error 404: Not Found")

    monkeypatch.setattr(pw, "RETRY_PAUSE_S", 0.0)
    history.price_path = gone
    history.candles = gone
    log: list[str] = []
    total = pw.path_dir(windows_dir, pw.KalshiPaths(history), kalshi, workers=1, log=log.append)
    rows = json.loads((windows_dir / "E.every5.h5.json").read_text())
    assert all(r["path"] is None for r in rows)
    assert total.get("files_failed", 0) == 0 and total["unreachable"] == 3
    assert rows[0]["settlement"] == 1.0            # the settlement still went on
    assert any("no bars" in m for m in log)
