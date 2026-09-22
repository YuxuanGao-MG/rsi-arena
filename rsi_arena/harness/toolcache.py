"""Answers of frozen tools, on disk, and the one rule for when to keep them.

Lived in ``kalshi/replay.py`` until a second and third topic needed the same
thing: a box of tools bounded at an instant, whose answers at a past instant
never change and so need fetching once, ever. Nothing in here knows what a
tool answers about; it knows only that history is immutable and a live market
is not.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable

from .tools import ToolResult


class ToolCache:
    """Answers of frozen tools, on disk. History does not change.

    Keyed on ``{tool, at, **args}`` with no TTL and no eviction, which is sound
    exactly as far as that sentence is: a settled match's candles at a past
    instant are the same candles forever. On a market still trading it is false,
    and nothing in the key says so. Live callers take :data:`NO_CACHE`.
    """

    def __init__(self, root: str | Path | None) -> None:
        self.root = Path(root) if root else None

    def get(self, key: dict[str, Any]) -> dict[str, Any] | None:
        path = self._path(key)
        if path is not None and path.exists():
            return json.loads(path.read_text())
        return None

    def put(self, key: dict[str, Any], value: dict[str, Any]) -> None:
        path = self._path(key)
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, default=str))

    def _path(self, key: dict[str, Any]) -> Path | None:
        if self.root is None:
            return None
        digest = hashlib.sha256(json.dumps(key, sort_keys=True, default=str).encode()).hexdigest()
        return self.root / key.get("tool", "tool") / f"{digest}.json"


class _NoCache(ToolCache):
    """A cache that refuses to remember, for a market that is still moving.

    The live collector reached the same tools through the plain cache and was
    safe only by accident: ``at`` is ``datetime.now()`` to the microsecond, so
    two sweeps never landed on one key. Nobody chose that, nothing tested it,
    and one caller rounding its instant to the minute would have served a
    ten-minute-old book to a five-minute forecast. Refusing here is cheap;
    finding that bug in the traces afterwards would not be.
    """

    def __init__(self) -> None:
        super().__init__(None)

    def get(self, key: dict[str, Any]) -> dict[str, Any] | None:
        return None

    def put(self, key: dict[str, Any], value: dict[str, Any]) -> None:
        return None


#: Pass this rather than ``None`` when the instant is now. ``None`` also caches
#: nothing, but it reads as an omission; this reads as a decision.
NO_CACHE = _NoCache()


def cached(cache: ToolCache, stamp: str, tool: str, args: dict[str, Any],
           compute: Callable[[], ToolResult]) -> ToolResult:
    """``compute()`` once per ``(tool, stamp, args)``; the cache answers after that.

    The shape every frozen toolbox needs and used to write inline: key the
    answer on the instant and the arguments, store the four fields of a
    :class:`ToolResult`, and hand back the same result whether it was computed
    or read. ``stamp`` is the instant as a string so a box built at the same
    instant twice keys the same way.
    """
    key = {"tool": tool, "at": stamp, **args}
    hit = cache.get(key)
    if hit is not None:
        return ToolResult(ok=hit["ok"], text=hit["text"], data=hit["data"], error=hit.get("error"))
    out = compute()
    cache.put(key, {"ok": out.ok, "text": out.text, "data": out.data, "error": out.error})
    return out


__all__ = ["ToolCache", "NO_CACHE", "cached"]
