from datetime import timedelta

import pytest

from rsi_arena.topics.kalshi_horizon import Fixture, build_windows
from rsi_arena.kalshi.replay import MatchEvent, MatchTimeline, ToolCache, realised_mid, replay_tools


def test_frozen_tools_never_see_past_the_instant(history, t0):
    at = t0 + timedelta(minutes=20)
    box = replay_tools(at, history)
    # Not an exact set: the box grows, and pinning its size turns widening the
    # search into a test failure. What must stay true is that nothing in it can
    # see past the instant — so the named absentees are the assertion.
    assert {"market_quote", "candlesticks", "previous_trades",
            "move_base_rate", "tape_imbalance", "state_summary"} <= set(box)
    for reaches_forward in ("game_state", "recent_plays", "team_news", "web_research",
                            "live_markets", "todays_fixtures", "market_settlement"):
        assert reaches_forward not in box, f"{reaches_forward} would answer with the future"
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


# --- the box a harness gets to compose from ----------------------------------


def test_game_state_needs_a_timeline_and_says_so(t0, history):
    """Score and clock are as replayable as the book — a timeline is timestamped
    events, so what the score was at an instant is a lookup. Without one those
    tools are absent rather than wrong, so a harness that names them fails to
    load instead of quietly getting a guess."""
    without = replay_tools(t0, history)
    assert "game_state" not in without and "minutes_since_goal" not in without

    line = MatchTimeline(game_id="g", league="EPL", home="H", away="A",
                         kickoff=t0 - timedelta(minutes=30),
                         events=[MatchEvent(seconds=600, kind="goal", team="H", text="1-0")])
    withline = replay_tools(t0, history, line=line)
    assert {"game_state", "minutes_since_goal", "recent_plays"} <= set(withline)

    state = withline["game_state"].safe_call()
    assert state.ok and state.data["home_score"] == 1

    quiet = withline["minutes_since_goal"].safe_call()
    assert quiet.ok and quiet.data["last_goal_minute"] == 10
    assert quiet.data["minutes_since"] == 20, "thirty minutes in, a goal on ten"


def test_a_later_event_is_not_visible_yet(t0, history):
    """The timeline holds the whole match including its goals. Standing at
    minute thirty, the ones at minute seventy have not happened."""
    line = MatchTimeline(game_id="g", league="EPL", home="H", away="A",
                         kickoff=t0 - timedelta(minutes=30),
                         events=[MatchEvent(seconds=600, kind="goal", team="H", text="early"),
                                 MatchEvent(seconds=4200, kind="goal", team="A", text="late")])
    box = replay_tools(t0, history, line=line)
    plays = box["recent_plays"].safe_call(limit=10)
    assert plays.ok
    texts = " ".join(e["text"] for e in plays.data["events"])
    assert "early" in texts and "late" not in texts


def test_the_box_has_more_than_three_tools(t0, history):
    """A search over three tools is barely a search. The arena's premise is that
    a harness composes primitives, and it had almost nothing to compose: GEPA
    could rewrite the prompt and the plan, and any tool name it reached for
    outside the box made the candidate fail to load."""
    box = replay_tools(t0, history)
    assert len(box) >= 14, sorted(box)
    assert {"market_quote", "candlesticks", "previous_trades"} <= set(box)


def test_arithmetic_tools_need_no_clock(t0, history):
    """Fees, de-vigging and sizing have no time in them, so freezing them is a
    matter of definition rather than care. They are the cheapest way to widen
    what a harness can compose."""
    box = replay_tools(t0, history)
    fees = box["trading_fees"].safe_call(price=0.35, contracts=100)
    assert fees.ok and fees.data["round_trip_usd"] > 0
    later = replay_tools(t0 + timedelta(hours=2), history)
    assert (fees.data["taker_fee_usd"]
            == later["trading_fees"].safe_call(price=0.35, contracts=100).data["taker_fee_usd"]), \
        "the same answer at any instant, because there is no instant in it"

    edge = box["price_the_edge"].safe_call(probability=0.6, yes_price=0.5)
    assert edge.ok and edge.data["worth_taking"] is True
    none_left = box["price_the_edge"].safe_call(probability=0.50, yes_price=0.50)
    assert none_left.ok and none_left.data["worth_taking"] is False


def test_a_bad_devig_method_is_a_sentence_not_a_traceback(t0, history):
    """The author of this file guessed 'multiplicative' and it is not one of the
    names. A model guessing will do the same, and should read which names exist."""
    out = replay_tools(t0, history)["devig_odds"].safe_call(
        american_odds=[-150, 320], method="multiplicative")
    assert not out.ok and "proportional" in out.error


