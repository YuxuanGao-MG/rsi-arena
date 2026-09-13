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

    @property
    def cost_usd(self) -> float:
        return self.run.cost_usd if self.run is not None else 0.0

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


async def evaluate(task: Task, harness: Harness, instances: list[Instance], llm: LLM, *,
                   concurrency: int = 4) -> list[Rollout]:
    """One run per instance. A harness that cannot run at all fails every instance, with the reason."""
    if not instances:
        return []
    try:
        harness.check(task.toolbox(instances[0]), inputs=set(task.inputs))
    except HarnessError as exc:
        return [Rollout(instance=i, run=None, outcome=task.failed(i, str(exc))) for i in instances]
    gate = asyncio.Semaphore(concurrency)

    async def one(instance: Instance) -> Rollout:
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
