"""Live news collection: a story is forecast once, graded when the bar prints,
and the toolbox must not remember.

Offline like the rest. The collector's own network - the news feed, the bars
- is the fakes in ``conftest``; what is exercised is the sweep, the grading
and the row the publisher reads.
"""

from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from rsi_arena.alpaca import NewsItem
from rsi_arena.harness import Harness
from rsi_arena.harness.toolcache import ToolCache
from tests.conftest import FakeBars, FakeLLM, FakeNews

UTC = timezone.utc
ROOT = Path(__file__).resolve().parent.parent
JEV = ROOT / "harnesses" / "news-equity-5m-jev.json"

#: Monday 2026-06-01, 10:20 New York: inside the window, a settled open behind it.
NOW = datetime(2026, 6, 1, 14, 20, tzinfo=UTC)


def load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def no_cache_allowed(monkeypatch):
    """Any read or write of the disk tool cache is an error. ``NO_CACHE``
    overrides both methods, so a live box survives this and a plain
    ``ToolCache`` does not."""
    def boom(*args, **kwargs):
        raise AssertionError("a live market must not go through the tool cache")

    monkeypatch.setattr(ToolCache, "get", boom)
    monkeypatch.setattr(ToolCache, "put", boom)


def item(id_: str, when: datetime, symbols=("ACME",), headline: str = "ACME raises guidance") -> NewsItem:
    return NewsItem(id=id_, created_at=when, updated_at=when, headline=headline, summary="up",
                    source="benzinga", symbols=symbols)


@pytest.fixture
def tape() -> FakeBars:
    """ACME and SPY drift a few basis points a minute around NOW."""
    return FakeBars(NOW, {"ACME": {m: 100.0 * (1 + 2.0 * m / 1e4) for m in range(-40, 40)},
                          "SPY": {m: 500.0 * (1 + 1.0 * m / 1e4) for m in range(-40, 40)}})


async def test_the_sweep_forecasts_a_new_item_once_and_ignores_a_seen_one(tape, no_cache_allowed):
    live = load_script("collect_live_news")
    feed = FakeNews([item("new", NOW - timedelta(seconds=30)),
                     item("old", NOW - timedelta(minutes=3)),
                     item("elsewhere", NOW - timedelta(seconds=20), symbols=("ZZZ",))])
    seen = {("ACME", "old")}
    since = NOW - timedelta(minutes=5)
    llm = FakeLLM()

    rows = await live.sweep(Harness.load(JEV), llm, tape, feed, ["ACME", "SPY"], since, NOW, seen,
                            log=lambda m: None)
    assert [r["news_id"] for r in rows] == ["new"], "one forecast: the new item, on the name in the universe"
    (row,) = rows
    assert row["ok"], row["error"]
    assert row["symbol"] == "ACME" and row["ticker"] == f"ACME@{NOW.isoformat()}#new"
    assert row["topic"] == "news-equity-5m" and row["venue"] == "alpaca-iex" and row["unit"] == "bps"
    assert row["context"]["headline"] == "ACME raises guidance" and row["league"] is None
    assert abs(row["mid_now"] - 100.0 * (1 + 2.0 * -1 / 1e4)) < 1e-9, "the 10:19 bar, closed on the instant"
    # A tool really ran, so the cache really was offered the chance to answer.
    assert any(s["kind"] == "tool" for s in row["run"]["trace"])
    assert seen == {("ACME", "old"), ("ACME", "new")}
    # The feed was asked in batches of at most forty symbols, bounded at now.
    assert all(c["end"] == NOW and len(c["symbols"]) <= live.BATCH for c in feed.calls)

    again = await live.sweep(Harness.load(JEV), llm, tape, feed, ["ACME", "SPY"], since, NOW, seen,
                             log=lambda m: None)
    assert again == [], "nothing is forecast twice"


async def test_an_item_outside_the_window_is_skipped_with_a_sentence(tape):
    live = load_script("collect_live_news")
    early = datetime(2026, 6, 1, 13, 33, tzinfo=UTC)                  # 09:33 ET: the open is not settled
    feed = FakeNews([item("dawn", early - timedelta(seconds=10))])
    said = []
    rows = await live.sweep(Harness.load(JEV), FakeLLM(), tape, feed, ["ACME"],
                            early - timedelta(minutes=5), early, set(), log=said.append)
    assert rows == [] and any("outside the window" in m for m in said)


