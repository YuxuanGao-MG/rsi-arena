import json
from datetime import timedelta
from pathlib import Path

from rsi_arena.bench import Window
from rsi_arena.harness import Harness
from rsi_arena.optimize import HorizonAdapter
from tests.conftest import FakeLLM

BASE = Path(__file__).resolve().parents[1] / "harnesses" / "horizon-5m.json"


def windows(t0):
    return [Window(ticker="A", at=t0 + timedelta(minutes=m), mid_now=0.40 + 0.01 * m,
                   realised=0.45 + 0.01 * m, game={"clock": f"{m}'"}) for m in (5, 10, 15)]


def test_evaluate_scores_each_window_and_builds_feedback(history, t0):
    llm = FakeLLM(lambda m, s, t: {"delta_cents": 5, "half_width_cents": 2, "confidence": 0.5,
                                   "driver": "drift", "falsifier": "none"})
    adapter = HorizonAdapter(Harness.load(BASE), llm, history=history)
    batch = adapter.evaluate(windows(t0), adapter.base.to_components(), capture_traces=True)
    assert [round(v, 6) for v in batch.scores] == [1.0, 1.0, 1.0]   # a perfect call every window
    assert round(batch.objective_scores[0]["skill"], 6) == 1.0
    assert batch.trajectories[0]["tools"][0]["tool"] == "market_quote"
    data = adapter.make_reflective_dataset(adapter.base.to_components(), batch, ["plan", "context"])
    assert set(data) == {"plan", "context"} and len(data["plan"]) == 3
    rec = data["plan"][0]
    assert "market printed" in rec["Feedback"] and "delta_cents" in rec["Feedback"]
    assert json.loads(rec["Generated Outputs"])["delta_cents"] == 5


def test_a_broken_candidate_scores_zero_with_the_reason(history, t0):
    adapter = HorizonAdapter(Harness.load(BASE), FakeLLM(), history=history)
    bad = {**adapter.base.to_components(), "tools": "market_quote, news_search"}
    batch = adapter.evaluate(windows(t0), bad, capture_traces=True)
    assert batch.scores == [0.0, 0.0, 0.0]
    assert "news_search" in batch.trajectories[0]["feedback"]
    worse = {**adapter.base.to_components(), "plan": "nope"}
    assert adapter.evaluate(windows(t0), worse).scores == [0.0, 0.0, 0.0]


def test_a_prediction_without_the_contract_is_told_so(history, t0):
    llm = FakeLLM(lambda m, s, t: {"move": 5})
    adapter = HorizonAdapter(Harness.load(BASE), llm, history=history)
    batch = adapter.evaluate(windows(t0), adapter.base.to_components(), capture_traces=True)
    assert batch.scores == [0.0, 0.0, 0.0]
    assert "delta_cents" in batch.trajectories[0]["feedback"]
