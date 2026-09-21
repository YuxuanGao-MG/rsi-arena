"""A chat model as a tool, chosen by the optimizer like any other name.

A decisions model cannot reason in text, so a plan for it may run one more
tool step whose answer is a paragraph from Opus. The step is a tool to the
plan and a model call to the ledger, and both have to be true at once.
"""

import json
from pathlib import Path

import pytest

from rsi_arena.harness import (Harness, LLMError, ModelTool, Runner, Toolbox, model_tool_names,
                               tool, with_model_tools)
from tests.conftest import FakeLLM

JEV = Path(__file__).resolve().parents[1] / "harnesses" / "horizon-5m-jev.json"

MOVE = {"type": "score", "instructions": "cents moved", "criteria": ["down", "flat", "up"],
        "values": [-2, 0, 2]}


def box():
    return Toolbox([tool("double", "doubles", {"type": "object", "properties": {"x": {"type": "number"}}})(
        lambda x: {"value": 2 * float(x)})])


def harness(plan, tools, model="anthropic/claude-sonnet-4.5", **cfg):
    return Harness.from_dict({"name": "t", "context": "sys", "tools": list(tools),
                              "config": {"model": model, "max_usd": 1.0, **cfg},
                              "plan": {"steps": plan}})


ASK = {"type": "tool", "name": "opinion", "tool": "ask_opus", "output_key": "opinion",
       "args": {"prompt": "read {{question}}", "system": "be brief"}}


async def test_a_plan_step_asks_opus_and_the_answer_is_charged():
    llm = FakeLLM(lambda m, s, t: "the book is thin", cost=0.03)
    h = harness([ASK, {"type": "prompt", "name": "p", "prompt": "{{opinion.text}}", "output_key": "out"}],
                tools=["ask_opus"])
    run = await Runner(llm, box()).run(h, question="A")
    assert run.ok, run.error
    assert run.state["opinion"] == {"text": "the book is thin", "model": "anthropic/claude-opus-5",
                                    "cost_usd": 0.03}
    assert llm.calls[0]["model"] == "anthropic/claude-opus-5" and llm.calls[0]["system"] == "be brief"
    assert llm.calls[0]["messages"] == [{"role": "user", "content": "read A"}]
    assert llm.calls[1]["messages"][0]["content"] == "the book is thin", "later steps read {{opinion.text}}"
    span = next(s for s in run.trace.spans if s.kind == "tool")
    assert span.name == "ask_opus" and span.cost_usd == 0.03 and span.status == "ok"
    assert run.cost_usd == pytest.approx(0.06), "the opinion costs what a prompt step costs"
    assert run.tools_seen()[0]["tool"] == "ask_opus", "the rewriter sees the delegation"


async def test_an_opus_call_past_the_ceiling_is_refused_like_a_prompt_step():
    llm = FakeLLM(lambda m, s, t: "x", cost=0.6)
    h = harness([ASK, {"type": "prompt", "name": "p", "prompt": "{{opinion.text}}"}],
                tools=["ask_opus"], max_usd=0.5)
    run = await Runner(llm, box()).run(h)
    assert run.error_kind == "budget", run.error
    assert len(llm.calls) == 1, "the prompt step after it never ran"
    assert next(s for s in run.trace.spans if s.kind == "tool").cost_usd == 0.6, "the money is in the trace"


async def test_a_free_form_prompt_may_call_a_model_tool_as_a_function():
    turns = iter([
        {"tool_calls": [{"id": "1", "type": "function",
                         "function": {"name": "ask_sonnet", "arguments": json.dumps({"prompt": "why?"})}}]},
        "because",
        "final",
    ])
    llm = FakeLLM(lambda m, s, t: next(turns), cost=0.01)
    h = harness([{"type": "prompt", "name": "free", "prompt": "go", "tools": ["*"], "output_key": "out"}],
                tools=["ask_sonnet"])
    run = await Runner(llm, box()).run(h)
    assert run.ok and run.output == "final"
    assert llm.calls[0]["tools"][0]["function"]["name"] == "ask_sonnet"
    assert llm.calls[1]["model"] == "anthropic/claude-sonnet-4.5" and llm.calls[1]["tools"] is None
    reply = [m for m in llm.calls[2]["messages"] if m.get("role") == "tool"][0]
    assert reply["content"] == "because"
    assert run.cost_usd == pytest.approx(0.03)


