"""The judgment is paid for before the search spends.

gen11 spent $64.48 of a $64.26 ceiling and bought nothing: the search ran on
candidates dearer than the incumbent it was priced at, the cascade paid at
that price too, and held-out got $6.50 of the $22 it needed. Each test here is
one piece of the structure that stops that from being the order of payment.
"""

from rsi_arena.loop import accept, summarise
from rsi_arena.loop.budget import (SpendStopper, any_rate, cascade_verdict, fresh_rate,
                                   holdout_shortfall, judgment_reserve)
from rsi_arena.loop.task import Outcome, Rollout


class _Inst:
    def __init__(self, i, g="g"):
        self.id, self.group = f"i{i}", g


class _Run:
    def __init__(self, cost, ok=True):
        self.cost_usd, self.ok = cost, ok


def roll(i, *, cost=None, remembered=None, scored=True, value=0.5, ok=True):
    return Rollout(instance=_Inst(i), run=_Run(cost, ok) if cost is not None else None,
                   outcome=Outcome(value=value, feedback="", objectives={},
                                   details={"scored": scored}),
                   remembered_cost=remembered)


def test_the_reserve_is_priced_off_the_incumbent_at_the_gates_ratio():
    base = [roll(i, remembered=0.03) for i in range(10)] + [roll(i, cost=0.04) for i in range(10)]
    r = judgment_reserve(base, windows=480, ratio=2.0, fallback_rate=0.5)
    assert abs(r.rate - 0.035) < 1e-9, "remembered and bought windows both price the incumbent"
    assert abs(r.usd - 480 * 0.035 * 2.0) < 1e-9
    assert "480 windows" in r.describe() and "2.0x" in r.describe()


def test_a_baseline_that_remembers_no_cost_does_not_reserve_nothing():
    """A scoreboard from before costs were recorded reports zero a window, and a
    reserve of zero is gen11 under a new name."""
    base = [roll(i, remembered=None) for i in range(10)]
    r = judgment_reserve(base, windows=100, ratio=2.0, fallback_rate=0.034)
    assert r.rate == 0.034 and abs(r.usd - 6.8) < 1e-9


def test_fresh_rate_ignores_remembered_windows_and_any_rate_counts_them():
    rollouts = [roll(0, cost=0.06), roll(1, remembered=0.02), roll(2, remembered=0.02)]
    assert abs(fresh_rate(rollouts) - 0.06) < 1e-9, "the next window costs what a run costs"
    assert abs(any_rate(rollouts) - (0.06 + 0.02 + 0.02) / 3) < 1e-9
    assert fresh_rate([]) == 0.0 and any_rate([]) == 0.0


class _Spent:
    def __init__(self, usd):
        self.spent_usd = usd


class _Rated:
    def __init__(self, rate):
        self.rate = rate


def test_the_stopper_fires_one_accepted_candidate_before_the_reserve():
    """GEPA checks between iterations, after a full valset pass; the line is
    drawn where one more such pass would cross into the reserve."""
    spent, rated = _Spent(10.0), _Rated(0.0)
    stop = SpendStopper(spent, 30.0, lookahead_windows=250, rate_of=rated, fallback_rate=0.04)
    # Nothing run yet: priced at the incumbent's rate. 10 + 250 * 0.04 = 20 < 30.
    assert stop() is False and stop.fired is False
    # The search turns out to be paying 1.7x. 10 + 250 * 0.068 = 27 < 30.
    rated.rate = 0.068
    assert stop() is False
    # 13 + 17 = 30: one more accepted candidate would eat the reserve.
    spent.spent_usd = 13.0
    assert stop() is True and stop.fired is True and stop.spent_at_stop == 13.0
    # Once fired, stays fired, and the sentence names the numbers.
    spent.spent_usd = 0.0
    assert stop() is True
    assert "$13.00" in stop.describe() and "$30.00" in stop.describe()


def test_the_stopper_uses_the_fallback_until_the_search_has_paid_for_a_window():
    stop = SpendStopper(_Spent(0.0), 100.0, lookahead_windows=10, rate_of=_Rated(0.0),
                        fallback_rate=0.5)
    assert stop.rate == 0.5
    stop.rate_of.rate = 0.01
    assert stop.rate == 0.01


def test_the_cascade_refuses_broken_worse_or_too_dear_in_that_order():
    inc = [roll(i, remembered=0.03) for i in range(8)]
    fine = [roll(i, cost=0.04) for i in range(8)]
    assert cascade_verdict(fine, inc, gap=0.0, floor=-0.005, max_unscored=0.25,
                           max_cost_ratio=2.0) is None

    broken = [roll(i, cost=0.0, scored=(i < 4)) for i in range(8)]
    why = cascade_verdict(broken, inc, gap=0.1, floor=-0.005, max_unscored=0.25,
                          max_cost_ratio=2.0)
    assert why and "never ran" in why

    why = cascade_verdict(fine, inc, gap=-0.01, floor=-0.005, max_unscored=0.25,
                          max_cost_ratio=2.0)
    assert why and "below" in why

    dear = [roll(i, cost=0.09) for i in range(8)]       # 3x the incumbent
    why = cascade_verdict(dear, inc, gap=0.2, floor=-0.005, max_unscored=0.25,
                          max_cost_ratio=2.0)
    assert why and "3.0x" in why and "cost" in why

    # An incumbent with no recorded cost cannot support a ratio, so the check
    # stands down rather than dividing by zero or refusing everything.
    free = [roll(i, remembered=None) for i in range(8)]
    assert cascade_verdict(dear, free, gap=0.2, floor=-0.005, max_unscored=0.25,
                           max_cost_ratio=2.0) is None


