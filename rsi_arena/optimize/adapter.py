"""The benchmark as GEPA sees it.

GEPA hands us a candidate (the harness as text components) and a batch of
windows, and wants a score per window plus, when asked, a trajectory it can
reflect on. Everything about *how* to mutate lives in GEPA; everything about
*what a good forecast is* lives here.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from gepa.core.adapter import EvaluationBatch, GEPAAdapter

from ..bench.evaluate import WINDOW_INPUTS, WindowResult, evaluate_windows
from ..bench.windows import Window
from ..harness import LLM, Harness, HarnessError
from ..harness.llm import run_sync
from ..kalshi._history import History
from ..kalshi.replay import ToolCache, replay_tools

#: What the optimizer is told a harness must produce, repeated in every feedback
#: record so a rewrite that drops the contract learns why it scored zero.
CONTRACT = ("The harness's last step must return a JSON object with numeric delta_cents "
            "(how many cents the mid moves over the next five minutes; 0 is a real answer) "
            "and half_width_cents (half the width of the two-sided quote it would stand behind). "
            "It may only read the inputs {{question}} (the contract ticker) and {{game}} "
            "(score, clock, recent events). Only the tools market_quote, candlesticks and "
            "previous_trades exist; nothing else can be frozen at a past instant.")


class HorizonAdapter(GEPAAdapter[Window, dict, dict]):
    def __init__(self, base: Harness, llm: LLM, *, history: History | None = None,
                 tool_cache: ToolCache | None = None, concurrency: int = 4,
                 evaluate=evaluate_windows) -> None:
        self.base, self.llm = base, llm
        self.history, self.tool_cache, self.concurrency = history, tool_cache, concurrency
        self._evaluate = evaluate
        self.evaluations = 0

    def harness(self, candidate: dict[str, str], at=None) -> Harness:
        """The candidate as a harness, refused now if it could not run on a window."""
        harness = self.base.from_components(candidate)
        if at is not None:
            harness.check(replay_tools(at, self.history, self.tool_cache), inputs=set(WINDOW_INPUTS))
        return harness

    def evaluate(self, batch: list[Window], candidate: dict[str, str],
                 capture_traces: bool = False) -> EvaluationBatch[dict, dict]:
        self.evaluations += len(batch)
        try:
            harness = self.harness(candidate, at=batch[0].at if batch else None)
        except HarnessError as exc:
            reason = f"harness rejected: {exc}"
            return EvaluationBatch(
                outputs=[{"error": reason} for _ in batch], scores=[0.0 for _ in batch],
                trajectories=[{"window": w.to_dict(), "error": reason, "feedback": reason}
                              for w in batch] if capture_traces else None,
                objective_scores=[{"skill": 0.0, "cost": 0.0} for _ in batch])
        results: list[WindowResult] = run_sync(self._evaluate(
            harness, batch, self.llm, history=self.history, tool_cache=self.tool_cache,
            concurrency=self.concurrency))
        outputs = [r.output if isinstance(r.output, dict) else {"output": r.output} for r in results]
        scores = [r.value for r in results]
        objective = [{"skill": max(0.0, min(1.0, r.value)), "cost": max(0.0, 1.0 - r.cost_usd / 0.05)}
                     for r in results]
        trajectories = [self._trajectory(r) for r in results] if capture_traces else None
        return EvaluationBatch(outputs=outputs, scores=scores, trajectories=trajectories,
                               objective_scores=objective)

    @staticmethod
    def _trajectory(r: WindowResult) -> dict[str, Any]:
        tools_seen = []
        if r.run is not None:
            for sp in r.run.trace.spans:
                if sp.kind == "tool":
                    tools_seen.append({"tool": sp.name, "args": sp.input,
                                       "answer": _clip(sp.output, 1200)})
        return {"window": r.window.to_dict(), "output": r.output, "score": r.value,
                "feedback": r.feedback(), "tools": tools_seen,
                "trace": r.run.trace.render() if r.run is not None else "",
                "error": r.error}

    def make_reflective_dataset(self, candidate: dict[str, str], eval_batch: EvaluationBatch[dict, dict],
                                components_to_update: list[str]) -> Mapping[str, Sequence[Mapping[str, Any]]]:
        records = []
        for traj, score in zip(eval_batch.trajectories or [], eval_batch.scores):
            w = traj.get("window", {})
            records.append({
                "Inputs": {"ticker": w.get("ticker"), "at": w.get("at"), "mid_now": w.get("mid_now"),
                           "game": json.dumps(w.get("game", {}))[:600],
                           "tool_answers": json.dumps(traj.get("tools", []), default=str)[:2500]},
                "Generated Outputs": json.dumps(traj.get("output"), default=str)[:1500],
                "Feedback": f"{traj.get('feedback', '')} Score {score:.3f}. Contract: {CONTRACT}",
                "score": score,
            })
        return {component: records for component in components_to_update}


def _clip(value: Any, limit: int) -> Any:
    if value is None:
        return None
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text if len(text) <= limit else text[:limit] + "…"
