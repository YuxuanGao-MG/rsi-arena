"""``scripts/quote_windows.py``: the touch goes onto a built question set
from the candles, and nothing else about the set moves."""

from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from rsi_arena.topics.kalshi_horizon import Window
from tests.conftest import FakeHistory

ROOT = Path(__file__).resolve().parent.parent
UTC = timezone.utc
T0 = datetime(2026, 8, 28, 18, 0, tzinfo=UTC)
M = timedelta(minutes=1)


def load_script():
    spec = importlib.util.spec_from_file_location("quote_windows", ROOT / "scripts" / "quote_windows.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def qw():
    return load_script()


def _raw(ticker: str, minute: int, mid_now: float, realised: float, **extra) -> dict:
    d = {"ticker": ticker, "at": (T0 + minute * M).isoformat(), "mid_now": mid_now,
         "realised": realised, "game": {"game_id": "1", "clock": f"{minute}'"},
         "event": ticker.rsplit("-", 1)[0]}
    d.update(extra)
    return d


@pytest.fixture
def history():
    # A: a candle every minute from 0 to 40 at a rising mid; the one at 25 is
    # a dead book. B: candles at 0 and 4 only, so an instant at 10 is stale.
    series = {"E-A": {m: 0.50 + m / 1000 for m in range(41)},
              "E-B": {0: 0.30, 4: 0.31, 40: 0.35, 45: 0.36}}
    return FakeHistory(T0, series, dead={("E-A", 25)})


@pytest.fixture
def windows_dir(tmp_path):
    d = tmp_path / "windows"
    d.mkdir()
    (d / "E.every5.h5.json").write_text(json.dumps([
        _raw("E-A", 10, 0.51, 0.515),          # entry candle at 10, horizon candle at 15
        _raw("E-A", 20, 0.52, 0.525),          # horizon at 25 is the dead candle
        _raw("E-A", 25, 0.525, 0.53),          # entry at 25 is the dead candle
        _raw("E-B", 10, 0.31, 0.31),           # last candle is 6 minutes old: stale
        _raw("E-B", 40, 0.35, 0.36),           # both fresh
    ]))
    return d


def test_touch_at_is_the_last_candle_at_or_before_the_instant(qw, history):
    candles = history.price_path("E-A", T0, T0 + 40 * M)
    bid, ask = qw.touch_at(candles, T0 + 10 * M + timedelta(seconds=30))
    assert abs(bid - 0.50) < 1e-9 and abs(ask - 0.52) < 1e-9      # the candle at 10, spread 2c
    assert qw.touch_at(candles, T0 - M) is None                    # nothing yet
    assert qw.touch_at(candles, T0 + 25 * M) is None               # a dead book is not a touch
    assert qw.touch_at(history.price_path("E-B", T0, T0 + 50 * M), T0 + 10 * M) is None  # stale


def test_fields_come_from_the_candles_at_the_instant_and_the_horizon(qw, history, windows_dir):
    log: list[str] = []
    total = qw.quote_dir(windows_dir, history, workers=2, log=log.append)
    rows = json.loads((windows_dir / "E.every5.h5.json").read_text())
    first = rows[0]
    assert abs(first["yes_bid"] - 0.50) < 1e-9 and abs(first["yes_ask"] - 0.52) < 1e-9
    assert abs(first["yes_bid_h"] - 0.505) < 1e-9 and abs(first["yes_ask_h"] - 0.525) < 1e-9
    last = rows[4]
    assert abs(last["yes_bid"] - 0.34) < 1e-9 and abs(last["yes_ask_h"] - 0.37) < 1e-9
    assert total["entry"] == 3 and total["horizon"] == 3 and total["both"] == 2
    assert any("E.every5.h5.json: 2/5 windows quoted, 3 left without" in m for m in log)


def test_stale_and_one_sided_candles_leave_none(qw, history, windows_dir):
    qw.quote_dir(windows_dir, history, workers=1, log=lambda m: None)
    rows = json.loads((windows_dir / "E.every5.h5.json").read_text())
    dead_horizon, dead_entry, stale = rows[1], rows[2], rows[3]
    assert dead_horizon["yes_bid"] is not None and dead_horizon["yes_bid_h"] is None
    assert dead_horizon["yes_ask_h"] is None
    assert dead_entry["yes_bid"] is None and dead_entry["yes_ask"] is None
    assert dead_entry["yes_bid_h"] is not None                     # the horizon candle at 30 is live
    assert all(stale[f] is None for f in qw.FIELDS)
    # A window left without a touch still loads, and the book falls back to the proxy for it.
    w = Window.from_dict(stale)
    assert w.yes_bid is None and w.mid_now == 0.31


def test_the_question_is_untouched(qw, history, windows_dir):
    path = windows_dir / "E.every5.h5.json"
    before = json.loads(path.read_text())
    qw.quote_dir(windows_dir, history, workers=1, log=lambda m: None)
    after = json.loads(path.read_text())
    assert len(after) == len(before)
    for old, new in zip(before, after):
        assert {k: v for k, v in new.items() if k not in qw.FIELDS} == old
        a, b = Window.from_dict(old), Window.from_dict(new)
        assert (a.id, a.mid_now, a.realised, a.game, a.event) == (b.id, b.mid_now, b.realised, b.game, b.event)
    # A candle history that disagrees with the question is refused, not written over it.
    tampered = [dict(r) for r in before]
    tampered[0]["mid_now"] = 0.99
    with pytest.raises(AssertionError):
        qw.check_unchanged(before, tampered)


def test_resumable_skips_windows_already_quoted(qw, history, windows_dir):
    path = windows_dir / "E.every5.h5.json"
    rows = json.loads(path.read_text())
    rows[0].update({"yes_bid": 0.11, "yes_ask": 0.12, "yes_bid_h": 0.13, "yes_ask_h": 0.14})
    path.write_text(json.dumps(rows))
    calls: list[tuple] = []
    real = history.price_path

    def counted(ticker, start=None, end=None, interval=1, clip_to_close=True):
        calls.append((ticker, start, end))
        return real(ticker, start, end, interval, clip_to_close)

    history.price_path = counted
    total = qw.quote_dir(windows_dir, history, workers=1, log=lambda m: None)
    after = json.loads(path.read_text())
    assert after[0]["yes_bid"] == 0.11 and after[0]["yes_ask_h"] == 0.14      # kept as written
    assert total["skipped"] == 1 and abs(after[4]["yes_bid"] - 0.34) < 1e-9   # the rest quoted
    # One fetch per ticker, spanning that ticker's windows with the pad either side.
    assert [c[0] for c in calls] == ["E-A", "E-B"]
    a_start, a_end = calls[0][1], calls[0][2]
    assert a_start == T0 + 20 * M - qw.PAD and a_end == T0 + 25 * M + 5 * M + qw.PAD
    # A file whose windows all carry the touch is not even fetched.
    for r in after:
        r.update({"yes_bid": 0.11, "yes_ask": 0.12, "yes_bid_h": 0.13, "yes_ask_h": 0.14})
    path.write_text(json.dumps(after))
    calls.clear()
    log: list[str] = []
    total = qw.quote_dir(windows_dir, history, workers=1, log=log.append)
    assert calls == [] and total["files_skipped"] == 1 and "already quoted" in log[0]
    # --force requotes over what was written.
    qw.quote_dir(windows_dir, history, workers=1, force=True, log=lambda m: None)
    forced = json.loads(path.read_text())
    assert abs(forced[0]["yes_bid"] - 0.50) < 1e-9 and forced[3]["yes_bid"] is None


def test_dry_run_writes_nothing(qw, history, windows_dir):
    path = windows_dir / "E.every5.h5.json"
    before = path.read_bytes()
    log: list[str] = []
    total = qw.quote_dir(windows_dir, history, workers=1, dry_run=True, log=log.append)
    assert path.read_bytes() == before
    assert not list(windows_dir.glob("*.tmp"))
    assert total["entry"] == 3 and any("dry run" in m for m in log)


def test_limit_and_horizon_pick_the_files(qw, history, windows_dir):
    (windows_dir / "F.every5.h5.json").write_text(json.dumps([_raw("E-A", 30, 0.53, 0.535)]))
    (windows_dir / "G.every5.h1.json").write_text(json.dumps([_raw("E-A", 30, 0.53, 0.531)]))
    total = qw.quote_dir(windows_dir, history, workers=1, limit=1, log=lambda m: None)
    assert total["files"] == 1
    assert "yes_bid" not in json.loads((windows_dir / "F.every5.h5.json").read_text())[0]
    total = qw.quote_dir(windows_dir, history, horizon=1, workers=1, log=lambda m: None)
    g = json.loads((windows_dir / "G.every5.h1.json").read_text())[0]
    assert total["files"] == 1 and abs(g["yes_bid_h"] - 0.521) < 1e-9     # the candle at 31


def test_report_reads_the_spreads_on_disk(qw, history, windows_dir):
    qw.quote_dir(windows_dir, history, workers=1, log=lambda m: None)
    log: list[str] = []
    out = qw.report(windows_dir, log=log.append)
    assert out["windows"] == 5 and out["entry"] == 3 and out["both"] == 2
    assert abs(out["median"] - 2.0) < 1e-9 and out["wider_than_proxy"] == 0.0
    assert set(out["by_prefix"]) == {"E"} and any("median" in m for m in log)
