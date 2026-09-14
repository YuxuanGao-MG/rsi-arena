"""Turn a five-minute forecast into a number worth ranking on.

The benchmark is no change. Predicting the price stays put is free and nearly
always nearly right, so absolute error rewards it. The score is skill against
no-change: the fraction of the benchmark's error the forecast removed. Zero is
worth exactly as much as silence; negative is worse than silence.

A window is reported to the optimizer as ``0.5 + skill/2`` clipped to [0, 1].

**The benchmark's error is floored at one tick**, and that floor is the whole
reason this file is worth reading. Without it the two halves of the loop
disagreed about the quarter of windows where the market did not move. The
per-window score called skill undefined there and handed back a flat 0.5
whatever the harness had said; the pooled statistic added that window's error to
its numerator and nothing to its denominator, so the same window could only ever
hurt. The optimizer therefore climbed a hill the gate could not see, and the
first thousand-call run proved it: GEPA raised the mean per-window score by
0.020 while the pooled statistic it would be judged on fell by 0.044. It had
followed its own correct reflection — "you are rewarded only for anticipating
moves that actually happen" — and started predicting movement on 27 of 29 dead
markets, which the objective it could see scored as free.

A market that moved less than a cent did not move, and a forecast wrong by less
than a cent is right. Flooring the denominator at a tick says both, bounds what
a quiet window can cost, and makes predicting a move on a dead market
*visibly* wrong to the optimizer rather than invisibly wrong to the gate. On the
run that found this, the floor flips the per-window verdict from +0.020 to
-0.118, agreeing with the pooled -0.041 instead of contradicting it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

_NEGLIGIBLE = 1e-4

#: One tick. The smallest move the exchange can express, and therefore the
#: smallest benchmark error that means anything. Used as a floor on the
#: denominator so that a window where nothing happened is scored rather than
#: excused, and cannot cost an unbounded amount.
_TICK = 0.01


def quote_from(mid: float, delta_cents: float, half_width_cents: float) -> tuple[float, float, float]:
    """The model's change and width, anchored on the exchange's mid, clamped to (0, 1)."""
    predicted = min(0.99, max(0.01, mid + delta_cents / 100))
    width = max(0.0, half_width_cents) / 100
    return predicted, max(0.0, predicted - width), min(1.0, predicted + width)


@dataclass(frozen=True)
class WindowScore:
    mid_now: float
    predicted: float
    realised: float
    half_width: float

    @property
    def error(self) -> float:
        return abs(self.predicted - self.realised)

    @property
    def naive_error(self) -> float:
        return abs(self.mid_now - self.realised)

    @property
    def unmeasurable(self) -> bool:
        """The market did not move. Still scored — kept for reporting, because a
        pooled number over mostly-quiet windows deserves the caveat."""
        return self.naive_error < _NEGLIGIBLE

    @property
    def benchmark(self) -> float:
        """No-change's error, floored at a tick. Never zero, so skill is defined."""
        return max(self.naive_error, _TICK)

    @property
    def skill(self) -> float:
        """Fraction of the benchmark's error removed. Negative is worse than silence.

        Defined on every window, including the ones where the market did not
        move: there, saying nothing is right and scores well, and predicting a
        move is wrong and costs — which is the distinction the old form could
        not make.
        """
        return 1 - self.error / self.benchmark

    @property
    def echoed(self) -> bool:
        return abs(self.predicted - self.mid_now) < _NEGLIGIBLE

    @property
    def covered(self) -> bool:
        return abs(self.realised - self.predicted) <= self.half_width

    @property
    def value(self) -> float:
        return max(0.0, min(1.0, 0.5 + self.skill / 2))

    @classmethod
    def silent(cls, mid_now: float, realised: float) -> "WindowScore":
        """What a harness that said nothing would have scored. Used for runs that produced no forecast."""
        return cls(mid_now=mid_now, predicted=mid_now, realised=realised, half_width=0.0)

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out.update(error=round(self.error, 4), naive_error=round(self.naive_error, 4),
                   skill=round(self.skill, 4), echoed=self.echoed, covered=self.covered,
                   unmeasurable=self.unmeasurable)
        return out


def score_output(output: Any, mid_now: float, realised: float) -> WindowScore | None:
    """None when the output carries no usable prediction."""
    if not isinstance(output, dict):
        return None
    delta = output.get("delta_cents")
    if isinstance(delta, bool) or not isinstance(delta, (int, float)):
        return None
    width = output.get("half_width_cents")
    width = float(width) if isinstance(width, (int, float)) and not isinstance(width, bool) else 0.0
    predicted, low, high = quote_from(mid_now, float(delta), width)
    return WindowScore(mid_now=mid_now, predicted=predicted, realised=realised, half_width=(high - low) / 2)


def pooled_skill(scores: list[WindowScore]) -> float:
    """Sum the errors first, so a quiet window cannot dominate a ratio.

    The same floor as :attr:`WindowScore.skill`, and for the same reason: the
    two must be the same function, or the optimizer is climbing a hill the gate
    does not measure.
    """
    if not scores:
        return 0.0
    benchmark = sum(s.benchmark for s in scores)
    return 1 - sum(s.error for s in scores) / benchmark


def pooled(scores: list[WindowScore]) -> dict[str, Any]:
    n = len(scores)
    if not n:
        return {"skill": 0.0, "skill_on_moves": 0.0, "mae": 0.0, "naive_mae": 0.0,
                "echoed": 0, "coverage": 0.0, "moved": 0, "unmeasurable": 0}
    moved = [s for s in scores if s.naive_error >= 0.01]
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
