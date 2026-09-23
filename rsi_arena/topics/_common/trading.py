"""What a topic says about a trade once the book has replayed it.

A rollout is scored before any book exists, so the "Book:" line cannot be
written by ``_feedback`` at scoring time; it is appended when the replay
attaches its record to the outcome, and every topic appends the same line.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any


def book_line(record: dict[str, Any]) -> str:
    """``Book: open_long 5% -> +120 USD (horizon)``, from a replay record."""
    action, size = record.get("action", "hold"), float(record.get("size") or 0.0)
    pnl = float(record.get("pnl_usd") or 0.0)
    reason = record.get("reason") or ("refused" if record.get("refused") else "no trade")
    return f"Book: {action} {size:.0%} -> {pnl:+.0f} USD ({reason})"


def with_trade(outcome: Any, record: dict[str, Any]) -> Any:
    """The outcome with the record under ``details["trade"]`` and the line
    on the end of its feedback, so the rewriter reads what the forecast was
    worth as money beside what it was worth as skill."""
    details = {**outcome.details, "trade": dict(record)}
    feedback = outcome.feedback.rstrip()
    return replace(outcome, details=details, feedback=f"{feedback} {book_line(record)}")


__all__ = ["book_line", "with_trade"]
