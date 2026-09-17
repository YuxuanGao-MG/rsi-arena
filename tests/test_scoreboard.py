"""An answer already paid for is not paid for again.

Offline. Nearly a third of a generation's bill was the incumbent being scored on
windows it had already been scored on — the same harness, because nothing has
ever been promoted, on the same windows, because the question set is committed
and a realised price is fixed once the candle prints.
"""

from __future__ import annotations

import pytest

from rsi_arena.loop import Outcome, Rollout, Scoreboard
from rsi_arena.loop.scoreboard import key


class W:
    def __init__(self, i="i0", mid=0.4, real=0.45):
        self.id, self.mid_now, self.realised = i, mid, real
        self.group = "g"


def outcome(value=0.7, scored=True):
    return Outcome(value=value, feedback="f", objectives={"skill": value},
                   details={"scored": scored, "error": 0.01, "naive_error": 0.05})


def test_a_remembered_answer_comes_back_whole():
    b = Scoreboard()
    b.put("fp", W(), outcome(), cost_usd=0.034)
    got = b.get("fp", W())
    assert got is not None and got.value == pytest.approx(0.7)
    assert got.details["naive_error"] == pytest.approx(0.05)
    assert b.hits == 1


def test_a_different_harness_is_a_different_answer():
    b = Scoreboard()
    b.put("fp", W(), outcome())
    assert b.get("other", W()) is None


def test_a_rebuilt_window_is_a_different_question():
    """Ids are ticker@instant and would survive a rebuild that changed the answer."""
    b = Scoreboard()
    b.put("fp", W(real=0.45), outcome())
    assert b.get("fp", W(real=0.45)) is not None
    assert b.get("fp", W(real=0.52)) is None, "a revised realised price is a new question"


def test_the_original_cost_is_remembered_not_the_zero_it_costs_to_recall():
    """The gate rejects a candidate costing twice the incumbent, guarded on the
    incumbent's cost being above zero. A memoised incumbent reporting zero would
    disable that check without saying so."""
    b = Scoreboard()
    b.put("fp", W(), outcome(), cost_usd=0.034)
    assert b.cost_of("fp", W()) == pytest.approx(0.034)
    r = Rollout(instance=W(), run=None, outcome=outcome(),
                remembered_cost=b.cost_of("fp", W()))
    assert r.cost_usd == pytest.approx(0.034)


def test_a_window_that_went_unscored_is_not_remembered():
    """A silence bought by running out of money must not become permanent."""
    b = Scoreboard()
    b.absorb("fp", [Rollout(instance=W(), run=None, outcome=outcome(scored=False))])
    assert len(b) == 0


def test_round_trips_through_disk(tmp_path):
    b = Scoreboard()
    b.put("fp", W(), outcome(), cost_usd=0.02)
    p = tmp_path / "scoreboard.json"
    b.save(p)
    back = Scoreboard.load(p)
    assert len(back) == 1
    assert back.get("fp", W()).value == pytest.approx(0.7)
    assert back.cost_of("fp", W()) == pytest.approx(0.02)


def test_an_unreadable_scoreboard_is_empty_rather_than_fatal(tmp_path):
    p = tmp_path / "scoreboard.json"
    p.write_text("{not json")
    assert len(Scoreboard.load(p)) == 0


def test_thinning_to_four_reuses_the_answers_bought_at_eight():
    """Why the memo actually pays: the cheaper split is a subset of the dearer one.

    Windows are thinned evenly across a match at `rows[int(i * len/k)]`, and for
    every match length in the question set the four-window pick is contained in
    the eight-window pick. Halving the windows per match therefore costs nothing
    to re-measure — it reuses what the last generation already bought.
    """
    def thin(n, k):
        return {int(i * (n / k)) for i in range(k)}

    for n in range(20, 40):
        assert thin(n, 4) <= thin(n, 8), f"{n} windows: the cheaper split is not a subset"
