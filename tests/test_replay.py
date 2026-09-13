from datetime import timedelta

from rsi_arena.topics.kalshi_horizon import Fixture, build_windows
from rsi_arena.kalshi.replay import MatchEvent, MatchTimeline, ToolCache, realised_mid, replay_tools


def test_frozen_tools_never_see_past_the_instant(history, t0):
    at = t0 + timedelta(minutes=20)
    box = replay_tools(at, history)
    assert set(box) == {"market_quote", "candlesticks", "previous_trades"}
    quote = box["market_quote"].safe_call(ticker="A")
    assert quote.ok and abs(quote.data["mid"] - 0.60) < 1e-9
    path = box["candlesticks"].safe_call(ticker="A", hours_back=0.25)
    assert path.ok and all(b["ts"] <= at.isoformat() for b in path.data["bars"])
    assert len(path.data["bars"]) == 16
    tape = box["previous_trades"].safe_call(ticker="A", limit=5)
    assert tape.ok and history.trade_calls[-1]["end"] == at


def test_tool_cache_serves_the_second_call(history, t0, tmp_path):
    cache = ToolCache(tmp_path)
    at = t0 + timedelta(minutes=10)
    replay_tools(at, history, cache)["previous_trades"].safe_call(ticker="A")
    replay_tools(at, history, cache)["previous_trades"].safe_call(ticker="A")
    assert len(history.trade_calls) == 1


def test_realised_mid_needs_a_two_sided_book(history, t0):
    assert abs(realised_mid("A", t0 + timedelta(minutes=10), 5, history) - 0.55) < 1e-9
    history.dead.add(("A", 15))
    assert realised_mid("A", t0 + timedelta(minutes=10), 5, history) is None


def test_timeline_replays_the_score_without_the_final(t0):
    line = MatchTimeline(game_id="g", league="EPL", home="H", away="A", kickoff=t0, events=[
        MatchEvent(seconds=600, kind="goal", team="H", text="1-0"),
        MatchEvent(seconds=3000, kind="penalty---scored", team="A", text="1-1"),
    ])
    assert line.score_at(t0 + timedelta(minutes=15)) == (1, 0, 15)
    assert line.final_score() == (1, 1)
    state = line.state_at(t0 + timedelta(minutes=12))
    assert state["home_score"] == 1 and state["recent_events"] and state["period"] == "1"
    assert line.windows(every_minutes=5)[0] == t0 + timedelta(minutes=5)
    assert MatchTimeline.from_dict(line.to_dict()) == line


def test_build_windows_attaches_truth_and_caches(history, t0, tmp_path):
    line = MatchTimeline(game_id="g", league="EPL", home="H", away="A", kickoff=t0)
    fixture = Fixture(league="EPL", game="g", event="E", tickers=("A", "B"))
    calls = []
    def fake_timeline(league, game):
        calls.append(game)
        return line
    windows = build_windows([fixture], history=history, windows_dir=tmp_path,
                            timeline_for=fake_timeline, log=lambda m: None)
    # Candles exist for minutes 0..39. A window needs a fresh candle at the instant
    # and one within the staleness limit five minutes on: 5,10,...,35 qualify (the
    # minute-35 window reads the minute-39 candle, 60s stale), seven per ticker.
    assert len(windows) == 14 and calls == ["g"]
    a = [w for w in windows if w.ticker == "A"][0]
    assert abs(a.realised - (a.mid_now + 0.05)) < 1e-9 and a.game["home"] == "H"
    again = build_windows([fixture], history=history, windows_dir=tmp_path,
                          timeline_for=fake_timeline, log=lambda m: None)
    assert calls == ["g"] and [w.id for w in again] == [w.id for w in windows]
