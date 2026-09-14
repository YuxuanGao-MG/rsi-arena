import pytest

from rsi_arena.topics.kalshi_horizon import (WindowScore, pooled, pooled_skill,
                                              quote_from, score_output)


def test_quote_from_anchors_on_the_exchange_mid_and_clamps():
    assert quote_from(0.50, 3, 2) == (0.53, 0.51, 0.55)
    assert quote_from(0.99, 50, 0)[0] == 0.99
    assert quote_from(0.02, -50, 0)[0] == 0.01


def test_skill_is_the_fraction_of_no_change_error_removed():
    s = score_output({"delta_cents": 5, "half_width_cents": 1}, 0.50, 0.60)
    assert round(s.skill, 3) == 0.5 and not s.echoed and not s.covered and round(s.value, 3) == 0.75
    worse = score_output({"delta_cents": -5}, 0.50, 0.60)
    assert worse.skill == -0.5 and worse.value == 0.25
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


def test_saying_nothing_on_a_dead_market_is_right_and_scores_so():
    """The other half of the same distinction. Predicting no change on a market
    that did not change is a correct forecast, not an absent one."""
    quiet = score_output({"delta_cents": 0}, 0.50, 0.50)
    assert quiet.unmeasurable and quiet.skill == 1.0 and quiet.value == 1.0


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


def test_pooled_sums_errors_before_dividing():
    a = score_output({"delta_cents": 10}, 0.50, 0.60)      # perfect on a big move
    b = score_output({"delta_cents": 1}, 0.50, 0.501)      # bad on a tiny one
    p = pooled([a, b])
    assert p["moved"] == 1 and p["skill"] > 0.9


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
