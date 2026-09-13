"""Tools: what a harness may call, bound by name.

A harness names its tools; the host supplies a :class:`Toolbox` with those
names. That indirection is what lets the same harness run against the live
exchange or against history frozen at an instant, and it is what stops an
optimizer from reaching a tool the host did not put in the box.

A tool never raises. A failure is a :class:`ToolResult` with ``ok=False`` and
an error the model can read.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class ToolResult:
    ok: bool
    text: str                      # what the model reads
    data: Any = None               # what code indexes into
    error: str | None = None

    @classmethod
    def failed(cls, reason: str) -> "ToolResult":
        return cls(ok=False, text=f"unavailable: {reason}", data={"error": reason}, error=reason)

    def for_state(self) -> Any:
        return self.data if self.ok else {"error": self.error}


class Tool:
    """Subclass and implement :meth:`call`, or use :func:`tool` on a function."""

    name: str = ""
    description: str = ""
    parameters: dict[str, Any] = {"type": "object", "properties": {}}

    def call(self, **args: Any) -> ToolResult:
        raise NotImplementedError

    def safe_call(self, **args: Any) -> ToolResult:
        try:
            out = self.call(**args)
        except Exception as exc:  # noqa: BLE001 - surfaced to the model
            return ToolResult.failed(f"{type(exc).__name__}: {exc}")
        if isinstance(out, ToolResult):
            return out
        text = out if isinstance(out, str) else json.dumps(out, default=str)[:20000]
        return ToolResult(ok=True, text=text, data=out)

    def schema(self) -> dict[str, Any]:
        return {"type": "function", "function": {
            "name": self.name, "description": self.description, "parameters": self.parameters}}


@dataclass
class FunctionTool(Tool):
    name: str = ""
    description: str = ""
    parameters: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {}})
    fn: Callable[..., Any] = lambda **_: None

    def call(self, **args: Any) -> ToolResult:
        return self.fn(**args)


def tool(name: str, description: str, parameters: dict[str, Any] | None = None):
    def wrap(fn: Callable[..., Any]) -> FunctionTool:
        return FunctionTool(name=name, description=description,
                            parameters=parameters or {"type": "object", "properties": {}}, fn=fn)
    return wrap


class Toolbox(dict):
    """Tools by name. A missing name says what is available."""

    def __init__(self, tools: list[Tool] | None = None) -> None:
        super().__init__((t.name, t) for t in (tools or []))

    def __missing__(self, name: str) -> Tool:
        raise KeyError(f"unknown tool {name!r} (have: {', '.join(sorted(self)) or 'none'})")

    def names(self) -> list[str]:
        return list(self)

    def schemas(self, only: list[str] | None = None) -> list[dict[str, Any]]:
        names = list(self) if only is None or only == ["*"] else only
        return [self[n].schema() for n in names]

    def describe(self) -> str:
        return "\n".join(f"- {t.name}: {t.description}" for t in self.values())
