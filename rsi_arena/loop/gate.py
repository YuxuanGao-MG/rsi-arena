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
import statistics
from dataclasses import asdict, dataclass, field
from typing import Any

from .task import Outcome, Rollout, Task


#: Below this many groups a resampling interval is theatre. With two matches a
#: cluster bootstrap can only draw {A,A}, {A,B} and {B,B}, and the interval it
#: reports collapses toward the observed difference — narrow, and meaningless.
MIN_GROUPS = 8

#: One-sided 2.5% at 80% power, in standard errors. Used only to report what
#: size of effect a test could have resolved, never to decide anything.
DETECTION_Z = 1.96 + 0.84

#: The fraction of a held-out side that may go unscored before the whole
#: comparison is refused. An unscored window is pooled as silence, which is a
#: real value — so a side that is one-quarter refusals is one-quarter fabricated,
#: and the fabrication is invisible in the interval. gen5's baseline was 69%
#: refusals and produced a fully-formed bootstrap interval; `over_budget` was the
#: only thing that stopped it being read, and §the 402 audit showed a path where
#: `over_budget` stays False. This guard does not depend on knowing why.
MAX_UNSCORED = 0.25


def paired_bootstrap(task: Task, candidate: list[Rollout], incumbent: list[Rollout], *,
                     n: int = 2000, seed: int = 0, level: float = 0.95,
                     min_groups: int = MIN_GROUPS) -> dict[str, Any]:
    """Interval of statistic(candidate) minus statistic(incumbent), on shared instances.

    **Resampled by group, not by instance.** Thirty-four windows of one match
    are one match seen thirty-four times: their five-minute horizons overlap,
    they share a scoreline, and a goal moves all of them at once. Drawing them
    independently pretends to thirty-four facts and reports an interval far
    tighter than the evidence supports — the split already refuses to put one
    match on both sides, and this is the same refusal applied to the arithmetic.

    Below :data:`MIN_GROUPS` the interval is reported as unusable rather than
    narrow, because too few clusters is not a small sample, it is no sample.
    """
    by_id = {r.instance.id: r.outcome for r in incumbent}
    pairs: list[tuple[str, Outcome, Outcome]] = [
        (r.instance.group, r.outcome, by_id[r.instance.id])
        for r in candidate if r.instance.id in by_id]
    if len(pairs) < 2:
        return {"paired": len(pairs), "groups": 0, "diff": 0.0,
                "low": 0.0, "high": 0.0, "usable": False}

    clusters: dict[str, list[tuple[Outcome, Outcome]]] = {}
    for group, cand, inc in pairs:
        clusters.setdefault(group, []).append((cand, inc))
    keys = sorted(clusters)

    def diff(sample: list[tuple[Outcome, Outcome]]) -> float:
        return task.statistic([c for c, _ in sample]) - task.statistic([i for _, i in sample])

    observed = diff([(c, i) for _, c, i in pairs])
    if len(keys) < min_groups:
        return {"paired": len(pairs), "groups": len(keys), "diff": round(observed, 4),
                "low": 0.0, "high": 0.0, "usable": False}

    rng = random.Random(seed)
    draws = []
    for _ in range(n):
        drawn = [pair for _ in keys for pair in clusters[keys[rng.randrange(len(keys))]]]
        draws.append(diff(drawn))
    draws.sort()
    lo, hi = draws[int((1 - level) / 2 * n)], draws[min(n - 1, int((1 + level) / 2 * n))]
    # What this test could have seen, as well as what it did see.
    #
    # An interval that straddles zero says "not proven", and "not proven" means
    # two very different things depending on whether the test could resolve the
    # effect at all. On a thirty-five match held-out set the smallest gap this
    # can resolve is about 0.027 pooled skill, and no rewrite anyone has found
    # has moved it by more than 0.01 — so every rejection so far was a statement
    # about the sample size, not about the candidate. Recording it makes the
    # difference visible in the manifest instead of inferable by someone who
    # already suspected it.
    se = statistics.pstdev(draws) or 0.0
    detectable = DETECTION_Z * se
    return {"paired": len(pairs), "groups": len(keys), "diff": round(observed, 4),
            "low": round(lo, 4), "high": round(hi, 4), "usable": True,
            "se": round(se, 5), "detectable": round(detectable, 4),
            "underpowered": abs(observed) < detectable}


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
           unchanged: bool = False, min_groups: int = MIN_GROUPS,
           stopped_early: bool = False, exhausted: bool = False,
           stop_reason: str = "", incomplete: str = "") -> Decision:
    """Promote a candidate, or say why not.

    ``unchanged`` is for the case the first real run hit: GEPA's best was the
    seed, so the "candidate" was the incumbent and every paired difference was
    exactly zero. The arithmetic is right and the sentence it produces is not —
    "not distinguishable from noise" describes a rewrite that tied, and what
    happened was that there was no rewrite. A loop whose job is to tell an
    improvement from a reshuffle should not report a search that found nothing
    as a near miss.
    """
    hold = paired_bootstrap(task, candidate_holdout, incumbent_holdout, seed=seed,
                            min_groups=min_groups)
    train = paired_bootstrap(task, candidate_train, incumbent_train, seed=seed,
                             min_groups=min_groups)
    reasons: list[str] = []
    ok = True
    if unchanged:
        return Decision(accepted=False, holdout=hold, train=train,
                        reasons=["the search returned the incumbent unchanged; "
                                 "nothing was proposed to gate"])
    if incomplete:
        # Held-out was never bought - not refused mid-way, never started - so
        # there is no comparison, fabricated or otherwise. The search's
        # candidates were scored in full and belong in the archive; only the
        # judgment is missing, and the sentence says which.
        return Decision(accepted=False, holdout=hold, train=train,
                        reasons=[f"incomplete: {incomplete}"])
    if exhausted:
        # Running out of money and being rejected by the cascade both stop the
        # run before held-out, and for a while they produced the same sentence.
        # The first generation it happened to reported "the cascade rejected it
        # on 20 train matches (+0.136)" — a positive number, in a sentence
        # claiming rejection, describing neither. The candidate had scored zero
        # because every call was refused, the incumbent had scored -0.136
        # because half of its calls were, and the difference between two sets of
        # refusals is not a measurement of anything. A generation that ran out
        # of money has no result to report and must not report one.
        return Decision(accepted=False, holdout=hold, train=train,
                        reasons=["the generation ran out of money before it could be "
                                 "judged; the numbers above are refusals, not evidence"])
    if stopped_early:
        # The cascade rejected it on a sample of train, so held-out was never
        # run and there is nothing to draw an interval from. Saying "no
        # held-out instances" would read as a fault; it was a decision.
        return Decision(accepted=False, holdout=hold, train=train,
                        reasons=[f"the cascade rejected it on {train.get('groups', 0)} "
                                 f"train matches ({train.get('diff', 0.0):+.3f}), so "
                                 f"held-out was never paid for"
                                 + (f": {stop_reason}" if stop_reason else "")])
    # Per side, not pooled across both: a candidate that is 40% refusals against
    # a clean incumbent averages to 20% and would slip a combined threshold,
    # while being exactly the comparison the guard exists to refuse.
    unscored = max(_unscored_fraction(candidate_holdout),
                   _unscored_fraction(incumbent_holdout))
    if unscored > MAX_UNSCORED:
        # Refused outright rather than folded into `ok`, because the numbers
        # above it are not weak evidence — they are part fabrication, and a
        # fabricated interval quoted beside a refusal reads as a near miss.
        return Decision(accepted=False, holdout=hold, train=train,
                        reasons=[f"{unscored:.0%} of the held-out windows went unscored "
                                 f"and were pooled as silence; above {MAX_UNSCORED:.0%} "
                                 f"the comparison is part fabrication and no verdict "
                                 f"is drawn from it"])
    if hold["paired"] < 2:
        ok = False
        reasons.append("no paired held-out instances to judge on")
    elif not hold.get("usable", True):
        ok = False
        reasons.append(
            f"{hold.get('groups', 0)} held-out fixtures is too few to draw an "
            f"interval from; {min_groups} is the minimum, so no gain can be "
            f"promoted no matter how large")
    elif hold["low"] <= 0:
        ok = False
        reasons.append(f"held-out gain {hold['diff']:+.3f} is not distinguishable from noise "
                       f"(95% interval {hold['low']:+.3f} to {hold['high']:+.3f})")
        if hold.get("underpowered"):
            # Not a rejection of the candidate. This test, on this many matches,
            # could not have resolved a gain of this size from any candidate.
            reasons.append(f"and could not have: on {hold['groups']} fixtures this test "
                           f"resolves {hold['detectable']:+.3f} at best, so a gain of "
                           f"{abs(hold['diff']):.3f} was never visible to it")
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


def _unscored_fraction(rollouts: list[Rollout]) -> float:
    """How much of an evaluation is silence nobody chose.

    A memoised outcome has no run but was genuinely scored; a refusal has no
    score however it is wrapped. The outcome's own record is the one thing both
    shapes carry.
    """
    if not rollouts:
        return 0.0
    unscored = sum(1 for r in rollouts if not r.outcome.details.get("scored"))
    return unscored / len(rollouts)


def _cost_per_instance(rollouts: list[Rollout]) -> float:
    return sum(r.cost_usd for r in rollouts) / len(rollouts) if rollouts else 0.0
