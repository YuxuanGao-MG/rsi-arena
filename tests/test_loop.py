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
    # An oracle removes every cent the benchmark missed by, so skill is exactly
    # one. Its *value* is smaller, because value is the edge in cents against a
    # ten-cent full point — a perfect call on a quiet market is still a small
    # edge, and the two numbers answer different questions on purpose.
    assert all(0.5 < r.outcome.value <= 1.0 for r in rollouts), \
        [r.outcome.value for r in rollouts]
    summary = summarise(tk, rollouts)
    assert summary["instances"] == 6 and summary["statistic"] == 1.0 and summary["unscored"] == 0
    assert "five minutes later" in rollouts[0].outcome.feedback and rollouts[0].outcome.details["scored"]


async def test_a_harness_that_cannot_run_fails_every_instance_with_the_reason(t0, history):
    tk = task(history, t0)
    broken = Harness.load(BASE).from_components({"tools": "market_quote, news_search"})
    rollouts = await evaluate(tk, broken, tk.instances(), FakeLLM(oracle))
    # The two numbers on this line used to disagree: the optimizer was handed
    # 0.0 and the gate computed 0.0 skill, which look equal and are not — 0.0 on
    # the optimizer's scale is a ten-cent error, and 0.0 skill is silence, which
    # is 0.5. They now say the same thing about the same window.
    assert all(r.run is None and r.outcome.value == 0.5 for r in rollouts)
    assert "news_search" in rollouts[0].outcome.feedback
    assert tk.statistic([r.outcome for r in rollouts]) == 0.0        # counted as silence


def test_adapter_speaks_gepa(t0, history):
    tk = task(history, t0)
    adapter = TaskAdapter(tk, Harness.load(BASE), FakeLLM(oracle))
    batch = adapter.evaluate(tk.instances()[:3], adapter.base.to_components(), capture_traces=True)
    # An oracle removes all of the benchmark's error, which on these windows is
    # well under the ten cents a full point is worth — a perfect forecast on a
    # quiet market is still a small edge, and the value says so.
    assert all(0.5 < v <= 1.0 for v in batch.scores), batch.scores
    assert set(batch.objective_scores[0]) == {"skill", "cost"}
    assert batch.trajectories[0]["tools"][0]["tool"] == "market_quote"
    data = adapter.make_reflective_dataset(adapter.base.to_components(), batch, ["plan", "context"])
    assert set(data) == {"plan", "context"} and len(data["plan"]) == 3
    rec = data["plan"][0]
    # The feedback carries both numbers and they are not the same number: skill
    # is the fraction of the benchmark's error removed, score is the edge in
    # cents. A rewriter reading this needs to see that a perfect call on a
    # five-cent move is not a full point of edge.
    assert "tool answers" in rec["Inputs"]
    assert "skill +1.00" in rec["Feedback"] and "Score 0.75" in rec["Feedback"]
    assert json.loads(rec["Generated Outputs"])["delta_cents"] == 5
    bad = adapter.evaluate(tk.instances()[:2], {**adapter.base.to_components(), "plan": "nope"}, True)
    # 0.5, not 0.0. A harness that cannot run said nothing, and saying nothing is
    # worth exactly silence — which is the middle of this scale, because value is
    # affine in error removed and silence removes none. This asserted 0.0 until
    # the day someone noticed the gate was reading the same window as silence
    # while the optimizer was reading it as ten cents wrong: half the range of
    # disagreement about the commonest failure there is. The reason for the
    # rejection still has to be in the feedback, and that is the part that
    # matters here.
    assert bad.scores == [0.5, 0.5] and "not valid JSON" in bad.trajectories[0]["feedback"]


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


# --- where the next generation goes ------------------------------------------


def _fake_run(root, name, created, **extra):
    import json
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "manifest.json").write_text(json.dumps(
        {"run_dir": str(d), "topic": "t", "created": created, **extra}))
    return d


def test_the_next_generation_follows_the_deepest_one(tmp_path):
    """A schedule cannot be told "continue from gen4" by a person twice a day,
    and one that always starts from the seed is not a lineage — it is the same
    experiment repeated."""
    from rsi_arena.cli import _next_name

    _fake_run(tmp_path, "gen1", "2026-09-15T01:00:00+00:00")
    _fake_run(tmp_path, "gen3", "2026-09-15T03:00:00+00:00")
    assert _next_name("gen3", tmp_path) == "gen4"


def test_a_number_in_the_middle_of_a_name_is_not_a_generation(tmp_path):
    """`gen1-floored` ends in a letter. Reading the 1 out of its middle produced
    `gen1-floore2`, a name that says nothing about which generation it is."""
    from rsi_arena.cli import _next_name

    _fake_run(tmp_path, "gen1-floored", "2026-09-15T01:00:00+00:00")
    assert _next_name("gen1-floored", tmp_path) == "gen2"


