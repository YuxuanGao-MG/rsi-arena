"""What a topic has to provide, and the one evaluation loop written against it.

An :class:`Instance` is one question with its answer attached. A :class:`Task`
turns an instance into a toolbox and run inputs, turns a finished run into an
:class:`Outcome`, and says how many outcomes pool into one number. The loop
never looks inside an instance; it only pairs them by ``id`` and splits them by
``group``.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol, runtime_checkable

from ..harness import LLM, Harness, HarnessError, Run, Runner, Toolbox


@runtime_checkable
class Instance(Protocol):
    @property
    def id(self) -> str: ...
    @property
    def group(self) -> str: ...
    def to_dict(self) -> dict[str, Any]: ...


@dataclass(frozen=True)
class Outcome:
    """What one run of a harness on one instance was worth.

    ``value`` is in [0, 1] and is what the optimizer maximises. ``feedback`` is
    what a rewriter is told about this instance, written for a model to read.
    ``objectives`` are named [0, 1] scores for a Pareto frontier. ``details``
    are the topic's own numbers, kept for reports and for the gate's statistic.
    """

    value: float
    feedback: str
    objectives: dict[str, float] = field(default_factory=dict)
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Rollout:
    instance: Instance
    run: Run | None
    outcome: Outcome
    #: What this answer cost when it was genuinely paid for, if it is being
    #: remembered rather than bought. Without it a memoised incumbent reports
    #: zero, and the gate's "a candidate may cost at most twice the incumbent"
    #: check silently disables itself, because it is guarded on the incumbent's
    #: cost being above zero.
    remembered_cost: float | None = None

    @property
    def cost_usd(self) -> float:
        if self.run is not None:
            return self.run.cost_usd
        return self.remembered_cost or 0.0

    @property
    def output(self) -> Any:
        return self.run.output if self.run is not None else None

    def to_dict(self, *, trace: bool = False) -> dict[str, Any]:
        return {"instance": self.instance.to_dict(), "outcome": self.outcome.to_dict(),
                "cost_usd": round(self.cost_usd, 6),
                "run": (self.run.to_dict() if trace else self.run.summary()) if self.run else None}


class Task(Protocol):
    name: str
    background: str                  # the task in words, shown to the rewriter once
    inputs: frozenset[str]           # the state names a plan may read

    def instances(self) -> list[Instance]: ...
    def toolbox(self, instance: Instance) -> Toolbox: ...
    def run_inputs(self, instance: Instance) -> dict[str, Any]: ...
    def score(self, instance: Instance, run: Run) -> Outcome: ...
    def failed(self, instance: Instance, reason: str) -> Outcome: ...
    def statistic(self, outcomes: list[Outcome]) -> float: ...
    def summary(self, outcomes: list[Outcome]) -> dict[str, Any]: ...


def split_by_group(instances: list[Instance], holdout: int, seed: int = 0
                   ) -> tuple[list[Instance], list[Instance]]:
    """Train and held-out instances, split by group and never within one."""
    groups = sorted({i.group for i in instances})
    random.Random(seed).shuffle(groups)
    holdout = max(0, min(holdout, len(groups) - 1))
    held = set(groups[:holdout])
    return [i for i in instances if i.group not in held], [i for i in instances if i.group in held]


def three_way_split(instances: list[Instance], audit: int, holdout: int, seed: int = 0,
                    epoch: int = 0) -> tuple[list[Instance], list[Instance], list[Instance]]:
    """Train, the held-out set the gate reads, and a frozen audit set.

    Two problems this answers, both measured rather than assumed.

    The gate reads a 95% cluster-bootstrap interval, and ``scripts/power.py``
    puts the smallest gap a thirty-five match held-out set can resolve at about
    0.027 pooled skill. The best rewrite anyone has found moved it by under 0.01.
    A test that cannot see its own search's output does not reject candidates —
    it rejects everything, and three generations of "no improvement" were never
    evidence about the rewrites. More matches is the only fix; loosening a gate
    that cannot measure only makes it wrong faster.

    The second problem is that the same held-out matches were being queried
    twice a day forever at a one-sided 2.5% threshold, which is textbook holdout
    erosion. So the evolutionary held-out set rotates on ``epoch``, bounding how
    many times any particular set of matches is asked; and a slice is cut away
    first, on the fixed seed, and never shown to search or gate at all. That
    audit set is scored only to confirm a promotion, which is why it can afford
    to be both large and honest: nothing has been promoted yet, so it has cost
    nothing so far.

    Rotation is not free — it invalidates the incumbent's cached held-out
    rollouts, which is a full extra evaluation — so ``epoch`` is expected to
    advance every few generations rather than every one.
    """
    groups = sorted({i.group for i in instances})
    random.Random(seed).shuffle(groups)
    audit = max(0, min(audit, max(0, len(groups) - 2)))
    held_audit = set(groups[:audit])

    rest = groups[audit:]
    random.Random(seed + epoch * 7919).shuffle(rest)     # a prime, so epochs do not alias
    holdout = max(0, min(holdout, len(rest) - 1))
    held_out = set(rest[:holdout])

    return ([i for i in instances if i.group not in held_audit and i.group not in held_out],
            [i for i in instances if i.group in held_out],
            [i for i in instances if i.group in held_audit])


def probe_sample(groups: set[str], n: int, seed: int = 0) -> set[str]:
    """``n`` groups drawn to stand for the rest, stratified by their prefix.

    It used to be ``sorted(groups)[:n]``, and group ids are Kalshi event tickers,
    so alphabetical order is league order: the first twenty were every
    ``KXBUNDESLIGAGAME-*`` there is and nothing else. Both the cheap filter that
    kills a candidate and the train scoreboard the gate reads were computed on
    one league of twenty-four matches. A harness that happened to suit the
    Bundesliga was being asked about nothing but the Bundesliga.

    Stratified rather than merely shuffled because the leagues are wildly
    unequal — MLS has a hundred and twenty matches to the Bundesliga's
    twenty-four — and a uniform draw of twenty would be mostly MLS by accident
    where this is mostly MLS in proportion.
    """
    if n <= 0 or n >= len(groups):
        return set(groups)
    rng = random.Random(seed)
    by_league: dict[str, list[str]] = {}
    for g in sorted(groups):
        by_league.setdefault(g.split("-")[0], []).append(g)
    for members in by_league.values():
        rng.shuffle(members)
    # Round-robin across leagues, so the smallest is represented before the
    # largest is exhausted.
    picked: list[str] = []
    order = sorted(by_league)
    while len(picked) < n:
        took = False
        for league in order:
            if by_league[league] and len(picked) < n:
                picked.append(by_league[league].pop())
                took = True
        if not took:
            break
    return set(picked)


async def evaluate(task: Task, harness: Harness, instances: list[Instance], llm: LLM, *,
                   concurrency: int = 4, memo: Any = None, fingerprint: str = "") -> list[Rollout]:
    """One run per instance. A harness that cannot run at all fails every instance, with the reason."""
    if not instances:
        return []
    try:
        harness.check(task.toolbox(instances[0]), inputs=set(task.inputs))
    except HarnessError as exc:
        return [Rollout(instance=i, run=None, outcome=task.failed(i, str(exc))) for i in instances]
    gate = asyncio.Semaphore(concurrency)

    async def one(instance: Instance) -> Rollout:
        # An answer already bought is not bought again.
        #
        # The incumbent has been the same harness for five generations, the
        # question set is built once and committed, and a window's realised price
        # was fixed the moment the candle printed — so its score is a pure
        # function that was costing three and a half cents a time, four hundred
        # and eighty times a generation.
        if memo is not None and fingerprint:
            known = memo.get(fingerprint, instance)
            if known is not None:
                return Rollout(instance=instance, run=None, outcome=known,
                               remembered_cost=memo.cost_of(fingerprint, instance))

        # Once the money is gone, stop doing the work that leads to spending it.
        #
        # A refused model call is instant, but the plan that reaches it is not:
        # the tool steps still run, and they are rate-limited reads against the
        # exchange. A generation that exhausted its budget early therefore kept
        # grinding through eight hundred held-out windows at exchange speed,
        # spending nothing and finishing nothing, until the job timeout. The
        # outcome is identical either way — silence — so it is worth nothing and
        # costs forty minutes.
        if getattr(llm, "over_budget", False):
            return Rollout(instance=instance, run=None,
                           outcome=task.failed(instance, "the generation's budget was gone"))
        runner = Runner(llm, task.toolbox(instance))
        async with gate:
            run = await runner.run(harness, **task.run_inputs(instance))
        return Rollout(instance=instance, run=run, outcome=task.score(instance, run))

    return await asyncio.gather(*(one(i) for i in instances))


def summarise(task: Task, rollouts: list[Rollout]) -> dict[str, Any]:
    n = len(rollouts)
    cost = sum(r.cost_usd for r in rollouts)
    out = {"instances": n,
           "statistic": round(task.statistic([r.outcome for r in rollouts]), 4) if n else 0.0,
           "mean_value": round(sum(r.outcome.value for r in rollouts) / n, 4) if n else 0.0,
           "failed_runs": sum(1 for r in rollouts if r.run is None or not r.run.ok),
           "cost_usd": round(cost, 4), "cost_per_instance": round(cost / n, 5) if n else 0.0}
    out.update(task.summary([r.outcome for r in rollouts]))
    return out
