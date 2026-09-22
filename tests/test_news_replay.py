"""The news-equity topic: a stock at the second a story broke, tools frozen
there, scored in basis points. Everything against fakes; no key, no network."""

from __future__ import annotations

import gzip
import json
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from rsi_arena.alpaca import (AlpacaBars, AlpacaData, BarStore, MissingCredentials, NewsItem, in_window,
                              known_bars, last_complete_bar, replay_tools, session)
from rsi_arena.alpaca.replay import TICK_BPS
from rsi_arena.harness import Harness, ToolCache, answers_to_output
from rsi_arena.loop import Settings, evaluate
from rsi_arena.topics import spec_of
from rsi_arena.topics.news_equity import (METRIC, BenchmarkItem, NewsEquity, NewsWindow, build_windows,
                                          load_benchmark, score_output, silent)

from .conftest import FakeBars, FakeLLM, FakeNews

UTC = timezone.utc
ROOT = Path(__file__).resolve().parents[1]
OPUS, JEV = ROOT / "harnesses" / "news-equity-5m.json", ROOT / "harnesses" / "news-equity-5m-jev.json"

#: Monday 2026-06-01, 10:00 New York (EDT), so minute 0 is 10:00 ET and the
#: prior session is Friday the 29th.
T0 = datetime(2026, 6, 1, 14, 0, tzinfo=UTC)
FRIDAY = -3 * 1440
TUESDAY = 1440


def _series(minutes, price=100.0, bps_per_minute=2.0, volume=100.0):
    return {m: (price * (1 + bps_per_minute * m / 1e4), volume) for m in minutes}


@pytest.fixture
def tape() -> FakeBars:
    """ACME drifts two basis points a minute today from 09:20 to 10:39 ET;
    Friday's session is a full day at the same drift on half the volume, and
    Tuesday's, which has not happened, is wild."""
    acme = {}
    acme.update(_series(range(-40, 40), volume=300.0))
    acme.update({FRIDAY + m: (100.0 * (1 + 2.0 * m / 1e4), 100.0) for m in range(-30, 360)})
    acme.update({TUESDAY + m: (100.0 * (1 + 40.0 * m / 1e4), 5000.0) for m in range(-30, 360)})
    spy = {m: 500.0 * (1 + 1.0 * m / 1e4) for m in range(-40, 40)}
    daily = {"ACME": {date(2026, 5, 29) - timedelta(days=i): 99.0 - i for i in range(25)}}
    daily["ACME"][date(2026, 6, 1)] = 123.0          # today's daily bar: must not be read
    return FakeBars(T0, {"ACME": acme, "SPY": spy}, daily=daily)


@pytest.fixture
def story() -> NewsItem:
    return NewsItem(id="story", created_at=T0 + timedelta(minutes=20), updated_at=T0 + timedelta(minutes=20),
                    headline="ACME raises guidance", summary="up", source="benzinga",
                    symbols=("ACME", "SPY", "ZZZ"))


# -- the point-in-time rule ------------------------------------------------------

def test_a_bar_is_known_once_it_has_closed(tape):
    at = T0 + timedelta(minutes=20, seconds=30)
    bars = tape.bars("ACME", T0, at)
    assert bars[-1].ts_open == T0 + timedelta(minutes=20), "the fake serves the open bar"
    assert known_bars(bars, at)[-1].ts_open == T0 + timedelta(minutes=19), "the rule does not"
    assert last_complete_bar(bars, at).ts_open == T0 + timedelta(minutes=19)
    assert tape.price_at("ACME", at) == pytest.approx(100 * (1 + 19 * 2 / 1e4))
    quote = replay_tools(at, tape)["market_quote"].safe_call(symbol="ACME")
    assert quote.ok and quote.data["bar_open"] == (T0 + timedelta(minutes=19)).isoformat()
    assert quote.data["stale_s"] == 30
    # Exactly on the minute the bar that just closed is known and the next is not.
    assert tape.price_at("ACME", T0 + timedelta(minutes=20)) == pytest.approx(100 * (1 + 19 * 2 / 1e4))
    # A name quiet for over three minutes has no price.
    assert tape.price_at("ACME", T0 + timedelta(minutes=43)) is None
    assert tape.price_at("ACME", T0 + timedelta(minutes=42)) == pytest.approx(100 * (1 + 39 * 2 / 1e4))


