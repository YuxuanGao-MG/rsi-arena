"""The loop end to end against the Kalshi topic, with a fake model and a fake history."""

import json
from datetime import timedelta
from pathlib import Path

from rsi_arena.harness import Harness
from rsi_arena.loop import (Generation, TaskAdapter, accept, evaluate, lineage, paired_bootstrap,
                            reflection_templates, split_by_group, summarise)
from rsi_arena.loop.generation import fingerprint, resolve_harness
from rsi_arena.topics.kalshi_horizon import KalshiHorizon, Window
from tests.conftest import FakeLLM

BASE = Path(__file__).resolve().parents[1] / "harnesses" / "horizon-5m.json"


def windows(t0, n=6, group="E"):
    return [Window(ticker="A", at=t0 + timedelta(minutes=5 * k), mid_now=0.40 + 0.05 * k,
                   realised=0.45 + 0.05 * k, game={"clock": f"{5 * k}'"}, event=group if k % 2 else "F")
            for k in range(1, n + 1)]


def task(history, t0):
    return KalshiHorizon(history=history, windows=windows(t0))


def oracle(m, s, t):        # always right: the fake series drifts +5c per window
    return {"delta_cents": 5, "half_width_cents": 2, "confidence": 0.5, "driver": "d", "falsifier": "f"}


def silent(m, s, t):
    return {"delta_cents": 0, "half_width_cents": 1, "confidence": 0.5, "driver": "d", "falsifier": "f"}


def test_split_is_by_group(t0, history):
    train, hold = split_by_group(task(history, t0).instances(), holdout=1, seed=0)
    assert train and hold and {w.group for w in train}.isdisjoint({w.group for w in hold})


async def test_evaluate_scores_and_summarises(t0, history):
    tk = task(history, t0)
    rollouts = await evaluate(tk, Harness.load(BASE), tk.instances(), FakeLLM(oracle))
    assert all(r.run.ok for r in rollouts)
    assert [round(r.outcome.value, 6) for r in rollouts] == [1.0] * 6
    summary = summarise(tk, rollouts)
    assert summary["instances"] == 6 and summary["statistic"] == 1.0 and summary["unscored"] == 0
    assert "five minutes later" in rollouts[0].outcome.feedback and rollouts[0].outcome.details["scored"]


async def test_a_harness_that_cannot_run_fails_every_instance_with_the_reason(t0, history):
    tk = task(history, t0)
    broken = Harness.load(BASE).from_components({"tools": "market_quote, news_search"})
    rollouts = await evaluate(tk, broken, tk.instances(), FakeLLM(oracle))
    assert all(r.run is None and r.outcome.value == 0.0 for r in rollouts)
    assert "news_search" in rollouts[0].outcome.feedback
    assert tk.statistic([r.outcome for r in rollouts]) == 0.0        # counted as silence


def test_adapter_speaks_gepa(t0, history):
    tk = task(history, t0)
    adapter = TaskAdapter(tk, Harness.load(BASE), FakeLLM(oracle))
    batch = adapter.evaluate(tk.instances()[:3], adapter.base.to_components(), capture_traces=True)
    assert [round(v, 6) for v in batch.scores] == [1.0, 1.0, 1.0]
    assert set(batch.objective_scores[0]) == {"skill", "cost"}
    assert batch.trajectories[0]["tools"][0]["tool"] == "market_quote"
    data = adapter.make_reflective_dataset(adapter.base.to_components(), batch, ["plan", "context"])
    assert set(data) == {"plan", "context"} and len(data["plan"]) == 3
    rec = data["plan"][0]
    assert "tool answers" in rec["Inputs"] and "Score 1.00" in rec["Feedback"]
    assert json.loads(rec["Generated Outputs"])["delta_cents"] == 5
    bad = adapter.evaluate(tk.instances()[:2], {**adapter.base.to_components(), "plan": "nope"}, True)
    assert bad.scores == [0.0, 0.0] and "not valid JSON" in bad.trajectories[0]["feedback"]


def test_reflection_templates_state_the_task_once_per_component():
    t = reflection_templates(KalshiHorizon(windows=[]), Harness.load(BASE))
    assert set(t) == {"context", "plan", "tools"}
    for text in t.values():
        assert "<curr_param>" in text and "<side_info>" in text and "delta_cents" in text
    assert '"type": "loop"' in t["plan"]


async def test_gate_promotes_a_real_gain_and_rejects_silence(t0, history):
    tk = task(history, t0)
    inst = tk.instances()
    base = await evaluate(tk, Harness.load(BASE), inst, FakeLLM(silent))
    good = await evaluate(tk, Harness.load(BASE), inst, FakeLLM(oracle))
    same = await evaluate(tk, Harness.load(BASE), inst, FakeLLM(silent))
    assert paired_bootstrap(tk, good, base, min_groups=1)["low"] > 0
    ci = paired_bootstrap(tk, same, base, min_groups=1)
    assert ci["low"] <= 0 <= ci["high"]
    d = accept(tk, candidate_train=good[:3], incumbent_train=base[:3],
               candidate_holdout=good[3:], incumbent_holdout=base[3:], min_groups=1)
    assert d.accepted, d.reasons
    d = accept(tk, candidate_train=good[:3], incumbent_train=base[:3],
               candidate_holdout=same[3:], incumbent_holdout=base[3:], min_groups=1)
    assert not d.accepted and "noise" in d.reasons[0]