def test_asking_for_a_time_past_the_window_gets_the_window(t0, history):
    """The one tool that takes a timestamp is the one place a harness could
    reach forward by asking. It is clamped rather than refused — a plan that asks
    for a later time gets this instant and is told so — because refusing would
    teach a rewriter to avoid the tool rather than to use it properly."""
    box = replay_tools(t0, history)
    ahead = box["market_at_time"].safe_call(ticker="A",
                                     when=(t0 + timedelta(hours=3)).isoformat())
    if ahead.ok:
        assert ahead.data["clamped"] is True
        assert ahead.data["answered_at"] <= t0.isoformat()

    behind = box["market_at_time"].safe_call(ticker="A",
                                      when=(t0 - timedelta(minutes=20)).isoformat())
    if behind.ok:
        assert behind.data["clamped"] is False


def test_a_match_that_has_not_kicked_off_is_not_in_progress(t0):
    """score_at clamps the minute at zero, and that read as a kickoff.

    A harness asked about Brentford against Chelsea two days early was told
    "in_progress, period 1, clock 0'" and wrote "Kickoff just happened, 0-0"
    into its driver. The replay benchmark could not see it — `windows` starts
    five minutes after kickoff — and live collection sees little else, because
    most open markets are pre-match.
    """
    from datetime import timedelta
    from rsi_arena.kalshi.replay import MatchTimeline

    line = MatchTimeline(game_id="g", league="EPL", home="Brentford", away="Chelsea",
                         kickoff=t0, events=[])

    before = line.state_at(t0 - timedelta(days=2))
    assert before["status"] == "scheduled"
    assert before["minutes_to_kickoff"] == 2 * 24 * 60
    # Absent, not zero. A key that says 0' is read as a fact.
    assert "clock" not in before and "period" not in before
    assert before["home_score"] is None and before["away_score"] is None

    during = line.state_at(t0 + timedelta(minutes=10))
    assert during["status"] == "in_progress" and during["clock"] == "10'"


# --- derived tools: the arithmetic a decisions model cannot do ---------------


class _TapedHistory:
    """A history whose tape has prints on both sides of the instant and honours
    ``end`` the way the API's max_ts does."""

    def __init__(self, base, prints):
        self.base, self.prints = base, prints

    def __getattr__(self, name):
        return getattr(self.base, name)

    def trades(self, ticker, start=None, end=None, max_trades=None):
        return [{"created_time": t["ts"].isoformat(), "yes_price_dollars": "0.50",
                 "count_fp": str(t["n"]), "taker_side": t["side"]}
                for t in self.prints
                if (end is None or t["ts"] <= end) and (start is None or t["ts"] >= start)]


def _line(at, minutes_in=30, goals=(10,)):
    return MatchTimeline(game_id="g", league="EPL", home="H", away="A",
                         kickoff=at - timedelta(minutes=minutes_in),
                         events=[MatchEvent(seconds=60 * m, kind="goal", team="H", text=f"{m}'")
                                 for m in goals])


def test_the_derived_tools_are_in_the_box_and_the_timeline_ones_only_with_a_line(t0, history):
    without = replay_tools(t0, history)
    assert {"move_base_rate", "tape_imbalance", "state_summary"} <= set(without)
    assert "game_clock" not in without and "goal_absorption" not in without
    assert len(without) >= 14
    assert {"game_clock", "goal_absorption"} <= set(replay_tools(t0, history, line=_line(t0)))


def test_game_clock_does_the_date_maths(t0, history):
    late = replay_tools(t0, history, line=_line(t0, minutes_in=87, goals=(10, 70)))
    out = late["game_clock"].safe_call()
    assert out.ok
    assert out.data["minutes_played"] == 87 and out.data["minutes_left_to_90"] == 3
    assert out.data["period"] == "2" and out.data["minutes_since_score_change"] == 17
    assert out.data["stoppage_near"] is True and "stoppage near" in out.data["verdict"]
    # Standing at minute thirty, the goal on seventy has not happened.
    early = replay_tools(t0, history, line=_line(t0, minutes_in=30, goals=(10, 70)))
    out = early["game_clock"].safe_call()
    assert out.data["minutes_since_score_change"] == 20 and out.data["stoppage_near"] is False
    none = replay_tools(t0, history, line=_line(t0, minutes_in=30, goals=()))["game_clock"].safe_call()
    assert none.data["minutes_since_score_change"] is None and "no goal" in none.data["verdict"]


