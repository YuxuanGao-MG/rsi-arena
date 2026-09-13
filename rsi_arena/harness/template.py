"""``{{placeholder}}`` rendering and a restricted condition evaluator.

State is one flat dict. A placeholder is a dotted path into it: ``{{plan.queries}}``
reads ``state["plan"]["queries"]``. A missing name is a ``KeyError`` naming the
path, because a harness that reads what nothing wrote is a broken harness and
should say so.

Conditions (``until``, ``skip_if``) are Python expressions over the same state,
parsed with ``ast`` and evaluated by hand. Only comparisons, boolean logic,
attribute and index reads, literals and ``len``/``abs``/``min``/``max`` are
allowed. Anything else is a :class:`ConditionError`. An optimizer writes these
strings, and a string an optimizer wrote must not be able to run code.
"""

from __future__ import annotations

import ast
import json
import operator
import re
from typing import Any

_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][\w\.]*)\s*\}\}")


class ConditionError(ValueError):
    """The expression is not one the evaluator will run."""


def lookup(state: dict[str, Any], path: str) -> Any:
    value: Any = state
    for part in path.split("."):
        if isinstance(value, dict):
            if part not in value:
                raise KeyError(path)
            value = value[part]
        elif isinstance(value, list) and part.isdigit():
            index = int(part)
            if index >= len(value):
                raise KeyError(path)
            value = value[index]
        else:
            if not hasattr(value, part):
                raise KeyError(path)
            value = getattr(value, part)
    return value


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return json.dumps(value, default=str, ensure_ascii=False)


def render(template: str, state: dict[str, Any]) -> str:
    return _PLACEHOLDER.sub(lambda m: _text(lookup(state, m.group(1))), template)


def reads(template: str) -> set[str]:
    """Top-level state names a template reads."""
    return {m.group(1).split(".")[0] for m in _PLACEHOLDER.finditer(template or "")}


_COMPARE = {
    ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt,
    ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge,
    ast.In: lambda a, b: a in b, ast.NotIn: lambda a, b: a not in b,
    ast.Is: operator.is_, ast.IsNot: operator.is_not,
}
_BINOP = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
          ast.Div: operator.truediv}
_CALLS = {"len": len, "abs": abs, "min": min, "max": max, "str": str,
          "float": float, "int": int, "bool": bool}


def evaluate(expr: str, state: dict[str, Any]) -> bool:
    try:
        tree = ast.parse(expr.strip(), mode="eval")
    except SyntaxError as exc:
        raise ConditionError(f"bad condition {expr!r}: {exc.msg}") from None
    return bool(_eval(tree.body, state, expr))


def _eval(node: ast.AST, state: dict[str, Any], expr: str) -> Any:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id in ("True", "False", "None"):
            return {"True": True, "False": False, "None": None}[node.id]
        if node.id not in state:
            raise ConditionError(f"condition {expr!r} reads {node.id!r}, which is not in state")
        return state[node.id]
    if isinstance(node, ast.Attribute):
        base = _eval(node.value, state, expr)
        if isinstance(base, dict):
            if node.attr not in base:
                raise ConditionError(f"condition {expr!r}: no key {node.attr!r}")
            return base[node.attr]
        if hasattr(base, node.attr) and not node.attr.startswith("_"):
            return getattr(base, node.attr)
        raise ConditionError(f"condition {expr!r}: no attribute {node.attr!r}")
    if isinstance(node, ast.Subscript):
        base = _eval(node.value, state, expr)
        key = _eval(node.slice, state, expr)
        try:
            return base[key]
        except (KeyError, IndexError, TypeError):
            raise ConditionError(f"condition {expr!r}: bad index {key!r}") from None
    if isinstance(node, ast.BoolOp):
        values = [_eval(v, state, expr) for v in node.values]
        return all(values) if isinstance(node.op, ast.And) else any(values)
    if isinstance(node, ast.UnaryOp):
        value = _eval(node.operand, state, expr)
        if isinstance(node.op, ast.Not):
            return not value
        if isinstance(node.op, ast.USub):
            return -value
        raise ConditionError(f"condition {expr!r}: unsupported operator")
    if isinstance(node, ast.Compare):
        left = _eval(node.left, state, expr)
        for op, comparator in zip(node.ops, node.comparators):
            right = _eval(comparator, state, expr)
            fn = _COMPARE.get(type(op))
            if fn is None:
                raise ConditionError(f"condition {expr!r}: unsupported comparison")
            if not fn(left, right):
                return False
            left = right
        return True
    if isinstance(node, ast.BinOp) and type(node.op) in _BINOP:
        return _BINOP[type(node.op)](_eval(node.left, state, expr), _eval(node.right, state, expr))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _CALLS:
        if node.keywords:
            raise ConditionError(f"condition {expr!r}: keyword arguments are not allowed")
        return _CALLS[node.func.id](*(_eval(a, state, expr) for a in node.args))
    if isinstance(node, (ast.List, ast.Tuple)):
        return [_eval(e, state, expr) for e in node.elts]
    raise ConditionError(f"condition {expr!r}: {type(node).__name__} is not allowed")
