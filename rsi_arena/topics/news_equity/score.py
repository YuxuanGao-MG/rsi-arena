"""The five-minute move on a stock, scored in basis points.

The argument is ``kalshi_horizon/score.py`` and the arithmetic is
``_common/metric.py``; this file is the unit. A tick is five basis points:
a large cap quotes one to five wide, so a move under five is inside the
spread and no-change is not wrong by it. A hundred basis points of edge is
a full point to the optimizer, which keeps the observed range of what a
forecast removes inside the value's linear region the way ten cents did on
Kalshi. Nothing is clamped: a return has no contract range.
"""

from __future__ import annotations

from typing import Any

from .._common.metric import Metric, MoveScore
from .._common.metric import pooled as _pooled
from .._common.metric import pooled_skill as _pooled_skill
from .._common.metric import score_output as _score_output

METRIC = Metric(tick=5.0, scale=100.0, unit="bps", relative=True, clamp=None,
                output_keys=("delta_bps", "half_width_bps"))


def score_output(output: Any, mid_now: float, realised: float) -> MoveScore | None:
    """None when the output carries no usable prediction."""
    return _score_output(output, mid_now, realised, METRIC)


def silent(mid_now: float, realised: float) -> MoveScore:
    return MoveScore.silent(mid_now, realised, METRIC)


def pooled_skill(scores: list[MoveScore]) -> float:
    return _pooled_skill(scores)


def pooled(scores: list[MoveScore]) -> dict[str, Any]:
    return _pooled(scores)


__all__ = ["METRIC", "MoveScore", "score_output", "silent", "pooled_skill", "pooled"]
