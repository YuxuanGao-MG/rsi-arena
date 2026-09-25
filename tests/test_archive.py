"""The archive keeps what the search found, including what it found by losing.

Offline. The point of every test here is that a candidate which loses on the
mean is not the same thing as a candidate which is no use, and the difference
is per-instance.
"""

from __future__ import annotations

import pickle

import pytest

from rsi_arena.loop import Archive, Entry, from_gepa_state
from rsi_arena.loop.generation import fingerprint_components


def entry(name: str, scores: dict[str, float], **kw) -> Entry:
    return Entry(id=name, components={"context": name}, generation="g1",
                 scores=scores, **kw)


def spread(value: float, n: int = 10) -> dict[str, float]:
    return {f"i{i}": value for i in range(n)}


def test_a_specialist_survives_a_better_generalist():
    """The whole reason for the archive.

    ``narrow`` is worse everywhere but one instance, where it is the only thing
    that has ever worked. Greedy selection deletes it. That deletion is what the
    Darwin Gödel Machine's ablation is about, and what GEPA's +6.05% against
    +12.44% measures.
    """
    a = Archive([entry("broad", spread(0.6)),
                 entry("narrow", {**spread(0.4), "i3": 0.99})])
    ids = {e.id for e in a.frontier()}
    assert ids == {"broad", "narrow"}
    assert a.wins()["narrow"] == ["i3"]
    assert len(a.wins()["broad"]) == 9


def test_a_candidate_worse_everywhere_is_dropped():
    a = Archive([entry("good", spread(0.6)), entry("bad", spread(0.5))])
    assert [e.id for e in a.frontier()] == ["good"]


def test_a_candidate_that_wins_nothing_is_not_on_the_frontier():
    """The frontier is candidates that own something, not every candidate."""
    a = Archive([entry("full", spread(0.6, 20)), entry("nowhere", spread(0.5, 20))])
    assert [e.id for e in a.frontier()] == ["full"]


def test_only_a_contested_instance_can_rank_anything():
    """An instance one candidate has seen cannot say it is better than anyone.

    This is how gen5's refused harness came to own the archive: scored an
    identical 0.5 on all 2,600 windows of its own split, with no earlier
    candidate having seen any of them, it was trivially instance-best on 2,586 —
    a sampling weight of sixty to one over every candidate that had actually
    forecast something, earned entirely by being refused.
    """
    alone = Archive([entry("a", spread(0.5, 30)), entry("b", {f"z{i}": 0.9 for i in range(30)})])
    assert alone.contested() == set()
    assert alone.wins() == {"a": [], "b": []}
    # Neither has been compared, so neither is dropped and neither dominates.
    assert {e.id for e in alone.frontier()} == {"a", "b"}
    picks = [alone.sample_parents(1, seed=s)[0].id for s in range(20)]
    assert set(picks) == {"a", "b"}, "an uncompared candidate must stay reachable"


def test_a_candidate_that_lost_every_comparison_it_had_is_dropped():
    """Compared and beaten is not the same as never compared."""
    a = Archive([entry("better", {**spread(0.6, 20), "i0": 0.9, "i1": 0.9}),
                 entry("beaten", {"i0": 0.5, "i1": 0.5})])
    assert a.contested() == {"i0", "i1"}
    assert [e.id for e in a.frontier()] == ["better"]


def test_parents_are_sampled_in_proportion_to_what_they_own():
    a = Archive([entry("wide", spread(0.9, 20)),
                 entry("narrow", {**spread(0.1, 20), "i0": 0.95})])
    picks = []
    for s in range(60):
        picks.append(a.sample_parents(1, seed=s)[0].id)
        # Reset between draws: this measures the weighting at a fixed state, not
        # the decay, which the next test measures on its own.
        for e in a.entries:
            e.children = 0
    assert picks.count("wide") > picks.count("narrow")
    assert picks.count("narrow") > 0, "a specialist must still be reachable"


def test_a_mined_parent_steps_aside():
    """1/(1+children). Without it one ancestor is sampled until the heat death."""
    a = Archive([entry("wide", spread(0.9, 40)), entry("narrow", {**spread(0.1, 40), "i0": 0.95})])
    first = a.sample_parents(1, seed=0)[0]
    assert first.children == 1
    wide = a.get("wide")
    picks = []
    for s in range(20):
        wide.children, a.get("narrow").children = 200, 0
        picks.append(a.sample_parents(1, seed=s)[0].id)
    assert picks.count("narrow") > picks.count("wide"), \
        "a parent mined two hundred times should be yielding to one mined never"


def test_sampling_several_parents_returns_several_distinct_ones():
    a = Archive([entry(f"c{i}", {**spread(0.5, 12), f"i{i}": 0.9}) for i in range(5)])
    picked = a.sample_parents(3, seed=7)
    assert len({e.id for e in picked}) == 3


def test_the_same_candidate_found_twice_is_one_entry_with_both_readings():
    a = Archive()
    a.add(entry("x", {"i0": 0.5}))
    a.add(Entry(id="x", components={"context": "x"}, generation="g2",
                scores={"i1": 0.7}, promoted=True))
    assert len(a) == 1
    only = a.entries[0]
    assert only.scores == {"i0": 0.5, "i1": 0.7} and only.promoted