def test_asking_for_a_time_past_the_instant_gets_the_instant(tape):
    at = T0 + timedelta(minutes=20)
    box = replay_tools(at, tape)
    ahead = box["market_at_time"].safe_call(symbol="ACME", when=(at + timedelta(hours=3)).isoformat())
    assert ahead.ok and ahead.data["clamped"] is True
    assert ahead.data["answered_at"] == at.isoformat()
    assert ahead.data["bar_open"] == (T0 + timedelta(minutes=19)).isoformat()
    behind = box["market_at_time"].safe_call(symbol="ACME", when=(at - timedelta(minutes=10)).isoformat())
    assert behind.ok and behind.data["clamped"] is False
    assert behind.data["bar_open"] == (T0 + timedelta(minutes=9)).isoformat()


def test_the_box_reads_nothing_after_the_instant(tape, story):
    at = T0 + timedelta(minutes=20)
    box = replay_tools(at, tape, item=story)
    for name in ("market_quote", "candlesticks", "recent_trades", "market_at_time", "volume_profile",
                 "trading_costs", "price_the_edge", "price_velocity", "market_shock", "daily_context",
                 "relative_volume", "the_story", "news_before", "sibling_moves", "market_tape",
                 "session_clock", "move_base_rate", "tape_imbalance", "news_absorption", "state_summary"):
        assert name in box, name
    path = box["candlesticks"].safe_call(symbol="ACME", hours_back=0.5)
    assert path.ok and all(b["t"] < at.isoformat() for b in path.data["bars"])
    assert path.data["bars"][-1]["t"] == (T0 + timedelta(minutes=19)).isoformat()
    hourly = box["candlesticks"].safe_call(symbol="ACME", hours_back=1, timeframe="1Hour")
    assert hourly.ok and len(hourly.data["bars"]) == 2, "09:xx and 10:xx"
    velocity = box["price_velocity"].safe_call(symbol="ACME")
    assert velocity.ok and velocity.data["move_5m_bps"] == pytest.approx(10.0, abs=0.05)
    assert velocity.data["verdict"] == "moving", "a steady drift is five times its typical minute"
    flat = replay_tools(at, FakeBars(T0, {"ACME": {m: 100.0 for m in range(-40, 40)}}))
    assert flat["price_velocity"].safe_call(symbol="ACME").data["verdict"] == "still"


# -- the story and its siblings -------------------------------------------------

def test_news_before_excludes_later_items_and_the_story_itself(tape, story):
    at = story.created_at

    def item(id_, when, symbols=("ACME",)):
        return NewsItem(id=id_, created_at=when, updated_at=when, headline=id_, summary="",
                        source="benzinga", symbols=symbols)
    feed = [item("earlier-today", at - timedelta(hours=2)), item("after", at + timedelta(minutes=10)),
            story, item("yesterday", at - timedelta(hours=23)), item("other-name", at - timedelta(hours=1), ("XYZ",))]
    for leaky in (False, True):
        out = replay_tools(at, tape, FakeNews(feed, leaky=leaky), item=story)["news_before"].safe_call(
            symbol="ACME", hours_back=24, limit=10)
        assert out.ok, out.error
        assert [i["id"] for i in out.data["items"]] == ["earlier-today", "yesterday"], leaky
        assert out.data["count"] == 2 and out.data["count_today"] == 1
    assert out.data["items"][0]["minutes_before"] == 120.0


def test_sibling_moves_excludes_the_story_symbol(tape, story):
    at = story.created_at
    out = replay_tools(at, tape, item=story)["sibling_moves"].safe_call(symbol="ACME")
    assert out.ok, out.error
    assert [s["symbol"] for s in out.data["siblings"]] == ["SPY", "ZZZ"]
    spy = out.data["siblings"][0]
    assert spy["move_5m_bps"] == pytest.approx(5.0, abs=0.05)
    assert out.data["siblings"][1]["move_5m_bps"] is None, "no bars, no move, no error"
    assert out.data["mean_move_bps"] == pytest.approx(5.0, abs=0.05)
    alone = replay_tools(at, tape, item=NewsItem(id="s", created_at=at, updated_at=at, headline="h",
                                                summary="", source="", symbols=("ACME",)))
    lonely = alone["sibling_moves"].safe_call(symbol="ACME")
    assert lonely.ok and lonely.data["siblings"] == [] and "no other symbol" in lonely.data["note"]


