"""The harness contract.

A harness is JSON: a context, a config, the names of the tools it may call,
and a plan of steps. A step is a prompt, a tool call, or a loop. Every step
writes into one flat state dict under ``output_key`` and later steps read it
with ``{{key}}``.

The same JSON is also what the optimizer edits. ``to_components`` splits it
into the text pieces GEPA mutates, and ``from_components`` puts them back and
validates the result, so a bad rewrite fails at load and not four steps into a
paid run.

Files written for the earlier runtime load unchanged: unknown fields are
ignored and ``config.default_model`` is read as ``config.model``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any, ClassVar, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .decisions import is_decision_model, validate_questions
from .template import reads as template_reads
from .tools import Toolbox

BUILT_IN_INPUTS = frozenset({"question", "loop_index", "loop_iteration", "loop_results", "last"})


class HarnessError(ValueError):
    """The harness cannot run as written. The message says why."""


class _Step(BaseModel):
    model_config = ConfigDict(extra="ignore")
    type: str
    name: str = ""
    output_key: str | None = None
    skip_if: str | None = None

    def reads(self) -> set[str]:
        return template_reads(self.skip_if or "")

    def writes(self) -> set[str]:
        return {self.output_key} if self.output_key else set()

    def tools_used(self) -> set[str]:
        return set()


class PromptStep(_Step):
    type: Literal["prompt"] = "prompt"
    prompt: str
    system: str | None = None
    model: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    tools: list[str] = Field(default_factory=list)
    max_tool_iterations: int = 6
    output_schema: dict[str, Any] | None = None
    #: For a decisions model (see ``decisions.py``): typed questions asked of
    #: the rendered prompt in place of a schema, and how their answers become
    #: the step's output fields. A step with questions calls no tools.
    questions: dict[str, Any] | None = None
    answers: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _questions_are_well_formed(self) -> "PromptStep":
        if self.questions is not None:
            try:
                validate_questions(self.questions, self.answers)
            except ValueError as exc:
                raise ValueError(f"step {self.name or 'prompt'!r}: {exc}") from None
            if self.tools:
                raise ValueError(f"step {self.name or 'prompt'!r} has questions and tools; a "
                                 f"decisions model answers questions and calls nothing")
        elif self.answers is not None:
            raise ValueError(f"step {self.name or 'prompt'!r} has answers but no questions")
        return self

    def reads(self) -> set[str]:
        return super().reads() | template_reads(self.prompt) | template_reads(self.system or "")

    def tools_used(self) -> set[str]:
        return set(self.tools)


class ToolStep(_Step):
    type: Literal["tool"] = "tool"
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    fail_ok: bool = False

    def reads(self) -> set[str]:
        found = super().reads()
        for value in self.args.values():
            if isinstance(value, str):
                found |= template_reads(value)
        return found

    def tools_used(self) -> set[str]:
        return {self.tool}


class LoopStep(_Step):
    type: Literal["loop"] = "loop"
    steps: list["AnyStep"] = Field(default_factory=list)
    max_loops: int = 3
    until: str | None = None
    collect: bool = True

    def reads(self) -> set[str]:
        found = super().reads() | template_reads(self.until or "")
        for step in self.steps:
            found |= step.reads()
        return found

    def writes(self) -> set[str]:
        found = super().writes()
        for step in self.steps:
            found |= step.writes()
        return found

    def tools_used(self) -> set[str]:
        found: set[str] = set()
        for step in self.steps:
            found |= step.tools_used()
        return found


AnyStep = Annotated[Union[PromptStep, ToolStep, LoopStep], Field(discriminator="type")]
LoopStep.model_rebuild()


class Plan(BaseModel):
    steps: list[AnyStep] = Field(default_factory=list)

    #: The contract, in words, for whoever writes a plan by hand or by model.
    GRAMMAR: ClassVar[str] = """A plan is {"steps": [...]}. Each step has "type", "name", and an optional
"output_key" (the state name its result is stored under) and "skip_if" (a condition over state).
Later steps read earlier results with {{name}} or {{name.field}}.

- {"type": "tool", "tool": <tool name>, "args": {...}, "fail_ok": false}
  Calls one tool with fixed arguments; argument strings may contain {{placeholders}}.
  The ask_* tools (ask_opus, ask_sonnet, ask_gpt5_mini) put a prompt to a chat model and
  answer in text: {"type": "tool", "tool": "ask_opus", "args": {"prompt": "..."},
  "output_key": "opinion"} makes {{opinion.text}} readable by later steps, which is how a
  decisions model gets a paragraph of reasoning it cannot write itself; the call is paid
  for like a prompt step.
- {"type": "prompt", "prompt": <text>, "system": null, "output_schema": null, "tools": [], "max_tool_iterations": 6}
  Asks the model. With "output_schema" (a JSON Schema) the step returns parsed JSON.
  With "tools" (a list of tool names, or ["*"] for all the harness lists) the model may
  call tools itself, in any order, up to max_tool_iterations turns.
- {"type": "prompt", "prompt": <text>, "questions": {...}, "answers": {...}}
  The same step for a decisions model (a model id starting typesafe/), which answers
  typed questions with probabilities and writes no text. "questions" maps a name to
  {"type": "noul"|"choice"|"score", "instructions": <text>, "criteria": ...}; a score
  question lists ordered levels in "criteria" and may give one number per level in
  "values". "answers" maps each output field to {"from": <question>, "as": <kind>} with
  kind one of mean, stdev, half_range (with "coverage"), confidence, probability, choice,
  score, or {"as": "const", "value": ...}; "min"/"max" clamp a number. A decisions model
  needs questions on every prompt step, a chat model refuses them, and neither may
  give a questions step tools.
