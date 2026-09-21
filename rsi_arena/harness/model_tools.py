"""A chat model as a tool, for a harness whose own model cannot reason in text.

A decisions model answers typed questions with probabilities and writes
nothing, so a plan for it is a fixed sequence of tool steps ending in one
question step. That leaves it no way to think hard about a window: it cannot
call tools, and it cannot be asked to explain. What it can be handed is one
more tool step whose answer is a paragraph from a chat model - the price path
and the tape, read by Opus, summarised in prose that the questions are then
asked against. Which model, and whether to ask at all, is the optimizer's to
choose: the ``tools`` component lists ``ask_opus`` like any other name, and a
plan step calls it with a prompt rendered from the state.

Point in time is kept the way it is kept for every other tool: the model
reads only the prompt it is given, which the plan renders from tools already
frozen at the instant. It has no web search, no tools of its own, and no
turn beyond the one. What it knows from training is the same leak a chat
model in the driving seat has always had, and nothing here widens it.

The call is charged. A ``ToolResult`` carries what the answer cost and the
runner adds it to the window's ledger, so a three-cent Opus opinion counts
against ``max_usd`` exactly as a three-cent prompt step would, and the gate's
cost ratio sees it in the trace.
"""

from __future__ import annotations

from typing import Any

from .decisions import is_decision_model
from .llm import LLM, LLMError, run_sync
from .tools import Tool, Toolbox, ToolResult

#: Tool name to the chat model behind it. The same three models the search may
#: put in the driving seat (``Settings.model_choices``), so a candidate that
#: delegates is choosing among models the gate has already priced.
MODEL_TOOLS: dict[str, str] = {
    "ask_opus": "anthropic/claude-opus-5",
    "ask_sonnet": "anthropic/claude-sonnet-4.5",
    "ask_gpt5_mini": "openai/gpt-5-mini",
}


class ModelTool(Tool):
    """One chat model, callable as a tool with a prompt and an optional system turn."""

    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "prompt": {"type": "string",
                       "description": "What to ask. Everything the model may know about the "
                                      "window has to be in here; it has no tools."},
            "system": {"type": ["string", "null"],
                       "description": "An optional system turn framing the answer."},
        },
        "required": ["prompt"],
    }
    #: A refused or failed provider call is the host's fault, not an answer
    #: for the model to read: the runner records it as a provider failure.
    passthrough = (LLMError,)

    def __init__(self, name: str, model: str, llm: LLM, description: str | None = None) -> None:
        if is_decision_model(model):
            # A decisions model has no text to give back, and asking one for a
            # paragraph is the mistake the spec refuses at load for a prompt
            # step; refusing it here keeps the tool's contract the same.
            raise ValueError(f"{name!r} would delegate to {model!r}, which answers typed "
                             f"questions and writes no text; a model tool needs a chat model")
        self.name, self.model, self.llm = name, model, llm
        self.description = description or (
            f"Ask {model} a question in prose and read its answer as text. It sees only "
            f"the prompt: no tools, no web, no game feed.")

    async def acall(self, prompt: str, system: str | None = None) -> ToolResult:  # type: ignore[override]
        completion = await self.llm.complete([{"role": "user", "content": prompt}],
                                             model=self.model, system=system or None,
                                             temperature=0)
        answer = completion.text
        return ToolResult(ok=True, text=answer,
                          data={"text": answer, "model": self.model,
                                "cost_usd": completion.cost_usd},
                          cost_usd=completion.cost_usd)

    def call(self, **args: Any) -> ToolResult:
        # The runner awaits ``acall``; this is for a caller with no loop.
        return run_sync(self.acall(**args))


def model_tool_names() -> list[str]:
    return sorted(MODEL_TOOLS)


def model_tools(llm: LLM) -> list[Tool]:
    return [ModelTool(name, model, llm) for name, model in sorted(MODEL_TOOLS.items())]


def with_model_tools(toolbox: Toolbox, llm: LLM) -> Toolbox:
    """A copy of the box with the model tools beside the host's own.

    A copy, because the frozen box is the host's statement of what a harness
    may see at the instant and must stay exactly that; a name the host already
    supplies is kept over ours.
    """
    merged = Toolbox(list(toolbox.values()))
    for t in model_tools(llm):
        merged.setdefault(t.name, t)
    return merged