def test_the_story_and_the_session(tape, story):
    at = story.created_at + timedelta(seconds=45)
    box = replay_tools(at, tape, item=story)
    out = box["the_story"].safe_call()
    assert out.ok and out.data["headline"] == "ACME raises guidance"
    assert out.data["seconds_since_publication"] == 45 and out.data["edited_after"] is False
    clock = box["session_clock"].safe_call()
    assert clock.ok and clock.data["phase"] == "open" and clock.data["minutes_since_open"] == pytest.approx(50.8)
    assert replay_tools(at, tape)["the_story"].safe_call().ok is False


# -- prior sessions only ---------------------------------------------------------

def test_relative_volume_and_base_rate_read_only_sessions_before_today(tape):
    at = T0 + timedelta(minutes=20)
    box = replay_tools(at, tape)
    rel = box["relative_volume"].safe_call(symbol="ACME", minutes=15)
    assert rel.ok, rel.error
    # Fifteen bars of 300 today against fifteen of 100 on the one prior
    # session that printed; Tuesday's five thousand a bar is the future.
    assert rel.data["volume"] == 4500 and rel.data["typical_volume"] == 1500
    assert rel.data["sessions"] == 1 and rel.data["ratio"] == 3.0 and rel.data["verdict"].startswith("heavy")

    rate = box["move_base_rate"].safe_call(symbol="ACME")
    assert rate.ok, rate.error
    assert rate.data["bucket"] == "10:00-10:30" and rate.data["sessions"] == 1
    # Friday drifted two a minute, so every five-minute window moved ten;
    # Tuesday's forty a minute would have shown as two hundred.
    assert rate.data["p50"] == pytest.approx(10.0, abs=0.1) and rate.data["p90"] == pytest.approx(10.0, abs=0.1)
    assert rate.data["samples"] == 30 and rate.data["moved_share"] == 1.0
    assert rate.data["verdict"].startswith("active")
    # Every bar the two tools read was before the instant.
    assert all(c["end"] <= at for c in tape.bar_calls)


def test_daily_context_reads_yesterday_and_today_before_the_instant(tape):
    at = T0 + timedelta(minutes=20)
    out = replay_tools(at, tape)["daily_context"].safe_call(symbol="ACME")
    assert out.ok, out.error
    assert out.data["prev_close"] == 99.0, "Friday's close, not today's daily bar"
    assert out.data["today_open"] == pytest.approx(100 * (1 - 30 * 2 / 1e4)), "the 09:30 bar"
    assert out.data["gap_bps"] == pytest.approx(((100 * (1 - 60 / 1e4)) / 99.0 - 1) * 1e4, abs=0.1)
    assert out.data["last"] == pytest.approx(100 * (1 + 19 * 2 / 1e4))
    assert out.data["position_in_range"] > 0.95 and out.data["sessions"] == 20
    assert 98 < out.data["range_so_far_bps"] < 104, "the drift plus the bars' own wicks"


# -- the tape ---------------------------------------------------------------------

def test_tape_imbalance_ignores_a_print_after_the_instant():
    at = T0 + timedelta(minutes=20)
    before = [{"t": at - timedelta(minutes=3), "p": 100.0, "s": 10},
              {"t": at - timedelta(minutes=2), "p": 100.1, "s": 30},
              {"t": at - timedelta(minutes=1), "p": 100.05, "s": 10}]
    after = [{"t": at + timedelta(minutes=1), "p": 90.0, "s": 500}]
    series = {"ACME": _series(range(-40, 40))}
    clean = replay_tools(at, FakeBars(T0, series, prints=before))["tape_imbalance"].safe_call(symbol="ACME")
    leaky = replay_tools(at, FakeBars(T0, series, prints=before + after, leaky=True))["tape_imbalance"].safe_call(symbol="ACME")
    assert clean.ok and clean.data == leaky.data, "a print from the future changes nothing"
    assert clean.data["prints"] == 3 and clean.data["uptick_volume"] == 30 and clean.data["downtick_volume"] == 10
    assert clean.data["imbalance"] == pytest.approx(0.5) and clean.data["verdict"].startswith("buyers")
    recent = replay_tools(at, FakeBars(T0, series, prints=before + after, leaky=True))["recent_trades"].safe_call(
        symbol="ACME", minutes_back=5, limit=20)
    assert recent.ok and recent.data["count"] == 3 and recent.data["trades"][0]["price"] == 100.05


