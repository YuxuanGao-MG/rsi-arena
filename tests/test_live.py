"""Live collection: the toolbox must not remember, and the rows must survive.

Offline like the rest. The collector's own network — Kalshi's event list, the
fixture feed — is not exercised here; what is exercised is the part that was
safe by accident until now.
"""

from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from rsi_arena.harness import Harness
from rsi_arena.kalshi.replay import NO_CACHE, ToolCache, live_tools, replay_tools
from tests.conftest import FakeHistory, FakeLLM

UTC = timezone.utc
ROOT = Path(__file__).resolve().parent.parent


def load_script(name: str):
    """A script under ``scripts/`` as a module. They are entry points, not a package."""
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def no_cache_allowed(monkeypatch):
    """Make any read or write of the disk tool cache an error.

    Patching the base class and not the refusal is the whole point: ``NO_CACHE``
    overrides both methods, so a live box survives this and a box holding a
    plain ``ToolCache`` — including the ``ToolCache(None)`` that ``replay_tools``
    falls back to — does not.
    """
    def boom(*args, **kwargs):
        raise AssertionError("a live market must not go through the tool cache")

    monkeypatch.setattr(ToolCache, "get", boom)
    monkeypatch.setattr(ToolCache, "put", boom)


def test_live_tools_read_and_write_no_cache(history, t0, no_cache_allowed):
    at = t0 + timedelta(minutes=10)
    box = live_tools(at, history)
    assert box["previous_trades"].safe_call(ticker="A").ok
    assert box["market_quote"].safe_call(ticker="A").ok
    # Not memoised either: the second call goes to history again, which is the
    # behaviour a book that moves between two sweeps needs.
    live_tools(at, history)["previous_trades"].safe_call(ticker="A")
    assert len(history.trade_calls) == 2


def test_a_plain_cache_would_have_been_used(history, t0, tmp_path, no_cache_allowed):
    """The guard above is not vacuous: the benchmark's box does touch the cache.

    ``safe_call`` turns the guard's exception into a failed result rather than
    letting it out, which is also how a cache read would surface in the live
    collector: as a tool failure in the trace, not a traceback.
    """
    out = replay_tools(t0, history, ToolCache(tmp_path))["market_quote"].safe_call(ticker="A")
    assert not out.ok and "must not go through the tool cache" in out.error


def test_no_cache_is_not_a_directory():
    assert NO_CACHE.root is None
    assert NO_CACHE.get({"tool": "market_quote", "at": "now"}) is None
    assert NO_CACHE.put({"tool": "market_quote", "at": "now"}, {"ok": True}) is None


def harness() -> Harness:
    return Harness.from_dict({
        "name": "live-test", "context": "forecast", "tools": ["market_quote"],
        "config": {"max_usd": 1.0},
        "plan": {"steps": [
            {"type": "tool", "name": "q", "tool": "market_quote",
             "args": {"ticker": "{{question}}"}, "output_key": "q"},
            {"type": "prompt", "name": "p", "prompt": "mid {{q.mid}} state {{game}}",
             "output_schema": {"type": "object"}, "output_key": "out"},
        ]}})


async def test_the_collector_forecasts_without_touching_the_cache(monkeypatch, no_cache_allowed):
    live = load_script("collect_live")
    # The fixture feed is the collector's only other network call inside `one`,
    # and what it returns does not change what is being asserted here.
    monkeypatch.setattr(live, "match_timeline", lambda league, game: None)

    now = datetime.now(UTC)
    hist = FakeHistory(now - timedelta(minutes=2), {"T": {0: 0.40, 1: 0.41, 2: 0.42}})
    llm = FakeLLM(lambda messages, schema, tools: {"delta_cents": 2.0, "half_width_cents": 3.0})

    row = await live.one(harness(), llm, "EPL", "401879275", "T", hist)

    assert row["ok"], row.get("error")
    assert row["output"]["delta_cents"] == 2.0
    assert abs(row["mid_now"] - 0.42) < 1e-9
    # A tool really ran, so the cache really was offered the chance to answer.
    assert any(s["kind"] == "tool" for s in row["run"]["trace"])


async def test_a_forecast_is_pending_until_the_horizon_prints(monkeypatch, tmp_path):
    """A row is written once, when its five minutes are up — never before."""
    live = load_script("collect_live")
    now = datetime.now(UTC)
    hist = FakeHistory(now - timedelta(minutes=2), {"T": {m: 0.40 + 0.01 * m for m in range(9)}})
    out = tmp_path / "forecasts.jsonl"

    fresh = {"at": now.isoformat(), "ticker": "T", "mid_now": 0.42,
             "output": {"delta_cents": 2.0}}
    assert live.write_resolved([fresh], hist, out) == [fresh]
    assert not out.read_text()

    old = dict(fresh, at=(now - timedelta(minutes=7)).isoformat())
    assert live.write_resolved([old], hist, out) == []
    written = json.loads(out.read_text())
    assert written["scored"]["skill"] is not None and written["realised"] is not None


