import pytest

from rsi_arena.topics.kalshi_horizon import (WindowScore, pooled, pooled_skill,
                                              quote_from, score_output)


def test_quote_from_anchors_on_the_exchange_mid_and_clamps():
    assert quote_from(0.50, 3, 2) == (0.53, 0.51, 0.55)
    assert quote_from(0.99, 50, 0)[0] == 0.99
    assert quote_from(0.02, -50, 0)[0] == 0.01


def test_skill_is_the_fraction_of_no_change_error_removed():
    """Skill is the ratio. Value is the numerator, scaled — the two answer
    different questions and only the second can be averaged."""
    s = score_output({"delta_cents": 5, "half_width_cents": 1}, 0.50, 0.60)
    assert round(s.skill, 3) == 0.5 and not s.echoed and not s.covered
    assert round(s.value, 3) == 0.75, "half a ten-cent move removed is a quarter point"
    worse = score_output({"delta_cents": -5}, 0.50, 0.60)
    assert worse.skill == -0.5
    assert worse.value == pytest.approx(0.25), "the same distance, the wrong way"
    assert score_output({"delta_cents": -30}, 0.50, 0.60).value == 0.0


def test_echo_scores_exactly_silence():
    echo = score_output({"delta_cents": 0}, 0.50, 0.55)
    assert echo.echoed and echo.skill == 0.0 and echo.value == 0.5
    assert WindowScore.silent(0.50, 0.55).to_dict() == echo.to_dict()


def test_a_move_predicted_on_a_dead_market_costs():
    """It used to be free, and that is what broke the first thousand-call run.

    The market went nowhere, so no-change's error was zero, so the per-window
    score called skill undefined and handed back a flat 0.5 whatever had been
    predicted — while the pooled statistic added the error to its numerator and
    nothing to its denominator. GEPA found the gap: told by its own reflection
    that it was "rewarded only for anticipating moves", it started predicting
    movement on 27 of 29 dead markets, gaining 0.020 on the objective it could
    see while losing 0.044 on the one it was judged by.
    """
    flat = score_output({"delta_cents": 2}, 0.50, 0.50)
    assert flat.unmeasurable, "the market really did not move"
    assert flat.skill < 0 and flat.value < 0.5, "and saying it did is wrong"


def test_silence_scores_exactly_zero_on_every_window():
    """The property the whole metric is defined around, and the one a first
    attempt at the floor destroyed.

    Written as ``1 - error/benchmark`` the floor reached the numerator too, so
    silence on a market that did not move scored ``(0.01 - 0)/0.01`` — a full
    point for having no opinion. Two models that echoed the mid on all
    sixty-eight held-out windows then posted the best skill in a model
    comparison, which is how it was caught.

    Silence removes none of the benchmark's error, so it is worth zero. Always,
    whatever the market did.
    """
    for mid, realised in ((0.50, 0.50), (0.50, 0.57), (0.50, 0.5001), (0.30, 0.10)):
        quiet = score_output({"delta_cents": 0}, mid, realised)
        assert quiet.skill == 0.0, (mid, realised)
        assert quiet.value == 0.5, "and half a point, which is the benchmark"


def test_a_sub_tick_move_is_no_move():
    """The exchange cannot express half a cent, so a benchmark error smaller
    than a tick is not a benchmark. Flooring there is what bounds the damage a
    single quiet window can do to a pooled number."""
    a = score_output({"delta_cents": 1}, 0.50, 0.5001)
    b = score_output({"delta_cents": 1}, 0.50, 0.50)
    assert a.benchmark == b.benchmark == 0.01
    real = score_output({"delta_cents": 1}, 0.50, 0.57)
    assert real.benchmark == pytest.approx(0.07), "a real move is its own benchmark"


def test_unusable_output_scores_nothing():
    assert score_output("text", 0.5, 0.6) is None
    assert score_output({"delta_cents": "5"}, 0.5, 0.6) is None
    assert score_output({"delta_cents": True}, 0.5, 0.6) is None


def test_pooled_sums_before_dividing_so_a_quiet_window_cannot_dominate():
    """Per-window ratios would let a market that moved a tenth of a cent swing
    the whole number. Summing first keeps a big move worth more than a small one
    — and the floor keeps a wrong call on the small one from being free."""
    a = score_output({"delta_cents": 10}, 0.50, 0.60)      # perfect on a big move
    b = score_output({"delta_cents": 1}, 0.50, 0.501)      # wrong on a tiny one
    p = pooled([a, b])
    assert p["moved"] == 1
    assert 0.8 < p["skill"] < 0.9, "the big move dominates, but the small miss is not free"
    assert b.skill < 0, "a cent of movement predicted where a tenth happened"


def test_the_value_is_affine_in_what_the_pooled_statistic_sums():
    """The invariant as arithmetic rather than as a hope.

    The pooled statistic is ``sum(removed) / sum(benchmark)``, and a window's
    benchmark depends on the window rather than on the harness — so over a fixed
    set of windows the denominator is a constant and the mean of ``removed`` is
    monotone in pooled skill by construction.

    Averaging *skill* is not, and that is not a subtlety: skill divides by a
    per-window benchmark, so a tenth-of-a-cent window and a ten-cent window
    carry equal weight in a mean and wildly different weight in the sum. It
    disagreed with the gate on real held-out data even once the metric itself
    was right.
    """
    windows = [score_output({"delta_cents": d}, 0.50, r)
               for d, r in ((0, 0.57), (5, 0.57), (3, 0.50), (0, 0.50), (-2, 0.48))]
    for w in windows:
        assert w.value == pytest.approx(0.5 + w.removed / 0.2, abs=1e-9) or w.value in (0.0, 1.0)