def test_tool_cache_serves_the_second_call(tape, tmp_path):
    at = T0 + timedelta(minutes=20)
    cache = ToolCache(tmp_path)
    replay_tools(at, tape, cache=cache)["recent_trades"].safe_call(symbol="ACME")
    replay_tools(at, tape, cache=cache)["recent_trades"].safe_call(symbol="ACME")
    assert len(tape.trade_calls) == 1
    n = len(tape.bar_calls)
    replay_tools(at, tape, cache=cache)["move_base_rate"].safe_call(symbol="ACME")
    m = len(tape.bar_calls)
    replay_tools(at, tape, cache=cache)["move_base_rate"].safe_call(symbol="ACME")
    assert m > n and len(tape.bar_calls) == m


def test_state_summary_is_one_short_paragraph(tape, story):
    at = story.created_at
    out = replay_tools(at, tape, item=story)["state_summary"].safe_call(symbol="ACME")
    assert out.ok, out.error
    text = out.data["summary"]
    assert text == out.text and len(text) <= 600
    assert "ACME last" in text and "Story (benzinga, 0s ago)" in text and "SPY" in text
    assert "Day: gap" in text and "Base rate:" in text
    absorbed = replay_tools(at + timedelta(minutes=3), tape, item=story)["news_absorption"].safe_call(symbol="ACME")
    assert absorbed.ok and absorbed.data["move_since_bps"] == pytest.approx(6.0, abs=0.05)
    assert absorbed.data["still_drifting"] is True and absorbed.data["seconds_since_publication"] == 180


def test_arithmetic_tools_need_no_clock(tape):
    box = replay_tools(T0, tape)
    cost = box["trading_costs"].safe_call(price=100.0, shares=100, spread_bps=3.0)
    assert cost.ok and cost.data["round_trip_usd"] == pytest.approx(3.0 + 0.278 + 0.0166, abs=1e-3)
    assert cost.data["breakeven_bps"] == pytest.approx(3.29, abs=0.01)
    later = replay_tools(T0 + timedelta(days=30), tape)["trading_costs"].safe_call(price=100.0, shares=100, spread_bps=3.0)
    assert later.data == cost.data
    edge = box["price_the_edge"].safe_call(expected_bps=20, half_width_bps=10, cost_bps=3)
    assert edge.ok and edge.data["worth_trading"] is True and edge.data["net_bps"] == 17
    none = box["price_the_edge"].safe_call(expected_bps=2, half_width_bps=10, cost_bps=3)
    assert none.ok and none.data["worth_trading"] is False


# -- the session ----------------------------------------------------------------------

def test_in_window_at_the_edges_and_across_dst():
    ny = session(T0)["clock_et"]
    assert ny == "10:00:00" and session(T0)["phase"] == "open"
    # EST: 09:35 ET is 14:35Z.
    est = datetime(2026, 3, 6, 14, 35, tzinfo=UTC)
    assert in_window(est) and not in_window(est - timedelta(minutes=1))
    # EDT, the Monday after the clocks went forward: 09:35 ET is 13:35Z.
    edt = datetime(2026, 3, 9, 13, 35, tzinfo=UTC)
    assert in_window(edt) and not in_window(edt - timedelta(minutes=1))
    assert not in_window(datetime(2026, 3, 9, 14, 34, tzinfo=UTC) - timedelta(hours=1))
    # 15:54 ET may still ask for five minutes; 15:55 may not.
    close = datetime(2026, 6, 1, 19, 54, tzinfo=UTC)
    assert in_window(close) and not in_window(close + timedelta(minutes=1))
    assert not in_window(datetime(2026, 6, 6, 15, 0, tzinfo=UTC)), "Saturday"
    assert session(datetime(2026, 6, 1, 12, 0, tzinfo=UTC))["phase"] == "pre"
    assert session(datetime(2026, 6, 1, 21, 0, tzinfo=UTC))["phase"] == "closed"


# -- the question set ------------------------------------------------------------------

