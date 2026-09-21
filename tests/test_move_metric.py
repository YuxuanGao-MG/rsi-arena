"""``Metric.KALSHI`` is ``kalshi_horizon/score.py``, to the last bit.

Every numeric case ``test_topic_score.py`` puts through ``WindowScore`` goes
through ``MoveScore`` with the Kalshi metric here, and the two are held equal
to 1e-12 on every property. The Kalshi topic keeps scoring through its own
file; this is what lets the next topic score through the shared one and claim
the same argument for its metric.
"""

from __future__ import annotations

import random

import pytest

from rsi_arena.topics._common.metric import Metric, MoveScore, pooled, pooled_skill, score_output
from rsi_arena.topics.kalshi_horizon import score as kalshi

KALSHI = Metric.KALSHI
PROPS = ("error", "naive_error", "benchmark", "skill", "removed", "value")
FLAGS = ("echoed", "covered", "unmeasurable")


def same(mine: MoveScore | None, theirs: kalshi.WindowScore | None) -> None:
    if theirs is None or mine is None:
        assert mine is None and theirs is None
        return
    for name in ("mid_now", "predicted", "realised", "half_width"):
        assert getattr(mine, name) == pytest.approx(getattr(theirs, name), abs=1e-12), name
    for name in PROPS:
        assert getattr(mine, name) == pytest.approx(getattr(theirs, name), abs=1e-12), name
    for name in FLAGS:
        assert getattr(mine, name) is getattr(theirs, name), name
    ours, ref = mine.to_dict(), theirs.to_dict()
    assert ours.pop("unit") == "cents"
    assert ours == ref


def both(output, mid, realised):
    return (score_output(output, mid, realised, KALSHI),
            kalshi.score_output(output, mid, realised))


# -- every case test_topic_score.py states -----------------------------------

def test_the_quote_anchors_and_clamps_the_same_way():
    for mid, delta, width in ((0.50, 3, 2), (0.99, 50, 0), (0.02, -50, 0)):
        mine, theirs = both({"delta_cents": delta, "half_width_cents": width}, mid, 0.5)
        predicted, low, high = kalshi.quote_from(mid, delta, width)
        assert mine.predicted == pytest.approx(predicted, abs=1e-12)
        assert mine.half_width == pytest.approx((high - low) / 2, abs=1e-12)
        same(mine, theirs)


def test_skill_value_and_the_wrong_way_round():
    same(*both({"delta_cents": 5, "half_width_cents": 1}, 0.50, 0.60))
    same(*both({"delta_cents": -5}, 0.50, 0.60))
    same(*both({"delta_cents": -30}, 0.50, 0.60))
    assert score_output({"delta_cents": -30}, 0.50, 0.60, KALSHI).value == 0.0


def test_echo_and_silence_agree():
    same(*both({"delta_cents": 0}, 0.50, 0.55))
    quiet = MoveScore.silent(0.50, 0.55, KALSHI)
    same(quiet, kalshi.WindowScore.silent(0.50, 0.55))
    assert quiet.to_dict() == {**kalshi.WindowScore.silent(0.50, 0.55).to_dict(), "unit": "cents"}


def test_a_move_on_a_dead_market_costs_the_same():
    mine, theirs = both({"delta_cents": 2}, 0.50, 0.50)
    same(mine, theirs)
    assert mine.unmeasurable and mine.skill < 0 and mine.value < 0.5


def test_silence_is_zero_on_every_window():
    for mid, realised in ((0.50, 0.50), (0.50, 0.57), (0.50, 0.5001), (0.30, 0.10)):
        mine, theirs = both({"delta_cents": 0}, mid, realised)
        same(mine, theirs)
        assert mine.skill == 0.0 and mine.value == 0.5


def test_a_sub_tick_move_floors_the_same():
    for realised in (0.5001, 0.50, 0.57):
        same(*both({"delta_cents": 1}, 0.50, realised))
    assert score_output({"delta_cents": 1}, 0.50, 0.5001, KALSHI).benchmark == 0.01


def test_unusable_output_is_none_for_both():
    for output in ("text", {"delta_cents": "5"}, {"delta_cents": True}, None, {}):
        same(*both(output, 0.5, 0.6))


def test_pooled_reports_the_same_numbers():
    cases = [({"delta_cents": 10}, 0.50, 0.60), ({"delta_cents": 1}, 0.50, 0.501)]
    mine = [score_output(o, m, r, KALSHI) for o, m, r in cases]
    theirs = [kalshi.score_output(o, m, r) for o, m, r in cases]
    assert pooled_skill(mine) == pytest.approx(pooled_skill(theirs), abs=1e-12)
    assert pooled(mine) == kalshi.pooled(theirs)
    assert pooled([]) == kalshi.pooled([])