def test_held_out_is_bought_whole_or_not_at_all():
    assert holdout_shortfall(400, 0.055, remaining_usd=None) is None, "no ceiling, no shortfall"
    assert holdout_shortfall(400, 0.055, remaining_usd=22.0) is None
    why = holdout_shortfall(400, 0.055, remaining_usd=6.5)
    assert why and "$22.00" in why and "$6.50" in why and "silence" in why
    assert holdout_shortfall(0, 0.055, remaining_usd=0.0) is None, "nothing to buy costs nothing"


def test_an_incomplete_generation_says_so_in_its_own_words():
    class T:
        def statistic(self, outcomes):
            return sum(o.value for o in outcomes) / max(1, len(outcomes))

    inc = [roll(i, remembered=0.03) for i in range(8)]
    d = accept(T(), candidate_train=inc, incumbent_train=inc, candidate_holdout=[],
               incumbent_holdout=[], incomplete="held-out would cost about $22.00")
    assert not d.accepted and d.reasons == ["incomplete: held-out would cost about $22.00"]

    # The cascade's sentence carries its reason, so a cost rejection does not
    # read as a score rejection.
    d = accept(T(), candidate_train=inc, incumbent_train=inc, candidate_holdout=[],
               incumbent_holdout=[], stopped_early=True,
               stop_reason="it costs 3.0x the incumbent a window on the probe")
    assert not d.accepted and "3.0x" in d.reasons[0] and "cascade rejected" in d.reasons[0]


def test_a_remembered_answer_is_not_a_failed_run():
    """gen11's manifest said the baseline failed 400 runs of 400 beside a
    perfectly good score: every one was served from the scoreboard."""
    class T:
        def statistic(self, outcomes):
            return 0.0

        def summary(self, outcomes):
            return {}

    rollouts = [roll(0, remembered=0.03), roll(1, cost=0.03, ok=True), roll(2, cost=0.0, ok=False)]
    assert summarise(T(), rollouts)["failed_runs"] == 1


def test_the_ratio_is_applied_to_a_floored_incumbent():
    """A Jev incumbent runs a window for a hundredth of a cent, and twice
    nothing is nothing: any candidate that asks Opus once (about three cents)
    fails on cost by construction, which forbids exactly the delegation the
    model tools exist for. The floor prices such an incumbent at a penny."""
    jev = [roll(i, remembered=0.0002) for i in range(8)]
    asks_once = [roll(i, cost=0.02) for i in range(8)]
    why = cascade_verdict(asks_once, jev, gap=0.2, floor=-0.005, max_unscored=0.25,
                          max_cost_ratio=2.0)
    assert why and "100.0x" in why, "without the floor the ratio is absurd"
    assert cascade_verdict(asks_once, jev, gap=0.2, floor=-0.005, max_unscored=0.25,
                           max_cost_ratio=2.0, cost_floor=0.01) is None

    # Dearer than twice the floor is still refused, and the sentence says the
    # floor was what it was measured against.
    why = cascade_verdict([roll(i, cost=0.03) for i in range(8)], jev, gap=0.2, floor=-0.005,
                          max_unscored=0.25, max_cost_ratio=2.0, cost_floor=0.01)
    assert why and "3.0x" in why and "floor" in why and "$0.0002" in why

    # An incumbent above the floor is priced at its own rate, as before.
    opus = [roll(i, remembered=0.034) for i in range(8)]
    why = cascade_verdict([roll(i, cost=0.09) for i in range(8)], opus, gap=0.2, floor=-0.005,
                          max_unscored=0.25, max_cost_ratio=2.0, cost_floor=0.01)
    assert why and "2.6x" in why and "floor" not in why

    # The floor lifts a cheap incumbent; it does not price an unrecorded one.
    free = [roll(i, remembered=None) for i in range(8)]
    assert cascade_verdict(asks_once, free, gap=0.2, floor=-0.005, max_unscored=0.25,
                           max_cost_ratio=2.0, cost_floor=0.01) is None


def test_the_reserve_for_a_jev_incumbent_is_priced_at_the_floor():
    """The gate lets a candidate cost twice the floored incumbent, so the
    reserve has to be priced there too, or a Jev baseline keeps back a few
    cents for a judgment that may legitimately cost twenty dollars of Opus."""
    jev = [roll(i, remembered=0.00015) for i in range(10)]
    r = judgment_reserve(jev, windows=480, ratio=2.0, fallback_rate=0.034, cost_floor=0.01)
    assert r.rate == 0.01 and r.floored
    assert abs(r.usd - 480 * 0.01 * 2.0) < 1e-9
    assert "floor" in r.describe()

    opus = [roll(i, remembered=0.034) for i in range(10)]
    r = judgment_reserve(opus, windows=480, ratio=2.0, fallback_rate=0.034, cost_floor=0.01)
    assert abs(r.rate - 0.034) < 1e-9 and not r.floored and "floor" not in r.describe()

    # No recorded cost falls back first, then the floor applies to the fallback.
    free = [roll(i, remembered=None) for i in range(10)]
    r = judgment_reserve(free, windows=100, ratio=2.0, fallback_rate=0.005, cost_floor=0.01)
    assert r.rate == 0.01 and r.floored
