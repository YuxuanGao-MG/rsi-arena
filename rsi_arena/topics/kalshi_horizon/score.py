"""Turn a five-minute forecast into a number worth ranking on.

The benchmark is no change. Predicting the price stays put is free and nearly
always nearly right, so absolute error rewards it. The score is skill against
no-change: the fraction of the benchmark's error the forecast removed. Zero is
worth exactly as much as silence; negative is worse than silence.

A window is reported to the optimizer as ``0.5 + skill/2`` clipped to [0, 1].
Where the market did not move, skill is undefined: the window is worth 0.5 and
flagged, so a pooled number can drop it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

_NEGLIGIBLE = 1e-4


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
        return self.naive_error < _NEGLIGIBLE

    @property
    def skill(self) -> float:
        return 0.0 if self.unmeasurable else 1 - self.error / self.naive_error

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
    """Sum the errors first, so a quiet window cannot dominate a ratio."""
    naive = sum(s.naive_error for s in scores)
    return 1 - sum(s.error for s in scores) / naive if naive else 0.0


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