def test_build_windows_offline_one_file_per_group_and_resumes(tape, tmp_path):
    def item(sym, at, id_):
        return BenchmarkItem(symbol=sym, news_id=id_, at=at, headline=f"{sym} news", source="benzinga",
                             symbols=(sym,), updated_at=at + timedelta(hours=1))
    items = [item("ACME", T0 + timedelta(minutes=20), "a"),           # good
             item("ACME", T0 - timedelta(minutes=40), "b"),           # 09:20 ET: before the window opens
             item("ACME", T0 + timedelta(minutes=38), "c"),           # horizon at 10:43, last bar 10:39: stale
             item("ACME", T0 + timedelta(days=1, minutes=20), "d"),   # Tuesday has bars at 10:20 in this tape
             item("SPY", T0 + timedelta(minutes=30), "e")]
    log = []
    windows = build_windows(items, bars=tape, windows_dir=tmp_path, log=log.append)
    assert [w.id for w in windows] == [f"ACME@{(T0 + timedelta(minutes=20)).isoformat()}#a",
                                       f"ACME@{(T0 + timedelta(days=1, minutes=20)).isoformat()}#d",
                                       f"SPY@{(T0 + timedelta(minutes=30)).isoformat()}#e"]
    a = windows[0]
    assert a.mid_now == pytest.approx(100 * (1 + 19 * 2 / 1e4)) and a.realised == pytest.approx(100 * (1 + 24 * 2 / 1e4))
    assert a.group == "ACME-20260601" and a.edited_after is True and a.to_dict()["group"] == a.group
    assert sorted(p.name for p in tmp_path.iterdir()) == ["ACME-20260601.h5.json", "ACME-20260602.h5.json",
                                                          "SPY-20260601.h5.json"]
    assert any("outside regular hours" in m for m in log) and any("horizon" in m for m in log)
    n = len(tape.bar_calls)
    again = build_windows(items, bars=tape, windows_dir=tmp_path, log=log.append)
    assert len(tape.bar_calls) == n and [w.id for w in again] == [w.id for w in windows]
    assert NewsWindow.from_dict(a.to_dict()) == a


def test_the_task_reads_its_windows_and_scores_in_bps(tape, tmp_path):
    task = NewsEquity(items=[BenchmarkItem(symbol="ACME", news_id="a", at=T0 + timedelta(minutes=20),
                                           headline="ACME raises guidance", symbols=("ACME", "SPY"))],
                      bars=tape, news=FakeNews([]), windows_dir=tmp_path)
    (w,) = task.instances()
    assert task.moved(w) and task.metric.unit == "bps"
    assert task.label(w).startswith("ACME at 2026-06-01T14:20:00Z: ACME raises guidance")
    inputs = task.run_inputs(w)
    assert inputs["question"] == "ACME" and json.loads(inputs["news"])["headline"] == "ACME raises guidance"
    assert json.loads(inputs["context"])["phase"] == "open"
    assert task.context_of(w)["session"]["clock_et"] == "10:20:00"
    box = task.toolbox(w)
    assert box["the_story"].safe_call().data["symbols"] == ["ACME", "SPY"]
    assert set(Harness.load(OPUS).tools) <= set(task.tools())
    assert task.instance_from_dict(w.to_dict()) == w
    task.use_instances([w])
    assert task.instances() == [w]

    class _Run:
        def __init__(self, output):
            self.output, self.cost_usd, self.ok, self.error, self.error_kind = output, 0.01, True, None, None

        def tools_seen(self):
            return []
    # The market moved ten basis points (9.96, the drift being linear in the
    # base price); a ten-point call removes all but that.
    good = task.score(w, _Run({"delta_bps": 10.0, "half_width_bps": 5}))
    assert good.details["skill"] == pytest.approx(1.0, abs=0.01) and good.value == pytest.approx(0.55, abs=1e-3)
    assert "+10.0 bps" in good.feedback and "skill +1.00" in good.feedback
    quiet = task.score(w, _Run({"delta_bps": 0}))
    assert quiet.value == pytest.approx(0.5) and "echoed the last close" in quiet.feedback
    broken = task.failed(w, "no such tool")
    assert broken.value == pytest.approx(0.45) and task.statistic([quiet, broken]) == 0.0
    assert task.summary([good, quiet, broken])["unscored"] == 1


# -- the metric, in basis points -------------------------------------------------------

def test_bps_metric_cases():
    assert METRIC.tick == 5 and METRIC.relative and spec_of("news-equity-5m").unit == "bps"
    quiet = score_output({"delta_bps": 0}, 100.0, 100.05)
    assert quiet.value == 0.5 and quiet.skill == 0.0 and quiet.echoed
    assert silent(100.0, 100.05).to_dict() == quiet.to_dict()
    small = score_output({"delta_bps": 0}, 100.0, 100.03)
    assert small.naive_error == pytest.approx(3.0) and small.benchmark == TICK_BPS and not small.moved
    loud = score_output({"delta_bps": 4}, 100.0, 100.0)
    assert loud.skill < 0 and loud.value < 0.5, "a call on a dead market costs"
    exact = score_output({"delta_bps": 20, "half_width_bps": 3}, 100.0, 100.20)
    assert exact.skill == pytest.approx(1.0) and exact.value == pytest.approx(0.6) and exact.covered
    assert score_output({"delta_bps": "20"}, 100.0, 100.2) is None
    assert score_output({"delta_bps": 20}, 0.0, 100.2) is None