def test_a_thousand_random_harnesses_never_split_the_two():
    """The synthetic version of the failure that took two goes to find: the mean
    rising while the pooled number falls."""
    import random

    rnd = random.Random(0)
    market = [(0.50, round(0.50 + rnd.choice([0, 0, 0.001, 0.02, -0.05, 0.07]), 4))
              for _ in range(40)]
    ref = [score_output({"delta_cents": 0}, m, r) for m, r in market]

    for _ in range(200):
        trial = [score_output({"delta_cents": rnd.choice([-8, -3, 0, 0, 1, 4, 9])}, m, r)
                 for m, r in market]
        d_mean = (sum(t.value for t in trial) - sum(r.value for r in ref)) / len(trial)
        d_pool = pooled_skill(trial) - pooled_skill(ref)
        if abs(d_mean) > 1e-9 and abs(d_pool) > 1e-9:
            assert d_mean * d_pool > 0, f"mean {d_mean:+.5f} against pooled {d_pool:+.5f}"


def test_the_optimizer_and_the_gate_cannot_disagree_in_direction():
    """The invariant the first thousand-call run broke.

    GEPA selects on the mean of per-window values; the gate promotes on pooled
    skill. If a change can raise one and lower the other, the optimizer climbs a
    hill the gate does not measure and no result from the loop means anything.
    Reconstructed here from the shape that actually occurred: a candidate that
    is better on the windows that moved and louder on the ones that did not.
    """
    moved = [(0.50, 0.57), (0.40, 0.46), (0.60, 0.52)]      # (mid, realised)
    quiet = [(0.50, 0.50), (0.30, 0.30), (0.70, 0.70), (0.45, 0.45)]

    def harness(on_moves: int, on_quiet: int):
        return ([score_output({"delta_cents": on_moves}, m, r) for m, r in moved]
                + [score_output({"delta_cents": on_quiet}, m, r) for m, r in quiet])

    echoes = harness(on_moves=0, on_quiet=0)       # says nothing, anywhere
    shouts = harness(on_moves=5, on_quiet=5)       # right-ish on movers, loud on the dead

    def mean(ss): return sum(s.value for s in ss) / len(ss)

    d_mean = mean(shouts) - mean(echoes)
    d_pool = pooled_skill(shouts) - pooled_skill(echoes)
    assert d_mean * d_pool > 0, (
        f"mean moved {d_mean:+.4f} and pooled moved {d_pool:+.4f} — opposite "
        "directions means the loop is optimising something it is not judged on")


def test_thinning_keeps_matches_and_spreads_across_each():
    """Power comes from matches — the gate resamples by match — so thirty-four
    windows of one game are thirty-four correlated observations bought at
    thirty-four times the price of eight. Thinning drops windows, never matches,
    and spreads what it keeps: the first eight of a football match are all the
    opening twenty minutes, before the scoreline has done anything a forecast
    could be wrong about.
    """
    from rsi_arena.topics.kalshi_horizon.task import _thin

    class W:
        def __init__(self, group, at):
            self.group, self.at, self.ticker = group, at, "t"

    windows = [W(g, f"2026-09-05T{12 + i // 60:02d}:{i % 60:02d}:00Z")
               for g in ("a", "b") for i in range(34)]
    thinned = _thin(windows, 8)

    assert len({w.group for w in thinned}) == 2, "no match is dropped"
    assert all(sum(1 for w in thinned if w.group == g) == 8 for g in "ab")
    kept = [w.at for w in thinned if w.group == "a"]
    assert kept[-1] > windows[20].at, "the tail of the match is represented"
    assert _thin(windows, 0) == windows, "zero keeps everything"


# -- the optimizer and the gate read a failure the same way -------------------

def test_a_failed_run_is_worth_exactly_silence():
    """Not zero.

    The optimizer used to be handed 0.0 for a run that produced nothing — the
    floor of the scale, what a harness gets for being ten cents wrong — while
    the gate read the same window as exactly silence. Half the range of
    disagreement about the most common failure there is.
    """
    from rsi_arena.topics.kalshi_horizon.task import KalshiHorizon
    from rsi_arena.topics.kalshi_horizon.windows import Window
    from datetime import datetime, timezone

    task = KalshiHorizon(windows=[])
    w = Window(ticker="T", at=datetime(2026, 9, 1, tzinfo=timezone.utc),
               mid_now=0.40, realised=0.46, event="E")
    silent = WindowScore.silent(w.mid_now, w.realised)

    unrunnable = task.failed(w, "no such tool")
    assert unrunnable.value == pytest.approx(silent.value)
    assert unrunnable.objectives["skill"] == pytest.approx(silent.value)

    class _NoOutput:
        output = None
        cost_usd = 0.0
        ok = False
        error = "provider"
        error_kind = "provider"

        def tools_seen(self):
            return []

    quiet = task.score(w, _NoOutput())
    assert quiet.value == pytest.approx(unrunnable.value), \
        "a run that failed and a run that said nothing are the same result"


def test_silence_is_the_middle_of_the_optimizer_scale():
    """0.5, because value is affine in error removed and silence removes none."""
    assert WindowScore.silent(0.40, 0.46).value == pytest.approx(0.5)
    assert WindowScore.silent(0.40, 0.40).value == pytest.approx(0.5)
