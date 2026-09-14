"""Decide whether a candidate replaces the incumbent.

GEPA picks its best candidate on the instances it was allowed to see. That is
not enough: the rule here is no regression on held-in *or* held-out, and the
held-out groups are never shown to the optimizer.

Noise is handled by a paired bootstrap: resample the same instances for
candidate and incumbent together and read the interval of the difference in
the task's pooled statistic. A candidate is better only if that interval sits
above zero.
"""

from __future__ import annotations

import random
from dataclasses import asdict, dataclass, field
from typing import Any

from .task import Outcome, Rollout, Task


def paired_bootstrap(task: Task, candidate: list[Rollout], incumbent: list[Rollout], *,
                     n: int = 2000, seed: int = 0, level: float = 0.95) -> dict[str, Any]:
    """Interval of statistic(candidate) minus statistic(incumbent), on shared instances."""
    by_id = {r.instance.id: r.outcome for r in incumbent}
    pairs: list[tuple[Outcome, Outcome]] = [(r.outcome, by_id[r.instance.id])
                                            for r in candidate if r.instance.id in by_id]
    if len(pairs) < 2:
        return {"paired": len(pairs), "diff": 0.0, "low": 0.0, "high": 0.0}

    def diff(sample: list[tuple[Outcome, Outcome]]) -> float:
        return task.statistic([c for c, _ in sample]) - task.statistic([i for _, i in sample])

    rng = random.Random(seed)
    draws = sorted(diff([pairs[rng.randrange(len(pairs))] for _ in pairs]) for _ in range(n))
    lo, hi = draws[int((1 - level) / 2 * n)], draws[min(n - 1, int((1 + level) / 2 * n))]
    return {"paired": len(pairs), "diff": round(diff(pairs), 4), "low": round(lo, 4), "high": round(hi, 4)}


@dataclass
class Decision:
    accepted: bool
    reasons: list[str] = field(default_factory=list)
    holdout: dict[str, Any] = field(default_factory=dict)
    train: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def accept(task: Task, *, candidate_train: list[Rollout], incumbent_train: list[Rollout],
           candidate_holdout: list[Rollout], incumbent_holdout: list[Rollout],
           max_cost_ratio: float = 2.0, seed: int = 0,
           unchanged: bool = False) -> Decision:
    """Promote a candidate, or say why not.

    ``unchanged`` is for the case the first real run hit: GEPA's best was the
    seed, so the "candidate" was the incumbent and every paired difference was
    exactly zero. The arithmetic is right and the sentence it produces is not —
    "not distinguishable from noise" describes a rewrite that tied, and what
    happened was that there was no rewrite. A loop whose job is to tell an
    improvement from a reshuffle should not report a search that found nothing
    as a near miss.
    """
    hold = paired_bootstrap(task, candidate_holdout, incumbent_holdout, seed=seed)
    train = paired_bootstrap(task, candidate_train, incumbent_train, seed=seed)
    reasons: list[str] = []
    ok = True
    if unchanged:
        return Decision(accepted=False, holdout=hold, train=train,
                        reasons=["the search returned the incumbent unchanged; "
                                 "nothing was proposed to gate"])
    if hold["paired"] < 2:
        ok = False
        reasons.append("no paired held-out instances to judge on")
    elif hold["low"] <= 0:
        ok = False
        reasons.append(f"held-out gain {hold['diff']:+.3f} is not distinguishable from noise "
                       f"(95% interval {hold['low']:+.3f} to {hold['high']:+.3f})")
    else:
        reasons.append(f"held-out gain {hold['diff']:+.3f} ({hold['low']:+.3f} to {hold['high']:+.3f})")
    if train["paired"] >= 2 and train["diff"] < 0:
        ok = False
        reasons.append(f"regresses on held-in instances by {train['diff']:+.3f}")
    cand_cost = _cost_per_instance(candidate_holdout)
    inc_cost = _cost_per_instance(incumbent_holdout)
    if inc_cost > 0 and cand_cost > max_cost_ratio * inc_cost:
        ok = False
        reasons.append(f"costs {cand_cost / inc_cost:.1f}x the incumbent per instance "
                       f"(limit {max_cost_ratio:.1f}x)")
    return Decision(accepted=ok, reasons=reasons, holdout=hold, train=train)


def _cost_per_instance(rollouts: list[Rollout]) -> float:
    return sum(r.cost_usd for r in rollouts) / len(rollouts) if rollouts else 0.0
