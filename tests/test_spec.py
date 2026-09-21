import json
from pathlib import Path

import pytest

from rsi_arena.harness import Harness, HarnessError, Toolbox, tool

BASE = Path(__file__).resolve().parents[1] / "harnesses" / "horizon-5m.json"


def test_base_harness_loads_from_the_earlier_runtime_format():
    h = Harness.load(BASE)
    assert h.name == "kalshi-horizon-5m"
    # The point is that `default_model` is read at all — the earlier runtime
    # wrote it under that name and this one calls it `model`. Which model it
    # names is a choice that changes; that it survives the rename is the
    # contract.
    written = json.loads(Path(BASE).read_text())["config"]["default_model"]
    assert h.config.model == written and "default_model" not in h.to_dict()["config"]
    assert h.tools == ["market_quote", "candlesticks", "previous_trades"]
    assert [s.type for s in h.plan.steps] == ["tool", "tool", "tool", "prompt"]
    assert h.plan.required_inputs() == {"game"}


def test_components_round_trip():
    h = Harness.load(BASE)
    parts = h.to_components()
    assert set(parts) == {"context", "plan", "tools", "model"}
    again = h.from_components(parts)
    assert again.plan.model_dump() == h.plan.model_dump()
    assert again.tools == h.tools and again.context == h.context


def test_bad_plan_json_fails_at_load_with_a_readable_message():
    h = Harness.load(BASE)
    with pytest.raises(HarnessError, match="plan is not valid JSON"):
        h.from_components({"plan": "{not json"})
    with pytest.raises(HarnessError, match="invalid harness"):
        h.from_components({"plan": json.dumps({"steps": [{"type": "prompt"}]})})


def test_check_refuses_unknown_tools_and_unmet_inputs():
    h = Harness.load(BASE)
    box = Toolbox([tool("market_quote", "")(lambda **a: {}), tool("candlesticks", "")(lambda **a: {}),
                   tool("previous_trades", "")(lambda **a: {})])
    h.check(box, inputs={"question", "game"})
    with pytest.raises(HarnessError, match="plan reads game"):
        h.check(box, inputs={"question"})
    h2 = h.from_components({"tools": "market_quote, game_state"})
    with pytest.raises(HarnessError, match="tools not available: game_state"):
        h2.check(box)
    h3 = h.from_components({"tools": "market_quote"})
    with pytest.raises(HarnessError, match="plan calls tools the harness does not list"):
        h3.check(box)
