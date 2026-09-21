"""One score for every "where does the price go in five minutes" topic.

``kalshi_horizon/score.py`` is the argument, in full, for why the metric is
skill against no-change with the benchmark floored at one tick, and why the
optimizer is shown ``0.5 + removed/2·scale`` rather than skill. None of that
depends on the price being a probability in cents. An equity on a headline and
a coin on a Sunday night ask the same question in a different unit, and this
file is that argument with the unit made a parameter.

Two kinds of unit. An **absolute** metric measures moves in cents of a price
that is itself a dollar fraction — Kalshi's — and keeps every price-level
quantity in the exchange's own units (dollars), exactly as ``WindowScore``
does, so a Kalshi rollout scored here is the same number to the last bit; the
tick and the scale are stated in cents and converted. A **relative** metric
measures everything in basis points of the mid at the instant, because a move
of a dollar means something different at $8 and at $800, and a five-minute
forecast on a stock or a coin is a statement about a return, not a level.

``Metric.KALSHI`` reproduces ``kalshi_horizon/score.py`` exactly and
``tests/test_move_metric.py`` holds it to that, case by case, to 1e-12. That
file stays as it is; this one is for the topics that come after it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: A move smaller than this, in the working unit, did not happen: the market
#: did not move, or the forecast echoed it. A hundredth of a cent for an
#: absolute metric, which is what ``score.py`` uses; a ten-thousandth of a
#: basis point for a relative one, which is to say exactly unchanged.
_NEGLIGIBLE = 1e-4


@dataclass(frozen=True)
class Metric:
    """What a topic's five-minute move is measured in.

    ``tick`` is the smallest move the venue can express and the floor on the
    benchmark's error; ``scale`` is the edge worth a full point to the
    optimizer. Both are in ``unit``. ``clamp`` bounds the predicted price for
    an absolute metric whose price is a probability; ``output_keys`` name the
    two numbers the harness's last step returns.
    """

    tick: float
    scale: float
    unit: str
    relative: bool
    clamp: tuple[float, float] | None
    output_keys: tuple[str, str]

    # -- unit arithmetic ----------------------------------------------------

    def _working(self, quantity: float) -> float:
        """A quantity in ``unit`` as the score works with it.

        Absolute: dollars, which is ``unit`` over a hundred. Relative: the unit
        is already the working unit, basis points.
        """
        return quantity if self.relative else quantity / 100

    @property
    def tick_working(self) -> float:
        return self._working(self.tick)

    @property
    def scale_working(self) -> float:
        return self._working(self.scale)

    def move(self, mid_now: float, price: float) -> float:
        """``price`` less ``mid_now``, in ``unit``. Signed."""
        if self.relative:
            return (price / mid_now - 1.0) * 1e4 if mid_now else 0.0
        return (price - mid_now) * 100

    def error_between(self, a: float, b: float, mid_now: float) -> float:
        """Distance between two prices in the working unit."""
        if self.relative:
            return abs(a - b) / mid_now * 1e4 if mid_now else 0.0
        return abs(a - b)

    def moved(self, mid_now: float, realised: float) -> bool:
        """Did the market move at least a tick between the instant and the horizon?"""
        return self.error_between(mid_now, realised, mid_now) >= self.tick_working

    def price_from(self, mid_now: float, delta: float) -> float:
        """The price a forecast of ``delta`` (in ``unit``) names, anchored on the mid."""
        if self.relative:
            return mid_now * (1.0 + delta / 1e4)
        predicted = mid_now + delta / 100
        if self.clamp is not None:
            lo, hi = self.clamp
            predicted = min(hi, max(lo, predicted))
        return predicted

    def scored_output(self, output: Any) -> tuple[float, float] | None:
        """``(delta, half_width)`` in ``unit`` from a harness's output, or None."""
        if not isinstance(output, dict):
            return None
        delta_key, width_key = self.output_keys
        delta = output.get(delta_key)
        if isinstance(delta, bool) or not isinstance(delta, (int, float)):
            return None
        width = output.get(width_key)
        width = float(width) if isinstance(width, (int, float)) and not isinstance(width, bool) else 0.0
        return float(delta), width

    def to_dict(self) -> dict[str, Any]:
        return {"tick": self.tick, "scale": self.scale, "unit": self.unit,
                "relative": self.relative, "clamp": list(self.clamp) if self.clamp else None,
                "output_keys": list(self.output_keys)}


#: Kalshi: a probability in cents, one-cent ticks, ten cents of edge to a full
#: point, the predicted price clamped inside the contract's range. The same
#: numbers ``kalshi_horizon/score.py`` carries as constants.
Metric.KALSHI = Metric(tick=1.0, scale=10.0, unit="cents", relative=False,  # type: ignore[attr-defined]
                       clamp=(0.01, 0.99), output_keys=("delta_cents", "half_width_cents"))


