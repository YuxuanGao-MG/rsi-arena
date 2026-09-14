import json

import pytest

from rsi_arena.harness import Harness, Runner, Toolbox, tool
from tests.conftest import FakeLLM


def box():
    return Toolbox([
        tool("double", "doubles", {"type": "object", "properties": {"x": {"type": "number"}}})(
            lambda x: {"value": 2 * float(x)}),
        tool("boom", "fails")(lambda **a: (_ for _ in ()).throw(RuntimeError("down"))),
    ])


def harness(plan, tools=("double", "boom"), **cfg):
    return Harness.from_dict({"name": "t", "context": "sys", "tools": list(tools),
                              "config": {"max_usd": 1.0, **cfg}, "plan": {"steps": plan}})


async def test_tool_then_schema_prompt_writes_state_and_output():
    def script(messages, schema, tools):
        assert schema is not None
        return {"answer": messages[-1]["content"]}
    llm = FakeLLM(script)
    h = harness([
        {"type": "tool", "name": "d", "tool": "double", "args": {"x": "{{question}}"}, "output_key": "d"},
        {"type": "prompt", "name": "p", "prompt": "got {{d.value}}", "output_schema": {"type": "object"},
         "output_key": "out"},
    ])
    run = await Runner(llm, box()).run(h, question="21")
    assert run.ok, run.error
    assert run.state["d"] == {"value": 42.0}
    assert run.output == {"answer": "got 42.0"}
    assert llm.calls[0]["system"] == "sys"
    assert [s.kind for s in run.trace.spans] == ["step", "tool", "step", "llm"]


async def test_loop_stops_on_condition_and_restores_outer_state():
    llm = FakeLLM(lambda m, s, t: "x")
    h = harness([{
        "type": "loop", "name": "L", "max_loops": 5, "until": "loop_iteration >= 2", "output_key": "all",
        "steps": [{"type": "prompt", "name": "p", "prompt": "i={{loop_iteration}}", "output_key": "p"}],
    }])
    run = await Runner(llm, box()).run(h)
    assert run.ok
    assert run.state["all"] == ["x", "x"]
    assert "loop_iteration" not in run.state
    assert len(llm.calls) == 2


async def test_tool_failure_is_an_error_unless_fail_ok():
    llm = FakeLLM()
    failing = harness([{"type": "tool", "name": "b", "tool": "boom", "output_key": "b"}])
    run = await Runner(llm, box()).run(failing)
    assert not run.ok and run.error_kind == "plan" and "down" in run.error
    tolerant = harness([{"type": "tool", "name": "b", "tool": "boom", "output_key": "b", "fail_ok": True},
                        {"type": "prompt", "name": "p", "prompt": "{{b}}"}])
    run = await Runner(llm, box()).run(tolerant)
    assert run.ok and "error" in run.state["b"]


async def test_budget_stops_the_run_before_the_next_paid_step():
    llm = FakeLLM(lambda m, s, t: "x", cost=0.6)
    h = harness([{"type": "prompt", "name": "a", "prompt": "1", "output_key": "a"},
                 {"type": "prompt", "name": "b", "prompt": "2", "output_key": "b"},
                 {"type": "prompt", "name": "c", "prompt": "3", "output_key": "c"}], max_usd=1.0)
    run = await Runner(llm, box()).run(h)
    assert run.error_kind == "budget"
    assert len(llm.calls) == 2            # a spent 0.6, b spent 0.6 (over), c never ran
    assert run.output == "x"              # the last thing produced is kept


async def test_free_form_prompt_runs_a_tool_loop():
    turns = iter([
        {"tool_calls": [{"id": "1", "type": "function",
                         "function": {"name": "double", "arguments": json.dumps({"x": 4})}}]},
        "final",
    ])
    llm = FakeLLM(lambda m, s, t: next(turns))
    h = harness([{"type": "prompt", "name": "free", "prompt": "go", "tools": ["*"], "output_key": "out"}])
    run = await Runner(llm, box()).run(h)
    assert run.ok and run.output == "final"
    tool_msgs = [m for m in llm.calls[1]["messages"] if m.get("role") == "tool"]
    assert tool_msgs and json.loads(tool_msgs[0]["content"]) == {"value": 8.0}
    assert llm.calls[0]["tools"] and llm.calls[0]["tools"][0]["function"]["name"] == "double"


async def test_a_tool_outside_the_harness_list_is_refused_to_the_model():
    turns = iter([
        {"tool_calls": [{"id": "1", "type": "function",
                         "function": {"name": "boom", "arguments": "{}"}}]},
        "done",
    ])
    llm = FakeLLM(lambda m, s, t: next(turns))
    h = harness([{"type": "prompt", "name": "free", "prompt": "go", "tools": ["double"], "output_key": "o"}],
                tools=("double",))
    run = await Runner(llm, box()).run(h)
    assert run.ok
    refusal = [m for m in llm.calls[1]["messages"] if m.get("role") == "tool"][0]["content"]
    assert "not available" in refusal


# --- the client outlives the loop that made it -------------------------------


def test_the_client_survives_a_new_event_loop() -> None:
    """`run_sync` opens a fresh `asyncio.run` per call, so GEPA's tenth
    evaluation runs on its tenth loop while a cached client's sockets still
    belong to the first. Every real bench ended in `Event loop is closed` after
    printing its results — exit 1 on a run that worked, which on CI cannot be
    told from one that did not — and optimize would have hit the same thing
    partway through, with candidates already paid for.
    """
    import asyncio

    from rsi_arena.harness.llm import OpenRouter

    llm = OpenRouter(api_key="test", cache=False)

    async def touch() -> int:
        llm._http()                      # binds to whatever loop is running
        llm._limit()
        return id(asyncio.get_running_loop())

    first = asyncio.run(touch())
    second = asyncio.run(touch())
    assert first != second, "the loops really are different"
    asyncio.run(llm.close())             # must not raise


def test_closing_from_a_foreign_loop_is_a_no_op() -> None:
    """Closing a client whose loop is already gone is the error itself, so it
    drops the reference instead. The sockets belong to a dead loop; the process
    reclaims them, and a command is one process."""
    import asyncio

    from rsi_arena.harness.llm import OpenRouter

    llm = OpenRouter(api_key="test", cache=False)

    async def bind() -> None:
        llm._http()

    asyncio.run(bind())                  # bound to a loop that is now closed
    asyncio.run(llm.close())             # a different loop; must not raise
    assert llm._client is None