def test_value_is_affine_in_removed_for_both():
    cases = ((0, 0.57), (5, 0.57), (3, 0.50), (0, 0.50), (-2, 0.48))
    for d, r in cases:
        mine, theirs = both({"delta_cents": d}, 0.50, r)
        same(mine, theirs)
        assert mine.value == pytest.approx(0.5 + mine.removed / 0.2, abs=1e-9) or mine.value in (0.0, 1.0)


def test_a_thousand_random_harnesses_score_identically():
    """The randomised monotonicity property, and bit-equality along the way."""
    rnd = random.Random(0)
    market = [(0.50, round(0.50 + rnd.choice([0, 0, 0.001, 0.02, -0.05, 0.07]), 4))
              for _ in range(40)]
    ref_mine = [score_output({"delta_cents": 0}, m, r, KALSHI) for m, r in market]
    ref_theirs = [kalshi.score_output({"delta_cents": 0}, m, r) for m, r in market]
    for a, b in zip(ref_mine, ref_theirs):
        same(a, b)

    for _ in range(200):
        deltas = [rnd.choice([-8, -3, 0, 0, 1, 4, 9]) for _ in market]
        mine = [score_output({"delta_cents": d}, m, r, KALSHI) for d, (m, r) in zip(deltas, market)]
        theirs = [kalshi.score_output({"delta_cents": d}, m, r) for d, (m, r) in zip(deltas, market)]
        for a, b in zip(mine, theirs):
            same(a, b)
        assert pooled_skill(mine) == pytest.approx(pooled_skill(theirs), abs=1e-12)
        d_mean = (sum(t.value for t in mine) - sum(r.value for r in ref_mine)) / len(mine)
        d_pool = pooled_skill(mine) - pooled_skill(ref_mine)
        if abs(d_mean) > 1e-9 and abs(d_pool) > 1e-9:
            assert d_mean * d_pool > 0, f"mean {d_mean:+.5f} against pooled {d_pool:+.5f}"


def test_the_optimizer_and_the_gate_cannot_disagree_in_direction():
    moved = [(0.50, 0.57), (0.40, 0.46), (0.60, 0.52)]
    quiet = [(0.50, 0.50), (0.30, 0.30), (0.70, 0.70), (0.45, 0.45)]

    def harness(on_moves: int, on_quiet: int):
        return ([score_output({"delta_cents": on_moves}, m, r, KALSHI) for m, r in moved]
                + [score_output({"delta_cents": on_quiet}, m, r, KALSHI) for m, r in quiet])

    echoes, shouts = harness(0, 0), harness(5, 5)

    def mean(ss):
        return sum(s.value for s in ss) / len(ss)

    d_mean = mean(shouts) - mean(echoes)
    d_pool = pooled_skill(shouts) - pooled_skill(echoes)
    assert d_mean * d_pool > 0


def test_silence_is_the_middle_of_the_scale():
    assert MoveScore.silent(0.40, 0.46, KALSHI).value == pytest.approx(0.5)
    assert MoveScore.silent(0.40, 0.40, KALSHI).value == pytest.approx(0.5)


# -- what a relative metric adds -----------------------------------------------

BPS = Metric(tick=1.0, scale=50.0, unit="bps", relative=True, clamp=None,
             output_keys=("delta_bps", "half_width_bps"))


def test_a_relative_metric_measures_in_basis_points_of_the_mid():
    """A dollar at $8 and a dollar at $800 are not the same move."""
    s = score_output({"delta_bps": 10, "half_width_bps": 5}, 200.0, 200.2, BPS)
    assert s.predicted == pytest.approx(200.2)
    assert s.naive_error == pytest.approx(10.0) and s.error == pytest.approx(0.0, abs=1e-9)
    assert s.skill == pytest.approx(1.0) and s.covered and not s.echoed
    assert s.value == pytest.approx(0.5 + 10 / 100)
    assert s.to_dict()["unit"] == "bps"
    assert BPS.move(200.0, 200.2) == pytest.approx(10.0)
    assert BPS.moved(200.0, 200.02) and not BPS.moved(200.0, 200.01)


def test_a_relative_metric_keeps_the_three_properties():
    silent = MoveScore.silent(100.0, 100.5, BPS)
    assert silent.skill == 0.0 and silent.value == 0.5
    dead = score_output({"delta_bps": 4}, 100.0, 100.0, BPS)
    assert dead.unmeasurable and dead.skill < 0 and dead.value < 0.5
    assert score_output({"delta_bps": 1}, 0.0, 1.0, BPS) is None, "no return on a price of zero"
    assert score_output({"delta_cents": 1}, 100.0, 101.0, BPS) is None, "the wrong keys are no forecast"
