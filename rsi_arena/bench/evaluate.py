"""Run a harness over windows and score each one."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

from ..harness import LLM, Harness, HarnessError, Run, Runner
from ..kalshi._history import History
from ..kalshi.replay import ToolCache, replay_tools
from .score import WindowScore, pooled, score_window, window_value
from .windows import Window

#: The inputs every window supplies to a plan. A plan may read these and no others.
WINDOW_INPUTS = frozenset({"question", "game"})


@dataclass
class WindowResult:
    window: Window
    run: Run | None
    score: WindowScore | None
    value: float
    cost_usd: float
    error: str | None = None

    @property
    def output(self) -> Any:
        return self.run.output if self.run is not None else None

    def feedback(self) -> str:
        """What the optimizer is told about this window. Written, not dumped."""
        w = self.window
        lines = [f"Contract {w.ticker} at {w.at.isoformat()[:16]}Z, mid then {w.mid_now:.3f}, "
                 f"market printed {w.realised:.3f} five minutes later "
                 f"(no-change would miss by {abs(w.realised - w.mid_now):.3f})."]
        if self.run is None and self.error:
            lines.append(f"The harness could not run: {self.error}")
        elif self.run is not None and self.run.error:
            lines.append(f"The run failed ({self.run.error_kind}): {self.run.error}")
        if self.score is None:
            lines.append("No usable prediction: the final output must be a JSON object with numeric "
                         "delta_cents (the move, in cents) and half_width_cents. Score 0.")
        else:
            s = self.score
            lines.append(f"Predicted {s.predicted:.3f} (delta {100 * (s.predicted - s.mid_now):+.1f}c, "
                         f"half-width {100 * s.half_width:.1f}c); missed by {s.error:.3f}; "
                         f"skill vs no-change {s.skill:+.2f}"
                         + (" (market did not move: unmeasurable)" if s.unmeasurable else "")
                         + ("; echoed the current mid exactly" if s.echoed else "")
                         + ("; realised price inside the quote" if s.covered else "; realised price outside the quote"))
        lines.append(f"Cost ${self.cost_usd:.4f}.")
        if self.run is not None:
            failed = [sp for sp in self.run.trace.spans if sp.kind == "tool" and sp.status == "error"]
            if failed:
                lines.append("Tool errors: " + "; ".join(f"{sp.name}: {sp.error}" for sp in failed))
        return " ".join(lines)


async def evaluate_windows(harness: Harness, windows: list[Window], llm: LLM, *,
                           history: History | None = None, tool_cache: ToolCache | None = None,
                           concurrency: int = 4) -> list[WindowResult]:
    """One run per window, tools frozen at that window's instant."""
    hist = history or History()
    gate = asyncio.Semaphore(concurrency)

    async def one(window: Window) -> WindowResult:
        box = replay_tools(window.at, hist, tool_cache)
        try:
            harness.check(box, inputs=set(WINDOW_INPUTS))
        except HarnessError as exc:
            return WindowResult(window=window, run=None, score=None, value=0.0, cost_usd=0.0,
                                error=str(exc))
        runner = Runner(llm, box)
        async with gate:
            run = await runner.run(harness, question=window.ticker,
                                   game=json.dumps(window.game)[:1200])
        score = score_window(run.output, window)
        return WindowResult(window=window, run=run, score=score, value=window_value(score),
                            cost_usd=run.cost_usd, error=run.error)

    return await asyncio.gather(*(one(w) for w in windows))


def summarise(results: list[WindowResult]) -> dict[str, Any]:
    scores = [r.score for r in results if r.score is not None]
    out = pooled(scores)
    out.update({
        "windows": len(results),
        "unscored": sum(1 for r in results if r.score is None),
        "failed_runs": sum(1 for r in results if r.run is not None and not r.run.ok),
        "mean_value": round(sum(r.value for r in results) / len(results), 4) if results else 0.0,
        "cost_usd": round(sum(r.cost_usd for r in results), 4),
        "cost_per_window": round(sum(r.cost_usd for r in results) / len(results), 5) if results else 0.0,
    })
    return out
