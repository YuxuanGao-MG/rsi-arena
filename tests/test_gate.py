import random

from rsi_arena.bench.score import WindowScore
from rsi_arena.optimize import accept, paired_bootstrap


def scores(offsets: list[float], seed: int = 0) -> list[WindowScore]:
    rng = random.Random(seed)
    out = []
    for i, off in enumerate(offsets):
        mid, realised = 0.5, 0.5 + rng.uniform(-0.05, 0.05)
        out.append(WindowScore(window_id=f"w{i}", mid_now=mid, predicted=mid + (realised - mid) * off,
                               realised=realised, half_width=0.01))
    return out


def test_bootstrap_separates_a_real_improvement_from_none():
    incumbent = scores([0.0] * 60)                 # echoes the mid: skill 0
    better = scores([0.5] * 60)                    # removes half the error everywhere
    same = scores([0.0] * 60)
    assert paired_bootstrap(better, incumbent)["low"] > 0
    ci = paired_bootstrap(same, incumbent)
    assert ci["low"] <= 0 <= ci["high"]


def test_accept_requires_held_out_gain_no_train_regression_and_bounded_cost():
    inc_t, inc_h = scores([0.0] * 40), scores([0.0] * 40, seed=1)
    good_t, good_h = scores([0.4] * 40), scores([0.4] * 40, seed=1)
    d = accept(candidate_train=good_t, incumbent_train=inc_t, candidate_holdout=good_h,
               incumbent_holdout=inc_h, candidate_cost=0.01, incumbent_cost=0.01)
    assert d.accepted, d.reasons
    overfit = accept(candidate_train=good_t, incumbent_train=inc_t, candidate_holdout=scores([0.0] * 40, seed=1),
                     incumbent_holdout=inc_h, candidate_cost=0.01, incumbent_cost=0.01)
    assert not overfit.accepted and "noise" in overfit.reasons[0]
    pricey = accept(candidate_train=good_t, incumbent_train=inc_t, candidate_holdout=good_h,
                    incumbent_holdout=inc_h, candidate_cost=0.05, incumbent_cost=0.01)
    assert not pricey.accepted and any("costs" in r for r in pricey.reasons)