def test_move_base_rate_reads_only_bars_before_the_instant(t0, history):
    """Contract A drifts a cent a minute for forty minutes, so every five-minute
    window moves five cents. Standing at minute twenty, the bars after it are
    the future and must not be in the sample."""
    at = t0 + timedelta(minutes=20)
    out = replay_tools(at, history)["move_base_rate"].safe_call(ticker="A")
    assert out.ok, out.error
    assert out.data["bucket"] == "30-70c", "the mid at minute twenty is 60c"
    # Bars at minutes 0..15 can each see a bar five minutes on, at or before
    # minute twenty; from minute 16 on they cannot, and nothing later exists.
    assert out.data["samples"] == 16
    assert out.data["mean_cents"] == pytest.approx(5.0) and out.data["p50"] == pytest.approx(5.0)
    assert out.data["moved_share"] == 1.0 and out.data["verdict"].startswith("active")
    # Nine minutes on, at 69c, nine more starting bars have a bar five on.
    later = replay_tools(t0 + timedelta(minutes=29), history)["move_base_rate"].safe_call(ticker="A")
    assert later.data["samples"] == 25 and later.data["p90"] == pytest.approx(5.0)
    # At 70c the price is in the next bucket, where this contract has no history yet.
    fresh = replay_tools(t0 + timedelta(minutes=30), history)["move_base_rate"].safe_call(ticker="A")
    assert fresh.ok and fresh.data["bucket"] == "70-90c" and fresh.data["samples"] == 0
    assert fresh.data["p50"] is None and "no five-minute history" in fresh.data["verdict"]
    flat = replay_tools(at, history)["move_base_rate"].safe_call(ticker="B")
    assert flat.data["moved_share"] == 0.0 and flat.data["verdict"].startswith("quiet")


def test_tape_imbalance_ignores_prints_after_the_instant(t0, history):
    at = t0 + timedelta(minutes=20)
    before = [{"ts": at - timedelta(minutes=3), "n": 30, "side": "yes"},
              {"ts": at - timedelta(minutes=1), "n": 10, "side": "no"}]
    after = [{"ts": at + timedelta(minutes=1), "n": 500, "side": "no"}]
    clean = replay_tools(at, _TapedHistory(history, before))["tape_imbalance"].safe_call(ticker="A")
    leaky = replay_tools(at, _TapedHistory(history, before + after))["tape_imbalance"].safe_call(ticker="A")
    assert clean.ok and clean.data == leaky.data, "a print from the future changes nothing"
    assert clean.data["prints"] == 2 and clean.data["yes_taken"] == 30 and clean.data["no_taken"] == 10
    assert clean.data["imbalance"] == pytest.approx(0.5) and clean.data["largest_print"] == 30
    assert clean.data["verdict"].startswith("buyers")
    # Older than minutes_back is out too, and an empty tape is balanced, not an error.
    stale = [{"ts": at - timedelta(minutes=30), "n": 99, "side": "no"}]
    quiet = replay_tools(at, _TapedHistory(history, stale))["tape_imbalance"].safe_call(
        ticker="A", minutes_back=10)
    assert quiet.ok and quiet.data["prints"] == 0 and quiet.data["verdict"].startswith("balanced")


def test_goal_absorption_reads_the_mid_at_the_goal_and_now(t0, history):
    """Thirty minutes in with a goal on ten, contract A has drifted twenty cents
    since, and is still drifting; contract B has not moved."""
    at = t0 + timedelta(minutes=30)
    out = replay_tools(at, history, line=_line(at, goals=(10,)))["goal_absorption"].safe_call(ticker="A")
    assert out.ok, out.error
    assert out.data["goal_minute"] == 10 and out.data["minutes_since"] == 20
    assert out.data["mid_at_goal"] == pytest.approx(0.50) and out.data["mid_now"] == pytest.approx(0.70)
    assert out.data["move_since_cents"] == pytest.approx(20.0)
    assert out.data["still_drifting"] is True and "still drifting" in out.data["verdict"]
    flat = replay_tools(at, history, line=_line(at))["goal_absorption"].safe_call(ticker="B")
    assert flat.data["move_since_cents"] == 0.0 and flat.data["still_drifting"] is False
    none = replay_tools(at, history, line=_line(at, goals=()))["goal_absorption"].safe_call(ticker="A")
    assert none.ok and none.data == {"ticker": "A", "note": "no goal yet"}


def test_state_summary_is_one_short_paragraph_built_from_the_others(t0, history, tmp_path):
    at = t0 + timedelta(minutes=30)
    box = replay_tools(at, history, ToolCache(tmp_path), line=_line(at))
    out = box["state_summary"].safe_call(ticker="A")
    assert out.ok, out.error
    text = out.data["summary"]
    assert text == out.text and len(text) <= 600
    assert "mid 70c" in text, "it names the mid"
    assert "Tape:" in text and "Clock:" in text and "Goal:" in text and "Base rate:" in text
    assert out.data["mid"] == pytest.approx(0.70)
    bare = replay_tools(at, history)["state_summary"].safe_call(ticker="A")
    assert bare.ok and "Clock:" not in bare.text and "mid 70c" in bare.text
    # It goes through the other tools' cache, so with a cache on disk a later
    # call of the tool it composed reads nothing again.
    n = len(history.trade_calls)
    replay_tools(at, history, ToolCache(tmp_path), line=_line(at))["tape_imbalance"].safe_call(ticker="A")
    assert len(history.trade_calls) == n