def test_round_trips_through_disk(tmp_path):
    a = Archive([entry("a", spread(0.6)), entry("b", spread(0.4))])
    a.sample_parents(1, seed=0)
    path = tmp_path / "archive.json"
    a.save(path)
    back = Archive.load(path)
    assert len(back) == 2
    assert {e.id for e in back.frontier()} == {e.id for e in a.frontier()}
    assert sum(e.children for e in back.entries) == 1


def test_an_unreadable_archive_is_empty_rather_than_fatal(tmp_path):
    """It is memory, not evidence. Losing it costs efficiency; refusing to run costs the generation."""
    path = tmp_path / "archive.json"
    path.write_text("{not json")
    assert len(Archive.load(path)) == 0


def test_reads_a_gepa_state_whose_rows_are_dicts(tmp_path):
    """The rows are dicts keyed by valset position, not lists.

    Read as lists they enumerate their own keys, and every candidate comes out
    with the identical row 0..N-1 — thirteen candidates, one distinct score row,
    a mean of 50.5 on a scale that tops out at 1. That is exactly what the first
    version of this reader produced.
    """
    run = tmp_path / "run"
    (run / "gepa").mkdir(parents=True)
    state = {"program_candidates": [{"context": "a"}, {"context": "b"}],
             "prog_candidate_val_subscores": [{0: 0.5, 1: 0.25}, {0: 0.75, 1: 0.5}],
             "parent_program_for_candidate": [None, 0],
             "num_metric_calls_by_discovery": [0, 40]}
    (run / "gepa" / "gepa_state.bin").write_bytes(pickle.dumps(state))

    found = from_gepa_state(run, "gen9", ["i0", "i1"],
                            lambda c: fingerprint_components(c, "m"))
    assert len(found) == 2
    assert found[0].scores == {"i0": 0.5, "i1": 0.25}
    assert found[1].scores == {"i0": 0.75, "i1": 0.5}
    assert found[1].parent == found[0].id
    assert found[1].discovered_after_calls == 40
    assert all(0.0 <= v <= 1.0 for e in found for v in e.scores.values())


def test_a_missing_gepa_state_is_not_an_error(tmp_path):
    assert from_gepa_state(tmp_path, "g", ["i0"], lambda c: "x") == []


# -- running out of money ------------------------------------------------------

def test_an_account_402_reads_as_exhaustion_not_as_a_quiet_harness():
    """The consequence is the same; the failure mode is worse.

    Our own ceiling refuses instantly and says so. A 402 is a per-call provider
    error, which the runner records and scores as silence — so a run that has
    simply run out of money produces a full set of rollouts that read as a
    harness which chose to stay quiet, and every number computed from them is
    wrong in a way nothing announces.
    """
    from rsi_arena.harness import OpenRouter

    client = OpenRouter(budget_usd=100.0, cache=False)
    assert client.over_budget is False
    client.starved = True
    assert client.over_budget is True, "a starved account is over budget whatever the ceiling says"


def test_a_ceiling_of_none_still_answers_the_question():
    from rsi_arena.harness import OpenRouter
    client = OpenRouter(cache=False)
    assert client.budget_usd is None and client.over_budget is False
    client.spent_usd = 10_000.0
    assert client.over_budget is False, "no ceiling means no ceiling"


def test_an_exhausted_client_stops_the_evaluation_immediately():
    """A refused call is instant; the plan that reaches it is not.

    The tool steps still run, and they are rate-limited reads against the
    exchange. A generation that exhausted its budget early kept grinding through
    eight hundred held-out windows at exchange speed, spending nothing and
    finishing nothing, until the job timeout.
    """
    import asyncio
    from rsi_arena.loop import evaluate
    from rsi_arena.harness import Harness

    class Spent:
        over_budget = True
        calls = 0

        async def complete(self, *a, **k):        # pragma: no cover - must never run
            raise AssertionError("asked a client that has nothing left")

    class Inst:
        id = "i0"
        group = "g"

        def to_dict(self):
            return {}

    class Task:
        name = "t"
        inputs = frozenset({"question", "game"})

        def toolbox(self, i):
            # Enough for harness.check to pass, so the test reaches the guard
            # rather than tripping over an unrelated one.
            from rsi_arena.harness.tools import FunctionTool, Toolbox
            return Toolbox([FunctionTool(name=n, description=n, fn=lambda **k: {})
                            for n in ("market_quote", "candlesticks", "previous_trades",
                                      "state_summary", "move_base_rate", "tape_imbalance",
                                      "settlement_countdown")])

        def run_inputs(self, i):
            return {}

        def failed(self, i, why):
            from rsi_arena.loop import Outcome
            return Outcome(value=0.5, feedback=why, objectives={}, details={"scored": False})

        def score(self, i, run):                  # pragma: no cover - must never run
            raise AssertionError("scored a run that was never made")

    rollouts = asyncio.run(evaluate(Task(), Harness.load("harnesses/horizon-5m.json"),
                                    [Inst(), Inst()], Spent()))
    assert len(rollouts) == 2
    assert all(r.run is None for r in rollouts)
    assert "budget" in rollouts[0].outcome.feedback
