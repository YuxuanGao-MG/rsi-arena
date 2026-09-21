"""A model that answers with distributions, wired through the same harness.

TypeSafe's Jev writes no text: it answers typed questions with probabilities.
The contract lets a prompt step carry ``questions`` in place of a schema and
``answers`` to turn the distribution into the output fields the topic scores.
"""

import json
from pathlib import Path

import pytest

from rsi_arena.harness import Harness, HarnessError, OpenRouter, Runner, Toolbox, decisions_url, tool
from rsi_arena.harness.decisions import answers_to_output, half_range_of, mean_of, stdev_of
from tests.conftest import FakeLLM

JEV = Path(__file__).resolve().parents[1] / "harnesses" / "horizon-5m-jev.json"
BASE = Path(__file__).resolve().parents[1] / "harnesses" / "horizon-5m.json"

MOVE = {"type": "score", "instructions": "cents moved", "criteria": ["down", "flat", "up"],
        "values": [-2, 0, 2]}


def box():
    return Toolbox([tool("double", "doubles", {"type": "object", "properties": {"x": {"type": "number"}}})(
        lambda x: {"value": 2 * float(x)})])


def harness(step, model="typesafe/jev-1.13"):
    return Harness.from_dict({"name": "t", "context": "sys", "tools": ["double"],
                              "config": {"model": model, "max_usd": 1.0},
                              "plan": {"steps": [step]}})


def test_the_jev_harness_loads_and_round_trips_its_components():
    h = Harness.load(JEV)
    assert h.config.model.startswith("typesafe/")
    assert h.from_components(h.to_components()).to_components() == h.to_components()
    step = h.plan.steps[-1]
    assert step.questions and step.answers and step.answers["delta_cents"]["as"] == "mean"


def test_a_decisions_model_refuses_a_step_that_asks_for_text():
    with pytest.raises(HarnessError, match="typed questions only"):
        harness({"type": "prompt", "name": "p", "prompt": "x", "output_schema": {"type": "object"}})


def test_a_chat_model_refuses_questions():
    with pytest.raises(HarnessError, match="chat model"):
        harness({"type": "prompt", "name": "p", "prompt": "x", "questions": {"move": MOVE}},
                model="anthropic/claude-opus-5")


def test_questions_are_checked_before_any_call():
    bad = dict(MOVE, values=[0, 0, 1])
    with pytest.raises(HarnessError, match="increase"):
        harness({"type": "prompt", "name": "p", "prompt": "x", "questions": {"move": bad}})
    with pytest.raises(HarnessError, match="not asked"):
        harness({"type": "prompt", "name": "p", "prompt": "x", "questions": {"move": MOVE},
                 "answers": {"d": {"from": "nope", "as": "mean"}}})
    with pytest.raises(HarnessError, match="no 'values'"):
        q = {k: v for k, v in MOVE.items() if k != "values"}
        harness({"type": "prompt", "name": "p", "prompt": "x", "questions": {"move": q},
                 "answers": {"d": {"from": "move", "as": "mean"}}})
    with pytest.raises(HarnessError, match="calls nothing"):
        harness({"type": "prompt", "name": "p", "prompt": "x", "questions": {"move": MOVE},
                 "tools": ["double"]})


def test_the_distribution_becomes_a_move_and_a_width():
    answer = {"type": "score", "score": 1.4, "confidence": 0.6,
              "probabilities": {"0": 0.1, "1": 0.5, "2": 0.4}}
    assert abs(mean_of(answer, [-2, 0, 2]) - 0.6) < 1e-9
    assert abs(stdev_of(answer, [-2, 0, 2]) - (0.1 * 2.6 ** 2 + 0.5 * 0.6 ** 2 + 0.4 * 1.4 ** 2) ** 0.5) < 1e-9
    # Nearest the mean first: flat (0.5) is not enough for 0.6, so up joins.
    assert half_range_of(answer, [-2, 0, 2], coverage=0.6) == pytest.approx(1.4)
    assert half_range_of(answer, [-2, 0, 2], coverage=0.5) == pytest.approx(0.6)
    out = answers_to_output({"move": dict(MOVE)}, {"move": answer}, {
        "delta_cents": {"from": "move", "as": "mean"},
        "half_width_cents": {"from": "move", "as": "half_range", "coverage": 0.6, "min": 2.0},
        "confidence": {"from": "move", "as": "confidence"},
        "driver": {"as": "const", "value": "d"}})
    assert out["delta_cents"] == pytest.approx(0.6) and out["half_width_cents"] == 2.0
    assert out["confidence"] == 0.6 and out["driver"] == "d" and out["decision"]["move"] is answer


