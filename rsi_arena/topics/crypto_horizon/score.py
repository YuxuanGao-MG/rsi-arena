"""The Kalshi score with the unit swapped: basis points of the price at the instant.

``kalshi_horizon/score.py`` is the argument, in full, for skill against
no-change with the benchmark floored at one tick, and ``_common/metric.py``
is that argument with the unit as a parameter. This file only says what the
unit is here.

A move is measured relative - in basis points of ``mid_now`` - because a
dollar on SOL at $140 and a dollar on BTC at $105,000 are not the same
event, and a one-minute forecast is a statement about a return.

Measured on the built question set (79,212 one-minute windows over 92 days
of BTC, ETH and SOL, June to September 2026): the absolute move has p50 2.8
bps, p75 5.7, p90 10.1, p95 13.7 and p99 25.3, with BTC the quietest (p50
2.2, p95 10.6) and SOL the loudest (3.9, 16.4). **The tick is two basis
points**: the BTC median, a little under the pooled one, and the floor on
the benchmark's error, so a window where the price did not move is scored
rather than excused and a call of movement on it is visibly wrong; 61% of
windows clear it. **Twenty basis points of edge is a full point** to the
optimizer: above the 95th percentile of a move, so the range of what a
forecast can remove stays inside the unclipped part of the scale even at
the 99th, which is the property the value's monotonicity in the pooled
statistic depends on. The Jev seed's levels sit on the same quantiles:
plus or minus 2, 6 and 15 bps at about p50, p75 and p95.
"""

from __future__ import annotations

from typing import Any

from ...crypto.replay import TICK_BPS
from .._common.metric import Metric, MoveScore, pooled, pooled_skill
from .._common.metric import score_output as _score_output

#: Twenty basis points of edge on one window is a full point to the optimizer.
VALUE_SCALE_BPS = 20.0

METRIC = Metric(tick=TICK_BPS, scale=VALUE_SCALE_BPS, unit="bps", relative=True, clamp=None,
                output_keys=("delta_bps", "half_width_bps"))

#: The same class the Kalshi topic calls ``WindowScore``, under that name too.
WindowScore = MoveScore


def score_output(output: Any, mid_now: float, realised: float) -> MoveScore | None:
    """None when the output carries no usable prediction."""
    return _score_output(output, mid_now, realised, METRIC)


def silent(mid_now: float, realised: float) -> MoveScore:
    """What a harness that said nothing would have scored."""
    return MoveScore.silent(mid_now, realised, METRIC)


def score_from_details(d: dict[str, Any]) -> MoveScore:
    """A score read back from an outcome's details, for the pooled statistic."""
    return MoveScore(mid_now=d["mid_now"], predicted=d["predicted"], realised=d["realised"],
                     half_width=d["half_width"], metric=METRIC)


__all__ = ["METRIC", "VALUE_SCALE_BPS", "WindowScore", "score_output", "silent", "score_from_details",
           "pooled", "pooled_skill"]