- {"type": "loop", "steps": [...], "max_loops": 3, "until": <condition>, "collect": true}
  Repeats its steps until the condition holds or max_loops is spent. Inside, loop_iteration
  (1-based) and loop_results are readable.

Conditions are small Python expressions over state names: comparisons, and/or/not, len().
The harness's "tools" list is the whole set a plan may name."""

    def reads(self) -> set[str]:
        found: set[str] = set()
        for step in self.steps:
            found |= step.reads()
        return found

    def writes(self) -> set[str]:
        found: set[str] = set()
        for step in self.steps:
            found |= step.writes()
        return found

    def tools_used(self) -> set[str]:
        found: set[str] = set()
        for step in self.steps:
            found |= step.tools_used()
        return found

    def required_inputs(self) -> set[str]:
        return self.reads() - self.writes() - BUILT_IN_INPUTS

    def outline(self, indent: int = 0) -> str:
        lines = []
        for step in self.steps:
            lines.append(f"{'  ' * indent}{step.name or step.type} ({step.type})")
            if isinstance(step, LoopStep):
                lines.append(Plan(steps=step.steps).outline(indent + 1))
        return "\n".join(line for line in lines if line)


class HarnessConfig(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)
    model: str = Field(default="anthropic/claude-sonnet-4.5", alias="default_model")
    temperature: float | None = 0.0
    max_tokens: int | None = None
    max_usd: float = 0.25
    max_calls: int = 50


class Harness(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str
    description: str = ""
    context: str = ""
    config: HarnessConfig = Field(default_factory=HarnessConfig)
    tools: list[str] = Field(default_factory=list)
    plan: Plan = Field(default_factory=Plan)

    @model_validator(mode="after")
    def _model_and_steps_agree(self) -> "Harness":
        """A decisions model answers only questions; a chat model answers none.

        Checked here rather than at call time so that a rewrite that swaps the
        model without rewriting the plan - or the plan without the model -
        fails at load with a sentence the optimizer can read, instead of
        failing every window with a provider error.
        """
        def walk(steps):
            for step in steps:
                if isinstance(step, LoopStep):
                    yield from walk(step.steps)
                elif isinstance(step, PromptStep):
                    yield step
        for step in walk(self.plan.steps):
            model = step.model or self.config.model
            if is_decision_model(model) and step.questions is None:
                raise ValueError(f"model {model!r} answers typed questions only, and step "
                                 f"{step.name or 'prompt'!r} asks it for text; give the step "
                                 f"'questions' and 'answers', or choose a chat model")
            if step.questions is not None and not is_decision_model(model):
                raise ValueError(f"step {step.name or 'prompt'!r} carries questions, which only "
                                 f"a decisions model (typesafe/...) answers; {model!r} is a chat model")
        return self

    # -- files --

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Harness":
        try:
            return cls.model_validate(data)
        except ValidationError as exc:
            raise HarnessError(f"invalid harness: {_first_error(exc)}") from None

    @classmethod
    def load(cls, path: str | Path) -> "Harness":
        return cls.from_dict(json.loads(Path(path).read_text()))

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(by_alias=False)

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2))

    # -- components, for the optimizer --

    COMPONENTS: ClassVar[tuple[str, ...]] = ("context", "plan", "tools", "model")

    def to_components(self) -> dict[str, str]:
        return {
            "context": self.context,
            "plan": json.dumps(self.plan.model_dump(exclude_none=True), indent=2),
            "tools": ", ".join(self.tools),
            # The model is a component because it is the largest measured lever:
            # swapping it once moved held-out skill by twelve points where the
            # best rewrite ever found moved it by under one. A search told to
            # improve a harness while barred from its biggest dial was
            # optimising the small knobs on principle.
            "model": self.config.model or "",
        }

    def from_components(self, components: dict[str, str]) -> "Harness":
        """This harness with the given text components swapped in, validated."""
        data = self.to_dict()
        if "context" in components:
            data["context"] = components["context"]
        if "tools" in components:
            data["tools"] = [t.strip() for t in components["tools"].split(",") if t.strip()]
        if "model" in components:
            data.setdefault("config", {})
            data["config"]["model"] = components["model"].strip()
        if "plan" in components:
            try:
                plan = json.loads(components["plan"])
            except json.JSONDecodeError as exc:
                raise HarnessError(f"plan is not valid JSON: {exc.msg} at line {exc.lineno}") from None
            if isinstance(plan, list):
                plan = {"steps": plan}
            data["plan"] = plan
        return Harness.from_dict(data)

    # -- checks --

    def check(self, toolbox: Toolbox, inputs: set[str] | None = None) -> None:
        """Refuse a harness that cannot run: unknown tools, unmet inputs."""
        unknown = [t for t in self.tools if t not in toolbox]
        if unknown:
            raise HarnessError(f"tools not available: {', '.join(unknown)} "
                               f"(available: {', '.join(sorted(toolbox)) or 'none'})")
        used = self.plan.tools_used() - {"*"}
        outside = sorted(used - set(self.tools))
        if outside:
            raise HarnessError(f"plan calls tools the harness does not list: {', '.join(outside)}")
        if inputs is not None:
            missing = sorted(self.plan.required_inputs() - inputs)
            if missing:
                raise HarnessError(f"plan reads {', '.join(missing)}, which the caller does not supply")
        if not self.plan.steps:
            raise HarnessError("plan has no steps")

    def outline(self) -> str:
        return (f"{self.name} [{self.config.model}]\n"
                f"tools: {', '.join(self.tools) or 'none'}\n{self.plan.outline()}")


def _first_error(exc: ValidationError) -> str:
    err = exc.errors()[0]
    where = ".".join(str(p) for p in err.get("loc", ()))
    return f"{where}: {err.get('msg')}" if where else str(err.get("msg"))