# -- the harness files -----------------------------------------------------------------

def test_both_harness_files_load_against_the_box():
    task = NewsEquity(windows=[])
    box = set(task.tools())
    for path in (OPUS, JEV):
        h = Harness.load(path)
        assert set(h.tools) <= box, path.name
        assert h.plan.required_inputs() <= task.inputs, path.name
        assert h.from_components(h.to_components()).to_components() == h.to_components()
    jev = Harness.load(JEV)
    assert jev.config.model == "typesafe/jev-1.13"
    step = jev.plan.steps[-1]
    assert step.questions["move"]["values"] == [-150, -60, -20, 0, 20, 60, 150]
    piled = {"move": {"type": "score", "score": 6.0,
                      "probabilities": {str(i): (1.0 if i == 6 else 0.0) for i in range(7)}, "confidence": 0.9}}
    out = answers_to_output(step.questions, piled, step.answers)
    assert out["delta_bps"] == 150 and out["half_width_bps"] == 2 and out["confidence"] == 0.9
    spread = {"move": {"type": "score", "score": 3.0,
                       "probabilities": {"2": 0.3, "3": 0.4, "4": 0.3}, "confidence": 0.5}}
    out = answers_to_output(step.questions, spread, step.answers)
    assert out["delta_bps"] == 0 and out["half_width_bps"] == 20


async def test_the_runner_carries_both_harnesses_over_a_window(tape, tmp_path):
    task = NewsEquity(items=[BenchmarkItem(symbol="ACME", news_id="a", at=T0 + timedelta(minutes=20),
                                           headline="ACME raises guidance", symbols=("ACME",))],
                      bars=tape, news=FakeNews([]), windows_dir=tmp_path)
    (w,) = task.instances()
    answer = {"delta_bps": 10, "half_width_bps": 5, "confidence": 0.7, "driver": "guidance", "falsifier": "fade"}
    llm = FakeLLM(script=lambda messages, schema, tools: answer)
    (rollout,) = await evaluate(task, Harness.load(OPUS), [w], llm)
    assert rollout.run is not None and rollout.run.ok, rollout.run.error if rollout.run else "no run"
    assert rollout.outcome.details["skill"] == pytest.approx(1.0, abs=0.01)
    prompt = llm.calls[-1]["messages"][-1]["content"]
    assert "ACME raises guidance" in prompt and '"stale_s": 0' in prompt, "the 10:19 bar closed on the instant"
    (jev,) = await evaluate(task, Harness.load(JEV), [w], FakeLLM())
    assert jev.run is not None and jev.run.ok, jev.run.error
    assert jev.outcome.details["echoed"] is True, "the default fake piles the mass on 'unchanged'"


# -- the topic seam --------------------------------------------------------------------

