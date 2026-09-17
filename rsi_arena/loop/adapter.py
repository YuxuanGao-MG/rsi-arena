"""The task as GEPA sees it.

GEPA hands over a candidate (the harness as text components) and a batch of
instances, and wants a score per instance plus, when asked, something to
reflect on. Everything about *how* to mutate is GEPA's; everything about
*what a good run is* is the task's. This file only translates.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from gepa.core.adapter import EvaluationBatch, GEPAAdapter

from ..harness import LLM, Harness, HarnessError, Plan, run_sync
from .generation import fingerprint_components
from .task import Instance, Rollout, Task, evaluate


class TaskAdapter(GEPAAdapter[Instance, dict, dict]):
    def __init__(self, task: Task, base: Harness, llm: LLM, *, concurrency: int = 4,
                 memo: Any = None, progress: Any = None) -> None:
        self.task, self.base, self.llm, self.concurrency = task, base, llm, concurrency
        self.evaluations = 0
        #: Outcomes already paid for. The search is the largest line in a
        #: generation and it re-scores: the seed candidate is evaluated in full
        #: at the start of every search, and the seed is usually a harness some
        #: earlier generation already measured on these very windows.
        self.memo = memo
        #: The heartbeat, if the run has one. Ticked per batch because this is
        #: the one chokepoint every candidate evaluation passes through.
        self.progress = progress
        # Resolved once. The tools prompt needs to know what was *not* called as
        # much as what was, and asking the topic per trajectory would cost the
        # question set each time.
        self.available = _available(task, base)

    def evaluate(self, batch: list[Instance], candidate: dict[str, str],
                 capture_traces: bool = False) -> EvaluationBatch[dict, dict]:
        self.evaluations += len(batch)
        if self.progress is not None:
            self.progress.tick("search", evaluations=self.evaluations,
                               spent_usd=round(getattr(self.llm, "spent_usd", 0.0), 2))
        try:
            harness = self.base.from_components(candidate)
        except HarnessError as exc:
            rollouts = [Rollout(instance=i, run=None, outcome=self.task.failed(i, str(exc))) for i in batch]
        else:
            # The memo is consulted only when traces are not wanted. A remembered
            # outcome has no trajectory, and the reflection path needs one — so
            # the cheap large scoring passes reuse, and the small minibatch that
            # feeds the rewriter is always run for real.
            fp = ""
            if self.memo is not None and not capture_traces:
                fp = fingerprint_components(candidate, harness.config.model)
            rollouts = run_sync(evaluate(self.task, harness, batch, self.llm,
                                         concurrency=self.concurrency,
                                         memo=None if capture_traces else self.memo,
                                         fingerprint=fp))
            if self.memo is not None and fp:
                self.memo.absorb(fp, rollouts)
        return EvaluationBatch(
            outputs=[_as_dict(r.output) for r in rollouts],
            scores=[r.outcome.value for r in rollouts],
            trajectories=[self._trajectory(r) for r in rollouts] if capture_traces else None,
            objective_scores=[r.outcome.objectives or {"value": r.outcome.value} for r in rollouts])

    @staticmethod
    def _trajectory(r: Rollout) -> dict[str, Any]:
        return {"inputs": r.run.inputs if r.run else r.instance.to_dict(),
                "tools": r.run.tools_seen() if r.run else [],
                "output": r.output, "feedback": r.outcome.feedback, "value": r.outcome.value,
                "error": r.run.error if r.run else r.outcome.feedback}

    def make_reflective_dataset(self, candidate: dict[str, str], eval_batch: EvaluationBatch[dict, dict],
                                components_to_update: list[str]) -> Mapping[str, Sequence[Mapping[str, Any]]]:
        """What each component is shown of a batch. Not the same thing for each.

        It used to be byte-identical for all three, and across fourteen
        candidates over two thousand-call searches the ``tools`` component's hash
        never changed once — the tool list was the one thing the loop is supposed
        to evolve and the one thing it never touched. A prompt asked to rewrite a
        tool allowlist while reading nothing but prose and a forecast has nothing
        tool-shaped to reason about, so it returns what it was given.

        Two independent 2026 harness ablations report that gains localise to
        tools and memory rather than the system prompt, which makes the component
        we were not mutating the one most likely to matter.

        So each component gets the evidence it can act on: ``tools`` sees which
        tools were called, what each cost in latency, which returned errors, and
        which available tools were never tried; ``plan`` sees the call sequence
        and the answers; ``context`` sees the prose and the outcome.
        """
        by_component: dict[str, list[dict[str, Any]]] = {}
        for component in components_to_update:
            records = []
            for traj in eval_batch.trajectories or []:
                records.append({
                    "Inputs": self._evidence(component, traj),
                    "Generated Outputs": _short(traj["output"], 3000),
                    "Feedback": f"{traj['feedback']} Score {traj['value']:.2f}.",
                })
            by_component[component] = records
        return by_component

    def _evidence(self, component: str, traj: dict[str, Any]) -> dict[str, Any]:
        inputs = {k: _short(v, 600) for k, v in traj["inputs"].items()}
        calls = traj.get("tools") or []
        if component == "tools":
            used = [c.get("tool") for c in calls]
            failed = [c.get("tool") for c in calls if c.get("error")]
            empty = [c.get("tool") for c in calls
                     if not c.get("error") and not str(c.get("answer") or "").strip()]
            untried = [t for t in self.available if t not in used]
            return {**inputs,
                    "tools called, in order": used,
                    "tools that returned an error": failed or "none",
                    "tools that answered with nothing": empty or "none",
                    "tools available but never called": untried or "none",
                    "what each answered": _short(calls, 4000)}
        if component == "plan":
            return {**inputs,
                    "the call sequence": [c.get("tool") for c in calls],
                    "tool answers": _short(calls, 4000)}
        return {**inputs, "tool answers": _short(calls, 2500)}


def _available(task: Task, base: Harness) -> list[str]:
    """Every tool a rewrite may reach for, not just the ones it already uses.

    This read `base.tools` and so told the rewriter that the three tools the
    harness happened to list were the only ones in existence. The box holds
    nine. A search that cannot learn what it is allowed to call is a search over
    the prompt with extra steps — and the arena's premise is that a harness
    composes primitives.

    Falls back to what the harness lists if the topic cannot say, because being
    wrong in that direction only narrows the search.

    Asks the topic rather than building an instance: loading the question set to
    find out which tools exist costs the whole benchmark for a string.
    """
    named = getattr(task, "tools", None)
    if callable(named):
        try:
            return sorted(named())
        except Exception:
            pass
    return list(base.tools)


def reflection_templates(task: Task, base: Harness) -> dict[str, str]:
    """One reflection prompt per component, with the task stated once.

    GEPA's default prompt asks for "a new instruction", which is right for the
    context and wrong for a JSON plan or a tool list. Each component gets a
    prompt that says what shape it has and what the task is.
    """
    head = (f"You are improving one part of a harness: a model wired to tools by a plan.\n\n"
            f"The task:\n{task.background}\n\n"
            f"Tools the harness may list (no others exist): "
            f"{', '.join(_available(task, base))}.\n"
            f"Inputs a plan may read: {', '.join(sorted(task.inputs))}.\n\n")
    examples = ("Below are instances the current harness ran on, what it did, and feedback on "
                "each. Read the feedback for patterns: what the harness kept getting wrong, "
                "and what a better one would do differently.\n```\n<side_info>\n```\n\n")
    return {
        "context": head + "The current context (the system prompt every model step sees):\n```\n<curr_param>\n```\n\n"
                   + examples + "Write a new context. Keep what works, fix what the feedback shows, and "
                   "state the domain facts a model would not otherwise know. Provide it within ``` blocks.",
        "plan": head + "The current plan, as JSON:\n```\n<curr_param>\n```\n\n"
                + "Plan grammar:\n" + Plan.GRAMMAR + "\n\n" + examples
                + "Write a new plan as JSON in the same grammar: change the steps, their prompts, "
                "their order, add or remove tool calls, or add a loop, whatever the feedback "
                "argues for. The last step's output is the harness's answer and must keep the "
                "output contract described in the task. Provide the JSON within ``` blocks.",
        "tools": head + "The current tool list:\n```\n<curr_param>\n```\n\n" + examples
                 + "Write the new tool list as comma-separated names drawn only from the tools "
                 "named above — including ones the current list leaves out, if the feedback "
                 "argues for them. A plan may only call tools in this list. Provide it within "
                 "``` blocks.",
    }


def _as_dict(output: Any) -> dict[str, Any]:
    return output if isinstance(output, dict) else {"output": output}


def _short(value: Any, limit: int) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text if len(text) <= limit else text[:limit] + "…"