def test_publisher_maps_a_row_onto_the_table():
    # psycopg2 is not a dev dependency — the publisher is the only thing that
    # needs it, and CI installs `.[dev]`.
    pytest.importorskip("psycopg2")
    pub = load_script("publish_live")

    at = "2026-09-16T06:36:59.423927+00:00"
    row = {"at": at, "league": "EPL", "game_id": "401879275", "ticker": "KXEPL-BRE",
           "mid_now": 0.325, "realised": 0.33, "harness": "horizon-5m",
           "output": {"delta_cents": 1.0}, "game": {"home_score": 0},
           "run": {"trace": [{"name": "q", "kind": "tool", "output": "x" * 4000}]},
           "ok": True, "error": None, "scored": {"skill": 0.25}}
    (at_, league, game_id, ticker, mid, realised, name,
     output, game, spans, skill, scored, ok, error) = pub.live_row(row)
    assert (at_, league, game_id, ticker) == (at, "EPL", "401879275", "KXEPL-BRE")
    assert (mid, realised, skill, scored, ok, error) == (0.325, 0.33, 0.25, True, True, None)
    assert output.adapted == {"delta_cents": 1.0} and game.adapted == {"home_score": 0}
    assert len(spans.adapted[0]["output"]) < pub.TRIM + 40

    unscored = dict(row, scored=None, unscored_because="no quote printed at the horizon")
    assert pub.live_row(unscored)[10] is None
    assert pub.live_row(unscored)[11] is False
    assert pub.live_row(unscored)[13] == "no quote printed at the horizon"


def test_publisher_resumes_and_never_reads_half_a_line(tmp_path):
    pytest.importorskip("psycopg2")
    pub = load_script("publish_live")
    path = tmp_path / "forecasts.jsonl"
    sidecar = path.with_suffix(path.suffix + ".published")

    path.write_text(json.dumps({"n": 1}) + "\n" + json.dumps({"n": 2}) + "\n")
    rows, end = pub.read_rows(path, 0)
    assert [r["n"] for r in rows] == [1, 2] and end == path.stat().st_size

    # The collector is still appending, so the tail may be a fragment.
    with path.open("a") as fh:
        fh.write('{"n": 3, "par')
    more, end2 = pub.read_rows(path, end)
    assert more == [] and end2 == end

    sidecar.write_text(json.dumps({"bytes": end}))
    assert pub.offset_of(sidecar, path) == end
    # A file shorter than the mark was rotated, and resuming would skip the lot.
    path.write_text(json.dumps({"n": 9}) + "\n")
    assert pub.offset_of(sidecar, path) == 0


def test_a_refused_day_is_retried_but_an_empty_one_is_not(monkeypatch):
    """The four Eredivisie events of 2026-09-16, in the form that lost them.

    One blank answer from the fixture feed was memoised for the whole sweep and
    every event kicking off that day reported as unlinkable. A refusal has to be
    worth asking about again; a day with no football does not.
    """
    live = load_script("collect_live")
    calls: list[str] = []

    def flaky(league: str, day: str) -> list[dict]:
        calls.append(day)
        if len(calls) == 1:
            raise RuntimeError("503 from the fixture feed")
        return [{"id": "1", "home": "Ajax Amsterdam", "away": "Excelsior"}]

    monkeypatch.setattr(live, "todays_games", flaky)
    assert live.fixtures_on("EREDIVISIE", "2026-09-19") == []
    assert live.fixtures_on("EREDIVISIE", "2026-09-19")          # asked again, answered
    assert live.fixtures_on("EREDIVISIE", "2026-09-19")          # and now remembered
    assert calls == ["2026-09-19", "2026-09-19"]

    monkeypatch.setattr(live, "todays_games", lambda league, day: [])
    assert live.fixtures_on("EREDIVISIE", "2026-06-30") == []
    monkeypatch.setattr(live, "todays_games", lambda league, day: 1 / 0)
    assert live.fixtures_on("EREDIVISIE", "2026-06-30") == []    # an empty day is final


def test_a_feed_that_stays_down_is_given_up_on(monkeypatch):
    live = load_script("collect_live")
    calls: list[str] = []

    def down(league: str, day: str) -> list[dict]:
        calls.append(day)
        raise RuntimeError("timed out")

    monkeypatch.setattr(live, "todays_games", down)
    for _ in range(6):
        assert live.fixtures_on("EPL", "2026-09-19") == []
    assert len(calls) == live.MAX_DAY_ATTEMPTS


def test_an_unanswered_live_quote_is_recorded_not_raised():
    """The first in-play window hit the per-window ledger at $1.65 of $0.20,
    produced no forecast, and resolve() read .skill off None - crashing the
    sweep and losing every still-pending grading with it."""
    import importlib.util, sys
    from datetime import datetime, timedelta, timezone

    spec = importlib.util.spec_from_file_location("cl", "scripts/collect_live.py")
    cl = importlib.util.module_from_spec(spec)
    sys.modules["cl"] = spec.loader.exec_module(spec.loader.load_module.__self__) if False else None
    spec.loader.exec_module(cl)

    class Hist:
        def __init__(self):
            pass

    row = {"at": (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(),
           "ticker": "T", "mid_now": 0.4, "output": None}
    cl.realised_mid = lambda *a, **k: 0.43
    done = cl.resolve(row, Hist())
    assert done is True
    assert row["scored"] is None
    assert "no forecast" in row["unscored_because"]
    assert row["realised"] == 0.43
