"""Turn a five-minute forecast into a number worth ranking on.

The benchmark is no change. Predicting the price stays put is free and nearly
always nearly right, so absolute error rewards it. The score is skill against
no-change: the fraction of the benchmark's error the forecast removed. Zero is
worth exactly as much as silence, negative is worse than silence.

A window is reported as ``0.5 + skill/2`` clipped to [0, 1], so the optimizer
sees half a point for matching the benchmark. On a window where the market did
not move, skill is undefined; it is reported as 0.5 and flagged so a pooled
number can drop it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .windows import Window

_NEGLIGIBLE = 1e-4


def quote_from(mid: float, delta_cents: float, half_width_cents: float) -> tuple[float, float, float]:
    """The model's change and width, anchored on the exchange's mid, clamped to (0, 1)."""
    predicted = min(0.99, max(0.01, mid + delta_cents / 100))
    width = max(0.0, half_width_cents) / 100
    return predicted, max(0.0, predicted - width), min(1.0, predicted + width)


@dataclass(frozen=True)
class WindowScore:
    window_id: str
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
        if self.unmeasurable:
            return 0.0
        return 1 - self.error / self.naive_error

    @property
    def echoed(self) -> bool:
        return abs(self.predicted - self.mid_now) < _NEGLIGIBLE

    @property
    def covered(self) -> bool:
        return abs(self.realised - self.predicted) <= self.half_width

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out.update(error=round(self.error, 4), naive_error=round(self.naive_error, 4),
                   skill=round(self.skill, 4), echoed=self.echoed, covered=self.covered,
                   unmeasurable=self.unmeasurable)
        return out


def score_window(output: Any, window: "Window") -> WindowScore | None:
    """None when the output carries no usable prediction."""
    if not isinstance(output, dict):
        return None
    delta = output.get("delta_cents")
    if isinstance(delta, bool) or not isinstance(delta, (int, float)):
        return None
    width = output.get("half_width_cents")
    width = width if isinstance(width, (int, float)) and not isinstance(width, bool) else 0.0
    predicted, low, high = quote_from(window.mid_now, float(delta), float(width))
    return WindowScore(window_id=window.id, mid_now=window.mid_now, predicted=predicted,
                       realised=window.realised, half_width=(high - low) / 2)


def window_value(score: WindowScore | None) -> float:
    """What one window contributes to the optimizer's objective, in [0, 1]."""
    if score is None:
        return 0.0
    return max(0.0, min(1.0, 0.5 + score.skill / 2))


def pooled(scores: list[WindowScore]) -> dict[str, Any]:
    """Pooled, not averaged: sum the errors first, so a quiet window cannot dominate."""
    n = len(scores)
    if not n:
        return {"n": 0, "skill": 0.0, "skill_on_moves": 0.0, "mae": 0.0, "naive_mae": 0.0,
                "echoed": 0, "coverage": 0.0, "moved": 0, "unmeasurable": 0}
    err = sum(s.error for s in scores)
    naive = sum(s.naive_error for s in scores)
    moved = [s for s in scores if s.naive_error >= 0.01]
    err_m = sum(s.error for s in moved)
    naive_m = sum(s.naive_error for s in moved)
    return {
        "n": n,
        "skill": round(1 - err / naive, 4) if naive else 0.0,
        "skill_on_moves": round(1 - err_m / naive_m, 4) if naive_m else 0.0,
        "mae": round(err / n, 4), "naive_mae": round(naive / n, 4),
        "echoed": sum(1 for s in scores if s.echoed),
        "coverage": round(sum(1 for s in scores if s.covered) / n, 4),
        "moved": len(moved), "unmeasurable": sum(1 for s in scores if s.unmeasurable),
    }