async def test_a_jev_plan_reads_the_opinion_before_it_answers():
    """market-like tool step -> ask_opus -> questions step, end to end on the fake."""
    def decisions(state, questions):
        assert state.startswith("sys\n\n"), "the context still leads the state"
        assert "got 42.0" in state and "OPUS: drifting up" in state
        return {"move": {"type": "score", "score": 2.0, "confidence": 0.9,
                         "probabilities": {"0": 0.0, "1": 0.0, "2": 1.0}}}
    llm = FakeLLM(lambda m, s, t: "OPUS: drifting up", decisions=decisions, cost=0.03)
    h = harness([
        {"type": "tool", "name": "d", "tool": "double", "args": {"x": "21"}, "output_key": "d"},
        {"type": "tool", "name": "opinion", "tool": "ask_opus", "output_key": "opinion",
         "args": {"prompt": "the book doubled to {{d.value}}; where next?"}},
        {"type": "prompt", "name": "predict", "output_key": "out",
         "prompt": "got {{d.value}}\nOpinion: {{opinion.text}}",
         "questions": {"move": MOVE},
         "answers": {"delta_cents": {"from": "move", "as": "mean"}}},
    ], tools=["double", "ask_opus"], model="typesafe/jev-1.13")
    run = await Runner(llm, box()).run(h)
    assert run.ok, run.error
    assert run.output["delta_cents"] == 2.0
    assert llm.calls[0]["model"] == "anthropic/claude-opus-5"
    assert "doubled to 42.0" in llm.calls[0]["messages"][0]["content"]
    assert llm.decided[0]["model"] == "typesafe/jev-1.13"
    assert [s.kind for s in run.trace.spans] == ["step", "tool", "step", "tool", "step", "llm"]
    assert run.cost_usd == pytest.approx(0.03 + 0.03 / 1000)


def test_a_decisions_model_cannot_be_a_tool():
    with pytest.raises(ValueError, match="writes no text"):
        ModelTool("ask_jev", "typesafe/jev-1.13", FakeLLM())


def test_the_model_tools_ride_beside_the_frozen_box_without_entering_it(history, t0):
    """The frozen box is the host's statement of the instant; the model tools
    are the runner's. A harness may name both, and the replay box stays what
    ``replay_tools`` says it is."""
    from rsi_arena.kalshi.replay import replay_tools

    frozen = replay_tools(t0, history)
    assert not set(model_tool_names()) & set(frozen)
    merged = with_model_tools(frozen, FakeLLM())
    assert set(model_tool_names()) <= set(merged) and set(frozen) <= set(merged)
    assert not set(model_tool_names()) & set(frozen), "merging copies; it does not widen the original"
    assert Runner(FakeLLM(), frozen, model_tools=False).toolbox is frozen


def test_a_model_tool_answers_synchronously_for_a_caller_with_no_loop():
    out = ModelTool("ask_opus", "anthropic/claude-opus-5", FakeLLM(lambda m, s, t: "yes")).safe_call(prompt="?")
    assert out.ok and out.text == "yes" and out.cost_usd == 0.001


async def test_a_provider_failure_in_a_model_tool_is_a_provider_failure():
    """A refused call is the host's fault, not text for the model to read."""
    class Refusing(FakeLLM):
        async def complete(self, *a, **k):
            raise LLMError(402, "no credit")

    h = harness([ASK, {"type": "prompt", "name": "p", "prompt": "{{opinion.text}}"}], tools=["ask_opus"])
    run = await Runner(Refusing(), box()).run(h)
    assert run.error_kind == "provider" and "no credit" in run.error


async def test_the_loop_checks_a_delegating_harness_against_the_merged_box(t0, history):
    """``evaluate`` checks the harness before any window runs, and the box it
    checks against has to be the one the runner holds - or a plan that names
    ask_opus fails every instance at load with 'tools not available'."""
    from datetime import timedelta

    from rsi_arena.loop import evaluate
    from rsi_arena.loop.adapter import reflection_templates
    from rsi_arena.topics.kalshi_horizon import KalshiHorizon, Window

    data = json.loads(JEV.read_text())
    data["tools"].append("ask_opus")
    data["plan"]["steps"].insert(3, {"type": "tool", "name": "opinion", "tool": "ask_opus",
                                     "output_key": "opinion",
                                     "args": {"prompt": "Path: {{path}}\nTape: {{tape}}"}})
    data["plan"]["steps"][-1]["prompt"] += "\nOpinion: {{opinion.text}}"
    h = Harness.from_dict(data)
    windows = [Window(ticker="A", at=t0 + timedelta(minutes=5 * k), mid_now=0.40 + 0.05 * k,
                      realised=0.45 + 0.05 * k, game={"clock": f"{5 * k}'"}, event="E")
               for k in range(1, 4)]
    tk = KalshiHorizon(history=history, windows=windows)
    llm = FakeLLM(lambda m, s, t: "thin book, drifting", cost=0.03)
    rollouts = await evaluate(tk, h, tk.instances(), llm)
    assert all(r.run.ok for r in rollouts), [r.run.error for r in rollouts]
    assert all(any(t["tool"] == "ask_opus" for t in r.run.tools_seen()) for r in rollouts)
    assert all("thin book" in d["state"] for d in llm.decided)
    assert all(r.cost_usd == pytest.approx(0.03 + 0.03 / 1000) for r in rollouts), \
        "the window is priced at the opinion plus the decision"
    # The rewriter is told it may delegate.
    assert set(model_tool_names()) <= set(tk.tools())
    assert "ask_opus" in reflection_templates(tk, h)["tools"]
