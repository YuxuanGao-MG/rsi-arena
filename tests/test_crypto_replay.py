"""The crypto box: frozen at an instant of a market that never closes."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from rsi_arena.crypto._binance import close_at
from rsi_arena.crypto.replay import TICK_BPS, live_tools, replay_tools, us_equity_hours
from rsi_arena.harness import Harness, ToolCache
from rsi_arena.harness.decisions import answers_to_output
from rsi_arena.topics import spec_of
from rsi_arena.topics.crypto_horizon import (METRIC, Benchmark, CryptoHorizon, CryptoWindow,
                                             build_windows, score_output, silent)
from rsi_arena.topics.crypto_horizon.windows import price_at
from tests.conftest import FakeAggTrades, FakeDaily, FakeFunding, FakeKlines, FakeOI, ramp

UTC = timezone.utc


# --- the point-in-time rule -------------------------------------------------------

def test_a_bar_is_known_only_when_complete(spot, c0):
    """At 12:20:00 the 12:19 bar has just closed and is the price; the 12:20 bar
    is being printed and is not. Thirty seconds later the answer is the same."""
    at = c0 + timedelta(minutes=20)
    bars = spot.klines("BTCUSDT", at - timedelta(minutes=5), at + timedelta(minutes=5))
    k = close_at(bars, at)
    assert k is not None and k.ts_open == c0 + timedelta(minutes=19)
    assert close_at(bars, at + timedelta(seconds=30)).ts_open == k.ts_open
    assert price_at(spot, "BTCUSDT", at) == pytest.approx(spot.series["BTCUSDT"][19])
    quote = replay_tools(at, spot)["market_quote"].safe_call(symbol="BTCUSDT")
    assert quote.ok and quote.data["price"] == pytest.approx(spot.series["BTCUSDT"][19])
    assert quote.data["bar_open"] == k.ts_open.isoformat() and quote.data["age_s"] == 0
    assert quote.data["change_1m_bps"] == pytest.approx(1.0, abs=0.01)
    # Three missing minutes are a hole, not a price.
    holed = FakeKlines(c0, {"BTCUSDT": {m: v for m, v in ramp(60).items() if m not in (17, 18, 19)}})
    assert price_at(holed, "BTCUSDT", at) is None
    assert not replay_tools(at, holed)["market_quote"].safe_call(symbol="BTCUSDT").ok


def test_candlesticks_stop_at_the_instant_and_resample(spot, c0):
    at = c0 + timedelta(minutes=30, seconds=20)
    box = replay_tools(at, spot)
    ones = box["candlesticks"].safe_call(symbol="BTCUSDT", hours_back=0.25, interval="1m")
    assert ones.ok and all(b["ts"] < at.isoformat() for b in ones.data["bars"])
    assert ones.data["bars"][-1]["ts"] == (c0 + timedelta(minutes=29)).isoformat()
    fives = box["candlesticks"].safe_call(symbol="BTCUSDT", hours_back=0.5, interval="5m")
    assert fives.ok and fives.data["bars"][-1]["ts"] == (c0 + timedelta(minutes=25)).isoformat(), \
        "the 12:30 five-minute bar is still being printed"
    bad = box["candlesticks"].safe_call(symbol="BTCUSDT", interval="3m")
    assert not bad.ok and "1m" in bad.error


def test_asking_for_a_time_past_the_window_gets_the_window(spot, c0):
    at = c0 + timedelta(minutes=20)
    box = replay_tools(at, spot)
    ahead = box["market_at_time"].safe_call(symbol="BTCUSDT", when=(at + timedelta(hours=3)).isoformat())
    assert ahead.ok and ahead.data["clamped"] is True and ahead.data["answered_at"] == at.isoformat()
    assert ahead.data["price"] == pytest.approx(spot.series["BTCUSDT"][19])
    behind = box["market_at_time"].safe_call(symbol="BTCUSDT", when=(at - timedelta(minutes=10)).isoformat())
    assert behind.ok and behind.data["clamped"] is False
    assert behind.data["price"] == pytest.approx(spot.series["BTCUSDT"][9])
    assert not box["market_at_time"].safe_call(symbol="BTCUSDT", when="yesterday").ok


# --- the perp: absent rather than wrong ------------------------------------------

def _ms(when: datetime) -> int:
    return int(when.timestamp() * 1000)


def test_open_interest_before_the_first_record_fails_with_a_sentence(spot, c0):
    first = c0 + timedelta(days=1)
    rows = [{"ts": _ms(first + timedelta(minutes=5 * i)), "oi": 1000 + i, "oi_usd": 1e8} for i in range(60)]
    early = replay_tools(c0, spot, futures=FakeOI(rows))["open_interest"].safe_call(symbol="BTCUSDT")
    assert not early.ok and early.error == "open interest for BTCUSDT not recorded before 2026-08-24"
    later = replay_tools(first + timedelta(hours=2), spot, futures=FakeOI(rows))["open_interest"].safe_call(symbol="BTCUSDT")
    # The period closing at 14:00 opened at 13:55 (row 23); a row is known when its period closes.
    assert later.ok and later.data["oi"] == 1023 and later.data["change_1h_pct"] == round((1023 / 1011 - 1) * 100, 3)
    assert later.data["change_4h_pct"] is None and later.data["change_24h_pct"] is None
    # A daily row is known a day after its stamp, and a daily-only series says so.
    daily = [{"ts": _ms(c0 - timedelta(days=d)), "oi": 500 + d, "oi_usd": 1e8, "period": "1D"} for d in (3, 2, 1)]
    day = replay_tools(c0 + timedelta(hours=12), spot, futures=FakeOI(daily))["open_interest"].safe_call(symbol="BTCUSDT")
    assert day.ok and day.data["oi"] == 501 and day.data["resolution"] == "1D"
    assert day.data["as_of"] == c0.isoformat() and day.data["change_1h_pct"] is None
    assert day.data["change_24h_pct"] == round((501 / 502 - 1) * 100, 3) and "daily points only" in day.data["verdict"]
    none = replay_tools(c0, spot)["open_interest"].safe_call(symbol="BTCUSDT")
    assert not none.ok and "no futures data" in none.error


def test_funding_serves_the_last_settled_rate_and_the_next_reset(spot, c0):
    rows = [{"ts": _ms(c0.replace(hour=h)), "rate": 0.0001 * (i + 1), "realized": 0.0001 * (i + 1)}
            for i, h in enumerate((0, 8))]
    rows.append({"ts": _ms(c0.replace(hour=16)), "rate": 0.005, "realized": 0.005})   # not yet
    out = replay_tools(c0, spot, futures=FakeFunding(rows))["funding_rate"].safe_call(symbol="BTCUSDT")
    assert out.ok, out.error
    assert out.data["rate"] == pytest.approx(0.0002) and out.data["settled_at"] == c0.replace(hour=8).isoformat()
    assert out.data["next_funding"] == c0.replace(hour=16).isoformat() and out.data["minutes_to_next"] == 240
    assert out.data["last_24h_bps"] == [1.0, 2.0], "the 16:00 rate has not settled"


# --- the chain: yesterday is the newest day there is -------------------------------

def test_daily_onchain_series_never_serve_the_day_of_the_instant(spot, c0):
    day = int(c0.replace(hour=0).timestamp())
    rows = [{"ts": day + 86400 * d, "value": 1000.0 + d} for d in (-3, -2, -1, 0, 1)]
    chain = FakeDaily({"n_transactions": ("daily", rows), "mempool_size": ("daily", rows),
                       "hash_rate": ("daily", rows), "stablecoin_supply": ("daily", rows),
                       "dex_volume": ("daily", rows)})
    box = replay_tools(c0, spot, onchain=chain)
    activity = box["chain_activity"].safe_call(asset="BTC")
    assert activity.ok, activity.error
    assert activity.data["n_transactions"]["value"] == 999.0
    assert activity.data["n_transactions"]["date"] == (c0 - timedelta(days=1)).date().isoformat()
    stables = box["stablecoin_supply"].safe_call()
    assert stables.ok and stables.data["total_usd"] == 999.0
    dex = box["dex_volume"].safe_call()
    assert dex.ok and dex.data["volume_usd"] == 999.0
    # Even at one second to midnight the day's own figure is the future.
    late = replay_tools(c0.replace(hour=23, minute=59, second=59), spot, onchain=chain)
    assert late["stablecoin_supply"].safe_call().data["total_usd"] == 999.0
    # Before the first point: a sentence, not the earliest point.
    early = replay_tools(c0 - timedelta(days=10), spot, onchain=chain)["stablecoin_supply"].safe_call()
    assert not early.ok and "not recorded before" in early.error


def test_block_series_serve_up_to_the_instant(spot, c0):
    sec = int(c0.timestamp())
    rows = [{"ts": sec - 600, "height": 1, "fee_10": 1, "fee_50": 2, "fee_90": 5},
            {"ts": sec, "height": 2, "fee_10": 1, "fee_50": 3, "fee_90": 6},
            {"ts": sec + 60, "height": 3, "fee_10": 9, "fee_50": 9, "fee_90": 9}]
    out = replay_tools(c0, spot, onchain=FakeDaily({"fee_rates": ("block", rows)}))["chain_fees"].safe_call()
    assert out.ok and out.data["height"] == 2 and out.data["fee_50_sat_vb"] == 3


# --- across the coins, and the calendar in the price ------------------------------

def test_cross_asset_excludes_the_asked_symbol(spot, c0):
    at = c0 + timedelta(hours=2)
    out = replay_tools(at, spot)["cross_asset"].safe_call(symbol="ETHUSDT")
    assert out.ok, out.error
    names = [r["symbol"] for r in out.data["others"]]
    assert "ETHUSDT" not in names and set(names) == {"BTCUSDT", "SOLUSDT"}
    btc = next(r for r in out.data["others"] if r["symbol"] == "BTCUSDT")
    assert btc["move_5m_bps"] == pytest.approx(5.0, abs=0.05) and btc["move_1h_bps"] == pytest.approx(60.0, abs=0.5)
    assert btc["move_24h_bps"] is None, "no bar a day ago"


def test_seasonality_and_base_rate_read_only_bars_before_the_instant(spot, c0):
    at = c0 + timedelta(days=2)
    clean = replay_tools(at, spot)
    # The same series with a crash in the bar being printed at the instant and
    # the bars after it: none of that is known yet, so nothing may change.
    crashed = dict(spot.series["BTCUSDT"])
    for m in range(2 * 1440, 2 * 1440 + 300):
        crashed[m] = 50000.0
    leaky = replay_tools(at, FakeKlines(c0, {**spot.series, "BTCUSDT": crashed}))
    for tool in ("hourly_seasonality", "move_base_rate"):
        a = clean[tool].safe_call(symbol="BTCUSDT")
        b = leaky[tool].safe_call(symbol="BTCUSDT")
        assert a.ok, a.error
        assert a.data == b.data, tool
    rate = clean["move_base_rate"].safe_call(symbol="BTCUSDT").data
    assert rate["horizon_minutes"] == 1 and rate["hour_utc"] == 12
    assert rate["samples"] == 120, "two days of the noon hour, sixty one-minute moves each"
    assert rate["p50"] == pytest.approx(1.0, abs=0.05) and rate["moved_share"] == 0.0, \
        "a basis point a minute never clears a two-basis-point tick"
    flat = clean["move_base_rate"].safe_call(symbol="ETHUSDT").data
    assert flat["verdict"].startswith("quiet") and flat["mean_abs_bps"] == 0.0
    season = clean["hourly_seasonality"].safe_call(symbol="BTCUSDT").data
    assert season["horizon_minutes"] == 1 and season["days"] == pytest.approx(2.0, abs=0.01)
    assert season["this_slot"]["samples"] == 0, "Tuesday noon has not been seen: the series ends there"
    # Half an hour into Monday noon, the slot holds the twenty-nine moves seen so far in it.
    partial = replay_tools(c0 + timedelta(days=1, minutes=30), spot)["hourly_seasonality"].safe_call(symbol="BTCUSDT").data
    assert partial["this_slot"]["samples"] == 29 and partial["this_slot"]["drift_bps"] == pytest.approx(1.0, abs=0.01)
    # A five-minute box measures five-minute moves, and says so.
    five = replay_tools(at, spot, horizon=5)["move_base_rate"].safe_call(symbol="BTCUSDT").data
    assert five["horizon_minutes"] == 5 and five["p50"] == pytest.approx(5.0, abs=0.05)
    assert five["moved_share"] == 1.0


def test_tape_imbalance_ignores_a_trade_after_the_instant(spot, c0):
    at = c0 + timedelta(minutes=20)
    before = [(at - timedelta(minutes=3), 100.0, 30.0, False),     # taker bought 30
              (at - timedelta(minutes=1), 100.0, 10.0, True)]      # taker sold 10
    after = [(at + timedelta(seconds=1), 100.0, 500.0, True)]
    clean = replay_tools(at, spot, FakeAggTrades(before))["tape_imbalance"].safe_call(symbol="BTCUSDT")
    leaky = replay_tools(at, spot, FakeAggTrades(before + after))["tape_imbalance"].safe_call(symbol="BTCUSDT")
    assert clean.ok and clean.data == leaky.data, "a print from the future changes nothing"
    assert clean.data["prints"] == 2 and clean.data["taker_bought"] == 30 and clean.data["taker_sold"] == 10
    assert clean.data["imbalance"] == pytest.approx(0.5) and clean.data["verdict"].startswith("buyers")
    assert clean.data["source"] == "aggTrades"
    stale = [(at - timedelta(minutes=30), 100.0, 99.0, True)]
    quiet = replay_tools(at, spot, FakeAggTrades(stale))["tape_imbalance"].safe_call(symbol="BTCUSDT", minutes_back=10)
    assert quiet.ok and quiet.data["prints"] == 0 and quiet.data["verdict"].startswith("balanced")
    # Without a tape the bars' own taker-buy volume stands in, and says so.
    bars_only = replay_tools(at, spot)["tape_imbalance"].safe_call(symbol="BTCUSDT")
    assert bars_only.ok and bars_only.data["source"] == "klines" and bars_only.data["imbalance"] == pytest.approx(0.2)
    # The newest prints come back newest first, none after the instant.
    tape = replay_tools(at, spot, FakeAggTrades(before + after))["recent_trades"].safe_call(symbol="BTCUSDT", limit=5)
    assert tape.ok and [t["side"] for t in tape.data["trades"]] == ["sell", "buy"]


def test_session_clock_does_the_date_maths(spot, c0):
    out = replay_tools(c0, spot)["session_clock"].safe_call()
    assert out.ok and out.data["weekend"] is True and out.data["minutes_to_funding"] == 240
    assert out.data["us_equity_open"] is False and out.data["minutes_to_utc_day_close"] == 720
    monday = c0 + timedelta(days=1, hours=2)                     # Monday 14:00 UTC, EDT
    open_, close_ = us_equity_hours(monday)
    assert open_ == monday.replace(hour=13, minute=30) and close_ == monday.replace(hour=20)
    during = replay_tools(monday, spot)["session_clock"].safe_call().data
    assert during["us_equity_open"] is True and during["minutes_to_us_close"] == 360
    winter = datetime(2026, 12, 7, 12, tzinfo=UTC)
    assert us_equity_hours(winter)[0] == winter.replace(hour=14, minute=30)


def test_state_summary_is_one_short_paragraph_built_from_the_others(spot, c0, tmp_path):
    at = c0 + timedelta(days=1, hours=2)
    rows = [{"ts": _ms(at.replace(hour=8)), "rate": 0.0001, "realized": 0.0001}]
    box = replay_tools(at, spot, FakeAggTrades([]), ToolCache(tmp_path), futures=FakeFunding(rows))
    out = box["state_summary"].safe_call(symbol="BTCUSDT")
    assert out.ok, out.error
    text = out.data["summary"]
    assert text == out.text and len(text) <= 600
    assert text.startswith("BTCUSDT 1") and "bps/1m" in text
    for part in ("Tape:", "Funding:", "Others:", "Base rate:", "Clock:"):
        assert part in text, part


# --- the box itself ------------------------------------------------------------------

def test_live_tools_add_the_book_and_the_mempool_and_replay_does_not(spot, c0):
    replay = replay_tools(c0, spot)
    assert "order_book" not in replay and "mempool_now" not in replay
    assert len(replay) >= 20
    live = live_tools(c0, spot)
    assert {"order_book", "mempool_now"} <= set(live)
    assert set(replay) <= set(live)


def test_tool_cache_serves_the_second_call(spot, c0, tmp_path):
    at = c0 + timedelta(minutes=20)
    tape = FakeAggTrades([(at - timedelta(minutes=1), 100.0, 1.0, False)])
    replay_tools(at, spot, tape, ToolCache(tmp_path))["recent_trades"].safe_call(symbol="BTCUSDT")
    replay_tools(at, spot, tape, ToolCache(tmp_path))["recent_trades"].safe_call(symbol="BTCUSDT")
    assert tape.calls == 1, "the tape is read once, then the cache answers"
    n = len(spot.calls)
    replay_tools(at, spot, tape, ToolCache(tmp_path))["market_quote"].safe_call(symbol="BTCUSDT")
    reads = len(spot.calls) - n
    assert reads >= 1
    replay_tools(at, spot, tape, ToolCache(tmp_path))["market_quote"].safe_call(symbol="BTCUSDT")
    assert len(spot.calls) == n + reads, "the bars are read once, then the cache answers"
    # A five-minute box at the same instant keys differently from a one-minute one.
    replay_tools(at, spot, tape, ToolCache(tmp_path), horizon=5)["market_quote"].safe_call(symbol="BTCUSDT")
    assert len(spot.calls) == n + 2 * reads


# --- the question set ------------------------------------------------------------------

def test_build_windows_writes_a_day_a_file_and_resumes(spot, c0, tmp_path):
    bench = Benchmark(symbols=("BTCUSDT", "ETHUSDT"), start=c0.date(), end=c0.date() + timedelta(days=1),
                      every=60, horizon=1)
    # The fake's day starts at noon, so the first day has twelve usable hours.
    windows = build_windows(bench, spot, windows_dir=tmp_path, log=lambda m: None)
    files = sorted(p.name for p in tmp_path.glob("*.json"))
    assert files == ["D20260823.every60.h1.json", "D20260824.every60.h1.json"]
    groups = {w.group for w in windows}
    assert groups == {"D20260823", "D20260824"}
    assert all(w.symbol in ("BTCUSDT", "ETHUSDT") for w in windows)
    assert sum(1 for w in windows if w.group == "D20260824") == 2 * 24
    one = next(w for w in windows if w.symbol == "BTCUSDT" and w.group == "D20260824")
    assert METRIC.move(one.mid_now, one.realised) == pytest.approx(1.0, abs=0.02)
    assert one.context["hour_utc"] == one.at.hour and "minutes_to_funding" in one.context
    assert one.at.minute == 5, "the first five minutes of the day are skipped"
    n = len(spot.calls)
    again = build_windows(bench, spot, windows_dir=tmp_path, log=lambda m: None)
    assert len(spot.calls) == n and [w.id for w in again] == [w.id for w in windows]
    assert CryptoWindow.from_dict(one.to_dict()) == one and one.to_dict()["group"] == "D20260824"
    # Thinning caps a day and keeps every day.
    task = CryptoHorizon(benchmark=bench, spot=spot, windows_dir=str(tmp_path), per_fixture=6)
    kept = task.instances()
    assert {w.group for w in kept} == groups
    assert all(sum(1 for w in kept if w.group == g) == 6 for g in groups)
    assert len({w.symbol for w in kept if w.group == "D20260824"}) == 2, "both coins survive the thinning"


# --- the metric in basis points ----------------------------------------------------------

def test_the_bps_metric_scores_silence_ticks_and_a_perfect_call():
    quiet = score_output({"delta_bps": 0}, 100000.0, 100020.0)
    assert quiet.skill == 0.0 and quiet.value == 0.5 and quiet.echoed
    assert silent(100000.0, 100020.0).to_dict() == quiet.to_dict()
    # A one-basis-point move floors to the two-basis-point tick.
    tiny = score_output({"delta_bps": 4}, 100000.0, 100010.0)
    assert tiny.benchmark == pytest.approx(TICK_BPS) and tiny.naive_error == pytest.approx(1.0)
    assert not tiny.moved and tiny.skill == pytest.approx(-1.0), \
        "calling four on a one-basis-point move costs, and the floor bounds the cost"
    # Twenty basis points called on a twenty-basis-point move is a full point.
    perfect = score_output({"delta_bps": 20, "half_width_bps": 5}, 100000.0, 100200.0)
    assert perfect.skill == pytest.approx(1.0) and perfect.value == pytest.approx(1.0)
    assert perfect.covered and perfect.moved
    # The unit is relative: the same move at a different level is the same score.
    assert score_output({"delta_bps": 20}, 200.0, 200.4).skill == pytest.approx(1.0)
    assert METRIC.unit == "bps" and METRIC.tick == TICK_BPS and METRIC.relative
    assert score_output({"delta_cents": 5}, 100.0, 100.1) is None, "the Kalshi key is not this key"


def test_a_failed_run_is_worth_slightly_less_than_silence(c0):
    from rsi_arena.topics.crypto_horizon.task import BREAKAGE
    task = CryptoHorizon(windows=[])
    w = CryptoWindow(symbol="BTCUSDT", at=c0, mid_now=100000.0, realised=100300.0)
    unrunnable = task.failed(w, "no such tool")
    assert unrunnable.value == pytest.approx(0.5 - BREAKAGE)
    assert task.statistic([unrunnable]) == 0.0
    assert "30.0 bps" in unrunnable.feedback and "delta_bps" in unrunnable.feedback
    assert task.moved(w) and not task.moved(CryptoWindow("BTCUSDT", c0, 100000.0, 100010.0))
    assert task.label(w) == "BTCUSDT at 2026-08-23T12:00Z"
    assert task.run_inputs(w) == {"question": "BTCUSDT", "context": "{}"}
    assert "Nothing measured yet" in task.model_notes
    assert "next 1 minute " in task.background and "2 bps" in task.background
    assert "next 5 minutes" in CryptoHorizon(windows=[], horizon=5).background


# --- the harness files and the spec --------------------------------------------------------

@pytest.mark.parametrize("path", ["harnesses/crypto-horizon-1m.json", "harnesses/crypto-horizon-1m-jev.json"])
def test_the_harness_files_fit_the_task(path):
    h = Harness.load(path)
    task = CryptoHorizon(windows=[])
    assert set(h.tools) <= set(task.tools()), sorted(set(h.tools) - set(task.tools()))
    assert h.plan.required_inputs() <= task.inputs, h.plan.required_inputs()
    assert h.from_components(h.to_components()).to_components() == h.to_components()
    last = h.plan.steps[-1]
    if last.questions:
        assert h.config.model == "typesafe/jev-1.13"
        assert last.questions["move"]["values"] == [-15, -6, -2, 0, 2, 6, 15]
        assert last.answers["delta_bps"] == {"from": "move", "as": "mean"}
        answer = {"move": {"type": "score", "score": 6.0,
                           "probabilities": {str(i): (1.0 if i == 6 else 0.0) for i in range(7)},
                           "confidence": 0.9},
                  "action": {"type": "choice", "choice": "open_long", "probabilities": {"open_long": 1.0}},
                  "size": {"type": "score", "score": 1.0,
                           "probabilities": {"0": 0.0, "1": 1.0, "2": 0.0, "3": 0.0}}}
        out = answers_to_output(last.questions, answer, last.answers)
        assert out["delta_bps"] == pytest.approx(15.0) and out["half_width_bps"] >= 1.0
        assert score_output(out, 100000.0, 100150.0).skill == pytest.approx(1.0)
        assert out["action"] == "open_long" and out["size"] == 0.02, "the book's order rides the output"
    else:
        assert set(last.output_schema["required"]) >= {"delta_bps", "half_width_bps", "action", "size"}
        assert set(last.output_schema["required"]) == set(last.output_schema["properties"]), "strict mode"


def test_the_topic_is_registered_and_the_command_prints_it(capsys):
    from rsi_arena.cli import main

    spec = spec_of("crypto-horizon-1m")
    assert spec.harness == spec.jev_harness == "harnesses/crypto-horizon-1m-jev.json"
    assert spec.benchmark == "benchmarks/crypto-2026-09.json" and spec.windows_dir == "benchmarks/windows-crypto"
    assert spec.runs_dir == "runs/crypto-horizon-1m" and spec.per_fixture == 24
    assert spec.window_usd == 0.00005 and spec.model_choices == ("typesafe/jev-1.13", "openai/gpt-5-mini")
    assert (spec.holdout, spec.audit, spec.max_metric_calls, spec.valset) == (30, 15, 4800, 600)
    assert spec.max_day_usd == 100 and spec.unit == "bps"
    assert main(["topic", "--topic", "crypto-horizon-1m", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["name"] == "crypto-horizon-1m" and out["unit"] == "bps" and "factory" not in out
    assert main(["topic", "--topic", "crypto-horizon-1m", "--shell"]) == 0
    assert "HARNESS=harnesses/crypto-horizon-1m-jev.json" in capsys.readouterr().out


def test_load_topic_reads_the_cadence_off_the_benchmark(tmp_path):
    from rsi_arena.loop import Settings
    from rsi_arena.topics import load_topic

    bench = tmp_path / "b.json"
    bench.write_text(json.dumps({"symbols": ["BTCUSDT"], "from": "2026-08-23", "to": "2026-08-24",
                                 "every": 15, "horizon": 3, "data_dir": str(tmp_path / "data")}))
    s = Settings(topic="crypto-horizon-1m", benchmark=str(bench), windows_dir=str(tmp_path / "w"),
                 cache_dir=str(tmp_path / "c"), per_fixture=4)
    task = load_topic(s)
    assert isinstance(task, CryptoHorizon)
    assert task.every_minutes == 15 and task.horizon == 3 and task.per_fixture == 4
    assert task.spot.store.root == tmp_path / "data" / "klines"
    assert "next 3 minutes" in task.background
    assert task.instances() == [], "no bars on disk, no windows, and no network asked"