def test_generations_chain_and_promote(tmp_path):
    h = Harness.load(BASE)
    g1 = Generation(run_dir=str(tmp_path / "gen1"), topic="t", incumbent=str(BASE),
                    incumbent_fingerprint=fingerprint(h), decision={"accepted": False, "reasons": ["no"]})
    g1.save()
    assert g1.promoted() == str(BASE)                                  # rejected: incumbent carries on
    (tmp_path / "gen2").mkdir()
    h.from_components({"context": "better"}).save(tmp_path / "gen2" / "best.json")
    g2 = Generation(run_dir=str(tmp_path / "gen2"), topic="t", parent=str(tmp_path / "gen1"),
                    incumbent=g1.promoted(), decision={"accepted": True, "reasons": ["gain"]})
    g2.save()
    harness, source, parent = resolve_harness(tmp_path / "gen2")
    assert harness.context == "better" and source.endswith("best.json") and parent == str(tmp_path / "gen2")
    chain = lineage(tmp_path / "gen2")
    assert [g.path.name for g in chain] == ["gen1", "gen2"] and chain[1].accepted


# --- identity of a harness, and of a search that found nothing ---------------


def test_renaming_a_candidate_does_not_change_its_fingerprint() -> None:
    """The loop calls every candidate `<name>+genN`. A fingerprint over
    `to_dict()` therefore differed every generation whether or not anything had
    been rewritten, and the first real run recorded a new fingerprint for a
    search that returned the seed untouched."""
    from rsi_arena.harness.spec import Harness
    from rsi_arena.loop.generation import fingerprint

    seed = Harness.load("harnesses/horizon-5m.json")
    renamed = seed.model_copy(update={"name": seed.name + "+gen1",
                                      "description": "written by gen1"})
    assert fingerprint(renamed) == fingerprint(seed)


def test_a_rewritten_component_does_change_it() -> None:
    from rsi_arena.harness.spec import Harness
    from rsi_arena.loop.generation import fingerprint

    seed = Harness.load("harnesses/horizon-5m.json")
    rewritten = seed.from_components({**seed.to_components(),
                                      "context": seed.context + "\n\nAlso: be brief."})
    assert fingerprint(rewritten) != fingerprint(seed)


def test_the_same_components_on_another_model_are_another_harness() -> None:
    """Same prompt, different model, different thing being measured."""
    from rsi_arena.harness.spec import Harness
    from rsi_arena.loop.generation import fingerprint

    seed = Harness.load("harnesses/horizon-5m.json")
    other = seed.model_copy(deep=True)
    other.config.model = "openai/gpt-5"
    assert fingerprint(other) != fingerprint(seed)


async def test_a_search_that_returned_the_incumbent_says_so(t0, history):
    """Not "not distinguishable from noise", which describes a rewrite that
    tied. On the first real run there was no rewrite: GEPA's best was the seed,
    so every paired difference was exactly zero and the report read as a near
    miss. Telling a search that found nothing from a rewrite that failed is the
    whole job."""
    tk = task(history, t0)
    inst = tk.instances()
    base = await evaluate(tk, Harness.load(BASE), inst, FakeLLM(silent))

    verdict = accept(tk, candidate_train=base[:3], incumbent_train=base[:3],
                     candidate_holdout=base[3:], incumbent_holdout=base[3:],
                     unchanged=True, min_groups=1)
    assert verdict.accepted is False
    assert "returned the incumbent unchanged" in " ".join(verdict.reasons)
    assert "noise" not in " ".join(verdict.reasons)


# --- the bootstrap has to respect the split it was given ---------------------


async def test_the_interval_is_drawn_over_matches_not_windows(t0, history):
    """Thirty-four windows of one match are one match seen thirty-four times:
    overlapping horizons, one scoreline, and a goal that moves all of them at
    once. Resampling them independently claims thirty-four facts and reports an
    interval far tighter than the evidence supports."""
    tk = task(history, t0)
    inst = tk.instances()
    base = await evaluate(tk, Harness.load(BASE), inst, FakeLLM(silent))
    good = await evaluate(tk, Harness.load(BASE), inst, FakeLLM(oracle))

    ci = paired_bootstrap(tk, good, base, min_groups=1)
    assert ci["groups"] == len({r.instance.group for r in base})
    assert ci["groups"] < ci["paired"], "more windows than matches, as always"


async def test_too_few_matches_is_no_sample_rather_than_a_small_one(t0, history):
    """With two matches a cluster bootstrap can only draw {A,A}, {A,B}, {B,B};
    the interval collapses toward the observed difference and reads as
    precision. The first thousand-call run was gated on exactly two held-out
    fixtures, so the number it reported could not have meant anything either
    way."""
    tk = task(history, t0)
    inst = tk.instances()
    base = await evaluate(tk, Harness.load(BASE), inst, FakeLLM(silent))
    good = await evaluate(tk, Harness.load(BASE), inst, FakeLLM(oracle))

    ci = paired_bootstrap(tk, good, base, min_groups=99)
    assert ci["usable"] is False
    assert ci["low"] == ci["high"] == 0.0, "no interval, rather than a narrow one"

    verdict = accept(tk, candidate_train=good, incumbent_train=base,
                     candidate_holdout=good, incumbent_holdout=base, min_groups=99)
    assert verdict.accepted is False
    assert "too few to draw an interval" in " ".join(verdict.reasons)
