"""Execute a harness against a toolbox and record everything it did.

The runner owns three things the harness must not: the model client, the
toolbox, and the budget. A step that would cross the ceiling is refused before
it spends. A run that fails still returns its trace, because a partial trace is
what the optimizer reads to write the next harness.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from .decisions import answers_to_output
from .llm import LLM, LLMError, parse_json_loose
from .model_tools import with_model_tools
from .spec import Harness, HarnessError, LoopStep, Plan, PromptStep, ToolStep
from .template import ConditionError, evaluate, render
from .tools import Toolbox, ToolResult


class BudgetExceeded(RuntimeError):
    def __init__(self, spent: float, limit: float, what: str) -> None:
        super().__init__(f"budget exceeded: {what} at ${spent:.4f} of ${limit:.4f}")
        self.spent, self.limit, self.what = spent, limit, what


@dataclass
class Span:
    name: str
    kind: str                     # step | llm | tool | loop | iteration
    depth: int
    started: float
    ended: float | None = None
    input: Any = None
    output: Any = None
    cost_usd: float = 0.0
    cached: bool = False
    error: str | None = None
    status: str = "ok"

    @property
    def duration_s(self) -> float:
        return (self.ended or time.monotonic()) - self.started

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "kind": self.kind, "depth": self.depth,
                "duration_s": round(self.duration_s, 3), "cost_usd": round(self.cost_usd, 6),
                "cached": self.cached, "status": self.status, "error": self.error,
                "input": _clip(self.input), "output": _clip(self.output)}


def _clip(value: Any, limit: int = 4000) -> Any:
    if value is None or isinstance(value, (int, float, bool)):
        return value
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text if len(text) <= limit else text[:limit] + f"… [{len(text) - limit} more]"


@dataclass
class Trace:
    spans: list[Span] = field(default_factory=list)

    @property
    def cost_usd(self) -> float:
        return sum(s.cost_usd for s in self.spans)

    @property
    def calls(self) -> int:
        return sum(1 for s in self.spans if s.kind in ("llm", "tool"))

    def render(self) -> str:
        lines = []
        for s in self.spans:
            mark = "✓" if s.status == "ok" else ("·" if s.status == "skipped" else "✗")
            cost = f" ${s.cost_usd:.5f}" if s.cost_usd else ""
            err = f"  {s.error}" if s.error else ""
            lines.append(f"{'  ' * s.depth}{mark} {s.name} ({s.kind}) {s.duration_s:.2f}s{cost}{err}")
        return "\n".join(lines)

    def to_dict(self) -> list[dict[str, Any]]:
        return [s.to_dict() for s in self.spans]


@dataclass
class Run:
    harness: str
    run_id: str
    inputs: dict[str, Any]
    output: Any
    state: dict[str, Any]
    trace: Trace
    error: str | None = None
    error_kind: str | None = None      # budget | provider | plan | tool | other

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def cost_usd(self) -> float:
        return self.trace.cost_usd

    def summary(self) -> dict[str, Any]:
        """The run without its trace — including the answer it produced.

        The forecast used to go with the trace, so a rollout scored without
        ``--trace`` kept the number it was graded on and lost what the harness
        actually said: the change it predicted, how wide it quoted, and why.
        Reading a run back then showed a row of dashes.

        The two are not the same size. A trace is tens of kilobytes of
        candlesticks; an answer is a few hundred bytes and is the point.
        """
        return {"harness": self.harness, "run_id": self.run_id, "ok": self.ok,
                "error": self.error, "error_kind": self.error_kind,
                "cost_usd": round(self.cost_usd, 6), "calls": self.trace.calls,
                "output": self.output}

    def tools_seen(self) -> list[dict[str, Any]]:
        """Every tool call and what came back, in order. What a rewriter needs to read."""
        return [{"tool": s.name, "args": s.input, "answer": _clip(s.output, 1500), "error": s.error}
                for s in self.trace.spans if s.kind == "tool"]

    def to_dict(self) -> dict[str, Any]:
        return {**self.summary(), "inputs": self.inputs, "output": self.output,
                "state": {k: _clip(v) for k, v in self.state.items()}, "trace": self.trace.to_dict()}


class _Ledger:
    def __init__(self, max_usd: float, max_calls: int) -> None:
        self.max_usd, self.max_calls = max_usd, max_calls
        self.spent = 0.0
        self.calls = 0

    def check(self, what: str) -> None:
        if self.calls >= self.max_calls:
            raise BudgetExceeded(self.spent, self.max_usd, f"call limit ({what})")
        if self.spent >= self.max_usd:
            raise BudgetExceeded(self.spent, self.max_usd, what)

    def add(self, usd: float, what: str) -> None:
        self.spent += usd
        self.calls += 1
        if self.spent > self.max_usd:
            raise BudgetExceeded(self.spent, self.max_usd, what)


class _Context:
    def __init__(self, harness: Harness, llm: LLM, toolbox: Toolbox, state: dict[str, Any]) -> None:
        self.harness, self.llm, self.toolbox, self.state = harness, llm, toolbox, state
        self.ledger = _Ledger(harness.config.max_usd, harness.config.max_calls)
        self.trace = Trace()
        self.depth = 0

    def span(self, name: str, kind: str, input: Any = None) -> Span:
        s = Span(name=name, kind=kind, depth=self.depth, started=time.monotonic(), input=input)
        self.trace.spans.append(s)
        return s


class Runner:
    def __init__(self, llm: LLM, toolbox: Toolbox, *, model_tools: bool = True) -> None:
        # The model tools ride beside the host's box rather than inside it, so
        # the frozen box stays the host's own statement of the instant and the
        # same client that answers prompt steps answers ``ask_*`` steps.
        self.llm = llm
        self.toolbox = with_model_tools(toolbox, llm) if model_tools else toolbox

    async def run(self, harness: Harness, *, question: str = "", **inputs: Any) -> Run:
        state: dict[str, Any] = {"question": question, **inputs}
        harness.check(self.toolbox, inputs=set(state))
        ctx = _Context(harness, self.llm, self.toolbox, state)
        run_id = uuid.uuid4().hex[:12]
        output: Any = None
        error: BaseException | None = None
        kind: str | None = None
        try:
            output = await self._plan(harness.plan, ctx)
        except BudgetExceeded as exc:
            error, kind, output = exc, "budget", ctx.state.get("last")
        except LLMError as exc:
            error, kind = exc, "provider"
        except (HarnessError, ConditionError, KeyError) as exc:
            error, kind = exc, "plan"
        except Exception as exc:  # noqa: BLE001 - recorded, not raised
            error, kind = exc, "other"
        return Run(harness=harness.name, run_id=run_id,
                   inputs={"question": question, **{k: _clip(v, 500) for k, v in inputs.items()}},
                   output=output, state=_public(ctx.state), trace=ctx.trace,
                   error=f"{type(error).__name__}: {error}" if error else None, error_kind=kind)

    async def _plan(self, plan: Plan, ctx: _Context) -> Any:
        result = None
        for step in plan.steps:
            result = await self._step(step, ctx)
        return result

    async def _step(self, step: Any, ctx: _Context) -> Any:
        label = step.name or step.type
        if step.skip_if and evaluate(step.skip_if, ctx.state):
            s = ctx.span(label, step.type)
            s.status, s.ended = "skipped", time.monotonic()
            if step.output_key:
                # A skipped step writes nothing, and a later step that reads
                # {{opinion.text}} must not fail for it: the name exists as
                # None, and a path through None renders empty. A name nothing
                # ever wrote is still a broken harness and still raises.
                ctx.state.setdefault(step.output_key, None)
            return ctx.state.get("last")
        ctx.ledger.check(label)
        span = ctx.span(label, "loop" if isinstance(step, LoopStep) else "step")
        ctx.depth += 1
        try:
            if isinstance(step, PromptStep):
                result = await self._prompt(step, ctx)
            elif isinstance(step, ToolStep):
                result = await self._tool(step, ctx)
            elif isinstance(step, LoopStep):
                result = await self._loop(step, ctx)
            else:
                raise HarnessError(f"unknown step type {step.type!r}")
        except Exception as exc:
            span.status, span.error = "error", f"{type(exc).__name__}: {exc}"
            raise
        finally:
            ctx.depth -= 1
            span.ended = time.monotonic()
        span.output = result
        ctx.state["last"] = result
        if step.output_key:
            ctx.state[step.output_key] = result
        return result

    async def _prompt(self, step: PromptStep, ctx: _Context) -> Any:
        cfg = ctx.harness.config
        prompt = render(step.prompt, ctx.state)
        system = render(step.system, ctx.state) if step.system else (ctx.harness.context or None)
        if step.questions is not None:
            return await self._decide(step, ctx, prompt, system)
        messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
        tool_names = step.tools
        if tool_names == ["*"]:
            tool_names = list(ctx.harness.tools)
        tools = ctx.toolbox.schemas(tool_names) if tool_names else None
        completion = None
        for iteration in range(step.max_tool_iterations + 1):
            last_turn = iteration == step.max_tool_iterations
            ctx.ledger.check(step.name or "prompt")
            span = ctx.span(f"llm[{iteration}]" if tools else "llm", "llm",
                            input=prompt if iteration == 0 else None)
            try:
                completion = await ctx.llm.complete(
                    messages, model=step.model or cfg.model, system=system,
                    schema=step.output_schema, tools=None if last_turn else tools,
                    temperature=cfg.temperature if step.temperature is None else step.temperature,
                    max_tokens=step.max_tokens or cfg.max_tokens)
            except Exception as exc:
                span.status, span.error, span.ended = "error", f"{type(exc).__name__}: {exc}", time.monotonic()
                raise
            span.ended = time.monotonic()
            span.cost_usd, span.cached = completion.cost_usd, completion.cached
            span.output = completion.text or [tc["function"]["name"] for tc in completion.tool_calls]
            ctx.ledger.add(completion.cost_usd, step.name or "prompt")
            if not completion.tool_calls:
                break
            messages.append(completion.message)
            results = await asyncio.gather(*(
                self._call_tool(tc["function"]["name"], _parse_args(tc["function"].get("arguments")), ctx)
                for tc in completion.tool_calls))
            for tc, result in zip(completion.tool_calls, results):
                messages.append({"role": "tool", "tool_call_id": tc.get("id", ""),
                                 "name": tc["function"]["name"], "content": result.text})
        assert completion is not None
        if step.output_schema:
            try:
                return parse_json_loose(completion.text)
            except ValueError as exc:
                raise HarnessError(f"step {step.name!r} did not return JSON: {exc}") from None
        return completion.text

    async def _decide(self, step: PromptStep, ctx: _Context, prompt: str, system: str | None) -> Any:
        """The prompt step for a model that answers questions instead of writing.

        The rendered prompt is the state, with the harness context ahead of it
        - a decisions model has no system turn, and the context is the domain
        knowledge the questions are asked against. The answers are mapped to
        the step's output fields by the plan's ``answers``.
        """
        cfg = ctx.harness.config
        state = f"{system}\n\n{prompt}" if system else prompt
        ctx.ledger.check(step.name or "prompt")
        span = ctx.span("decide", "llm", input=state)
        try:
            decision = await ctx.llm.decide(state, step.questions or {}, model=step.model or cfg.model)
        except Exception as exc:
            span.status, span.error, span.ended = "error", f"{type(exc).__name__}: {exc}", time.monotonic()
            raise
        span.ended = time.monotonic()
        span.cost_usd, span.cached = decision.cost_usd, decision.cached
        span.output = decision.answers
        ctx.ledger.add(decision.cost_usd, step.name or "prompt")
        try:
            return answers_to_output(step.questions or {}, decision.answers, step.answers)
        except ValueError as exc:
            raise HarnessError(f"step {step.name!r} could not read its answers: {exc}") from None

    async def _call_tool(self, name: str, args: dict[str, Any], ctx: _Context):
        span = ctx.span(name, "tool", input=args)
        if name not in ctx.harness.tools or name not in ctx.toolbox:
            result = ToolResult.failed(f"tool {name!r} is not available to this harness")
        else:
            try:
                result = await ctx.toolbox[name].safe_acall(**args)
            except Exception as exc:
                span.status, span.error = "error", f"{type(exc).__name__}: {exc}"
                span.ended = time.monotonic()
                raise
        span.ended = time.monotonic()
        span.output = result.text
        span.cost_usd = result.cost_usd
        if not result.ok:
            span.status, span.error = "error", result.error
        if result.cost_usd:
            # A tool that asked a model spent the window's money, and the
            # ledger is the one place that knows whether it may.
            ctx.ledger.add(result.cost_usd, name)
        return result

    async def _tool(self, step: ToolStep, ctx: _Context) -> Any:
        args = _render_args(step.args, ctx.state)
        result = await self._call_tool(step.tool, args, ctx)
        if not result.ok and not step.fail_ok:
            raise HarnessError(f"tool {step.tool} failed: {result.error}")
        return result.for_state()

    async def _loop(self, step: LoopStep, ctx: _Context) -> Any:
        results: list[Any] = []
        outer = {k: ctx.state.get(k) for k in ("loop_index", "loop_iteration", "loop_results")}
        ctx.state["loop_results"] = results
        for index in range(step.max_loops):
            ctx.state["loop_index"], ctx.state["loop_iteration"] = index, index + 1
            span = ctx.span(f"iteration {index + 1}", "iteration")
            ctx.depth += 1
            try:
                last = None
                for inner in step.steps:
                    last = await self._step(inner, ctx)
                results.append(last)
                span.output = last
            finally:
                ctx.depth -= 1
                span.ended = time.monotonic()
            if step.until and evaluate(step.until, ctx.state):
                break
        for k, v in outer.items():
            if v is None:
                ctx.state.pop(k, None)
            else:
                ctx.state[k] = v
        return results if step.collect else (results[-1] if results else None)


def _render_args(args: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    def walk(value: Any) -> Any:
        if isinstance(value, str):
            return render(value, state)
        if isinstance(value, dict):
            return {k: walk(v) for k, v in value.items()}
        if isinstance(value, list):
            return [walk(v) for v in value]
        return value
    return {k: walk(v) for k, v in args.items()}


def _parse_args(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _public(state: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in state.items() if k not in ("loop_index", "loop_iteration", "loop_results")}