async def test_a_burst_is_capped_not_chased(tape):
    live = load_script("collect_live_news")
    feed = FakeNews([item(f"n{i}", NOW - timedelta(seconds=10 + i), headline=f"story {i}") for i in range(5)])
    llm = FakeLLM()
    rows = await live.sweep(Harness.load(JEV), llm, tape, feed, ["ACME"], NOW - timedelta(minutes=5),
                            NOW, set(), max_forecasts=2, log=lambda m: None)
    assert len(rows) == 2 and len(llm.decided) == 2


def test_a_forecast_is_pending_until_the_horizon_prints(tape, tmp_path):
    """A row is written once, when its five minutes are up - never before."""
    live = load_script("collect_live_news")
    out = tmp_path / "news-forecasts.jsonl"
    row = {"at": NOW.isoformat(), "ticker": f"ACME@{NOW.isoformat()}#new", "symbol": "ACME",
           "mid_now": 100.0 * (1 + 2.0 * -1 / 1e4), "output": {"delta_bps": 10.0, "half_width_bps": 5.0}}

    assert live.write_resolved([row], tape, out, now=NOW + timedelta(minutes=2)) == [row]
    assert not out.read_text()

    assert live.write_resolved([row], tape, out, now=NOW + timedelta(minutes=7)) == []
    written = json.loads(out.read_text())
    # Five minutes on, the 10:24 bar is the last closed one: ten basis points
    # above the 10:19 bar, which is what was said.
    assert written["realised"] == pytest.approx(100.0 * (1 + 2.0 * 4 / 1e4))
    assert written["scored"]["skill"] == pytest.approx(1.0, abs=0.01)
    assert written["scored"]["naive_error"] == pytest.approx(10.0, abs=0.05)

    quiet = dict(row, output=None, ticker=f"ACME@{NOW.isoformat()}#none")
    assert live.resolve(quiet, tape, now=NOW + timedelta(minutes=7)) is True
    assert quiet["scored"] is None and "no forecast" in quiet["unscored_because"]


def test_the_row_is_what_the_publisher_expects():
    pytest.importorskip("psycopg2")
    live = load_script("collect_live_news")
    pub = load_script("publish_live")
    row = {"at": NOW.isoformat(), "topic": live.TOPIC, "symbol": "ACME", "venue": live.VENUE,
           "unit": live.UNIT, "ticker": live.instance_id("ACME", NOW, "new"), "news_id": "new",
           "league": None, "game_id": None, "game": None, "mid_now": 100.0, "realised": 100.1,
           "context": {"headline": "h"}, "harness": "news-equity-5m-jev",
           "output": {"delta_bps": 10.0}, "run": {"trace": []}, "ok": True, "error": None,
           "scored": {"skill": 1.0}}
    values = pub.live_row(row, pub.TOPIC_COLUMNS)
    (at, league, game_id, ticker, mid, realised, harness, output, game, spans, skill, scored, ok,
     error, topic, symbol, venue, context, unit) = values
    assert (ticker, league, game_id) == (row["ticker"], None, None) and ticker
    assert (topic, symbol, venue, unit) == ("news-equity-5m", "ACME", "alpaca-iex", "bps")
    assert context.adapted == {"headline": "h"} and skill == 1.0 and scored is True
    assert live.symbol_of(ticker) == "ACME"


async def test_no_keys_is_a_sentence_not_a_traceback(monkeypatch, capsys):
    monkeypatch.delenv("APCA_API_KEY_ID", raising=False)
    monkeypatch.delenv("APCA_API_SECRET_KEY", raising=False)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    live = load_script("collect_live_news")
    assert await live.main(["--minutes", "0"]) == 0
    assert "no Alpaca keys" in capsys.readouterr().out


def test_the_universe_reads_like_discovery_does(tmp_path):
    live = load_script("collect_live_news")
    (tmp_path / "u.txt").write_text("# names\naapl\nSPY  # the index\n\nAAPL\n")
    assert live.read_universe(tmp_path / "u.txt") == ["AAPL", "SPY"]