def test_a_name_is_never_reused(tmp_path):
    from rsi_arena.cli import _next_name

    _fake_run(tmp_path, "gen1", "2026-09-15T01:00:00+00:00")
    _fake_run(tmp_path, "gen2", "2026-09-15T02:00:00+00:00")
    assert _next_name("gen1", tmp_path) == "gen2b", "gen2 is taken"


def test_an_unreadable_manifest_is_skipped_not_fatal(tmp_path, capsys):
    """A half-written or hand-made manifest should not stop a schedule from
    finding where to continue."""
    import argparse

    from rsi_arena.cli import cmd_next

    _fake_run(tmp_path, "gen1", "2026-09-15T01:00:00+00:00")
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / "manifest.json").write_text('{"topic": "no run_dir"}')

    code = cmd_next(argparse.Namespace(runs_dir=str(tmp_path), seed="seed.json", json=True))
    assert code == 0
    out = capsys.readouterr()
    assert "gen2" in out.out and "skipping" in out.err


# --- the cascade -------------------------------------------------------------


async def test_a_cascade_rejection_says_it_never_paid_for_held_out(t0, history):
    """Scoring a candidate on train and held-out is two thirds of a generation's
    bill and most candidates are not close. When the probe rejects one, held-out
    was never run — and reporting that as "no held-out instances" would read as a
    fault when it was a decision."""
    tk = task(history, t0)
    inst = tk.instances()
    base = await evaluate(tk, Harness.load(BASE), inst, FakeLLM(silent))

    verdict = accept(tk, candidate_train=base[:3], incumbent_train=base[:3],
                     candidate_holdout=[], incumbent_holdout=[],
                     stopped_early=True, min_groups=1)
    assert verdict.accepted is False
    reason = " ".join(verdict.reasons)
    assert "cascade" in reason and "never paid for" in reason
    assert "no paired held-out" not in reason


async def test_a_candidate_that_clears_the_probe_is_still_gated(t0, history):
    """The cascade only ever rejects. Surviving it buys the full evaluation, not
    a promotion — the held-out interval is still the only way in."""
    tk = task(history, t0)
    inst = tk.instances()
    base = await evaluate(tk, Harness.load(BASE), inst, FakeLLM(silent))
    good = await evaluate(tk, Harness.load(BASE), inst, FakeLLM(oracle))

    verdict = accept(tk, candidate_train=good[:3], incumbent_train=base[:3],
                     candidate_holdout=good[3:], incumbent_holdout=base[3:],
                     stopped_early=False, min_groups=1)
    assert verdict.accepted, verdict.reasons


async def test_the_probe_is_the_train_scoreboard(t0, history):
    """The gate asks of train only whether the candidate got worse, and a
    regression shows on twenty matches as well as on a hundred and forty. Scoring
    the full train set again was two thirds of a generation's bill for an answer
    the probe already had — held-out is the half that needs power, and held-out
    is still scored in full.
    """
    tk = task(history, t0)
    inst = tk.instances()
    base = await evaluate(tk, Harness.load(BASE), inst, FakeLLM(silent))
    good = await evaluate(tk, Harness.load(BASE), inst, FakeLLM(oracle))

    groups = sorted({r.instance.group for r in base})
    probe = [r for r in good if r.instance.group in groups[:1]]
    against = [r for r in base if r.instance.group in groups[:1]]
    assert probe and len(probe) < len(good), "a probe is a subset, or it is not a probe"

    verdict = accept(tk, candidate_train=probe, incumbent_train=against,
                     candidate_holdout=good, incumbent_holdout=base, min_groups=1)
    assert verdict.accepted, verdict.reasons
    assert verdict.train["paired"] == len(probe), "judged on what was actually scored"


def test_the_rewriter_is_told_every_tool_it_may_reach_for(t0, history):
    """It was told the three the harness already used, and that no others
    existed. The box holds nine.

    That is not a wording problem. The arena's premise is that a harness
    composes primitives; a search that cannot learn what it is allowed to call
    is a search over the prompt with extra steps, and every rewrite GEPA has
    produced so far changed only the context.
    """
    from rsi_arena.loop.adapter import reflection_templates

    tk = task(history, t0)
    base = Harness.load(BASE)
    prompt = reflection_templates(tk, base)["tools"]

    for name in tk.tools():
        assert name in prompt, f"{name} is in the box and not in the prompt"
    assert len(tk.tools()) > len(base.tools), "the box is wider than what the seed uses"