def test_the_spec_and_the_cli_know_the_topic(capsys):
    from rsi_arena.cli import main

    spec = spec_of("news-equity-5m")
    assert spec.harness == "harnesses/news-equity-5m-jev.json" == spec.jev_harness
    assert spec.benchmark == "benchmarks/news-2026-09.json" and spec.windows_dir == "benchmarks/windows-news"
    assert spec.runs_dir == "runs/news-equity-5m" and spec.per_fixture == 0
    assert spec.window_usd == 0.00005 and spec.model_choices == ("typesafe/jev-1.13", "openai/gpt-5-mini")
    assert (spec.holdout, spec.audit, spec.max_metric_calls, spec.valset, spec.max_day_usd) == (300, 100, 4800, 400, 15)
    assert main(["topic", "--topic", "news-equity-5m", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["harness"] == spec.harness and out["unit"] == "bps" and "factory" not in out
    # A flag left unset on the command line is the topic's to answer.
    from rsi_arena.cli import _settings, build_parser
    s = _settings(build_parser().parse_args(["optimize", "--topic", "news-equity-5m"]))
    assert s.harness == spec.harness and s.benchmark == spec.benchmark
    assert s.runs_dir == spec.runs_dir and s.window_usd == spec.window_usd
    assert s.model_choices == spec.model_choices
    assert Settings().harness == "harnesses/horizon-5m.json", "the first topic keeps the dataclass"
    assert json.loads((ROOT / "benchmarks" / "news-2026-09.json").read_text()) == []
    assert json.loads((ROOT / "runs" / "archive.news-equity-5m.json").read_text()) == {"entries": []}
    universe = [l for l in (ROOT / "benchmarks" / "universe-us.txt").read_text().splitlines()
                if l and not l.startswith("#")]
    assert {"AAPL", "SPY", "TLT", "BRK.B"} <= set(universe) and len(universe) >= 100


# -- the data layer against a mock transport -----------------------------------------------

def _transport(pages: dict[str, list[dict]], seen: list[httpx.Request], statuses: list[int] | None = None):
    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if statuses:
            code = statuses.pop(0)
            if code == 429:
                return httpx.Response(429, headers={"Retry-After": "3"}, json={"message": "slow down"})
            if code >= 500:
                return httpx.Response(code, json={})
        token = request.url.params.get("page_token")
        rows = pages[request.url.path]
        index = int(token) if token else 0
        body = dict(rows[index])
        if index + 1 < len(rows):
            body["next_page_token"] = str(index + 1)
        return httpx.Response(200, json=body)
    return httpx.MockTransport(handle)


def test_the_client_reads_keys_late_pages_and_waits_on_429(monkeypatch):
    monkeypatch.delenv("APCA_API_KEY_ID", raising=False)
    monkeypatch.delenv("APCA_API_SECRET_KEY", raising=False)
    seen: list[httpx.Request] = []
    pages = {"/v2/stocks/bars": [{"bars": {"AAPL": [{"t": "2026-06-01T13:30:00Z", "o": 1, "h": 1, "l": 1, "c": 1, "v": 1, "n": 1, "vw": 1}]}},
                                 {"bars": {"AAPL": [{"t": "2026-06-01T13:31:00Z", "o": 2, "h": 2, "l": 2, "c": 2, "v": 2, "n": 2, "vw": 2}]}}]}
    client = AlpacaData(transport=_transport(pages, seen), sleep=lambda s: seen.append(s))
    with pytest.raises(MissingCredentials, match="APCA_API_KEY_ID"):
        client.get("/v2/stocks/bars", {"symbols": "AAPL"})
    assert not seen, "nothing was sent without keys"
    monkeypatch.setenv("APCA_API_KEY_ID", "k")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "s")
    merged = client.collect("/v2/stocks/bars", "bars", {"symbols": "AAPL"})
    assert [b["c"] for b in merged["AAPL"]] == [1, 2]
    assert seen[0].headers["APCA-API-KEY-ID"] == "k" and "page_token" not in str(seen[0].url)
    assert seen[1].url.params["page_token"] == "1"

    seen.clear()
    slow = AlpacaData(key_id="k", secret="s", transport=_transport(pages, seen, statuses=[429, 503]),
                      sleep=lambda s: seen.append(("slept", s)))
    out = slow.get("/v2/stocks/bars", {"symbols": "AAPL"})
    assert out["bars"]["AAPL"][0]["c"] == 1
    assert ("slept", 3.0) in seen and ("slept", 1.0) in seen and slow.requests == 3


def test_the_bar_store_makes_the_second_read_offline(tmp_path):
    seen: list[httpx.Request] = []
    day = date(2026, 6, 1)
    rows = [{"t": f"2026-06-01T{13 + (m + 30) // 60:02d}:{(m + 30) % 60:02d}:00Z", "o": 100 + m, "h": 100 + m,
             "l": 100 + m, "c": 100 + m, "v": 10, "n": 1, "vw": 100 + m} for m in range(0, 60)]
    daily = [{"t": "2026-05-29T04:00:00Z", "o": 99, "h": 99, "l": 99, "c": 99, "v": 1e6, "n": 1, "vw": 99}]
    pages = {"/v2/stocks/bars": [{"bars": {"ACME": rows}}]}

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.params.get("timeframe") == "1Day":
            return httpx.Response(200, json={"bars": {"ACME": daily}})
        return httpx.Response(200, json=pages["/v2/stocks/bars"][0])
    client = AlpacaData(key_id="k", secret="s", transport=httpx.MockTransport(handle))
    bars = AlpacaBars(client, BarStore(tmp_path))
    at = datetime(2026, 6, 1, 14, 0, 30, tzinfo=UTC)
    assert bars.price_at("ACME", at) == 129, "the 13:59 bar; the 14:00 bar is still open"
    assert bars.fetches == 1 and (tmp_path / "ACME" / "2026-06-01.json.gz").exists()
    assert seen[0].url.params["start"] == "2026-06-01T00:00:00Z" and seen[0].url.params["feed"] == "iex"
    with gzip.open(tmp_path / "ACME" / "2026-06-01.json.gz", "rt") as fh:
        assert len(json.load(fh)) == 60
    offline = AlpacaBars(AlpacaData(transport=httpx.MockTransport(lambda r: httpx.Response(500))),
                         BarStore(tmp_path))
    assert offline.price_at("ACME", at) == 129 and offline.realised_price("ACME", at) == 134
    assert offline.fetches == 0
    prior = bars.daily("ACME", before_date=day, days=20)
    assert [b.c for b in prior] == [99] and (tmp_path / "ACME" / "daily.json.gz").exists()
    assert [b.c for b in offline.daily("ACME", before_date=day, days=1)] == [99]
    assert bars.ensure_days("ACME", [day]) == 0


# -- discovery -----------------------------------------------------------------------------

def test_discovery_keeps_one_of_a_repeated_headline_and_spaces_the_rest():
    import importlib.util
    from collections import Counter

    spec = importlib.util.spec_from_file_location("discover_news", ROOT / "scripts" / "discover_news.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    at = T0 + timedelta(minutes=20)

    def item(id_, when, headline):
        return NewsItem(id=id_, created_at=when, updated_at=when, headline=headline, summary="",
                        source="benzinga", symbols=("ACME",))
    feed = [("ACME", item("a", at, "ACME rises 3% on guidance")),
            ("ACME", item("b", at + timedelta(minutes=12), "ACME rises 4% on guidance")),   # same story
            ("ACME", item("c", at + timedelta(minutes=15), "ACME names a new CFO")),        # 15m after a: kept
            ("ACME", item("d", at + timedelta(minutes=20), "ACME to buy Widget Co")),       # 5m after c: too close
            ("ACME", item("e", at + timedelta(minutes=50), "ACME rises 5% on guidance")),   # 50m on: a new story
            ("ACME", item("f", at - timedelta(hours=3), "ACME pre-market note"))]          # 07:20 ET
    rejects, examples = Counter(), {}
    rows = mod.select(feed, per_symbol_day=0, rejects=rejects, examples=examples)
    assert [r.news_id for r in rows] == ["a", "c", "e"]
    assert rejects == {"duplicate headline within 30 minutes": 1,
                       "within 10 minutes of a kept item on the name": 1,
                       "outside regular hours": 1}
    assert mod.normalise("ACME rises 3% on guidance") == mod.normalise("ACME rises 4% on guidance")
    capped = mod.select(feed, per_symbol_day=2, rejects=Counter(), examples={})
    # Spaced the way _common/thin spaces a match: index int(i * n/k), so three
    # kept items capped at two keep the first and the middle, never the last two.
    assert [r.news_id for r in capped] == ["a", "c"], "evenly spaced through the day, not the last two"

def test_discover_news_dry_run_is_the_whole_pipeline_offline(tmp_path, monkeypatch):
    monkeypatch.delenv("APCA_API_KEY_ID", raising=False)
    monkeypatch.delenv("APCA_API_SECRET_KEY", raising=False)
    out = tmp_path / "news.json"
    proc = subprocess.run([sys.executable, "scripts/discover_news.py", "--dry-run", "--out", str(out),
                           "--data-dir", str(tmp_path / "data"), "--from", "2026-06-01", "--to", "2026-06-05"],
                          cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "DRY RUN" in proc.stdout and "python scripts/discover_news.py --universe" in proc.stdout
    assert "reject" in proc.stdout and "outside regular hours" in proc.stdout
    rows = load_benchmark(out)
    assert rows and all(in_window(r.at) for r in rows)
    assert all(r.source == "benzinga" for r in rows)
    # The store it filled is enough to build every window with no key at all.
    windows = build_windows(rows, bars=AlpacaBars(AlpacaData(), BarStore(tmp_path / "data" / "bars")),
                            windows_dir=tmp_path / "windows")
    assert len(windows) == len(rows)
    assert len({w.group for w in windows}) >= 4
    # And without keys the real run refuses with the command to run, not a traceback.
    real = subprocess.run([sys.executable, "scripts/discover_news.py", "--out", str(tmp_path / "x.json")],
                          cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert real.returncode == 2 and "APCA_API_KEY_ID" in real.stdout and "Traceback" not in real.stderr
