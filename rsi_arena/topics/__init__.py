"""Topics: each one a :class:`~rsi_arena.loop.Task`. Adding one is a package and a line here."""

from __future__ import annotations

from typing import Callable

from ..loop import Settings, Task
from .kalshi_horizon import KalshiHorizon

TOPICS: dict[str, Callable[[Settings], Task]] = {
    KalshiHorizon.name: KalshiHorizon.from_settings,
}


def load_topic(settings: Settings) -> Task:
    try:
        return TOPICS[settings.topic](settings)
    except KeyError:
        raise KeyError(f"unknown topic {settings.topic!r} (have: {', '.join(TOPICS)})") from None


__all__ = ["TOPICS", "load_topic", "KalshiHorizon"]