def test_a_missing_distribution_falls_back_to_the_reported_level():
    answer = {"type": "score", "score": 2}
    assert mean_of(answer, [-2, 0, 2]) == 2.0 and half_range_of(answer, [-2, 0, 2]) == 0.0


async def test_the_runner_asks_questions_of_the_rendered_prompt_with_the_context_ahead():
    def decisions(state, questions):
        assert state.startswith("sys\n\n") and "got 42.0" in state
        return {"move": {"type": "score", "score": 2.0, "confidence": 0.9,
                         "probabilities": {"0": 0.0, "1": 0.0, "2": 1.0}}}
    llm = FakeLLM(decisions=decisions)
    h = harness({"type": "prompt", "name": "p", "prompt": "got {{d.value}}", "output_key": "out",
                 "questions": {"move": MOVE},
                 "answers": {"delta_cents": {"from": "move", "as": "mean"},
                             "half_width_cents": {"from": "move", "as": "half_range"}}})
    h = Harness.from_dict({**h.to_dict(), "plan": {"steps": [
        {"type": "tool", "name": "d", "tool": "double", "args": {"x": "21"}, "output_key": "d"},
        h.plan.steps[0].model_dump(exclude_none=True)]}})
    run = await Runner(llm, box()).run(h)
    assert run.ok, run.error
    assert run.output["delta_cents"] == 2.0 and run.output["half_width_cents"] == 0.0
    assert llm.decided[0]["model"] == "typesafe/jev-1.13"
    assert "values" in llm.decided[0]["questions"]["move"], "the runner hands the step's questions over whole"
    assert [s.kind for s in run.trace.spans] == ["step", "tool", "step", "llm"]


async def test_the_jev_harness_runs_end_to_end_on_the_fake(t0, history):
    from rsi_arena.loop import evaluate
    from rsi_arena.topics.kalshi_horizon import KalshiHorizon, Window
    from datetime import timedelta
    windows = [Window(ticker="A", at=t0 + timedelta(minutes=5 * k), mid_now=0.40 + 0.05 * k,
                      realised=0.45 + 0.05 * k, game={"clock": f"{5 * k}'"}, event="E")
               for k in range(1, 4)]
    tk = KalshiHorizon(history=history, windows=windows)
    rollouts = await evaluate(tk, Harness.load(JEV), tk.instances(), FakeLLM())
    assert all(r.run.ok for r in rollouts), [r.run.error for r in rollouts]
    # The default fake puts all mass on the middle level - "unchanged" - so the
    # forecast is silence with the floor width, and every window scores as such.
    assert all(r.output["delta_cents"] == 0.0 and r.output["half_width_cents"] == 0.5 for r in rollouts)
    assert all(r.outcome.details["scored"] for r in rollouts)


def test_decisions_go_to_the_alpha_route_on_openrouter_and_beside_chat_elsewhere():
    assert decisions_url("https://openrouter.ai/api/v1") == "https://openrouter.ai/api/alpha/decisions"
    assert decisions_url("https://openmesh-bench-production.up.railway.app/v1") == \
        "https://openmesh-bench-production.up.railway.app/v1/decisions"


async def test_the_client_posts_state_and_questions_without_our_values_and_charges_the_usage():
    seen = {}

    class Reply:
        status_code = 200
        text = ""

        def json(self):
            return {"model": "typesafe/jev-1.13-20260917",
                    "answers": {"move": {"type": "score", "score": 1.0, "probabilities": {"0": 0, "1": 1, "2": 0},
                                         "confidence": 1.0}},
                    "usage": {"input_tokens": 400, "output_tokens": 30, "cost": 1.8e-05}}

    class Http:
        async def post(self, url, *, json, headers):
            seen.update(url=url, body=json)
            return Reply()

    class Open:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    c = OpenRouter(api_key="k", cache=False, budget_usd=1.0)
    c._loop, c._client, c._gate, c._bind = object(), Http(), Open(), (lambda: None)   # noqa: SLF001
    d = await c.decide("state text", {"move": MOVE}, model="typesafe/jev-1.13")
    assert seen["url"].endswith("/api/alpha/decisions")
    assert seen["body"] == {"model": "typesafe/jev-1.13", "state": "state text",
                            "questions": {"move": {k: v for k, v in MOVE.items() if k != "values"}}}
    assert d.answers["move"]["score"] == 1.0 and d.cost_usd == 1.8e-05 and d.model.startswith("typesafe/")
    assert c.calls == 1 and abs(c.spent_usd - 1.8e-05) < 1e-12
