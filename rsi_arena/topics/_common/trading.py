"""What a topic says about a trade once the book has replayed it.

A rollout is scored before any book exists, so the "Book:" line cannot be
written by ``_feedback`` at scoring time; it is appended when the replay
attaches its record to the outcome, and every topic appends the same line.
The line and the attach live in ``rsi_arena.trading`` (the loop attaches
records too, and may not import a topic); this is the topics' name for them.
"""

from __future__ import annotations

from ...trading import book_line, with_trade

__all__ = ["book_line", "with_trade"]
