"""Decide whether a candidate replaces the incumbent.

GEPA picks its best candidate on the windows it was allowed to see. That is not
enough to promote it: the rule here is Self-Harness's, no regression on held-in
*or* held-out, with Meta-Harness's held-out protection. The held-out fixtures
are never shown to the optimizer.

Noise is handled by a paired bootstrap over windows: resample the same windows
for candidate and incumbent together and read the interval of the pooled-skill
difference. A candidate is better only if that interval sits above zero.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from ..bench.score import WindowScore


def _pooled_skill(scores: list[WindowScore]) -> float:
    naive = sum(s.naive_error for s in scores)
    if naive <= 0:
        return 0.0
    return 1 - sum(s.error for s in scores) / naive


def paired_bootstrap(candidate: list[WindowScore], incumbent: list[WindowScore], *,
                     n: int = 2000, seed: int = 0, level: float = 0.95) -> dict:
    """Interval of pooled skill(candidate) minus pooled skill(incumbent), same windows."""
    by_id = {s.window_id: s for s in incumbent}
    pairs = [(c, by_id[c.window_id]) for c in candidate if c.window_id in by_id]
    if len(pairs) < 2:
        return {"n": len(pairs), "diff": 0.0, "low": 0.0, "high": 0.0, "paired": len(pairs)}
    rng = random.Random(seed)
    point = _pooled_skill([c for c, _ in pairs]) - _pooled_skill([i for _, i in pairs])
    diffs = []
    for _ in range(n):
        sample = [pairs[rng.randrange(len(pairs))] for _ in pairs]
        diffs.append(_pooled_skill([c for c, _ in sample]) - _pooled_skill([i for _, i in sample]))
    diffs.sort()
    lo = diffs[int((1 - level) / 2 * n)]
    hi = diffs[min(n - 1, int((1 + level) / 2 * n))]
    return {"n": len(pairs), "diff": round(point, 4), "low": round(lo, 4), "high": round(hi, 4),
            "paired": len(pairs)}


@dataclass
class Decision:
    accepted: bool
    reasons: list[str] = field(default_factory=list)
    holdout: dict = field(default_factory=dict)
    train: dict = field(default_factory=dict)


def accept(*, candidate_train: list[WindowScore], incumbent_train: list[WindowScore],
           candidate_holdout: list[WindowScore], incumbent_holdout: list[WindowScore],
           candidate_cost: float, incumbent_cost: float, max_cost_ratio: float = 2.0,
           seed: int = 0) -> Decision:
    reasons: list[str] = []
    hold = paired_bootstrap(candidate_holdout, incumbent_holdout, seed=seed)
    train = paired_bootstrap(candidate_train, incumbent_train, seed=seed)
    ok = True
    if hold["paired"] < 2:
        ok, _ = False, reasons.append("no paired held-out windows to judge on")
    elif hold["low"] <= 0:
        ok = False
        reasons.append(f"held-out improvement {hold['diff']:+.3f} is not distinguishable from noise "
                       f"(95% interval {hold['low']:+.3f} to {hold['high']:+.3f})")
    else:
        reasons.append(f"held-out skill improves by {hold['diff']:+.3f} "
                       f"({hold['low']:+.3f} to {hold['high']:+.3f})")
    if train["paired"] >= 2 and train["diff"] < 0:
        ok = False
        reasons.append(f"regresses on held-in windows by {train['diff']:+.3f}")
    if incumbent_cost > 0 and candidate_cost > max_cost_ratio * incumbent_cost:
        ok = False
        reasons.append(f"costs {candidate_cost / incumbent_cost:.1f}x the incumbent "
                       f"(limit {max_cost_ratio:.1f}x)")
    return Decision(accepted=ok, reasons=reasons, holdout=hold, train=train)
