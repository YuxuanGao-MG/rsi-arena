"""Topics: each one a :class:`~rsi_arena.loop.Task`, and what a run of it defaults to.

Adding one is a package and a :class:`TopicSpec` here. The spec is the seam:
everything the CLI, the workflow and the scripts used to know about Kalshi by
name — which harness file, which benchmark, what a window costs, which models
the search may pick — is a field on it, and ``rsi-arena topic`` prints it so a
shell can read the same answers.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, fields
from typing import Any, Callable

from ..loop import Settings, Task
from .crypto_horizon import CryptoHorizon
from .kalshi_horizon import KalshiHorizon
from .news_equity import NewsEquity


@dataclass(frozen=True)
class TopicSpec:
    """Where a topic's files are and what a run of it costs, before any flag."""

    name: str
    factory: Callable[[Settings], Task]
    harness: str                      # the seed harness
    jev_harness: str                  # the seed for a decisions model, if the topic has one
    benchmark: str
    windows_dir: str                  # the question set, built once and versioned
    runs_dir: str
    per_fixture: int                  # instances kept per group; 0 keeps all
    window_usd: float                 # dollars an instance before anything is measured
    model_choices: tuple
    holdout: int
    audit: int
    max_metric_calls: int
    valset: int
    max_day_usd: float                # what a day of the loop on this topic may spend
    unit: str = "cents"               # the metric's unit, for readers that never load the task

    #: The settings a flag left unset takes from the spec. Everything else on
    #: ``Settings`` keeps the dataclass default whatever the topic.
    FILLS = ("harness", "benchmark", "windows_dir", "runs_dir", "per_fixture", "window_usd",
             "model_choices")

    def fill(self, settings: Settings) -> Settings:
        """Every ``None`` on ``settings`` that this spec has an answer for, answered."""
        for name in self.FILLS:
            if getattr(settings, name, None) is None:
                setattr(settings, name, getattr(self, name))
        return settings

    def to_dict(self) -> dict[str, Any]:
        out = {f.name: getattr(self, f.name) for f in fields(self) if f.name != "factory"}
        out["model_choices"] = list(self.model_choices)
        return out

    def shell(self) -> str:
        """One ``export`` line a workflow can ``eval``."""
        pairs = [("HARNESS", self.harness), ("BENCHMARK", self.benchmark),
                 ("WINDOWS_DIR", self.windows_dir), ("RUNS_DIR", self.runs_dir),
                 ("PER_FIXTURE", self.per_fixture), ("WINDOW_USD", self.window_usd),
                 ("HOLDOUT", self.holdout), ("AUDIT", self.audit),
                 ("MAX_METRIC_CALLS", self.max_metric_calls), ("VALSET", self.valset),
                 ("MODEL_CHOICES", ",".join(self.model_choices)),
                 ("MAX_DAY_USD", self.max_day_usd), ("JEV_HARNESS", self.jev_harness)]
        return "export " + " ".join(f"{k}={shlex.quote(str(v))}" for k, v in pairs)


_defaults = Settings()

TOPICS: dict[str, TopicSpec] = {
    KalshiHorizon.name: TopicSpec(
        name=KalshiHorizon.name,
        factory=KalshiHorizon.from_settings,
        # Read off the settings dataclass rather than restated, so the first
        # topic's spec is by construction what a bare ``Settings()`` has always
        # been.
        harness=_defaults.harness,
        jev_harness="harnesses/horizon-5m-jev.json",
        benchmark=_defaults.benchmark,
        windows_dir=_defaults.windows_dir,
        runs_dir=_defaults.runs_dir,
        per_fixture=_defaults.per_fixture,
        window_usd=_defaults.window_usd,
        model_choices=_defaults.model_choices,
        holdout=_defaults.holdout,
        audit=_defaults.audit,
        max_metric_calls=_defaults.max_metric_calls,
        valset=_defaults.valset,
        # Two generations a day at the measured cold price, with room.
        max_day_usd=100.0,
        unit=KalshiHorizon.metric.unit,
    ),
    NewsEquity.name: TopicSpec(
        name=NewsEquity.name,
        factory=NewsEquity.from_settings,
        # The seed is the decisions-model harness: at two thousandths of a
        # cent a window the whole question set is judged every generation,
        # which is the resolution the Kalshi topic never could buy. The Opus
        # file is the chat-model seed for a model comparison. ``TopicSpec``
        # has one seed field, so the Jev file is it.
        harness="harnesses/news-equity-5m-jev.json",
        jev_harness="harnesses/news-equity-5m-jev.json",
        benchmark="benchmarks/news-2026-09.json",
        windows_dir="benchmarks/windows-news",
        runs_dir="runs/news-equity-5m",
        per_fixture=0,
        window_usd=0.00005,
        model_choices=("typesafe/jev-1.13", "openai/gpt-5-mini"),
        holdout=50,
        audit=30,
        max_metric_calls=2400,
        valset=400,
        max_day_usd=15.0,
        unit=NewsEquity.metric.unit,
    ),
    CryptoHorizon.name: TopicSpec(
        name=CryptoHorizon.name,
        factory=CryptoHorizon.from_settings,
        # The seed the loop starts from is the decisions-model harness: the
        # spec has one slot for the seed and one for the Jev seed, and on this
        # topic they are the same file. The chat-model seed beside it
        # (harnesses/crypto-horizon-1m.json) is there to be named with
        # ``--harness`` when a run wants Opus in the driving seat.
        harness="harnesses/crypto-horizon-1m-jev.json",
        jev_harness="harnesses/crypto-horizon-1m-jev.json",
        benchmark="benchmarks/crypto-2026-09.json",
        windows_dir="benchmarks/windows-crypto",
        runs_dir="runs/crypto-horizon-1m",
        # Twenty-four instants a day across three coins: power comes from days.
        per_fixture=24,
        # A Jev window: two thousandths of a cent, before anything is measured.
        window_usd=0.00005,
        model_choices=("typesafe/jev-1.13", "openai/gpt-5-mini"),
        holdout=30,
        audit=15,
        max_metric_calls=2400,
        valset=600,
        max_day_usd=15.0,
        unit=CryptoHorizon.metric.unit,
    ),
}


def spec_of(topic: str) -> TopicSpec:
    try:
        return TOPICS[topic]
    except KeyError:
        raise KeyError(f"unknown topic {topic!r} (have: {', '.join(TOPICS)})") from None


def load_topic(settings: Settings) -> Task:
    return spec_of(settings.topic).factory(settings)


__all__ = ["TOPICS", "TopicSpec", "load_topic", "spec_of", "KalshiHorizon", "NewsEquity", "CryptoHorizon"]