# -- the three-way split ------------------------------------------------------

def test_the_audit_set_never_moves_and_the_holdout_does():
    """Rotation bounds erosion; the frozen slice is the thing that can still be trusted."""
    from rsi_arena.loop import three_way_split

    class I:
        def __init__(self, g):
            self.group = g

    inst = [I(f"m{i}") for i in range(200)]
    audits, holdouts = [], []
    for epoch in range(3):
        train, hold, audit = three_way_split(inst, audit=40, holdout=60, seed=0, epoch=epoch)
        assert len(train) + len(hold) + len(audit) == 200
        audits.append({x.group for x in audit})
        holdouts.append({x.group for x in hold})
        # Nothing the gate or the search touches may be in the audit set.
        assert not ({x.group for x in train} & audits[-1])
        assert not (holdouts[-1] & audits[-1])
    assert audits[0] == audits[1] == audits[2], "the audit set is frozen"
    assert holdouts[0] != holdouts[1], "the evolutionary held-out set rotates"


def test_a_split_with_no_audit_set_is_the_old_two_way_one():
    from rsi_arena.loop import three_way_split

    class I:
        def __init__(self, g):
            self.group = g

    inst = [I(f"m{i}") for i in range(50)]
    train, hold, audit = three_way_split(inst, audit=0, holdout=10, seed=0, epoch=0)
    assert audit == [] and len(hold) == 10 and len(train) == 40


def test_the_probe_is_not_one_league():
    """sorted(groups)[:20] on Kalshi tickers is alphabetical, and alphabetical is by league."""
    from rsi_arena.loop import probe_sample
    groups = ({f"KXMLSGAME-{i}" for i in range(120)}
              | {f"KXBUNDESLIGAGAME-{i}" for i in range(24)}
              | {f"KXEPLGAME-{i}" for i in range(40)})
    picked = probe_sample(groups, 20, seed=0)
    leagues = {g.split("-")[0] for g in picked}
    assert len(picked) == 20
    assert len(leagues) == 3, f"the probe drew from {leagues}"


def test_each_component_reflects_on_evidence_it_can_act_on(t0, history):
    """Byte-identical records for all three is why `tools` never once changed.

    Across fourteen candidates in two thousand-call searches the tool
    allowlist's hash was constant. A prompt asked to rewrite a tool list while
    reading prose and a forecast has nothing tool-shaped to reason about.
    """
    tk = task(history, t0)
    adapter = TaskAdapter(tk, Harness.load(BASE), FakeLLM(oracle))
    batch = adapter.evaluate(tk.instances()[:2], adapter.base.to_components(), capture_traces=True)
    data = adapter.make_reflective_dataset(adapter.base.to_components(), batch,
                                           ["tools", "plan", "context"])

    tools_in = data["tools"][0]["Inputs"]
    plan_in = data["plan"][0]["Inputs"]
    context_in = data["context"][0]["Inputs"]
    assert tools_in != plan_in != context_in

    # The tools prompt must be able to see what it was never given.
    assert "tools available but never called" in tools_in
    untried = tools_in["tools available but never called"]
    assert untried != "none" and "market_shock" in untried, untried
    assert tools_in["tools called, in order"][0] == "market_quote"
    assert "the call sequence" in plan_in and "the call sequence" not in context_in


def test_running_out_of_money_does_not_read_as_a_cascade_rejection():
    """They both stop the run before held-out, and they mean opposite things.

    The first generation it happened to reported "the cascade rejected it on 20
    train matches (+0.136)" — a positive number, in a sentence claiming
    rejection, describing neither. The candidate scored zero because every call
    was refused; the incumbent scored -0.136 because half of its calls were; and
    the difference between two sets of refusals measures nothing.
    """
    from rsi_arena.loop import accept

    class T:
        def statistic(self, outcomes):
            return 0.0

    empty: list = []
    broke = accept(T(), candidate_train=empty, incumbent_train=empty,
                   candidate_holdout=empty, incumbent_holdout=empty, exhausted=True)
    assert not broke.accepted
    assert "ran out of money" in broke.reasons[0]
    assert "cascade" not in broke.reasons[0]

    cut = accept(T(), candidate_train=empty, incumbent_train=empty,
                 candidate_holdout=empty, incumbent_holdout=empty, stopped_early=True)
    assert "cascade" in cut.reasons[0]

    # Exhaustion wins when both are set: the cascade's number was computed from
    # refusals too, so quoting it would be quoting noise.
    both = accept(T(), candidate_train=empty, incumbent_train=empty,
                  candidate_holdout=empty, incumbent_holdout=empty,
                  stopped_early=True, exhausted=True)
    assert "ran out of money" in both.reasons[0]