@dataclass(frozen=True)
class MoveScore:
    """One window, scored. The same properties as ``WindowScore``, plus the unit.

    ``mid_now``, ``predicted`` and ``realised`` are prices as the venue prints
    them. ``half_width`` and every derived quantity are in the metric's working
    unit: dollars for an absolute metric, basis points for a relative one.
    """

    mid_now: float
    predicted: float
    realised: float
    half_width: float
    metric: Metric = field(compare=False)

    @property
    def error(self) -> float:
        return self.metric.error_between(self.predicted, self.realised, self.mid_now)

    @property
    def naive_error(self) -> float:
        return self.metric.error_between(self.mid_now, self.realised, self.mid_now)

    @property
    def unmeasurable(self) -> bool:
        """The market did not move. Still scored — kept for reporting."""
        return self.naive_error < _NEGLIGIBLE

    @property
    def benchmark(self) -> float:
        """No-change's error, floored at a tick. Never zero, so skill is defined."""
        return max(self.naive_error, self.metric.tick_working)

    @property
    def skill(self) -> float:
        """Fraction of the benchmark's error removed. Zero is exactly silence.

        Numerator unfloored, denominator floored: ``score.py`` explains why the
        other three combinations each scored silence wrongly.
        """
        return (self.naive_error - self.error) / self.benchmark

    @property
    def echoed(self) -> bool:
        return self.metric.error_between(self.predicted, self.mid_now, self.mid_now) < _NEGLIGIBLE

    @property
    def covered(self) -> bool:
        return self.error <= self.half_width

    @property
    def moved(self) -> bool:
        return self.naive_error >= self.metric.tick_working

    @property
    def removed(self) -> float:
        """Error the benchmark made, less the error the forecast made."""
        return self.naive_error - self.error

    @property
    def value(self) -> float:
        """What the optimizer sees: affine in :attr:`removed`, clipped to [0, 1]."""
        return max(0.0, min(1.0, 0.5 + self.removed / (2 * self.metric.scale_working)))

    @classmethod
    def silent(cls, mid_now: float, realised: float, metric: Metric) -> "MoveScore":
        """What a harness that said nothing would have scored."""
        return cls(mid_now=mid_now, predicted=mid_now, realised=realised, half_width=0.0,
                   metric=metric)

    def to_dict(self) -> dict[str, Any]:
        return {"mid_now": self.mid_now, "predicted": self.predicted, "realised": self.realised,
                "half_width": self.half_width,
                "error": round(self.error, 4), "naive_error": round(self.naive_error, 4),
                "skill": round(self.skill, 4), "echoed": self.echoed, "covered": self.covered,
                "unmeasurable": self.unmeasurable, "unit": self.metric.unit}


def score_output(output: Any, mid_now: float, realised: float, metric: Metric) -> MoveScore | None:
    """None when the output carries no usable prediction."""
    read = metric.scored_output(output)
    if read is None:
        return None
    if metric.relative and not mid_now:
        return None                      # a return on a price of zero is not a number
    delta, width = read
    predicted = metric.price_from(mid_now, delta)
    if metric.relative:
        return MoveScore(mid_now=mid_now, predicted=predicted, realised=realised,
                         half_width=max(0.0, width), metric=metric)
    # Absolute: the quote is a band inside the contract's range, and the width
    # kept is half of what survived the clip — as ``quote_from`` has it.
    half = max(0.0, width) / 100
    low, high = max(0.0, predicted - half), min(1.0, predicted + half)
    return MoveScore(mid_now=mid_now, predicted=predicted, realised=realised,
                     half_width=(high - low) / 2, metric=metric)


def pooled_skill(scores: list[MoveScore]) -> float:
    """Sum the errors first, so a quiet window cannot dominate a ratio."""
    if not scores:
        return 0.0
    benchmark = sum(s.benchmark for s in scores)
    removed = sum(s.naive_error - s.error for s in scores)
    return removed / benchmark if benchmark else 0.0


def pooled(scores: list[MoveScore]) -> dict[str, Any]:
    n = len(scores)
    if not n:
        return {"skill": 0.0, "skill_on_moves": 0.0, "mae": 0.0, "naive_mae": 0.0,
                "echoed": 0, "coverage": 0.0, "moved": 0, "unmeasurable": 0}
    moved = [s for s in scores if s.moved]
    return {
        "skill": round(pooled_skill(scores), 4),
        "skill_on_moves": round(pooled_skill(moved), 4),
        "mae": round(sum(s.error for s in scores) / n, 4),
        "naive_mae": round(sum(s.naive_error for s in scores) / n, 4),
        "echoed": sum(1 for s in scores if s.echoed),
        "coverage": round(sum(1 for s in scores if s.covered) / n, 4),
        "moved": len(moved),
        "unmeasurable": sum(1 for s in scores if s.unmeasurable),
    }


__all__ = ["Metric", "MoveScore", "score_output", "pooled_skill", "pooled"]
