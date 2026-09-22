"""Keep a few instances per group, spread across it.

Statistical power comes from groups — the gate resamples by group — so
thirty-four windows of one match are thirty-four correlated observations bought
at thirty-four times the price of eight. The same holds for a trading day of
one stock or one coin. Thinning drops instances, never groups.
"""

from __future__ import annotations

from typing import Any, TypeVar

T = TypeVar("T")


def thin(instances: list[T], per_group: int, *, key: Any = None) -> list[T]:
    """At most ``per_group`` instances per group, evenly spaced through it.

    Evenly spaced rather than the first N, because the first N of a football
    match are all the opening twenty minutes — the quietest part, before the
    scoreline has done anything a forecast could be wrong about — and the first
    N of a trading day are the open. ``key`` orders instances within a group;
    the default is ``(at, ticker)``, which every move topic's instance has.
    """
    if per_group <= 0:
        return instances
    order = key or (lambda i: (i.at, i.ticker))
    by_group: dict[str, list[T]] = {}
    for i in instances:
        by_group.setdefault(i.group, []).append(i)      # type: ignore[attr-defined]
    out: list[T] = []
    for group in sorted(by_group):
        rows = sorted(by_group[group], key=order)
        if len(rows) <= per_group:
            out.extend(rows)
            continue
        step = len(rows) / per_group
        out.extend(rows[int(i * step)] for i in range(per_group))
    return out


__all__ = ["thin"]
